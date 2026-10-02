"""Import-time storage fail-fast, in a clean subprocess (no conftest stubs).

The subprocess imports the real yt_dlp, installed from requirements.txt both
locally and in CI. Its environment is built from scratch, so no R2_* value from
the developer's shell can leak in — and none of these runs can reach R2: the
r2 cases fail before any client exists, and the local case proves boto3 is
never even imported.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(tmp_path, code, **env):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
            "DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, env={**base, **env},
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def test_r2_missing_vars_fails_naming_only_missing(tmp_path):
    rc, out = _run(tmp_path, "import app.main", STORAGE="r2",
                   R2_BUCKET="bucket-SENTINEL", R2_ACCOUNT_ID="acct-SENTINEL")
    assert rc != 0
    assert "R2_ACCESS_KEY_ID" in out and "R2_SECRET_ACCESS_KEY" in out
    assert "bucket-SENTINEL" not in out and "acct-SENTINEL" not in out


def test_unknown_storage_mode_fails(tmp_path):
    rc, out = _run(tmp_path, "import app.main", STORAGE="bogus")
    assert rc != 0
    assert "STORAGE" in out


def test_local_mode_never_imports_boto3(tmp_path):
    rc, out = _run(tmp_path, 'import app.main, sys; assert "boto3" not in sys.modules',
                   STORAGE="local")
    assert rc == 0, out
