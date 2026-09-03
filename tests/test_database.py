"""Roundtrip tests for database helpers (channel + episode lifecycle)."""
import os

import pytest

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
    """remove_channel is keyed on the primary key (channel_id), not the url."""
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.remove_channel(db.get_channels()[0]["channel_id"])
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


# --- global oldest-first ordering (disk-pressure pruner) ---------------------

def test_get_all_episodes_oldest_first_spans_channels(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    # Interleave two channels so a per-channel ordering would give a different
    # answer than the global one the pruner needs.
    db.upsert_episode(_ep(1, CID))    # 2026-06-02
    db.upsert_episode(_ep(0, CID2))   # 2026-06-01 — oldest overall
    db.upsert_episode(_ep(3, CID2))   # 2026-06-04
    db.upsert_episode(_ep(2, CID))    # 2026-06-03
    rows = db.get_all_episodes_oldest_first()
    assert [r["id"] for r in rows] == ["v000", "v001", "v002", "v003"]
    assert {r["channel_id"] for r in rows} == {CID, CID2}


# --- nightly backup ----------------------------------------------------------

def _setup_backups(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    backups = tmp_path / "backups"
    monkeypatch.setattr(db, "BACKUP_DIR", str(backups))
    return backups


def test_backup_db_produces_a_readable_snapshot(tmp_path, monkeypatch):
    import sqlite3
    _setup_backups(tmp_path, monkeypatch)
    for i in range(4):
        db.upsert_episode(_ep(i))

    path = db.backup_db()
    assert os.path.exists(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 4
    finally:
        conn.close()


def test_backup_db_does_not_clobber_a_same_second_file(tmp_path, monkeypatch):
    backups = _setup_backups(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0))
    first = db.backup_db()
    # VACUUM INTO refuses an existing destination; a second run inside the same
    # second must pick a fresh name rather than raising and losing the backup.
    monkeypatch.setattr(db, "datetime", _FrozenDatetime)
    second = db.backup_db()
    third = db.backup_db()
    assert second != third
    assert len({first, second, third}) == 3
    assert len(list(backups.iterdir())) == 3


class _FrozenDatetime:
    """datetime stand-in pinned to one second, to force a filename collision."""
    @staticmethod
    def now(tz=None):
        import datetime as _dt
        return _dt.datetime(2026, 9, 3, 3, 0, 0, tzinfo=_dt.timezone.utc)


def test_prune_backups_keeps_the_seven_newest(tmp_path, monkeypatch):
    backups = _setup_backups(tmp_path, monkeypatch)
    backups.mkdir()
    names = [f"episodes-2026090{i}-000000.db" for i in range(1, 10)]  # 9 files
    for n in names:
        (backups / n).write_text("x")
    (backups / "notes.txt").write_text("not a backup")  # must be left alone

    deleted = db.prune_backups()
    remaining = sorted(p.name for p in backups.iterdir() if p.name.endswith(".db"))
    assert len(remaining) == 7
    assert remaining == sorted(names[2:])          # the 7 newest by timestamp
    assert len(deleted) == 2
    assert (backups / "notes.txt").exists()


def test_prune_backups_tolerates_missing_dir(tmp_path, monkeypatch):
    _setup_backups(tmp_path, monkeypatch)  # never created
    assert db.prune_backups() == []


def test_integrity_check_ok_on_healthy_db(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert db.integrity_check() == "ok"


def test_run_backup_job_alerts_on_failed_integrity_check(tmp_path, monkeypatch):
    backups = _setup_backups(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0))
    bad = "*** in database main *** wrong # of entries in index idx_episodes_channel_id"
    monkeypatch.setattr(db, "integrity_check", lambda: bad)
    alerts = []
    monkeypatch.setattr(db.notify, "send_backup_failure_alert",
                        lambda reason, **kw: alerts.append(reason))

    db.run_backup_job()

    assert len(alerts) == 1
    assert bad in alerts[0]
    # A damaged-but-readable DB is still snapshotted — better than nothing.
    assert len(list(backups.iterdir())) == 1


def test_run_backup_job_happy_path_is_silent(tmp_path, monkeypatch):
    backups = _setup_backups(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0))
    alerts = []
    monkeypatch.setattr(db.notify, "send_backup_failure_alert",
                        lambda reason, **kw: alerts.append(reason))

    db.run_backup_job()

    assert alerts == []
    assert len(list(backups.iterdir())) == 1


def test_run_backup_job_alerts_when_backup_raises(tmp_path, monkeypatch):
    _setup_backups(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "backup_db",
                        lambda: (_ for _ in ()).throw(RuntimeError("disk full")))
    alerts = []
    monkeypatch.setattr(db.notify, "send_backup_failure_alert",
                        lambda reason, **kw: alerts.append(reason))

    db.run_backup_job()  # must not raise — a scheduled job crashing is the bug

    assert len(alerts) == 1 and "disk full" in alerts[0]


def test_run_backup_job_prunes_to_retention(tmp_path, monkeypatch):
    backups = _setup_backups(tmp_path, monkeypatch)
    backups.mkdir()
    for i in range(1, 10):
        (backups / f"episodes-2026080{i}-000000.db").write_text("x")
    db.upsert_episode(_ep(0))

    db.run_backup_job()  # writes one more (newest), then prunes to 7

    remaining = sorted(p.name for p in backups.iterdir())
    assert len(remaining) == db._BACKUP_RETAIN
    assert remaining[-1].startswith("episodes-20")  # today's snapshot survives


# --- feed tokens, settings, combined episodes -------------------------------

def _legacy_db(tmp_path, monkeypatch):
    """Build a pre-v1.14 database by hand (no feed_token / itunes_* / settings).

    Creating it with raw SQL rather than an older init_db() is the only way to
    prove the migration actually adds the columns to an existing file.
    """
    import sqlite3
    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE channels (
        url TEXT PRIMARY KEY, channel_id TEXT, channel_name TEXT,
        added_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.execute("""CREATE TABLE unsubscribed_channels (
        channel_id TEXT PRIMARY KEY, channel_name TEXT)""")
    conn.execute("INSERT INTO channels (url, channel_id, channel_name) VALUES (?, ?, ?)",
                 ("https://www.youtube.com/@A", CID, "A"))
    conn.execute("INSERT INTO channels (url) VALUES (?)",
                 ("https://www.youtube.com/@Unpolled",))  # channel_id still NULL
    conn.execute("INSERT INTO unsubscribed_channels (channel_id, channel_name) VALUES (?, ?)",
                 (CID2, "B"))
    conn.commit()
    conn.close()
    return path


def _tokens():
    with db.get_conn() as conn:
        rows = conn.execute("SELECT url, feed_token FROM channels").fetchall()
        un = conn.execute(
            "SELECT channel_id, feed_token FROM unsubscribed_channels").fetchall()
    return {r["url"]: r["feed_token"] for r in rows} | {r["channel_id"]: r["feed_token"] for r in un}


def test_migration_adds_columns_and_backfills_tokens(tmp_path, monkeypatch):
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()
    with db.get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
        un_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(unsubscribed_channels)").fetchall()}
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"feed_token", "itunes_category", "itunes_language", "itunes_explicit"} <= cols
    assert "feed_token" in un_cols
    assert "settings" in tables

    tokens = _tokens()
    assert len(tokens) == 3
    assert all(t for t in tokens.values())          # every row got one
    assert len(set(tokens.values())) == 3           # and each is distinct


def test_migration_is_idempotent_and_never_rotates_tokens(tmp_path, monkeypatch):
    """init_db() runs on EVERY startup. A rotated token would silently break
    every already-subscribed podcast app once enforcement is on — the worst
    failure mode in this feature."""
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()
    before = _tokens()
    all_before = db.get_or_create_all_feed_token()

    db.init_db()
    db.init_db()

    assert _tokens() == before
    assert db.get_or_create_all_feed_token() == all_before


# --- channels primary-key migration (v1.15.0) --------------------------------

def _pk_flags():
    """{column: (pk, notnull)} for channels, the shape assertion that survives
    ALTER TABLE ... RENAME rewriting the stored CREATE statement."""
    with db.get_conn() as conn:
        return {r["name"]: (r["pk"], r["notnull"])
                for r in conn.execute("PRAGMA table_info(channels)").fetchall()}


def _v114_db(tmp_path, monkeypatch, rows):
    """Build a v1.14-shaped database (url PRIMARY KEY, feed_token present) with
    the given (url, channel_id, channel_name, feed_token) rows, in order."""
    import sqlite3
    path = str(tmp_path / "v114.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE channels (
        url TEXT PRIMARY KEY, channel_id TEXT, channel_name TEXT,
        added_at TEXT NOT NULL DEFAULT (datetime('now')),
        feed_token TEXT, itunes_category TEXT, itunes_language TEXT,
        itunes_explicit TEXT)""")
    conn.executemany(
        "INSERT INTO channels (url, channel_id, channel_name, feed_token) "
        "VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return path


def test_fresh_install_gets_the_channel_id_pk_schema(tmp_path, monkeypatch):
    """AC1: an empty DB is built in the new shape directly, no migration."""
    _setup_tmp(tmp_path, monkeypatch)
    flags = _pk_flags()
    assert flags["channel_id"] == (1, 1)   # primary key, NOT NULL
    assert flags["url"] == (0, 1)          # attribute, NOT NULL (UNIQUE)


def test_migration_flips_the_primary_key(tmp_path, monkeypatch):
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()
    flags = _pk_flags()
    assert flags["channel_id"] == (1, 1)
    assert flags["url"] == (0, 1)
    assert {r["url"] for r in db.get_channels()} == {
        "https://www.youtube.com/@A", "https://www.youtube.com/@Unpolled"}


def test_migration_gives_a_null_channel_id_a_pending_identity(tmp_path, monkeypatch):
    """The @Unpolled row (channel_id NULL) gets a placeholder that can never
    reach the filesystem or a route — the colon fails _CHANNEL_ID_RE."""
    from app import downloader
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()
    row = next(r for r in db.get_channels()
               if r["url"] == "https://www.youtube.com/@Unpolled")
    assert row["channel_id"] is not None
    assert db.is_pending_channel_id(row["channel_id"])
    assert downloader._CHANNEL_ID_RE.match(row["channel_id"]) is None


def test_migration_is_idempotent_across_schema_rows_and_tokens(tmp_path, monkeypatch):
    """AC3: init_db() runs on every boot; runs 2 and 3 must change nothing."""
    _legacy_db(tmp_path, monkeypatch)
    db.init_db()
    flags, tokens = _pk_flags(), _tokens()
    rows = [tuple(r) for r in db.get_channels()]

    db.init_db()
    db.init_db()

    assert _pk_flags() == flags
    assert _tokens() == tokens
    assert [tuple(r) for r in db.get_channels()] == rows


def test_migration_takes_an_unprunable_pre_migration_backup(tmp_path, monkeypatch):
    """AC4: the snapshot is taken BEFORE the migration (it still holds the old
    schema), and its prefix keeps it outside prune_backups()' glob forever."""
    import sqlite3
    _legacy_db(tmp_path, monkeypatch)
    backups = tmp_path / "backups"
    monkeypatch.setattr(db, "BACKUP_DIR", str(backups))

    db.init_db()

    snaps = [p for p in backups.iterdir() if p.name.startswith("pre-pk-migration-")]
    assert len(snaps) == 1
    conn = sqlite3.connect(str(snaps[0]))
    try:
        pk = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
        assert pk["url"] == 1 and pk["channel_id"] == 0      # the OLD schema
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 2
    finally:
        conn.close()
    assert db.prune_backups(retain=0) == []                  # never pruned
    assert snaps[0].exists()


def test_migration_rolls_back_completely_on_failure(tmp_path, monkeypatch):
    """The BEGIN IMMEDIATE test. Without it the CREATE TABLE channels_new
    commits on its own and the next boot dies on "table already exists"."""
    import sqlite3
    from contextlib import contextmanager
    _legacy_db(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    real_get_conn = db.get_conn

    class _FailOnRename:
        """Connection proxy that blows up at the migration's final statement."""
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **k):
            if "RENAME TO channels" in sql:
                raise RuntimeError("simulated crash mid-migration")
            return self._conn.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def _wrapped():
        with real_get_conn() as conn:
            yield _FailOnRename(conn)

    monkeypatch.setattr(db, "get_conn", _wrapped)
    with pytest.raises(RuntimeError):
        db.init_db()   # must NOT swallow it — a half-migrated DB must not be served
    monkeypatch.undo()

    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "channels" in tables and "channels_new" not in tables
        pk = {r[1]: r[5] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
        assert pk["url"] == 1                                # still the old schema
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 2
    finally:
        conn.close()


def test_migration_collapses_duplicate_channel_ids_keeping_the_oldest(tmp_path, monkeypatch):
    """Two rows sharing one channel_id are a UNIQUE violation under the new PK.
    The first-added row wins, because it holds the feed token a podcast app is
    most likely already subscribed with."""
    _v114_db(tmp_path, monkeypatch, [
        ("https://www.youtube.com/@A", CID, "A", "token-incumbent"),
        ("https://www.youtube.com/channel/" + CID, CID, "A", "token-duplicate"),
        ("https://www.youtube.com/@B", CID2, "B", "token-b"),
    ])
    db.init_db()
    rows = {r["url"]: r for r in db.get_channels()}
    assert set(rows) == {"https://www.youtube.com/@A", "https://www.youtube.com/@B"}
    assert rows["https://www.youtube.com/@A"]["feed_token"] == "token-incumbent"
    assert rows["https://www.youtube.com/@B"]["feed_token"] == "token-b"


def test_migration_preserves_every_feed_token(tmp_path, monkeypatch):
    """The highest-severity silent failure mode: a rotated token stops a live
    podcast subscription updating without anything erroring."""
    _v114_db(tmp_path, monkeypatch, [
        ("https://www.youtube.com/@A", CID, "A", "token-a"),
        ("https://www.youtube.com/@B", CID2, "B", "token-b"),
    ])
    db.init_db()
    assert {r["channel_id"]: r["feed_token"] for r in db.get_channels()} == {
        CID: "token-a", CID2: "token-b"}


def test_add_then_resolve_swaps_the_primary_key_in_place(tmp_path, monkeypatch):
    """AC5: a newly added channel is pending until its first poll resolves it;
    the swap keeps the same row, so added_at and feed_token survive."""
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    pending = db.get_channels()[0]
    assert db.is_pending_channel_id(pending["channel_id"])
    assert db.get_channel_id_for_url(url) is None   # "not resolved yet", as before
    # A token issued against the placeholder must ride along on the swap — it
    # is the same row, and rotating it would break a subscription.
    token = db.get_or_create_feed_token(pending["channel_id"])
    assert token

    db.update_channel_meta(url, CID, "A")

    rows = db.get_channels()
    assert len(rows) == 1
    assert rows[0]["channel_id"] == CID
    assert rows[0]["added_at"] == pending["added_at"]
    assert rows[0]["feed_token"] == token
    assert db.get_feed_token(CID) == token
    assert db.get_channel_id_for_url(url) == CID


def test_resolving_onto_an_existing_channel_id_keeps_the_incumbent(tmp_path, monkeypatch):
    """AC5b: two URL variants of one channel. The already-resolved row keeps its
    feed token and settings; the duplicate is dropped rather than raising an
    IntegrityError in the middle of a poll."""
    _setup_tmp(tmp_path, monkeypatch)
    url_a = "https://www.youtube.com/@A"
    url_b = "https://www.youtube.com/channel/" + CID
    db.add_channel(url_a)
    db.update_channel_meta(url_a, CID, "A")
    db.set_channel_feed_settings(CID, "Comedy", "es", "clean")
    incumbent = db.get_channels()[0]
    db.add_channel(url_b)

    db.update_channel_meta(url_b, CID, "A")   # must not raise

    rows = db.get_channels()
    assert len(rows) == 1
    assert rows[0]["url"] == url_a
    assert rows[0]["feed_token"] == incumbent["feed_token"]
    assert rows[0]["itunes_category"] == "Comedy"


def test_add_channel_with_id_is_idempotent(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/channel/" + CID
    db.add_channel_with_id(CID, url, "A")
    token = db.get_channels()[0]["feed_token"] or db.get_or_create_feed_token(CID)
    db.add_channel_with_id(CID, url, "Renamed")
    rows = db.get_channels()
    assert len(rows) == 1
    assert rows[0]["channel_name"] == "Renamed"
    assert db.get_or_create_feed_token(CID) == token   # never rotated


def test_get_channel_by_url(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    assert db.get_channel_by_url(url) is None
    db.add_channel(url)
    db.update_channel_meta(url, CID, "A")
    assert db.get_channel_by_url(url)["channel_id"] == CID


def test_get_or_create_feed_token_is_stable(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "A")
    first = db.get_or_create_feed_token(CID)
    assert first
    assert db.get_or_create_feed_token(CID) == first
    assert db.get_feed_token(CID) == first


def test_get_or_create_feed_token_covers_a_row_added_after_migration(tmp_path, monkeypatch):
    """A channels row's channel_id is NULL until the first successful poll, so
    the init_db() backfill can't key on it — the lazy path has to."""
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_unsubscribed_channel(CID2, "B")
    token = db.get_or_create_feed_token(CID2)
    assert token and db.get_feed_token(CID2) == token


def test_feed_token_is_none_for_unknown_channel(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert db.get_feed_token("UCnothing1234567890123456") is None
    assert db.get_or_create_feed_token("UCnothing1234567890123456") is None


def test_all_feed_token_is_stable(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    first = db.get_or_create_all_feed_token()
    assert first
    assert db.get_or_create_all_feed_token() == first
    assert db.get_setting("all_feed_token") == first


def test_settings_roundtrip(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert db.get_setting("nope") is None
    db.set_setting("k", "v1")
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"


def test_get_episode_returns_none_for_unknown(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert db.get_episode("nope") is None
    db.upsert_episode(_ep(1))
    assert db.get_episode("v001")["title"] == "t1"


def test_get_combined_episodes_excludes_unsubscribed_and_dedupes(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    # Adding a second URL variant for a channel we already have used to create a
    # second channels row sharing one channel_id; since 1.15 the second row's
    # first poll collapses into the first, so there is nothing left to
    # double-count.
    db.add_channel("https://www.youtube.com/@A")
    db.update_channel_meta("https://www.youtube.com/@A", CID, "A")
    db.add_channel("https://www.youtube.com/channel/" + CID)
    db.update_channel_meta("https://www.youtube.com/channel/" + CID, CID, "A")
    assert [r["url"] for r in db.get_channels()] == ["https://www.youtube.com/@A"]
    db.upsert_unsubscribed_channel(CID2, "B")

    db.upsert_episode(_ep(1, CID))
    db.upsert_episode(_ep(2, CID))
    db.upsert_episode(_ep(3, CID2))  # one-off — must not appear

    rows = db.get_combined_episodes(100)
    assert [r["id"] for r in rows] == ["v002", "v001"]  # newest first, no dupes


def test_get_combined_episodes_respects_limit(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    db.add_channel("https://www.youtube.com/@A")
    db.update_channel_meta("https://www.youtube.com/@A", CID, "A")
    for i in range(1, 8):
        db.upsert_episode(_ep(i))
    rows = db.get_combined_episodes(3)
    assert [r["id"] for r in rows] == ["v007", "v006", "v005"]


def test_channel_feed_settings_roundtrip(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert db.get_channel_feed_settings(CID) is None  # unknown channel
    db.add_channel("https://www.youtube.com/@A")
    db.update_channel_meta("https://www.youtube.com/@A", CID, "A")
    row = db.get_channel_feed_settings(CID)
    assert (row["itunes_category"], row["itunes_language"], row["itunes_explicit"]) \
        == (None, None, None)
    db.set_channel_feed_settings(CID, "Comedy", "es", "clean")
    row = db.get_channel_feed_settings(CID)
    assert row["itunes_category"] == "Comedy"
    db.set_channel_feed_settings(CID, None, None, None)
    assert db.get_channel_feed_settings(CID)["itunes_category"] is None
