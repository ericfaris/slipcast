"""Tests for the episode cap (prune) and members-only skip behavior."""
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from app import database as db, downloader

CID = "UCabc12345678901234567890"  # valid channel_id per the regex


def _setup_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(downloader, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(downloader, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    db.init_db()


def _ep(i, cid=CID):
    return {
        "id": f"v{i:03d}", "channel_id": cid, "channel_name": "C",
        "title": f"t{i}", "description": "",
        "published": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
        "duration": 1, "filename": f"v{i:03d}.mp3", "filesize": 1, "thumbnail": None,
    }


def test_prune_enforces_cap(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    for i in range(30):
        db.upsert_episode(_ep(i))
    assert len(db.get_episodes(CID)) == 30
    downloader._prune_channel(CID)
    assert len(db.get_episodes(CID)) == 20


def test_prune_keeps_newest(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 5)
    for i in range(10):
        db.upsert_episode(_ep(i))
    downloader._prune_channel(CID)
    remaining = db.get_episodes(CID)
    assert len(remaining) == 5
    # get_episodes returns newest-first; kept set should be the 5 most recent
    pubs = [r["published"] for r in remaining]
    assert pubs == sorted(pubs, reverse=True)


def test_prune_records_skip_for_deleted(tmp_path, monkeypatch):
    """Pruned videos are remembered so future polls don't re-download them."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 2)
    for i in range(5):
        db.upsert_episode(_ep(i))  # published ascends with i -> newest = highest i
    downloader._prune_channel(CID)
    remaining = {e["id"] for e in db.get_episodes(CID)}
    assert remaining == {"v004", "v003"}
    # the three dropped episodes are skip-marked; the kept ones are not
    assert db.get_skip_video_ids(CID) == {f"v{i:03d}" for i in range(5)} - remaining


def test_prune_deletes_thumbnails_and_sweeps_orphans(tmp_path, monkeypatch):
    """Prune deletes the dropped episode's audio + thumbnail, and the sweep
    removes any leftover files the DB no longer references — except channel.jpg."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 2)
    audio_dir = downloader._audio_dir_for(CID)
    thumb_dir = downloader._thumbnail_dir_for(CID)

    for i in range(4):  # v000..v003, oldest..newest by published
        ep = _ep(i)
        ep["thumbnail"] = f"v{i:03d}.jpg"
        db.upsert_episode(ep)
        open(os.path.join(audio_dir, ep["filename"]), "wb").close()
        open(os.path.join(thumb_dir, ep["thumbnail"]), "wb").close()

    # Cover art and stray leftovers that no episode references.
    open(os.path.join(thumb_dir, "channel.jpg"), "wb").close()
    open(os.path.join(audio_dir, "orphan.mp3.part"), "wb").close()
    open(os.path.join(thumb_dir, "ghost.jpg"), "wb").close()
    # orphan.mp3.part looks like a yt-dlp temp file, so the sweep only removes
    # it once it's old enough that it can't still be an in-flight download —
    # back-date it past the grace window (see test below for the "recent, so
    # kept" half of this behavior).
    old = time.time() - downloader._RECENT_FILE_GRACE_SECONDS - 60
    os.utime(os.path.join(audio_dir, "orphan.mp3.part"), (old, old))

    downloader._prune_channel(CID)

    kept = {e["id"] for e in db.get_episodes(CID)}
    assert kept == {"v002", "v003"}  # two newest
    assert sorted(os.listdir(audio_dir)) == ["v002.mp3", "v003.mp3"]
    assert sorted(os.listdir(thumb_dir)) == ["channel.jpg", "v002.jpg", "v003.jpg"]


def test_sweep_skips_recent_temp_files_but_deletes_stale_ones(tmp_path, monkeypatch):
    """A yt-dlp temp/in-progress file (e.g. another thread's in-flight download)
    must survive a concurrent sweep if it's recent, but a genuinely abandoned
    one (old) still gets cleaned up."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    audio_dir = downloader._audio_dir_for(CID)
    thumb_dir = downloader._thumbnail_dir_for(CID)

    recent_temp = os.path.join(audio_dir, "vAAAAAAAAAA.part")
    stale_temp = os.path.join(audio_dir, "vBBBBBBBBBB.m4a")
    open(recent_temp, "wb").close()
    open(stale_temp, "wb").close()
    old = time.time() - downloader._RECENT_FILE_GRACE_SECONDS - 60
    os.utime(stale_temp, (old, old))
    open(os.path.join(thumb_dir, "channel.jpg"), "wb").close()

    downloader._sweep_orphan_files(CID)

    remaining = os.listdir(audio_dir)
    assert "vAAAAAAAAAA.part" in remaining  # recent — left alone
    assert "vBBBBBBBBBB.m4a" not in remaining  # stale — swept


def test_poll_does_not_redownload_pruned_video(tmp_path, monkeypatch):
    """Regression: a channel can list an older-dated video at the top of its
    feed (pinned/premiere/re-upload). The download loop walks channel order
    while prune keeps newest-by-date, so without a skip record the two fight
    and re-download the same video every poll, pushing counts past the cap."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 2)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "C")

    dates = {
        "vAAAAAAAAAA": "2026-06-01T00:00:00+00:00",  # newest
        "vBBBBBBBBBB": "2026-05-01T00:00:00+00:00",  # 2nd newest
        "vXXXXXXXXXX": "2025-01-01T00:00:00+00:00",  # old, but listed first
    }

    def _ep_for(vid):
        return {
            "id": vid, "channel_id": CID, "channel_name": "C", "title": vid,
            "description": "", "published": dates[vid], "duration": 1,
            "filename": f"{vid}.mp3", "filesize": 1, "thumbnail": None,
        }

    # Seed the two newest as already-downloaded (files on disk + DB rows).
    os.makedirs(downloader._audio_dir_for(CID), exist_ok=True)
    for vid in ("vAAAAAAAAAA", "vBBBBBBBBBB"):
        open(os.path.join(downloader._audio_dir_for(CID), f"{vid}.mp3"), "wb").close()
        db.upsert_episode(_ep_for(vid))

    # Channel lists the stale video FIRST — the churn trigger.
    entries = [{"id": v, "availability": None} for v in ("vXXXXXXXXXX", "vAAAAAAAAAA", "vBBBBBBBBBB")]

    downloaded = []

    def _fake_download(entry, cid, cname, **_kw):
        vid = entry["id"]
        path = os.path.join(downloader._audio_dir_for(cid), f"{vid}.mp3")
        if os.path.exists(path):
            return None  # already downloaded — matches the real function
        open(path, "wb").close()
        downloaded.append(vid)
        return _ep_for(vid)

    monkeypatch.setattr(downloader, "_fetch_channel_entries", lambda *a, **k: (entries, CID, "C"))
    monkeypatch.setattr(downloader, "_download_entry", _fake_download)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)

    # First poll: X is downloaded (channel order) then pruned (old date) and
    # recorded as a skip; the cap holds at the two newest by date.
    downloader.poll_channel(url)
    assert "vXXXXXXXXXX" in downloaded
    assert "vXXXXXXXXXX" in db.get_skip_video_ids(CID)
    assert {e["id"] for e in db.get_episodes(CID)} == {"vAAAAAAAAAA", "vBBBBBBBBBB"}

    # Second poll: X must NOT be re-downloaded, and the count stays at the cap.
    downloaded.clear()
    downloader.poll_channel(url)
    assert downloaded == []
    assert {e["id"] for e in db.get_episodes(CID)} == {"vAAAAAAAAAA", "vBBBBBBBBBB"}


def test_get_channel_id_for_url(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    # No channel_id resolved yet -> None
    assert db.get_channel_id_for_url(url) is None
    db.update_channel_meta(url, CID, "C")
    assert db.get_channel_id_for_url(CID + "x") is None  # unknown url
    assert db.get_channel_id_for_url(url) == CID


def test_poll_prunes_even_when_fetch_fails(tmp_path, monkeypatch):
    """Regression: an over-cap channel must be pruned even if the fetch raises
    (e.g. expired cookies), instead of returning early and never capping."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)

    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "C")
    for i in range(34):
        db.upsert_episode(_ep(i))
    assert len(db.get_episodes(CID)) == 34

    # Fetch blows up the way an auth/cookie failure would.
    def _boom(*_a, **_k):
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader, "_fetch_channel_entries", _boom)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader.notify, "send_cookie_alert", lambda *a, **k: None)

    downloader.poll_channel(url)

    # Even though the fetch failed and poll returned early, the cap was enforced.
    assert len(db.get_episodes(CID)) == 20


def _stub_poll_io(monkeypatch, entries):
    """Wire poll_channel's external I/O to in-memory stubs and return a list
    that records the ids actually handed to _download_entry."""
    downloaded_ids = []

    def _fake_download(entry, cid, cname, **_kw):
        downloaded_ids.append(entry["id"])
        return _ep(int(entry["id"][1:]), cid)

    monkeypatch.setattr(downloader, "_fetch_channel_entries",
                        lambda *a, **k: (entries, CID, "C"))
    monkeypatch.setattr(downloader, "_download_entry", _fake_download)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)
    return downloaded_ids


def test_poll_loop_caps_downloads(tmp_path, monkeypatch):
    """Regression for 'episode numbers above twenty': a successful poll must
    stop downloading at the cap and finish with exactly MAX episodes."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)

    entries = [{"id": f"v{i:03d}", "availability": None} for i in range(34)]
    downloaded_ids = _stub_poll_io(monkeypatch, entries)

    downloader.poll_channel(url)

    # The loop itself stopped at the cap (didn't download all 34) ...
    assert len(downloaded_ids) == 20
    # ... and the channel ends at exactly the cap.
    assert len(db.get_episodes(CID)) == 20


def test_poll_skips_members_only_and_records_skip(tmp_path, monkeypatch):
    """Members-only entries are not downloaded and are remembered for fast-skip."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)

    entries = [
        {"id": "v000", "availability": None},
        {"id": "v001", "availability": "subscriber_only"},
        {"id": "v002", "availability": None},
    ]
    downloaded_ids = _stub_poll_io(monkeypatch, entries)

    downloader.poll_channel(url)

    assert downloaded_ids == ["v000", "v002"]  # member-only one skipped
    assert "v001" in db.get_skip_video_ids(CID)
    assert len(db.get_episodes(CID)) == 2


def test_poll_strips_query_string_before_appending_videos(tmp_path, monkeypatch):
    """Regression: a share-link URL like '...?si=X5yUqCOVRTbOweX5' must not
    become '...?si=X5yUqCOVRTbOweX5/videos' — the query string swallows the
    '/videos' suffix, making yt-dlp resolve to the channel's tab list instead
    of its actual videos, so every poll silently downloads nothing."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    raw_url = "https://youtube.com/@jason_samosa?si=X5yUqCOVRTbOweX5"
    db.add_channel(raw_url)

    fetched_urls = []

    def _fake_fetch(url, max_entries):
        fetched_urls.append(url)
        return [], CID, "C"

    monkeypatch.setattr(downloader, "_fetch_channel_entries", _fake_fetch)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)

    downloader.poll_channel(raw_url)

    assert fetched_urls == ["https://youtube.com/@jason_samosa/videos"]


def test_poll_records_ok_run(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    entries = [{"id": f"v{i:03d}", "availability": None} for i in range(3)]
    _stub_poll_io(monkeypatch, entries)

    downloader.poll_channel(url)

    runs = db.get_recent_poll_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["downloaded"] == 3
    assert runs[0]["finished_at"]


def test_poll_records_error_run(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "C")

    def _boom(*_a, **_k):
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader, "_fetch_channel_entries", _boom)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader.notify, "send_cookie_alert", lambda *a, **k: None)

    downloader.poll_channel(url)

    runs = db.get_recent_poll_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert "bot" in runs[0]["error"]
    assert runs[0]["channel_id"] == CID  # resolved from the known URL


def test_poll_all_warns_when_cookies_expiring_soon(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader, "COOKIE_EXPIRY_WARN_DAYS", 7)
    monkeypatch.setattr(downloader, "cookies_status",
                        lambda: {"expired": False, "days_until_expiry": 3,
                                 "expires_at": "2026-12-24 12:45 UTC"})
    calls = []
    monkeypatch.setattr(downloader.notify, "send_cookie_expiry_warning",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(downloader.notify, "send_cookie_alert", lambda *a, **k: calls.append("alert"))
    monkeypatch.setattr(db, "get_channels", lambda: [])

    downloader.poll_all()

    assert calls == [(3, "2026-12-24 12:45 UTC")]


def test_poll_all_no_warning_when_expiry_far_off(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader, "COOKIE_EXPIRY_WARN_DAYS", 7)
    monkeypatch.setattr(downloader, "cookies_status",
                        lambda: {"expired": False, "days_until_expiry": 179,
                                 "expires_at": "2026-12-24 12:45 UTC"})
    calls = []
    monkeypatch.setattr(downloader.notify, "send_cookie_expiry_warning",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(db, "get_channels", lambda: [])

    downloader.poll_all()

    assert calls == []


def test_poll_all_missing_cookies_uses_alert_not_warning(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: False)
    calls = []
    monkeypatch.setattr(downloader.notify, "send_cookie_alert", lambda *a, **k: calls.append("alert"))
    monkeypatch.setattr(downloader.notify, "send_cookie_expiry_warning",
                        lambda *a, **k: calls.append("warn"))
    monkeypatch.setattr(db, "get_channels", lambda: [])

    downloader.poll_all()

    assert calls == ["alert"]


def test_member_only_detection():
    assert downloader._looks_like_member_only("ERROR: [youtube] x: Join this channel to get access")
    assert downloader._looks_like_member_only("This video is members-only content")
    assert not downloader._looks_like_member_only("Video unavailable")
    assert not downloader._looks_like_member_only("")


def test_skip_video_roundtrip(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.add_skip_video("vid1", CID, "members_only")
    db.add_skip_video("vid1", CID, "members_only")  # idempotent
    db.add_skip_video("vid2", CID, "members_only")
    assert db.get_skip_video_ids(CID) == {"vid1", "vid2"}
    db.delete_skip_videos_for_channel(CID)
    assert db.get_skip_video_ids(CID) == set()


# --- orphan detection --------------------------------------------------------

def test_find_orphan_channels_detects_db_only_orphan(tmp_path, monkeypatch):
    """Episodes exist for a channel_id with no channels/unsubscribed row."""
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0, CID))
    db.upsert_episode(_ep(1, CID))
    orphans = downloader.find_orphan_channels()
    assert len(orphans) == 1
    assert orphans[0]["channel_id"] == CID
    assert orphans[0]["channel_name"] == "C"
    assert orphans[0]["episode_count"] == 2


def test_find_orphan_channels_detects_disk_only_orphan(tmp_path, monkeypatch):
    """A leftover directory with no DB rows at all is still reported."""
    _setup_tmp(tmp_path, monkeypatch)
    os.makedirs(os.path.join(downloader.AUDIO_DIR, CID), exist_ok=True)
    with open(os.path.join(downloader.AUDIO_DIR, CID, "stray.mp3"), "wb") as f:
        f.write(b"x" * 100)
    orphans = downloader.find_orphan_channels()
    assert len(orphans) == 1
    assert orphans[0]["channel_id"] == CID
    assert orphans[0]["episode_count"] == 0
    assert orphans[0]["bytes"] == 100


def test_find_orphan_channels_ignores_owned_channels(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "C")
    db.upsert_episode(_ep(0, CID))
    assert downloader.find_orphan_channels() == []


def test_find_orphan_channels_ignores_unsubscribed_channels(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_unsubscribed_channel(CID, "C")
    db.upsert_episode(_ep(0, CID))
    assert downloader.find_orphan_channels() == []


# --- per-channel poll lock ---------------------------------------------------

def test_poll_channel_refuses_concurrent_poll_of_same_channel(tmp_path, monkeypatch):
    """A second poll of the same channel while one is in flight must return
    immediately with an 'already_polling' marker, not block or double-run."""
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)

    key = downloader._poll_lock_key(url)
    lock = downloader._get_poll_lock(key)
    lock.acquire()  # simulate an in-flight poll of this channel
    try:
        result = downloader.poll_channel(url)
    finally:
        lock.release()
    assert result["already_polling"] is True


def test_poll_channel_lock_key_normalizes_url_variants(tmp_path, monkeypatch):
    base = "https://youtube.com/@Chan"
    with_slash = "https://youtube.com/@Chan/"
    with_query = "https://youtube.com/@Chan?si=abc123"
    assert downloader._poll_lock_key(base) == downloader._poll_lock_key(with_slash)
    assert downloader._poll_lock_key(base) == downloader._poll_lock_key(with_query)


def test_poll_channel_releases_lock_after_completion(tmp_path, monkeypatch):
    """A poll must not leave the channel permanently locked out for later polls."""
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)
    entries = [{"id": "v000", "availability": None}]
    _stub_poll_io(monkeypatch, entries)

    downloader.poll_channel(url)
    result = downloader.poll_channel(url)  # must run normally, not report already_polling
    assert not result.get("already_polling")


# --- one-off downloads respect the episode cap -------------------------------

def test_download_single_prunes_over_cap_channel(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 2)
    for i in range(5):
        db.upsert_episode(_ep(i))  # 5 episodes already on this unsubscribed channel

    info = {"id": "vNEW00000001", "channel_id": CID, "channel": "C"}
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL",
                        lambda *a, **k: _FakeYDL(info))
    monkeypatch.setattr(downloader, "_download_entry",
                        lambda entry, cid, cname, **_kw: _ep(5, cid))

    downloader.download_single("https://youtu.be/vNEW00000001", subscribe=False)

    assert len(db.get_episodes(CID)) == 2  # capped, same as a polled channel


