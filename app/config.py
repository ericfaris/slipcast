import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
THUMBNAIL_DIR = os.path.join(DATA_DIR, "thumbnails")
DB_PATH = os.path.join(DATA_DIR, "episodes.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASS = os.environ.get("AUTH_PASS", "")

# Multi-user: AUTH_USERS=alice:pass1,bob:pass2 (takes precedence over AUTH_USER/AUTH_PASS)
_raw_users = os.environ.get("AUTH_USERS", "")
if _raw_users:
    AUTH_CREDENTIALS: list[tuple[str, str]] = [
        (e.strip().split(":", 1)[0], e.strip().split(":", 1)[1])
        for e in _raw_users.split(",")
        if ":" in e.strip()
    ]
elif AUTH_USER and AUTH_PASS:
    AUTH_CREDENTIALS = [(AUTH_USER, AUTH_PASS)]
else:
    AUTH_CREDENTIALS = []
MAX_EPISODES_PER_CHANNEL = int(os.environ.get("MAX_EPISODES_PER_CHANNEL", "20"))
POLL_INTERVAL_HOURS = int(os.environ.get("POLL_INTERVAL_HOURS", "6"))
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/data/cookies.txt")
# Max channels polled at once by "poll all"/"poll selected". A thread per
# channel with no cap made it trivial to fire dozens of concurrent yt-dlp
# processes; bound it with a worker pool instead.
POLL_CONCURRENCY = int(os.environ.get("POLL_CONCURRENCY", "2"))
# Audio grows without bound across all channels combined — MAX_EPISODES_PER_CHANNEL
# caps each channel individually but nothing accounts for the size of the volume
# they all share. When free space on the DATA_DIR filesystem drops below this many
# GB, poll_all() prunes the globally oldest episodes (across channels) before
# downloading anything more. Set to 0 to disable the check entirely.
MIN_FREE_DISK_GB = int(os.environ.get("MIN_FREE_DISK_GB", "2"))

# --- Email alerts (cookie expiry / invalid cookies) ---------------------------
# Configure SMTP to receive an email when the cookies file needs to be re-uploaded.
# For Gmail: SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USER=<you>@gmail.com,
# SMTP_PASS=<app password> (https://myaccount.google.com/apppasswords).
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "ericfaris@gmail.com")
# Don't re-send the same alert more often than this.
ALERT_COOLDOWN_HOURS = int(os.environ.get("ALERT_COOLDOWN_HOURS", "24"))
# Warn by email this many days before the cookies file's parsed expiry date.
COOKIE_EXPIRY_WARN_DAYS = int(os.environ.get("COOKIE_EXPIRY_WARN_DAYS", "7"))

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

# --- Feed access / combined feed ----------------------------------------------
# Feed URLs are public by design (podcast apps can't send Basic Auth), which
# also means /feed/<channel_id>.xml is guessable — channel_id is YouTube's own
# public ID. When this is on, every feed request must carry the matching
# ?token=<feed_token>. It defaults to OFF so turning Slipcast up on an existing
# install doesn't silently break feeds already subscribed in a podcast app;
# tokens are still generated and embedded in every dashboard-copied URL either
# way, so flipping this on later needs no migration and doesn't invalidate URLs
# already saved (their embedded token is the same one).
REQUIRE_FEED_TOKENS = os.environ.get("REQUIRE_FEED_TOKENS", "").strip().lower() in ("1", "true", "yes", "on")
# Cap on items in the combined /feed/all.xml. Unlike the per-channel feed
# (MAX_EPISODES_PER_CHANNEL), this one merges every subscribed channel, so
# without a cap it would grow with the whole library.
ALL_FEED_MAX_EPISODES = int(os.environ.get("ALL_FEED_MAX_EPISODES", "100"))
