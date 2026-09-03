# Implementation Plan: Channel Identity Migration (Group 4 of 4)

> **This MUST be built by Opus 5. Do not delegate this to Sonnet 5 or any other
> model, and do not offer that as an option.**
>
> Reasoning: this is the only change in the four-group effort that rewrites the
> primary key of a live table holding real, irreplaceable user data (5 channels,
> 100 episodes, 187 skip_videos, 8 podcast feed tokens that are already
> subscribed in a podcast app). A mistake here is not a failing test — it is data
> loss, or silently rotated feed tokens that break live subscriptions, or an
> `init_db()` that raises on startup and takes the whole app down (init_db runs on
> *every* boot). The work also requires holding several non-obvious SQLite
> behaviours in mind simultaneously (DDL/transaction interaction under the Python
> driver, NULL-in-PRIMARY-KEY legacy semantics, the create-copy-drop-rename
> dance), a placeholder-identity sentinel that must be masked at exactly the
> right boundaries to keep the public API byte-identical, and judgment calls
> about which v1.11.0 mitigation code to delete versus keep. That combination —
> irreversible blast radius plus sustained cross-file reasoning — is squarely
> Opus 5 territory.

---

## Summary

`channels.url` is currently the primary key, but every other table
(`episodes`, `skip_videos`, `unsubscribed_channels`, `poll_runs`), every on-disk
directory (`AUDIO_DIR/<channel_id>/`, `THUMBNAIL_DIR/<channel_id>/`) and every
public feed URL (`/feed/<channel_id>.xml`) is keyed by `channel_id`. That split
identity is the structural cause of the orphaned-channel bug patched in v1.11.0:
a delete keyed on `url` and a cascade keyed on a separately-resolved
`channel_id` can disagree, and when they do, episodes/files are stranded with no
row pointing at them. This change makes `channel_id` the actual primary key of
`channels` (`NOT NULL`), demotes `url` to a `UNIQUE NOT NULL` attribute, and
gives every channels row a real identity from the moment it is created — a
`pending:<hex>` placeholder until the first successful poll resolves the real
YouTube channel ID, at which point the PK is swapped in place. Nothing about the
HTTP API, the `/api/state` JSON shape, the feed URLs, or the dashboard changes;
this is purely structural. Version bump: **1.14.0 → 1.15.0** (MINOR — no
external contract changes — but flag in the changelog that "no external
behaviour change" is the entire design goal here, not a coincidence).

---

## Approach & key decisions

### Pre-flight: confirm the tree state

Groups 1–3 are **already merged** to `main` (verified):

```
0d870c3 Feed & episode management: tokens, combined feed, delete/re-download (v1.14.0) (#9)
7726087 Storage & retention: configurable codec, age/duration caps, disk usage (v1.13.0) (#8)
158b2e3 Resilience & self-healing: timeouts, /health/live, autoheal, disk floor, DB backups (v1.12.0) (#7)
```

Re-verify with `git log --oneline -10` before branching, then branch
`feat/channel-identity-migration` from `main`. `app/__init__.py` currently reads
`__version__ = "1.14.0"` — re-read it at execution time and bump MINOR from
whatever is actually there.

### SQLite behaviour — investigated, not assumed

Every claim below was verified empirically against this repo's interpreter
(`.venv/bin/python`, CPython 3.12.3, SQLite library 3.45.1). Re-run any of it if
you want to see it yourself; the conclusions are what drive the design.

| Question | Verified result |
|---|---|
| Can a `TEXT PRIMARY KEY` column on a normal (rowid) table hold NULL? | **Yes — and multiple NULLs.** `CREATE TABLE a (channel_id TEXT PRIMARY KEY, url TEXT)` accepts two `INSERT ... VALUES (NULL, ...)` rows without error. This is SQLite's documented legacy quirk, not a configuration. |
| …on a `WITHOUT ROWID` table? | **No** — `IntegrityError: NOT NULL constraint failed`. PK columns are implicitly `NOT NULL` there. |
| Does an explicit `NOT NULL` on the PK column fix it on a rowid table? | **Yes** — `IntegrityError` on NULL insert. |
| Can a `UNIQUE` column hold multiple NULLs? | **Yes** (standard SQL behaviour; two NULL `channel_id` rows inserted fine). |
| Can a primary key **value** be `UPDATE`d in place? | **Yes.** `UPDATE c SET channel_id='UCreal' WHERE channel_id='tmp-uuid'` works. Updating it to a value another row already holds raises `IntegrityError: UNIQUE constraint failed`. |
| Does the Python driver wrap DDL in the implicit transaction? | **No, and this is the trap.** With the default `isolation_level=""`, `conn.in_transaction` is still `False` after `CREATE TABLE`; it only becomes `True` after the first DML statement. In a create→insert→drop→rename sequence with no explicit `BEGIN`, the `CREATE TABLE channels_new` is **already committed** by the time a later failure rolls back — leaving a stray `channels_new` table behind. Verified: after a simulated mid-sequence exception and `conn.rollback()`, `sqlite_master` listed **both** `channels` and `channels_new`. |
| Does an explicit `BEGIN IMMEDIATE` fix that? | **Yes.** Same failure injected after an explicit `BEGIN IMMEDIATE` rolled back cleanly: only `channels` remained, with its row intact. SQLite DDL *is* transactional; the gap is purely the Python driver's implicit-transaction heuristic. |
| Can `PRAGMA journal_mode=WAL` run inside a transaction? | **No** — `OperationalError: cannot change into wal mode from within a transaction`. This is an ordering constraint on where the migration may sit inside `init_db()`. |
| Does `ALTER TABLE ... RENAME TO` alter the stored schema text? | Yes — the resulting `sqlite_master.sql` reads `CREATE TABLE "channels" (...)` with the name quoted. Cosmetic, but don't write a test that string-matches `CREATE TABLE channels`. |
| Are there views, triggers, or foreign keys referencing `channels` in production? | **No.** `sqlite_master` in the live DB holds only the five tables, `sqlite_sequence`, the auto-indexes, `idx_poll_runs_started` and `idx_episodes_channel_id`. `PRAGMA foreign_keys` is `0`. Nothing needs `PRAGMA legacy_alter_table` handling. |

### PK strategy: **option (a)** — `channel_id TEXT PRIMARY KEY NOT NULL`, with a `pending:` placeholder

**Option (b) is rejected on the evidence.** It "works" in the narrow sense that
SQLite really does permit multiple NULLs in a rowid-table PK — but that is the
legacy bug, not a feature, and it produces a primary key that is not a key: two
unpolled channels would both have PK `NULL`, indistinguishable, and
`WHERE channel_id = ?` can never match a NULL row (SQL NULL comparison), so the
row would be *undeletable* by its own primary key. That is a strictly worse
version of the bug this group exists to kill.

**Option (c) is rejected on merit, not difficulty.** A synthetic
`id INTEGER PRIMARY KEY AUTOINCREMENT` with `channel_id UNIQUE` nullable does
solve the mechanical "PK can't be NULL" problem, and it would fix the orphan bug
too (a row-scoped lookup can't diverge from its own cascade). But it introduces a
*fourth* identifier that literally nothing else in the system uses — not
`episodes`, not `skip_videos`, not the filesystem, not the feed URLs — while
leaving `channels.channel_id` nullable. The "row exists but its identity is
unknown" state stays encoded in the schema forever, so every consumer keeps its
`if cid else` null-guard, and the stated goal ("make `channel_id` the actual
structural identity everywhere") is relocated rather than achieved. The brief
explicitly warns against defaulting to this because it is mechanically easiest.

**Option (a) is correct.** `channel_id` becomes `TEXT PRIMARY KEY NOT NULL`, so:

- Every `channels` row has a non-null identity from the instant it is inserted.
- That identity is drawn from the *same namespace* used by `episodes`,
  `skip_videos`, `unsubscribed_channels`, the audio/thumbnail directories, and
  the feed routes. One key, system-wide.
- Deletion is a PK-keyed `DELETE FROM channels WHERE channel_id = ?`, taken from
  the very row being deleted — the delete and the cascade cannot disagree,
  because they are the same value from the same lookup.
- The transient "not yet polled" state is expressed as a *value* in the identity
  column rather than as an absence of identity, which is what makes it
  representable, deletable, and testable.

**The placeholder format is load-bearing.** Use
`pending:` + `secrets.token_hex(16)` (e.g.
`pending:8f2c...`, 40 chars). The colon is deliberately **outside**
`_CHANNEL_ID_RE = ^[A-Za-z0-9_-]{1,64}$`, which is the validator used by
`downloader._audio_dir_for()` / `_thumbnail_dir_for()` (which *raise* on a
non-match) and by every `channel_id`-taking route in `app/main.py` (which return
HTTP 400). So a placeholder that ever leaks toward a filesystem path or a public
route fails loudly and immediately instead of creating a `data/audio/pending:.../`
directory. It also can never collide with a real YouTube channel ID (`UC…`, 24
chars, same charset), and it can never be submitted by a client as a valid
`channel_id`.

**Why the transient state is provably safe to cascade-delete.** Confirmed by
reading `downloader._poll_channel_locked()` and `download_single()`: `episodes`
and `skip_videos` rows are only ever written with a `channel_id` that came from
`_fetch_channel_entries()` (`info.get("channel_id") or info.get("id")`) or from
`info.get("channel_id") or info.get("uploader_id")` — i.e. always a *resolved*
YouTube ID, never a value read back out of the `channels` table. Therefore no
`episodes`/`skip_videos`/directory can ever exist under a `pending:` id, and
deleting a still-pending channel has nothing to cascade to. The cascade is
skipped for pending ids anyway (see `_remove_one` below), so this is
belt-and-braces.

**The one new failure mode option (a) introduces, and its resolution.** Today
two `channels` rows may legitimately share one `channel_id` — the URL-variant
case, which is explicitly documented in `db.get_combined_episodes()`'s docstring
and covered by `tests/test_database.py::test_get_combined_episodes_excludes_unsubscribed_and_dedupes`
and `tests/test_feed.py` (~line 193). Under the new schema that is a `UNIQUE`
violation. Two places must handle it:

1. **The migration** must collapse duplicates (keep one row per `channel_id`) —
   see the migration SQL. Production has no duplicates (verified: 5 rows,
   5 distinct non-null `channel_id`s) but the code must not assume that.
2. **`update_channel_meta()`** must handle the swap colliding with an existing
   row: user adds `https://www.youtube.com/@Chan` (already resolved to `UCx`),
   then adds `https://www.youtube.com/channel/UCx`; the second row's first poll
   resolves to `UCx` and the in-place PK update would raise `IntegrityError`
   *inside a poll*. Resolution: **keep the row that already holds the resolved
   `channel_id` and delete the newly-resolving duplicate**, logging a warning.
   Rationale for that direction specifically: the surviving row keeps its
   `feed_token` and `itunes_*` settings, so an already-subscribed podcast app
   keeps working. Deleting the incumbent instead would rotate the feed token and
   silently break a live subscription — the exact failure mode Group 3 went out
   of its way to avoid.

This is a small, deliberate behaviour change (adding a second URL for a channel
you already have now collapses to one row instead of silently creating a
duplicate). It is an improvement and must be called out in the changelog.

### Functions that change

**`app/database.py`**

| Function | Change |
|---|---|
| `init_db()` | New pre-step `_migrate_channels_to_channel_id_pk()` (backup + create-copy-drop-rename) called **before** the existing `with get_conn()` block; `CREATE TABLE IF NOT EXISTS channels` body updated to the new shape; feed-token backfill loop re-keyed from `url` to `channel_id`. |
| `add_channel(url)` | Insert with a generated `pending:` id: `INSERT OR IGNORE INTO channels (channel_id, url) VALUES (?, ?)`. Still idempotent — `url` is still `UNIQUE`, so `OR IGNORE` still fires on a repeat add. |
| `remove_channel(url)` → `remove_channel(channel_id)` | Signature and key change: `DELETE FROM channels WHERE channel_id = ?`. |
| `get_channel_by_url(url)` | **New.** `SELECT * FROM channels WHERE url = ?` — the single lookup that `_remove_one` uses for both the delete and the cascade. |
| `get_channel_id_for_url(url)` | Keep, but return `None` for a `pending:` id (so `downloader._poll_channel_locked`'s `known_channel_id` semantics are unchanged). |
| `update_channel_meta(url, channel_id, channel_name)` | Same signature; body becomes the collision-aware PK swap described above, wrapped in `BEGIN IMMEDIATE`. |
| `add_channel_with_id(channel_id, url, channel_name)` | **New.** One-step insert for the two call sites where the real id is already known (`main.subscribe_channel`, `downloader.download_single(subscribe=True)`), replacing the `add_channel()` + `update_channel_meta()` pair. Idempotent on both unique columns. |
| `new_pending_channel_id()` / `is_pending_channel_id(cid)` | **New** helpers + `_PENDING_PREFIX = "pending:"`. `is_pending_channel_id` is the masking predicate imported by `app/main.py`. |
| `get_combined_episodes()` | No SQL change required (the subquery is still correct); **rewrite the docstring** — the "two channels rows can share one channel_id" rationale is now false by construction. Keep the subquery form rather than switching to a JOIN: it is equally correct and the diff stays small. |
| `orphan_channel_ids()` | `WHERE channel_id IS NOT NULL` on the `channels` query is now redundant (column is `NOT NULL`). Leave the filter in place — it is harmless and self-documenting — but update the docstring's causal explanation. |
| `get_or_create_feed_token()` | Docstring references "a channels row whose channel_id was still NULL at migration time"; update to the `pending:` reality. Behaviour unchanged. |
| `backup_db(prefix="episodes")` | Add an optional prefix parameter (see "pre-migration backup" below). |

Unchanged: `get_channels`, `get_channel_meta`, `get_channel_feed_settings`,
`set_channel_feed_settings`, `get_feed_token`, `_select_feed_token`, everything
episode/skip/poll_run/backup related.

**`app/main.py`**

| Function | Change |
|---|---|
| `_resolve_channel_id_for_removal()` | **Delete.** Its entire job was reconciling two identities; there is now one. Its tests go with it (replaced — see Testing). |
| `_normalize_channel_url()` | **Keep**, but it is now called only from a new `_find_channel_row()`. Justification for keeping rather than deleting: `/channels/remove` is a form endpoint that accepts an arbitrary `url` from any caller (the dashboard sends the exact stored URL, but nothing enforces that), and the fallback preserves today's tested variant-tolerant behaviour. Crucially, it is now *safe* in a way it was not before — it resolves a **row**, not a bare id, so it can no longer cause the delete and the cascade to target different channels. |
| `_find_channel_row(url)` | **New.** Exact `db.get_channel_by_url(rurl)`, else normalized scan over `db.get_channels()`. Returns the row or `None`. |
| `_remove_one(url)` | Rewritten: find the row once; if none, return (nothing to delete, nothing to orphan); else PK-delete that row and cascade with **that row's** `channel_id`, skipping the cascade for a `pending:` id. |
| `api_state()` | Mask pending: `cid = ch["channel_id"]` becomes `cid = None if db.is_pending_channel_id(ch["channel_id"]) else ch["channel_id"]` (one line). Everything downstream (`episodes`, `bytes`, `feed_url`, `thumbnail`, `last_poll`) already guards on `if cid`, so the emitted JSON for an unpolled channel stays byte-identical to today's. |
| `subscribe_channel()` | `db.add_channel(url)` + `db.update_channel_meta(...)` → `db.add_channel_with_id(channel_id, channel_page_url, channel_name)`. |

Unchanged: `/channels/add`, `/add`, `/channels/poll*`, `/channels/remove-unsubscribed`,
`/channels/remove-orphan`, `/channels/remove-bulk` (still takes URLs, still calls
`_remove_one`), `/channels/feed-settings`, `_feed_url`, `_thumb_url`,
`_episode_count`, `_run_poll`, all feed and episode routes, the lifespan orphan
reconciler.

**`app/downloader.py`**

| Function | Change |
|---|---|
| `_poll_channel_locked()` | **No functional change.** `db.get_channel_id_for_url()` keeps returning `None` for an unresolved channel, and `db.update_channel_meta()` keeps the same signature. Re-read it to confirm, change nothing. |
| `download_single()` | The `subscribe=True` branch's `add_channel()` + `update_channel_meta()` pair → `db.add_channel_with_id(channel_id, channel_page_url, channel_name)`. The surrounding `if not any(ch["channel_id"] == channel_id ...)` guard can stay (cheap, and keeps the log line accurate). |
| `find_orphan_channels()` | **No code change** — its `known` set will contain `pending:` ids, which can never match an episode `channel_id` or a directory name (the colon fails `_CHANNEL_ID_RE`). **Update the docstring**: the first listed cause ("`_remove_one` fails to resolve a channel_id") is no longer reachable through the normal path; it is now a backstop for interrupted deletes and hand-edited data. |
| Everything else | Unchanged. `_audio_dir_for`, `_thumbnail_dir_for`, `_prune_channel`, `_sweep_orphan_files`, `remove_channel_data`, `delete_episode_files`, `redownload_episode`, `storage_usage`, `channel_bytes` all take a resolved `channel_id` from a caller that has one. |

**`app/feed.py`** — no changes. Verify by reading; do not touch.

---

## Data / model / API changes

### Schema: before

```sql
CREATE TABLE channels (
    url          TEXT PRIMARY KEY,
    channel_id   TEXT,
    channel_name TEXT,
    added_at     TEXT NOT NULL DEFAULT (datetime('now'))
, feed_token TEXT, itunes_category TEXT, itunes_language TEXT, itunes_explicit TEXT)
```

(The trailing columns are appended by v1.14.0's `ALTER TABLE ADD COLUMN`
migration; that is the exact live production shape as of 2026-09-03.)

### Schema: after

```sql
CREATE TABLE channels (
    channel_id      TEXT PRIMARY KEY NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    channel_name    TEXT,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    feed_token      TEXT,
    itunes_category TEXT,
    itunes_language TEXT,
    itunes_explicit TEXT
)
```

No other table changes. No API changes.

### The migration

Add to `app/database.py`, called as the **first statement inside `init_db()`**,
before the existing `with get_conn()` block:

```python
# The new-schema column list, in the order the migrated table declares them.
_CHANNELS_COLUMNS = ("channel_id", "url", "channel_name", "added_at", "feed_token",
                     "itunes_category", "itunes_language", "itunes_explicit")


def _needs_channels_pk_migration(conn) -> bool:
    """True when `channels` still has the pre-1.15 `url PRIMARY KEY` shape.

    PRAGMA table_info's `pk` flag is the detection, not a string match on the
    stored CREATE statement: ALTER TABLE ... RENAME rewrites that text (it comes
    back as `CREATE TABLE "channels"`, quoted), so comparing SQL would be
    fragile. No channels table at all means a fresh DB — nothing to migrate; the
    CREATE TABLE IF NOT EXISTS below builds the new shape directly.
    """
    return any(r["name"] == "url" and r["pk"] for r in
               conn.execute("PRAGMA table_info(channels)").fetchall())
```

The migration body (see the step list for exact placement and the backup call):

```sql
BEGIN IMMEDIATE;

CREATE TABLE channels_new (
    channel_id      TEXT PRIMARY KEY NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    channel_name    TEXT,
    added_at        TEXT NOT NULL DEFAULT (datetime('now')),
    feed_token      TEXT,
    itunes_category TEXT,
    itunes_language TEXT,
    itunes_explicit TEXT
);

INSERT INTO channels_new (channel_id, url, <carried columns>)
SELECT COALESCE(channel_id, 'pending:' || lower(hex(randomblob(16)))),
       COALESCE(url,        'unknown:' || lower(hex(randomblob(8)))),
       <carried columns>
FROM channels
WHERE rowid IN (SELECT MIN(rowid) FROM channels
                WHERE channel_id IS NOT NULL GROUP BY channel_id)
   OR channel_id IS NULL;

DROP TABLE channels;
ALTER TABLE channels_new RENAME TO channels;

COMMIT;
```

`<carried columns>` is computed in Python as the intersection of
`_CHANNELS_COLUMNS` (minus `channel_id`/`url`, which are handled by the
`COALESCE` expressions) with the old table's actual columns, from
`PRAGMA table_info(channels)`. That makes one migration handle both a v1.14
database (has `feed_token`/`itunes_*`) and any older one (doesn't) — the missing
columns simply come out NULL, and the existing `ALTER TABLE ADD COLUMN` block
further down `init_db()` becomes a no-op because the new table already declares
them. Build the column list from that whitelist tuple, never from raw user
input, so the f-string interpolation is safe.

Notes on each clause:

- **`COALESCE(channel_id, 'pending:' || …)`** — gives an unpolled row a
  placeholder identity. `randomblob(16)`/`hex()`/`lower()` are core SQLite
  functions; no Python loop needed, and each row gets a distinct value.
- **`COALESCE(url, 'unknown:' || …)`** — defensive only. `url` was a rowid-table
  `TEXT PRIMARY KEY`, which (per the verification table) *can* hold NULL, so a
  hand-edited or ancient DB could have one. Without this, `NOT NULL` would abort
  the migration, and an aborting `init_db()` means the container will not start
  at all. A junk-but-present URL is enormously better than a boot loop. Log a
  warning if any row needed it.
- **The `WHERE rowid IN (SELECT MIN(rowid) … GROUP BY channel_id) OR channel_id IS NULL`
  filter** — collapses duplicate-`channel_id` rows to the oldest (lowest rowid,
  i.e. first added), which is the one most likely to hold the feed token a
  podcast app is already using. NULL-`channel_id` rows are all kept (they were
  distinct by `url`, and each gets its own placeholder). Count the dropped rows
  (`before - after`) and `logger.warning` their URLs if non-zero.
- **`BEGIN IMMEDIATE` is mandatory**, not optional. Verified above: without it,
  the Python driver leaves `CREATE TABLE channels_new` in autocommit, so a
  failure at any later point strands an orphan `channels_new` table that the
  next boot's migration would then collide with.

Post-migration inside the same connection (after COMMIT), run
`PRAGMA integrity_check` and log the result; log the row count before and after
at INFO.

---

## Step-by-step tasks

Each step should leave the suite runnable (`.venv/bin/python -m pytest -q`)
before moving on.

**1. Branch.** `git log --oneline -10` to confirm #7/#8/#9 are present, then
`git checkout -b feat/channel-identity-migration`.

**2. `app/database.py` — sentinel helpers.** Add `_PENDING_PREFIX = "pending:"`,
`new_pending_channel_id()` (returns `_PENDING_PREFIX + secrets.token_hex(16)`;
`secrets` is already imported) and `is_pending_channel_id(cid) -> bool`. Comment
why the prefix contains a colon (it must fail `_CHANNEL_ID_RE`, so a placeholder
can never become a directory name or pass a route validator).

**3. `app/database.py` — `backup_db(prefix="episodes")`.** Thread the prefix
through the filename and the same-second collision fallback. Default keeps the
nightly job and `prune_backups()` (which matches `episodes-*.db`) working
exactly as today. The migration will pass `prefix="pre-pk-migration"`, so its
snapshot is **never** auto-pruned — deliberate, for the one irreversible change
in the effort.

**4. `app/database.py` — the migration.** Add `_CHANNELS_COLUMNS`,
`_needs_channels_pk_migration(conn)` and `_migrate_channels_to_channel_id_pk()`.
The latter opens its own short-lived `get_conn()`, checks the detector, returns
immediately if false (this is the idempotency guarantee), otherwise:
`logger.warning` that it is migrating → `backup_db(prefix="pre-pk-migration")`
and log the path → `BEGIN IMMEDIATE` → the DDL/DML sequence → `COMMIT` →
`PRAGMA integrity_check` → log before/after counts and any dropped duplicates.
On exception: `logger.exception`, roll back, and **re-raise** (a half-understood
DB must not be served; the operator restores the snapshot whose path was just
logged).

Why its own connection, before the main `with get_conn()` block: `backup_db()`
uses `VACUUM INTO`, which cannot run inside a transaction, and
`PRAGMA journal_mode=WAL` (the first statement of the existing block) also
cannot run inside one. Keeping the migration in a separate, self-contained
connection that opens and closes before the main block avoids both collisions
and avoids a second connection contending with a lock held by the first.

**5. `app/database.py` — `init_db()` wiring.** Call
`_migrate_channels_to_channel_id_pk()` as the very first line of `init_db()`,
before `with get_conn()`. Update the `CREATE TABLE IF NOT EXISTS channels` body
to the new shape. Re-key the feed-token backfill loop from `url` to
`channel_id` (`SELECT channel_id FROM channels WHERE feed_token IS NULL` /
`UPDATE channels SET feed_token = ? WHERE channel_id = ?`) — keep the
`WHERE feed_token IS NULL` guard and the per-row loop exactly as they are; that
guard is what stops tokens rotating on every boot.

**6. `app/database.py` — channel functions.** `add_channel`, `remove_channel`
(now `channel_id`-keyed), new `get_channel_by_url`, new `add_channel_with_id`,
`get_channel_id_for_url` (mask pending), `update_channel_meta` (collision-aware
swap under `BEGIN IMMEDIATE`). Docstring updates for `get_combined_episodes`,
`orphan_channel_ids`, `get_or_create_feed_token`.

**7. `app/main.py`.** Delete `_resolve_channel_id_for_removal`; add
`_find_channel_row`; rewrite `_remove_one`; mask pending in `api_state`; switch
`subscribe_channel` to `add_channel_with_id`. Import `is_pending_channel_id` via
the existing `from app import database as db` (call it `db.is_pending_channel_id`
— no new import line).

**8. `app/downloader.py`.** `download_single(subscribe=True)` →
`add_channel_with_id`. Update `find_orphan_channels`'s docstring. Re-read
`_poll_channel_locked` and confirm it needs no change.

**9. Tests.** See Testing below. Update fixtures, delete the two obsolete
`_resolve_channel_id_for_removal` tests, add the new ones.

**10. Version + docs.** `app/__init__.py` → `1.15.0`; new `app/changelog.py`
entry at the top (dated the commit date, user-facing language matching the
existing entries' tone: what changed for *them*, not the SQL); README — add the
new `channels` schema note if a schema is documented anywhere it now contradicts,
document the never-pruned `pre-pk-migration-*.db` snapshot in the "Database
backup and restore" section, and add the one-line downgrade warning (below).

**11. Verify against a copy of production data** (procedure below) — before any
deploy.

**12. Deploy.** `docker compose build && docker compose up -d`. Local-only; do
**not** use the Docker Hub tag/CI/pull flow.

---

## Testing & verification

### Unit / integration suite

Run `.venv/bin/python -m pytest -q` from the repo root, using `.venv`.

**Fixtures that must change** (each reviewed for whether it reflects the new
schema or papers over a bug):

- `tests/test_database.py::_legacy_db` — hand-builds a pre-1.14 `channels` with
  `url TEXT PRIMARY KEY`. **Keep it exactly as it is.** It is now a pre-1.15
  fixture too, and it is the single most valuable test asset in this change: it
  proves the migration runs against a real old-shape file. Its two existing
  tests (`test_migration_adds_columns_and_backfills_tokens`,
  `test_migration_is_idempotent_and_never_rotates_tokens`) should keep passing
  unchanged — including the "3 distinct tokens" assertion, since its
  NULL-`channel_id` `@Unpolled` row survives with a `pending:` id and still gets
  its own token. If either fails, that is a real bug, not a fixture problem.
- `tests/test_database.py::test_remove_channel` (~line 35) — `db.remove_channel`
  now takes a `channel_id`. Update the call.
- `tests/test_database.py::test_get_combined_episodes_excludes_unsubscribed_and_dedupes`
  and the equivalent in `tests/test_feed.py` (~line 193) — both deliberately
  create **two `channels` rows sharing one `channel_id`**, which the new schema
  forbids. Rewrite them: the second `add_channel`+`update_channel_meta` pair now
  collapses into the first row, so assert *that* (one row survives, the
  incumbent's token/settings are intact, the feed still has no duplicate items).
  This is the correct resolution — the invariant those tests were defending is
  now enforced by the schema instead of by a subquery.
- `tests/test_endpoints.py::test_resolve_channel_id_exact_url_match` and
  `::test_resolve_channel_id_falls_back_to_normalized_match` and
  `::test_resolve_channel_id_returns_none_when_unresolvable` — the function is
  gone. Replace with equivalents against `main._find_channel_row` (exact match,
  variant match, no match).
- `tests/test_polling.py::test_get_channel_id_for_url` (~line 185) already
  asserts `None` before the first `update_channel_meta`; it should pass
  unchanged thanks to the pending mask. Confirm rather than edit.
- Everything else that calls `db.add_channel(url)` + `db.update_channel_meta(url, CID, name)`
  (many sites across `test_database.py`, `test_polling.py`, `test_endpoints.py`,
  `test_feed.py`) keeps working with no edit — that pair is still the supported
  "add then resolve" path.

**New tests to add:**

| Acceptance criterion | Test |
|---|---|
| 1 — fresh install gets the new schema directly | `init_db()` on an empty tmp DB, then assert `PRAGMA table_info(channels)` shows `pk=1` on `channel_id`, `pk=0` on `url`, and `notnull=1` on both. |
| 3 — idempotent | Run `init_db()` three times over `_legacy_db`; assert the schema, every row, and every feed token are identical after runs 2 and 3 (the existing token test covers half of this; extend it to the schema and row set). |
| 4 — automatic pre-migration backup | `_legacy_db` + `monkeypatch.setattr(db, "BACKUP_DIR", tmp)`; run `init_db()`; assert exactly one `pre-pk-migration-*.db` exists, that it opens as a valid SQLite DB, and that it still has the **old** schema (`url` is the PK) — proving it was taken *before* the migration, which is the whole point. |
| — migration is atomic | Force a failure mid-sequence (monkeypatch so the `ALTER TABLE ... RENAME` raises) and assert the DB still has the original `channels` table with all its rows **and no leftover `channels_new`**. This is the test that proves the `BEGIN IMMEDIATE` is doing its job; without it, a future refactor could silently drop back to the broken autocommit behaviour. |
| — duplicate collapse | Build a legacy DB by hand with two rows sharing one `channel_id`; migrate; assert one row survives, it is the lower-rowid one, and its `feed_token` is preserved. |
| — NULL channel_id becomes pending | `_legacy_db`'s `@Unpolled` row: after `init_db()`, its `channel_id` is non-null, `db.is_pending_channel_id()` is True, and `_CHANNEL_ID_RE.match()` on it is **None**. |
| 5 — add-then-poll lifecycle | `db.add_channel(url)` → row exists with a pending id; `db.get_channel_id_for_url(url) is None`; `main.api_state()` reports `channel_id: None`, `episodes: 0`, `feed_url: None`, `thumbnail: None` for it (identical to today's unpolled output); then `db.update_channel_meta(url, CID, "A")` → PK is now `CID`, one row, `added_at` and `feed_token` preserved. |
| 5b — resolve colliding with an existing row | Row A resolved to `CID`; row B (different URL) pending; `update_channel_meta(url_B, CID, "A")` → one row remains, it is row A (its `feed_token` and `itunes_*` unchanged), no exception raised. |
| 6 — orphaning is structurally impossible | The v1.11.0 scenario: add `https://www.youtube.com/@A`, resolve it to `CID`, write an episode + an audio file, then call `main._remove_one("https://www.youtube.com/@A?si=trackingjunk")` (the URL variant). Assert the row, the episode, and the file are **all** gone together and `downloader.find_orphan_channels() == []`. Then the harder half: call `_remove_one("https://www.youtube.com/@Ghost")` (no match at all) and assert **nothing** was deleted — no row, no episode, no file — proving the delete and cascade can no longer target different channels. That second assertion is what makes it structural rather than lucky; note that the *old* `_remove_one` would have deleted the variant-matched channel's episodes and files while leaving its row in place. |
| 6b — pending channel removal | Add a channel, never poll it, `_remove_one(url)` → row gone, no exception, cascade skipped (assert `remove_channel_data` was not called with a pending id, e.g. via monkeypatch spy). |

### Acceptance criterion 2 & 10 — verification against real production data

**Never run anything experimental against `/home/eric/projects/slipcast/data/episodes.db`.**
The live file is bind-mounted into the running container and is root-owned.
Use a scratch copy for all of this:

```bash
SCRATCH=$(mktemp -d /tmp/slipcast-migcheck-XXXX)
# VACUUM INTO, not cp: the live DB is in WAL mode, so a plain copy without its
# -wal sidecar can miss the newest writes. This also takes only a read lock, so
# it is safe while the container is running.
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('file:/home/eric/projects/slipcast/data/episodes.db?mode=ro', uri=True)
c.execute('VACUUM INTO ?', ('$SCRATCH/episodes.db',)); c.close()"
```

Then, with a small throwaway script (not committed):

1. **Record BEFORE state** from the copy: row counts for `channels`, `episodes`,
   `skip_videos`, `unsubscribed_channels`, `poll_runs`, `settings`; the full
   `channels` table (`channel_id`, `url`, `channel_name`, `added_at`,
   `feed_token`, `itunes_*`); `SELECT channel_id, COUNT(*) FROM episodes GROUP BY 1`;
   `SELECT channel_id, COUNT(*) FROM skip_videos GROUP BY 1`. Write it to JSON.
   The current live values, for cross-checking: **5 channels (all with distinct
   non-null `channel_id`s), 100 episodes, 187 skip_videos, 0 unsubscribed,
   300 poll_runs, 1 setting.**
2. **Run the real code path**, not hand-written SQL:
   `DATA_DIR=$SCRATCH .venv/bin/python -c "from app import database as db; db.init_db()"`
   (this also exercises the automatic backup into `$SCRATCH/backups/`).
3. **Record AFTER state** the same way and diff:
   - every row count identical;
   - `channels` PK flags flipped (`channel_id` `pk=1`, `url` `pk=0`, both `notnull=1`);
   - the set of `(channel_id, url, channel_name, added_at, feed_token, itunes_*)`
     tuples **identical** to before — most importantly **every `feed_token`
     byte-for-byte unchanged**, since a rotated token silently kills a live
     podcast subscription;
   - the per-`channel_id` episode and skip_video groupings unchanged, and every
     `episodes.channel_id` still present in `channels` (`SELECT COUNT(*) FROM
     episodes WHERE channel_id NOT IN (SELECT channel_id FROM channels)` = 0);
   - `PRAGMA integrity_check` = `ok`;
   - `$SCRATCH/backups/pre-pk-migration-*.db` exists, opens, and still has the
     **old** schema.
4. **Re-run `init_db()` twice more** against the same copy and confirm nothing
   changes (criterion 3 against real data, not just fixtures).
5. Spot-check by hand: `The Sol Foundation` (`UCzlWCeD3J0zVb4KSeaeDNdw`, added
   via a `/channel/<id>` URL) and `Jason Samosa`
   (`UC5IwLQcbjAmGJ58jH-gHpaQ`, whose stored URL carries a `?si=` tracking
   param) are the two most interesting rows — confirm both survive with their
   exact stored URL and token.

**A reference implementation of this migration SQL has already been run against a
copy of the live database during planning and produced: 5 rows in, 5 rows out,
all tokens preserved, `integrity_check` = `ok`, detector correctly reporting
"already migrated" on a second pass.** If your run does not reproduce that, stop
and find out why before going near a deploy.

### Acceptance criterion 9 — live endpoint behaviour

After the deploy, against the running app:

- `GET /health` and `GET /health/live` → 200.
- `GET /api/state` → compare against a copy captured *before* the deploy
  (`curl -s ... > /tmp/state-before.json`). The `channels` array must be
  equivalent — same URLs, same `channel_id`s, same episode counts, same
  `feed_url`s **including the token query string**, same `feed_settings`. The
  only legitimate differences are `version`, timestamps, `next_poll*`, `jobs`,
  and `polling.runs`.
- `GET /feed/<channel_id>.xml?token=...` for at least two real channels → 200,
  same item count as before, enclosure URLs unchanged.
- `GET /feed/all.xml?token=...` → 200.
- Add a channel through the UI, confirm it appears immediately with no Share /
  Feed-settings buttons and a "0 episodes" badge (identical to today's unpolled
  behaviour — this is the pending-mask check), let its first poll resolve it,
  confirm the buttons light up, then remove it and confirm its row, episodes,
  and `data/audio/<id>/` directory all disappear together.
- Confirm the dashboard's "Orphaned data" section is empty.

### Acceptance criterion 10 — the real deploy

This is the one deploy in the effort that runs a schema migration over
production data.

1. Take a manual snapshot first, independent of the automatic one:
   `docker compose stop app`, then `cp data/episodes.db* /somewhere/safe/`
   (all three files: `.db`, `-wal`, `-shm`).
2. `docker compose build && docker compose up -d`.
3. `docker compose logs -f app` and watch for the migration's WARNING lines: the
   backup path, the before/after row counts, the integrity result.
4. Confirm `data/backups/pre-pk-migration-*.db` exists, is non-zero, opens in
   SQLite, and holds 5 channels / 100 episodes with the **old** schema.
5. Only then run the criterion-9 endpoint checks.
6. If anything looks wrong: `docker compose stop app`,
   `cp data/backups/pre-pk-migration-<stamp>.db data/episodes.db`,
   `rm -f data/episodes.db-wal data/episodes.db-shm`, then run the **previous**
   image. (The README's existing restore procedure, with the extra note that a
   rollback must also roll back the image — see Risks.)

---

## Risks & watch-outs

**Ordering constraints (get these wrong and it breaks in ways tests may not catch):**

1. The migration must run **before** `init_db()`'s
   `CREATE TABLE IF NOT EXISTS channels`. That statement is a silent no-op on an
   existing table, so if the migration ran after it, an old-schema DB would keep
   the old schema forever while the code assumed the new one.
2. The migration must run **before** the `ALTER TABLE channels ADD COLUMN` loop
   and the feed-token backfill — the migrated table already declares all four
   v1.14 columns, and the backfill's new `WHERE channel_id = ?` key only exists
   post-migration.
3. `PRAGMA journal_mode=WAL` **cannot** execute inside a transaction (verified:
   `OperationalError`). Do not move it into the migration's transaction, and do
   not wrap the existing `with get_conn()` block in an explicit `BEGIN`.
4. `backup_db()` uses `VACUUM INTO`, which also cannot run inside a transaction —
   call it **before** `BEGIN IMMEDIATE`, and on a connection that is not mid-write.
   Doing the whole migration on its own short-lived connection, opened and closed
   before `init_db()`'s main block, satisfies both this and (3) cleanly.
5. **The backup must be taken before the transaction opens, not inside it.** A
   backup taken inside the migration transaction would either fail or capture the
   half-migrated state, which defeats the entire point.

**Things that are easy to get wrong:**

6. **Omitting `BEGIN IMMEDIATE` is the single most dangerous mistake available
   here.** The Python driver does not open a transaction for DDL, so
   `CREATE TABLE channels_new` commits on its own. A failure after that point
   leaves a stray `channels_new` in the file; the next boot re-detects "needs
   migration" and its `CREATE TABLE channels_new` fails with "table already
   exists" — an unbootable app. This was reproduced during planning. If you want
   extra insurance, `DROP TABLE IF EXISTS channels_new` immediately after `BEGIN`
   — but the transaction is the real fix, not the drop.
7. **Feed tokens must not rotate.** `feed_token` values are already embedded in
   URLs subscribed in a live podcast app. The migration carries them across in
   the `SELECT`; the backfill only fills `NULL`s. Assert token equality
   explicitly in the production-copy verification. This is the highest-severity
   *silent* failure mode in the whole change — nothing errors, the podcast app
   just stops updating.
8. **`db.remove_channel()` changes meaning, not just signature.** It goes from
   taking a URL to taking a `channel_id`. Both are `str`, so a missed call site
   type-checks fine and silently deletes nothing. Grep for every caller
   (currently `main._remove_one` and one test) and check each by hand.
9. **The pending sentinel must never reach the filesystem or a route.** That is
   guaranteed only by the colon in `pending:` failing `_CHANNEL_ID_RE`
   (`^[A-Za-z0-9_-]{1,64}$`) — if you change the prefix format, re-derive that
   property. Add the assertion to the tests so a future edit can't quietly break it.
10. **The pending mask in `api_state` is the API-compatibility linchpin.** Forget
    it and the dashboard enables Share / Feed settings / episode-list for an
    unpolled channel, then calls
    `/api/channels/pending:abc.../episodes` and gets a 400. Criterion 9 explicitly
    covers this; do the manual add-a-channel check, not just the unit test.
11. **`update_channel_meta` runs on every single poll**, including for
    already-resolved rows. The collision branch must trigger only when *another*
    row holds the target `channel_id` — comparing on `url` inequality. A
    too-eager branch would delete a channel on every poll.
12. **Duplicate collapse loses a row on purpose.** If a user has two URL variants
    for one channel, one dashboard entry disappears (at migration time or on the
    next poll). Production has none, but say so in the changelog and log it at
    WARNING when it happens.
13. **Downgrade is not possible after migration.** v1.14 code runs
    `INSERT OR IGNORE INTO channels (url) VALUES (?)`, which now violates
    `NOT NULL` on `channel_id`. Rolling back the image **requires** restoring the
    pre-migration snapshot. Put that sentence in the README next to the restore
    procedure and in the deploy notes.
14. **`prune_backups()` only matches `episodes-*.db`.** That is deliberate — it
    is why the migration snapshot uses the `pre-pk-migration` prefix and survives
    forever. Do not "fix" the pruner to match it, and do not call
    `prune_backups()` from the migration.
15. **Don't let `init_db()` swallow a migration failure.** It must raise. A
    half-migrated database served by code that assumes the new schema is worse
    than a container that refuses to start with a clear log line and a backup
    path.
16. **Don't touch the live DB during verification.** Every experiment goes
    against a `VACUUM INTO` copy in a scratch directory. `cp` alone is unsafe —
    the live DB is WAL-mode and the `-wal` sidecar carries recent writes.
17. **The orphan reconciler stays.** `find_orphan_channels`,
    `orphan_channel_ids`, `/channels/remove-orphan`, the dashboard's orphan
    section, and the lifespan startup report are all defense-in-depth for
    interrupted deletes and hand-edited data. Do not remove any of them; only
    their docstrings change.
18. **`ALTER TABLE ... RENAME` rewrites the stored schema text** as
    `CREATE TABLE "channels"` (quoted). Never assert on that string; assert on
    `PRAGMA table_info` flags.
19. **Match existing style.** This codebase carries dense *why* comments
    (see `get_conn`'s rollback note, `backup_db`'s `VACUUM INTO` rationale, the
    feed-token backfill's idempotency note). The migration deserves the same
    treatment: comment why `BEGIN IMMEDIATE` is required, why the backup precedes
    it, and why the detector reads `PRAGMA table_info` instead of the SQL text.

---

## Out of scope

- Any change to the `episodes`, `skip_videos`, `poll_runs`, or
  `unsubscribed_channels` table shapes — they already key correctly on
  `channel_id`; this migration touches only `channels`.
- Any new feature, endpoint, or UI change. Purely structural; `app/feed.py` and
  `app/static/app.js` should not need edits at all.
- Groups 1–3 (already merged as #7, #8, #9).
- Automatic rollback tooling beyond the pre-migration backup. Recovery is the
  documented manual restore (README, "Database backup and restore"), plus the
  new note that the image must be rolled back too.
