import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app import notify
from app.config import BACKUP_DIR, DB_PATH

logger = logging.getLogger(__name__)


# --- channel identity -------------------------------------------------------

# The placeholder identity a channels row carries between "the user added this
# URL" and "the first successful poll told us the real YouTube channel id".
# Since 1.15 channel_id is the primary key of `channels`, so a row has to have
# one from the instant it is inserted; this is that value.
#
# The colon in the prefix is load-bearing. It makes a placeholder fail
# _CHANNEL_ID_RE (^[A-Za-z0-9_-]{1,64}$) — the validator used by
# downloader._audio_dir_for()/_thumbnail_dir_for() (which raise on a non-match)
# and by every channel_id-taking route in app/main.py (which return HTTP 400).
# So a placeholder that ever leaks toward a filesystem path or a public URL
# fails loudly and immediately instead of quietly creating a
# data/audio/pending:…/ directory. It also cannot collide with a real YouTube
# channel id (UC…, 24 chars, same charset), and a client cannot submit one as a
# valid channel_id. If this format ever changes, re-derive that property.
_PENDING_PREFIX = "pending:"


def new_pending_channel_id() -> str:
    """A fresh placeholder identity for a channels row that has never polled."""
    return _PENDING_PREFIX + secrets.token_hex(16)


def is_pending_channel_id(cid: str | None) -> bool:
    """True when a channel_id is a placeholder, i.e. "not resolved yet".

    This is the masking predicate: at the two boundaries that used to observe a
    NULL channel_id — get_channel_id_for_url() and main.api_state() — a pending
    id is reported back as None, which keeps the JSON API and the poller's
    "have we resolved this channel yet?" logic byte-identical to pre-1.15.
    """
    return bool(cid) and cid.startswith(_PENDING_PREFIX)


# --- channels primary-key migration (v1.15.0) -------------------------------

# The new-schema column list, in the order the migrated table declares them.
# The carried-column list is built from this whitelist (never from raw input),
# which is what makes the f-string interpolation in the migration safe.
_CHANNELS_COLUMNS = ("channel_id", "url", "channel_name", "added_at", "feed_token",
                     "itunes_category", "itunes_language", "itunes_explicit")

_CHANNELS_NEW_DDL = """
    CREATE TABLE channels_new (
        channel_id      TEXT PRIMARY KEY NOT NULL,
        url             TEXT NOT NULL UNIQUE,
        channel_name    TEXT,
        added_at        TEXT NOT NULL DEFAULT (datetime('now')),
        feed_token      TEXT,
        itunes_category TEXT,
        itunes_language TEXT,
        itunes_explicit TEXT
    )
"""

# The rows the migration keeps: one per distinct channel_id (the lowest rowid,
# i.e. the first added — the one most likely to hold the feed_token a podcast
# app is already subscribed with), plus every channel_id IS NULL row (those
# were distinct by url, and each gets its own placeholder identity).
_CHANNELS_KEEP_FILTER = """
    rowid IN (SELECT MIN(rowid) FROM channels
              WHERE channel_id IS NOT NULL GROUP BY channel_id)
    OR channel_id IS NULL
"""


def _needs_channels_pk_migration(conn) -> bool:
    """True when `channels` still has the pre-1.15 `url PRIMARY KEY` shape.

    PRAGMA table_info's `pk` flag is the detection, not a string match on the
    stored CREATE statement: ALTER TABLE ... RENAME rewrites that text (it comes
    back as `CREATE TABLE "channels"`, quoted), so comparing SQL would be
    fragile. No channels table at all means a fresh DB — nothing to migrate; the
    CREATE TABLE IF NOT EXISTS in init_db() builds the new shape directly.
    """
    return any(r["name"] == "url" and r["pk"] for r in
               conn.execute("PRAGMA table_info(channels)").fetchall())