class _FakeYDL:
    def __init__(self, info):
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, *a, **k):
        return self._info


# --- thumbnail download can't hang forever -----------------------------------

def test_thumbnail_fetch_timeout_degrades_gracefully(tmp_path, monkeypatch):
    """A stalled thumbnail host must not pin the poll thread (v1.10.0 class)."""
    def _timeout(*a, **k):
        raise TimeoutError("the read timed out")

    monkeypatch.setattr(downloader.urllib.request, "urlopen", _timeout)
    dest = str(tmp_path / "thumb.jpg")

    assert downloader._download_thumbnail(
        "https://i.ytimg.com/vi/abc/hq.jpg", dest) is False
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".tmp")  # temp file cleaned up


def test_thumbnail_ffmpeg_timeout_degrades_gracefully(tmp_path, monkeypatch):
    import io
    import subprocess

    class _Resp:
        def __enter__(self):
            return io.BytesIO(b"\xff\xd8jpegbytes")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(downloader.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    def _hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)

    monkeypatch.setattr(downloader.subprocess, "run", _hang)
    dest = str(tmp_path / "thumb.jpg")

    assert downloader._download_thumbnail(
        "https://i.ytimg.com/vi/abc/hq.jpg", dest) is False
    assert not os.path.exists(dest + ".tmp")


