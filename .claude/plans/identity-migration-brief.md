# Concept Brief: Channel Identity Migration (Group 4 of 4 — do last)

## Problem

`channels.url` is the primary key (`app/database.py`, `init_db()`), but every
other table that relates to a channel — `episodes`, `skip_videos`, and (as of
Group 1/2 additions) potentially more — is keyed by `channel_id`, and so is
every on-disk directory (`AUDIO_DIR/<channel_id>/`,
`THUMBNAIL_DIR/<channel_id>/`) and the public feed URL
(`/feed/<channel_id>.xml`). `channel_id` is only populated after a channel's
first successful poll (`update_channel_meta()`); until then it's `NULL`.

This split identity is the root cause of the orphaned-channel bug fixed in
v1.11.0 (PR #6): `_remove_one()` in `app/main.py` looks up `channel_id` by
matching `channels.url`, and when that match fails (a URL variant, or a
channel removed before its first successful poll), the `channels` row is
deleted but the episodes/skip_videos/files — everything actually keyed by
`channel_id` — are orphaned. v1.11.0 added a fallback URL-normalization match
plus an orphan reconciler as a safety net, but the underlying structural
problem — two different identity keys for the same conceptual entity — is
still there, and can still produce a new class of edge case neither of those
mitigations anticipated.

## Goal

Make `channel_id` the actual, structural identity for a channel everywhere in
the schema, so this class of bug becomes impossible rather than merely
caught by a reconciler. `url` becomes an attribute of a channel (how you
add/find it, kept unique), not its identity.

## In scope

1. **Schema migration**: `channels` table's primary key changes from `url`
   to `channel_id`. `url` becomes a `UNIQUE NOT NULL` column instead.
   - **The hard part**: `channel_id` is `NULL` until the first successful
     poll. A primary key cannot be `NULL`. Options to resolve this
     (planner's call, but investigate and justify whichever is chosen):
     a. Generate a placeholder/temporary local ID (e.g. a UUID) at
        `add_channel()` time, used as the PK until the real `channel_id` is
        known, then the row's PK is updated in place once
        `update_channel_meta()` runs (an `UPDATE channels SET
        channel_id = ? WHERE channel_id = ?` swap) — SQLite allows updating
        a primary key column value.
        b. Keep `channel_id` nullable but make it the PK anyway (SQLite,
           unlike strict SQL, permits multiple NULLs in a PK/unique column in
           some configurations — verify this is actually true for SQLite
           before relying on it, it is easy to get wrong) — likely NOT safe,
           investigate and probably reject.
        c. Give `channels` a separate synthetic autoincrement `id` as the
           actual PK, with `channel_id UNIQUE` (nullable until known) and
           `url UNIQUE NOT NULL` as two independently-unique columns — this
           sidesteps the "PK can't be null" problem entirely and may be the
           simplest correct answer; every *other* table would then reference
           `channel_id` (once known) as they already do, not the synthetic
           id — so this may not actually solve "channel_id is the real
           identity everywhere" so much as relocate the problem. Weigh this
           honestly against option (a) rather than defaulting to it just
           because it's mechanically easiest.
     The planner must pick one, explain why, and think through what happens
     to `episodes`/`skip_videos` rows that were written while a channel's
     `channel_id` was still unknown (should be none in practice — those
     tables are only ever written with a resolved `channel_id`, confirm this
     by reading `poll_channel()`/`download_single()` carefully — but verify
     rather than assume).
   - Write a real migration in `db.init_db()`'s existing "migrate existing
     DBs" block (see the existing `thumbnail` column migration there for the
     pattern), that:
     - Detects the old schema (current `channels.url PRIMARY KEY` shape) vs.
       the new one, so this is idempotent and safe to run against both a
       fresh DB and an already-migrated one.
     - Migrates the **existing production data** correctly: every channel
       currently has a resolved `channel_id` (confirm this by inspecting
       actual production data — the brief-writer already checked: as of
       2026-09-03, all 5 rows in `channels` have non-null `channel_id`), so
       the migration path for *this specific database* doesn't need to
       handle the "add a channel, no poll has happened yet" transient case
       during migration itself — but the ongoing code (not just the
       one-time migration) absolutely does, since that transient state
       recurs every time a new channel is added.
     - SQLite doesn't support `ALTER TABLE ... DROP CONSTRAINT` or changing
       a primary key in place — changing the PK requires the
       create-new-table-copy-data-drop-old-rename pattern (`CREATE TABLE
       channels_new (...)`, `INSERT INTO channels_new SELECT ...`,
       `DROP TABLE channels`, `ALTER TABLE channels_new RENAME TO
       channels`), all inside a transaction. Get this exactly right — a
       failure partway through must not leave the DB in a broken
       intermediate state (wrap in `BEGIN`/`COMMIT`, or rely on `get_conn()`'s
       existing commit-on-success/rollback-on-exception behavior — verify
       that's actually sufficient for a multi-statement DDL sequence in
       SQLite, DDL and transactions interact in ways worth double-checking).
     - **Before running the migration at all**, take a backup using the
       `db.backup_db()` function Group 1 already added (`app/database.py`)
       — this migration is the single highest-risk change in this entire
       effort, and an automatic pre-migration snapshot costs nothing and
       means a bad migration is recoverable. Do this as the very first thing
       `init_db()` does if it detects an old-schema DB that needs migrating.

2. **Update every function that currently keys on `url`** to key on
   `channel_id` instead, wherever a resolved `channel_id` is available —
   audit `app/database.py` (`add_channel`, `remove_channel`, `get_channels`,
   `get_channel_meta`, `get_channel_id_for_url`, `update_channel_meta`) and
   `app/main.py` (`_remove_one`, `_resolve_channel_id_for_removal`,
   `_normalize_channel_url` — much of the fallback-matching machinery added
   in v1.11.0's PR #6 becomes simplifiable or removable once `channel_id` is
   the real key; the planner should identify what v1.11.0 code this
   migration makes obsolete and remove it, not leave two mechanisms doing
   the same job).
   - `add_channel(url)` still takes a URL (that's how a user adds one) but
     the row's actual identity resolution changes per whichever option was
     picked in item 1.
   - Removal (`remove_channel`/`_remove_one`) should now be a straightforward
     `channel_id`-keyed delete once a channel_id is known, eliminating the
     whole class of "URL didn't match" orphan bug at its root — though the
     orphan reconciler (v1.11.0) should stay as defense-in-depth, not be
     removed; it's now a backstop for a bug class that should no longer be
     reachable through the normal path, not the primary safety net.

3. **Verify every downstream consumer still works**: `app/downloader.py`
   (`poll_channel`, `download_single`, all the `_audio_dir_for`/
   `_thumbnail_dir_for` callers), `app/feed.py`, `app/main.py`'s `/api/state`
   and all channel-related routes. None of these should need to change
   *behavior*, only possibly the specific DB calls they make — this is a
   structural/internal migration, not a feature change. The API surface
   (`/api/state` shape, endpoint behavior) must be unchanged.

## Out of scope

- Any change to `episodes`, `skip_videos`, `poll_runs`, or
  `unsubscribed_channels` table shapes — they already correctly key on
  `channel_id`; this migration only touches `channels`.
- Any new feature, endpoint, or UI change — this is purely structural.
- Groups 1–3 (already shipped or in progress on their own branches) — this
  group must branch from `main` **after** Groups 1–3 have all merged (this
  is explicitly sequenced last, per the user's approved plan, specifically
  so it doesn't have to reconcile schema/behavior changes from three
  concurrently-moving branches). Confirm all three are merged to `main`
  before starting — if any aren't yet, that's a signal to wait, not to
  proceed against a stale `main`.
- Automatic rollback tooling beyond the pre-migration backup — if the
  migration goes wrong, the documented recovery is "restore the
  `backup_db()` snapshot taken immediately before migrating" (per the
  existing manual restore procedure Group 1 documented in README.md), not a
  new automated rollback mechanism.

## Constraints

- **This is the highest-risk change in the whole four-group effort** — it
  rewrites the primary key of a live table holding real user data (5
  channels, 100 episodes at time of writing). Test the migration against a
  **copy of actual production data**, not just synthetic test fixtures —
  copy `data/episodes.db` (the real file) into a scratch location and run
  the migration against that copy as part of verification, separately from
  the pytest suite's synthetic DBs.
- Match existing code style/comment density exactly.
- This is a MINOR version bump (schema migrations that preserve all data
  and external behavior are not breaking in SemVer terms here — no API
  contract changes) — but flag in the brief/plan/changelog that this is
  structurally significant even though it's not a MAJOR bump, since "no
  external behavior change" is the whole design goal, not an accident.
  Read `app/__init__.py`'s current value at execution time (Groups 1–3 will
  have already bumped it multiple times) and increment MINOR from whatever
  is current.
