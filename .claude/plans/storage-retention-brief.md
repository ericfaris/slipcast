# Concept Brief: Storage & Retention (Group 2 of 4)

## Problem

Audio is hardcoded to MP3 at 128kbps (`app/downloader.py` `_ydl_opts()`), with
no retention besides a flat per-channel episode-count cap
(`MAX_EPISODES_PER_CHANNEL`, currently 20 for every channel). Production data
shows this is lopsided: one channel (UAP Gerb) is 2.4GB for 20 episodes —
multi-hour livestreams — while another (Lex Clips, now removed) was 129MB for
the same 20-episode cap. There is no visibility into per-channel disk usage
in the dashboard, and no way to catch long videos before they consume most of
a channel's cap in disk space.

## Goal

Give the user levers to control audio size (codec/bitrate) and content
(retention by age, a max-duration filter) without a schema migration, and
surface per-channel/total disk usage in the dashboard so the effect of those
levers is visible.

## In scope

1. **Configurable audio codec/bitrate** (global, not per-channel — see
   "Decisions made" below for why per-channel is deferred):
   - New env vars in `app/config.py`: `AUDIO_CODEC` (default `"mp3"`,
     also accepts `"opus"`) and `AUDIO_BITRATE_KBPS` (default `"128"`).
   - `_ydl_opts()` in `app/downloader.py` must use these instead of the
     hardcoded `"preferredcodec": "mp3"` / `"preferredquality": "128"`.
   - The on-disk filename extension changes with codec (`.mp3` vs `.opus` —
     confirm what yt-dlp's `FFmpegExtractAudio` postprocessor actually
     produces for `preferredcodec="opus"`; it may be `.opus` or `.ogg`,
     verify against yt-dlp's actual behavior/docs rather than assuming).
     Every place in the codebase that assumes `.mp3` needs to either derive
     the extension from config or handle it generically — audit
     `_download_entry()`'s `expected_file`, `app/feed.py`'s
     `fe.enclosure(audio_url, ..., "audio/mpeg")` (the enclosure MIME type
     must match the codec: `audio/mpeg` for mp3, `audio/ogg` or
     `audio/opus` for opus — verify what podcast apps expect), and
     `app/safety.py`'s `is_safe_media_name` (should already work — it doesn't
     hardcode an extension — confirm).
   - **This changes the extension/MIME for NEW downloads only.** Existing
     `.mp3` files and their feed entries must keep working exactly as
     before — this is not a migration of existing audio. Don't rewrite or
     re-encode anything already downloaded.
   - Document clearly in README.md that changing this mid-flight produces a
     feed with mixed-format episodes (old `.mp3` entries alongside new
     `.opus` ones) — which is fine (both are valid enclosures), but the user
     should know to expect it, not be surprised.

2. **Retention by age**: new env var `MAX_EPISODE_AGE_DAYS` (default `0`,
   meaning disabled). When set, `_prune_channel()` in `app/downloader.py`
   should also remove episodes older than this many days (based on the
   `published` column), in addition to the existing count-based cap — same
   file/DB/skip_videos deletion pattern already used for the count-based
   prune (reuse `_remove_if_exists`, `db.delete_episode`, `db.add_skip_video`
   with a reason like `"aged_out"`). Both caps apply — an episode surviving
   the count cap can still be pruned for being too old, and vice versa is
   already the existing behavior.

3. **Max-duration filter**: new env var `MAX_EPISODE_DURATION_MINUTES`
   (default `0`, meaning disabled). Videos longer than this should not be
   downloaded during channel polls (or one-off downloads — planner's call
   on whether to apply it there too; the brief's inclination is yes, for
   consistency, but a one-off download is an explicit user request so
   consider whether it should still be blocked, or just warned about).
   **Investigate first**: check whether `_fetch_channel_entries()`'s
   `extract_flat=True` entries actually carry a reliable `duration` field
   before assuming a pre-download check is possible — yt-dlp's flat
   extraction sometimes omits it. If it's unreliable, filtering must happen
   post-download (after `_download_entry()` returns `info` with a real
   `duration`) — delete the just-downloaded file and record a skip_video
   with reason `"too_long"` (same shape as the existing `MemberOnlyError`
   skip pattern in `poll_channel()`), so future polls don't re-attempt it.
   Post-download filtering wastes bandwidth on the rejected download but is
   the only reliable option if flat entries lack duration — confirm which
   applies by testing/reading the actual yt-dlp behavior, don't guess.

