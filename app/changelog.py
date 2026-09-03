"""Human-curated release notes shown in the dashboard's version dialog.

This ships inside the container (unlike git history, which isn't copied into
the image), so it's the source of truth for "what changed in each version".
Keep the newest release first, and update it alongside ``app.__version__``
whenever you cut a release. ``date`` is the release (commit/tag) date.
"""

CHANGELOG = [
    {
        "version": "1.14.0",
        "date": "2026-09-03",
        "changes": [
            "Feed URLs can now be protected with an access token. Every feed URL used to be guessable — it's just /feed/<channel_id>.xml, and the channel ID is YouTube's own public one — so anyone who worked one out could pull your audio. Each channel now has a secret token that the dashboard automatically appends to the URL you copy. Enforcement is OFF by default so nothing breaks on upgrade: set REQUIRE_FEED_TOKENS=true and restart to require it, and because the token was already embedded in the URLs you copied earlier, feeds already subscribed in your podcast app keep working. This guards against someone guessing a feed URL — it is not real per-listener authentication, since the token travels in the URL like every other private podcast feed.",
            "New combined feed at /feed/all.xml: every subscribed channel merged into one podcast, newest episode first, capped by the new ALL_FEED_MAX_EPISODES setting (default 100). One-off downloads are deliberately left out. There's a 'Share all-channels feed' button next to the Subscribed channels heading, with the same QR code and copy affordances as a per-channel feed.",
            "Individual episodes can now be deleted or re-downloaded from the episode list. Previously a single corrupt or truncated download could only be fixed by removing and re-adding the whole channel. Delete removes the audio, the thumbnail, and the database row — and unlike an automatic prune, it does not blacklist the video, so the next poll can pick it up again. Re-download forces a fresh fetch even when the file is already on disk, which is the fix for a file that downloaded incompletely.",
            "Feeds can now carry per-channel iTunes metadata. Every feed was previously hardcoded to category 'Technology', language 'en', and explicit 'no' regardless of what the channel actually is. A new 'Feed settings' button on each subscribed channel opens a category / language / explicit form; anything you leave blank keeps today's default, so nothing changes until you set something.",
        ],
    },
    {
        "version": "1.13.0",
        "date": "2026-09-03",
        "changes": [
            "Audio codec and bitrate are now configurable: new AUDIO_CODEC (default 'mp3') and AUDIO_BITRATE_KBPS (default '128') env vars. Setting AUDIO_CODEC=opus roughly halves file size at equivalent quality, but Opus isn't universally supported by podcast apps (notably Apple Podcasts and Pocket Casts) — check your app before switching. Changing this only affects new downloads: existing .mp3 episodes are never re-encoded, so a feed can end up mixing .mp3 and .opus items, which is fine (both are valid enclosures) but worth expecting.",
            "New MAX_EPISODE_AGE_DAYS setting (default 0, disabled) prunes episodes older than N days, on top of the existing MAX_EPISODES_PER_CHANNEL count cap — either cap can drop an episode independently, and dropped episodes are remembered so they're never re-downloaded.",
            "New MAX_EPISODE_DURATION_MINUTES setting (default 0, disabled) skips videos longer than N minutes during channel polls — aimed at multi-hour livestreams that can otherwise eat most of a channel's disk budget on their own. Most over-long videos are caught before downloading (channel listings usually carry a duration); live streams and premieres that don't report one are caught right after download instead, with the file deleted immediately. One-off (single-video) downloads are exempt from this cap, since they're an explicit request — they log a warning and download anyway.",
            "The dashboard now shows disk usage: a per-channel size badge next to the episode count, and a total 'on disk' figure for all subscribed channels, both computed live from the audio and thumbnail directories.",
            "These are global settings only — per-channel overrides (different caps or codec per channel) are not yet supported.",
        ],
    },
    {
        "version": "1.12.0",
        "date": "2026-09-03",
        "changes": [
            "Closed the last two ways a poll could hang forever: downloading a thumbnail and converting it with ffmpeg now both time out (30s and 60s). Neither had any timeout at all, so a thumbnail server that accepted the connection and then went quiet could wedge the scheduler indefinitely — the same silent, log-free failure as the multi-week outage fixed in 1.10.0, just reached by a different route. A thumbnail that times out is now logged and skipped; the episode still downloads.",
            "New /health/live endpoint: a deliberately narrow report of only the things a restart could plausibly fix (is the scheduler running, has polling stalled). /health is unchanged and remains the full picture, including cookie validity — but because expired cookies are not something restarting repairs, they no longer make the container look restart-worthy. The Docker healthcheck now watches /health/live instead of /health.",
            "Slipcast can now restart itself when it wedges. A host-side timer (ops/autoheal.sh, installed manually — see ops/README.md) checks /health/live every five minutes and restarts the container when it fails, at most 3 times per hour. After that it stops trying and emails you instead of restarting in a loop, and it keeps reporting until the problem is dealt with. It sends that email directly over SMTP, so it still reaches you when the app itself is down.",
            "Slipcast now protects itself against a full disk. Per-channel episode caps never accounted for the size of the volume all channels share, so audio could grow until the disk filled — which produces confusing mid-write failures rather than a clear error. When free space drops below the new MIN_FREE_DISK_GB setting (default 2 GB), the oldest episodes across all channels are deleted before polling, just enough to get back above the line, and you always get an email listing exactly what was removed. Set it to 0 to turn the check off.",
            "The database is now backed up nightly at 03:00 to DATA_DIR/backups/, keeping the last seven snapshots, and checked for corruption on the way. If the check fails or a backup can't be taken, you get an email. Restoring is a documented manual step (see the README) rather than something Slipcast does on its own.",
        ],
    },
    {
        "version": "1.11.0",
        "date": "2026-09-03",
        "changes": [
            "Fixed a data-integrity bug where removing a channel could, in some cases (a URL variant, or a channel removed before its first successful poll), leave its downloaded episodes and audio/thumbnail files behind forever with no channel row pointing at them — invisible in the dashboard but still served at a live feed URL. Slipcast now checks for this kind of orphaned data on every startup (logged, never auto-deleted) and lists anything found in a new 'Orphaned data' section on the dashboard, with a one-click Delete.",
            "One-off (unsubscribed) video downloads can now be removed from the dashboard — previously the only option was to subscribe to them, so they could never be deleted and their audio could grow without limit. They're also now capped at the same MAX_EPISODES_PER_CHANNEL as subscribed channels.",
            "Fixed a bug where polling the same channel from two places at once (the scheduled poll and a manual 'poll now', or two overlapping 'poll all' runs) could delete each other's in-progress downloads. Each channel now polls exclusively — a second concurrent attempt is skipped with a clear 'already polling' message instead of corrupting the first one's work.",
            "'Poll all' and 'poll selected' now run through a bounded worker pool (new POLL_CONCURRENCY env var, default 2) instead of spawning one unbounded thread per channel.",
            "The database now uses WAL mode and a busy-timeout, so concurrent poll writes no longer risk a 'database is locked' error.",
            "/health now reports real status (HTTP 503 + a breakdown of what's wrong) when the scheduler has stopped, polling has gone stale, or cookies are missing/expired — previously it always reported healthy, including throughout the multi-week silent-polling outage fixed in 1.10.0. Wired up to a Docker HEALTHCHECK.",
            "Dashboard load is faster on installs with many episodes: episode counts are now computed with a single grouped query instead of loading every episode row per channel.",
        ],
    },
    {
        "version": "1.10.0",
        "date": "2026-09-01",
        "changes": [
            "Polling is now resilient to single-video failures: when one video won't download (a transient YouTube 403, a yt-dlp/ffmpeg hiccup), the poll logs it and moves on instead of aborting that channel — and, previously, every channel queued after it.",
            "New 'silent failure' email alert: if a poll finishes but some videos quietly failed to download (with cookies still valid), you get an email listing the affected channels so gaps don't go unnoticed for weeks.",
            "Added a 30-second network timeout to all yt-dlp operations. A stalled download could previously hang the scheduler indefinitely — with no error and no log output — blocking every future poll until a restart.",
            "Scheduled-job crashes are now logged loudly, and the poll schedule tolerates a slow run instead of wedging permanently.",
        ],
    },
    {
        "version": "1.9.3",
        "date": "2026-07-16",
        "changes": [
            "Fixed a bug where some channel listings included a non-video entry whose ID looked like a video ID but was actually the channel's own ID, causing a confusing 'Video unavailable' error on every poll. Video IDs are now validated against YouTube's actual 11-character format before a download is attempted.",
        ],
    },
    {
        "version": "1.9.2",
        "date": "2026-07-15",
        "changes": [
            "Fixed a bug where adding a channel via a YouTube share link (containing a `?si=...` tracking parameter) silently downloaded zero episodes, forever — the tracking query broke the URL used to fetch the channel's video list. New channels now correctly backfill their most recent episodes regardless of how the link was copied.",
        ],
    },
    {
        "version": "1.9.1",
        "date": "2026-07-14",
        "changes": [
            "Pruning a channel now also deletes the dropped episode's thumbnail, and each poll sweeps away any audio/thumbnail files the database no longer references (leftovers from interrupted prunes or older versions that never cleaned up thumbnails). Only the current episodes' files stay on disk.",
        ],
    },
    {
        "version": "1.9.0",
        "date": "2026-07-11",
        "changes": [
            "New Slipcast logo and full favicon set (browser tabs, iOS home screen, high-res app icons) plus a web app manifest so the dashboard installs as a standalone app.",
            "Refreshed the brand palette to match the new logo, with a red-to-purple gradient on channel avatars.",
            "Feeds without their own artwork now fall back to the branded Slipcast cover image.",
        ],
    },
    {
        "version": "1.8.3",
        "date": "2026-06-27",
        "changes": [
            "Hardening: episode/feed media filenames are now validated before being used in URLs, so a malformed stored name can never produce a path-traversal link.",
        ],
    },
    {
        "version": "1.8.2",
        "date": "2026-06-27",
        "changes": [
            "Channel and episode thumbnails (and the episode player) now load over whatever host you open the dashboard on — previously they could be blocked by the content security policy when not using the public URL.",
        ],
    },
    {
        "version": "1.8.1",
        "date": "2026-06-27",
        "changes": [
            "Removed the duplicate \"Poll all\" button from the header — the polling dashboard's \"Poll all now\" already covers it.",
        ],
    },
    {
        "version": "1.8.0",
        "date": "2026-06-27",
        "changes": [
            "Get an email a week before your cookies expire (configurable via COOKIE_EXPIRY_WARN_DAYS), while they still work — so you can refresh before polling ever stops.",
            "This advance warning is separate from the existing 'cookies broken' alert, so the two don't suppress each other.",
        ],
    },
    {
        "version": "1.7.0",
        "date": "2026-06-27",
        "changes": [
            "The cookies card now shows a concrete expiry date parsed from your cookies.txt, plus a countdown, so you know the hard deadline before polls fail.",
            "Warns when cookies expire within 7 days, and flags already-expired cookies in red.",
        ],
    },
    {
        "version": "1.6.0",
        "date": "2026-06-27",
        "changes": [
            "New polling dashboard: a countdown to the next poll, overall health, and a log of recent poll runs (per channel, with new-episode counts and errors).",
            "Each subscribed channel now shows the status of its last poll.",
            "Polls only consider a channel's newest videos, eliminating wasteful download-then-prune churn that could briefly push a channel over its episode cap.",
        ],
    },
    {
        "version": "1.5.0",
        "date": "2026-06-27",
        "changes": [
            "Click the version number to view this changelog, with release dates and the running build.",
        ],
    },
    {
        "version": "1.4.1",
        "date": "2026-06-27",
        "changes": [
            "Enforce the per-channel episode cap even when a poll's fetch fails (e.g. expired cookies), so channels no longer drift over the limit.",
            "Cap the RSS feed itself as a safety net so podcast apps never see more than the limit.",
            "Fix a crash that could blank the feed for channels without cover art.",
        ],
    },
    {
        "version": "1.4.0",
        "date": "2026-06-27",
        "changes": [
            "Browse a channel's downloaded episodes in a modal, with inline playback.",
        ],
    },
    {
        "version": "1.3.1",
        "date": "2026-06-27",
        "changes": [
            "Enforce a per-channel episode cap and fast-skip members-only videos.",
        ],
    },
    {
        "version": "1.3.0",
        "date": "2026-06-27",
        "changes": [
            "Dashboard overhaul: channel cards, live progress, search, bulk actions, and QR feed sharing.",
        ],
    },
    {
        "version": "1.2.5",
        "date": "2026-06-25",
        "changes": [
            "Harden the app against security-review findings.",
        ],
    },
    {
        "version": "1.2.2",
        "date": "2026-06-25",
        "changes": [
            "Rebrand from YouTube RSS to Slipcast.",
        ],
    },
]
