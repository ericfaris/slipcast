"""Human-curated release notes shown in the dashboard's version dialog.

This ships inside the container (unlike git history, which isn't copied into
the image), so it's the source of truth for "what changed in each version".
Keep the newest release first, and update it alongside ``app.__version__``
whenever you cut a release. ``date`` is the release (commit/tag) date.
"""

CHANGELOG = [
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