def _migrate_channels_to_channel_id_pk() -> None:
    """Rewrite `channels` so channel_id is the primary key and url an attribute.

    Every other table (episodes, skip_videos, unsubscribed_channels, poll_runs),
    every on-disk directory and every feed URL is already keyed by channel_id;
    only `channels` keyed on url. That split identity is what allowed a delete
    (keyed on url) and its cascade (keyed on a separately-resolved channel_id)
    to disagree and strand data — the bug patched in v1.11.0. One key kills it.

    SQLite cannot change a primary key in place, so this is the standard
    create-copy-drop-rename dance. Three ordering constraints make it live in
    its own short-lived connection, opened and closed before init_db()'s main
    block rather than inside it:

    * The backup must be taken BEFORE the transaction opens — backup_db() uses
      VACUUM INTO, which cannot run inside a transaction, and a snapshot taken
      mid-migration would capture the half-migrated state, defeating the point.
    * PRAGMA journal_mode=WAL (init_db()'s first statement) also cannot run
      inside a transaction.
    * A second connection opened while the first held a write lock would just
      contend with it.

    BEGIN IMMEDIATE is mandatory, not decoration. The Python driver does not
    open a transaction for DDL (conn.in_transaction stays False after a CREATE
    TABLE), so without an explicit BEGIN the `CREATE TABLE channels_new` commits
    on its own; a failure at any later point then strands a channels_new table
    in the file, and the next boot's migration dies on "table already exists" —
    an app that cannot start. With the explicit BEGIN the whole sequence rolls
    back cleanly. Raises on failure for the same reason: a half-understood
    database must not be served by code that assumes the new schema. The
    operator restores the snapshot whose path is logged just below.
    """
    with get_conn() as conn:
        if not _needs_channels_pk_migration(conn):
            return  # fresh DB or already migrated — this is the idempotency guarantee
        old_cols = [r["name"] for r in conn.execute("PRAGMA table_info(channels)").fetchall()]
        before = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        null_urls = conn.execute("SELECT COUNT(*) FROM channels WHERE url IS NULL").fetchone()[0]
        dropped = [r["url"] for r in conn.execute(
            "SELECT url FROM channels WHERE channel_id IS NOT NULL "
            "AND rowid NOT IN (SELECT MIN(rowid) FROM channels "
            "                  WHERE channel_id IS NOT NULL GROUP BY channel_id)"
        ).fetchall()]

    logger.warning("Migrating `channels` to a channel_id primary key (%d row(s))", before)
    snapshot = backup_db(prefix="pre-pk-migration")
    logger.warning("Pre-migration snapshot written: %s — restore this file (and the "
                   "previous image; older code cannot write the new schema) if "
                   "anything looks wrong afterwards", snapshot)
    if dropped:
        logger.warning("Collapsing %d duplicate channel row(s) sharing a channel_id "
                       "with an older row; these URLs will disappear from the "
                       "dashboard: %s", len(dropped), ", ".join(dropped))
    if null_urls:
        logger.warning("%d channel row(s) had a NULL url and are being given a "
                       "placeholder 'unknown:' URL so the migration can proceed",
                       null_urls)

    # Only the columns the new table declares AND the old table actually has,
    # so one migration handles a v1.14 database (which has feed_token/itunes_*)
    # and any older one (which doesn't — those simply come out NULL, and
    # init_db()'s ALTER TABLE ADD COLUMN block then becomes a no-op because the
    # new table already declares them). channel_id/url are excluded here; they
    # are handled by the COALESCE expressions below.
    carried = [c for c in _CHANNELS_COLUMNS
               if c not in ("channel_id", "url") and c in old_cols]
    carried_sql = "".join(f", {c}" for c in carried)

    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Belt-and-braces against a pre-1.15 build that crashed mid-migration
            # without the transaction. The BEGIN above is the real fix.
            conn.execute("DROP TABLE IF EXISTS channels_new")
            conn.execute(_CHANNELS_NEW_DDL)
            # COALESCE on channel_id gives an unpolled row a placeholder
            # identity. COALESCE on url is purely defensive: url was a
            # rowid-table TEXT PRIMARY KEY, which SQLite (legacy quirk) lets
            # hold NULL, and NOT NULL would abort the migration — an aborting
            # init_db() means the container never starts. A junk-but-present URL
            # beats a boot loop.
            conn.execute(f"""
                INSERT INTO channels_new (channel_id, url{carried_sql})
                SELECT COALESCE(channel_id, ? || lower(hex(randomblob(16)))),
                       COALESCE(url, 'unknown:' || lower(hex(randomblob(8)))){carried_sql}
                FROM channels
                WHERE {_CHANNELS_KEEP_FILTER}
            """, (_PENDING_PREFIX,))
            conn.execute("DROP TABLE channels")
            conn.execute("ALTER TABLE channels_new RENAME TO channels")
            conn.execute("COMMIT")
        except Exception:
            logger.exception("channels primary-key migration FAILED — rolling back. "
                             "The database is unchanged; restore %s if in doubt",
                             snapshot)
            conn.rollback()
            raise
        after = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        logger.warning("channels migration complete: %d row(s) in, %d row(s) out, "
                       "integrity_check=%s", before, after, result)


