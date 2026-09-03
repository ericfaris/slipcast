import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
AUDIO_DIR = os.path.join(DATA_DIR, "audio")
THUMBNAIL_DIR = os.path.join(DATA_DIR, "thumbnails")
DB_PATH = os.path.join(DATA_DIR, "episodes.db")

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