- `tests/conftest.py` and every existing test that constructs a `channels`
  row or relies on its shape will likely need updating — this is expected
  and is not a sign the migration is wrong, but each change must be
  reviewed for whether it reflects the new schema correctly or is
  papering over a real bug.

## Acceptance criteria

1. A fresh install (`init_db()` on an empty DB) produces the new schema
   directly — verify by inspecting the resulting table definition.
2. Running `init_db()` against a **copy of the actual current production
   database** (`data/episodes.db`, copied to a scratch path, never run
   against the live file directly during testing) migrates it correctly:
   all 5 channels, 100 episodes, and their relationships are intact
   afterward, verified by comparing row counts and spot-checking specific
   channel_id/episode relationships before and after.
3. Running `init_db()` a second time against an already-migrated DB is a
   no-op (idempotent) — no error, no data change.
4. A pre-migration backup is taken automatically before the migration runs,
   verified by checking `data/backups/` (or the scratch-copy equivalent)
   for a new snapshot dated just before the migration.
5. Adding a new channel (no poll yet) still works — the row exists in a
   sensible interim state per whichever design was chosen, and becomes
   fully resolved after the first successful poll, exactly as before from
   the user's perspective.
6. Removing a channel (`_remove_one`) via `channel_id` no longer has any
   code path that can fail to resolve the identity and orphan data — verify
   with a test that specifically tries the URL-variant scenario that used
   to trigger the v1.11.0 bug and confirms it's now structurally
   impossible, not just caught by the reconciler.
