# Concept Brief: Feed & Episode Management (Group 3 of 4)

## Problem

Four gaps in the current feed/episode surface:

1. **Feed URLs are guessable.** A feed URL is just `/feed/<channel_id>.xml`,
   and `channel_id` is YouTube's own public channel ID — anyone who knows or
   guesses it can pull your audio; there's no access control on feed/audio
   endpoints (by design, so podcast apps work without credentials — see
   README's "Basic auth" note), but that also means zero secrecy.
2. **No combined feed.** Each channel is a separate subscription; most users
   want one feed across everything.
3. **No episode-level management.** `db.delete_episode()` exists but no route
   uses it — there's no way to remove one bad download or re-fetch a video
   that downloaded corrupted/incomplete, short of removing the whole channel.
4. **Feed metadata is hardcoded per-install, not per-channel.** Every feed
   is iTunes category "Technology", language "en", explicit "no"
   (`app/feed.py`), regardless of what the channel actually is.

## Goal

Add opt-in feed access tokens (backward compatible — off by default, so
existing podcast-app subscriptions don't break), a combined all-channels
feed, episode-level delete/re-download from the dashboard, and per-channel
feed metadata overrides.

## In scope

### 1. Feed access tokens (opt-in)

- New env var `REQUIRE_FEED_TOKENS` (default `false`/unset — off).
- New nullable `feed_token` column on both `channels` and
  `unsubscribed_channels` tables (additive `ALTER TABLE`, following the
  existing migration pattern already in `db.init_db()` for the `thumbnail`
  column — read that pattern before writing this one). Generate via
  `secrets.token_urlsafe(24)` the first time a channel is created/polled and
  doesn't have one yet (backfill lazily, not in a blocking migration pass
  over every row at startup — or, if simpler and still safe, backfill all
  existing rows once in `init_db()`'s migration block; planner's call, but
  either way every channel must end up with a token once this ships,
  regardless of `REQUIRE_FEED_TOKENS`, so turning enforcement on later
  doesn't require yet another migration).
- New tiny `settings` key/value table (`key TEXT PRIMARY KEY, value TEXT`)
  for install-wide settings — first user: a token for the combined
  `/feed/all.xml` (see item 2). Generate and store lazily on first access,
  same pattern as the per-channel token.
- `GET /feed/{channel_id}.xml` (`app/main.py`) gains an optional `token`
  query param. When `REQUIRE_FEED_TOKENS` is true: reject (404, not 401 —
  don't reveal whether the channel exists to an unauthenticated caller)
  any request whose `token` doesn't match the stored `feed_token` for that
  channel_id, using `secrets.compare_digest`. When false (default): token
  is accepted if present but never required — so this ships inert until the
  user opts in, and existing feed URLs already saved in podcast apps keep
  working.
- Feed URLs surfaced in the dashboard (`_feed_url()` in `app/main.py`, and
  the share modal in `app/static/app.js`) must include `?token=...` in the
  URL they hand out whenever a token exists for that channel — regardless
  of whether enforcement is currently on — so the URL is ready to work the
  moment the user flips `REQUIRE_FEED_TOKENS` on, and copying/sharing it
  from the dashboard always gets the "future-proof" version.
- Document in README: what this protects against (URL guessing/enumeration
  by someone who doesn't already have a copied feed URL), what it doesn't
  (it's a shared secret in a URL, not real auth — same caveat as most
  podcast-app tokens), how to enable it, and that enabling it does not
  retroactively invalidate URLs already saved in a podcast app (their
  embedded token still matches).

### 2. Combined feed

- `GET /feed/all.xml` — merges episodes from all **subscribed** channels
  only (not one-off/unsubscribed), sorted by `published` descending, capped
  at a new env var `ALL_FEED_MAX_EPISODES` (default `100`). Build this in
  `app/feed.py` as a new function (e.g. `build_combined_feed()`) that
  reuses as much of `build_feed()`'s per-entry logic as practical (safety
  checks via `is_safe_media_name`, itunes fields per entry) rather than
  duplicating it wholesale — factor out the shared per-episode entry-building
  into a helper both call, if that's a clean split; otherwise duplicate
  narrowly rather than forcing an awkward abstraction. Title/description for
  the combined feed itself (not per-entry) should be something like
  "Slipcast — All Channels" with a description noting it aggregates every
  subscribed channel. Cover art: fall back to the branded Slipcast cover
  (`cover-512.png`), same as the existing per-channel fallback.
- Respects the same token mechanism as item 1, using the `settings` table's
  "all" token, when `REQUIRE_FEED_TOKENS` is on.
- Add a route in `app/main.py`: `GET /feed/all.xml` — note this must be
  registered so it doesn't collide with the existing
  `GET /feed/{channel_id}.xml` route (FastAPI matches literal paths before
  path params by registration order in some setups, but verify — register
  the literal `/feed/all.xml` route, and confirm via a test that a request
  to it doesn't get swallowed by the `{channel_id}` route thinking "all" is
  a channel_id).
- Surface it in the dashboard: one link/share affordance near the top of the
  "Subscribed channels" section (e.g. next to the section heading or in the
  poll card), using the existing share-modal pattern in `app/static/app.js`.

### 3. Episode-level delete/re-download

- `POST /episodes/delete` (Form: `episode_id`) — deletes the episode's audio
  file, thumbnail file (if any), and DB row via `db.delete_episode()`,
  reusing the existing `_remove_if_exists()` pattern from
  `app/downloader.py` (import/reuse it, don't duplicate the file-removal
  logic). Does NOT add a `skip_videos` entry — an explicit user delete
  should be re-downloadable on the next poll if the video is still in the
  channel's recent list (unlike a members-only or disk-pressure skip, which
  are deliberately permanent).
- `POST /episodes/redownload` (Form: `episode_id`) — looks up the episode's
  `channel_id`/`channel_name`/video `id` from the DB (read it before
  deleting, or accept the row already exists and it's a genuine
  re-download of a still-present episode — clarify: this endpoint should
  work both when the episode row/file still exists — force a fresh
  download, replacing the file — and after a delete, since "delete then
  redownload" is the primary use case for a corrupted/incomplete file).
  Add a new function in `app/downloader.py` (e.g. `redownload_episode
  (video_id, channel_id, channel_name)`) that reuses `_download_entry()`
  but bypasses its `if os.path.exists(expected_file): return None`
  short-circuit (that check exists to avoid redundant work on a normal
  poll — an explicit user re-download request should override it,
  removing the existing file first if present). Wire it through the
  existing `jobs` tracker (`app/jobs.py`) the same way `_run_download`/
  `_run_poll` already do, so the dashboard shows a spinner/toast.
- Both endpoints require the episode_id to actually exist in the DB first
  (404 if not — don't silently no-op).
- UI: add Delete and Re-download buttons to the episode list modal
  (`#ep-modal` / the episode-rendering code in `app/static/app.js` — find
  where episodes are rendered inside `openEpisodes`/similar and the
  `/api/channels/{channel_id}/episodes` consumer) next to each episode row,
  following the existing button/action patterns (`btn btn-ghost btn-sm`,
  `btn btn-danger-ghost btn-sm`) and the existing `act()` helper for
  POST+refresh+toast.

### 4. Per-channel feed metadata

- New nullable columns on `channels` (additive `ALTER TABLE`, same pattern
  as above): `itunes_category`, `itunes_language`, `itunes_explicit`.
  `app/feed.py`'s `build_feed()` uses the channel's stored value when set,
  falling back to the current hardcoded defaults ("Technology", "en", "no")
  when null — so this ships with zero behavior change for every existing
  channel until the user explicitly sets something.
- Validate `itunes_category` against Apple's actual allowed category list
  (feedgen's `itunes_category()` may already validate/raise on an invalid
  value — check, and if it does, surface that as a clean 400 rather than a
  500) and `itunes_explicit` against `{"yes", "no", "clean"}` (feedgen's
  accepted values — verify) server-side before storing.
- New endpoint: `POST /channels/feed-settings` (Form: `channel_id`,
  `category`, `language`, `explicit`) updating those three columns.
- UI: a small settings affordance per channel card (a new lightweight modal,
  following the existing `share-modal`/`ep-modal` pattern in the `_PAGE`
  HTML template in `app/main.py` and the corresponding JS in
  `app/static/app.js`) — a "Feed settings" button opening a form with a
  category dropdown (Apple's real category list — a reasonably-sized
  curated subset is fine, doesn't need every subcategory), a language
  input, and an explicit toggle/select.

## Out of scope

- Anything involving `unsubscribed_channels` feed metadata (one-off feeds
  keep the current hardcoded defaults — this is about subscribed channels,
  where a user is more likely to care about correct categorization).
- Combined feed for one-off/unsubscribed channels.
- Any change to the identity model / `channels.url` as primary key — that's
  Group 4, entirely separate, do not touch schema in a way that assumes or
  depends on that migration having happened or not.
- Group 1 (resilience/self-healing — already shipped as v1.12.0, merged)
  and Group 2 (storage/codec/retention — concurrently in progress on branch
  `feat/storage-retention` at brief-writing time; may or may not be merged
  to `main` by the time this executes). Do not touch
  `app/downloader.py`'s `_ydl_opts()`, disk-pressure pruning, or backup
  code — those are Group 1/2 territory. If Group 2 hasn't merged yet and
  this group's `app/downloader.py` edits (reusing `_remove_if_exists`,
  adding `redownload_episode`) land first, that's fine — they're additive
  and shouldn't conflict with Group 2's codec/retention changes, but branch
  from current `main` at execution time either way and re-read the actual
  file rather than assuming its shape.
- Real per-viewer authentication on feeds — tokens are a shared secret in a
  URL, not user accounts; document that distinction clearly rather than
  overselling what this provides.

## Constraints

- Match existing code style/comment density exactly.
- Additive schema changes only (new nullable columns, one new small table) —
  no changes to existing column meanings or the `channels.url` primary key.
- This is a MINOR version bump. Read `app/__init__.py`'s current value at
  execution time (do not hardcode — Groups 1 and 2 may have already bumped
  it) and increment MINOR from whatever is current; changelog entry dated
  2026-09-03 (or actual date), matching existing changelog prose style.
- Reuse `secrets.compare_digest` for token comparison (already used
  elsewhere in `app/main.py` for auth) — no naive `==` on secrets.
- The `/feed/`, `/audio/`, `/thumbnails/` prefixes are in `_PUBLIC_PREFIXES`
  (no Basic Auth) — feed tokens are a *separate*, opt-in mechanism layered
  on top, not a replacement for that. `/episodes/delete`,
  `/episodes/redownload`, `/channels/feed-settings` are normal
  authenticated dashboard mutations (Basic Auth + CSRF via the existing
  middleware) — they are NOT public, unlike the feed/audio endpoints.

## Acceptance criteria

1. With `REQUIRE_FEED_TOKENS` unset/false, existing feed URLs (no token)
   keep working exactly as before — verify with a test.
2. With `REQUIRE_FEED_TOKENS=true`, a request to `/feed/{id}.xml` with a
   missing or wrong token returns 404; the correct token returns the feed.
   Verify with a test.
3. `/feed/all.xml` returns episodes from every subscribed channel, sorted
   newest first, capped at `ALL_FEED_MAX_EPISODES`, and does not include
   unsubscribed/one-off channel episodes. Verify with a test seeding
   multiple channels.
4. `POST /episodes/delete` removes the file, thumbnail, and DB row for a
   real episode_id, 404s for a nonexistent one, and does NOT add a
   skip_videos entry. Verify with a test.
5. `POST /episodes/redownload` re-fetches the episode even when the file
   already exists (or was just deleted), and the resulting episode row/file
   reflects the fresh download. Verify with a test (can mock the yt-dlp
   layer following `tests/conftest.py`'s existing stub pattern).
6. Setting per-channel `itunes_category`/`language`/`explicit` via
   `/channels/feed-settings` changes that channel's feed output
   accordingly; an unset channel still produces today's defaults. Verify
   with a test comparing feed XML before/after setting values.
7. Full test suite passes: `.venv/bin/python -m pytest -q`.
8. Version bumped, changelog entry added, README updated (new env vars:
   `REQUIRE_FEED_TOKENS`, `ALL_FEED_MAX_EPISODES`; new endpoints; the
   token mechanism's real security properties, stated plainly).
9. Local deploy succeeds; the running instance's `/feed/all.xml` and at
   least one existing channel's `/feed/{id}.xml` both still resolve
   correctly against real production data (5 channels, ~100 episodes at
   time of writing).

## Open questions & decisions made

- **Token delivery**: query param (`?token=`), not a path segment — simpler
  routing, no new path-matching complexity.
- **Token enforcement default**: OFF (`REQUIRE_FEED_TOKENS=false`) —
  backward compatible with already-subscribed podcast apps. This is a
  deliberate choice to avoid silently breaking the user's existing feed
  subscriptions on deploy.
- **404 vs 401 on a bad token**: 404, to avoid confirming the channel_id is
  valid to an unauthenticated caller.
- **Combined feed scope**: subscribed channels only, not one-offs.
- **Per-channel metadata UI**: a new small modal, following existing modal
  patterns — planner should keep the category list reasonably short
  (Apple's real top-level categories) rather than exhaustively enumerating
  every subcategory.

## Relevant files/areas

- `app/database.py` — schema migration (new columns, new `settings` table)
  in `init_db()`'s migration block; new helper functions for token
  get/generate, feed-settings get/set, combined-feed episode query.
- `app/main.py` — `GET /feed/{channel_id}.xml` (token check), new
  `GET /feed/all.xml`, new `POST /episodes/delete`,
  `POST /episodes/redownload`, `POST /channels/feed-settings`;
  `_feed_url()` (include token); `_PAGE` HTML template (new modal markup).
- `app/feed.py` — `build_feed()` (per-channel metadata overrides),
  new `build_combined_feed()`.
- `app/downloader.py` — reuse `_remove_if_exists()`; new
  `redownload_episode()`.
- `app/static/app.js` — episode modal delete/re-download buttons, feed
  settings modal, combined-feed share affordance, token-aware feed URLs
  in the share modal.
- `app/config.py` — `REQUIRE_FEED_TOKENS`, `ALL_FEED_MAX_EPISODES`.
- `app/changelog.py`, `app/__init__.py`, `README.md`.
- `tests/test_feed.py`, `tests/test_endpoints.py`, `tests/test_database.py`.

## Repo commands & tree state

- **Tests**: `.venv/bin/python -m pytest -q` (run from
  `/home/eric/projects/slipcast` repo root; use the repo's `.venv`).
- **Build**: `docker compose build`
- **Deploy (local-only)**: `docker compose up -d`
- **Git**: at brief-writing time, Group 1 has merged to `main` (v1.12.0,
  PR #7) and Group 2 is building concurrently on branch
  `feat/storage-retention` (not yet merged). Branch this group's work as
  `feat/feed-episode-management` off whatever `main` is at execution time
  — do not branch off Group 2's branch, and re-read the actual current
  file contents rather than trusting any stale description here of what
  `main` contains at brief-writing time.
