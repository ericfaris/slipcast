"""Media storage: one interface, two drivers, selected by STORAGE=local|r2.

    local  audio + thumbnails on the filesystem under DATA_DIR (the original
           behaviour, kept byte-for-byte).
    r2     audio + thumbnails in a private Cloudflare R2 bucket, through boto3
           (imported lazily, so local mode never loads it).

Identity rule: persisted data never changes between modes. ``episodes.filename``
and ``episodes.thumbnail`` stay plain basenames and ``channel_id`` stays the
directory/prefix name. Every operation takes a ``MediaRef`` built by
``media_ref(kind, base_dir, channel_id, name)``, which carries both the logical
object key (``<kind>/<channel_id>/<name>``, used by r2) and the filesystem path
(``<base_dir>/<channel_id>/<name>``, used by local). Callers pass their *own*
module's AUDIO_DIR/THUMBNAIL_DIR as ``base_dir``, so per-module test patches of
those globals keep working. Rollback is flipping STORAGE back to local.

Index: the r2 driver never HEADs an object to answer "does it exist / how big
is it". It keeps an in-memory index built from one paginated ListObjectsV2 per
prefix, refreshed periodically (``refresh_index_job``) and updated in place on
its own puts/deletes. An index that has never loaded fails *closed*
(``StorageUnavailable``) for anything that decides to download or delete —
otherwise one transient list failure would make every episode look missing and
re-download the whole library. Only cosmetic call sites swallow StorageError.

Secrets: STORAGE and R2_* are read from the environment here, never stored in
``app.config``. Error messages name missing variables, never values, and every
botocore error is re-raised as a scrubbed ``StorageError`` (account id, key id,
secret and endpoint host replaced) ``from None`` so the original traceback —
which can embed the endpoint URL — never reaches a log.

Always call ``get()`` per operation; never capture the driver at import time
(tests swap drivers per test).

This module imports only the stdlib, app.config and app.safety — app.database
imports it, so importing app.database/app.downloader here would be a cycle.
"""
import base64
import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import NamedTuple

from app import config
from app.safety import is_safe_media_name

logger = logging.getLogger(__name__)

KINDS = ("audio", "thumbnails")
BACKUP_PREFIX = "backups/"
R2_REQUIRED_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")

# Same pattern as downloader._CHANNEL_ID_RE / main._CHANNEL_ID_RE.
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SEGMENT = r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}"
_PUT_KEY_RULES = (
    (re.compile(rf"^audio/[A-Za-z0-9_-]{{1,64}}/{_SEGMENT}$"),
     frozenset({"mp3", "opus", "m4a", "webm", "ogg"})),
    (re.compile(rf"^thumbnails/[A-Za-z0-9_-]{{1,64}}/{_SEGMENT}$"),
     frozenset({"jpg", "jpeg", "png", "webp"})),
    (re.compile(rf"^backups/{_SEGMENT}$"),
     frozenset({"db", "sqlite", "gz"})),
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    # yt-dlp's opus output is an Ogg container — matches feed._ENCLOSURE_TYPES.
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "db": "application/vnd.sqlite3",
    "sqlite": "application/vnd.sqlite3",
    "gz": "application/gzip",
}

# After a failed first index load, don't retry on every request — that would
# hammer R2 (and stall each request on a network timeout) during an outage.
_INDEX_RETRY_BACKOFF_SECONDS = 30
_STAGING_PREFIX = "dl-"
_MD5_CHUNK = 1024 * 1024

# Module clock for the backoff, so tests can move time without sleeping.
_clock = time.monotonic


class StorageConfigError(RuntimeError):
    """STORAGE / R2_* are missing or invalid. Raised at startup (fail fast)."""


class StorageError(RuntimeError):
    """A storage operation failed. The message is always scrubbed of secrets."""


class StorageUnavailable(StorageError):
    """The r2 index could not be loaded, so existence can't be answered."""


class MediaRef(NamedTuple):
    kind: str         # "audio" | "thumbnails"
    channel_id: str
    name: str         # basename, e.g. "<video_id>.mp3", "channel.jpg"
    local_path: str   # absolute path local mode uses (caller-built)

    @property
    def key(self) -> str:
        return f"{self.kind}/{self.channel_id}/{self.name}"


