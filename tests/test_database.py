"""Roundtrip tests for database helpers (channel + episode lifecycle)."""
from app import database as db

CID = "UCabc12345678901234567890"
CID2 = "UCdef12345678901234567890"


def _setup_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()


def _ep(i, cid=CID):
    return {
        "id": f"v{i:03d}", "channel_id": cid, "channel_name": "C",
        "title": f"t{i}", "description": "",
        "published": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
        "duration": 1, "filename": f"v{i:03d}.mp3", "filesize": 1, "thumbnail": None,
    }


def test_add_channel_is_idempotent(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.add_channel(url)  # INSERT OR IGNORE
    assert [r["url"] for r in db.get_channels()] == [url]


def test_remove_channel(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.remove_channel(url)
    assert db.get_channels() == []


def test_unsubscribed_channel_roundtrip(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_unsubscribed_channel(CID, "Name")
    db.upsert_unsubscribed_channel(CID, "Renamed")  # REPLACE updates name
    rows = db.get_unsubscribed_channels()
    assert len(rows) == 1 and rows[0]["channel_name"] == "Renamed"
    db.remove_unsubscribed_channel(CID)
    assert db.get_unsubscribed_channels() == []


def test_get_all_channel_ids_is_distinct(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    # episode id (the video id) is globally unique, so use distinct ids per channel
    db.upsert_episode(_ep(0, CID))
    db.upsert_episode(_ep(1, CID))
    db.upsert_episode(_ep(9, CID2))
    assert set(db.get_all_channel_ids()) == {CID, CID2}


def test_delete_episodes_for_channel_returns_and_removes(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0, CID))
    db.upsert_episode(_ep(1, CID))
    db.upsert_episode(_ep(9, CID2))
    removed = db.delete_episodes_for_channel(CID)
    assert {r["id"] for r in removed} == {"v000", "v001"}
    assert db.get_episodes(CID) == []
    assert len(db.get_episodes(CID2)) == 1  # other channel untouched


def _run(cid, status="ok", n=0, started="2026-06-27T00:00:00+00:00"):
    return {
        "channel_id": cid, "channel_name": "C", "url": "u",
        "started_at": started, "finished_at": "2026-06-27T00:01:00+00:00",
        "status": status, "downloaded": n, "error": None,
    }


def test_poll_run_roundtrip_and_order(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.record_poll_run(_run(CID, "ok", 2))
    db.record_poll_run(_run(CID, "error"))
    runs = db.get_recent_poll_runs()
    assert len(runs) == 2
    assert runs[0]["status"] == "error"  # newest first (by id)
    assert runs[1]["downloaded"] == 2


def test_poll_run_retention(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "_POLL_RUNS_RETAIN", 5)
    for i in range(12):
        db.record_poll_run(_run(CID, "ok", i))
    runs = db.get_recent_poll_runs(100)
    assert len(runs) == 5
    # only the most recent five survive
    assert [r["downloaded"] for r in runs] == [11, 10, 9, 8, 7]


def test_last_poll_run_per_channel(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.record_poll_run(_run(CID, "ok", 1))
    db.record_poll_run(_run(CID, "error"))      # newer for CID
    db.record_poll_run(_run(CID2, "ok", 3))
    last = db.get_last_poll_run_per_channel()
    assert set(last) == {CID, CID2}
    assert last[CID]["status"] == "error"
    assert last[CID2]["downloaded"] == 3


def test_episode_counts_groups_by_channel(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0, CID))
    db.upsert_episode(_ep(1, CID))
    db.upsert_episode(_ep(9, CID2))
    assert db.episode_counts() == {CID: 2, CID2: 1}


def test_episode_counts_empty_when_no_episodes(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert db.episode_counts() == {}


def test_orphan_channel_ids_excludes_known_channels(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0, CID))       # orphan: no channels/unsubscribed row
    db.upsert_episode(_ep(1, CID2))
    db.add_channel("https://www.youtube.com/@A")
    db.update_channel_meta("https://www.youtube.com/@A", CID, "A")  # CID now owned
    db.upsert_unsubscribed_channel(CID2, "B")  # CID2 now owned
    assert db.orphan_channel_ids() == set()


def test_orphan_channel_ids_finds_truly_orphaned(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0, CID))
    assert db.orphan_channel_ids() == {CID}


def test_upsert_episode_is_idempotent_on_id(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0))
    dup = _ep(0)
    dup["title"] = "updated"
    db.upsert_episode(dup)  # same id -> replace, not duplicate
    eps = db.get_episodes(CID)
    assert len(eps) == 1 and eps[0]["title"] == "updated"
