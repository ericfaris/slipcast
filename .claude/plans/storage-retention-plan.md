# Implementation Plan: Storage & Retention (Group 2 of 4)

**Recommended executor model: Sonnet 5.** Ten files are touched but every risky
decision (opus extension, MIME type, pre-vs-post duration filtering, one-off
policy) is already resolved below with exact values and file/line-level
instructions, leaving mechanical, well-tested edits.

Source brief: `.claude/plans/storage-retention-brief.md` (read it too — this
plan supersedes it wherever they differ).

---

## Summary

Slipcast currently hardcodes MP3/128kbps audio and enforces only a flat
per-channel episode-count cap, so a channel of multi-hour livestreams eats
gigabytes while a clips channel uses a fraction of that — and nothing in the
dashboard shows it. This change adds four global env-var levers
(`AUDIO_CODEC`, `AUDIO_BITRATE_KBPS`, `MAX_EPISODE_AGE_DAYS`,
`MAX_EPISODE_DURATION_MINUTES`) plus per-channel and total disk-usage figures
in `GET /api/state` and on the dashboard, so the user can shrink audio, drop
stale or over-long episodes, and see the effect. No schema migration, no
re-encoding of existing audio: old `.mp3` files and their feed entries keep
working byte-for-byte as they do today, and defaults are unchanged.

---

## Approach & key decisions

### Investigation (a): does `extract_flat=True` carry `duration`? — YES, usually

Verified against the installed yt-dlp in this repo
(`.venv/lib/python3.12/site-packages/yt_dlp/extractor/youtube/_tab.py`):

- `YoutubeTabBaseInfoExtractor._extract_video()` (line ~76) builds every flat
  entry with an explicit `'duration': duration` key, resolved in three
  fallbacks: `renderer['lengthSeconds']`, then `parse_duration()` of
  `lengthText` / the `thumbnailOverlayTimeStatusRenderer` overlay text, then a
  regex over the title's accessibility label.
- The newer lockup layout path
  (`_extract_lockup_view_model()`, line ~342) also passes
  `duration=` parsed from the thumbnail badge text.

So a `/videos` tab entry normally has a usable integer `duration`. **But it is
`None` for entries with no length badge** — currently-live streams
(`overlay_style == 'LIVE'`), upcoming premieres, and any renderer whose layout
yt-dlp doesn't parse. `int_or_none`/`parse_duration` returning `None` is a
normal, silent outcome.

**Design choice: hybrid — pre-download check when `duration` is present, plus a
post-download backstop using the real `info["duration"]`.** The pre-check gets
the common case for free (no bandwidth wasted); the post-check guarantees the
cap actually holds for live/premiere/unparsed entries. Both record a
`skip_videos` row with reason `"too_long"`, so neither path re-attempts on the
next poll.

*Rejected:* pre-check only (silently lets multi-hour livestreams through —
exactly the UAP Gerb case that motivated the brief); post-check only (wastes a
full multi-hour download every time, when the metadata was right there).

### Investigation (b): what does `FFmpegExtractAudio` produce for `opus`?

Verified in `.venv/lib/python3.12/site-packages/yt_dlp/postprocessor/ffmpeg.py`:

```python
ACODECS = {
    # name: (ext, encoder, opts)
    'mp3':    ('mp3',  'libmp3lame', ()),
    'opus':   ('opus', 'libopus',    ()),
    'vorbis': ('ogg',  'libvorbis',  ()),
    ...
}
```

`FFmpegExtractAudioPP.run()` looks up `ACODECS[target_format]` and does
`replace_extension(path, extension, ...)`, then sets `information['ext']`.
So `preferredcodec="opus"` produces **`<video_id>.opus`** (Ogg-encapsulated
Opus), never `.ogg`. (`.ogg` is what `preferredcodec="vorbis"` gives.)

**MIME type: `audio/ogg`.** `.opus` files are Ogg containers (RFC 7845, which
registers `audio/ogg` for Ogg Opus; `audio/opus` is the RTP payload type, not a
file type). This also matches what the app already serves: `/audio` is a
`StaticFiles` mount (`app/main.py:300`) and Python's built-in
`mimetypes.types_map['.opus'] == 'audio/ogg'` (confirmed on 3.12, independent
of `/etc/mime.types`, which the slim container may not have), so the enclosure
type and the actual `Content-Type` on the wire agree.

**The feed's enclosure MIME must be derived from each episode's stored
filename extension, not from the current `AUDIO_CODEC` setting** — a feed can
legitimately contain both old `.mp3` and new `.opus` items, and the config may
have changed since the old ones were downloaded.

### Other decisions