def media_ref(kind: str, base_dir: str, channel_id: str, name: str) -> MediaRef:
    """Build a validated MediaRef. Raises ValueError on any unsafe component."""
    if kind not in KINDS:
        raise ValueError(f"Invalid media kind: {kind!r}")
    if not isinstance(channel_id, str) or not _CHANNEL_ID_RE.match(channel_id):
        raise ValueError(f"Invalid channel_id: {channel_id!r}")
    if not is_safe_media_name(name):
        raise ValueError(f"Unsafe media name: {name!r}")
    return MediaRef(kind, channel_id, name, os.path.join(base_dir, channel_id, name))


def validate_put_key(key: str) -> None:
    """Raise ValueError unless ``key`` is one we are willing to write.

    Called before every put: a single fixed prefix per kind, no extra path
    segments, no leading slash, backslash, control character or ``..``, and an
    allow-listed extension for that prefix.
    """
    if (not isinstance(key, str) or not key or key.startswith("/") or "\\" in key
            or ".." in key or _CONTROL_CHARS_RE.search(key)):
        raise ValueError(f"Refusing unsafe object key: {key!r}")
    ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    for pattern, exts in _PUT_KEY_RULES:
        if pattern.match(key):
            if ext in exts:
                return
            break
    raise ValueError(f"Refusing unsafe object key: {key!r}")


def content_type_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def r2_config_from_env(env) -> dict:
    """The four R2 settings, stripped. Names (never values) of missing ones."""
    values = {var: (env.get(var) or "").strip() for var in R2_REQUIRED_VARS}
    missing = sorted(var for var, value in values.items() if not value)
    if missing:
        raise StorageConfigError(
            "STORAGE=r2 but required env var(s) are missing: "
            f"{', '.join(missing)} (see .env.example)"
        )
    return {
        "account_id": values["R2_ACCOUNT_ID"],
        "access_key_id": values["R2_ACCESS_KEY_ID"],
        "secret_access_key": values["R2_SECRET_ACCESS_KEY"],
        "bucket": values["R2_BUCKET"],
    }


def _default_staging_root() -> str:
    return os.path.join(tempfile.gettempdir(), "slipcast-staging")


def config_from_env(env) -> dict:
    mode = (env.get("STORAGE") or "").strip().lower()
    if mode in ("", "local"):
        return {"mode": "local"}
    if mode == "r2":
        return {
            "mode": "r2",
            **r2_config_from_env(env),
            "staging_root": (env.get("STORAGE_STAGING_DIR") or "").strip()
                            or _default_staging_root(),
            "presign_expiry": config.PRESIGN_EXPIRY_SECONDS,
        }
    raw = (env.get("STORAGE") or "").strip()[:20]
    raise StorageConfigError(f'Unknown STORAGE "{raw}" — use "local" or "r2"')


def _md5_file(path: str) -> tuple[int, "hashlib._Hash"]:
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_MD5_CHUNK)
            if not chunk:
                break
            md5.update(chunk)
            size += len(chunk)
    return size, md5


# ---------------------------------------------------------------------------
# local driver
# ---------------------------------------------------------------------------

class LocalDriver:
    """Today's filesystem behaviour, on ``ref.local_path``."""

    mode = "local"

    def exists(self, ref: MediaRef) -> bool:
        return os.path.exists(ref.local_path)

    def size(self, ref: MediaRef) -> int | None:
        try:
            return os.path.getsize(ref.local_path)
        except OSError:
            return None

    def mtime(self, ref: MediaRef) -> float | None:
        try:
            return os.path.getmtime(ref.local_path)
        except OSError:
            return None

    def peek_size(self, ref: MediaRef) -> None:
        return None

    def put_file(self, src: str, ref: MediaRef) -> int:
        validate_put_key(ref.key)
        if os.path.realpath(src) != os.path.realpath(ref.local_path):
            os.makedirs(os.path.dirname(ref.local_path), exist_ok=True)
            shutil.copyfile(src, ref.local_path)
        return os.path.getsize(ref.local_path)

    def remove(self, ref: MediaRef) -> bool:
        if os.path.exists(ref.local_path):
            os.remove(ref.local_path)
            return True
        return False

    @contextlib.contextmanager
    def staging(self, local_dir: str):
        # yt-dlp keeps writing straight into the real directory, exactly as
        # before; nothing is moved or cleaned up on exit.
        os.makedirs(local_dir, exist_ok=True)
        yield local_dir

    def ensure_index(self) -> bool:
        return True

    def refresh(self) -> None:
        return None

    def health_info(self) -> dict:
        return {"mode": "local", "ok": True}


