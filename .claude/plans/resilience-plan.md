# Implementation Plan: Resilience & Self-Healing (Group 1 of 4)

Source brief: `/home/eric/projects/slipcast/.claude/plans/resilience-brief.md` (read it too —
this plan is the authority on *how*, the brief is the authority on *what/why*).

## Recommended executor model

**Opus 5.** This touches ~16 files across four subsystems (downloader, FastAPI health
routes, APScheduler jobs, host-side bash+SMTP script) and includes genuinely destructive
logic (auto-deleting episodes/files to free disk) plus a restart-loop guard whose failure
mode is "restarts the container forever". The rolling-window bookkeeping, the
`/health` vs `/health/live` split (a wrong split re-creates a restart loop on cookie
expiry), and shell-level SMTP are all easy to get subtly wrong in ways tests won't catch.

---

## Summary

Slipcast has already had one multi-week silent polling outage caused by an unbounded
network wait, and two more unbounded waits remain in `_download_thumbnail()`. Beyond
hangs, the app has no defence against a full disk, a lost/corrupt SQLite DB, or a wedged
process that only a restart can fix. This change bounds the two remaining hang paths,
splits `/health` into a full status report plus a narrow restart-fixable liveness signal
(`/health/live`), ships a version-controlled host-side systemd autoheal script that
restarts the container off that signal with a 3-per-rolling-hour cap and emails when the
cap is exhausted, auto-prunes the globally oldest episodes when free disk drops below
`MIN_FREE_DISK_GB` (with an alert email so the deletion is never silent), and adds a
nightly `VACUUM INTO` backup with 7-file retention plus a `PRAGMA integrity_check` that
alerts on corruption. All alerting reuses `app/notify.py`'s existing SMTP + cooldown
plumbing; no new services or dependencies are introduced.

---

## Approach & key decisions

1. **Liveness split via a shared helper, not a duplicated route.**
   `/health` today computes three checks inline. Extract the *restart-fixable* subset
   (scheduler running + poll recency, including the `_STARTED_MONOTONIC` startup grace)
   into one helper used by both routes. `/health/live` returns only that; `/health` layers
   the cookie check on top and keeps its exact current response shape.
   *Rejected:* a `?strict=` query param on `/health` — Docker `HEALTHCHECK` and curl in a
   shell script both read cleaner against a distinct path, and a separate path can never
   be accidentally invoked without the flag.

2. **Backup lives in `app/database.py`, scheduled from `app/main.py`'s `lifespan`.**
   `database.py` already owns `DB_PATH`, `get_conn()` and the WAL/pragma knowledge, and the
   existing tests monkeypatch `db.DB_PATH` — putting backup there makes it testable with the
   established fixture pattern (`tests/test_database.py::_setup_tmp`).
   *Rejected:* a new `app/backup.py` module — one more import surface for ~60 lines that are
   pure DB concerns.

3. **`VACUUM INTO` for backups.** Works correctly against a live WAL database (it takes a
   read transaction and writes a fresh, fully-checkpointed, non-WAL file), so it is safe to
   run while polls are writing, and the output is a single self-contained snapshot file with
   no `-wal`/`-shm` sidecars to copy. `PRAGMA integrity_check` is run against the *source*
   DB in the same job.
   *Rejected:* `sqlite3.Connection.backup()` (produces a copy that may still need WAL files
   reasoned about) and file copying (unsafe under WAL).

4. **Cron at 03:00 for the backup.** Expressed as `add_job(..., "cron", hour=3, ...)`.
   `POLL_INTERVAL_HOURS` is interval-based from process start, so no fixed hour avoids it;
   an overlap is harmless (see decision 3), and a fixed hour is far easier to reason about
   ("last night's backup") than a 24h interval anchored to an arbitrary restart time.

5. **Disk pruning is global and oldest-first, gated at the top of `poll_all()`.**
   A new `db.get_all_episodes_oldest_first()` returns every episode row ordered by
   `published ASC`, and the pruner walks it deleting audio + thumbnail + row (reusing
   `_remove_if_exists()` and `db.delete_episode()`, exactly as `_prune_channel()` does)
   until free space clears the threshold. Each deleted video is also recorded via
   `db.add_skip_video(..., "disk_pressure")` — without it the very next poll re-downloads
   what was just deleted and refills the disk, the same fight `_prune_channel()` documents.
   *Rejected:* per-channel proportional pruning — the brief explicitly wants global
   oldest-first, and it is far simpler to reason about.