def init_db():
    # First, before anything else touches the table. It has to precede the
    # CREATE TABLE IF NOT EXISTS below (a silent no-op on an existing table, so
    # an old-schema DB would otherwise keep the old schema forever while the
    # code assumed the new one), the ALTER TABLE ADD COLUMN block, and the
    # feed-token backfill (whose WHERE channel_id = ? key only exists after the
    # migration).
    _migrate_channels_to_channel_id_pk()
    with get_conn() as conn:
        # WAL lets readers (e.g. /api/state, polled every few seconds by the
        # dashboard) proceed without blocking on a writer, and vice versa —
        # important once multiple poll threads can write concurrently (see
        # POLL_CONCURRENCY). It's a durable, one-time setting stored in the DB
        # file itself, so this only needs to run at init, not per connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id      TEXT PRIMARY KEY NOT NULL,
                url             TEXT NOT NULL UNIQUE,
                channel_name    TEXT,
                added_at        TEXT NOT NULL DEFAULT (datetime('now')),
                feed_token      TEXT,
                itunes_category TEXT,
                itunes_language TEXT,
                itunes_explicit TEXT
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
        # Feed access tokens + per-channel iTunes metadata overrides. Every new
        # column is nullable, so an older DB keeps working untouched until
        # something writes to one; NULL means "use the built-in default" for the
        # itunes_* columns (see app/feed.py).
        ch_cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
        for col in ("feed_token", "itunes_category", "itunes_language", "itunes_explicit"):
            if col not in ch_cols:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {col} TEXT")
        un_cols = {r[1] for r in conn.execute("PRAGMA table_info(unsubscribed_channels)").fetchall()}
        if "feed_token" not in un_cols:
            conn.execute("ALTER TABLE unsubscribed_channels ADD COLUMN feed_token TEXT")
        # Install-wide key/value settings. First (and so far only) user: the
        # combined feed's access token, which belongs to no single channel.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        # One-time backfill so every existing row has a token the moment this
        # ships — turning REQUIRE_FEED_TOKENS on later then needs no second
        # migration. The WHERE feed_token IS NULL guard is what makes init_db()
        # (which runs on EVERY startup) idempotent: rotating a token here would
        # silently break every already-subscribed podcast app. Each row needs
        # its own secret, so this is a per-row loop rather than one UPDATE.
        for row in conn.execute(
                "SELECT channel_id FROM channels WHERE feed_token IS NULL").fetchall():
            conn.execute("UPDATE channels SET feed_token = ? WHERE channel_id = ?",
                         (secrets.token_urlsafe(24), row["channel_id"]))
        for row in conn.execute(
                "SELECT channel_id FROM unsubscribed_channels WHERE feed_token IS NULL").fetchall():
            conn.execute("UPDATE unsubscribed_channels SET feed_token = ? WHERE channel_id = ?",
                         (secrets.token_urlsafe(24), row["channel_id"]))


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


def get_episode(episode_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()


def get_combined_episodes(limit: int) -> list[sqlite3.Row]:
    """Newest-first episodes across all *subscribed* channels, for /feed/all.xml.

    A subquery rather than a JOIN on channels. As of 1.15 channel_id is that
    table's primary key, so two rows can no longer share one and a JOIN would no
    longer double an episode — but the subquery is equally correct, states the
    intent ("is this channel subscribed?") more directly than a join whose
    one-row-ness is an implicit schema assumption, and keeps the diff small.
    """
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM episodes
            WHERE channel_id IN
                (SELECT channel_id FROM channels WHERE channel_id IS NOT NULL)
            ORDER BY published DESC
            LIMIT ?
        """, (limit,)).fetchall()


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

    Since 1.15 a channels row and its cascade share one identity, so the normal
    delete path can no longer strand anything; what is left is a backstop for a
    delete interrupted partway through (row gone, episodes not yet) and for
    hand-edited data. This is the DB-side half of orphan detection; app.downloader
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
    """Add a channel by URL, with a placeholder identity until its first poll.

    Still idempotent: url is UNIQUE, so OR IGNORE fires on a repeat add exactly
    as it did when url was the primary key (the freshly generated placeholder is
    simply discarded in that case).
    """
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)",
                     (new_pending_channel_id(), url))


def remove_channel(channel_id: str):
    """Delete one channels row by its primary key.

    Keyed on channel_id since 1.15, not url. The caller (main._remove_one) takes
    this value off the very row it looked up and cascades with the same value,
    so the delete and the cleanup that follows it cannot target different
    channels — the structural end of the orphaned-data bug patched in v1.11.0.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))