- **Codec/bitrate are read as module-level names in `app/downloader.py`**
  (`from app.config import AUDIO_CODEC, AUDIO_BITRATE_KBPS, ...`), matching the
  existing `MAX_EPISODES_PER_CHANNEL` pattern, because the test suite
  monkeypatches these on the module (`monkeypatch.setattr(downloader,
  "MAX_EPISODES_PER_CHANNEL", 2)`). Same for the new retention/duration values.
- **Codec→extension mapping and validation live in `downloader.py`, not
  `config.py`** — `config.py` has no logger and does no validation today; it
  just parses env. An unrecognized `AUDIO_CODEC` falls back to `mp3` with a
  logged warning rather than producing files with an extension nothing can map.
- **`AUDIO_BITRATE_KBPS` stays a string** (`"128"`), exactly as the current
  hardcoded `"preferredquality": "128"` — required for acceptance criterion 2
  (default `_ydl_opts()` output unchanged).
- **Re-download protection when the codec changes:** `_download_entry()`'s
  existence check must look for the video under *any* known audio extension,
  not just the currently-configured one. Otherwise flipping `AUDIO_CODEC` to
  `opus` makes every existing `.mp3` episode look missing and re-downloads the
  whole library. Old files stay `.mp3`; only genuinely-new videos get `.opus`.
- **The duration filter does NOT block one-off downloads** (`download_single`).
  That function is explicitly documented as "bypassing all availability
  filters", it's an explicit human request, and a block would surface through
  the job tracker as the misleading toast "Nothing downloaded — video may be
  unavailable, private, or already saved". Instead, log a warning line noting
  the video exceeds `MAX_EPISODE_DURATION_MINUTES` and download it anyway.
  Document this in README. (Mechanism: keyword-only
  `enforce_duration: bool = True` on `_download_entry`, passed `False` by
  `download_single`.)
- **Do not touch `_YTDLP_PRECONVERT_SUFFIXES`.** It contains `".opus"`, which
  under `AUDIO_CODEC=opus` also matches the *final* file. This is correct and
  must be left alone: `_sweep_orphan_files()` checks `keep_audio` membership
  *before* the temp-suffix test, so a referenced `.opus` episode is never
  swept, and an unreferenced one (mid-download, DB row not yet written) gets
  the 1-hour `_RECENT_FILE_GRACE_SECONDS` protection. Removing `.opus` from
  that tuple would make in-flight opus downloads deletable immediately — a
  regression. Add a short comment saying so.
- **Storage sizes are computed on demand, uncached.** `_dir_bytes()` already
  exists and `find_orphan_channels()` already does per-request `listdir`s; the
  dashboard refreshes `/api/state` every 9s with a flat ~20-40 files per
  channel. A TTL cache was considered and rejected as premature (and it
  complicates tests); noted under Risks as the follow-up if it ever matters.

---

## Step-by-step tasks

Branch from current `main`: `git checkout main && git pull && git checkout -b
feat/storage-retention`. Do **not** branch from or merge
`feat/resilience-self-healing` (Group 1). If the working tree has uncommitted
Group 1 changes in `app/config.py` / `app/database.py` / `app/downloader.py` /
`app/notify.py`, stash or ignore them — branch from committed `main`.

### 1. `app/config.py` — new env vars

Append after the existing `MIN_FREE_DISK_GB` block (keep the comment register:
full sentences, explain *why*, not *what*):

```python
# --- Audio format ------------------------------------------------------------
# Codec/bitrate for the audio yt-dlp extracts. "mp3" (the default) is what every
# podcast app understands; "opus" is roughly half the size at equivalent quality
# but is not universally supported (Apple Podcasts and Pocket Casts in
# particular). Changing this affects NEW downloads only — episodes already on
# disk keep their original format, so a feed can legitimately mix the two.
AUDIO_CODEC = os.environ.get("AUDIO_CODEC", "mp3").strip().lower()
AUDIO_BITRATE_KBPS = os.environ.get("AUDIO_BITRATE_KBPS", "128").strip()

# --- Retention ---------------------------------------------------------------
# Drop episodes older than this many days, on top of the MAX_EPISODES_PER_CHANNEL
# count cap. 0 disables age-based pruning (the default — count cap only).
MAX_EPISODE_AGE_DAYS = int(os.environ.get("MAX_EPISODE_AGE_DAYS", "0"))
# Don't download channel videos longer than this many minutes — a single
# multi-hour livestream can consume more disk than a whole channel of normal
# uploads. 0 disables the check. One-off downloads are exempt (an explicit
# request); they only log a warning.
MAX_EPISODE_DURATION_MINUTES = int(os.environ.get("MAX_EPISODE_DURATION_MINUTES", "0"))
```

### 2. `app/downloader.py` — codec/extension plumbing

a. Extend the `from app.config import (...)` block with `AUDIO_BITRATE_KBPS`,
   `AUDIO_CODEC`, `MAX_EPISODE_AGE_DAYS`, `MAX_EPISODE_DURATION_MINUTES`
   (keep alphabetical order).

