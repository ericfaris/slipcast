import base64
import ipaddress
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app import changelog
from app import database as db
from app import jobs
from app import notify
from app.safety import is_safe_media_name
from app.config import (
    AUDIO_DIR, ALERT_EMAIL, AUTH_CREDENTIALS, BASE_URL, COOKIES_FILE,
    POLL_CONCURRENCY, POLL_INTERVAL_HOURS, THUMBNAIL_DIR,
)
from app.downloader import (
    cookies_status, download_single, find_orphan_channels, poll_all,
    poll_channel, remove_channel_data, valid_cookie_file,
)
from app.feed import build_feed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

def _get_version() -> str:
    # Static package version is the source of truth (works inside Docker where
    # there's no git history). Append the short git sha when available locally.
    from app import __version__
    version = __version__
    try:
        import subprocess
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if sha:
            version = f"{version}+{sha}"
    except Exception:
        pass
    return version

VERSION = _get_version()

# When this process started — for a locally rebuilt/restarted container this is
# effectively "deployed at". Shown in the About dialog as "running since", and
# used by /health as a grace period before "no poll has ever run" counts as
# degraded (a freshly started container hasn't had a chance to poll yet).
_STARTED_MONOTONIC = time.monotonic()
STARTED_AT = datetime.now(timezone.utc).isoformat()

_scheduler: BackgroundScheduler | None = None

# Bounds how many channels poll concurrently from "poll all"/"poll selected" —
# previously one unbounded thread per channel, which could also fire dozens of
# concurrent yt-dlp processes. submit() returns immediately, so the endpoints
# that use this still respond right away.
_poll_executor = ThreadPoolExecutor(max_workers=POLL_CONCURRENCY, thread_name_prefix="poll")

# Paths that podcast apps access — no auth required. Matched as prefixes, so
# "/health" also covers "/health/live"; keep it that way — the Docker
# HEALTHCHECK and ops/autoheal.sh both curl /health/live with no credentials,
# and tightening this to an exact match would silently mark the container
# unhealthy and (with the autoheal timer running) restart-loop it.
_PUBLIC_PREFIXES = ("/feed/", "/audio/", "/thumbnails/", "/static/", "/health", "/favicon.ico")

# Rate limiting: max failed auth attempts per IP within the window
_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW = 60  # seconds
_failed_attempts: dict[str, list[float]] = defaultdict(list)
_rate_limit_lock = threading.Lock()
_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_COOKIE_BYTES = 5 * 1024 * 1024  # 5 MB


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    db.init_db()

    _scheduler = BackgroundScheduler()
    # coalesce + a generous misfire grace so a slow run doesn't permanently
    # wedge the schedule; max_instances=1 still prevents overlapping polls.
    _scheduler.add_job(poll_all, "interval", hours=POLL_INTERVAL_HOURS,
                       coalesce=True, misfire_grace_time=3600, max_instances=1)
    _scheduler.add_job(_prune_rate_limit_table, "interval", hours=1)
    # Nightly DB snapshot + corruption check. 03:00 is a quiet hour, and a fixed
    # hour is far easier to reason about ("last night's backup") than a 24h
    # interval anchored to whenever the container last restarted. VACUUM INTO
    # reads a live WAL database safely, so overlapping a poll is harmless.
    _scheduler.add_job(db.run_backup_job, "cron", hour=3, coalesce=True,
                       misfire_grace_time=3600, max_instances=1)
    _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    _scheduler.start()

    channels = db.get_channels()
    if channels:
        logger.info("Running initial poll for %d channel(s)", len(channels))
        threading.Thread(target=poll_all, daemon=True).start()
    else:
        logger.warning("No channels configured — add one at %s", BASE_URL)

    # Report-only: log any channel data (episodes/files) with no owning row,
    # so it's at least visible in the logs even before anyone opens the
    # dashboard. Never auto-delete at startup — this is the user's data; the
    # dashboard's orphan section and /channels/remove-orphan are how it's
    # actually cleaned up, on purpose, by a person.
    try:
        orphans = find_orphan_channels()
        for o in orphans:
            logger.warning(
                "Orphaned channel data: %s (%s) — %d episode(s), %.1f MB on disk, "
                "no channels/unsubscribed_channels row owns it. Remove it from the "
                "dashboard's orphaned-data section if you don't want it kept.",
                o["channel_id"], o["channel_name"], o["episode_count"], o["bytes"] / 1_048_576,
            )
        if orphans:
            logger.warning("Found %d orphaned channel(s) at startup", len(orphans))
    except Exception:  # noqa: BLE001 — reconciliation must never block startup
        logger.exception("Orphan reconciliation failed")

    yield

    _scheduler.shutdown()
    _poll_executor.shutdown(wait=False)


app = FastAPI(title="Slipcast", lifespan=lifespan)


