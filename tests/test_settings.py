"""Runtime-editable settings (app/settings.py) and the Settings dialog API."""
import ast
import asyncio
import json
import pathlib

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import config, database as db, main, settings

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"
NAMES = [s.name for s in settings.SPECS]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh DB per test, and every editable config value restored afterwards
    (settings.save() assigns to app.config directly)."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    for name in NAMES:
        monkeypatch.setattr(config, name, settings.DEFAULTS[name])


def _json_request(body) -> Request:
    payload = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/settings", "headers": [
        (b"content-type", b"application/json")], "query_string": b""}
    return Request(scope, receive)


# --- parse ---------------------------------------------------------------------

@pytest.mark.parametrize("name,raw,expected", [
    ("POLL_INTERVAL_HOURS", "12", 12),
    ("POLL_INTERVAL_HOURS", 3, 3),
    ("MAX_EPISODE_AGE_DAYS", " 0 ", 0),
    ("REQUIRE_FEED_TOKENS", "on", True),
    ("REQUIRE_FEED_TOKENS", False, False),
    ("REQUIRE_FEED_TOKENS", "0", False),
    ("ALERT_EMAIL", " me@example.com ", "me@example.com"),
    ("ALERT_EMAIL", "", ""),
])
def test_parse_accepts(name, raw, expected):
    assert settings.parse(name, raw) == expected


@pytest.mark.parametrize("name,raw", [
    ("POLL_INTERVAL_HOURS", "0"),       # below min — 0 would mean "poll constantly"
    ("POLL_INTERVAL_HOURS", "169"),
    ("POLL_INTERVAL_HOURS", "1.5"),
    ("POLL_INTERVAL_HOURS", "abc"),
    ("POLL_INTERVAL_HOURS", True),      # bool is an int subclass — must not slip through
    ("POLL_CONCURRENCY", "9"),
    ("MAX_EPISODES_PER_CHANNEL", "0"),
    ("REQUIRE_FEED_TOKENS", "maybe"),
    ("ALERT_EMAIL", "not-an-email"),
    ("BASE_URL", "http://x"),           # not editable
    ("SMTP_PASS", "x"),                 # secrets are never editable
])
def test_parse_rejects(name, raw):
    with pytest.raises(ValueError):
        settings.parse(name, raw)


def test_parse_accepts_out_of_range_env_default(monkeypatch):
    """An install whose .env sets an unusual value can still save it unchanged."""
    monkeypatch.setitem(settings.DEFAULTS, "MAX_EPISODES_PER_CHANNEL", 5000)
    assert settings.parse("MAX_EPISODES_PER_CHANNEL", "5000") == 5000
    with pytest.raises(ValueError):
        settings.parse("MAX_EPISODES_PER_CHANNEL", "4000")


# --- save / load ----------------------------------------------------------------

def test_save_applies_and_persists():
    changed = settings.save({"POLL_INTERVAL_HOURS": "3", "REQUIRE_FEED_TOKENS": True})
    assert config.POLL_INTERVAL_HOURS == 3
    assert config.REQUIRE_FEED_TOKENS is True
    assert "POLL_INTERVAL_HOURS" in changed
    assert db.get_setting("config:POLL_INTERVAL_HOURS") == "3"


def test_save_is_all_or_nothing():
    before = config.POLL_INTERVAL_HOURS
    with pytest.raises(ValueError, match="Channels polled at once"):
        settings.save({"POLL_INTERVAL_HOURS": "3", "POLL_CONCURRENCY": "99"})
    assert config.POLL_INTERVAL_HOURS == before
    assert db.get_setting("config:POLL_INTERVAL_HOURS") is None


def test_saving_default_removes_override():
    """Storing the env default would pin it forever — a later .env change
    would then be silently ignored. Saving the default deletes the row."""
    default = settings.DEFAULTS["MAX_EPISODES_PER_CHANNEL"]
    settings.save({"MAX_EPISODES_PER_CHANNEL": default + 1})
    assert db.get_setting("config:MAX_EPISODES_PER_CHANNEL") is not None
    changed = settings.save({"MAX_EPISODES_PER_CHANNEL": str(default)})
    assert changed == ["MAX_EPISODES_PER_CHANNEL"]
    assert db.get_setting("config:MAX_EPISODES_PER_CHANNEL") is None
    assert config.MAX_EPISODES_PER_CHANNEL == default


def test_save_unchanged_reports_nothing():
    assert settings.save({"POLL_INTERVAL_HOURS": settings.DEFAULTS["POLL_INTERVAL_HOURS"]}) == []


def test_load_applies_stored_overrides():
    db.set_setting("config:MAX_EPISODE_AGE_DAYS", "30")
    db.set_setting("config:REQUIRE_FEED_TOKENS", "1")
    db.set_setting("config:ALERT_EMAIL", "alerts@example.com")
    settings.load()
    assert config.MAX_EPISODE_AGE_DAYS == 30
    assert config.REQUIRE_FEED_TOKENS is True
    assert config.ALERT_EMAIL == "alerts@example.com"