b. Add near the other module constants:

```python
# yt-dlp's FFmpegExtractAudio postprocessor maps each preferredcodec to a fixed
# output extension (see ACODECS in yt_dlp/postprocessor/ffmpeg.py): "mp3" ->
# .mp3, "opus" -> .opus (an Ogg container). Anything we don't know how to name
# is not worth guessing at, so we fall back to mp3.
_CODEC_EXTENSIONS = {"mp3": "mp3", "opus": "opus"}
# Extensions a previously-downloaded episode may already carry. The existence
# check consults all of them so switching AUDIO_CODEC doesn't make every
# existing episode look missing and re-download the whole library.
_KNOWN_AUDIO_EXTENSIONS = ("mp3", "opus")
```

Note the helpers below must read the module-global `AUDIO_CODEC` at call time
(so tests can monkeypatch it) — do not capture it in a module-level constant.
Place them after `logger = logging.getLogger(__name__)`.

   Prefer this exact shape so the fallback lives in one place — `_audio_codec()`
   owns the validation/warning and `_audio_ext()` is derived from it:

```python
def _audio_codec() -> str:
    if AUDIO_CODEC not in _CODEC_EXTENSIONS:
        logger.warning("Unsupported AUDIO_CODEC %r — falling back to mp3", AUDIO_CODEC)
        return "mp3"
    return AUDIO_CODEC


def _audio_ext() -> str:
    return _CODEC_EXTENSIONS[_audio_codec()]
```

c. `_ydl_opts()` — replace the hardcoded postprocessor values with:

```python
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": _audio_codec(),
            "preferredquality": AUDIO_BITRATE_KBPS,
        }],
```

Everything else in `_ydl_opts()` (format selector, outtmpl, sleep intervals)
is unchanged.

d. `_download_entry()` — extension-aware paths. Replace:

```python
    expected_file = os.path.join(audio_dir, f"{video_id}.mp3")
    if os.path.exists(expected_file):
```

with a lookup that honours pre-existing files in any known format:

```python
    expected_file = os.path.join(audio_dir, f"{video_id}.{_audio_ext()}")
    # An episode downloaded before AUDIO_CODEC changed still lives under its
    # original extension; treat any of them as "already have it" so flipping the
    # codec doesn't re-download an entire library.
    existing = next(
        (p for p in (os.path.join(audio_dir, f"{video_id}.{e}") for e in _KNOWN_AUDIO_EXTENSIONS)
         if os.path.exists(p)),
        None,
    )
    if existing:
        logger.debug("Already downloaded: %s", video_id)
        return None
```

The post-download `if not os.path.exists(expected_file)` check and the returned
dict (`"filename": os.path.basename(expected_file)`) stay as they are — they
now naturally carry the new extension.

### 3. `app/downloader.py` — duration filter

a. Add next to `MemberOnlyError`:

```python
class TooLongError(Exception):
    """Raised when a video exceeds MAX_EPISODE_DURATION_MINUTES."""
```

b. Add a predicate near `_audio_ext()`:

```python
def _exceeds_duration_cap(duration) -> bool:
    """True if duration (seconds, possibly None) is over the configured cap.

    A missing duration is never "too long" — flat channel listings omit it for
    live streams and premieres, which the post-download check catches instead.
    """
    if not MAX_EPISODE_DURATION_MINUTES or not duration:
        return False
    return duration > MAX_EPISODE_DURATION_MINUTES * 60
```

c. `_download_entry(entry, channel_id, channel_name, *, enforce_duration: bool = True)`
   — after the `info = ydl.extract_info(...)` call succeeds and after the
   `expected_file` existence check, add the backstop:

```python
    if enforce_duration and _exceeds_duration_cap(info.get("duration")):
        # Flat channel listings omit duration for live streams/premieres, so
        # this is the only reliable point to catch them. Bin the file we just
        # paid for and let the caller remember not to try again.
        logger.info("Discarding %s — %ss exceeds the %d-minute cap",
                    video_id, info.get("duration"), MAX_EPISODE_DURATION_MINUTES)
        _remove_if_exists(expected_file)
        raise TooLongError(video_id)
```

`_remove_if_exists` is defined below `_download_entry` in the file; that's fine
at runtime (module-level resolution).

d. `_poll_channel_locked()` download loop — pre-check, placed immediately after
   the `availability` members-only block and **before** `considered += 1` (so a
   rejected video doesn't consume a cap slot, matching the members-only
   treatment):

```python
            if _exceeds_duration_cap(entry.get("duration")):
                logger.debug("Skipping over-long video: %s", video_id)
                if video_id:
                    db.add_skip_video(video_id, channel_id, "too_long")
                continue
```

   and a new `except` clause alongside `except MemberOnlyError:` on the
   `_download_entry` call:

