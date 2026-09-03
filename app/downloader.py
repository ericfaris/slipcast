import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

import yt_dlp

from app import database as db
from app import notify
from app.config import (
    AUDIO_DIR,
    COOKIE_EXPIRY_WARN_DAYS,
    COOKIES_FILE,
    DATA_DIR,
    MAX_EPISODES_PER_CHANNEL,
    MIN_FREE_DISK_GB,
    THUMBNAIL_DIR,
)

_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Real YouTube video IDs are exactly 11 chars. Channel listings occasionally
# surface non-video entries (e.g. a tab link) whose "id" is the 24-char
# channel ID itself; a loose length let those through to yt-dlp, which then
# fails with a confusing "Video unavailable" error.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Hosts YouTube/Google serve thumbnails from. We only fetch thumbnail URLs that
# resolve to these, so attacker-influenced metadata can't point urlretrieve at
# an internal address (SSRF) or a local file (file://).
_ALLOWED_THUMBNAIL_HOST_SUFFIXES = (".ytimg.com", ".ggpht.com", ".googleusercontent.com")

# Both of these bound _download_thumbnail's two network/subprocess waits, for
# the same reason _base_ydl_opts() sets socket_timeout: neither urllib nor
# subprocess.run applies a timeout by default, so a thumbnail host that accepts
# the connection and then never sends a byte — or an ffmpeg that wedges — hangs
# the poll thread forever. Because APScheduler runs poll_all with
# max_instances=1, one hung run silently blocks every future scheduled poll
# with no error and no log: exactly the v1.10.0 multi-week outage, reached by a
# different path. A thumbnail is cosmetic, so timing one out and moving on is
# always the right trade.
_THUMBNAIL_FETCH_TIMEOUT = 30
_FFMPEG_TIMEOUT = 60


def _allowed_thumbnail_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host.endswith(_ALLOWED_THUMBNAIL_HOST_SUFFIXES) or host in ("ytimg.com", "ggpht.com")

# Error fragments that indicate YouTube is rejecting us for lack of valid cookies.
_AUTH_ERROR_SIGNALS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "this video is available to this channel's members",
    "use --cookies",
    # Not the bare word "cookies" — that matched any error merely *mentioning*
    # cookies (e.g. a message suggesting a workaround) and fired a cookie-
    # expiry email on unrelated failures. This phrase is yt-dlp's actual
    # diagnostic wording for a rejected/expired cookie file.
    "cookies are no longer valid",
    "login required",
    "account cookies",
    "consent",
)


def _looks_like_auth_error(message: str) -> bool:
    m = (message or "").lower()
    return any(sig in m for sig in _AUTH_ERROR_SIGNALS)


# Error fragments that mean a video requires channel membership. During channel
# polls we record these so we don't waste time re-attempting them every cycle.
_MEMBER_ONLY_SIGNALS = (
    "join this channel",
    "members-only",
    "members only",
    "available to this channel's members",
    "members of this channel",
)


def _looks_like_member_only(message: str) -> bool:
    m = (message or "").lower()
    return any(sig in m for sig in _MEMBER_ONLY_SIGNALS)


class MemberOnlyError(Exception):
    """Raised when a video can't be downloaded because it's members-only."""


logger = logging.getLogger(__name__)


def _download_thumbnail(url: str, dest: str) -> bool:
    if os.path.exists(dest):
        return True
    if not _allowed_thumbnail_url(url):
        logger.warning("Refusing to fetch thumbnail from disallowed URL: %r", url)
        return False
    tmp = dest + ".tmp"
    try:
        # urlretrieve() takes no timeout at all, so it can't be used here.
        # urlopen's timeout bounds each individual socket operation rather than
        # total wall time — the same guarantee yt-dlp's socket_timeout gives,
        # and enough to stop a silently stalled peer from pinning the thread.
        with urllib.request.urlopen(url, timeout=_THUMBNAIL_FETCH_TIMEOUT) as resp, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp, dest],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg thumbnail conversion failed for %s: %s", url, result.stderr.decode())
        os.remove(tmp)
        return os.path.exists(dest)
    except Exception as exc:
        logger.warning("Failed to download thumbnail %s: %s", url, exc)
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