4. **Storage visibility in the dashboard**:
   - Extend `GET /api/state` (`app/main.py`) with per-channel disk usage (in
     bytes) for both subscribed and unsubscribed channels, and a
     `"total_bytes"` figure across everything (reuse the `_dir_bytes()`
     helper pattern already in `app/downloader.py`'s
     `find_orphan_channels()` — audio dir + thumbnail dir per channel_id).
   - Add a small storage readout to the dashboard (`app/static/app.js`):
     per-channel size shown alongside the existing episode-count badge
     (`epBadge`), and a total at the top of the subscribed-channels section
     (near the existing `#subs-count` count-pill). Match the existing
     `fmtBytes`-style formatting already added in v1.11.0's orphan UI
     (`app/static/app.js`) — reuse or extract that helper rather than
     duplicating it.
   - This is read-only — no new controls in the UI for this pass (codec,
     age, duration are env-var-only, consistent with how
     `MAX_EPISODES_PER_CHANNEL`/`POLL_INTERVAL_HOURS` already work).

## Out of scope (explicitly, for this pass)

- **True per-channel caps/settings** (a `channels.max_episodes` column,
  a UI control to set it per channel). This needs both a schema addition
  and new UI surface (a settings affordance per channel card), which is
  meaningfully more work and risk than the global-config approach above.
  Deferred to a later pass — note this explicitly in the changelog/README
  as a known limitation ("per-channel overrides are not yet supported").
