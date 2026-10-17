"""Runtime-editable settings, changed from the dashboard's Settings dialog.

Each setting here is still defined (with its env-var default) in app/config.py;
this module layers a DB override on top. Overrides live in the `settings` table
under ``config:<NAME>`` and are applied by assigning to the ``app.config``
module attribute, so every reader must look the value up as ``config.NAME`` at
use time — a ``from app.config import NAME`` would freeze the env value and
silently ignore the dashboard.

Saving a value equal to the env default deletes the override rather than
storing a copy, so a later .env change still takes effect for anything the
dashboard hasn't actually customised.

Secrets (SMTP, R2, auth) and anything that only makes sense at startup
(storage mode, BASE_URL) are deliberately not editable here.
"""
import logging
import re
import threading
from dataclasses import dataclass

from app import config
from app import database as db

logger = logging.getLogger(__name__)

_KEY_PREFIX = "config:"
# Loose on purpose — this only guards against obvious typos in the dashboard;
# the SMTP server is the real validator.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_lock = threading.Lock()


@dataclass(frozen=True)
class Spec:
    name: str
    group: str
    label: str
    help: str
    kind: str  # "int" | "bool" | "email"
    min: int = 0
    max: int = 0


SPECS: tuple[Spec, ...] = (
    Spec("POLL_INTERVAL_HOURS", "Polling & downloads", "Poll every (hours)",
         "How often every subscribed channel is checked for new videos. Changing this restarts the countdown.",
         "int", 1, 168),
    Spec("POLL_CONCURRENCY", "Polling & downloads", "Channels polled at once",
         "Upper bound on simultaneous channel polls for Poll all / Poll selected.",
         "int", 1, 8),
    Spec("MAX_EPISODE_DURATION_MINUTES", "Polling & downloads", "Skip videos longer than (minutes)",
         "Channel polls skip anything longer, e.g. multi-hour livestreams. One-off downloads are exempt. 0 = no limit.",
         "int", 0, 1440),
    Spec("MAX_EPISODES_PER_CHANNEL", "Retention", "Episodes kept per channel",
         "Older episodes beyond this count are deleted after each poll.",
         "int", 1, 1000),
    Spec("MAX_EPISODE_AGE_DAYS", "Retention", "Delete episodes older than (days)",
         "Applies on top of the per-channel count. 0 = never delete by age.",
         "int", 0, 3650),
    Spec("MIN_FREE_DISK_GB", "Retention", "Minimum free disk (GB)",
         "Below this, the oldest episodes across all channels are deleted before polling. Ignored when media is stored in R2. 0 = off.",
         "int", 0, 10000),
    Spec("REQUIRE_FEED_TOKENS", "Feeds & alerts", "Require feed tokens",
         "Feeds only answer URLs carrying their ?token=. URLs copied from this dashboard already include it.",
         "bool"),
    Spec("ALL_FEED_MAX_EPISODES", "Feeds & alerts", "Combined feed size (episodes)",
         "Newest-first cap on the all-channels feed.",
         "int", 1, 1000),
    Spec("ALERT_EMAIL", "Feeds & alerts", "Alert email",
         "Where cookie, poll-failure, disk and backup alerts go. Blank = no alerts. SMTP itself is configured in .env.",
         "email"),
    Spec("COOKIE_EXPIRY_WARN_DAYS", "Feeds & alerts", "Warn before cookie expiry (days)",
         "Email this many days before the uploaded cookies file expires.",
         "int", 0, 90),
    Spec("ALERT_COOLDOWN_HOURS", "Feeds & alerts", "Alert cooldown (hours)",
         "Minimum gap between repeats of the same alert email.",
         "int", 1, 720),
)
_BY_NAME = {s.name: s for s in SPECS}

# The env-derived values, captured before any override is applied. These are
# what "default" means in the dialog and what a reset returns to.
DEFAULTS = {s.name: getattr(config, s.name) for s in SPECS}


def parse(name: str, raw) -> object:
    """Validate one submitted value; raises ValueError with a user-facing message."""
    spec = _BY_NAME.get(name)
    if spec is None:
        raise ValueError(f"Unknown setting: {name}")
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"{spec.label} must be on or off")
    if spec.kind == "email":
        text = str(raw if raw is not None else "").strip()
        if text and (len(text) > 254 or not _EMAIL_RE.match(text)):
            raise ValueError(f"{spec.label} must be a valid email address (or blank)")
        return text
    # int — reject bools explicitly (True is an int in Python)
    if isinstance(raw, bool):
        raise ValueError(f"{spec.label} must be a whole number")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{spec.label} must be a whole number") from None
    # The env default is always accepted, even outside the range, so an install
    # whose .env sets an unusual value can still save the dialog unchanged.
    if value != DEFAULTS[name] and not spec.min <= value <= spec.max:
        raise ValueError(f"{spec.label} must be between {spec.min} and {spec.max}")
    return value


def _encode(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def load() -> None:
    """Apply stored overrides to app.config. Called once at startup.

    A stored value that no longer validates (e.g. a range was tightened in a
    later release) is skipped with a warning, leaving the env default in force,
    rather than failing startup.
    """
    with _lock:
        for spec in SPECS:
            stored = db.get_setting(_KEY_PREFIX + spec.name)
            if stored is None:
                continue
            try:
                setattr(config, spec.name, parse(spec.name, stored))
            except ValueError as e:
                logger.warning("Ignoring stored setting %s: %s", spec.name, e)


def save(values: dict) -> list[str]:
    """Validate every submitted value, then persist and apply them.

    All-or-nothing: one bad value raises ValueError before anything is written.
    Returns the names whose effective value actually changed, so the caller can
    react (reschedule polling, resize the poll pool).
    """
    parsed = {name: parse(name, raw) for name, raw in values.items()}
    changed = []
    with _lock:
        for name, value in parsed.items():
            if value == DEFAULTS[name]:
                db.delete_setting(_KEY_PREFIX + name)
            else:
                db.set_setting(_KEY_PREFIX + name, _encode(value))
            if getattr(config, name) != value:
                setattr(config, name, value)
                changed.append(name)
    if changed:
        logger.info("Settings changed: %s", ", ".join(changed))
    return changed


def snapshot() -> list[dict]:
    """Every editable setting with its current and default value, for the dialog."""
    return [
        {
            "name": s.name,
            "group": s.group,
            "label": s.label,
            "help": s.help,
            "kind": s.kind,
            "min": s.min if s.kind == "int" else None,
            "max": s.max if s.kind == "int" else None,
            "value": getattr(config, s.name),
            "default": DEFAULTS[s.name],
        }
        for s in SPECS
    ]
