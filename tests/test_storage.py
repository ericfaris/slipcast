"""Unit tests for app/storage.py: refs/keys, config, both drivers, the r2 index."""
import base64
import hashlib
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import config, storage
from fake_s3 import FakeS3

CID = "UCabc12345678901234567890"


# --- media_ref -----------------------------------------------------------------

def test_media_ref_builds_key_and_local_path(tmp_path):
    ref = storage.media_ref("audio", str(tmp_path), CID, "abc123def45.mp3")
    assert ref.key == f"audio/{CID}/abc123def45.mp3"
    assert ref.local_path == os.path.join(str(tmp_path), CID, "abc123def45.mp3")
    assert storage.media_ref("thumbnails", "/x", CID, "channel.jpg").key == f"thumbnails/{CID}/channel.jpg"


@pytest.mark.parametrize("kind,cid,name", [
    ("video", CID, "a.mp3"),
    ("audio", "../x", "a.mp3"),
    ("audio", "a/b", "a.mp3"),
    ("audio", "", "a.mp3"),
    ("audio", CID, "../x.mp3"),
    ("audio", CID, ".hidden"),
    ("audio", CID, "a/b.mp3"),
    ("audio", CID, None),
])
def test_media_ref_rejects_unsafe(kind, cid, name):
    with pytest.raises(ValueError):
        storage.media_ref(kind, "/data/audio", cid, name)


# --- validate_put_key / content types ----------------------------------------------

@pytest.mark.parametrize("key", [
    f"audio/{CID}/v.mp3",
    f"audio/{CID}/-abc_DEF12.opus",
    f"thumbnails/{CID}/channel.jpg",
    "backups/episodes-20261002-030000.db",
])
def test_validate_put_key_accepts(key):
    storage.validate_put_key(key)


@pytest.mark.parametrize("key", [
    f"/audio/{CID}/v.mp3",            # leading slash
    "audio/../x.mp3",                 # ..
    "audio/UC/sub/v.mp3",             # extra segment
    "audio/UC/v.exe",                 # extension
    "thumbnails/UC/v.mp3",            # wrong extension for the prefix
    "other/x.mp3",                    # prefix
    "audio/UC/v\x00.mp3",             # control char
    "audio/UC/v\n.mp3",               # control char
    "backups/x.mp3",                  # extension
    "audio/UC\\x/v.mp3",              # backslash
    "audio/UC/v",                     # no extension
    "",
])
def test_validate_put_key_rejects(key):
    with pytest.raises(ValueError):
        storage.validate_put_key(key)


@pytest.mark.parametrize("name,ctype", [
    ("a.mp3", "audio/mpeg"), ("a.opus", "audio/ogg"), ("a.jpg", "image/jpeg"),
    ("a.JPEG", "image/jpeg"), ("a.m4a", "audio/mp4"), ("x.db", "application/vnd.sqlite3"),
    ("a.weird", "application/octet-stream"), ("noext", "application/octet-stream"),
])
def test_content_type_for(name, ctype):
    assert storage.content_type_for(name) == ctype


# --- config_from_env ------------------------------------------------------------------

_FULL_R2 = {"STORAGE": "r2", "R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "key",
            "R2_SECRET_ACCESS_KEY": "secret", "R2_BUCKET": "bucket"}


def test_config_from_env_local_default():
    assert storage.config_from_env({}) == {"mode": "local"}
    assert storage.config_from_env({"STORAGE": "LOCAL "}) == {"mode": "local"}


def test_config_from_env_r2():
    cfg = storage.config_from_env(dict(_FULL_R2, R2_BUCKET="  bucket  "))
    assert cfg["mode"] == "r2"
    assert cfg["bucket"] == "bucket"
    assert cfg["account_id"] == "acct"
    assert cfg["presign_expiry"] == config.PRESIGN_EXPIRY_SECONDS
    assert cfg["staging_root"].endswith("slipcast-staging")
    cfg2 = storage.config_from_env(dict(_FULL_R2, STORAGE_STAGING_DIR="/tmp/elsewhere"))
    assert cfg2["staging_root"] == "/tmp/elsewhere"