def test_thumbnail_calls_pass_timeouts(tmp_path, monkeypatch):
    """Both blocking calls must actually receive a bounded timeout."""
    import io

    seen = {}

    class _Resp:
        def __enter__(self):
            return io.BytesIO(b"jpegbytes")

        def __exit__(self, *a):
            return False

    def _urlopen(url, timeout=None, **k):
        seen["fetch"] = timeout
        return _Resp()

    class _Done:
        returncode = 0
        stderr = b""

    def _run(cmd, **kwargs):
        seen["ffmpeg"] = kwargs.get("timeout")
        open(cmd[-1], "wb").write(b"jpeg")
        return _Done()

    monkeypatch.setattr(downloader.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(downloader.subprocess, "run", _run)

    downloader._download_thumbnail("https://i.ytimg.com/vi/abc/hq.jpg",
                                   str(tmp_path / "t.jpg"))

    assert seen["fetch"] == downloader._THUMBNAIL_FETCH_TIMEOUT > 0
    assert seen["ffmpeg"] == downloader._FFMPEG_TIMEOUT > 0


# --- disk-pressure auto-prune -------------------------------------------------

CID2 = "UCdef12345678901234567890"


def _seed_episode_with_files(ep):
    """Insert an episode row and create its audio + thumbnail files on disk."""
    db.upsert_episode(ep)
    audio = os.path.join(downloader._audio_dir_for(ep["channel_id"]), ep["filename"])
    open(audio, "wb").write(b"a" * 16)
    thumb = None
    if ep["thumbnail"]:
        thumb = os.path.join(downloader._thumbnail_dir_for(ep["channel_id"]), ep["thumbnail"])
        open(thumb, "wb").write(b"t")
    return audio, thumb


def _fake_disk(monkeypatch, free_gbs):
    """shutil.disk_usage stub returning each value in free_gbs, then the last."""
    import collections
    Usage = collections.namedtuple("Usage", "total used free")
    seq = list(free_gbs)

    def _usage(path):
        gb = seq.pop(0) if len(seq) > 1 else seq[0]
        return Usage(100 * 1024 ** 3, 0, int(gb * 1024 ** 3))

    monkeypatch.setattr(downloader.shutil, "disk_usage", _usage)


def _capture_prune_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(downloader.notify, "send_disk_prune_alert",
                        lambda pruned, freed, free_gb, **kw: calls.append(
                            (list(pruned), freed, free_gb)))
    return calls


def test_disk_floor_prunes_globally_oldest_across_channels(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "MIN_FREE_DISK_GB", 2)

    files = {}
    # Interleaved across two channels: v000 (CID2) and v001 (CID) are the two
    # oldest overall, so a per-channel pruner would pick different victims.
    for i, cid in ((0, CID2), (1, CID), (2, CID2), (3, CID)):
        ep = _ep(i, cid)
        ep["thumbnail"] = f"v{i:03d}.jpg"
        ep["filesize"] = 1_048_576
        files[ep["id"]] = _seed_episode_with_files(ep)

    # Below the floor for the initial check and after the first deletion;
    # clear once the two oldest are gone.
    _fake_disk(monkeypatch, [0.5, 0.9, 3.0])
    alerts = _capture_prune_alert(monkeypatch)

    downloader._enforce_disk_floor()

    remaining = {r["id"] for r in db.get_all_episodes_oldest_first()}
    assert remaining == {"v002", "v003"}  # the two globally oldest went
    for gone in ("v000", "v001"):
        audio, thumb = files[gone]
        assert not os.path.exists(audio)
        assert not os.path.exists(thumb)
    for kept in ("v002", "v003"):
        assert os.path.exists(files[kept][0])

    # Skip-marked, or the very next poll re-downloads them and refills the disk.
    assert db.get_skip_video_ids(CID2) == {"v000"}
    assert db.get_skip_video_ids(CID) == {"v001"}

    assert len(alerts) == 1
    pruned, freed, free_gb = alerts[0]
    assert len(pruned) == 2 and any("t0" in p for p in pruned)
    assert freed == 2 * 1_048_576
    assert free_gb == 3.0


def test_disk_floor_noop_when_space_is_plentiful(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "MIN_FREE_DISK_GB", 2)
    for i in range(3):
        _seed_episode_with_files(_ep(i))
    _fake_disk(monkeypatch, [50.0])
    alerts = _capture_prune_alert(monkeypatch)

    downloader._enforce_disk_floor()

    assert len(db.get_episodes(CID)) == 3
    assert alerts == []
    assert db.get_skip_video_ids(CID) == set()


def test_disk_floor_disabled_by_zero_threshold(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "MIN_FREE_DISK_GB", 0)
    _seed_episode_with_files(_ep(0))

    def _boom(path):
        raise AssertionError("disk must not even be checked when disabled")

    monkeypatch.setattr(downloader.shutil, "disk_usage", _boom)
    downloader._enforce_disk_floor()
    assert len(db.get_episodes(CID)) == 1


def test_disk_floor_alerts_when_nothing_left_to_prune(tmp_path, monkeypatch):
    """Still-full after deleting everything is the loudest case, not a quiet one."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "MIN_FREE_DISK_GB", 2)
    _fake_disk(monkeypatch, [0.1])  # never clears
    alerts = _capture_prune_alert(monkeypatch)

    downloader._enforce_disk_floor()  # no episodes exist at all

    assert len(alerts) == 1
    assert alerts[0][0] == ["(nothing left to prune)"]


def test_disk_floor_continues_past_a_failed_deletion(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(downloader, "MIN_FREE_DISK_GB", 2)
    for i in range(3):
        _seed_episode_with_files(_ep(i))

    real_remove = downloader._remove_if_exists

    def _flaky(path):
        if path.endswith("v000.mp3"):
            raise OSError("permission denied")
        return real_remove(path)

    monkeypatch.setattr(downloader, "_remove_if_exists", _flaky)
    # 0.5 GB at the initial check; clear once one episode has actually gone.
    _fake_disk(monkeypatch, [0.5, 3.0])
    _capture_prune_alert(monkeypatch)

    downloader._enforce_disk_floor()

    # v000 failed and was left intact; the loop moved on and freed v001 instead.
    assert {r["id"] for r in db.get_all_episodes_oldest_first()} == {"v000", "v002"}


def test_poll_all_enforces_disk_floor_before_polling(tmp_path, monkeypatch):
    """Space is freed up front, not discovered halfway through a download."""
    _setup_tmp(tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(downloader, "_enforce_disk_floor",
                        lambda: order.append("disk"))
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda p: True)
    monkeypatch.setattr(downloader, "cookies_status",
                        lambda: {"present": True, "expired": False,
                                 "days_until_expiry": None})
    monkeypatch.setattr(db, "get_channels",
                        lambda: [{"url": "https://www.youtube.com/@A"}])
    monkeypatch.setattr(downloader, "poll_channel",
                        lambda url: order.append("poll") or None)

    downloader.poll_all()

    assert order == ["disk", "poll"]


def test_poll_all_survives_a_failing_disk_check(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "_enforce_disk_floor",
                        lambda: (_ for _ in ()).throw(RuntimeError("statvfs failed")))
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda p: True)
    monkeypatch.setattr(downloader, "cookies_status",
                        lambda: {"present": True, "expired": False,
                                 "days_until_expiry": None})
    polled = []
    monkeypatch.setattr(db, "get_channels", lambda: [{"url": "https://x"}])
    monkeypatch.setattr(downloader, "poll_channel",
                        lambda url: polled.append(url) or None)

    downloader.poll_all()  # remediation failing must never abort the run

    assert polled == ["https://x"]


# --- configurable audio codec/bitrate ----------------------------------------

def test_ydl_opts_defaults_unchanged(tmp_path, monkeypatch):
    """Regression guard: with the defaults, _ydl_opts() must still produce
    byte-for-byte the same postprocessor options as before this change."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "AUDIO_CODEC", "mp3")
    monkeypatch.setattr(downloader, "AUDIO_BITRATE_KBPS", "128")
    opts = downloader._ydl_opts(CID)
    assert opts["postprocessors"] == [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "128",
    }]
    assert opts["outtmpl"].endswith("%(id)s.%(ext)s")