def test_load_skips_invalid_stored_value():
    """A stored value that no longer validates must not break startup."""
    db.set_setting("config:POLL_INTERVAL_HOURS", "0")
    settings.load()
    assert config.POLL_INTERVAL_HOURS == settings.DEFAULTS["POLL_INTERVAL_HOURS"]


def test_load_ignores_unrelated_settings_rows():
    db.get_or_create_all_feed_token()  # the other user of the settings table
    settings.load()  # must not raise or touch config


def test_snapshot_shape():
    snap = settings.snapshot()
    assert [s["name"] for s in snap] == NAMES
    poll = next(s for s in snap if s["name"] == "POLL_INTERVAL_HOURS")
    assert poll["kind"] == "int" and poll["min"] == 1 and poll["max"] == 168
    assert poll["value"] == config.POLL_INTERVAL_HOURS
    tokens = next(s for s in snap if s["name"] == "REQUIRE_FEED_TOKENS")
    assert tokens["min"] is None


# --- readers see live values -----------------------------------------------------

def test_no_module_freezes_an_editable_setting():
    """Regression guard: `from app.config import NAME` binds the env value at
    import time, so a Settings-dialog change would be silently ignored by that
    module. Editable settings must be read as config.NAME."""
    offenders = []
    for path in APP_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.config":
                offenders += [f"{path.name}: {a.name}" for a in node.names if a.name in NAMES]
    assert offenders == []


def test_feed_token_enforcement_follows_saved_setting():
    settings.save({"REQUIRE_FEED_TOKENS": False})
    assert main._token_ok("abc", None) is True
    settings.save({"REQUIRE_FEED_TOKENS": True})
    assert main._token_ok("abc", None) is False
    assert main._token_ok("abc", "abc") is True


# --- endpoints ------------------------------------------------------------------

def test_api_settings_lists_all():
    body = json.loads(main.api_settings().body)
    assert [s["name"] for s in body["settings"]] == NAMES


def test_save_endpoint_rejects_bad_value_with_field_name():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.save_settings(_json_request({"values": {"POLL_INTERVAL_HOURS": "0"}})))
    assert exc.value.status_code == 400
    assert "Poll every" in exc.value.detail


@pytest.mark.parametrize("body", [[], {"values": []}, {"nope": {}}])
def test_save_endpoint_rejects_malformed_body(body):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.save_settings(_json_request(body)))
    assert exc.value.status_code == 400


def test_save_endpoint_reschedules_poll(monkeypatch):
    calls = []

    class FakeScheduler:
        def reschedule_job(self, job_id, **kw):
            calls.append((job_id, kw))

    monkeypatch.setattr(main, "_scheduler", FakeScheduler())
    new = 7 if settings.DEFAULTS["POLL_INTERVAL_HOURS"] != 7 else 8
    resp = asyncio.run(main.save_settings(_json_request({"values": {"POLL_INTERVAL_HOURS": str(new)}})))
    body = json.loads(resp.body)
    assert body["changed"] == ["POLL_INTERVAL_HOURS"]
    assert calls == [(main._POLL_JOB_ID, {"trigger": "interval", "hours": new})]
    assert json.loads(main.api_settings().body)["settings"][0]["value"] == new


def test_save_endpoint_unrelated_change_does_not_reschedule(monkeypatch):
    class Boom:
        def reschedule_job(self, *a, **k):
            raise AssertionError("must not reschedule")

    monkeypatch.setattr(main, "_scheduler", Boom())
    asyncio.run(main.save_settings(_json_request({"values": {"MAX_EPISODE_AGE_DAYS": "45"}})))
    assert config.MAX_EPISODE_AGE_DAYS == 45


def test_concurrency_change_swaps_poll_pool(monkeypatch):
    start = main.ThreadPoolExecutor(max_workers=2, thread_name_prefix="poll")
    monkeypatch.setattr(main, "_poll_executor", start)
    monkeypatch.setattr(main, "_poll_executor_size", 2)
    ran = []
    monkeypatch.setattr(main, "_run_poll", lambda url: ran.append(url))

    asyncio.run(main.save_settings(_json_request({"values": {"POLL_CONCURRENCY": 3}})))

    assert main._poll_executor is not start
    assert main._poll_executor_size == 3
    assert start._shutdown  # old pool retired (queued work still drains)
    main._submit_poll("https://youtube.com/@x")  # lands on the new pool
    main._poll_executor.shutdown(wait=True)
    assert ran == ["https://youtube.com/@x"]


def test_toasts_render_above_dialogs():
    """Regression: the toaster sat at z-index 60 under .modal (80), so the
    'Settings saved' toast was hidden behind the dialog's backdrop."""
    import re
    css = (APP_DIR / "static" / "styles.css").read_text()

    def z(selector):
        block = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M).group(1)
        return int(re.search(r"z-index:\s*(\d+)", block).group(1))

    assert z(".toaster") > z(".modal")


def test_settings_dialog_has_inline_save_status():
    """The Save result is also shown next to the button (app.js setPrefsNote)."""
    assert 'id="prefs-note"' in main._PAGE and 'aria-live="polite"' in main._PAGE
    assert "setPrefsNote(" in (APP_DIR / "static" / "app.js").read_text()