```python
            except TooLongError:
                # Recorded so future polls skip it before spending the download.
                if video_id:
                    db.add_skip_video(video_id, channel_id, "too_long")
                considered -= 1
                continue
```

   (Order matters: put it before the broad `except Exception` clause.)

e. `download_single()` — exempt one-offs. Change the call to
   `_download_entry({"id": video_id}, channel_id, channel_name, enforce_duration=False)`
   and, just before it, warn when it's over the cap:

```python
    if _exceeds_duration_cap(info.get("duration")):
        logger.warning("One-off download of %s is longer than the %d-minute cap "
                       "— downloading anyway (explicit request)",
                       video_id, MAX_EPISODE_DURATION_MINUTES)
```

   Do not add a `TooLongError` handler here (it can't be raised with
   `enforce_duration=False`).

### 4. `app/downloader.py` — age-based retention in `_prune_channel()`

Add above `_prune_channel`:

```python
def _aged_out(ep) -> bool:
    """True if an episode's published date is past MAX_EPISODE_AGE_DAYS.

    An unparseable date is never treated as aged out — a bad timestamp must not
    silently delete audio.
    """
    if not MAX_EPISODE_AGE_DAYS:
        return False
    try:
        pub = datetime.fromisoformat(ep["published"])
    except (ValueError, TypeError):
        return False
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - pub).days > MAX_EPISODE_AGE_DAYS
```

Then in `_prune_channel`, replace `to_delete = episodes[MAX_EPISODES_PER_CHANNEL:]`
with the union of both caps, and record the reason per episode:

```python
    episodes = db.get_episodes(channel_id)
    over_cap = episodes[MAX_EPISODES_PER_CHANNEL:]
    over_cap_ids = {ep["id"] for ep in over_cap}
    # Both caps apply: an episode inside the count cap can still be too old.
    aged = [ep for ep in episodes[:MAX_EPISODES_PER_CHANNEL] if _aged_out(ep)]
    for ep, reason in [(e, "pruned") for e in over_cap] + [(e, "aged_out") for e in aged]:
        ...  # existing body, with db.add_skip_video(ep["id"], channel_id, reason)
```