def test_ydl_opts_honours_codec_and_bitrate(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "AUDIO_CODEC", "opus")
    monkeypatch.setattr(downloader, "AUDIO_BITRATE_KBPS", "64")
    opts = downloader._ydl_opts(CID)
    assert opts["postprocessors"][0]["preferredcodec"] == "opus"
    assert opts["postprocessors"][0]["preferredquality"] == "64"
    assert downloader._audio_ext() == "opus"


def test_unknown_codec_falls_back_to_mp3(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "AUDIO_CODEC", "flurble")
    assert downloader._audio_ext() == "mp3"
    opts = downloader._ydl_opts(CID)
    assert opts["postprocessors"][0]["preferredcodec"] == "mp3"


def test_download_entry_skips_existing_file_in_other_format(tmp_path, monkeypatch):
    """Flipping AUDIO_CODEC must not make an already-downloaded episode look
    missing and re-download the whole library."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "AUDIO_CODEC", "opus")
    audio_dir = downloader._audio_dir_for(CID)
    open(os.path.join(audio_dir, "vAAAAAAAAAA.mp3"), "wb").close()

    def _boom(*a, **k):
        raise AssertionError("yt-dlp must not be invoked for an already-downloaded video")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _boom)

    result = downloader._download_entry({"id": "vAAAAAAAAAA"}, CID, "C")
    assert result is None


# --- age-based retention ------------------------------------------------------

def _ep_aged(i, days_old, cid=CID):
    published = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {
        "id": f"v{i:03d}", "channel_id": cid, "channel_name": "C",
        "title": f"t{i}", "description": "", "published": published,
        "duration": 1, "filename": f"v{i:03d}.mp3", "filesize": 1, "thumbnail": None,
    }


def test_prune_removes_aged_out_episodes(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 100)  # count cap inert
    monkeypatch.setattr(downloader, "MAX_EPISODE_AGE_DAYS", 30)
    ages = {0: 1, 1: 5, 2: 40, 3: 400}
    audio_dir = downloader._audio_dir_for(CID)
    for i, days_old in ages.items():
        ep = _ep_aged(i, days_old)
        db.upsert_episode(ep)
        open(os.path.join(audio_dir, ep["filename"]), "wb").close()

    downloader._prune_channel(CID)

    remaining = {e["id"] for e in db.get_episodes(CID)}
    assert remaining == {"v000", "v001"}
    assert db.get_skip_video_ids(CID) == {"v002", "v003"}
    assert not os.path.exists(os.path.join(audio_dir, "v002.mp3"))
    assert not os.path.exists(os.path.join(audio_dir, "v003.mp3"))
    assert os.path.exists(os.path.join(audio_dir, "v000.mp3"))
    assert os.path.exists(os.path.join(audio_dir, "v001.mp3"))


def test_prune_age_disabled_by_default(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 100)
    monkeypatch.setattr(downloader, "MAX_EPISODE_AGE_DAYS", 0)
    for i, days_old in {0: 1, 1: 5, 2: 40, 3: 400}.items():
        db.upsert_episode(_ep_aged(i, days_old))

    downloader._prune_channel(CID)

    assert len(db.get_episodes(CID)) == 4


def test_prune_applies_both_caps(tmp_path, monkeypatch):
    """An episode can be dropped either for being over the count cap or too
    old — both apply, independently, and record the matching reason."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 3)
    monkeypatch.setattr(downloader, "MAX_EPISODE_AGE_DAYS", 30)
    # By age (newest first): v000(1d), v001(2d), v003(40d), v002(50d), v004(60d), v005(70d).
    # v003 ranks inside the top-3 count-cap slice but is still past the 30-day
    # age cap, so it must be dropped for "aged_out" specifically — the other
    # two dropped episodes (v002, v004... wait v005) are dropped for "pruned"
    # (over the count cap) regardless of age.
    episodes = [
        _ep_aged(0, 1), _ep_aged(1, 2), _ep_aged(3, 40),
        _ep_aged(2, 50), _ep_aged(4, 60), _ep_aged(5, 70),
    ]
    for ep in episodes:
        db.upsert_episode(ep)

    downloader._prune_channel(CID)

    remaining = {e["id"] for e in db.get_episodes(CID)}
    skipped = db.get_skip_video_ids(CID)
    assert remaining | skipped == {f"v{i:03d}" for i in range(6)}
    assert remaining.isdisjoint(skipped)
    # v003 (40 days old) must be gone regardless of cap position.
    assert "v003" not in remaining

    with sqlite3.connect(db.DB_PATH) as conn:
        rows = conn.execute("SELECT video_id, reason FROM skip_videos WHERE channel_id=?", (CID,)).fetchall()
    reasons = {vid: reason for vid, reason in rows}
    assert reasons.get("v003") == "aged_out"
    assert any(r == "pruned" for r in reasons.values())


