"""One-off media migration: copy the on-disk library up to Cloudflare R2.

Copies every episode audio file and thumbnail under DATA_DIR/audio/<channel_id>/
and DATA_DIR/thumbnails/<channel_id>/ to the bucket as
audio/<channel_id>/<file> and thumbnails/<channel_id>/<file>, so STORAGE=r2 can
be switched on afterwards with nothing looking "missing".

Run it inside the container (scripts/ is in the image, and compose passes the
R2_* vars through):

    docker compose exec app python scripts/migrate_to_r2.py            # DRY RUN: plan only
    docker compose exec app python scripts/migrate_to_r2.py --apply    # upload, then verify
    docker compose exec app python scripts/migrate_to_r2.py --data-dir /data [--apply]

R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET are
required regardless of STORAGE (run this while STORAGE=local).

Safety:
  - Dry run is the default and makes ONLY read-only list calls.
  - Local files are opened read-only. This script NEVER deletes, moves or
    modifies anything on local disk (the local library stays as the backup),
    and never deletes anything in the bucket.
  - Idempotent: objects already present with the same size are skipped, so a
    re-run only uploads what's missing (a size mismatch is re-uploaded).
  - Uploads send Content-MD5 (R2 rejects a corrupted body); --apply then
    re-lists the bucket and checks every object's size and ETag against the
    local file's MD5.
  - Only safe names are uploaded (valid channel id, safe basename, allow-listed
    extension); yt-dlp leftovers (.part, .ytdl, .tmp) and stray entries are
    listed as ignored.
  - Prints the bucket name only — never credentials, the account id or the
    endpoint.

Exit codes: 0 ok, 1 upload failures or verify mismatches, 2 bad arguments or
missing R2_* variables.
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, storage  # noqa: E402

_CHANNEL_ID_RE = storage._CHANNEL_ID_RE


def _fmt_bytes(n: int) -> str:
    value, unit = float(n or 0), "B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="migrate_to_r2.py",
        description="Copy the local audio/thumbnail library to the R2 bucket "
                    "(dry run unless --apply). Never deletes anything.",
    )
    parser.add_argument("--apply", action="store_true",
                        help="actually upload, then verify every object")
    parser.add_argument("--data-dir", default=config.DATA_DIR,
                        help="data directory holding audio/ and thumbnails/ "
                             "(default: %(default)s)")
    return parser.parse_args(argv)


def scan(data_dir: str):
    """Return (files, ignored): files are (path, ref, size); ignored are labels."""
    files, ignored = [], []
    for kind in storage.KINDS:
        base = os.path.join(data_dir, kind)
        if not os.path.isdir(base):
            continue
        for cid in sorted(os.listdir(base)):
            cdir = os.path.join(base, cid)
            if os.path.islink(cdir) or not os.path.isdir(cdir):
                ignored.append(f"{kind}/{cid} (not a channel directory)")
                continue
            if not _CHANNEL_ID_RE.match(cid):
                ignored.append(f"{kind}/{cid}/ (invalid channel id)")
                continue
            for entry in sorted(os.scandir(cdir), key=lambda e: e.name):
                label = f"{kind}/{cid}/{entry.name}"
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    ignored.append(f"{label} (not a regular file)")
                    continue
                try:
                    ref = storage.media_ref(kind, base, cid, entry.name)
                    storage.validate_put_key(ref.key)
                except ValueError:
                    ignored.append(f"{label} (not an uploadable media name)")
                    continue
                files.append((entry.path, ref, entry.stat(follow_symlinks=False).st_size))
    return files, ignored


def _md5_hex(path: str) -> str:
    md5 = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def migrate(data_dir: str, driver, apply: bool = False, log=print) -> dict:
    files, ignored = scan(data_dir)
    result = {
        "local_files": len(files), "local_bytes": sum(size for _, _, size in files),
        "ignored": len(ignored), "planned": 0, "skipped": 0, "uploaded": 0,
        "replaced": 0, "failed": 0, "verified": 0, "mismatched": 0,
    }
    log(f"{'APPLY' if apply else 'DRY RUN'}: {data_dir} -> bucket {driver.bucket}")
    for label in ignored:
        log(f"  ignore   {label}")

    driver.refresh()  # one listing: what's already in the bucket
    plan = []
    for path, ref, size in files:
        meta = driver.peek_meta(ref)
        if meta and meta["size"] == size:
            result["skipped"] += 1
            continue
        action = "replace" if meta else "upload"
        plan.append((action, path, ref, size))
        log(f"  {action:<8} {ref.key} ({_fmt_bytes(size)})")
    result["planned"] = len(plan)

    if apply:
        for action, path, ref, _size in plan:
            try:
                driver.put_file(path, ref)
                result["uploaded" if action == "upload" else "replaced"] += 1
            except Exception as exc:  # noqa: BLE001 — count it, keep going
                result["failed"] += 1
                log(f"  FAILED   {ref.key}: {exc}")

        driver.refresh()
        for path, ref, size in files:
            meta = driver.peek_meta(ref)
            etag = (meta or {}).get("etag") or ""
            ok = bool(meta) and meta["size"] == size and (
                "-" in etag or not etag or etag == _md5_hex(path))
            if ok:
                result["verified"] += 1
            else:
                result["mismatched"] += 1
                log(f"  MISMATCH {ref.key}")

    log(f"local files:      {result['local_files']} ({_fmt_bytes(result['local_bytes'])})")
    log(f"ignored:          {result['ignored']}")
    log(f"already present:  {result['skipped']}")
    if apply:
        log(f"uploaded:         {result['uploaded']}")
        log(f"replaced:         {result['replaced']}")
        log(f"failed:           {result['failed']}")
        log(f"verified:         {result['verified']}")
        log(f"verify mismatches: {result['mismatched']}")
    else:
        log(f"would upload:     {result['planned']}  (re-run with --apply)")
    return result


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cfg = storage.r2_config_from_env(os.environ)
    except storage.StorageConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        driver = storage.R2Driver(**cfg, staging_root=None)
        result = migrate(args.data_dir, driver, apply=args.apply)
    except storage.StorageError as exc:  # already scrubbed of secrets
        print(f"R2 error: {exc}", file=sys.stderr)
        return 1
    return 1 if (result["failed"] or result["mismatched"]) else 0


if __name__ == "__main__":
    sys.exit(main())