# ---------------------------------------------------------------------------
# r2 driver
# ---------------------------------------------------------------------------

def _build_client(account_id: str, access_key_id: str, secret_access_key: str):
    """A boto3 S3 client for R2. boto3 is imported here, never at module level."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            # Newer botocore sends CRC checksum headers by default, which R2
            # rejects. Puts carry an explicit ContentMD5 instead.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # Mandatory: an unbounded network wait inside poll_all wedges the
            # scheduler with no error and no log (the v1.10.0 outage — see
            # downloader._base_ydl_opts).
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _is_same_or_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _check_staging_root(staging_root: str) -> None:
    """Staging dirs are rmtree'd — never let them overlap persisted data."""
    resolved = os.path.realpath(staging_root)
    data_dir = os.path.realpath(config.DATA_DIR)
    protected = [os.path.realpath(p) for p in
                 (config.DATA_DIR, config.AUDIO_DIR, config.THUMBNAIL_DIR, config.BACKUP_DIR)]
    if (any(_is_same_or_inside(resolved, p) for p in protected)
            or _is_same_or_inside(data_dir, resolved)):
        raise StorageConfigError(
            "STORAGE_STAGING_DIR must not be DATA_DIR, one of its media/backup "
            "dirs, anything inside them, or an ancestor of DATA_DIR"
        )