# --- max-duration filter ------------------------------------------------------

def test_poll_skips_over_long_entry_before_download(tmp_path, monkeypatch):
    """A flat listing entry that already carries a duration over the cap must
    never reach _download_entry (no bandwidth wasted), and must stay skipped
    on the next poll."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    monkeypatch.setattr(downloader, "MAX_EPISODE_DURATION_MINUTES", 30)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)

    entries = [
        {"id": "v000", "availability": None, "duration": 600},
        {"id": "v001", "availability": None, "duration": 7200},  # over cap
    ]
    downloaded_ids = _stub_poll_io(monkeypatch, entries)

    downloader.poll_channel(url)
    assert downloaded_ids == ["v000"]
    assert "v001" in db.get_skip_video_ids(CID)

    # Second poll: the over-long entry is skipped instantly, never re-attempted
    # (v000 is re-"downloaded" here only because _stub_poll_io's fake never
    # checks file existence like the real _download_entry does).
    downloaded_ids.clear()
    downloader.poll_channel(url)
    assert "v001" not in downloaded_ids


def test_poll_discards_over_long_video_after_download(tmp_path, monkeypatch):
    """A live-stream/premiere entry with no flat duration is caught by the
    post-download backstop instead."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 20)
    monkeypatch.setattr(downloader, "MAX_EPISODE_DURATION_MINUTES", 30)
    url = "https://www.youtube.com/@SomeChannel"
    db.add_channel(url)

    entries = [{"id": "v000", "availability": None, "duration": None}]

    def _fake_download(entry, cid, cname, **_kw):
        raise downloader.TooLongError("v000")

    monkeypatch.setattr(downloader, "_fetch_channel_entries",
                        lambda *a, **k: (entries, CID, "C"))
    monkeypatch.setattr(downloader, "_download_entry", _fake_download)
    monkeypatch.setattr(downloader, "valid_cookie_file", lambda _p: True)
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)

    result = downloader.poll_channel(url)

    assert result["downloaded"] == 0
    assert "v000" in db.get_skip_video_ids(CID)
    assert db.get_episodes(CID) == []


