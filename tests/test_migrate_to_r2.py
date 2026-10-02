"""scripts/migrate_to_r2.py against FakeS3: dry run, apply, idempotence, and
that nothing is ever deleted (locally or remotely)."""
import importlib.util
import os
import subprocess
import sys

import pytest

from app import storage
from fake_s3 import FakeS3

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "migrate_to_r2.py")
CID = "UCabc12345678901234567890"

spec = importlib.util.spec_from_file_location("migrate_to_r2", SCRIPT)
mig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig)


@pytest.fixture
def library(tmp_path):
    files = {
        f"audio/{CID}/a.mp3": b"a" * 100,
        f"audio/{CID}/b.opus": b"b" * 50,
        f"thumbnails/{CID}/channel.jpg": b"c" * 10,
        f"audio/{CID}/c.mp3.part": b"partial",       # ignored
        "audio/bad id/x.mp3": b"x",                  # ignored
        f"thumbnails/{CID}/.hidden.jpg": b"h",       # ignored
    }
    data = tmp_path / "data"
    for rel, content in files.items():
        p = data / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    fake = FakeS3()
    drv = storage.R2Driver(bucket="b", client=fake, staging_root=str(tmp_path / "s"))
    return str(data), files, drv, fake


def _quiet(*a, **k):
    pass


def _assert_local_untouched(data, files):
    for rel, content in files.items():
        with open(os.path.join(data, rel), "rb") as f:
            assert f.read() == content


def test_dry_run_plans_and_writes_nothing(library):
    data, files, drv, fake = library
    result = mig.migrate(data, drv, apply=False, log=_quiet)
    assert result["planned"] == 3 and result["ignored"] == 3
    assert result["local_files"] == 3 and result["local_bytes"] == 160
    assert fake.calls_of("put_object") == []
    assert {op for op, _ in fake.calls} == {"list_objects_v2"}
    _assert_local_untouched(data, files)


def test_apply_uploads_verifies_and_is_idempotent(library):
    data, files, drv, fake = library
    result = mig.migrate(data, drv, apply=True, log=_quiet)
    assert result["uploaded"] == 3 and result["verified"] == 3
    assert result["failed"] == 0 and result["mismatched"] == 0
    for key in (f"audio/{CID}/a.mp3", f"audio/{CID}/b.opus", f"thumbnails/{CID}/channel.jpg"):
        assert fake.objects[key]["body"] == files[key]
    assert fake.objects[f"audio/{CID}/b.opus"]["content_type"] == "audio/ogg"
    assert len(fake.objects) == 3

    puts = len(fake.calls_of("put_object"))
    again = mig.migrate(data, drv, apply=True, log=_quiet)
    assert again["uploaded"] == 0 and again["skipped"] == 3 and again["verified"] == 3
    assert len(fake.calls_of("put_object")) == puts
    _assert_local_untouched(data, files)
    assert fake.calls_of("delete_object") == []


def test_size_mismatch_is_replaced(library):
    data, files, drv, fake = library
    fake.seed(f"audio/{CID}/a.mp3", b"short")
    result = mig.migrate(data, drv, apply=True, log=_quiet)
    assert result["replaced"] == 1 and result["uploaded"] == 2
    assert fake.objects[f"audio/{CID}/a.mp3"]["body"] == files[f"audio/{CID}/a.mp3"]
    assert result["mismatched"] == 0


def test_put_failure_is_counted_not_fatal(library):
    data, files, drv, fake = library
    fake.fail_next("put_object")
    result = mig.migrate(data, drv, apply=True, log=_quiet)
    assert result["failed"] == 1
    assert result["uploaded"] == 2
    assert result["mismatched"] == 1        # the failed one isn't in the bucket
    assert len(fake.objects) == 2
    _assert_local_untouched(data, files)
    assert fake.calls_of("delete_object") == []


def test_output_never_prints_secrets(library):
    data, files, _drv, _ = library
    fake = FakeS3()
    drv = storage.R2Driver(bucket="b", account_id="acct-SENTINEL", access_key_id="KEY-SENTINEL",
                           secret_access_key="SECRET-SENTINEL", client=fake,
                           staging_root=os.path.join(os.path.dirname(data), "s"))
    lines = []
    mig.migrate(data, drv, apply=True, log=lines.append)
    out = "\n".join(lines)
    assert "bucket b" in out
    assert "SENTINEL" not in out and "r2.cloudflarestorage.com" not in out


def test_parse_args():
    assert mig.parse_args(["--apply"]).apply is True
    assert mig.parse_args([]).apply is False
    assert mig.parse_args(["--data-dir", "/x"]).data_dir == "/x"
    with pytest.raises(SystemExit) as exc:
        mig.parse_args(["--nope"])
    assert exc.value.code == 2


def test_missing_env_exits_2_naming_vars_only(tmp_path):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "DATA_DIR": str(tmp_path), "R2_BUCKET": "bucket-SENTINEL"}
    proc = subprocess.run([sys.executable, SCRIPT], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2
    assert "R2_ACCOUNT_ID" in out and "R2_ACCESS_KEY_ID" in out
    assert "R2_SECRET_ACCESS_KEY" in out
    assert "bucket-SENTINEL" not in out
