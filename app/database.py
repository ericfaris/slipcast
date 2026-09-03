import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH


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