Keep the existing explanatory comment about why skips are recorded, the
thumbnail deletion, and the trailing `_sweep_orphan_files(channel_id)` call
exactly as they are. (`over_cap_ids` is only needed if you prefer an explicit
dedup guard; the two slices are disjoint by construction, so it's optional —
don't add an unused variable.)

### 5. `app/downloader.py` — storage helpers

Next to the existing `_dir_bytes()` (keep it; `find_orphan_channels()` uses it):

```python
def channel_bytes(channel_id: str) -> int:
    """Bytes on disk for one channel — its audio plus its thumbnails."""
    return (_dir_bytes(os.path.join(AUDIO_DIR, channel_id))
            + _dir_bytes(os.path.join(THUMBNAIL_DIR, channel_id)))


def storage_usage() -> tuple[dict[str, int], int]:
    """Return (bytes per channel_id, total bytes) across every channel directory.

    The total covers every directory under AUDIO_DIR/THUMBNAIL_DIR, including
    channels the DB no longer owns (see find_orphan_channels), so it matches
    what the volume is actually holding rather than only what's subscribed.
    """
    ids: set[str] = set()
    for base in (AUDIO_DIR, THUMBNAIL_DIR):
        if os.path.isdir(base):
            ids.update(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))
    per_channel = {cid: channel_bytes(cid) for cid in ids}
    return per_channel, sum(per_channel.values())
```

Also refactor `find_orphan_channels()`'s `size = _dir_bytes(...) + _dir_bytes(...)`
line to `size = channel_bytes(cid)` (identical behavior, one definition).

### 6. `app/feed.py` — enclosure MIME from the filename

Add near the top (after the imports):

```python
# The enclosure type must describe the file we actually stored, not whatever
# AUDIO_CODEC currently says — a feed can hold .mp3 episodes downloaded before
# the setting changed alongside newer .opus ones. yt-dlp's opus output is an Ogg
# container (RFC 7845), which is audio/ogg; audio/opus is an RTP payload type,
# not a file type.
_ENCLOSURE_TYPES = {".mp3": "audio/mpeg", ".opus": "audio/ogg", ".ogg": "audio/ogg",
                    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
                    ".wav": "audio/wav"}


def _enclosure_type(filename: str) -> str:
    return _ENCLOSURE_TYPES.get(os.path.splitext(filename)[1].lower(), "audio/mpeg")
```

and change the enclosure line to
`fe.enclosure(audio_url, str(ep["filesize"] or 0), _enclosure_type(ep["filename"]))`.
`os` is already imported. Nothing else in `feed.py` changes.

### 7. `app/safety.py` — confirm only, no change

`_SAFE_MEDIA_NAME_RE = ^[A-Za-z0-9_-][A-Za-z0-9._-]*$` accepts `<id>.opus`
exactly as it accepts `<id>.mp3` — no extension is hardcoded anywhere in the
module. Leave the file untouched; add a test (task 10) proving it.

### 8. `app/main.py` — storage in `/api/state` and the page shell

a. Import `storage_usage` alongside the other downloader imports (~line 31).

b. In `api_state()`, before building the channel lists:

```python
    sizes, total_bytes = storage_usage()
```

   Add `"bytes": sizes.get(cid, 0) if cid else 0` to each subscribed-channel
   dict and `"bytes": sizes.get(cid, 0)` to each unsubscribed one, and
   `"total_bytes": total_bytes` to the top-level JSON payload (next to
   `"orphans"`). Guard the `storage_usage()` call the same way `find_orphan_channels()`
   is guarded — a disk read must never break the dashboard:

```python
    sizes, total_bytes = {}, 0
    try:
        sizes, total_bytes = storage_usage()
    except Exception:  # noqa: BLE001 — storage figures must never break the dashboard
        logger.exception("Failed to compute storage usage")
```

c. In `_PAGE`, in the subscribed section head (currently
   `<h2 id="subs-h">Subscribed channels <span id="subs-count" class="count-pill"></span></h2>`),
   add a sibling pill after the `</h2>`, inside the same `.section-head` div:

```html
                <span id="subs-storage" class="pill"></span>
```

   (`.pill` already exists in `styles.css` — it's what `#poll-interval` uses.)

### 9. `app/static/app.js` — dashboard readout

- Leave `fmtBytes` (line ~195) exactly where it is and reuse it; JS function
  declarations hoist, so callers earlier in the file are fine.
- Add a helper next to `epBadge`:

```js
function sizeBadge(ch) {
  if (!ch.bytes) return null;
  return el('span', { class: 'ep-badge', title: 'Audio + thumbnails on disk', text: fmtBytes(ch.bytes) });
}
```

  (`el()` already drops `null` children, and `.ch-sub` already accepts a null
  from `lastPollBadge`.)
- `subscribedCard`: `el('div', { class: 'ch-sub' }, [epBadge(ch), sizeBadge(ch), lastPollBadge(ch)])`
- `oneoffCard`: `el('div', { class: 'ch-sub' }, [epBadge(ch), sizeBadge(ch)])`
- In `render()`, right after `$('#subs-count').textContent = d.channels.length;`:

```js
  $('#subs-storage').textContent = d.total_bytes ? `${fmtBytes(d.total_bytes)} on disk` : '';
```

No CSS changes needed.

### 10. Tests

All new tests follow the existing fixtures/monkeypatch style. Run with
`.venv/bin/python -m pytest -q` from the repo root.

**`tests/test_polling.py`** (module already imports `db, downloader`, has
`CID`, `_setup_tmp`, `_ep`, `_stub_poll_io`):

1. `test_ydl_opts_defaults_unchanged` — monkeypatch `downloader.AUDIO_CODEC` to
   `"mp3"` and `downloader.AUDIO_BITRATE_KBPS` to `"128"` (the defaults),
   assert `_ydl_opts(CID)["postprocessors"] == [{"key": "FFmpegExtractAudio",
   "preferredcodec": "mp3", "preferredquality": "128"}]` and that
   `outtmpl` still ends with `"%(id)s.%(ext)s"`. (Acceptance 2.)
2. `test_ydl_opts_honours_codec_and_bitrate` — monkeypatch to `"opus"`/`"64"`,
   assert `preferredcodec == "opus"`, `preferredquality == "64"`, and
   `downloader._audio_ext() == "opus"`. (Acceptance 1.)
3. `test_unknown_codec_falls_back_to_mp3` — monkeypatch `AUDIO_CODEC` to
   `"flurble"`, assert `_audio_ext() == "mp3"` and `preferredcodec == "mp3"`.
4. `test_download_entry_skips_existing_file_in_other_format` — with
   `AUDIO_CODEC="opus"`, touch `<audio_dir>/vAAAAAAAAAA.mp3`, call
   `_download_entry({"id": "vAAAAAAAAAA"}, CID, "C")`, assert it returns `None`
   without invoking yt-dlp (monkeypatch `downloader.yt_dlp.YoutubeDL` to a
   raising sentinel to prove it isn't called).
5. `test_prune_removes_aged_out_episodes` — seed ~5 episodes with `published`
   spread across e.g. 1, 5, 40, 400 days ago (compute with
   `datetime.now(timezone.utc) - timedelta(days=n)`), set
   `downloader.MAX_EPISODES_PER_CHANNEL` high (so the count cap is inert) and
   `downloader.MAX_EPISODE_AGE_DAYS = 30`; assert only the recent ones remain,
   that the removed ids are in `db.get_skip_video_ids(CID)`, and that their
   audio files are gone from disk. (Acceptance 3.)
6. `test_prune_age_disabled_by_default` — same seeding with
   `MAX_EPISODE_AGE_DAYS = 0`, assert nothing is removed.
7. `test_prune_applies_both_caps` — over-cap *and* aged episodes present;
   assert both sets are gone and both reasons recorded (query
   `db.get_skip_video_ids` for presence; reasons can be checked with a direct
   `sqlite3` read of `skip_videos` if you want it precise).
8. `test_poll_skips_over_long_entry_before_download` — entries carry
   `"duration"`; set `downloader.MAX_EPISODE_DURATION_MINUTES = 30`; use
   `_stub_poll_io` and assert the 7200s entry never reaches `_download_entry`,
   is in `db.get_skip_video_ids(CID)` and stays skipped on a second poll.
   (Acceptance 4, pre-download path.)
9. `test_poll_discards_over_long_video_after_download` — entry with
   `duration=None` (the live-stream case); monkeypatch `_download_entry` to a
   stub that raises `downloader.TooLongError("v000")`; assert the poll
   completes, records the `too_long` skip, and stores no episode.
10. `test_download_entry_deletes_over_long_file` — exercise the real
    `_download_entry`: monkeypatch `downloader.yt_dlp.YoutubeDL` to a fake that,
    on `extract_info(..., download=True)`, creates
    `<audio_dir>/vAAAAAAAAAA.mp3` and returns `{"id": ..., "duration": 7200,
    "title": "x", "upload_date": "20260101"}`; with the cap at 30 minutes assert
    `TooLongError` is raised and the file is gone from disk.
11. `test_download_single_ignores_duration_cap` — one-off path still downloads
    an over-long video (asserts the documented exemption).

**Existing test to update:** `test_download_single_prunes_over_cap_channel`
monkeypatches `_download_entry` with `lambda entry, cid, cname: _ep(5, cid)`.
`download_single` now passes `enforce_duration=False`, so change that stub to
`lambda entry, cid, cname, **_kw: _ep(5, cid)`. Grep for every other
`_download_entry` stub (`_fake_download` in `test_poll_does_not_redownload_pruned_video`
and in `_stub_poll_io`) — those are called by `poll_channel`, which passes no
kwarg, so they can stay, but adding `**_kw` to all of them is harmless and
future-proof.

**`tests/test_feed.py`:**

12. `test_feed_enclosure_type_matches_extension` — seed one `.mp3` and one
    `.opus` episode in the same channel, build the feed, assert the two items'
    `enclosure/@type` are `audio/mpeg` and `audio/ogg` respectively and both
    URLs are well-formed. (Acceptance criterion under Constraints: mixed
    formats in one channel.)
13. `test_feed_unknown_extension_defaults_to_audio_mpeg` — a `.weird` filename
    still yields `audio/mpeg` (and is still emitted, since `is_safe_media_name`
    accepts it).

**`tests/test_safety.py`:**

14. `test_opus_filename_is_safe` — `is_safe_media_name("vAAAAAAAAAA.opus")` is
    `True` (alongside the existing `.mp3` cases).

**`tests/test_endpoints.py`:**

15. Extend `test_api_state_shape` with `"total_bytes"` in the key list.
16. `test_api_state_reports_storage` — point `db.DB_PATH`,
    `downloader.AUDIO_DIR`, `downloader.THUMBNAIL_DIR` at `tmp_path`
    (`main` imports `storage_usage` by name, so monkeypatch the module-level
    dirs on `downloader` — verify at write time whether `main` resolves them
    through `downloader`; if `main` calls the imported function, patching
    `downloader.AUDIO_DIR` is sufficient because the function reads the
    globals at call time). Add a channel + write files of known byte length
    (e.g. 1000 bytes of audio, 500 of thumbnail), call `main.api_state()`,
    parse the JSON body and assert the channel's `"bytes" == 1500` and
    `"total_bytes" == 1500`. (Acceptance 5.)

**`tests/conftest.py`:** the yt_dlp stub's `_YoutubeDL.extract_info` returns
`{}` and takes `*a, **k` — sufficient for everything above. No change needed
unless a new test wants a richer default; prefer per-test fakes (the existing
`_FakeYDL` pattern in `tests/test_polling.py`) over touching the shared stub.

### 11. Version, changelog, README, compose

- `app/__init__.py`: read the current value at execution time (`cat
  app/__init__.py`) and bump the MINOR component — if it still says `1.11.0`,
  set `1.12.0`; if Group 1 merged and bumped it, bump from whatever is there.
  Do not hardcode `1.12.0` blindly.
- `app/changelog.py`: prepend a new entry `{"version": <the version you just
  set>, "date": "2026-09-03"}` (or the actual run date), newest-first, with 4-5
  full-sentence, user-facing bullets in the existing register — configurable
  codec/bitrate and what it costs (app compatibility, mixed feeds), age-based
  retention, the max-duration filter (including the one-off exemption), the
  dashboard storage readout, and one line noting per-channel overrides aren't
  supported yet. `tests/test_changelog.py` enforces uniqueness, ordering, and
  that the shipping version has an entry.
- `README.md`:
  - Configuration table: add `AUDIO_CODEC` (`mp3`), `AUDIO_BITRATE_KBPS`
    (`128`), `MAX_EPISODE_AGE_DAYS` (`0` — disabled), and
    `MAX_EPISODE_DURATION_MINUTES` (`0` — disabled).
  - "Important notes": a bullet that changing `AUDIO_CODEC` mid-flight is safe
    but produces a feed mixing `.mp3` and `.opus` episodes (both valid
    enclosures — old files are never re-encoded), that Opus isn't universally
    supported by podcast apps (Apple Podcasts, Pocket Casts), and that these
    caps are global — **per-channel overrides are not yet supported**.
  - "How It Works" step 3: "downloaded as MP3 (128 kbps by default — see
    `AUDIO_CODEC`/`AUDIO_BITRATE_KBPS`)"; step 5: mention `MAX_EPISODE_AGE_DAYS`
    alongside the count cap; add the duration filter to step 2 (Filtering).
  - API table: change `/audio/<channel_id>/<file>.mp3` to
    `/audio/<channel_id>/<file>` (extension depends on `AUDIO_CODEC`).
  - Data layout block: `<video_id>.mp3` → `<video_id>.mp3` *(or `.opus`)*.
  - Management UI section: mention the per-channel size badge and the total
    "on disk" pill.
- `docker-compose.yml`: add the four vars near `MAX_EPISODES_PER_CHANNEL`, each
  with a one-line comment, defaulted to today's behavior
  (`AUDIO_CODEC=mp3`, `AUDIO_BITRATE_KBPS=128`, `MAX_EPISODE_AGE_DAYS=0`,
  `MAX_EPISODE_DURATION_MINUTES=0`). Purely additive — don't reformat
  neighboring lines (Group 1 may have touched this file).

### 12. Verify & deploy

```bash
cd /home/eric/projects/slipcast
.venv/bin/python -m pytest -q
.venv/bin/python -c "import ast,sys; ast.parse(open('app/main.py').read())"   # sanity
node --check app/static/app.js                                               # JS syntax
docker compose build && docker compose up -d
curl -su "$AUTH_USER:$AUTH_PASS" localhost:8000/api/state | python3 -m json.tool | head -40
```

Confirm `total_bytes` and per-channel `bytes` look plausible against
`du -sb data/audio/* data/thumbnails/*`, then open the dashboard and check the
badges render. Commit on `feat/storage-retention`; open a PR against `main`
(do not merge Group 1's branch into it).

---

## Data / model / API changes

- **No schema migration.** `episodes`, `channels`, `skip_videos`, `poll_runs`
  are untouched. `skip_videos.reason` gains two new *values* — `"aged_out"`,
  `"too_long"` — but it's a free-text column already holding `"pruned"` and
  `"members_only"`.
- **`GET /api/state`** gains:
  - `channels[].bytes: int` — audio + thumbnails for that channel_id.
  - `unsubscribed[].bytes: int` — same.
  - `total_bytes: int` — sum across every channel directory on disk, including
    orphaned ones.
  No fields are removed or renamed; no other endpoint changes.
- **New env vars** (all optional, all defaulting to current behavior):
  `AUDIO_CODEC` (`mp3`), `AUDIO_BITRATE_KBPS` (`128`), `MAX_EPISODE_AGE_DAYS`
  (`0`), `MAX_EPISODE_DURATION_MINUTES` (`0`).
- **New public function** `downloader.storage_usage() -> tuple[dict[str,int], int]`
  and `downloader.channel_bytes(channel_id) -> int`.
- **New exception** `downloader.TooLongError`.
- **Changed signature** `_download_entry(entry, channel_id, channel_name, *,
  enforce_duration=True)` (private; one existing test stub needs `**_kw`).

---

## Testing & verification — acceptance criteria map

| Brief AC | Proven by |
|---|---|
| 1. opus/64k options + extension + MIME | `test_ydl_opts_honours_codec_and_bitrate`, `test_feed_enclosure_type_matches_extension` |
| 2. defaults byte-identical | `test_ydl_opts_defaults_unchanged` (regression guard) |
| 3. age-based prune + skip record | `test_prune_removes_aged_out_episodes`, `test_prune_age_disabled_by_default`, `test_prune_applies_both_caps` |
| 4. duration filter + no re-attempt | `test_poll_skips_over_long_entry_before_download`, `test_poll_discards_over_long_video_after_download`, `test_download_entry_deletes_over_long_file`, `test_download_single_ignores_duration_cap` |
| 5. `/api/state` byte figures | `test_api_state_reports_storage`, `test_api_state_shape` |
| 6. dashboard renders figures | `node --check app/static/app.js` + reading the diff against the `orphanCard`/`fmtBytes` precedent; confirm live in step 12 |
| 7. suite green | `.venv/bin/python -m pytest -q` |
| 8. version/changelog/README | `tests/test_changelog.py` (version has an entry, ordering, uniqueness) + manual README review |
| 9. local deploy | `docker compose build && docker compose up -d`, then the `curl`/`du` cross-check and a dashboard look |

Mixed-format constraint from the brief is covered by
`test_feed_enclosure_type_matches_extension` (one channel, `.mp3` + `.opus` in
one `get_episodes()` result) and `test_opus_filename_is_safe`.

---

## Risks & watch-outs

1. **Opus at the wrong bitrate (silent).** `FFmpegExtractAudioPP.run()` takes a
   *lossless copy* path when the source stream's codec already equals the
   target (`target_format == filecodec`) — a WebM/Opus source with
   `AUDIO_CODEC=opus` is copied, not re-encoded, so `AUDIO_BITRATE_KBPS` has no
   effect on it. Our format selector prefers `bestaudio[ext=m4a]` (AAC) first,
   which does get re-encoded, but don't be surprised if some opus files come out
   at YouTube's own bitrate. Do **not** "fix" this by changing the format
   selector — that risks the n-challenge/format-availability breakage the
   Dockerfile comment warns about. Mention it in the README bullet if you like;
   don't engineer around it.
2. **Don't remove `.opus` from `_YTDLP_PRECONVERT_SUFFIXES`** — see the decision
   above. It looks wrong under `AUDIO_CODEC=opus` and is actually the safe
   configuration.
3. **Codec flip must not re-download the library** — the multi-extension
   existence check in `_download_entry` is the guard. Test 4 exists specifically
   for this.
4. **`_audio_ext()`/`_exceeds_duration_cap()`/`_aged_out()` must read module
   globals at call time**, not bind config at import. Every test in this repo
   monkeypatches `downloader.<CONST>`; a captured constant makes them silently
   pass against the wrong value.
5. **Ordering in the poll loop.** The duration pre-check goes after the
   `availability` skip and before `considered += 1`; the `TooLongError` handler
   goes before the broad `except Exception`. Getting either wrong either
   swallows the skip into the generic failure path (and emails a false poll
   failure) or lets over-long videos consume cap slots.
6. **Never delete on an unparseable date.** `_aged_out()` returns `False` on a
   bad `published` value. `test_feed_survives_bad_published_date` shows such
   rows exist in the wild.
7. **`_prune_channel` runs three times per poll** (before fetch, after fetch,
   in `finally`) and once per one-off download — age pruning inherits that, so
   keep `_aged_out()` cheap and side-effect free.
8. **`/api/state` disk cost.** The dashboard refreshes every 9s and now
   `listdir`s every channel directory (on top of `find_orphan_channels()`'s
   existing scan). Fine at this scale; if it ever bites, the follow-up is a
   ~30-second TTL memo in `storage_usage()` — not part of this pass.
9. **Group boundaries.** Do not touch `app/notify.py`'s alert functions,
   `/health`, disk-pressure pruning in `poll_all()`, or backup code in
   `app/database.py` — Group 1 owns them and may be mid-flight. Note that
   `MIN_FREE_DISK_GB` already exists in `app/config.py` and
   `docker-compose.yml` on `main` but is unused in committed code; leave it
   alone.
10. **Version bump is read-then-increment**, never hardcoded — Group 1 may land
    a bump first.
11. **`epBadge` returns a `<button>` when episodes exist**; `sizeBadge` must be
    a separate `<span>`, not folded into that button's text, or clicking the
    size opens the episodes modal in a confusing way.

---

## Out of scope (do not build)

- **True per-channel caps/settings** — no `channels.max_episodes` column, no
  per-channel codec, no per-channel UI control. Global env vars only. Say so
  explicitly in the changelog and README ("per-channel overrides are not yet
  supported").
- Re-encoding, renaming, or migrating any already-downloaded audio.
- Any UI control for codec / age / duration — these are env-var-only, exactly
  like `MAX_EPISODES_PER_CHANNEL` and `POLL_INTERVAL_HOURS`. The storage
  readout is read-only.
- Group 1 territory: `app/notify.py` alert plumbing, `/health*`, disk-pressure
  pruning, database backups.
- Group 3 (feed tokens, combined feed, episode-level management, per-channel
  feed metadata) and Group 4 (identity/schema migration).
- New browser-automation test infrastructure for the dashboard change.