def test_download_entry_deletes_over_long_file(tmp_path, monkeypatch):
    """Exercise the real _download_entry: the just-downloaded file must be
    removed and TooLongError raised when the real duration exceeds the cap."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODE_DURATION_MINUTES", 30)
    audio_dir = downloader._audio_dir_for(CID)

    class _FakeDownloadYDL:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            open(os.path.join(audio_dir, "vAAAAAAAAAA.mp3"), "wb").close()
            return {"id": "vAAAAAAAAAA", "duration": 7200, "title": "x",
                    "upload_date": "20260101"}

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", lambda *a, **k: _FakeDownloadYDL())

    try:
        downloader._download_entry({"id": "vAAAAAAAAAA"}, CID, "C")
        assert False, "expected TooLongError"
    except downloader.TooLongError:
        pass
    assert not os.path.exists(os.path.join(audio_dir, "vAAAAAAAAAA.mp3"))


def test_download_single_ignores_duration_cap(tmp_path, monkeypatch):
    """One-off downloads are an explicit user request — they warn but still
    download an over-long video."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(downloader, "MAX_EPISODE_DURATION_MINUTES", 30)

    info = {"id": "vNEW00000001", "channel_id": CID, "channel": "C", "duration": 7200}
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", lambda *a, **k: _FakeYDL(info))

    calls = []

    def _fake_download(entry, cid, cname, **kw):
        calls.append(kw)
        return _ep(0, cid)

    monkeypatch.setattr(downloader, "_download_entry", _fake_download)

    downloader.download_single("https://youtu.be/vNEW00000001", subscribe=False)

    assert calls == [{"enforce_duration": False}]
    assert len(db.get_episodes(CID)) == 1


