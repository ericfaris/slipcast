import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app import notify
from app.config import BACKUP_DIR, DB_PATH

logger = logging.getLogger(__name__)


def init_db():
    with get_conn() as conn:
        # WAL lets readers (e.g. /api/state, polled every few seconds by the
        # dashboard) proceed without blocking on a writer, and vice versa —
        # important once multiple poll threads can write concurrently (see
        # POLL_CONCURRENCY). It's a durable, one-time setting stored in the DB
        # file itself, so this only needs to run at init, not per connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                url          TEXT PRIMARY KEY,
                channel_id   TEXT,
                channel_name TEXT,
                added_at     TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS unsubscribed_channels (
                channel_id   TEXT PRIMARY KEY,
                channel_name TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id          TEXT PRIMARY KEY,
                channel_id  TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                title       TEXT NOT NULL,
                description TEXT,
                published   TEXT NOT NULL,
                duration    INTEGER,
                filename    TEXT NOT NULL,
                filesize    INTEGER,
                thumbnail   TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skip_videos (
                video_id   TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                reason     TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id   TEXT,
                channel_name TEXT,
                url          TEXT,
                started_at   TEXT NOT NULL,
                finished_at  TEXT,
                status       TEXT NOT NULL,   -- 'ok' | 'error'
                downloaded   INTEGER NOT NULL DEFAULT 0,
                error        TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_poll_runs_started ON poll_runs(started_at DESC)")
        # episodes is filtered by channel_id on every /api/state, feed build, and
        # episode-listing request — without an index each of those is a full
        # table scan.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_channel_id ON episodes(channel_id)")
        # migrate existing DBs
        cols = {r[1] for r in conn.execute("PRAGMA table_info(episodes)").fetchall()}
        if "thumbnail" not in cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN thumbnail TEXT")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL still serializes writers; without a busy_timeout a second writer
    # (concurrent poll threads, see POLL_CONCURRENCY) gets an immediate
    # "database is locked" instead of waiting briefly for the first to finish.
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        # Don't rely on sqlite3's implicit rollback-on-close — be explicit so a
        # half-applied multi-statement write (e.g. record_poll_run's insert +
        # prune) never gets partially committed on close.
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_episode(ep: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO episodes
                (id, channel_id, channel_name, title, description, published, duration, filename, filesize, thumbnail)
            VALUES
                (:id, :channel_id, :channel_name, :title, :description, :published, :duration, :filename, :filesize, :thumbnail)
        """, ep)


def get_episodes(channel_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM episodes
            WHERE channel_id = ?
            ORDER BY published DESC
        """, (channel_id,)).fetchall()


def get_all_episodes_oldest_first() -> list[sqlite3.Row]:
    """Every episode across all channels, oldest first.

    Used by the disk-pressure pruner (app/downloader.py), which frees space by
    globally oldest episode rather than per channel — unlike get_episodes(),
    which is per-channel and newest-first for feed building.
    """
    with get_conn() as conn:
        return conn.execute("SELECT * FROM episodes ORDER BY published ASC").fetchall()


def get_all_channel_ids() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT channel_id FROM episodes").fetchall()
        return [r["channel_id"] for r in rows]


def episode_counts() -> dict[str, int]:
    """{channel_id: episode count} for every channel, in one query.

    /api/state is polled by the dashboard every few seconds and previously
    called get_episodes() (loads every row) once per channel to get a count —
    O(channels * episodes) work on every poll of the poll endpoint itself.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT channel_id, COUNT(*) AS n FROM episodes GROUP BY channel_id"
        ).fetchall()
        return {r["channel_id"]: r["n"] for r in rows}


def orphan_channel_ids() -> set[str]:
    """channel_ids referenced by episodes but owned by neither channels nor
    unsubscribed_channels.

    This happens when a channels row is deleted without its channel_id ever
    being resolved (see main._resolve_channel_id_for_removal) — the episodes,
    skip_videos, and on-disk files are left behind with no row and no UI to
    find them. This is the DB-side half of orphan detection; app.downloader
    also checks for on-disk directories with no matching row at all (a channel
    whose channel_id was resolved and removed, but whose files failed to
    delete, or that never had an episode row to begin with).
    """
    with get_conn() as conn:
        ep_ids = {r["channel_id"] for r in
                  conn.execute("SELECT DISTINCT channel_id FROM episodes").fetchall()}
        known = {r["channel_id"] for r in
                 conn.execute("SELECT channel_id FROM channels WHERE channel_id IS NOT NULL").fetchall()}
        known |= {r["channel_id"] for r in
                  conn.execute("SELECT channel_id FROM unsubscribed_channels").fetchall()}
    return ep_ids - known


def delete_episode(episode_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))


def add_channel(url: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO channels (url) VALUES (?)", (url,))


def remove_channel(url: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM channels WHERE url = ?", (url,))


def get_channels() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM channels ORDER BY added_at").fetchall()


def get_channel_meta(channel_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT url, channel_id, channel_name FROM channels WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()


def get_channel_id_for_url(url: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT channel_id FROM channels WHERE url = ?", (url,)
        ).fetchone()
        return row["channel_id"] if row and row["channel_id"] else None


def update_channel_meta(url: str, channel_id: str, channel_name: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE channels SET channel_id = ?, channel_name = ? WHERE url = ?",
            (channel_id, channel_name, url)
        )


def get_unsubscribed_channels() -> list:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM unsubscribed_channels ORDER BY channel_name").fetchall()


def remove_unsubscribed_channel(channel_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM unsubscribed_channels WHERE channel_id = ?", (channel_id,))


def upsert_unsubscribed_channel(channel_id: str, channel_name: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO unsubscribed_channels (channel_id, channel_name) VALUES (?, ?)",
            (channel_id, channel_name)
        )


def add_skip_video(video_id: str, channel_id: str, reason: str = ""):
    """Remember a video we should not re-attempt on future polls (e.g. members-only)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO skip_videos (video_id, channel_id, reason) VALUES (?, ?, ?)",
            (video_id, channel_id, reason),
        )


def get_skip_video_ids(channel_id: str) -> set:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT video_id FROM skip_videos WHERE channel_id = ?", (channel_id,)
        ).fetchall()
        return {r["video_id"] for r in rows}