def _is_trusted_proxy(ip: str) -> bool:
    """Only believe forwarded-for headers from a loopback/private peer.

    Behind the Cloudflare tunnel the direct peer is cloudflared on the Docker
    bridge (loopback/RFC1918), and the real client is in CF-Connecting-IP. If a
    request arrives from a public peer we must NOT trust client-supplied
    forwarding headers, or an attacker could spoof IPs to evade the rate limiter.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private


def _client_ip(request: Request) -> str:
    """Return the real client IP, accounting for Cloudflare and other proxies."""
    peer = request.client.host if request.client else None
    if peer and _is_trusted_proxy(peer):
        for header in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
            value = request.headers.get(header)
            if value:
                return value.split(",")[0].strip()
    return peer or "unknown"


def _client_ip_unverified(request: Request) -> bool:
    """True if _client_ip() fell back to the trusted-proxy peer itself.

    That happens when a request reaches the app without going through
    cloudflared (e.g. direct LAN/Tailscale access) and carries none of the
    forwarding headers, so the peer (typically the Docker bridge gateway) is
    used as-is. Multiple distinct real clients hitting the app this way all
    collapse into the same rate-limit bucket and log identity, which can look
    like one repeat offender when it's actually several. This flag lets the
    log line say so without changing the bucketing key itself.
    """
    peer = request.client.host if request.client else None
    if not (peer and _is_trusted_proxy(peer)):
        return False
    return not any(request.headers.get(h) for h in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"))


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is rate-limited (too many recent failures)."""
    now = time.monotonic()
    with _rate_limit_lock:
        _failed_attempts[ip] = [t for t in _failed_attempts[ip] if now - t < _RATE_LIMIT_WINDOW]
        return len(_failed_attempts[ip]) >= _RATE_LIMIT_MAX


def _record_failure(ip: str, request: Request | None = None):
    now = time.monotonic()
    with _rate_limit_lock:
        _failed_attempts[ip].append(now)
    if request is not None and _client_ip_unverified(request):
        port = request.client.port if request.client else "?"
        logger.warning(
            "Failed auth attempt from %s (direct/unverified, peer port %s — "
            "may be one of several distinct clients bypassing cloudflared)",
            ip, port,
        )
    else:
        logger.warning("Failed auth attempt from %s", ip)


def _clear_failures(ip: str):
    with _rate_limit_lock:
        _failed_attempts.pop(ip, None)


def _prune_rate_limit_table():
    """Remove IPs with no recent failures to prevent unbounded memory growth."""
    now = time.monotonic()
    with _rate_limit_lock:
        stale = [ip for ip, attempts in _failed_attempts.items()
                 if all(now - t >= _RATE_LIMIT_WINDOW for t in attempts)]
        for ip in stale:
            del _failed_attempts[ip]


def _on_job_error(event):
    """Make APScheduler job crashes loud instead of silent.

    A scheduled job that raises otherwise vanishes with no log line — this is
    how polling could stop for weeks unnoticed.
    """
    logger.error("Scheduled job %s raised an exception", event.job_id,
                 exc_info=event.exception)


# GET endpoints that mutate state ("shareable links"). They bypass the usual
# POST-only CSRF gate, so they get the same Origin/Referer check.
_MUTATING_GET_PATHS = frozenset({"/add", "/download"})


def _is_state_changing(request: Request) -> bool:
    return request.method == "POST" or request.url.path in _MUTATING_GET_PATHS