# --- explicit episode re-download -------------------------------------------

VID = "vAAAAAAAAAA"  # 11 chars — a valid video_id per the regex


def test_redownload_replaces_existing_file(tmp_path, monkeypatch):
    """AC5: the "already on disk" short-circuit must be bypassed.

    _download_entry returns None when the file already exists, so a re-download
    only works if the old file is gone by the time it's called — that's what the
    stub asserts.
    """
    _setup_tmp(tmp_path, monkeypatch)
    audio_dir = downloader._audio_dir_for(CID)
    path = os.path.join(audio_dir, f"{VID}.mp3")
    with open(path, "wb") as f:
        f.write(b"corrupt")

    seen = {}

    def _fake_download(entry, channel_id, channel_name, **kwargs):
        seen["existed"] = os.path.exists(path)  # must already be gone
        with open(path, "wb") as f:
            f.write(b"fresh")
        return {**_ep(1), "id": entry["id"], "filename": f"{VID}.mp3", "filesize": 5}

    monkeypatch.setattr(downloader, "_download_entry", _fake_download)
    result = downloader.redownload_episode(VID, CID, "C")

    assert seen["existed"] is False
    assert result["id"] == VID
    assert open(path, "rb").read() == b"fresh"


def test_redownload_removes_other_codec_file_too(tmp_path, monkeypatch):
    """An episode downloaded under a different AUDIO_CODEC still occupies the
    "already have it" slot — leaving it behind would make the next poll think
    the video is present under the old extension."""
    _setup_tmp(tmp_path, monkeypatch)
    audio_dir = downloader._audio_dir_for(CID)
    stale = os.path.join(audio_dir, f"{VID}.opus")
    with open(stale, "wb") as f:
        f.write(b"old")
    monkeypatch.setattr(downloader, "_download_entry", lambda *a, **k: None)
    assert downloader.redownload_episode(VID, CID, "C") is None
    assert not os.path.exists(stale)


