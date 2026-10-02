"""r2 mode: downloads stage locally, upload only the final file, and never
write media under DATA_DIR. Runs against FakeS3 via the fake_r2 fixture."""
import os

import pytest

from app import database as db, downloader, storage

CID = "UCabc12345678901234567890"
VID = "dQw4w9WgXcQ"
N = 7


@pytest.fixture
def r2(fake_r2, tmp_path, monkeypatch):
    drv, fake = fake_r2
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(downloader, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(downloader, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    monkeypatch.setattr(downloader, "AUDIO_CODEC", "mp3")
    db.init_db()
    return drv, fake


def _no_local_media(tmp_path):
    return not os.path.exists(tmp_path / "audio") and not os.path.exists(tmp_path / "thumb")


def _staging_empty(drv):
    return not os.path.exists(drv.staging_root) or os.listdir(drv.staging_root) == []


class _FakeYDL:
    """Writes the "converted" mp3 where outtmpl points, like yt-dlp would."""
    constructed = 0

    def __init__(self, opts, *, duration=60, thumbnail=None, fail=None):
        type(self).constructed += 1
        self.opts, self.duration, self.thumbnail, self.fail = opts, duration, thumbnail, fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _path(self, vid, ext):
        return self.opts["outtmpl"].replace("%(id)s", vid).replace("%(ext)s", ext)

    def extract_info(self, url, download=True):
        vid = url.rsplit("=", 1)[-1]
        if self.fail:
            with open(self._path(vid, "webm.part"), "wb") as f:
                f.write(b"partial")
            raise downloader.yt_dlp.utils.DownloadError(self.fail)
        with open(self._path(vid, "mp3"), "wb") as f:
            f.write(b"audio" * N)
        return {"id": vid, "title": "T", "duration": self.duration,
                "upload_date": "20260101", "thumbnail": self.thumbnail}


def _patch_ydl(monkeypatch, **kw):
    _FakeYDL.constructed = 0
    seen = []

    def factory(opts):
        seen.append(opts)
        return _FakeYDL(opts, **kw)
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", factory)
    return seen


def _thumb_writer(ok=True):
    def fake(url, dest):
        if not ok:
            return False
        with open(dest, "wb") as f:
            f.write(b"jpeg")
        return True
    return fake


def test_download_uploads_and_leaves_no_local_files(r2, tmp_path, monkeypatch):
    drv, fake = r2
    seen = _patch_ydl(monkeypatch)
    row = downloader._download_entry({"id": VID}, CID, "C")

    assert row["filename"] == f"{VID}.mp3"
    assert row["filesize"] == N * 5
    obj = fake.objects[f"audio/{CID}/{VID}.mp3"]
    assert obj["body"] == b"audio" * N
    assert obj["content_type"] == "audio/mpeg"
    assert seen[0]["outtmpl"].startswith(drv.staging_root)
    assert _no_local_media(tmp_path)
    assert _staging_empty(drv)
    assert drv.exists(downloader._audio_ref(CID, f"{VID}.mp3"))


def test_download_uploads_episode_thumbnail(r2, tmp_path, monkeypatch):
    drv, fake = r2
    _patch_ydl(monkeypatch, thumbnail="https://i.ytimg.com/vi/x/hq.jpg")
    monkeypatch.setattr(downloader, "_download_thumbnail", _thumb_writer())
    row = downloader._download_entry({"id": VID}, CID, "C")
    assert row["thumbnail"] == f"{VID}.jpg"
    assert fake.objects[f"thumbnails/{CID}/{VID}.jpg"]["body"] == b"jpeg"
    assert fake.objects[f"thumbnails/{CID}/{VID}.jpg"]["content_type"] == "image/jpeg"
    assert _no_local_media(tmp_path) and _staging_empty(drv)


def test_download_thumbnail_failure_keeps_audio(r2, tmp_path, monkeypatch):
    drv, fake = r2
    _patch_ydl(monkeypatch, thumbnail="https://i.ytimg.com/vi/x/hq.jpg")
    monkeypatch.setattr(downloader, "_download_thumbnail", _thumb_writer(ok=False))
    row = downloader._download_entry({"id": VID}, CID, "C")
    assert row["thumbnail"] is None
    assert f"audio/{CID}/{VID}.mp3" in fake.objects
    assert not any(k.startswith("thumbnails/") for k in fake.objects)
    assert _staging_empty(drv)


def test_thumbnail_upload_failure_is_cosmetic(r2, tmp_path, monkeypatch):
    drv, fake = r2
    _patch_ydl(monkeypatch, thumbnail="https://i.ytimg.com/vi/x/hq.jpg")
    monkeypatch.setattr(downloader, "_download_thumbnail", _thumb_writer())
    real_put = drv.put_file

    def put(src, ref):
        if ref.kind == "thumbnails":
            raise storage.StorageError("boom")
        return real_put(src, ref)
    monkeypatch.setattr(drv, "put_file", put)
    row = downloader._download_entry({"id": VID}, CID, "C")
    assert row and row["thumbnail"] is None
    assert _staging_empty(drv)


def test_download_error_uploads_nothing(r2, tmp_path, monkeypatch):
    drv, fake = r2
    _patch_ydl(monkeypatch, fail="HTTP Error 403: Forbidden")
    assert downloader._download_entry({"id": VID}, CID, "C") is None
    assert fake.calls_of("put_object") == []
    assert _staging_empty(drv) and _no_local_media(tmp_path)


def test_too_long_uploads_nothing(r2, tmp_path, monkeypatch):
    drv, fake = r2
    monkeypatch.setattr(downloader, "MAX_EPISODE_DURATION_MINUTES", 30)
    _patch_ydl(monkeypatch, duration=7200)
    with pytest.raises(downloader.TooLongError):
        downloader._download_entry({"id": VID}, CID, "C")
    assert fake.calls_of("put_object") == []
    assert _staging_empty(drv) and _no_local_media(tmp_path)


def test_upload_failure_raises_and_cleans_staging(r2, tmp_path, monkeypatch):
    drv, fake = r2
    _patch_ydl(monkeypatch)
    fake.fail_next("put_object")
    with pytest.raises(storage.StorageError):
        downloader._download_entry({"id": VID}, CID, "C")
    assert _staging_empty(drv)
    assert not drv.exists(downloader._audio_ref(CID, f"{VID}.mp3"))
    assert fake.objects == {}


def test_already_present_in_other_codec_skips_download(r2, monkeypatch):
    drv, fake = r2
    fake.seed(f"audio/{CID}/{VID}.opus", b"old")
    _patch_ydl(monkeypatch)
    assert downloader._download_entry({"id": VID}, CID, "C") is None
    assert _FakeYDL.constructed == 0


def test_index_unavailable_fails_closed(r2, monkeypatch):
    drv, fake = r2
    fake.fail_next("list_objects_v2")
    _patch_ydl(monkeypatch)
    with pytest.raises(storage.StorageUnavailable):
        downloader._download_entry({"id": VID}, CID, "C")
    assert _FakeYDL.constructed == 0
    assert fake.calls_of("put_object") == []


def test_redownload_deletes_object_first_then_uploads(r2, monkeypatch):
    drv, fake = r2
    fake.seed(f"audio/{CID}/{VID}.mp3", b"corrupt")
    fake.seed(f"audio/{CID}/{VID}.opus", b"old codec")
    ref = downloader._audio_ref(CID, f"{VID}.mp3")
    real = downloader._download_entry
    seen = {}

    def wrapped(entry, channel_id, channel_name, **kw):
        seen["existed"] = drv.exists(ref)
        return real(entry, channel_id, channel_name, **kw)
    monkeypatch.setattr(downloader, "_download_entry", wrapped)
    _patch_ydl(monkeypatch)

    row = downloader.redownload_episode(VID, CID, "C")
    assert seen["existed"] is False
    assert row["filename"] == f"{VID}.mp3"
    assert fake.objects[f"audio/{CID}/{VID}.mp3"]["body"] == b"audio" * N
    assert f"audio/{CID}/{VID}.opus" not in fake.objects


def test_delete_episode_files_removes_objects(r2):
    drv, fake = r2
    fake.seed(f"audio/{CID}/v001.mp3", b"x")
    fake.seed(f"thumbnails/{CID}/v001.jpg", b"x")
    drv.ensure_index()
    downloader.delete_episode_files(CID, "v001.mp3", "v001.jpg")
    assert fake.objects == {}
    assert not drv.exists(downloader._audio_ref(CID, "v001.mp3"))
    assert not drv.exists(downloader._thumb_ref(CID, "v001.jpg"))


def test_delete_episode_files_refuses_unsafe(r2):
    drv, fake = r2
    downloader.delete_episode_files(CID, "../../precious.txt", "../x.jpg")
    downloader.delete_episode_files("../etc", "v001.mp3", None)
    assert fake.calls_of("delete_object") == []


def test_channel_art_uploaded_once(r2, tmp_path, monkeypatch):
    drv, fake = r2

    class _FlatYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"channel_id": CID, "channel": "C", "entries": [],
                    "thumbnail": "https://yt3.ggpht.com/abc=s900"}

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FlatYDL)
    monkeypatch.setattr(downloader, "_download_thumbnail", _thumb_writer())
    downloader._fetch_channel_entries("https://www.youtube.com/@C/videos", 3)
    assert fake.objects[f"thumbnails/{CID}/channel.jpg"]["body"] == b"jpeg"
    downloader._fetch_channel_entries("https://www.youtube.com/@C/videos", 3)
    assert len(fake.calls_of("put_object")) == 1
    assert _no_local_media(tmp_path) and _staging_empty(drv)


def test_dir_helpers_do_not_create_dirs_in_r2_mode(r2, tmp_path):
    assert downloader._audio_dir_for(CID) == os.path.join(str(tmp_path / "audio"), CID)
    downloader._thumbnail_dir_for(CID)
    assert _no_local_media(tmp_path)
    with pytest.raises(ValueError):
        downloader._audio_dir_for("../x")