def _csrf_ok(request: Request) -> bool:
    """Validate Origin/Referer against Host for state-changing requests.

    - POST (UI form submissions always carry Origin/Referer): fail **closed** —
      a missing header is rejected.
    - Mutating GET shareable links: allow a missing Origin/Referer (top-level
      navigation, bookmarks, address-bar) but reject a *mismatched* one, which
      blocks the embedded cross-site request (`<img src=".../add?...">`) attack.
    """
    host = request.headers.get("Host", "")
    for header in ("Origin", "Referer"):
        value = request.headers.get(header, "")
        if value and value != "null":
            return urlparse(value).netloc == host
    # No usable Origin/Referer: only allowed for non-POST (GET shareable links).
    return request.method != "POST"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    is_public = request.url.path.startswith(_PUBLIC_PREFIXES)
    if not AUTH_CREDENTIALS:
        if not is_public and _is_state_changing(request) and not _csrf_ok(request):
            return Response(status_code=403, content="CSRF check failed")
        return await call_next(request)

    if is_public:
        return await call_next(request)

    ip = _client_ip(request)

    if _check_rate_limit(ip):
        logger.warning("Rate-limited auth attempt from %s", ip)
        return Response(status_code=429, content="Too many failed login attempts. Try again later.")

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            username, password = decoded.split(":", 1)
            if any(
                secrets.compare_digest(username.encode(), u.encode()) and
                secrets.compare_digest(password.encode(), p.encode())
                for u, p in AUTH_CREDENTIALS
            ):
                _clear_failures(ip)
                if _is_state_changing(request) and not _csrf_ok(request):
                    return Response(status_code=403, content="CSRF check failed")
                return await call_next(request)
        except Exception:
            pass
    _record_failure(ip, request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Slipcast"'})

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_DIR, exist_ok=True)
app.mount("/audio", StaticFiles(directory=AUDIO_DIR), name="audio")
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_DIR), name="thumbnails")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(STATIC_DIR, "favicon.ico"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_poll_label() -> str:
    """Return a human-readable 'next poll in X' string, or empty string if unknown."""
    if _scheduler is None:
        return ""
    for job in _scheduler.get_jobs():
        if job.func is poll_all and job.next_run_time:
            now = datetime.now(timezone.utc)
            delta = job.next_run_time - now
            total = int(delta.total_seconds())
            if total <= 0:
                return "polling now"
            h, rem = divmod(total, 3600)
            m = rem // 60
            if h:
                return f"next poll in {h}h {m:02d}m"
            return f"next poll in {m}m"
    return ""


def _next_poll_at() -> str | None:
    """ISO timestamp of the next scheduled poll_all run, if known."""
    if _scheduler is None:
        return None
    for job in _scheduler.get_jobs():
        if job.func is poll_all and job.next_run_time:
            return job.next_run_time.isoformat()
    return None


def _run_dict(r) -> dict:
    return {
        "channel_id": r["channel_id"],
        "channel_name": r["channel_name"] or r["url"] or "Unknown",
        "status": r["status"],
        "downloaded": r["downloaded"],
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "error": r["error"],
    }


def _polling_state() -> dict:
    runs = db.get_recent_poll_runs(25)
    last_at = runs[0]["finished_at"] or runs[0]["started_at"] if runs else None
    return {
        "interval_hours": POLL_INTERVAL_HOURS,
        "next_poll_at": _next_poll_at(),
        "next_poll": _next_poll_label(),
        "last_poll_at": last_at,
        "runs": [_run_dict(r) for r in runs],
    }


def _feed_url(channel_id: str) -> str:
    return f"{BASE_URL}/feed/{channel_id}.xml"


def _channel_thumb_exists(channel_id: str) -> bool:
    return bool(channel_id) and os.path.exists(os.path.join(THUMBNAIL_DIR, channel_id, "channel.jpg"))


def _thumb_url(channel_id: str) -> str | None:
    # Relative (same-origin) so it loads under any host and satisfies the
    # img-src 'self' CSP. Only the feed URL (copied into podcast apps) needs
    # to be absolute; in-browser <img>/<audio> assets must not hard-code
    # BASE_URL or they break when the UI is reached on a different host.
    return f"/thumbnails/{channel_id}/channel.jpg" if _channel_thumb_exists(channel_id) else None


def _episode_count(channel_id: str | None) -> int:
    return len(db.get_episodes(channel_id)) if channel_id else 0


def _total_episodes() -> int:
    # A single GROUP BY instead of loading every episode row per channel
    # (twice, before/after) on every download job.
    return sum(db.episode_counts().values())


def _is_valid_channel_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


# ---------------------------------------------------------------------------
# Background job wrappers — record progress so the UI can show it live
# ---------------------------------------------------------------------------

def _run_poll(url: str, label: str | None = None):
    rurl = url.rstrip("/")

    def lookup():
        ch = next((c for c in db.get_channels() if c["url"] == rurl), None)
        if not ch:
            return None, None
        return (ch["channel_name"] or None), (ch["channel_id"] or None)

    pre_name, pre_cid = lookup()
    label = label or pre_name or rurl
    before = _episode_count(pre_cid)
    jid = jobs.start("poll", label)
    try:
        summary = poll_channel(url)
        if summary and summary.get("already_polling"):
            jobs.finish(jid, "success", f"{label}: already polling — skipped")
            return
        post_name, post_cid = lookup()
        label = post_name or label
        added = _episode_count(post_cid) - before
        if added > 0:
            jobs.finish(jid, "success", f"{label}: {added} new episode(s)")
        else:
            jobs.finish(jid, "success", f"{label}: no new episodes")
    except Exception as exc:  # poll_channel is defensive, but never let a job hang
        logger.exception("Poll job failed for %s", rurl)
        jobs.finish(jid, "error", f"{label}: {exc}")


def _run_download(url: str, subscribe: bool):
    jid = jobs.start("download", url)
    before = _total_episodes()
    try:
        download_single(url, subscribe)
        if _total_episodes() > before:
            jobs.finish(jid, "success", "Episode downloaded")
        else:
            jobs.finish(jid, "error", "Nothing downloaded — video may be unavailable, private, or already saved")
    except Exception as exc:
        logger.exception("Download job failed for %s", url)
        jobs.finish(jid, "error", str(exc))


# ---------------------------------------------------------------------------
# Management UI shell — content is rendered client-side from /api/state
# ---------------------------------------------------------------------------

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light dark">
    <link rel="icon" href="/static/favicon.ico" sizes="48x48">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <meta name="theme-color" content="#0e1020">
    <link rel="stylesheet" href="/static/styles.css">
    <title>Slipcast</title>
</head>
<body>
    <a class="skip-link" href="#main">Skip to content</a>
    <header class="appbar">
        <div class="appbar-inner">
            <div class="brand">
                <svg class="brand-mark" viewBox="0 0 48 48" aria-hidden="true" width="26" height="26">
                    <defs>
                        <linearGradient id="sc-grad" x1="0" y1="0" x2="1" y2="1">
                            <stop offset="0" stop-color="#F40A02"/>
                            <stop offset="1" stop-color="#5415A0"/>
                        </linearGradient>
                    </defs>
                    <path d="M8 13.5c0-3.5 3.8-5.7 6.8-3.9l15 9.1c2.9 1.8 2.9 6 0 7.8l-15 9.1c-3 1.8-6.8-.4-6.8-3.9V13.5z" fill="url(#sc-grad)"/>
                    <path d="M34 17a10.5 10.5 0 0 1 0 14" fill="none" stroke="url(#sc-grad)" stroke-width="3.6" stroke-linecap="round"/>
                    <path d="M39.5 12.5a17.5 17.5 0 0 1 0 23" fill="none" stroke="url(#sc-grad)" stroke-width="3.6" stroke-linecap="round"/>
                </svg>
                <span class="brand-name">Slipcast</span>
            </div>
            <div class="appbar-actions">
                <span id="activity" class="activity" hidden><span class="spinner"></span><span id="activity-text">Working…</span></span>
                <button id="settings-btn" class="btn btn-icon" type="button" aria-label="Settings" title="Settings">
                    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="3"/>
                        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
                    </svg>
                </button>
            </div>
        </div>
    </header>

    <div id="cookie-banner" class="banner" hidden></div>

    <main id="main" class="wrap">
        <section class="section" aria-labelledby="poll-h">
            <div class="poll-card card" id="poll-card" hidden>
                <div class="poll-gauge" id="poll-gauge" role="img" aria-label="Time until next poll">
                    <svg viewBox="0 0 84 84" width="84" height="84" aria-hidden="true">
                        <circle class="poll-ring-bg" cx="42" cy="42" r="37" fill="none" stroke-width="6"/>
                        <circle class="poll-ring-fg" id="poll-ring" cx="42" cy="42" r="37" fill="none"
                                stroke-width="6" stroke-linecap="round" transform="rotate(-90 42 42)"/>
                    </svg>
                    <div class="poll-gauge-label">
                        <span class="poll-gauge-num" id="poll-countdown">—</span>
                        <span class="poll-gauge-cap">until next</span>
                    </div>
                </div>
                <div class="poll-info">
                    <div class="poll-title-row">
                        <h2 id="poll-h">Polling</h2>
                        <span class="pill" id="poll-interval"></span>
                        <span class="poll-health" id="poll-health"></span>
                    </div>
                    <div class="poll-facts" id="poll-facts"></div>
                    <div class="poll-runs" id="poll-runs"></div>
                </div>
                <button class="btn btn-ghost btn-sm poll-now" id="poll-now" type="button">Poll all now</button>
            </div>
        </section>

        <section class="section" aria-labelledby="subs-h">
            <div class="section-head">
                <h2 id="subs-h">Subscribed channels <span id="subs-count" class="count-pill"></span></h2>
            </div>

            <form id="add-form" class="inline-form" autocomplete="off">
                <label class="visually-hidden" for="add-url">YouTube channel URL</label>
                <input id="add-url" name="url" type="text" inputmode="url"
                       placeholder="Paste a YouTube channel URL — e.g. https://youtube.com/@channel" required>
                <button class="btn btn-primary" type="submit">Add channel</button>
            </form>

            <div class="toolbar" id="subs-toolbar" hidden>
                <div class="search">
                    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="11" cy="11" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="m20 20-3.2-3.2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                    <input id="subs-search" type="search" placeholder="Search channels" aria-label="Search subscribed channels">
                </div>
                <label class="sort">Sort
                    <select id="subs-sort" aria-label="Sort channels">
                        <option value="added">Recently added</option>
                        <option value="name">Name (A–Z)</option>
                        <option value="episodes">Most episodes</option>
                    </select>
                </label>
            </div>

            <div id="bulk-bar" class="bulk-bar" hidden>
                <span id="bulk-count"></span>
                <div class="bulk-actions">
                    <button class="btn btn-ghost" type="button" id="bulk-poll">Poll selected</button>
                    <button class="btn btn-danger-ghost" type="button" id="bulk-remove">Remove selected</button>
                    <button class="btn btn-text" type="button" id="bulk-clear">Clear</button>
                </div>
            </div>

            <div id="subs-grid" class="grid"></div>
        </section>

        <section class="section" aria-labelledby="oneoff-h">
            <div class="section-head">
                <h2 id="oneoff-h">One-off downloads <span id="oneoff-count" class="count-pill"></span></h2>
            </div>
            <form id="dl-form" class="inline-form" autocomplete="off">
                <label class="visually-hidden" for="dl-url">YouTube video URL</label>
                <input id="dl-url" name="url" type="text" inputmode="url"
                       placeholder="Paste a video URL — e.g. https://youtu.be/abc123" required>
                <label class="check">
                    <input type="checkbox" id="dl-subscribe"> Also subscribe
                </label>
                <button class="btn btn-primary" type="submit">Download</button>
            </form>
            <div id="oneoff-grid" class="grid"></div>
        </section>

        <section class="section" aria-labelledby="orphans-h" id="orphans-section" hidden>
            <div class="section-head">
                <h2 id="orphans-h">Orphaned data <span id="orphans-count" class="count-pill"></span></h2>
            </div>
            <p class="share-name">Episodes and files left behind by a removed channel — safe to delete if you don't recognize them.</p>
            <div id="orphans-grid" class="grid"></div>
        </section>

        <section class="section" aria-labelledby="cookies-h">
            <div class="section-head"><h2 id="cookies-h">YouTube cookies</h2></div>
            <div class="card cookies-card">
                <div id="cookies-status" class="cookies-status"></div>
                <form id="cookies-form" class="inline-form" enctype="multipart/form-data">
                    <input id="cookies-file" name="file" type="file" accept=".txt" required>
                    <button class="btn btn-primary" type="submit">Upload cookies.txt</button>
                </form>
                <div class="cookies-email" id="cookies-email"></div>
                <details class="howto">
                    <summary>How to export cookies.txt</summary>
                    <ol>
                        <li>Install the <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" target="_blank" rel="noopener">Get cookies.txt LOCALLY</a> extension (Chrome) or <a href="https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/" target="_blank" rel="noopener">cookies.txt</a> (Firefox).</li>
                        <li>Open <a href="https://www.youtube.com" target="_blank" rel="noopener">youtube.com</a> while logged in.</li>
                        <li>Click the extension and export as <strong>cookies.txt</strong>.</li>
                        <li>Upload the file above. Re-upload every few weeks when downloads start failing.</li>
                    </ol>
                </details>
            </div>
        </section>
    </main>

    <div id="toaster" class="toaster" aria-live="polite" aria-atomic="false"></div>

    <!-- Feed share dialog -->
    <div id="share-modal" class="modal" hidden>
        <div class="modal-backdrop" data-close></div>
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="share-title">
            <button class="modal-close" type="button" data-close aria-label="Close">&times;</button>
            <h3 id="share-title">Share feed</h3>
            <p id="share-name" class="share-name"></p>
            <div id="share-qr" class="share-qr"></div>
            <div class="share-url">
                <input id="share-url-input" type="text" readonly aria-label="Feed URL">
                <button class="btn btn-ghost" type="button" id="share-copy">Copy</button>
            </div>
            <div class="share-apps">
                <a id="share-pocketcasts" class="btn btn-ghost" target="_blank" rel="noopener">Pocket Casts</a>
                <a id="share-apple" class="btn btn-ghost">Apple Podcasts</a>
            </div>
        </div>
    </div>

    <!-- Episodes list dialog -->
    <div id="ep-modal" class="modal" hidden>
        <div class="modal-backdrop" data-close></div>
        <div class="modal-card modal-wide" role="dialog" aria-modal="true" aria-labelledby="ep-title">
            <button class="modal-close" type="button" data-close aria-label="Close">&times;</button>
            <h3 id="ep-title">Episodes</h3>
            <p id="ep-sub" class="share-name"></p>
            <div id="ep-list" class="ep-list"></div>
        </div>
    </div>

    <!-- Settings / About dialog -->
    <div id="settings-modal" class="modal" hidden>
        <div class="modal-backdrop" data-close></div>
        <div class="modal-card modal-wide" role="dialog" aria-modal="true" aria-labelledby="settings-title">
            <button class="modal-close" type="button" data-close aria-label="Close">&times;</button>
            <h3 id="settings-title">About Slipcast</h3>
            <p class="share-name">Self-hosted YouTube-to-podcast server</p>
            <dl class="about-meta">
                <div><dt>Version</dt><dd id="about-version">—</dd></div>
                <div><dt>Running since</dt><dd id="about-started">—</dd></div>
            </dl>
            <h4 class="about-h">Changelog</h4>
            <div id="changelog-list" class="changelog"></div>
        </div>
    </div>

    <noscript><p style="padding:24px;text-align:center">Slipcast's dashboard needs JavaScript enabled.</p></noscript>
    <script src="/static/vendor/qrcode.min.js"></script>
    <script src="/static/app.js"></script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        content=_PAGE,
        headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"},
    )