6. **Autoheal is a host bash script + systemd timer, delivered but not installed.**
   It decides on `curl /health/live`, not on Docker's health status (Docker's status is
   only *reported*, for the log line). Restart bookkeeping is a rolling-window timestamp
   file pruned on read — the same shape as `main._check_rate_limit`. When the cap is hit it
   writes a pause marker, emails via SMTP using python3's `smtplib` (reading the project's
   `.env`, so it works even with the container fully down), and on each subsequent tick
   still *reports* (logs, and re-emails at most every 24h) rather than going silent. The
   pause clears automatically the first time `/health/live` is healthy again.
   *Rejected:* a Docker autoheal sidecar (can't email when the whole stack is down) and
   `curl --url smtps://` (fiddly quoting/auth vs. a 15-line python heredoc).

7. **State files live in `./data/`** (`.autoheal_state`, `.autoheal_paused`), next to the
   existing `.alert_state` — already the durable, gitignored, host-visible volume.
   *Rejected:* `/etc/slipcast/` — needs root and a second install step for no benefit.

---

## Step-by-step tasks

Branch: `feat/resilience-self-healing` off `main` (`git switch -c feat/resilience-self-healing`).
Working tree should be clean before starting; run the test suite once first to confirm a
green baseline: `.venv/bin/python -m pytest -q`.

### Task 1 — Bound the two hangs in `_download_thumbnail()`
**File:** `app/downloader.py`

- Add module constants near `_ALLOWED_THUMBNAIL_HOST_SUFFIXES`:
  `_THUMBNAIL_FETCH_TIMEOUT = 30` and `_FFMPEG_TIMEOUT = 60`, with a "why" comment in the
  register of the existing `socket_timeout` comment in `_base_ydl_opts()`: an unbounded wait
  here hangs a poll thread forever, and with `max_instances=1` that silently blocks every
  future scheduled poll — the exact v1.10.0 outage via a different path.
- In `_download_thumbnail()`, replace `urllib.request.urlretrieve(url, tmp)` with an
  explicitly-timed fetch. `urlretrieve` takes no timeout, so use:
  ```python
  with urllib.request.urlopen(url, timeout=_THUMBNAIL_FETCH_TIMEOUT) as resp, open(tmp, "wb") as out:
      shutil.copyfileobj(resp, out)
  ```
  (add `import shutil` at module top — note `remove_channel_data()` currently does a
  function-local `import shutil`; leave that line alone or drop it once the module-level
  import exists, either is fine, but do not change that function's behaviour).
  Note the `timeout=` here bounds each socket operation, not total wall time; that is
  the same guarantee `socket_timeout` gives yt-dlp and is sufficient.
- Add `timeout=_FFMPEG_TIMEOUT` to the `subprocess.run([...ffmpeg...])` call.
- `subprocess.TimeoutExpired` and `urllib.error.*`/`OSError` are all `Exception` subclasses,
  so the existing `except Exception` already degrades gracefully (warn + cleanup + return
  `False`). Confirm the `tmp` cleanup path still runs; no behaviour change needed there.
- Acceptance criterion 1 requires a `timeout` argument literally present on both calls.

### Task 2 — `MIN_FREE_DISK_GB` config
**File:** `app/config.py`

- Add below `POLL_CONCURRENCY`, following the existing `int(os.environ.get(...))` pattern:
  ```python
  MIN_FREE_DISK_GB = int(os.environ.get("MIN_FREE_DISK_GB", "2"))
  ```
  with a short comment: audio grows unbounded across all channels; below this many GB free
  on the `DATA_DIR` filesystem, `poll_all()` prunes the globally oldest episodes before
  downloading more.
- Add `BACKUP_DIR = os.path.join(DATA_DIR, "backups")` next to the other derived paths at
  the top (`AUDIO_DIR`/`THUMBNAIL_DIR`/`DB_PATH`).

### Task 3 — Two new alert emails
**File:** `app/notify.py`

Follow `send_poll_failure_alert()` exactly: a private `_<x>_message() -> EmailMessage`
builder plus a public sender that checks `_smtp_configured()`, honours
`config.ALERT_COOLDOWN_HOURS` via `_last_sent`/`_record_sent` under its **own** key, accepts
`force: bool = False`, returns `True` if sent, and swallows send exceptions with
`logger.error`. Plain-text bodies are fine (the poll-failure alert is plain-text only).

- `send_disk_prune_alert(pruned: list[str], freed_bytes: int, free_gb: float, force=False) -> bool`
  — key `"disk_prune"`. Subject e.g. `⚠️ Slipcast: pruned N episode(s) to free disk space`.
  Body: how much was free, the threshold, how much was freed, and the list of pruned
  episodes (cap the listing at ~20 lines with a "…and N more"), phrased so the user knows
  data was deleted and can raise the disk or lower `MAX_EPISODES_PER_CHANNEL`.
  Return `False` immediately if `pruned` is empty (mirrors the `if not problems` guard).
- `send_backup_failure_alert(reason: str, force=False) -> bool` — key `"backup_failure"`.
  Subject e.g. `⚠️ Slipcast: database backup problem`. Body states the reason (a failed
  `integrity_check` result, or a backup exception) and points at the manual restore
  procedure in README.md.

### Task 4 — Global oldest-first episode query
**File:** `app/database.py`

```python
def get_all_episodes_oldest_first() -> list[sqlite3.Row]:
    """Every episode across all channels, oldest first.

    Used by the disk-pressure pruner (app/downloader.py), which frees space by
    globally oldest episode rather than per channel — unlike get_episodes(),
    which is per-channel and newest-first for feed building.
    """
    with get_conn() as conn:
        return conn.execute("SELECT * FROM episodes ORDER BY published ASC").fetchall()
```

### Task 5 — Disk-pressure auto-prune
**File:** `app/downloader.py`

- Extend the `from app.config import (...)` block with `DATA_DIR` and `MIN_FREE_DISK_GB`
  (importing them into the module namespace matters: the tests monkeypatch
  `downloader.AUDIO_DIR` etc. the same way, and will monkeypatch these).
- Add:
  ```python
  def _free_disk_gb(path: str) -> float:
      return shutil.disk_usage(path).free / (1024 ** 3)
  ```
- Add `_enforce_disk_floor() -> None`:
  - Return immediately if `MIN_FREE_DISK_GB <= 0` (an explicit opt-out).
  - `free = _free_disk_gb(DATA_DIR)`; return if `free >= MIN_FREE_DISK_GB`.
  - Log a warning with the current free GB and the threshold.
  - Walk `db.get_all_episodes_oldest_first()`, and for each episode until free space clears
    the threshold or the list is exhausted:
    - `_remove_if_exists(os.path.join(_audio_dir_for(ep["channel_id"]), ep["filename"]))`
    - if `ep["thumbnail"]`: `_remove_if_exists(os.path.join(_thumbnail_dir_for(ep["channel_id"]), ep["thumbnail"]))`
    - `db.delete_episode(ep["id"])`
    - `db.add_skip_video(ep["id"], ep["channel_id"], "disk_pressure")` — with the same "why"
      comment rationale as `_prune_channel()`: otherwise the next poll immediately
      re-downloads exactly what we deleted and refills the disk.
    - accumulate `ep["filesize"] or 0` into `freed_bytes` and
      `f'{ep["channel_name"]} — {ep["title"]}'` into a `pruned` list.
    - Re-check `_free_disk_gb(DATA_DIR)` **after each deletion** and break when clear.
      (Deleting one 200 MB file at a time and re-`statvfs`-ing is cheap and stops as soon
      as possible — do not batch-delete a guessed count.)
  - Guard each per-episode deletion in a `try/except Exception` that logs and continues, so
    one unlink failure (permissions, already gone) can't abort the whole remediation.
  - Skip a `channel_id` that fails `_CHANNEL_ID_RE` (`_audio_dir_for` raises `ValueError` on
    those) — log and continue; still delete the DB row.
  - If anything was pruned: `logger.warning` a summary and call
    `notify.send_disk_prune_alert(pruned, freed_bytes, _free_disk_gb(DATA_DIR))`.
  - If the list is exhausted and free space is *still* below the threshold, log an error
    saying nothing is left to prune (the alert already went out for whatever was pruned; if
    nothing at all was pruned, still send the alert with an empty-list-safe message — pass a
    single synthetic line such as `"(nothing left to prune)"` so the human hears about it,
    since `send_disk_prune_alert([])` returns False by design).
- In `poll_all()`, as the **first** statement of the function body, before the cookie checks:
  ```python
  try:
      _enforce_disk_floor()
  except Exception:  # noqa: BLE001 — remediation must never abort the poll run
      logger.exception("Disk-pressure check failed")
  ```
  with a comment explaining it runs before any download so a full disk is relieved rather
  than hit mid-download.

### Task 6 — `/health/live` and the shared liveness helper
**File:** `app/main.py`

- Add a helper above `health()`:
  ```python
  def _liveness(checks: dict[str, str]) -> list[str]:
      """Restart-fixable health only: scheduler running + polling not stalled.

      Deliberately excludes cookie validity and disk space — a restart cannot fix
      either, and the host autoheal script (ops/autoheal.sh) restarts off this
      signal, so including them would produce a restart loop on cookie expiry.
      Fills `checks` in place and returns the list of problems found.
      """
  ```
  Move the existing scheduler block and the whole polling block (`channels`, `stale_after`,
  `uptime`, `last_poll_age`, and all four branches) into it **verbatim** — same `checks` keys
  (`"scheduler"`, `"polling"`), same strings, same problem messages. Do not change any
  wording; `tests/test_endpoints.py` asserts on `checks["scheduler"] == "running"`,
  `checks["polling"] == "starting up"` and `"no poll has completed since startup"`.
- Rewrite `health()` to call `_liveness(checks)` for `problems`, then append the existing
  cookie check, then build the identical body/status-code. Keep its docstring, extending it
  with one sentence pointing at `/health/live`.
- Add:
  ```python
  @app.get("/health/live")
  def health_live():
      """Narrow liveness signal for the host autoheal restarter (ops/autoheal.sh)
      and the Docker HEALTHCHECK: 200 only when a restart would NOT be pointless.
      ...
      """
      checks: dict[str, str] = {}
      problems = _liveness(checks)
      ok = not problems
      return JSONResponse(
          {"status": "ok" if ok else "degraded", "version": VERSION,
           "checks": checks, "problems": problems},
          status_code=200 if ok else 503,
      )
  ```
- `_PUBLIC_PREFIXES` already contains `"/health"` and the auth middleware uses
  `request.url.path.startswith(_PUBLIC_PREFIXES)`, so `/health/live` is **already**
  unauthenticated — no change needed. Add a brief comment on that line noting the prefix
  covers `/health/live` too, so nobody later "tightens" it to an exact match and breaks the
  Docker healthcheck.

### Task 7 — Nightly backup + integrity check
**Files:** `app/database.py`, `app/main.py`

In `app/database.py` (import `os`, `logging`, `datetime` as needed; import `BACKUP_DIR`
alongside `DB_PATH` from `app.config` so tests can monkeypatch `db.BACKUP_DIR`):

- `_BACKUP_RETAIN = 7` module constant with a comment.
- `backup_db() -> str`: `os.makedirs(BACKUP_DIR, exist_ok=True)`, build
  `episodes-YYYYMMDD-HHMMSS.db` from `datetime.now(timezone.utc)`, then
  `conn.execute("VACUUM INTO ?", (dest,))` via `get_conn()`. Comment why `VACUUM INTO`:
  safe against a live WAL database, and it emits a single self-contained non-WAL snapshot
  (no `-wal`/`-shm` sidecars to copy). Return the destination path.
  Note: `VACUUM INTO` fails if the destination file already exists — the second-resolution
  timestamp makes a collision essentially impossible, but if the target exists, append a
  short `secrets.token_hex(2)` suffix rather than overwriting.
- `prune_backups(retain: int = _BACKUP_RETAIN) -> list[str]`: list `BACKUP_DIR` for files
  matching `episodes-*.db`, sort by name descending (the timestamp format sorts
  lexicographically), delete everything past `retain`, return the deleted paths. Tolerate a
  missing `BACKUP_DIR` (return `[]`).
- `integrity_check() -> str`: `conn.execute("PRAGMA integrity_check").fetchone()[0]` —
  returns `"ok"` on a healthy DB. Keep it as a thin function so a test can monkeypatch it.
- `run_backup_job() -> None`: the scheduled entry point.
  1. `result = integrity_check()`; if `result != "ok"`, `logger.error(...)` and
     `notify.send_backup_failure_alert(f"PRAGMA integrity_check returned: {result}")`.
     **Still take the backup** (a corrupt-but-readable DB snapshot is better than nothing)
     — but if `VACUUM INTO` itself then raises, that is caught below.
  2. `path = backup_db()`; `deleted = prune_backups()`; `logger.info` both.
  3. Wrap the whole body in `try/except Exception` → `logger.exception` +
     `notify.send_backup_failure_alert(f"backup failed: {exc}")`, so a scheduler job can
     never crash silently.
  **Import cycle warning:** `app/notify.py` imports `app.config` only, and
  `app/database.py` currently imports nothing from the app but config — importing
  `app.notify` at the top of `database.py` is therefore safe (no cycle). Verify with
  `.venv/bin/python -c "import app.database"` after the edit; if anything unexpected
  appears, do the `from app import notify` import inside `run_backup_job()` instead.

In `app/main.py` `lifespan()`, next to the existing `_prune_rate_limit_table` job:
```python
# Nightly DB snapshot + corruption check. 03:00 is a quiet hour; VACUUM INTO
# reads a live WAL database safely, so overlapping a poll is harmless.
_scheduler.add_job(db.run_backup_job, "cron", hour=3, coalesce=True,
                   misfire_grace_time=3600, max_instances=1)
```

### Task 8 — Repoint the Docker healthchecks
**Files:** `Dockerfile`, `docker-compose.yml`

- `Dockerfile`: `CMD curl -fsS http://localhost:8000/health/live || exit 1`. Update the
  comment above it: `/health/live` is the restart-fixable signal — pointing the healthcheck
  at the full `/health` would mark the container unhealthy (and, with an external
  restarter, restart-loop it) on cookie expiry, which a restart cannot fix.
- `docker-compose.yml`: `test: ["CMD", "curl", "-fsS", "http://localhost:8000/health/live"]`
  and update the comment block above `healthcheck:` the same way, keeping the existing note
  that `restart: unless-stopped` does not itself restart on unhealthy (now adding: that's
  what `ops/autoheal.sh` is for).
- Add `- MIN_FREE_DISK_GB=2` to the `environment:` list with a one-line comment, near
  `MAX_EPISODES_PER_CHANNEL`.

### Task 9 — `ops/` autoheal script + systemd units
**New files:** `ops/autoheal.sh` (mode 755), `ops/slipcast-autoheal.service`,
`ops/slipcast-autoheal.timer`, `ops/README.md`

`ops/autoheal.sh` — `#!/usr/bin/env bash`, `set -uo pipefail` (**not** `-e`: a failing
`curl -f` is expected control flow), logging every line to stdout with a timestamp prefix
(systemd captures it into the journal). Structure:

1. Config block at the top, all overridable by env: `PROJECT_DIR` (default the script's own
   `..` via `$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)`), `HEALTH_URL`
   (`http://127.0.0.1:8000/health/live`), `CONTAINER` (`slipcast-app-1`),
   `STATE_FILE` (`$PROJECT_DIR/data/.autoheal_state`),
   `PAUSE_FILE` (`$PROJECT_DIR/data/.autoheal_paused`),
   `MAX_RESTARTS=3`, `WINDOW_SECONDS=3600`, `PAUSE_EMAIL_INTERVAL=86400`.
2. Load `.env`: `set -a; . "$PROJECT_DIR/.env"; set +a` guarded by `[ -f ... ]`.
   (`.env` holds the SMTP values; it is gitignored, so never echo its contents.)
3. Report Docker's view for the log only:
   `docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo unknown`
   — logged, never used for the restart decision (it can disagree with `/health/live`
   during the healthcheck's `start_period`/`retries` window).
4. Probe: `curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null` → exit code decides.
5. **Healthy:** if `PAUSE_FILE` exists, remove it and log "recovered — restart budget
   released"; truncate/clear `STATE_FILE`; exit 0.
6. **Unhealthy:**
   - If `PAUSE_FILE` exists: log "restarts exhausted; awaiting human", and if the timestamp
     inside it is older than `PAUSE_EMAIL_INTERVAL`, re-send the exhausted email and rewrite
     the file with the new timestamp. Exit 0 — never restart while paused, but never go
     silent either.
   - Otherwise, read `STATE_FILE` (one unix timestamp per line), **drop lines older than
     `WINDOW_SECONDS`** and rewrite the file — the same prune-stale-entries-on-read rolling
     window `app/main.py`'s `_check_rate_limit`/`_failed_attempts` uses.
   - If the surviving count `>= MAX_RESTARTS`: write `date +%s` into `PAUSE_FILE`, send the
     "restarts exhausted" email, log loudly, exit 0.
   - Else: append `date +%s` to `STATE_FILE`, log the restart with the current count, and
     run `docker compose -f "$PROJECT_DIR/docker-compose.yml" restart app`
     (fall back to `docker restart "$CONTAINER"` if the compose command fails). Exit 0.
7. `send_email <subject> <body>` helper: if `command -v python3` is missing or
   `SMTP_HOST`/`SMTP_USER`/`SMTP_PASS`/`ALERT_EMAIL` are unset, log "email not sent: …" and
   return. Otherwise a `python3 - <<'PY'` heredoc that reads
   `SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM/ALERT_EMAIL/SUBJECT/BODY` **from
   `os.environ`** (export them before the heredoc — do **not** interpolate secrets into the
   script text) and mirrors `app/notify.py::_send`: port 465 → `smtplib.SMTP_SSL` with
   `ssl.create_default_context()`, otherwise `SMTP` + `ehlo/starttls/ehlo`, `timeout=20`,
   `login`, `send_message` of an `EmailMessage`. Wrap in try/except and print the failure.
   The email body must say: how many restarts were attempted in the last hour, that Slipcast
   is *not* being restarted any more, the host/container name, and how to inspect
   (`docker compose logs --tail=200 app`, `curl -s localhost:8000/health`).

`ops/slipcast-autoheal.service` — `Type=oneshot`, `ExecStart=/home/eric/projects/slipcast/ops/autoheal.sh`,
`User=eric`, `WorkingDirectory=/home/eric/projects/slipcast`, a `[Unit] Description=`.
No `[Install]` section (the timer is what gets enabled).

`ops/slipcast-autoheal.timer` — `OnBootSec=5min`, `OnUnitActiveSec=5min`,
`Unit=slipcast-autoheal.service`, `[Install] WantedBy=timers.target`.

`ops/README.md` — follow the `CLOUDFLARE_TUNNEL.md` register (numbered steps, fenced
commands, a "Verify" step, a Troubleshooting/Notes section at the end). Cover:
- What the script does and, explicitly, why it decides on `/health/live` not `/health`.
- Install (the user runs these by hand — this PR does **not** install anything):
  ```bash
  chmod +x /home/eric/projects/slipcast/ops/autoheal.sh
  sudo cp /home/eric/projects/slipcast/ops/slipcast-autoheal.{service,timer} /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now slipcast-autoheal.timer
  ```
- Verify: `systemctl status slipcast-autoheal.timer`, `systemctl list-timers slipcast-autoheal*`,
  `journalctl -u slipcast-autoheal.service -n 50`, and a safe manual dry run
  (`/home/eric/projects/slipcast/ops/autoheal.sh` while the app is healthy — it logs and
  exits without restarting anything).
- The state files (`data/.autoheal_state`, `data/.autoheal_paused`), the cap semantics, and
  how to clear a pause by hand (`rm data/.autoheal_paused`).
- The WSL2 caveat, matching `CLOUDFLARE_TUNNEL.md`'s existing note: systemd here runs
  inside WSL2, so the timer only fires while WSL2 is up.

### Task 10 — Tests
See "Testing & verification" below for the specific cases; add them to the existing files,
matching each file's fixture style (`_setup_tmp(tmp_path, monkeypatch)` in
`tests/test_polling.py` / `tests/test_database.py`, the `smtp` fixture in
`tests/test_notify.py`, direct handler calls + `_health_body()` in `tests/test_endpoints.py`).

### Task 11 — Version, changelog, README
- `app/__init__.py`: **read the current value first** (`1.11.0` at planning time) and bump
  the MINOR component (→ `1.12.0` if unchanged). Do not assume.
- `app/changelog.py`: new **top** entry, `"date": "2026-09-03"`, matching the version you
  just set. Prose style must match the 1.10.0/1.11.0 entries — user-facing impact,
  full sentences, no commit-log fragments. Suggested beats (one bullet each):
  thumbnail-download/conversion timeouts closing the last two ways a poll could hang
  forever; the new `/health/live` liveness endpoint and what makes it different from
  `/health`; the host autoheal timer that restarts a wedged container up to 3x an hour then
  emails instead of looping; automatic pruning of the globally oldest episodes when disk
  drops below `MIN_FREE_DISK_GB`, always with an email so the deletion is never silent;
  nightly database backups (7 kept) with a corruption check that emails on failure.
  `tests/test_changelog.py` already asserts the changelog and `__version__` agree — read it
  before writing so the entry satisfies whatever it checks.
- `README.md`:
  - Config table: add `MIN_FREE_DISK_GB` | `2` | row.
  - API Endpoints table: add the `/health/live` row (auth `None`), and amend the `/health`
    row to note it is the *full* report and that `/health/live` is what the Docker
    healthcheck and autoheal use.
  - Update the existing "Important notes" bullet about the Docker `HEALTHCHECK` — it now
    hits `/health/live`, and point at `ops/README.md` for actual restart-on-unhealthy.
  - Data Layout tree: add `backups/  # nightly VACUUM INTO snapshots, last 7 kept`.
  - New `### Database backup and restore` subsection under "How It Works" or "Data Layout":
    nightly at 03:00 into `DATA_DIR/backups/`, 7 retained, integrity-checked with an email
    on failure, and the **manual** restore procedure:
    ```bash
    docker compose stop app
    cp data/backups/episodes-YYYYMMDD-HHMMSS.db data/episodes.db
    rm -f data/episodes.db-wal data/episodes.db-shm   # stale WAL from the old DB
    docker compose start app
    ```
    with a line saying restore is deliberately manual — never automated against a
    possibly-still-corrupting condition.
  - A pointer to `ops/README.md` in the "Deploying" section.

---

## Data / model / API changes

**No schema migration.** No table or column changes; `db.init_db()` is untouched.

**New DB helpers** (`app/database.py`): `get_all_episodes_oldest_first()`, `backup_db()`,
`prune_backups()`, `integrity_check()`, `run_backup_job()`.

**New endpoint**

| Method | Path | Auth | Codes | Body |
|---|---|---|---|---|
| `GET` | `/health/live` | none | 200 / 503 | `{"status": "ok"\|"degraded", "version": str, "checks": {"scheduler": str, "polling": str}, "problems": [str]}` |

`GET /health` is unchanged in shape and semantics (`status`, `version`, `checks` with
`scheduler`/`polling`/`cookies`, `problems`; 200 or 503).

**New env var:** `MIN_FREE_DISK_GB` (int, default `2`). No other new env vars — backup hour
(3) and retention (7) are code constants.

**New on-disk artifacts:** `DATA_DIR/backups/episodes-YYYYMMDD-HHMMSS.db`,
`data/.autoheal_state`, `data/.autoheal_paused`. All under the already-gitignored `data/`.

**New notify keys:** `disk_prune`, `backup_failure` (in the existing `.alert_state` file).

---

## Testing & verification

Run from `/home/eric/projects/slipcast`: `.venv/bin/python -m pytest -q`
(do **not** use bare `python`/`pytest`).

| Brief AC | How it's proven |
|---|---|
| 1 (thumbnail timeouts) | `tests/test_polling.py`: stub `downloader.urllib.request.urlopen` to raise `TimeoutError` and assert `_download_thumbnail()` returns `False`, leaves no `.tmp` behind, and does not raise. Add a second test that stubs `urlopen` to yield bytes and `downloader.subprocess.run` to raise `subprocess.TimeoutExpired`, asserting the same graceful `False`. Plus a static check in the same test module (or by inspection) that both call sites pass a timeout. |
| 2 (`/health/live` ignores cookies/disk) | `tests/test_endpoints.py`: with a running fake scheduler, no channels, and `main.cookies_status` returning `{"present": False, "expired": True}`, assert `main.health_live().status_code == 200` **and** `main.health().status_code == 503`. Add a stale-poll test: `db.get_recent_poll_runs` returning a run finished long ago + `POLL_INTERVAL_HOURS=1` → `health_live()` is 503 with `"polling appears stalled"`. Add a scheduler-down test → 503. |
| 3 (`/health` unchanged) | The five existing `test_health_*` tests must pass **unmodified**. Do not edit them. If one breaks, the refactor changed behaviour — fix the code, not the test. |
| 4 (Docker healthcheck) | `grep -n "health/live" Dockerfile docker-compose.yml` shows both; `grep -c "8000/health\"" ...` shows no bare `/health` left in either healthcheck. |
| 5 (autoheal script) | Not unit-testable (host-level). Verify by: `bash -n ops/autoheal.sh` (syntax), `shellcheck ops/autoheal.sh` if available, `test -x ops/autoheal.sh`, and a **dry run** against the healthy running container — `./ops/autoheal.sh` should log "healthy" and exit 0 **without** restarting (confirm with `docker compose ps` uptime before/after). Then a code-review-level read of the cap logic. Record this explicitly in the PR description as code-review-verified, per the brief. |
| 6 (disk prune) | `tests/test_polling.py`: seed episodes across **two** channel_ids with interleaved `published` dates and real files on disk; monkeypatch `downloader.DATA_DIR` to `tmp_path`, `downloader.MIN_FREE_DISK_GB` to a value above the fake free space, and `downloader.shutil.disk_usage` with a stateful fake that reports low free space for the first N calls then enough. Assert: the deleted episodes are the globally oldest **regardless of channel**, their audio/thumbnail files are gone, `db.add_skip_video` recorded them, newer episodes survived, and a captured `notify.send_disk_prune_alert` was called with the pruned list. Add a no-op test: plenty of free disk → nothing deleted, no alert. Add a `poll_all()`-entry test asserting `_enforce_disk_floor` is invoked (monkeypatch it to append to a list) before channels are polled. |
| 7 (backup + retention) | `tests/test_database.py`: (a) seed 9 files named `episodes-2026090{1..9}-000000.db` in a monkeypatched `db.BACKUP_DIR`, run `prune_backups()`, assert exactly 7 remain and they are the 7 newest by name. (b) with `db.DB_PATH` on a real temp DB holding a few episodes, call `backup_db()`, assert the returned path exists and `sqlite3.connect(path).execute("SELECT COUNT(*) FROM episodes").fetchone()[0]` matches. |
| 8 (integrity check alerts) | `tests/test_database.py`: monkeypatch `db.integrity_check` to return `"*** in database main *** wrong # of entries in index"` and `db.notify.send_backup_failure_alert` to record calls; run `run_backup_job()` and assert the alert fired with the failing text. Plus a happy-path test asserting no alert when `integrity_check()` returns `"ok"`. Also `tests/test_notify.py`: add `send_disk_prune_alert` / `send_backup_failure_alert` cases to the existing `smtp` fixture pattern — sends when configured, debounced within cooldown, `force=True` bypasses, and each uses its own cooldown key (send one, assert the other still sends). |
| 9 (suite green) | `.venv/bin/python -m pytest -q` |
| 10 (version/changelog/README) | `.venv/bin/python -c "import app; print(app.__version__)"` matches the top changelog entry (`tests/test_changelog.py` enforces this); `grep -n "MIN_FREE_DISK_GB\|/health/live\|backups/" README.md`. |
| 11 (local deploy) | `docker compose build && docker compose up -d`, then wait for the healthcheck `start_period` (2m) and run: `docker compose ps` (expect `healthy`), `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health/live`, `curl -s http://localhost:8000/health | head -c 400`, and `docker compose logs --tail=50 app` for the startup lines. **Deploy is local-only** — build + `up -d` on this machine; this project does **not** use a Docker Hub tag/CI/pull flow. |

---

## Risks & watch-outs

- **Do not break the five existing `/health` tests.** The refactor must move the scheduler
  and polling blocks verbatim — the exact `checks` strings (`"running"`, `"starting up"`,
  `"no poll has ever completed"`) and problem strings are asserted on.
- **`_liveness()` must never consult cookies or disk.** This is the single most important
  correctness property in the change: if it does, the autoheal script restart-loops the
  container every 5 minutes on a cookie expiry until the cap trips.
- **`urlretrieve` has no `timeout` kwarg.** Passing one is a `TypeError` at runtime, not a
  static error — you must switch to `urlopen(..., timeout=...)` + `copyfileobj`.
- **`subprocess.run(timeout=...)` raises `TimeoutExpired` but the child may linger.** The
  existing broad `except Exception` handles the raise; that's acceptable here (ffmpeg on a
  thumbnail is short-lived) — do not add process-group killing.
- **Deleting for disk pressure is destructive and irreversible.** Re-check free space after
  *each* deletion so the loop stops at the minimum necessary. Never prune when
  `MIN_FREE_DISK_GB <= 0`. Make sure the alert fires even in the "nothing left to prune"
  case; a silent failure to free space is exactly the class of bug this pass exists to kill.
- **Tests must monkeypatch `downloader.DATA_DIR` and `downloader.shutil.disk_usage`, not
  the config module** — the module-level `from app.config import ...` binds names at import,
  so patching `config.MIN_FREE_DISK_GB` after import has no effect. Follow the existing
  `monkeypatch.setattr(downloader, "MAX_EPISODES_PER_CHANNEL", ...)` idiom.
- **Import order in `database.py`:** adding `from app import notify` at module top is safe
  today (notify imports only `app.config`), but verify with
  `.venv/bin/python -c "import app.main"` — a cycle would break the whole app at boot, not
  just a test.
- **`VACUUM INTO` refuses an existing destination file.** Handle the (unlikely)
  same-second collision rather than letting the job raise.
- **A stale `-wal`/`-shm` pair must be removed during restore** or SQLite may replay the old
  write-ahead log over the restored snapshot. The README restore steps must say so.
- **Ordering:** Task 6 (`/health/live`) must land before Task 8 (healthchecks) and Task 9
  (autoheal), or the container reports unhealthy against a 404 and the script restart-loops.
  Task 2 (config) before Task 5. Task 4 before Task 5. Task 3 before Tasks 5 and 7.
- **`set -e` in `ops/autoheal.sh` would abort on the expected `curl -f` failure** — the
  unhealthy branch would never run. Use `set -uo pipefail` only.
- **Never interpolate `.env` secrets into the script's text or logs** — export them and read
  from `os.environ` inside the python heredoc.
- **The pause marker must not silence the script.** Every tick while paused still logs, and
  re-emails at most once per `PAUSE_EMAIL_INTERVAL`. Auto-clearing on a healthy probe is the
  intended recovery path.
- **`slipcast-app-1` is the compose-derived container name** (project `name: slipcast`,
  service `app`). Keep it overridable via env in the script, and prefer
  `docker compose -f <project>/docker-compose.yml restart app` as the primary restart path.
- **Do not install or enable the systemd units** — out of scope; deliver the files and docs.

---

## Out of scope (do not build)

- Any external monitoring service (Healthchecks.io, Uptime Kuma) — rejected in favour of
  email-only via the existing SMTP config.
- Killing a wedged Python thread from inside the process — impossible in Python; the
  host-side restart is the only recovery, which is why the autoheal script exists.
- Actually installing/enabling the systemd timer on the host. Ship the versioned script,
  units, and install docs only.
- Group 2 (codec/bitrate settings, per-channel caps/retention, max-duration filter),
  Group 3 (feed tokens, combined feed, episode-level management, per-channel feed
  metadata), and Group 4 (channel_id-as-primary-key schema migration). Do not touch those
  code paths.
- Automatic restore from a backup, per-user or per-channel backup policy, or any UI surface
  for backups/autoheal (no dashboard changes in this pass).