def delete_skip_videos_for_channel(channel_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM skip_videos WHERE channel_id = ?", (channel_id,))


def delete_episodes_for_channel(channel_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM episodes WHERE channel_id = ?", (channel_id,)
        ).fetchall()
        conn.execute("DELETE FROM episodes WHERE channel_id = ?", (channel_id,))
    return rows


# --- poll history -----------------------------------------------------------

# Keep the table bounded; we only ever surface the most recent runs.
_POLL_RUNS_RETAIN = 300


def record_poll_run(run: dict) -> None:
    """Persist one channel poll outcome. Expected keys: channel_id, channel_name,
    url, started_at, finished_at, status ('ok'|'error'), downloaded, error."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO poll_runs
               (channel_id, channel_name, url, started_at, finished_at, status, downloaded, error)
               VALUES (:channel_id, :channel_name, :url, :started_at, :finished_at, :status, :downloaded, :error)""",
            {
                "channel_id": run.get("channel_id"),
                "channel_name": run.get("channel_name"),
                "url": run.get("url"),
                "started_at": run["started_at"],
                "finished_at": run.get("finished_at"),
                "status": run["status"],
                "downloaded": run.get("downloaded", 0),
                "error": run.get("error"),
            },
        )
        conn.execute(
            """DELETE FROM poll_runs WHERE id NOT IN
               (SELECT id FROM poll_runs ORDER BY id DESC LIMIT ?)""",
            (_POLL_RUNS_RETAIN,),
        )