def get_channel_by_url(url: str) -> sqlite3.Row | None:
    """The whole channels row for an exact URL, or None.

    Row-at-a-time rather than id-at-a-time on purpose: a caller that holds the
    row can delete it and cascade from the same lookup, which is what makes the
    two impossible to disagree.
    """
    with get_conn() as conn:
        return conn.execute("SELECT * FROM channels WHERE url = ?", (url,)).fetchone()


def add_channel_with_id(channel_id: str, url: str, channel_name: str):
    """Insert a channel whose real channel_id is already known.

    The two call sites that have one up front (main.subscribe_channel and
    downloader.download_single(subscribe=True)) used to call add_channel() then
    update_channel_meta() — two writes, with the row briefly holding a
    placeholder identity in between for no reason. Idempotent on both unique
    columns: OR IGNORE absorbs a repeat of either the id or the url, and the
    follow-up UPDATE keeps the name fresh (what update_channel_meta used to do)
    without touching feed_token or added_at.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO channels (channel_id, url, channel_name) VALUES (?, ?, ?)",
            (channel_id, url, channel_name),
        )
        conn.execute("UPDATE channels SET channel_name = ? WHERE channel_id = ?",
                     (channel_name, channel_id))


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
    """The resolved YouTube channel_id for a stored URL, or None.

    A placeholder id reads back as None, so callers keep the pre-1.15 meaning of
    this function — "has this channel ever polled successfully?" — unchanged
    (see downloader._poll_channel_locked's known_channel_id).
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT channel_id FROM channels WHERE url = ?", (url,)
        ).fetchone()
        if not row or is_pending_channel_id(row["channel_id"]):
            return None
        return row["channel_id"] or None


def update_channel_meta(url: str, channel_id: str, channel_name: str):
    """Record a poll's resolved identity on the channels row added as `url`.

    Since 1.15 channel_id is the primary key, so a newly-added channel's first
    successful poll swaps the row's PK from its `pending:` placeholder to the
    real YouTube id in place (SQLite does allow updating a primary key value).
    This runs on EVERY poll, not just the first, so for an already-resolved row
    it is the same name refresh it always was.

    The collision case: the user added two URL variants of one channel (say
    /@Chan and /channel/UCx). One resolved first and now owns UCx; the other's
    first poll would swap onto a taken primary key and raise IntegrityError in
    the middle of a poll. The resolution is to keep the incumbent and drop the
    newly-resolving duplicate — the incumbent holds the feed_token and itunes_*
    settings a podcast app may already be subscribed with, and rotating that
    token would silently stop that subscription updating. The `url != ?` test is
    what keeps this branch from firing on the ordinary re-poll of an
    already-resolved row, where the row holding channel_id IS this row; a
    too-eager branch here would delete a channel on every single poll.

    BEGIN IMMEDIATE so the check and the swap are one step: concurrent poll
    threads (POLL_CONCURRENCY) can otherwise both see "no collision" and race.
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        other = conn.execute(
            "SELECT url FROM channels WHERE channel_id = ? AND url != ?",
            (channel_id, url),
        ).fetchone()
        if other:
            logger.warning(
                "Channel %s is already registered as %s — dropping the duplicate "
                "row added as %s and keeping the existing row's feed token and "
                "settings", channel_id, other["url"], url)
            conn.execute("DELETE FROM channels WHERE url = ?", (url,))
            conn.execute("UPDATE channels SET channel_name = ? WHERE channel_id = ?",
                         (channel_name, channel_id))
        else:
            conn.execute(
                "UPDATE channels SET channel_id = ?, channel_name = ? WHERE url = ?",
                (channel_id, channel_name, url)
            )
        conn.execute("COMMIT")


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


# --- feed access tokens -----------------------------------------------------

# Key in the settings table holding the combined feed's token. It belongs to no
# single channel, so it can't live in either channels table.
_ALL_FEED_TOKEN_KEY = "all_feed_token"


def get_feed_token(channel_id: str) -> str | None:
    """The stored token for a channel_id, or None if no row owns it.

    Read-only on purpose: this is the request path (a feed fetch must not
    write). init_db()'s backfill and main._feed_url()'s get-or-create are what
    guarantee a token exists by the time anyone fetches a feed.
    """
    with get_conn() as conn:
        return _select_feed_token(conn, channel_id)


def _select_feed_token(conn, channel_id: str) -> str | None:
    """channels first, then unsubscribed_channels — the feed route serves both."""
    row = conn.execute(
        "SELECT feed_token FROM channels WHERE channel_id = ? AND feed_token IS NOT NULL",
        (channel_id,),
    ).fetchone()
    if row:
        return row["feed_token"]
    row = conn.execute(
        "SELECT feed_token FROM unsubscribed_channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return row["feed_token"] if row else None


def get_or_create_feed_token(channel_id: str) -> str | None:
    """Token for a channel_id, generating one if the owning row has none yet.

    Covers what the init_db() backfill can't: rows added after it ran, and a row
    whose identity was still a `pending:` placeholder at backfill time (the
    backfill gave that placeholder a token; once the first poll swaps the real
    channel_id in, the token comes along with the row, but a row created between
    boots has never seen the backfill at all). Returns None when no row owns
    that channel_id.

    The `AND feed_token IS NULL` guard plus the re-read makes two racing callers
    converge on one token rather than the second clobbering (and invalidating)
    the first — writers serialize under WAL + busy_timeout.
    """
    with get_conn() as conn:
        existing = _select_feed_token(conn, channel_id)
        if existing:
            return existing
        token = secrets.token_urlsafe(24)
        conn.execute(
            "UPDATE channels SET feed_token = ? WHERE channel_id = ? AND feed_token IS NULL",
            (token, channel_id),
        )
        conn.execute(
            "UPDATE unsubscribed_channels SET feed_token = ? "
            "WHERE channel_id = ? AND feed_token IS NULL",
            (token, channel_id),
        )
        return _select_feed_token(conn, channel_id)


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )


def get_or_create_all_feed_token() -> str:
    """Token for the combined /feed/all.xml, created on first access.

    INSERT OR IGNORE then re-SELECT (rather than INSERT OR REPLACE) so two
    racing callers can't end up handing out two different values, one of which
    is already dead.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (_ALL_FEED_TOKEN_KEY, secrets.token_urlsafe(24)),
        )
        return conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_ALL_FEED_TOKEN_KEY,)
        ).fetchone()["value"]