def test_redownload_rejects_bad_ids(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(downloader, "_download_entry",
                        lambda *a, **k: called.append(a))
    assert downloader.redownload_episode("../etc/passwd", CID, "C") is None
    assert downloader.redownload_episode(VID, "../etc", "C") is None
    assert called == []


def test_redownload_returns_none_for_members_only(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)

    def _raise(*a, **k):
        raise downloader.MemberOnlyError(VID)

    monkeypatch.setattr(downloader, "_download_entry", _raise)
    assert downloader.redownload_episode(VID, CID, "C") is None


def test_delete_episode_files_removes_audio_and_thumbnail(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    audio = os.path.join(downloader._audio_dir_for(CID), "v001.mp3")
    thumb = os.path.join(downloader._thumbnail_dir_for(CID), "v001.jpg")
    for p in (audio, thumb):
        with open(p, "wb") as f:
            f.write(b"x")
    downloader.delete_episode_files(CID, "v001.mp3", "v001.jpg")
    assert not os.path.exists(audio)
    assert not os.path.exists(thumb)


def test_delete_episode_files_refuses_unsafe_names(tmp_path, monkeypatch):
    """Defense in depth: a hand-edited row must not turn a delete into a
    traversal that reaches outside the channel's own directory."""
    _setup_tmp(tmp_path, monkeypatch)
    outside = tmp_path / "precious.txt"
    outside.write_text("keep me")
    downloader.delete_episode_files(CID, "../../precious.txt", None)
    downloader.delete_episode_files("../etc", "v001.mp3", None)
    assert outside.exists()


def test_delete_episode_files_tolerates_missing_files(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    downloader.delete_episode_files(CID, "gone.mp3", "gone.jpg")  # must not raise