def test_config_from_env_missing_names_only_never_values():
    env = {"STORAGE": "r2", "R2_BUCKET": "bucket-SENTINEL", "R2_ACCOUNT_ID": "acct-SENTINEL",
           "R2_SECRET_ACCESS_KEY": "   "}
    with pytest.raises(storage.StorageConfigError) as exc:
        storage.config_from_env(env)
    msg = str(exc.value)
    assert "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY" in msg
    assert "R2_BUCKET" not in msg and "R2_ACCOUNT_ID" not in msg
    assert "SENTINEL" not in msg


def test_config_from_env_unknown_mode():
    with pytest.raises(storage.StorageConfigError) as exc:
        storage.config_from_env({"STORAGE": "s3-but-very-long-value-here"})
    assert "STORAGE" in str(exc.value)


# --- LocalDriver ---------------------------------------------------------------------

def test_local_driver_point_ops(tmp_path):
    drv = storage.LocalDriver()
    ref = storage.media_ref("audio", str(tmp_path), CID, "v.mp3")
    assert not drv.exists(ref) and drv.size(ref) is None and drv.mtime(ref) is None

    src = tmp_path / "elsewhere.mp3"
    src.write_bytes(b"12345")
    assert drv.put_file(str(src), ref) == 5           # cross-path copy
    assert drv.exists(ref) and drv.size(ref) == 5 and src.exists()
    assert drv.put_file(ref.local_path, ref) == 5     # same path: no-op
    assert drv.peek_size(ref) is None
    assert drv.remove(ref) is True
    assert not drv.exists(ref)
    assert drv.remove(ref) is False
    assert drv.ensure_index() is True
    assert drv.health_info() == {"mode": "local", "ok": True}


def test_local_driver_put_rejects_invalid_key(tmp_path):
    src = tmp_path / "x.exe"
    src.write_bytes(b"x")
    ref = storage.media_ref("audio", str(tmp_path), CID, "x.exe")
    with pytest.raises(ValueError):
        storage.LocalDriver().put_file(str(src), ref)


def test_local_staging_yields_given_dir_and_keeps_files(tmp_path):
    target = tmp_path / "audio" / CID
    with storage.LocalDriver().staging(str(target)) as stage:
        assert stage == str(target)
        (target / "f.mp3").write_bytes(b"x")
    assert (target / "f.mp3").exists()


# --- R2Driver ------------------------------------------------------------------------

def _ref(name="v.mp3", kind="audio", cid=CID):
    return storage.media_ref(kind, "/unused", cid, name)


def test_r2_put_file_uploads_and_indexes(fake_r2, tmp_path):
    drv, fake = fake_r2
    src = tmp_path / "v.mp3"
    data = b"hello audio"
    src.write_bytes(data)
    assert drv.put_file(str(src), _ref()) == len(data)

    obj = fake.objects[f"audio/{CID}/v.mp3"]
    assert obj["body"] == data
    assert obj["content_type"] == "audio/mpeg"
    put = fake.calls_of("put_object")[0]
    assert put["ContentMD5"] == base64.b64encode(hashlib.md5(data).digest()).decode()
    assert put["ContentLength"] == len(data)
    assert drv.exists(_ref()) and drv.size(_ref()) == len(data)
    assert drv.peek_size(_ref()) == len(data)


def test_r2_put_rejects_invalid_key_before_any_call(fake_r2, tmp_path):
    drv, fake = fake_r2
    src = tmp_path / "x.exe"
    src.write_bytes(b"x")
    with pytest.raises(ValueError):
        drv.put_file(str(src), _ref("x.exe"))
    assert fake.calls_of("put_object") == []


def test_r2_exists_size_mtime_from_index(fake_r2):
    drv, fake = fake_r2
    when = datetime(2026, 1, 2, tzinfo=timezone.utc)
    fake.seed(f"audio/{CID}/v.mp3", b"x" * 42, last_modified=when)
    assert drv.exists(_ref())
    assert drv.size(_ref()) == 42
    assert drv.mtime(_ref()) == when.timestamp()
    assert not drv.exists(_ref("other.mp3"))
    assert drv.size(_ref("other.mp3")) is None
    assert fake.calls_of("head_object") == []


