"""Shared test fixtures.

The app imports ``yt_dlp`` at module load; it's a heavy dependency we don't
need for unit tests, so we install a lightweight stub before anything imports it.
"""
import os
import sys
import tempfile
import types

# Tests must never reach the real R2 bucket. A developer's shell (or a future
# dotenv load) may carry the production R2_* values, so pin local mode and drop
# them before any app module is imported — app.main initialises storage at
# import time.
os.environ["STORAGE"] = "local"
for _var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
             "R2_BUCKET", "STORAGE_STAGING_DIR"):
    os.environ.pop(_var, None)

# Point DATA_DIR at a writable temp dir before any app module reads config,
# so import-time os.makedirs() doesn't try to create /data.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ytrss-test-"))

if "yt_dlp" not in sys.modules:
    stub = types.ModuleType("yt_dlp")

    class _DownloadError(Exception):
        pass

    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = _DownloadError
    stub.utils = utils

    class _YoutubeDL:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            return {}

    stub.YoutubeDL = _YoutubeDL
    sys.modules["yt_dlp"] = stub
    sys.modules["yt_dlp.utils"] = utils


import pytest  # noqa: E402  (after the env pinning / yt_dlp stub above, on purpose)

from app import storage  # noqa: E402

# The genuine client factory, captured before the autouse guard below patches it,
# for the one test that inspects the client config (with boto3.client stubbed).
REAL_BUILD_CLIENT = storage._build_client


@pytest.fixture(autouse=True)
def _isolate_storage(monkeypatch):
    """Every test starts and ends on the same storage driver, and no test can
    ever build a real S3 client (which could reach the production bucket)."""
    saved = storage._driver

    def _no_real_client(*a, **k):
        raise AssertionError("tests must never build a real S3 client")

    monkeypatch.setattr(storage, "_build_client", _no_real_client)
    yield
    storage._set_driver(saved)


@pytest.fixture
def fake_r2(tmp_path):
    """Switch the app to r2 mode against an in-memory FakeS3. Yields (driver, fake)."""
    from fake_s3 import FakeS3

    fake = FakeS3()
    drv = storage.R2Driver(
        bucket="test-bucket", account_id="acct-test", access_key_id="AKIDTEST",
        secret_access_key="SECRETTEST", client=fake,
        staging_root=str(tmp_path / "staging"),
    )
    storage._set_driver(drv)
    yield drv, fake