7. Every existing test in the full suite passes (with necessary, reviewed
   updates to fixtures that assumed the old schema).
8. `.venv/bin/python -m pytest -q` passes.
9. All existing API endpoints behave identically — `/api/state`,
   `/feed/{id}.xml`, channel add/remove/poll, etc. — verified by exercising
   them against the running app after migration (not just unit tests).
10. Local deploy succeeds against the **real** `data/episodes.db` (this is
    the one group in this whole effort where "deploy" means "run the
    migration against production data" — treat that deploy step with
    corresponding care: confirm the pre-migration backup exists and is
    valid *before* declaring this done, and be prepared to restore it if
    anything looks wrong post-migration).

## Open questions & decisions made

- **PK strategy** (option a/b/c above): left to the planner, who must
  investigate SQLite's actual behavior (not assume) and justify the choice
  in the plan, including how it affects every other function currently
  keying on `url`.
- **Migration mechanism**: create-copy-drop-rename, inside a transaction,
  pattern to be detailed by the planner referencing SQLite's actual
  constraints (no in-place `ALTER TABLE` for PK changes).
- **Safety net**: automatic pre-migration backup via the existing
  `db.backup_db()` (Group 1), checked before proceeding.

## Relevant files/areas

- `app/database.py` — `init_db()` (the migration itself, in the existing
  "migrate existing DBs" block), every `channels`-touching function.
- `app/main.py` — `_remove_one()`, `_resolve_channel_id_for_removal()`,
  `_normalize_channel_url()` (v1.11.0 additions — audit for
  simplification/removal), every route touching channels.
- `app/downloader.py` — `poll_channel()`, `download_single()`, anywhere a
  channel's identity is looked up or written.
- `tests/test_database.py`, `tests/conftest.py`, `tests/test_endpoints.py`,
  `tests/test_polling.py` — fixture updates.
- `README.md`, `app/changelog.py`, `app/__init__.py`.

## Repo commands & tree state

- **Tests**: `.venv/bin/python -m pytest -q` (repo root, use `.venv`).
- **Build**: `docker compose build`
- **Deploy (local-only)**: `docker compose up -d`
- **Git**: this group must branch from `main` **only after** Groups 1, 2,
  and 3 have all merged — verify with `git log --oneline -10` that all
  three PRs are present before branching `feat/channel-identity-migration`.
  Do not start this group's build while any of the other three are still
  in flight on their own branches.
- **Production data**: `data/episodes.db` (the real, live file — bind-mounted
  into the running container, currently: 5 channels, 100 episodes, all with
  resolved `channel_id`s). Copy it, never run experimental migrations
  directly against it. The orchestrating session will run the real
  migration (via a real deploy) only after verifying against the copy.