# --- per-channel feed metadata ----------------------------------------------

def get_channel_feed_settings(channel_id: str) -> sqlite3.Row | None:
    """The three iTunes overrides for a subscribed channel, or None.

    None (an unsubscribed or unknown channel_id) is what makes app/feed.py fall
    back to the built-in defaults — one-off feeds deliberately have no overrides.
    """
    with get_conn() as conn:
        return conn.execute(
            "SELECT itunes_category, itunes_language, itunes_explicit "
            "FROM channels WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()


def set_channel_feed_settings(channel_id: str, category: str | None,
                              language: str | None, explicit: str | None) -> None:
    """Store (or, with None, clear back to the default) a channel's overrides."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE channels SET itunes_category = ?, itunes_language = ?, "
            "itunes_explicit = ? WHERE channel_id = ?",
            (category, language, explicit, channel_id),
        )


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


def backup_db(prefix: str = "episodes") -> str:
    """Write a timestamped snapshot of the database into BACKUP_DIR.

    `prefix` names the file. The default is what the nightly job writes and what
    prune_backups() matches, so those two are unaffected. The channels
    primary-key migration passes prefix="pre-pk-migration" precisely so its
    snapshot falls outside the pruner's glob and is kept forever — that is the
    one irreversible change in the schema's history, and its "before" copy is
    the only way back.

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
    dest = os.path.join(BACKUP_DIR, f"{prefix}-{stamp}.db")
    # VACUUM INTO refuses to write to an existing file rather than overwriting
    # it. A same-second collision needs two runs of this job in one second,
    # which shouldn't happen — but a raise here would lose the night's backup,
    # so fall back to a unique name instead of failing.
    if os.path.exists(dest):
        dest = os.path.join(BACKUP_DIR, f"{prefix}-{stamp}-{secrets.token_hex(2)}.db")
    with get_conn() as conn:
        conn.execute("VACUUM INTO ?", (dest,))
    return dest


def prune_backups(retain: int = _BACKUP_RETAIN) -> list[str]:
    """Delete all but the `retain` newest snapshots. Returns the paths removed.

    The YYYYMMDD-HHMMSS stamp sorts lexicographically, so a plain reverse
    filename sort is a chronological sort — no stat() per file needed.

    Only `episodes-*` is matched, deliberately: the pre-pk-migration snapshot
    uses its own prefix so it survives forever. Do not widen this glob.
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