# ---------------------------------------------------------------------------
# JSON API consumed by the dashboard
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    last_runs = db.get_last_poll_run_per_channel()
    counts = db.episode_counts()

    def _last_poll(cid):
        r = last_runs.get(cid) if cid else None
        if not r:
            return None
        return {
            "status": r["status"],
            "at": r["finished_at"] or r["started_at"],
            "downloaded": r["downloaded"],
            "error": r["error"],
        }

    channels = []
    for ch in db.get_channels():
        cid = ch["channel_id"]
        channels.append({
            "url": ch["url"],
            "channel_id": cid,
            "name": ch["channel_name"] or ch["url"],
            "episodes": counts.get(cid, 0) if cid else 0,
            "feed_url": _feed_url(cid) if cid else None,
            "thumbnail": _thumb_url(cid) if cid else None,
            "added_at": ch["added_at"],
            "last_poll": _last_poll(cid),
        })

    unsubscribed = []
    for ch in db.get_unsubscribed_channels():
        cid = ch["channel_id"]
        unsubscribed.append({
            "channel_id": cid,
            "name": ch["channel_name"] or cid,
            "episodes": counts.get(cid, 0),
            "feed_url": _feed_url(cid),
            "thumbnail": _thumb_url(cid),
        })

    orphans = []
    try:
        orphans = find_orphan_channels()
    except Exception:  # noqa: BLE001 — orphan listing must never break the dashboard
        logger.exception("Failed to list orphaned channels")

    return JSONResponse({
        "channels": channels,
        "unsubscribed": unsubscribed,
        "orphans": orphans,
        "cookies": cookies_status(),
        "email": {"configured": notify._smtp_configured(), "address": ALERT_EMAIL},
        "next_poll": _next_poll_label(),
        "polling": _polling_state(),
        "jobs": jobs.snapshot(),
        "version": VERSION,
    })