- Re-encoding or migrating any already-downloaded audio.
- Group 1 (already in progress on its own branch — do not touch
  `app/notify.py`'s alert plumbing, `/health`, disk-pressure pruning, or
  backups; those are Group 1's territory and may be mid-flight on a
  sibling branch when this work happens — if you see uncommitted or
  branch-only Group 1 changes, that's expected, work only on this group's
  concerns on this group's own branch).
- Group 3 (feed tokens, combined feed, episode-level management, per-channel
  feed metadata) and Group 4 (identity/schema migration) — do not touch
  those code paths.

## Constraints

- Match existing code style/comment density exactly (see app/downloader.py,
  app/config.py for the register).
- This is a MINOR version bump. Read `app/__init__.py`'s current value at
  execution time (do not hardcode — Group 1 may have already bumped it) and
  increment the MINOR component from whatever is current; changelog entry
  dated 2026-09-03 (or the actual date if this runs later), following the
  existing changelog prose style (user-facing impact, full sentences).
- `app/safety.py`'s `is_safe_media_name` and the feed-building path
  (`app/feed.py`) must keep working for BOTH old `.mp3` files and any new
  non-mp3 files in the same channel's episode list — verify with a test
  that mixes both in one channel's `get_episodes()` result and confirms the
  feed builds correctly for both.
- Existing tests must keep passing; `tests/conftest.py`'s yt_dlp stub may
  need to reflect whatever codec/duration fields the new tests exercise —
  check it before assuming what the stub returns.

## Acceptance criteria

1. `AUDIO_CODEC=opus AUDIO_BITRATE_KBPS=64` produces yt-dlp options that
   request Opus at 64kbps, and downloaded files land with the correct
   extension; the feed enclosure MIME type matches. Verify via a test that
   inspects `_ydl_opts()`'s returned dict under each codec setting.
2. Default behavior (`AUDIO_CODEC` unset) is byte-for-byte identical to
   today: MP3 128kbps, `.mp3` extension, `audio/mpeg` MIME. Verify with a
   test asserting the default `_ydl_opts()` output is unchanged from before
   this change (a regression guard).
3. `MAX_EPISODE_AGE_DAYS` set to N causes `_prune_channel()` to remove
   episodes with `published` older than N days, in addition to the count
   cap, and records a skip_video so they aren't re-downloaded. Verify with
   a test seeding episodes at various ages.
4. `MAX_EPISODE_DURATION_MINUTES` set to N causes videos longer than N
   minutes to be excluded from downloads (mechanism per whichever of
   pre/post-download filtering the investigation in item 3 above
   determines is reliable), and they are not re-attempted on the next
   poll. Verify with a test.
5. `GET /api/state` includes per-channel byte sizes and a total, computed
   correctly against real files on disk. Verify with a test seeding known
   file sizes and checking the returned numbers.
6. The dashboard renders per-channel and total storage figures (verify by
   reading the app.js changes for correctness — no new browser-automation
   test infra needed for this small addition, but do sanity-check the
   markup/JS is well-formed, e.g. no syntax errors, matches existing
   patterns).
7. Full test suite passes: `.venv/bin/python -m pytest -q`.
8. Version bumped, changelog entry added, README updated (new env vars:
   `AUDIO_CODEC`, `AUDIO_BITRATE_KBPS`, `MAX_EPISODE_AGE_DAYS`,
   `MAX_EPISODE_DURATION_MINUTES`; note on mixed-format feeds; note on
   per-channel caps being out of scope for now).
9. Local deploy succeeds and the dashboard shows correct storage figures
   against the real running instance's data.

## Open questions & decisions made

- **Per-channel codec/caps**: deferred (schema + UI risk not worth it in
  this pass — see Out of scope). Global env vars only.
- **Duration-filter mechanism** (pre- vs post-download): left for the
  planner to determine by actually checking what `extract_flat=True`
  entries contain — do not guess, verify against real yt-dlp behavior or
  documentation.
- **Opus file extension**: left for the planner to verify against yt-dlp's
  actual `FFmpegExtractAudio` postprocessor behavior rather than assumed.
- **Duration filter applying to one-off downloads**: planner's call,
  reasoned in the plan (leaning toward applying it, since a one-off
  download of a multi-hour stream is presumably still unwanted, but a
  one-off is an explicit user request so document whichever choice is made
  and why).

## Relevant files/areas

- `app/downloader.py` — `_ydl_opts()` (codec/bitrate), `_download_entry()`
  (extension handling, duration check), `_prune_channel()` (age-based
  retention), `poll_channel()` (duration-filter skip recording, following
  the existing `MemberOnlyError`/`add_skip_video` pattern).
- `app/config.py` — new env vars, following the existing pattern.
- `app/feed.py` — enclosure MIME type must match codec.
- `app/safety.py` — confirm `is_safe_media_name` codec-agnostic (should
  already be, since it doesn't hardcode an extension pattern).
- `app/main.py` — `/api/state` storage figures (reuse `_dir_bytes()`-style
  logic from `app/downloader.py`'s `find_orphan_channels()`).
- `app/static/app.js` — dashboard storage display; reuse/extract the
  `fmtBytes` helper already added for the orphans UI in v1.11.0.
- `app/changelog.py`, `app/__init__.py`, `README.md`.
- `tests/test_polling.py`, `tests/test_endpoints.py`, `tests/test_feed.py`,
  `tests/conftest.py` (yt_dlp stub may need extending).

## Repo commands & tree state

- **Tests**: `.venv/bin/python -m pytest -q` (run from
  `/home/eric/projects/slipcast` repo root; use the repo's `.venv`, not
  bare `python`/`pytest`).
- **Build**: `docker compose build`
- **Deploy (local-only)**: `docker compose up -d`
- **Git**: at brief-writing time, `main` is clean, but **Group 1's build is
  running concurrently on branch `feat/resilience-self-healing`** — this
  group's work must branch from `main` as `feat/storage-retention`, NOT
  from Group 1's branch, and must not depend on any Group 1 change. If
  Group 1 has already merged to `main` by the time this executes, that's
  fine — branch from current `main` either way; just don't assume Group 1's
  files exist if `main` hasn't picked them up yet, and don't touch
  Group 1's territory (`app/notify.py` alert functions, `/health*`,
  disk-pressure pruning in `poll_all()`, backup code in `app/database.py`)
  regardless of merge order.
- Current version at brief-writing time: `1.11.0` in `app/__init__.py` —
  Group 1 may bump this first; verify the actual current value before
  bumping further, do not hardcode.