class R2Driver:
    mode = "r2"

    def __init__(self, *, bucket: str, account_id: str = "", access_key_id: str = "",
                 secret_access_key: str = "", client=None, staging_root: str | None = None,
                 presign_expiry: int = 3600):
        self.bucket = bucket
        self._account_id = account_id
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self.staging_root = staging_root or _default_staging_root()
        _check_staging_root(self.staging_root)
        self.presign_expiry = presign_expiry
        self._client = client if client is not None else _build_client(
            account_id, access_key_id, secret_access_key)

        self._lock = threading.Lock()          # guards the fields below; never held across I/O
        self._refresh_lock = threading.Lock()  # serialises refreshes / first loads
        self._index: dict[str, dict] = {}
        self._pending: list | None = None
        self._loaded = False
        self._last_error: str | None = None
        self._last_error_at: float | None = None
        self._last_refresh_at: float | None = None

    # --- secrets / client wrapper ------------------------------------------

    def _scrub(self, message: str) -> str:
        secrets_ = []
        if self._account_id:
            secrets_.append(f"{self._account_id}.r2.cloudflarestorage.com")
        secrets_ += [self._account_id, self._access_key_id, self._secret_access_key]
        for s in secrets_:
            if s:
                message = message.replace(s, "***")
        return message[:300]

    def _call(self, op: str, **kwargs):
        try:
            return getattr(self._client, op)(**kwargs)
        except Exception as exc:  # noqa: BLE001 — every client error is re-raised scrubbed
            raise StorageError(self._scrub(f"{op} failed: {exc}")) from None

    # --- index ---------------------------------------------------------------

    def _list_prefix(self, prefix: str):
        """Yield every object under ``prefix`` straight from R2 (paginated)."""
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._call("list_objects_v2", **kwargs)
            for obj in resp.get("Contents") or []:
                yield obj
            if not resp.get("IsTruncated"):
                return
            token = resp.get("NextContinuationToken")
            if not token:
                return

    @staticmethod
    def _meta(obj) -> dict:
        lm = obj.get("LastModified")
        if isinstance(lm, datetime):
            last_modified = lm.timestamp()
        else:
            last_modified = float(lm or 0)
        etag = obj.get("ETag")
        return {"size": int(obj.get("Size") or 0),
                "etag": etag.strip('"') if etag else None,
                "last_modified": last_modified}

    @staticmethod
    def _apply(index: dict, op: str, key: str, meta) -> None:
        if op == "put":
            index[key] = meta
        else:
            index.pop(key, None)

    def _record(self, op: str, key: str, meta=None) -> None:
        """Apply a put/delete to the index; also queue it if a list is in flight,
        so the (older) list result swapped in afterwards can't erase it."""
        if not key.startswith(tuple(f"{k}/" for k in KINDS)):
            return
        with self._lock:
            self._apply(self._index, op, key, meta)
            if self._pending is not None:
                self._pending.append((op, key, meta))

    def refresh(self) -> None:
        with self._refresh_lock:
            self._refresh_locked()

    def _refresh_locked(self) -> None:
        with self._lock:
            self._pending = []
        try:
            new_index: dict[str, dict] = {}
            for kind in KINDS:
                for obj in self._list_prefix(f"{kind}/"):
                    new_index[obj["Key"]] = self._meta(obj)
        except Exception as exc:  # noqa: BLE001
            err = exc if isinstance(exc, StorageError) else StorageError(self._scrub(str(exc)))
            with self._lock:
                self._pending = None
                self._last_error = str(err)
                self._last_error_at = _clock()
            raise err from None
        with self._lock:
            for op, key, meta in self._pending or []:
                self._apply(new_index, op, key, meta)
            self._index = new_index
            self._loaded = True
            self._last_error = None
            self._last_error_at = None
            self._last_refresh_at = time.time()
            self._pending = None

    def ensure_index(self) -> bool:
        if self._loaded:
            return True
        with self._refresh_lock:
            if self._loaded:  # another thread's first load finished while we waited
                return True
            if (self._last_error_at is not None
                    and _clock() - self._last_error_at < _INDEX_RETRY_BACKOFF_SECONDS):
                return False
            try:
                self._refresh_locked()
                return True
            except StorageError:
                return False

    def _require_loaded(self) -> None:
        if not self.ensure_index():
            raise StorageUnavailable("media storage (R2) is unavailable")

    def _lookup(self, key: str) -> dict | None:
        self._require_loaded()
        with self._lock:
            return self._index.get(key)

    # --- point operations ----------------------------------------------------

    def exists(self, ref: MediaRef) -> bool:
        return self._lookup(ref.key) is not None

    def size(self, ref: MediaRef) -> int | None:
        meta = self._lookup(ref.key)
        return meta["size"] if meta else None

    def mtime(self, ref: MediaRef) -> float | None:
        meta = self._lookup(ref.key)
        return meta["last_modified"] if meta else None

    def peek_size(self, ref: MediaRef) -> int | None:
        """Index size or None. Never loads, never does I/O, never raises."""
        with self._lock:
            meta = self._index.get(ref.key)
        return meta["size"] if meta else None

    def peek_meta(self, ref: MediaRef) -> dict | None:
        """A copy of the index entry ({size, etag, last_modified}) or None.
        Like peek_size: no load, no I/O, never raises."""
        with self._lock:
            meta = self._index.get(ref.key)
        return dict(meta) if meta else None

    def _put_key(self, src: str, key: str) -> int:
        validate_put_key(key)
        size, md5 = _md5_file(src)
        with open(src, "rb") as body:
            resp = self._call(
                "put_object", Bucket=self.bucket, Key=key, Body=body,
                ContentLength=size, ContentType=content_type_for(key.rsplit("/", 1)[-1]),
                ContentMD5=base64.b64encode(md5.digest()).decode("ascii"),
            )
        etag = ((resp or {}).get("ETag") or "").strip('"') or md5.hexdigest()
        self._record("put", key, {"size": size, "etag": etag, "last_modified": time.time()})
        return size

    def put_file(self, src: str, ref: MediaRef) -> int:
        return self._put_key(src, ref.key)

    def remove(self, ref: MediaRef) -> bool:
        self._call("delete_object", Bucket=self.bucket, Key=ref.key)  # idempotent
        with self._lock:
            was_indexed = ref.key in self._index
        self._record("delete", ref.key)
        return was_indexed

    # --- r2-only listing -----------------------------------------------------

    def _snapshot(self) -> dict:
        self._require_loaded()
        with self._lock:
            return dict(self._index)

    def list_channel(self, kind: str, channel_id: str) -> list[tuple[str, int, float]]:
        prefix = f"{kind}/{channel_id}/"
        out = []
        for key, meta in self._snapshot().items():
            if key.startswith(prefix):
                name = key[len(prefix):]
                if name and "/" not in name:
                    out.append((name, meta["size"], meta["last_modified"]))
        return sorted(out)

    def channel_ids(self, kind: str) -> set[str]:
        prefix = f"{kind}/"
        ids = set()
        for key in self._snapshot():
            if key.startswith(prefix):
                parts = key[len(prefix):].split("/")
                if len(parts) == 2 and parts[0]:
                    ids.add(parts[0])
        return ids

    def delete_channel(self, channel_id: str) -> int:
        """Delete every object under both prefixes for one channel.

        Lists R2 directly rather than trusting the index, so a stale index can't
        leave objects behind.
        """
        if not _CHANNEL_ID_RE.match(channel_id or ""):
            raise ValueError(f"Invalid channel_id: {channel_id!r}")
        deleted = 0
        for kind in KINDS:
            keys = [obj["Key"] for obj in self._list_prefix(f"{kind}/{channel_id}/")]
            for key in keys:
                self._call("delete_object", Bucket=self.bucket, Key=key)
                self._record("delete", key)
                deleted += 1
        return deleted

    # --- backups ---------------------------------------------------------------

    def put_backup(self, path: str) -> str:
        key = BACKUP_PREFIX + os.path.basename(path)
        self._put_key(path, key)
        return key

    def prune_backups(self, retain: int, prefix: str = "episodes-") -> list[str]:
        """Delete all but the ``retain`` newest ``backups/<prefix>*.db`` objects.

        The timestamped names sort lexicographically = chronologically, exactly
        like database.prune_backups(). Other prefixes (pre-pk-migration-*) are
        never touched.
        """
        keys = [obj["Key"] for obj in self._list_prefix(BACKUP_PREFIX + prefix)
                if obj["Key"].endswith(".db")]
        deleted = []
        for key in sorted(keys, reverse=True)[retain:]:
            self._call("delete_object", Bucket=self.bucket, Key=key)
            deleted.append(key)
        return deleted

    # --- serving / staging / health -------------------------------------------

    def presigned_url(self, ref: MediaRef, method: str = "GET") -> str:
        # A presigned GET signature is invalid for a HEAD request.
        client_method = "head_object" if method.upper() == "HEAD" else "get_object"
        return self._call(
            "generate_presigned_url", ClientMethod=client_method,
            Params={"Bucket": self.bucket, "Key": ref.key}, ExpiresIn=self.presign_expiry,
        )

    @contextlib.contextmanager
    def staging(self, local_dir=None):
        # One fresh dir per download so concurrent downloads never collide;
        # always removed, whatever happens inside the block.
        os.makedirs(self.staging_root, exist_ok=True)
        stage = tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self.staging_root)
        try:
            yield stage
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def health_info(self) -> dict:
        """Cached state only — never does I/O."""
        with self._lock:
            refreshed = (datetime.fromtimestamp(self._last_refresh_at, tz=timezone.utc).isoformat()
                         if self._last_refresh_at else None)
            return {
                "mode": "r2",
                "bucket": self.bucket,
                "ok": self._loaded and not self._last_error,
                "index_loaded": self._loaded,
                "object_count": len(self._index),
                "last_refresh_at": refreshed,
                "last_error": self._last_error,
            }