def get_recent_poll_runs(limit: int = 25) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM poll_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_last_poll_run_per_channel() -> dict[str, sqlite3.Row]:
    """Most recent run for each channel_id, keyed by channel_id."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT pr.* FROM poll_runs pr
               JOIN (SELECT channel_id, MAX(id) AS mid FROM poll_runs
                     WHERE channel_id IS NOT NULL GROUP BY channel_id) last
               ON pr.id = last.mid"""
        ).fetchall()
    return {r["channel_id"]: r for r in rows}


# --- backups ----------------------------------------------------------------

# One snapshot a night, so this is a week of history. Enough to notice and roll
# back a problem that took a few days to surface, without the backups
# themselves becoming the thing that fills the disk (each is roughly the size
# of the live DB, which is small — it holds metadata, not audio).
_BACKUP_RETAIN = 7


def backup_db() -> str:
    """Write a timestamped snapshot of the database into BACKUP_DIR.

    VACUUM INTO rather than a file copy or Connection.backup(): the DB runs in
    WAL mode (see init_db), where the live file on its own is not a complete
    database — a plain copy without the -wal sidecar can lose the most recent
    writes. VACUUM INTO takes a read transaction and emits a single, fully
    checkpointed, non-WAL file, so it is safe to run while polls are writing
    and the result is one self-contained file with no sidecars to keep with it.
    Returns the path written.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"episodes-{stamp}.db")
    # VACUUM INTO refuses to write to an existing file rather than overwriting
    # it. A same-second collision needs two runs of this job in one second,
    # which shouldn't happen — but a raise here would lose the night's backup,
    # so fall back to a unique name instead of failing.
    if os.path.exists(dest):
        dest = os.path.join(BACKUP_DIR, f"episodes-{stamp}-{secrets.token_hex(2)}.db")
    with get_conn() as conn:
        conn.execute("VACUUM INTO ?", (dest,))
    return dest


def prune_backups(retain: int = _BACKUP_RETAIN) -> list[str]:
    """Delete all but the `retain` newest snapshots. Returns the paths removed.

    The YYYYMMDD-HHMMSS stamp sorts lexicographically, so a plain reverse
    filename sort is a chronological sort — no stat() per file needed.
    """
    try:
        names = [n for n in os.listdir(BACKUP_DIR)
                 if n.startswith("episodes-") and n.endswith(".db")]
    except OSError:
        # No backup dir yet (first run, or the volume was replaced) — nothing
        # to prune, and the next backup_db() will create it.
        return []
    deleted: list[str] = []
    for name in sorted(names, reverse=True)[retain:]:
        path = os.path.join(BACKUP_DIR, name)
        try:
            os.remove(path)
            deleted.append(path)
        except OSError as exc:
            logger.warning("Could not delete old backup %s: %s", path, exc)
    return deleted


def integrity_check() -> str:
    """PRAGMA integrity_check — returns "ok" on a healthy database.

    Kept as a thin wrapper so the backup job's failure path is testable.
    """
    with get_conn() as conn:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]


def run_backup_job() -> None:
    """Scheduled nightly: verify the database, snapshot it, prune old snapshots.

    Detection only — a failing integrity check emails a human rather than
    attempting an automatic restore, which could overwrite a recoverable
    database using a condition that is still actively corrupting it.
    """
    try:
        result = integrity_check()
        if result != "ok":
            logger.error("Database integrity check FAILED: %s", result)
            notify.send_backup_failure_alert(
                f"PRAGMA integrity_check returned: {result}"
            )
            # Still take the snapshot: a copy of a damaged-but-readable
            # database is strictly better than no copy at all, and the
            # already-retained older snapshots are what you'd actually restore.

        path = backup_db()
        logger.info("Database backup written: %s (%d bytes)", path, os.path.getsize(path))
        deleted = prune_backups()
        if deleted:
            logger.info("Pruned %d old backup(s), keeping the newest %d",
                        len(deleted), _BACKUP_RETAIN)
    except Exception as exc:  # noqa: BLE001 — a scheduled job must never
        # crash silently; that class of failure is exactly what this pass exists
        # to eliminate.
        logger.exception("Database backup job failed")
        notify.send_backup_failure_alert(f"backup failed: {exc}")