def _thumbnail_dir_for(channel_id: str) -> str:
    if not _CHANNEL_ID_RE.match(channel_id):
        raise ValueError(f"Invalid channel_id: {channel_id!r}")
    path = os.path.join(THUMBNAIL_DIR, channel_id)
    os.makedirs(path, exist_ok=True)
    return path


def _audio_dir_for(channel_id: str) -> str:
    if not _CHANNEL_ID_RE.match(channel_id):
        raise ValueError(f"Invalid channel_id: {channel_id!r}")
    path = os.path.join(AUDIO_DIR, channel_id)
    os.makedirs(path, exist_ok=True)
    return path


_NETSCAPE_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")


def valid_cookie_file(path: str) -> bool:
    """Return True if path is a non-empty, Netscape-format cookies file.

    yt-dlp rejects empty or malformed cookie files with a hard error that
    aborts the whole extraction, so we validate before handing one over.
    A valid file either starts with the Netscape header comment or contains
    at least one tab-separated cookie line with the expected 7 fields.
    """
    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(_NETSCAPE_HEADERS):
                    return True
                if stripped and not stripped.startswith("#"):
                    # data line: domain, flag, path, secure, expiry, name, value
                    if len(line.rstrip("\n").split("\t")) >= 7:
                        return True
    except OSError as exc:
        logger.warning("Could not read cookies file %s: %s", path, exc)
    return False


# We compute a cookie expiry deadline from the Google/YouTube cookies, since
# those are what gate Slipcast's access. In practice exports are often *not*
# logged in — they carry only anonymous visitor cookies (VISITOR_INFO1_LIVE,
# __Secure-ROLLOUT_TOKEN, …) that still satisfy YouTube's bot check — so we
# don't require login cookies to be present; we just take the earliest real
# expiry among the relevant cookies.
_COOKIE_DOMAIN_SUFFIXES = ("youtube.com", "google.com")

# Cookies that rotate every few hours/days and are refreshed automatically.
# Including them would report a misleadingly soon deadline (e.g. GPS expires
# the same day), so they're excluded from the expiry calculation.
_VOLATILE_COOKIE_NAMES = frozenset({
    "GPS", "YSC", "OTZ", "SIDCC", "NID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC",
})


def cookie_file_expiry(path: str) -> int | None:
    """Earliest expiry (unix seconds) among the Google/YouTube cookies in a
    Netscape cookies file, ignoring session cookies and volatile rotation
    tokens. Returns None if none are present/parseable.

    This is the file's *hard* deadline — after it, the relevant cookies are
    gone. YouTube also rotates cookies server-side well before this (see
    age_days), so treat it as a ceiling, not a prediction.
    """
    earliest: int | None = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 7:
                    continue
                domain, name = fields[0].lstrip(".").lower(), fields[5]
                if not domain.endswith(_COOKIE_DOMAIN_SUFFIXES):
                    continue
                if name in _VOLATILE_COOKIE_NAMES:
                    continue
                try:
                    expiry = int(fields[4])
                except ValueError:
                    continue
                if expiry <= 0:  # session cookie — no fixed expiry
                    continue
                if earliest is None or expiry < earliest:
                    earliest = expiry
    except OSError as exc:
        logger.warning("Could not read cookies file %s for expiry: %s", path, exc)
    return earliest


def cookies_status() -> dict:
    """Return info about the current cookies file.

    Combines two signals so the UI can warn before polls fail:
    - 'expires_at'/'days_until_expiry': the file's hard expiry, parsed from the
      login cookies themselves — a known deadline after which auth fails.
    - 'age_days'/'stale': YouTube rotates cookies server-side every few weeks
      regardless of the file's stated expiry, so age is the practical estimate.
    """
    if not valid_cookie_file(COOKIES_FILE):
        return {"present": False, "updated": None, "age_days": None, "stale": False,
                "expires_at": None, "days_until_expiry": None, "expired": False}

    now = datetime.now(tz=timezone.utc)
    mtime = os.path.getmtime(COOKIES_FILE)
    age_days = (now - datetime.fromtimestamp(mtime, tz=timezone.utc)).days
    updated = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    expiry_ts = cookie_file_expiry(COOKIES_FILE)
    expires_at = None
    days_until_expiry = None
    expired = False
    if expiry_ts is not None:
        exp_dt = datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
        expires_at = exp_dt.strftime("%Y-%m-%d %H:%M UTC")
        days_until_expiry = (exp_dt - now).days
        expired = exp_dt <= now

    return {
        "present": True, "updated": updated, "age_days": age_days,
        "stale": age_days >= 21,
        "expires_at": expires_at, "days_until_expiry": days_until_expiry,
        "expired": expired,
    }


