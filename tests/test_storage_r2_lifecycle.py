"""r2 mode: prune, orphan sweep, channel removal, usage figures, the disk floor,
the poll readiness gate, and DB backups — each asserted against both the
FakeS3 objects and the driver's index."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import database as db, downloader, notify

CID = "UCabc12345678901234567890"
OTHER = "UCdef12345678901234567890"


@pytest.fixture
def r2(fake_r2, tmp_path, monkeypatch):
    drv, fake = fake_r2
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(downloader, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(downloader, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    db.init_db()
    return drv, fake


def _ep(i, cid=CID, thumb=True):
    return {
        "id": f"v{i:03d}", "channel_id": cid, "channel_name": "C",
        "title": f"t{i}", "description": "",
        "published": f"2026-06-{i + 1:02d}T00:00:00+00:00",
        "duration": 1, "filename": f"v{i:03d}.mp3", "filesize": 1,
        "thumbnail": f"v{i:03d}.jpg" if thumb else None,
    }


def _seed_ep(fake, ep, size=10):
    db.upsert_episode(ep)
    fake.seed(f"audio/{ep['channel_id']}/{ep['filename']}", b"a" * size)
    if ep["thumbnail"]:
        fake.seed(f"thumbnails/{ep['channel_id']}/{ep['thumbnail']}", b"t")


def test_prune_channel_deletes_oldest_objects_and_index(r2, monkeypatch):
    drv, fake = r2
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 2)
    for i in range(5):
        _seed_ep(fake, _ep(i))
    drv.ensure_index()

    downloader._prune_channel(CID)

    assert {e["id"] for e in db.get_episodes(CID)} == {"v003", "v004"}
    assert db.get_skip_video_ids(CID) >= {"v000", "v001", "v002"}
    for i in range(3):
        assert f"audio/{CID}/v{i:03d}.mp3" not in fake.objects
        assert f"thumbnails/{CID}/v{i:03d}.jpg" not in fake.objects
        assert not drv.exists(downloader._audio_ref(CID, f"v{i:03d}.mp3"))
        assert not drv.exists(downloader._thumb_ref(CID, f"v{i:03d}.jpg"))
    for i in (3, 4):
        assert drv.exists(downloader._audio_ref(CID, f"v{i:03d}.mp3"))


def test_prune_drops_row_with_unsafe_filename_without_touching_storage(r2, monkeypatch):
    drv, fake = r2
    monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", 0)
    db.upsert_episode({**_ep(0, thumb=False), "filename": "../evil.mp3"})
    drv.ensure_index()
    downloader._prune_channel(CID)
    assert db.get_episodes(CID) == []
    assert fake.calls_of("delete_object") == []


def test_sweep_removes_stray_keeps_referenced_and_channel_art(r2):
    drv, fake = r2
    _seed_ep(fake, _ep(0))
    fake.seed(f"audio/{CID}/x.mp3", b"stray")
    fake.seed(f"thumbnails/{CID}/channel.jpg", b"art")
    drv.ensure_index()

    downloader._sweep_orphan_files(CID)

    assert f"audio/{CID}/x.mp3" not in fake.objects
    assert not drv.exists(downloader._audio_ref(CID, "x.mp3"))
    assert f"audio/{CID}/v000.mp3" in fake.objects
    assert f"thumbnails/{CID}/v000.jpg" in fake.objects
    assert f"thumbnails/{CID}/channel.jpg" in fake.objects


def test_sweep_grace_protects_recent_unreferenced_opus(r2):
    drv, fake = r2
    now = datetime.now(timezone.utc)
    fake.seed(f"audio/{CID}/fresh000000.opus", b"x", last_modified=now)
    fake.seed(f"audio/{CID}/stale000000.opus", b"x", last_modified=now - timedelta(hours=2))
    drv.ensure_index()

    downloader._sweep_orphan_files(CID)

    assert f"audio/{CID}/fresh000000.opus" in fake.objects
    assert f"audio/{CID}/stale000000.opus" not in fake.objects


def test_remove_channel_data_deletes_only_that_channel(r2):
    drv, fake = r2
    _seed_ep(fake, _ep(0))
    fake.seed(f"thumbnails/{CID}/channel.jpg", b"art")
    _seed_ep(fake, _ep(1, cid=OTHER))
    drv.ensure_index()

    downloader.remove_channel_data(CID)

    assert not any(f"/{CID}/" in k for k in fake.objects)
    assert f"audio/{OTHER}/v001.mp3" in fake.objects
    assert not drv.exists(downloader._audio_ref(CID, "v000.mp3"))
    assert drv.exists(downloader._audio_ref(OTHER, "v001.mp3"))
    downloader.remove_channel_data("../etc")  # refused, no raise


def test_usage_and_orphans_from_index(r2):
    drv, fake = r2
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "A")
    fake.seed(f"audio/{CID}/a.mp3", b"a" * 100)
    fake.seed(f"thumbnails/{CID}/a.jpg", b"t" * 5)
    fake.seed(f"audio/{OTHER}/b.mp3", b"b" * 40)   # index-only orphan

    assert downloader.channel_bytes(CID) == 105
    per, total = downloader.storage_usage()
    assert per == {CID: 105, OTHER: 40}
    assert total == 145
    orphans = downloader.find_orphan_channels()
    assert [(o["channel_id"], o["bytes"], o["episode_count"]) for o in orphans] == [(OTHER, 40, 0)]


def test_disk_floor_skipped_in_r2_mode(r2, monkeypatch):
    drv, fake = r2
    monkeypatch.setattr(downloader, "MIN_FREE_DISK_GB", 2)

    def _no_disk(path):
        raise AssertionError("disk_usage must not be consulted in r2 mode")
    monkeypatch.setattr(downloader.shutil, "disk_usage", _no_disk)
    alerts = []
    monkeypatch.setattr(downloader.notify, "send_disk_prune_alert",
                        lambda *a, **k: alerts.append(a))
    _seed_ep(fake, _ep(0))

    downloader._enforce_disk_floor()

    assert len(db.get_episodes(CID)) == 1
    assert fake.calls_of("delete_object") == []
    assert alerts == []


def test_poll_all_skips_when_index_unavailable(r2, monkeypatch):
    drv, fake = r2
    db.add_channel("https://www.youtube.com/@A")
    fake.fail_next("list_objects_v2")
    polled, alerts = [], []
    monkeypatch.setattr(downloader, "poll_channel", lambda url: polled.append(url))
    monkeypatch.setattr(downloader.notify, "send_poll_failure_alert",
                        lambda problems, **k: alerts.append(problems))
    monkeypatch.setattr(downloader, "_enforce_disk_floor", lambda: None)

    downloader.poll_all()

    assert polled == []
    assert len(alerts) == 1 and "R2" in alerts[0][0]


def test_poll_all_polls_when_index_loads(r2, monkeypatch):
    drv, fake = r2
    db.add_channel("https://www.youtube.com/@A")
    polled = []
    monkeypatch.setattr(downloader, "poll_channel", lambda url: polled.append(url))
    monkeypatch.setattr(downloader, "_enforce_disk_floor", lambda: None)
    monkeypatch.setattr(downloader.notify, "send_cookie_alert", lambda *a, **k: None)
    downloader.poll_all()
    assert polled == ["https://www.youtube.com/@A"]


# --- DB backups mirrored to R2 -------------------------------------------------------

@pytest.fixture
def backup_env(r2, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    alerts = []
    monkeypatch.setattr(notify, "send_backup_failure_alert",
                        lambda reason, **k: alerts.append(reason))
    return (*r2, alerts)


def test_backup_job_mirrors_to_r2_with_retention(backup_env, tmp_path):
    drv, fake, alerts = backup_env
    for day in range(1, 10):
        fake.seed(f"backups/episodes-202608{day:02d}-000000.db", b"old")
    fake.seed("backups/pre-pk-migration-x.db", b"keep")

    db.run_backup_job()

    local = os.listdir(tmp_path / "backups")
    assert len(local) == 1
    key = f"backups/{local[0]}"
    obj = fake.objects[key]
    assert obj["body"] == (tmp_path / "backups" / local[0]).read_bytes()
    assert obj["content_type"] == "application/vnd.sqlite3"
    remote = sorted(k for k in fake.objects if k.startswith("backups/episodes-"))
    assert len(remote) == 7 and key in remote
    assert remote[0] == "backups/episodes-20260804-000000.db"
    assert "backups/pre-pk-migration-x.db" in fake.objects
    assert alerts == []


def test_backup_job_r2_failure_keeps_local_and_alerts(backup_env, tmp_path):
    drv, fake, alerts = backup_env
    fake.fail_next("put_object")

    db.run_backup_job()  # must not raise

    assert len(os.listdir(tmp_path / "backups")) == 1
    assert len(alerts) == 1 and "R2" in alerts[0]
    assert not any(k.startswith("backups/") for k in fake.objects)


def test_backup_job_local_mode_stays_local(tmp_path, monkeypatch):
    from app import storage

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    db.init_db()
    storage.init({"STORAGE": "local"})
    alerts = []
    monkeypatch.setattr(notify, "send_backup_failure_alert",
                        lambda reason, **k: alerts.append(reason))
    db.run_backup_job()
    assert len(os.listdir(tmp_path / "backups")) == 1
    assert alerts == []