@app.get("/api/changelog")
def api_changelog():
    return JSONResponse({
        "version": VERSION,
        "started_at": STARTED_AT,
        "entries": changelog.CHANGELOG,
    })


@app.get("/api/channels/{channel_id}/episodes")
def api_channel_episodes(channel_id: str):
    if not _CHANNEL_ID_RE.match(channel_id):
        raise HTTPException(status_code=400, detail="Invalid channel ID")
    episodes = []
    for ep in db.get_episodes(channel_id):
        # Validate the stored filenames before interpolating them into a path
        # (defense in depth — see app/safety.py). A bad value yields a null
        # URL rather than a traversal payload.
        filename, thumb = ep["filename"], ep["thumbnail"]
        if not is_safe_media_name(filename):
            logger.warning("Skipping unsafe audio filename for %s: %r", channel_id, filename)
        episodes.append({
            "id": ep["id"],
            "title": ep["title"],
            "published": ep["published"],
            "added_at": ep["created_at"],
            "duration": ep["duration"],
            "filesize": ep["filesize"],
            # Relative (same-origin) — these feed in-browser <audio>/<img> in
            # the episode modal, so they must work under any host and satisfy
            # the default-src/img-src 'self' CSP. (The RSS feed uses absolute
            # BASE_URL URLs separately, in app/feed.py.)
            "audio_url": f"/audio/{channel_id}/{filename}" if is_safe_media_name(filename) else None,
            "thumbnail": f"/thumbnails/{channel_id}/{thumb}" if is_safe_media_name(thumb) else None,
        })
    return JSONResponse({"channel_id": channel_id, "episodes": episodes})