def _base_ydl_opts() -> dict:
    # yt-dlp's default JS runtime is "deno"; we ship Node 22 instead (see
    # Dockerfile) for the YouTube n-challenge solver. Without this, format
    # selection fails with "Requested format is not available".
    # socket_timeout is critical: without it a stalled TCP read hangs the whole
    # poll_all run forever. Because APScheduler runs poll_all with the default
    # max_instances=1, one hung run silently blocks every future scheduled poll
    # (no error, no log) until the process restarts. Bound every network op.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
        "socket_timeout": 30,
    }
    if valid_cookie_file(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logger.info("Using cookies file: %s (%d bytes)", COOKIES_FILE, os.path.getsize(COOKIES_FILE))
    elif COOKIES_FILE and os.path.exists(COOKIES_FILE):
        logger.warning(
            "Cookies file %s is empty or not in Netscape format — ignoring it; "
            "downloads may fail or be rate-limited", COOKIES_FILE,
        )
    else:
        logger.warning("No cookies file found at %s — downloads may fail", COOKIES_FILE)
    return opts


def _ydl_opts(channel_id: str) -> dict:
    return {
        **_base_ydl_opts(),
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "outtmpl": os.path.join(_audio_dir_for(channel_id), "%(id)s.%(ext)s"),
        "extract_flat": False,
        "sleep_interval": 2,
        "max_sleep_interval": 5,
    }


def _fetch_channel_entries(channel_url: str, max_entries: int) -> tuple[list[dict], str, str]:
    """Return metadata for the most recent max_entries videos without downloading."""
    opts = {
        **_base_ydl_opts(),
        "extract_flat": True,
        "playlistend": max_entries,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    entries = info.get("entries") or []
    channel_id = info.get("channel_id") or info.get("id", "unknown")
    channel_name = info.get("channel") or info.get("uploader") or info.get("title", "Unknown")

    # download channel cover art
    channel_thumb_url = info.get("thumbnail")
    if not channel_thumb_url:
        # extract_flat often omits thumbnail; try thumbnails list (highest res last)
        thumbnails = info.get("thumbnails") or []
        for t in reversed(thumbnails):
            if t.get("url"):
                channel_thumb_url = t["url"]
                break
    if channel_thumb_url:
        dest = os.path.join(_thumbnail_dir_for(channel_id), "channel.jpg")
        _download_thumbnail(channel_thumb_url, dest)

    return entries, channel_id, channel_name


def _download_entry(entry: dict, channel_id: str, channel_name: str) -> dict | None:
    video_id = entry.get("id")
    if not video_id:
        return None
    if not _VIDEO_ID_RE.match(video_id):
        logger.warning("Skipping entry with suspicious video_id: %r", video_id)
        return None

    audio_dir = _audio_dir_for(channel_id)
    expected_file = os.path.join(audio_dir, f"{video_id}.mp3")

    if os.path.exists(expected_file):
        logger.debug("Already downloaded: %s", video_id)
        return None

    url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    logger.info("Downloading %s: %s", video_id, entry.get("title", ""))

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(channel_id)) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        if _looks_like_member_only(str(exc)):
            # Let the caller decide whether to remember/skip this one.
            raise MemberOnlyError(video_id) from exc
        logger.warning("Failed to download %s: %s", video_id, exc)
        return None

    if not os.path.exists(expected_file):
        logger.warning("Expected file not found after download: %s", expected_file)
        return None

    published = info.get("upload_date", "")
    if published:
        published = datetime.strptime(published, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
    else:
        published = datetime.now(timezone.utc).isoformat()

    # download episode thumbnail
    thumb_filename = None
    thumb_url = info.get("thumbnail")
    if thumb_url:
        thumb_dest = os.path.join(_thumbnail_dir_for(channel_id), f"{video_id}.jpg")
        if _download_thumbnail(thumb_url, thumb_dest):
            thumb_filename = os.path.basename(thumb_dest)

    return {
        "id": video_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "title": info.get("title", video_id),
        "description": info.get("description", ""),
        "published": published,
        "duration": info.get("duration"),
        "filename": os.path.basename(expected_file),
        "filesize": os.path.getsize(expected_file),
        "thumbnail": thumb_filename,
    }


def _remove_if_exists(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
        logger.info("Pruned %s", path)


def _prune_channel(channel_id: str):
    episodes = db.get_episodes(channel_id)
    to_delete = episodes[MAX_EPISODES_PER_CHANNEL:]
    for ep in to_delete:
        _remove_if_exists(os.path.join(_audio_dir_for(channel_id), ep["filename"]))
        if ep["thumbnail"]:
            _remove_if_exists(os.path.join(_thumbnail_dir_for(channel_id), ep["thumbnail"]))
        db.delete_episode(ep["id"])
        # Remember it so we never re-download a video we deliberately dropped.
        # YouTube channel listings aren't strictly chronological (pinned videos,
        # premieres, re-uploads), so a video the channel lists near the top can
        # have an upload date that puts it past the cap. Without this, the
        # download loop (channel order) and prune (upload-date order) fight each
        # other and re-download the same videos every poll forever.
        db.add_skip_video(ep["id"], channel_id, "pruned")
    # Even with the DB capped, disk can hold files the DB no longer references:
    # thumbnails that predate thumbnail-pruning, audio left by an interrupted
    # prune, or yt-dlp `.part`/temp leftovers. Sweep them so only the current
    # episodes' files remain on disk.
    _sweep_orphan_files(channel_id)


# yt-dlp writes intermediate files into the *same* audio directory as the
# final .mp3 while a download is in progress (the pre-conversion audio format,
# plus its own ".part"/".ytdl" progress-tracking files); _download_thumbnail
# similarly writes "<dest>.tmp" before the ffmpeg conversion lands. None of
# these are ever referenced by an episode row, so a sweep running in one
# thread while another thread is mid-download would previously delete them
# out from under it. We can't tell a file that's genuinely orphaned (left by
# an interrupted, abandoned download) from one that's simply mid-flight by
# name alone — but a real download/conversion finishes in well under an hour,
# so recency is a reliable enough signal: skip temp-shaped files touched
# recently, delete ones old enough that they can't still be in progress.
_YTDLP_TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
_YTDLP_PRECONVERT_SUFFIXES = (".m4a", ".webm", ".opus", ".aac", ".ogg", ".mp4", ".mkv")
_RECENT_FILE_GRACE_SECONDS = 3600


def _looks_like_ytdlp_temp(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(_YTDLP_TEMP_SUFFIXES) or lower.endswith(_YTDLP_PRECONVERT_SUFFIXES)


def _is_recent(path: str) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < _RECENT_FILE_GRACE_SECONDS
    except OSError:
        return False


def _sweep_orphan_files(channel_id: str) -> None:
    """Delete any audio/thumbnail file not referenced by the channel's episodes.

    Runs against whatever the DB currently holds (already capped to
    MAX_EPISODES_PER_CHANNEL), so it reflects exactly the kept episodes. The
    channel's cover art (`channel.jpg`) is always preserved. Files that look
    like an in-progress yt-dlp download or thumbnail conversion are left alone
    unless they're stale (see _RECENT_FILE_GRACE_SECONDS) — see module-level
    comment above.
    """
    episodes = db.get_episodes(channel_id)
    keep_audio = {ep["filename"] for ep in episodes if ep["filename"]}
    keep_thumbs = {ep["thumbnail"] for ep in episodes if ep["thumbnail"]}
    keep_thumbs.add("channel.jpg")

    audio_dir = _audio_dir_for(channel_id)
    for name in os.listdir(audio_dir):
        if name in keep_audio:
            continue
        path = os.path.join(audio_dir, name)
        if _looks_like_ytdlp_temp(name) and _is_recent(path):
            continue
        _remove_if_exists(path)

    thumb_dir = _thumbnail_dir_for(channel_id)
    for name in os.listdir(thumb_dir):
        if name in keep_thumbs:
            continue
        path = os.path.join(thumb_dir, name)
        if _looks_like_ytdlp_temp(name) and _is_recent(path):
            continue
        _remove_if_exists(path)


# Per-channel locks so two concurrent pollers (the scheduled poll_all, a
# manual "poll now", "poll selected", and "poll all" from the UI) can never
# run poll_channel for the *same* channel at once. Without this, two
# concurrent runs each prune/sweep against their own snapshot of "current
# episodes" while the other is mid-download, deleting each other's in-flight
# files (see _sweep_orphan_files). Keyed on the normalized URL so "add trailing
# slash"/"share-link query string" variants of the same channel still collide.
_poll_locks: dict[str, threading.Lock] = {}
_poll_locks_guard = threading.Lock()


def _poll_lock_key(url: str) -> str:
    parsed = urlparse(url.rstrip("/"))
    return parsed._replace(query="", fragment="").geturl().rstrip("/").lower()


def _get_poll_lock(key: str) -> threading.Lock:
    with _poll_locks_guard:
        lock = _poll_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _poll_locks[key] = lock
        return lock


def poll_channel(channel_url: str):
    """Poll one channel, refusing to run concurrently with itself.

    Returns a summary dict on completion, or {"already_polling": True, ...} if
    another poll of the same channel is already in flight — the caller (see
    main._run_poll) surfaces that through the jobs tracker as a toast instead
    of silently blocking or double-running.
    """
    lock = _get_poll_lock(_poll_lock_key(channel_url))
    if not lock.acquire(blocking=False):
        logger.info("Skipping poll for %s — already in progress", channel_url)
        return {"channel_name": None, "downloaded": 0, "failures": [],
                "error": None, "already_polling": True}
    try:
        return _poll_channel_locked(channel_url)
    finally:
        lock.release()


def _poll_channel_locked(channel_url: str):
    original_url = channel_url.rstrip("/")
    # Strip query/fragment (e.g. share-link `?si=...` tracking params) before
    # appending "/videos" — otherwise it lands inside the query string and
    # yt-dlp resolves the malformed URL to the channel's tab list (Videos/
    # Live/Shorts) instead of actual videos, silently downloading nothing.
    parsed = urlparse(original_url)
    channel_url = parsed._replace(query="", fragment="").geturl().rstrip("/") + "/videos"
    logger.info("Polling channel: %s", channel_url)

    started_at = datetime.now(timezone.utc).isoformat()
    known_channel_id = db.get_channel_id_for_url(original_url)

    # Enforce the cap using the channel_id we already know, before fetching.
    # A fetch that fails (e.g. expired cookies) used to return early and leave
    # an over-cap channel un-pruned forever; pruning up front fixes that.
    if known_channel_id:
        _prune_channel(known_channel_id)

    try:
        entries, channel_id, channel_name = _fetch_channel_entries(channel_url, MAX_EPISODES_PER_CHANNEL * 3)
    except Exception as exc:
        logger.error("Failed to fetch channel %s: %s", channel_url, exc)
        if not valid_cookie_file(COOKIES_FILE) or _looks_like_auth_error(str(exc)):
            notify.send_cookie_alert()
        _record_run(original_url, known_channel_id, started_at,
                    status="error", error=str(exc))
        return
    db.update_channel_meta(original_url, channel_id, channel_name)

    # Enforce the episode cap up front too, so a channel that drifted over the
    # limit (e.g. an earlier poll was interrupted before it could prune) is
    # corrected on the very next poll, even if this one is interrupted again.
    _prune_channel(channel_id)

    skip_ids = db.get_skip_video_ids(channel_id)
    downloaded = 0
    considered = 0  # real (non-skip, non-members) videos seen, in channel order
    error = None
    video_failures: list[str] = []  # per-video download errors, for alerting
    try:
        for entry in entries:
            # Only ever look at the channel's newest MAX videos. Walking deeper
            # (chasing MAX *new* downloads) would pull videos that sit past the
            # cap by date, download them, then immediately prune them — wasteful
            # churn. Channel listings are newest-first, so the top MAX are what
            # we want to keep.
            if considered >= MAX_EPISODES_PER_CHANNEL:
                break
            video_id = entry.get("id")
            if video_id in skip_ids:
                continue
            if entry.get("availability") in ("subscriber_only", "needs_auth", "premium_only"):
                logger.debug("Skipping member-only video: %s", video_id)
                if video_id:
                    db.add_skip_video(video_id, channel_id, "members_only")
                continue
            considered += 1
            try:
                result = _download_entry(entry, channel_id, channel_name)
            except MemberOnlyError:
                # Remember it so future polls skip it instantly instead of
                # re-attempting (and timing out) every single cycle. It wasn't a
                # real downloadable video, so don't count it toward the cap.
                if video_id:
                    db.add_skip_video(video_id, channel_id, "members_only")
                logger.debug("Recorded members-only skip: %s", video_id)
                considered -= 1
                continue
            except Exception as exc:  # noqa: BLE001
                # One video failing to download (403, transient network error,
                # a yt-dlp/ffmpeg postprocessor bug) must not abort the rest of
                # this channel — or, via re-raise, every remaining channel in
                # the poll_all run. Log it, note it for the alert, move on.
                logger.error("Failed to download %s from %s: %s",
                             video_id, channel_name, exc)
                video_failures.append(f"{video_id}: {exc}")
                if _looks_like_auth_error(str(exc)):
                    notify.send_cookie_alert()
                continue
            if result:
                db.upsert_episode(result)
                downloaded += 1
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001 — record, but never re-raise: a
        # raise here propagates out of poll_all and kills the scheduled thread,
        # skipping every channel after this one.
        error = str(exc)
        logger.exception("Unexpected error polling %s", channel_name)
    finally:
        # Always cap to the newest MAX episodes, even if the loop raised.
        _prune_channel(channel_id)
        run_error = error
        if not run_error and video_failures:
            run_error = (f"{len(video_failures)} video(s) failed to download: "
                         + "; ".join(video_failures[:5]))
        _record_run(original_url, channel_id, started_at,
                    status="error" if (error or video_failures) else "ok",
                    downloaded=downloaded, channel_name=channel_name, error=run_error)
    logger.info("Done polling %s (%s) — %d new, %d failed",
                channel_name, channel_id, downloaded, len(video_failures))
    return {"channel_name": channel_name, "downloaded": downloaded,
            "failures": video_failures, "error": error}


def _record_run(url, channel_id, started_at, *, status, downloaded=0,
                channel_name=None, error=None):
    """Persist a poll outcome; never let bookkeeping break a poll."""
    if channel_name is None and channel_id:
        meta = db.get_channel_meta(channel_id)
        channel_name = meta["channel_name"] if meta else None
    try:
        db.record_poll_run({
            "channel_id": channel_id,
            "channel_name": channel_name,
            "url": url,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "downloaded": downloaded,
            "error": error,
        })
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record poll run for %s", url)


def _free_disk_gb(path: str) -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


def _enforce_disk_floor() -> None:
    """Delete the globally oldest episodes until free disk clears the floor.

    MAX_EPISODES_PER_CHANNEL caps each channel on its own, but nothing caps the
    total: enough channels, or long enough episodes, and the volume fills. A
    full disk doesn't produce a clean error — yt-dlp fails mid-write, SQLite
    starts refusing writes, and the failure looks like something else entirely.
    So we free space *before* downloading rather than discovering it partway
    through, and we do it globally oldest-first (not per channel) because the
    thing that ran out is one shared filesystem.

    This deletes the user's downloads irreversibly, so it always emails.
    """
    if MIN_FREE_DISK_GB <= 0:
        return  # explicit opt-out

    free = _free_disk_gb(DATA_DIR)
    if free >= MIN_FREE_DISK_GB:
        return

    logger.warning(
        "Free disk %.2f GB is below MIN_FREE_DISK_GB=%d — pruning oldest episodes",
        free, MIN_FREE_DISK_GB,
    )

    pruned: list[str] = []
    freed_bytes = 0
    for ep in db.get_all_episodes_oldest_first():
        channel_id = ep["channel_id"]
        try:
            # _audio_dir_for/_thumbnail_dir_for raise on a channel_id that
            # doesn't match the safe-name regex. That row's files can't be
            # located safely, but the row itself should still go — otherwise
            # it's an undeletable blocker at the head of the oldest-first list.
            if _CHANNEL_ID_RE.match(channel_id):
                _remove_if_exists(os.path.join(_audio_dir_for(channel_id), ep["filename"]))
                if ep["thumbnail"]:
                    _remove_if_exists(
                        os.path.join(_thumbnail_dir_for(channel_id), ep["thumbnail"])
                    )
            else:
                logger.warning(
                    "Disk prune: not touching files for suspicious channel_id %r "
                    "(dropping the row only)", channel_id,
                )
            db.delete_episode(ep["id"])
            # Same reasoning as _prune_channel(): without a skip marker the very
            # next poll re-downloads exactly what we just deleted and refills
            # the disk, and the two fight each other forever.
            db.add_skip_video(ep["id"], channel_id, "disk_pressure")
        except Exception:  # noqa: BLE001 — one bad row (permissions, a file
            # already gone) must not abort the rest of the remediation.
            logger.exception("Disk prune failed for episode %s", ep["id"])
            continue

        freed_bytes += ep["filesize"] or 0
        pruned.append(f'{ep["channel_name"]} — {ep["title"]}')

        # Re-stat after every single deletion rather than guessing a batch
        # size: one episode can be hundreds of MB, and this stops the moment
        # we're back above the floor, deleting the minimum necessary.
        free = _free_disk_gb(DATA_DIR)
        if free >= MIN_FREE_DISK_GB:
            break

    if pruned:
        logger.warning("Disk prune removed %d episode(s), freed %.0f MB, now %.2f GB free",
                       len(pruned), freed_bytes / 1_048_576, free)
        notify.send_disk_prune_alert(pruned, freed_bytes, free)

    if free < MIN_FREE_DISK_GB:
        logger.error(
            "Free disk still %.2f GB after pruning everything available "
            "(threshold %d GB) — something other than episodes is using the volume",
            free, MIN_FREE_DISK_GB,
        )
        if not pruned:
            # send_disk_prune_alert([]) is a no-op by design, but "the disk is
            # full and I have nothing left to delete" is the single most
            # important thing to tell a human about, so synthesize a line.
            notify.send_disk_prune_alert(["(nothing left to prune)"], 0, free)


def poll_all():
    # Relieve disk pressure before anything downloads, so a full volume is
    # fixed up front rather than hit halfway through a write.
    try:
        _enforce_disk_floor()
    except Exception:  # noqa: BLE001 — remediation must never abort the poll run
        logger.exception("Disk-pressure check failed")

    if not valid_cookie_file(COOKIES_FILE):
        logger.warning("Cookies file missing/invalid at poll time — alerting")
        notify.send_cookie_alert()
    else:
        # Cookies still work but are nearing their parsed expiry — warn ahead of
        # time so there's no polling gap. Debounced separately from the
        # "already broken" alert.
        status = cookies_status()
        days_left = status.get("days_until_expiry")
        if (not status.get("expired") and days_left is not None
                and days_left <= COOKIE_EXPIRY_WARN_DAYS):
            logger.info("Cookies expire in %d days — sending advance warning", days_left)
            notify.send_cookie_expiry_warning(days_left, status["expires_at"])
    channels = db.get_channels()
    problems: list[str] = []
    for ch in channels:
        try:
            summary = poll_channel(ch["url"])
        except Exception as exc:  # noqa: BLE001 — belt-and-braces: never let one
            # channel abort the whole scheduled run.
            logger.exception("poll_channel crashed for %s", ch["url"])
            problems.append(f"{ch['url']}: {exc}")
            continue
        if summary and (summary["failures"] or summary["error"]):
            name = summary["channel_name"] or ch["url"]
            detail = summary["error"] or f"{len(summary['failures'])} video(s) failed"
            problems.append(f"{name}: {detail}")

    if problems:
        logger.warning("poll_all finished with %d problem channel(s)", len(problems))
        notify.send_poll_failure_alert(problems)


def download_single(video_url: str, subscribe: bool = False):
    """Download a specific video URL, bypassing all availability filters."""
    logger.info("Downloading single video: %s", video_url)
    try:
        with yt_dlp.YoutubeDL(_base_ydl_opts()) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        logger.error("Failed to fetch video info %s: %s", video_url, exc)
        return

    video_id = info.get("id")
    if not video_id:
        logger.error("Could not get video ID from %s", video_url)
        return

    channel_id = info.get("channel_id") or info.get("uploader_id", "unknown")
    channel_name = info.get("channel") or info.get("uploader", "Unknown")

    if subscribe:
        channels = db.get_channels()
        if not any(ch["channel_id"] == channel_id for ch in channels):
            channel_page_url = f"https://www.youtube.com/channel/{channel_id}"
            db.add_channel(channel_page_url)
            db.update_channel_meta(channel_page_url, channel_id, channel_name)
            logger.info("Subscribed to channel: %s", channel_name)
    else:
        channels = db.get_channels()
        if not any(ch["channel_id"] == channel_id for ch in channels):
            db.upsert_unsubscribed_channel(channel_id, channel_name)

    try:
        result = _download_entry({"id": video_id}, channel_id, channel_name)
    except MemberOnlyError:
        # One-off downloads are explicit user requests — don't record a skip,
        # just report that membership is required.
        logger.warning("Cannot download members-only video without membership: %s", video_url)
        return
    if result:
        db.upsert_episode(result)
        logger.info("Downloaded single video: %s", result["title"])
        # One-off/unsubscribed channels are never polled, so poll_channel's cap
        # enforcement never runs for them — without this, repeatedly
        # downloading individual videos from the same channel grows its audio
        # without limit. Subscribed channels get this for free via poll_channel,
        # but it's harmless (a no-op below the cap) to call it again here too.
        _prune_channel(channel_id)


def remove_channel_data(channel_id: str):
    """Delete all downloaded files for a channel."""
    import shutil
    if not _CHANNEL_ID_RE.match(channel_id):
        logger.error("Refusing to delete data for suspicious channel_id: %r", channel_id)
        return
    audio_dir = os.path.join(AUDIO_DIR, channel_id)
    thumb_dir = os.path.join(THUMBNAIL_DIR, channel_id)
    for path in (audio_dir, thumb_dir):
        if os.path.exists(path):
            shutil.rmtree(path)
            logger.info("Removed directory: %s", path)


def _dir_bytes(path: str) -> int:
    total = 0
    if os.path.isdir(path):
        for name in os.listdir(path):
            fp = os.path.join(path, name)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def find_orphan_channels() -> list[dict]:
    """Find channel data (episodes rows and/or on-disk directories) owned by
    neither a `channels` row nor an `unsubscribed_channels` row.

    Two ways this happens: `_remove_one` (app/main.py) fails to resolve a
    channel_id before deleting the `channels` row (a URL variant, or a channel
    removed before update_channel_meta ever ran) and leaves its episodes,
    skip_videos, and files behind; or those files/rows survive a delete that
    was interrupted partway through. This only detects — nothing here deletes
    anything; callers decide (see app.main lifespan report and the
    /channels/remove-orphan endpoint).
    """
    known = {ch["channel_id"] for ch in db.get_channels() if ch["channel_id"]}
    known |= {ch["channel_id"] for ch in db.get_unsubscribed_channels()}

    candidate_ids = set(db.orphan_channel_ids())
    for base in (AUDIO_DIR, THUMBNAIL_DIR):
        if os.path.isdir(base):
            for name in os.listdir(base):
                if _CHANNEL_ID_RE.match(name) and name not in known:
                    candidate_ids.add(name)
    candidate_ids -= known

    orphans = []
    for cid in sorted(candidate_ids):
        episodes = db.get_episodes(cid)
        name = episodes[0]["channel_name"] if episodes else cid
        size = _dir_bytes(os.path.join(AUDIO_DIR, cid)) + _dir_bytes(os.path.join(THUMBNAIL_DIR, cid))
        orphans.append({
            "channel_id": cid,
            "channel_name": name,
            "episode_count": len(episodes),
            "bytes": size,
        })
    return orphans