def test_r2_remove(fake_r2):
    drv, fake = fake_r2
    fake.seed(f"audio/{CID}/v.mp3", b"x")
    assert drv.exists(_ref())
    assert drv.remove(_ref()) is True
    assert f"audio/{CID}/v.mp3" not in fake.objects
    assert not drv.exists(_ref())
    assert drv.remove(_ref()) is False   # missing: no raise


def test_r2_refresh_paginates_with_prefix(tmp_path):
    fake = FakeS3(page_size=2)
    for i in range(5):
        fake.seed(f"audio/{CID}/v{i}.mp3", b"x")
    drv = storage.R2Driver(bucket="b", client=fake, staging_root=str(tmp_path / "s"))
    drv.refresh()
    assert all(drv.exists(_ref(f"v{i}.mp3")) for i in range(5))
    lists = fake.calls_of("list_objects_v2")
    assert all(c["Prefix"] in ("audio/", "thumbnails/") for c in lists)
    assert len([c for c in lists if c["Prefix"] == "audio/"]) == 3
    assert any("ContinuationToken" in c for c in lists)


def test_r2_refresh_picks_up_out_of_process_objects(fake_r2):
    drv, fake = fake_r2
    assert drv.ensure_index()
    fake.seed(f"thumbnails/{CID}/channel.jpg", b"img")
    assert not drv.exists(_ref("channel.jpg", "thumbnails"))
    drv.refresh()
    assert drv.exists(_ref("channel.jpg", "thumbnails"))