def _ok(message: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": True, "message": message, **extra})


# ---------------------------------------------------------------------------
# Mutating actions
# ---------------------------------------------------------------------------

@app.get("/download")
def download_via_link(url: str, subscribe: bool = False):
    """Shareable link — clicking it downloads a specific video."""
    threading.Thread(target=_run_download, args=[url, subscribe], daemon=True).start()
    return RedirectResponse("/", status_code=302)


@app.post("/episodes/download")
def download_episode(url: str = Form(...), subscribe: bool = Form(False)):
    if not _is_valid_channel_url(url):
        raise HTTPException(status_code=400, detail="Enter a valid http(s) video URL")
    threading.Thread(target=_run_download, args=[url, subscribe], daemon=True).start()
    return _ok("Download started — this can take a minute")


@app.get("/add")
def add_via_link(channel: str):
    """Shareable link — clicking it adds the channel and redirects to the UI."""
    db.add_channel(channel.rstrip("/"))
    threading.Thread(target=_run_poll, args=[channel], daemon=True).start()
    return RedirectResponse("/", status_code=302)


@app.post("/channels/add")
def add_channel(url: str = Form(...)):
    if not _is_valid_channel_url(url):
        raise HTTPException(status_code=400, detail="Enter a valid http(s) channel URL")
    db.add_channel(url.rstrip("/"))
    threading.Thread(target=_run_poll, args=[url], daemon=True).start()
    return _ok("Channel added — fetching episodes")


@app.post("/channels/subscribe")
def subscribe_channel(channel_id: str = Form(...), channel_name: str = Form(...)):
    if not _CHANNEL_ID_RE.match(channel_id):
        raise HTTPException(status_code=400, detail="Invalid channel ID")
    channel_page_url = f"https://www.youtube.com/channel/{channel_id}"
    db.add_channel(channel_page_url)
    db.update_channel_meta(channel_page_url, channel_id, channel_name)
    db.remove_unsubscribed_channel(channel_id)
    threading.Thread(target=_run_poll, args=[channel_page_url, channel_name], daemon=True).start()
    return _ok(f"Subscribed to {channel_name}")


def _normalize_channel_url(url: str) -> str:
    """Loose comparison key for matching channel URLs across variants (case,
    trailing slash, tracking query string) that all denote the same channel."""
    p = urlparse(url.rstrip("/"))
    return f"{p.netloc.lower()}{p.path.rstrip('/').lower()}"


def _resolve_channel_id_for_removal(rurl: str) -> str | None:
    """Best-effort channel_id lookup for a channels row about to be deleted.

    An exact URL match (the old behavior) misses variants — different case,
    a trailing slash, a tracking query string — and channels removed before
    update_channel_meta ever populated channel_id. Without a resolved id, the
    row disappears from the `channels` table but its episodes, skip_videos,
    and on-disk audio/thumbnails are never cleaned up and become invisible
    orphans (see find_orphan_channels / the startup reconciler), which is
    exactly what happened in production. A normalized comparison across all
    channel rows catches the variant case; if that still fails, the orphan
    reconciler is the safety net that eventually surfaces what's left behind.
    """
    channel_id = db.get_channel_id_for_url(rurl)
    if channel_id:
        return channel_id
    norm = _normalize_channel_url(rurl)
    for ch in db.get_channels():
        if ch["channel_id"] and _normalize_channel_url(ch["url"]) == norm:
            return ch["channel_id"]
    return None


def _remove_one(url: str):
    rurl = url.rstrip("/")
    channel_id = _resolve_channel_id_for_removal(rurl)
    db.remove_channel(rurl)
    if channel_id:
        db.delete_episodes_for_channel(channel_id)
        db.delete_skip_videos_for_channel(channel_id)
        remove_channel_data(channel_id)


@app.post("/channels/remove")
def remove_channel(url: str = Form(...)):
    _remove_one(url)
    return _ok("Channel removed")


@app.post("/channels/remove-unsubscribed")
def remove_unsubscribed_channel_endpoint(channel_id: str = Form(...)):
    """Remove a one-off (unsubscribed) channel's row, episodes, and files.

    Previously the only way to get rid of an unsubscribed channel was to
    Subscribe to it — there was no way to delete it, so its audio grew
    unbounded (see also: the _prune_channel call added to download_single).
    """
    if not _CHANNEL_ID_RE.match(channel_id):
        raise HTTPException(status_code=400, detail="Invalid channel ID")
    db.remove_unsubscribed_channel(channel_id)
    db.delete_episodes_for_channel(channel_id)
    db.delete_skip_videos_for_channel(channel_id)
    remove_channel_data(channel_id)
    return _ok("Channel removed")


@app.post("/channels/remove-orphan")
def remove_orphan_channel(channel_id: str = Form(...)):
    """Delete episodes/skip_videos/files for a channel_id that find_orphan_channels
    reported as owned by no `channels`/`unsubscribed_channels` row."""
    if not _CHANNEL_ID_RE.match(channel_id):
        raise HTTPException(status_code=400, detail="Invalid channel ID")
    db.delete_episodes_for_channel(channel_id)
    db.delete_skip_videos_for_channel(channel_id)
    remove_channel_data(channel_id)
    return _ok("Orphaned data removed")


@app.post("/channels/remove-bulk")
async def remove_channels_bulk(request: Request):
    data = await request.json()
    urls = [u for u in data.get("urls", []) if isinstance(u, str)]
    for u in urls:
        _remove_one(u)
    return _ok(f"Removed {len(urls)} channel(s)")


@app.post("/channels/poll")
def poll_now(url: str = Form(...)):
    threading.Thread(target=_run_poll, args=[url], daemon=True).start()
    return _ok("Polling channel")


@app.post("/channels/poll-bulk")
async def poll_channels_bulk(request: Request):
    data = await request.json()
    urls = [u for u in data.get("urls", []) if isinstance(u, str)]
    # Bounded worker pool instead of a thread per channel — submit() returns
    # immediately (it only queues the work), so the endpoint still responds
    # right away; poll_channel's own per-channel lock covers the rest.
    for u in urls:
        _poll_executor.submit(_run_poll, u)
    return _ok(f"Polling {len(urls)} channel(s)")


@app.post("/channels/poll-all")
def poll_all_now():
    channels = db.get_channels()
    for ch in channels:
        _poll_executor.submit(_run_poll, ch["url"])
    return _ok(f"Polling {len(channels)} channel(s)")


@app.post("/auth/cookies")
async def upload_cookies(file: UploadFile = File(...)):
    if not COOKIES_FILE:
        raise HTTPException(status_code=500, detail="COOKIES_FILE env var not set")
    content = await file.read(_MAX_COOKIE_BYTES + 1)
    if len(content) > _MAX_COOKIE_BYTES:
        raise HTTPException(status_code=413, detail="Cookie file too large (max 5 MB)")
    os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
    # Validate before overwriting the existing (possibly working) file so a
    # bad upload can't silently break every channel poll.
    tmp_path = COOKIES_FILE + ".upload"
    with open(tmp_path, "wb") as f:
        f.write(content)
    if not valid_cookie_file(tmp_path):
        os.remove(tmp_path)
        raise HTTPException(
            status_code=400,
            detail="Not a valid Netscape-format cookies.txt (file is empty or malformed).",
        )
    os.replace(tmp_path, COOKIES_FILE)
    logger.info("Cookies file updated (%d bytes)", len(content))
    return _ok("Cookies updated — downloads enabled")


@app.post("/auth/test-email")
def test_email():
    if not notify._smtp_configured():
        raise HTTPException(
            status_code=400,
            detail="Email alerts not configured — set SMTP_HOST/SMTP_USER/SMTP_PASS in .env",
        )
    if notify.send_cookie_alert(force=True):
        return _ok("Test email sent")
    raise HTTPException(status_code=502, detail="Failed to send test email — check server logs")


# ---------------------------------------------------------------------------
# Feed endpoints
# ---------------------------------------------------------------------------

@app.get("/feed/{channel_id}.xml", response_class=Response)
def get_feed(channel_id: str):
    rss = build_feed(channel_id)
    if not rss:
        raise HTTPException(status_code=404, detail="Channel not found or no episodes yet")
    return Response(content=rss, media_type="application/rss+xml")


def _seconds_since_last_poll() -> float | None:
    """Seconds since the most recent poll_runs row finished (or started, if it
    never finished), or None if no run has ever been recorded."""
    runs = db.get_recent_poll_runs(1)
    if not runs:
        return None
    ts = runs[0]["finished_at"] or runs[0]["started_at"]
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _liveness(checks: dict[str, str]) -> list[str]:
    """Restart-fixable health only: scheduler running + polling not stalled.

    Deliberately excludes cookie validity and disk space — a restart cannot fix
    either, and the host autoheal script (ops/autoheal.sh) restarts off this
    signal, so including them would turn a routine cookie expiry into a restart
    loop every five minutes until the cap trips. Anything added here must be a
    condition where "restart the container" is a plausible remedy.

    Fills `checks` in place and returns the list of problems found.
    """
    problems: list[str] = []

    scheduler_running = _scheduler is not None and _scheduler.running
    checks["scheduler"] = "running" if scheduler_running else "not running"
    if not scheduler_running:
        problems.append("scheduler is not running")

    channels = db.get_channels()
    # A poll finishing later than ~3x its own interval means it's stuck, not
    # just running a bit long (matches the coalesce/misfire tolerance the
    # scheduler itself is configured with — see lifespan).
    stale_after = POLL_INTERVAL_HOURS * 3 * 3600
    uptime = time.monotonic() - _STARTED_MONOTONIC
    last_poll_age = _seconds_since_last_poll()
    if not channels:
        checks["polling"] = "no channels configured"
    elif last_poll_age is None:
        # Never recorded a run — only a problem once the process has been up
        # long enough that the initial poll should have finished. A container
        # that just started must not report degraded before it's had a chance.
        if uptime > stale_after:
            checks["polling"] = "no poll has ever completed"
            problems.append("no poll has completed since startup")
        else:
            checks["polling"] = "starting up"
    elif last_poll_age > stale_after:
        checks["polling"] = f"stale — last run {int(last_poll_age // 3600)}h ago"
        problems.append("polling appears stalled")
    else:
        checks["polling"] = "ok"

    return problems


@app.get("/health")
def health():
    """Report actual health, not just "the process is up".

    The old unconditional {"status": "ok"} would have reported healthy
    throughout the v1.10.0 multi-week silent-polling outage this project
    already suffered — a hung scheduler with no error and no log output.
    Returns 503 + "degraded" (with a "checks"/"problems" breakdown, nothing
    sensitive) when the scheduler isn't running, polling has gone stale, or
    the cookies file is missing/expired.

    This is the *full* report, for humans and dashboards. /health/live is the
    narrower restart-fixable subset that automation watches.
    """
    checks: dict[str, str] = {}
    problems = _liveness(checks)

    cstatus = cookies_status()
    if not cstatus.get("present"):
        checks["cookies"] = "missing or invalid"
        problems.append("cookies file is missing or invalid")
    elif cstatus.get("expired"):
        checks["cookies"] = "expired"
        problems.append("cookies file has expired")
    else:
        checks["cookies"] = "ok"

    ok = not problems
    body = {
        "status": "ok" if ok else "degraded",
        "version": VERSION,
        "checks": checks,
        "problems": problems,
    }
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/health/live")
def health_live():
    """Narrow liveness signal for the Docker HEALTHCHECK and the host autoheal
    restarter (ops/autoheal.sh): 200 only when a restart would NOT be pointless.

    Same shape as /health, minus the cookie check. Expired cookies and a full
    disk make Slipcast degraded but are not things a restart repairs, so they
    must never appear here — an automated restarter reading this endpoint would
    otherwise bounce the container in a loop while the real fix (upload fresh
    cookies, free space) went undone.
    """
    checks: dict[str, str] = {}
    problems = _liveness(checks)
    ok = not problems
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "version": VERSION,
            "checks": checks,
            "problems": problems,
        },
        status_code=200 if ok else 503,
    )
