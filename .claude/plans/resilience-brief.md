# Concept Brief: Resilience & Self-Healing (Group 1 of 4)

## Problem

Slipcast has already suffered one multi-week silent polling outage (fixed in
v1.10.0: a missing socket_timeout hung poll_all forever with no error, no log,
and `/health` reported "ok" throughout). v1.11.0 made `/health` reflect real
status (scheduler up, poll recency, cookie validity) but nothing acts on that
signal, and two more unbounded-wait code paths were found during that review
that can cause the exact same class of hang:

- `app/downloader.py` `_download_thumbnail()`: `urllib.request.urlretrieve(url, tmp)`
  has no timeout.
- Same function: `subprocess.run(["ffmpeg", "-y", "-i", tmp, dest], capture_output=True)`
  has no timeout.

Either can hang a poll thread indefinitely, reproducing the v1.10.0 outage via
a different path.

Beyond that, the app has no defense against: disk filling up (audio is
currently 5.2GB and grows unbounded across all channels combined — no
account for total volume size), the SQLite DB being lost or corrupted (no
backups exist anywhere), or a wedged process recovering without a human
manually restarting it.

## Goal

Make failures that *can* be fixed by a restart actually get restarted (bounded,
so it can't loop forever), make failures that *can't* be fixed by a restart
(disk pressure, DB loss) self-remediate where safe, and make everything that
still needs a human reliably reach one by email — reusing the existing SMTP
alert plumbing in `app/notify.py` rather than building new infra.

Explicitly **not** pursuing an external monitoring service (Healthchecks.io /
Uptime Kuma) — the user chose email-only via existing SMTP config over adding
a new dependency.

## In scope

1. **Timeout fix**: bound `urlretrieve` and the `ffmpeg` `subprocess.run` call
   in `_download_thumbnail()` (app/downloader.py) so neither can hang a poll
   thread forever. A failed/timed-out thumbnail download should degrade
   gracefully (as it already does on other exceptions there — log a warning,
   return False) not abort the episode download.

2. **`/health` liveness/degraded split**: separate a narrow "is a restart
   likely to help" signal from the existing full status report, so an
   external restarter (Item 3) never restart-loops on a condition a restart
   cannot fix (e.g. expired cookies, low disk).
   - New `GET /health/live` (add to `_PUBLIC_PREFIXES` in app/main.py):
     returns 200 only when the scheduler is running AND (no channels
     configured OR the most recent poll_runs row is younger than roughly
     `3 * POLL_INTERVAL_HOURS`, using the same startup-grace logic the
     current `/health` already has). Returns 503 otherwise. This must NOT
     consider cookie validity or disk space — those are not restart-fixable.
   - Existing `GET /health` keeps its current full behavior (scheduler +
     poll recency + cookie validity), unchanged in shape (`status`, `version`,
     `checks`, `problems`) — just no longer the thing the restarter polls.
   - Both stay unauthenticated, matching current `/health`.

3. **Host-side autoheal with a restart cap and email**: a script run by a
   systemd timer (matching how `cloudflared` already runs as a systemd
   service on this host — see CLOUDFLARE_TUNNEL.md for the pattern to
   follow) that:
   - Checks Docker's health status for the `slipcast-app-1` container
     (`docker inspect --format='{{.State.Health.Status}}'`) — but the actual
     decision to restart should be based on `/health/live` returning
     non-200 (curl it directly), NOT on Docker's own HEALTHCHECK status,
     since Docker's HEALTHCHECK (added in v1.11.0) still points at `/health`
     (the full report) and would restart-loop on cookie expiry. Repoint the
     Docker `HEALTHCHECK` itself at `/health/live` too, so `docker compose ps`
     and this script agree on what "unhealthy" means for restart purposes.
   - Tracks restart timestamps in a small state file (e.g.
     `/data/.autoheal_state` inside the bind-mounted `./data` dir, or a
     separate file outside the repo under e.g. `/etc/slipcast/` — pick
     whichever fits the existing pattern in this repo/host cleaner; note
     `./data` is already the durable, gitignored volume).
   - Caps restarts at 3 per rolling hour. Under the cap: `docker compose
     restart app` (or equivalent) and log it. At/over the cap: do NOT
     restart again; instead send an email directly via SMTP (reading the
     same `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM`/
     `ALERT_EMAIL` values Slipcast already uses — from the project's `.env`
     file — so it works even if the app container is fully down) saying
     restarts are exhausted and human attention is needed, then stop trying
     until a human intervenes (a marker file or long cooldown, not a silent
     infinite skip — the next timer tick should still report, not just
     go quiet).
   - Document this script's location, the systemd timer unit, and how to
     check its status, following the existing `CLOUDFLARE_TUNNEL.md` /
     `README.md` documentation conventions in this repo.
   - This script runs on the host, not in the container — it is not part of
     `app/`. Put it under a new `ops/` directory in the repo (e.g.
     `ops/autoheal.sh`, `ops/slipcast-autoheal.service`,
     `ops/slipcast-autoheal.timer`) so it's version-controlled, with a
     short `ops/README.md` explaining host installation (this is documentation
     + a script the user installs manually via `systemctl` commands they run
     themselves after this PR — the plan does NOT need to actually install
     it on the host, since that's a live, host-level, outside-the-repo
     action; document exact install steps instead).

4. **Disk-pressure auto-prune**: when free space on the filesystem backing
   `DATA_DIR` drops below a threshold, prune the oldest episodes *across all
   channels* (not per-channel) until back above it, or until nothing is left
   to prune. New env var `MIN_FREE_DISK_GB` (default `2`). Check this at the
   start of every `poll_all()` run (app/downloader.py) before polling any
   channel, and log what was pruned. This is real remediation (not just
   detection) — it should actually delete files and DB rows for the oldest
   episodes (by `published` date, oldest first, regardless of which channel
   they belong to) via the existing `db.delete_episode()` and file-removal
   patterns already used in `_prune_channel()`/`_sweep_orphan_files()|
   `_remove_if_exists()`. Send an email alert (new notify.py function,
   following the existing `send_cookie_alert()`/`send_poll_failure_alert()`
   pattern including its cooldown mechanism) when this fires, listing what
   was pruned, so silent data loss to free space never happens unnoticed.

5. **DB backup + restore**:
   - Nightly `PRAGMA VACUUM INTO` of `episodes.db` to a timestamped file
     under `DATA_DIR/backups/` (e.g. `episodes-YYYYMMDD-HHMMSS.db`), scheduled
     via the existing `BackgroundScheduler` in app/main.py's `lifespan`
     (`add_job(..., "cron", hour=<pick a quiet hour, e.g. 3>, ...)` or
     `"interval", hours=24` — planner's call, but must not collide with
     `POLL_INTERVAL_HOURS` timing in a way that's hard to reason about).
   - Retain the last 7 backups; prune older ones each time a new backup is
     made.
   - Also run `PRAGMA integrity_check` as part of the backup job; if it
     fails, email an alert (reuse the notify.py pattern) — corruption
     detection, not automatic restore (restore should be a documented manual
     procedure, not an automated action against a possibly-still-corrupting
     condition).
   - Document the manual restore procedure (stop container, copy a backup
     file over `episodes.db`, restart) in README.md.

## Out of scope (explicitly, for this pass)

- External monitoring service (Healthchecks.io/Uptime Kuma) — rejected by
  the user in favor of email-only.
- Killing a genuinely wedged Python thread from inside the process — Python
  cannot do this; the host-side restart (Item 3) is the only recovery for a
  truly stuck thread, which is why Item 3 exists.
- Any of Group 2 (codec/bitrate, per-channel caps/retention, max-duration
  filter), Group 3 (feed tokens, combined feed, episode-level management,
  per-channel feed metadata), or Group 4 (channel_id-as-primary-key schema
  migration) — those are separate, later passes in this same overall effort;
  do not touch their code paths here.
- Actually installing/enabling the systemd timer on the host machine as part
  of this change — that's a manual, host-level step the user (or a later,
  explicit non-code action) performs; this pass delivers the versioned
  script + docs only.

## Constraints

- Match existing code style exactly — this codebase uses substantial
  explanatory "why" comments above non-obvious logic (see existing comments
  throughout app/downloader.py and app/main.py for the register to match).
- All new email alerts must reuse `app/notify.py`'s existing
  cooldown/dedup mechanism (`_last_sent`/`_record_sent` against
  `_ALERT_STATE_FILE`) so a repeated condition doesn't spam.
- `POLL_CONCURRENCY`, the per-channel poll lock, and the orphan reconciler
  added in v1.11.0 (already deployed, merged as PR #6, commit d886673) are
  the current baseline — do not regress any of that.
- SQLite is in WAL mode (set in `app/database.py` `init_db()`); the backup
  job must account for WAL mode when using `VACUUM INTO` (it should work
  correctly under WAL, but verify — `VACUUM INTO` produces a plain,
  non-WAL snapshot file, which is what we want for a backup).
- This is a MINOR version bump per this project's SemVer convention
  (`app/__init__.py` + a new top entry in `app/changelog.py`, following the
  existing changelog prose style — see the 1.10.0/1.11.0 entries for the
  register: user-facing impact, not a commit-log style list). Use today's
  date, 2026-09-03, and pick the next version after whatever is current in
  `app/__init__.py` at execution time (check it — do not hardcode a version
  number in the plan in case it has moved).

## Acceptance criteria

1. `_download_thumbnail()` cannot hang longer than a bounded time even if the
   remote host or ffmpeg never responds (verify with a timeout parameter
   present on both the urlretrieve call and the subprocess.run call).
2. `GET /health/live` returns 200 when the scheduler is running and polling
   isn't stale, and does NOT flip to non-200 merely because cookies are
   expired or disk is low (verify by simulating expired cookies / low disk
   in a test and confirming `/health/live` stays 200 while `/health` reports
   degraded).
3. `GET /health` behavior is unchanged from v1.11.0 (existing tests for it
   must still pass unmodified in intent, even if updated for the new route
   existing alongside it).
4. The Docker `HEALTHCHECK` in `Dockerfile`/`docker-compose.yml` points at
   `/health/live`, not `/health`.
5. `ops/autoheal.sh` (or equivalent) exists, is executable, restarts the
   container when `/health/live` is unhealthy, caps at 3 restarts/rolling
   hour, and sends an email (via direct SMTP, reading `.env`) when the cap
   is hit — verify by reading the script logic carefully (host-level
   execution isn't testable in CI/pytest, so this is a code-review-level
   verification, documented as such) plus a dry-run invocation against the
   locally running container if safe to do so without disrupting it.
6. A poll_all() run started with free disk below `MIN_FREE_DISK_GB` prunes
   the globally oldest episodes (across channels) until back above the
   threshold, and sends an alert email — verify with a test that fakes low
   disk space (e.g. monkeypatching `shutil.disk_usage`) and confirms
   episodes were deleted oldest-first regardless of channel and that an
   alert function was called.
7. A nightly backup job exists on the scheduler, writes a timestamped
   `VACUUM INTO` snapshot under `DATA_DIR/backups/`, and prunes down to the
   7 most recent — verify with a test that seeds 9 fake backup files and
   confirms exactly 7 remain after the prune step runs, plus a test that the
   backup function actually produces a valid, openable SQLite file from a
   real (test) `episodes.db`.
8. `PRAGMA integrity_check` runs as part of the backup job and a failure
   triggers an alert email — verify with a test that simulates a failing
   integrity_check result and confirms the alert path fires.
9. Full test suite passes: `.venv/bin/python -m pytest -q` from repo root.
10. Version bumped, changelog entry added, README updated (new env vars:
    `MIN_FREE_DISK_GB` and anything else introduced; new `/health/live`
    endpoint; backup/restore procedure; pointer to `ops/README.md` for the
    autoheal timer).
11. Local deploy (`docker compose build && docker compose up -d`) succeeds,
    the container reports healthy, and `/health/live` and `/health` both
    respond correctly against the real running instance.

## Open questions & decisions made

- **Dead-man's switch**: rejected. Email-only via existing SMTP config.
- **Autoheal mechanism**: host-side systemd timer script (not a Docker
  sidecar), because it can email even when the container itself is down,
  matching the existing `cloudflared` systemd pattern on this host.
- **Disk threshold**: `MIN_FREE_DISK_GB` env var, default `2` (GB free on
  the `DATA_DIR` filesystem).
- **Backup cadence/retention**: nightly, keep last 7.
- **Restart cap**: 3 restarts per rolling hour, then stop and email — the
  planner should pick the exact state-file mechanics and hour-key
  bookkeeping approach; keep it simple (a small append-only or fixed-size
  timestamp log is fine, following the same "prune stale entries on read"
  pattern `app/main.py`'s `_check_rate_limit`/`_failed_attempts` already
  uses for a similar rolling-window problem — a good model to reference).
- Exact backup schedule hour (e.g. 3am local/UTC) and how it's expressed via
  APScheduler (`cron` vs `interval`) is the planner's call; avoid colliding
  awkwardly with `POLL_INTERVAL_HOURS`-driven polls but this isn't a hard
  constraint (they can overlap; SQLite WAL mode handles concurrent
  readers fine, and `VACUUM INTO` reads a live DB safely).

## Relevant files/areas

- `app/downloader.py` — `_download_thumbnail()` (timeout fix), `poll_all()`
  (disk-pressure check entry point), existing `_prune_channel()` /
  `_sweep_orphan_files()` / `_remove_if_exists()` patterns to reuse for
  cross-channel oldest-first pruning, existing per-channel lock pattern
  (`_poll_locks`) as a reference for the restart-cap rolling-window pattern.
- `app/main.py` — `lifespan()` (scheduler job registration — add backup job
  here), existing `/health` implementation (`_seconds_since_last_poll()`,
  `_STARTED_MONOTONIC`, `stale_after` logic) to share/extract for
  `/health/live`, `_PUBLIC_PREFIXES`.
- `app/notify.py` — existing `_smtp_configured()`, `_send()`,
  `_last_sent()`/`_record_sent()` cooldown mechanism, `send_cookie_alert()`
  and `send_poll_failure_alert()` as the pattern to follow for new alert
  functions (disk-pressure alert, backup-integrity alert).
  `_ALERT_STATE_FILE` for the dedup mechanism.
- `app/config.py` — add `MIN_FREE_DISK_GB` (and any other new env vars the
  planner introduces) following the existing `int(os.environ.get(...))`
  pattern.
- `app/database.py` — `get_conn()`/`DB_PATH` for the backup job to read from.
- `Dockerfile`, `docker-compose.yml` — repoint `HEALTHCHECK`/`healthcheck:`
  at `/health/live`.
- `app/changelog.py`, `app/__init__.py` — version bump + entry.
- `README.md` — new env vars, `/health/live`, backup/restore docs.
- `CLOUDFLARE_TUNNEL.md` — reference for the existing systemd-service
  documentation pattern to match for the new `ops/` systemd timer docs.
- New: `ops/autoheal.sh`, `ops/slipcast-autoheal.service`,
  `ops/slipcast-autoheal.timer`, `ops/README.md`.
- `tests/test_polling.py`, `tests/test_endpoints.py`, `tests/test_notify.py`,
  `tests/test_database.py` — existing test files to extend, matching their
  current fixture/mocking patterns (see `tests/conftest.py` for the yt_dlp
  stub and `DATA_DIR` tempdir setup already in place).

## Repo commands & tree state

- **Tests**: `.venv/bin/python -m pytest -q` (run from
  `/home/eric/projects/slipcast` repo root; do NOT assume `python`/`pytest`
  are on bare `PATH` — this project has a `.venv`).
- **Build**: `docker compose build`
- **Deploy (local-only; this project does NOT use the Docker Hub tag/CI/pull
  flow some other projects use)**: `docker compose up -d`
- **Health check after deploy**: `curl -s http://localhost:8000/health` and
  `curl -s http://localhost:8000/health/live`
- **Git**: working tree was clean on `main` at the start of this pass
  (verified via `git status` immediately before writing this brief — no
  pre-existing uncommitted changes). Branch for this work:
  `feat/resilience-self-healing`, created off `main`.
- Current version at brief-writing time: `1.11.0` (last shipped: PR #6,
  commit `d886673`, merged to `main`). Verify this is still current
  `app/__init__.py` content before bumping — do not assume.