# ---------------------------------------------------------------------------
# module singleton
# ---------------------------------------------------------------------------

_driver = None
_driver_lock = threading.Lock()


def _driver_from_config(cfg: dict):
    if cfg["mode"] == "local":
        return LocalDriver()
    return R2Driver(
        bucket=cfg["bucket"], account_id=cfg["account_id"],
        access_key_id=cfg["access_key_id"], secret_access_key=cfg["secret_access_key"],
        staging_root=cfg["staging_root"], presign_expiry=cfg["presign_expiry"],
    )


def init(env=None):
    """Build and install the driver from env. Raises StorageConfigError."""
    global _driver
    drv = _driver_from_config(config_from_env(os.environ if env is None else env))
    with _driver_lock:
        _driver = drv
    return drv


def get():
    """The active driver, initialised from os.environ on first use."""
    drv = _driver
    if drv is not None:
        return drv
    with _driver_lock:
        if _driver is not None:
            return _driver
    return init()


def _set_driver(drv) -> None:
    global _driver
    _driver = drv


def _reset() -> None:
    global _driver
    _driver = None


def sweep_staging(root: str | None, max_age: float = 3600) -> int:
    """Best-effort removal of stale ``dl-*`` dirs left by a crashed download."""
    if not root or not os.path.isdir(root):
        return 0
    removed = 0
    now = time.time()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            if (entry.name.startswith(_STAGING_PREFIX)
                    and entry.is_dir(follow_symlinks=False)
                    and now - entry.stat(follow_symlinks=False).st_mtime > max_age):
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def refresh_index_job() -> None:
    """Scheduler entry: rebuild the r2 index. Never raises."""
    try:
        get().refresh()
    except StorageError as exc:
        logger.warning("Media storage index refresh failed: %s", exc)
