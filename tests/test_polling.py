"""Tests for the episode cap (prune) and members-only skip behavior."""
import os

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

    downloader._prune_channel(CID)

    kept = {e["id"] for e in db.get_episodes(CID)}
    assert kept == {"v002", "v003"}  # two newest
    assert sorted(os.listdir(audio_dir)) == ["v002.mp3", "v003.mp3"]
    assert sorted(os.listdir(thumb_dir)) == ["channel.jpg", "v002.jpg", "v003.jpg"]


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

    def _fake_download(entry, cid, cname):
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

    def _fake_download(entry, cid, cname):
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