def test_r2_ensure_index_dedupes_concurrent_first_loads(fake_r2):
    drv, fake = fake_r2
    gate = threading.Event()
    fake.hooks["before_list"] = lambda kw: gate.wait(5)
    results = []
    threads = [threading.Thread(target=lambda: results.append(drv.ensure_index()))
               for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [True, True]
    lists = fake.calls_of("list_objects_v2")
    assert [c["Prefix"] for c in lists] == ["audio/", "thumbnails/"]  # one round


def _blocked_refresh(drv, fake):
    started, release = threading.Event(), threading.Event()

    def hook(kw):
        started.set()
        release.wait(5)
    fake.hooks["before_list"] = hook
    t = threading.Thread(target=drv.refresh)
    t.start()
    assert started.wait(5)
    return t, release


def test_r2_put_during_refresh_survives_swap(fake_r2, tmp_path):
    drv, fake = fake_r2
    drv.refresh()
    t, release = _blocked_refresh(drv, fake)
    src = tmp_path / "n.mp3"
    src.write_bytes(b"new")
    # Remove the object from the fake's listing view to prove the index entry
    # comes from the pending replay, not the (older) list result.
    drv.put_file(str(src), _ref("n.mp3"))
    with fake._lock:
        fake.objects.pop(f"audio/{CID}/n.mp3")
    release.set()
    fake.hooks["before_list"] = None
    t.join(5)
    assert drv.exists(_ref("n.mp3"))


def test_r2_delete_during_refresh_survives_swap(fake_r2):
    drv, fake = fake_r2
    fake.seed(f"audio/{CID}/old.mp3", b"x")
    drv.refresh()
    t, release = _blocked_refresh(drv, fake)
    drv.remove(_ref("old.mp3"))
    # The in-flight list may already have seen the object; re-seed so the stale
    # list result definitely contains it.
    fake.seed(f"audio/{CID}/old.mp3", b"x")
    release.set()
    fake.hooks["before_list"] = None
    t.join(5)
    assert not drv.exists(_ref("old.mp3"))


def test_r2_list_failure_fails_closed_and_scrubs(fake_r2):
    drv, fake = fake_r2
    fake.fail_next("list_objects_v2", RuntimeError(
        "Could not connect to the endpoint URL: https://acct-test.r2.cloudflarestorage.com/"
        "test-bucket key=AKIDTEST secret=SECRETTEST"))
    assert drv.ensure_index() is False
    with pytest.raises(storage.StorageUnavailable):
        drv.exists(_ref())
    info = drv.health_info()
    assert info["ok"] is False and info["index_loaded"] is False
    err = info["last_error"]
    assert err and "***" in err
    for secret in ("acct-test", "AKIDTEST", "SECRETTEST"):
        assert secret not in err
    assert drv.peek_size(_ref()) is None   # never raises


def test_r2_storage_error_has_no_chained_traceback(fake_r2):
    drv, fake = fake_r2
    fake.fail_next("delete_object")
    with pytest.raises(storage.StorageError) as exc:
        drv.remove(_ref())
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_r2_backoff_after_failed_load(fake_r2, monkeypatch):
    drv, fake = fake_r2
    now = [1000.0]
    monkeypatch.setattr(storage, "_clock", lambda: now[0])
    fake.fail_next("list_objects_v2")
    assert drv.ensure_index() is False
    n = len(fake.calls_of("list_objects_v2"))
    now[0] += 10
    assert drv.ensure_index() is False
    assert len(fake.calls_of("list_objects_v2")) == n      # no retry inside 30 s
    now[0] += 30
    assert drv.ensure_index() is True
    assert drv.health_info()["ok"] is True


def test_r2_presigned_urls(fake_r2):
    drv, fake = fake_r2
    get_url = drv.presigned_url(_ref())
    head_url = drv.presigned_url(_ref(), "HEAD")
    calls = fake.calls_of("generate_presigned_url")
    assert calls[0]["ClientMethod"] == "get_object"
    assert calls[0]["ExpiresIn"] == drv.presign_expiry == 3600
    assert calls[0]["Params"] == {"Bucket": "test-bucket", "Key": f"audio/{CID}/v.mp3"}
    assert calls[1]["ClientMethod"] == "head_object"
    assert "method=get_object" in get_url and "method=head_object" in head_url


def test_r2_staging_dir_always_removed(fake_r2):
    drv, _ = fake_r2
    with drv.staging("/ignored") as stage:
        assert os.path.basename(stage).startswith("dl-")
        assert os.path.dirname(stage) == drv.staging_root
        open(os.path.join(stage, "f"), "wb").close()
    assert not os.path.exists(stage)
    with pytest.raises(RuntimeError):
        with drv.staging(None) as stage2:
            raise RuntimeError("boom")
    assert not os.path.exists(stage2)
    assert os.listdir(drv.staging_root) == []


def test_r2_delete_channel_lists_r2_not_index(fake_r2):
    drv, fake = fake_r2
    other = "UCother123456789012345678"
    fake.seed(f"audio/{CID}/a.mp3", b"x")
    fake.seed(f"thumbnails/{CID}/channel.jpg", b"x")
    fake.seed(f"audio/{other}/a.mp3", b"x")
    drv.ensure_index()
    fake.seed(f"audio/{CID}/late.mp3", b"x")   # after the index loaded
    assert drv.delete_channel(CID) == 3
    assert sorted(fake.objects) == [f"audio/{other}/a.mp3"]
    assert not drv.exists(_ref("a.mp3"))
    assert drv.exists(_ref("a.mp3", cid=other))
    with pytest.raises(ValueError):
        drv.delete_channel("../x")


def test_r2_list_channel_and_channel_ids(fake_r2):
    drv, fake = fake_r2
    fake.seed(f"audio/{CID}/a.mp3", b"xx")
    fake.seed(f"thumbnails/{CID}/a.jpg", b"x")
    fake.seed("audio/UCzz/b.mp3", b"x")
    assert [n for n, _, _ in drv.list_channel("audio", CID)] == ["a.mp3"]
    assert drv.list_channel("audio", CID)[0][1] == 2
    assert drv.channel_ids("audio") == {CID, "UCzz"}
    assert drv.channel_ids("thumbnails") == {CID}


def test_r2_backups_put_and_prune(fake_r2, tmp_path):
    drv, fake = fake_r2
    for day in range(1, 10):
        fake.seed(f"backups/episodes-202608{day:02d}-000000.db", b"old")
    fake.seed("backups/pre-pk-migration-x.db", b"keep")
    local = tmp_path / "episodes-20261002-030000.db"
    local.write_bytes(b"sqlite bytes")
    assert drv.put_backup(str(local)) == "backups/episodes-20261002-030000.db"
    assert fake.objects["backups/episodes-20261002-030000.db"]["content_type"] == "application/vnd.sqlite3"
    gone = drv.prune_backups(7)
    assert len(gone) == 3
    remaining = sorted(k for k in fake.objects if k.startswith("backups/episodes-"))
    assert len(remaining) == 7
    assert "backups/episodes-20261002-030000.db" in remaining
    assert "backups/episodes-20260801-000000.db" not in remaining
    assert "backups/pre-pk-migration-x.db" in fake.objects
    # backups never enter the media index
    assert not any(k.startswith("backups/") for k in drv._index)


def test_sweep_staging(tmp_path):
    old = tmp_path / "dl-x"
    fresh = tmp_path / "dl-y"
    other = tmp_path / "keep-me"
    for d in (old, fresh, other):
        d.mkdir()
    afile = tmp_path / "dl-file"
    afile.write_bytes(b"x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    os.utime(afile, (past, past))
    assert storage.sweep_staging(str(tmp_path)) == 1
    assert not old.exists() and fresh.exists() and other.exists() and afile.exists()
    assert storage.sweep_staging(str(tmp_path / "missing")) == 0
    assert storage.sweep_staging(None) == 0


@pytest.mark.parametrize("root", ["data", "audio", "thumbs", "backups", "inside", "ancestor"])
def test_staging_root_safety(root):
    data = config.DATA_DIR
    path = {"data": data, "audio": config.AUDIO_DIR, "thumbs": config.THUMBNAIL_DIR,
            "backups": config.BACKUP_DIR, "inside": os.path.join(config.AUDIO_DIR, "x"),
            "ancestor": os.path.dirname(data)}[root]
    with pytest.raises(storage.StorageConfigError):
        storage.R2Driver(staging_root=path, client=FakeS3(), bucket="b")


def test_init_and_get(monkeypatch):
    drv = storage.init({"STORAGE": "local"})
    assert storage.get() is drv and drv.mode == "local"
    storage._reset()
    monkeypatch.setenv("STORAGE", "local")
    assert storage.get().mode == "local"
    with pytest.raises(storage.StorageConfigError):
        storage.init({"STORAGE": "r2"})


def test_init_r2_uses_build_client(monkeypatch, tmp_path):
    built = {}

    def fake_build(account_id, key_id, secret):
        built.update(account_id=account_id)
        return FakeS3()
    monkeypatch.setattr(storage, "_build_client", fake_build)
    drv = storage.init(dict(_FULL_R2, STORAGE_STAGING_DIR=str(tmp_path / "s")))
    assert drv.mode == "r2" and drv.bucket == "bucket" and built == {"account_id": "acct"}


def test_refresh_index_job_never_raises(fake_r2, caplog):
    drv, fake = fake_r2
    fake.fail_next("list_objects_v2")
    storage.refresh_index_job()   # logs a warning only
    storage.refresh_index_job()
    assert drv.health_info()["ok"] is True


def test_real_client_config(monkeypatch):
    import boto3
    from conftest import REAL_BUILD_CLIENT

    captured = {}

    def fake_client(service, **kwargs):
        captured["service"] = service
        captured.update(kwargs)
        return FakeS3()
    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(storage, "_build_client", REAL_BUILD_CLIENT)
    storage.R2Driver(bucket="b", account_id="a1", access_key_id="k", secret_access_key="s")

    assert captured["service"] == "s3"
    assert captured["endpoint_url"] == "https://a1.r2.cloudflarestorage.com"
    assert captured["region_name"] == "auto"
    cfg = captured["config"]
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert cfg.signature_version == "s3v4"
    assert cfg.connect_timeout and cfg.read_timeout


def test_local_storage_mode_in_tests():
    """conftest pins STORAGE=local before anything imports the app."""
    assert os.environ["STORAGE"] == "local"
    assert storage.get().mode == "local"
    for var in storage.R2_REQUIRED_VARS:
        assert var not in os.environ
