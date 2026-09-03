"""Tests for app.notify cookie-alert emailing (debounce + SMTP transport)."""
import time

import pytest

from app import config, notify


@pytest.fixture
def smtp(monkeypatch, tmp_path):
    """Configure SMTP + an isolated alert-state file, and capture sent mail."""
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USER", "me@gmail.com")
    monkeypatch.setattr(config, "SMTP_PASS", "app-password")
    monkeypatch.setattr(config, "SMTP_FROM", "me@gmail.com")
    monkeypatch.setattr(config, "ALERT_EMAIL", "me@gmail.com")
    monkeypatch.setattr(config, "ALERT_COOLDOWN_HOURS", 24)
    monkeypatch.setattr(notify, "_ALERT_STATE_FILE", str(tmp_path / ".alert_state"))

    sent = []
    monkeypatch.setattr(notify, "_send", lambda msg: sent.append(msg))
    return sent


def test_not_sent_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASS", "")
    monkeypatch.setattr(notify, "_ALERT_STATE_FILE", str(tmp_path / ".alert_state"))
    assert notify.send_cookie_alert() is False


def test_sends_when_configured(smtp):
    assert notify.send_cookie_alert() is True
    assert len(smtp) == 1


def test_debounced_within_cooldown(smtp):
    assert notify.send_cookie_alert() is True
    assert notify.send_cookie_alert() is False  # suppressed by cooldown
    assert len(smtp) == 1


def test_force_bypasses_cooldown(smtp):
    assert notify.send_cookie_alert() is True
    assert notify.send_cookie_alert(force=True) is True
    assert len(smtp) == 2


def test_resends_after_cooldown(smtp, monkeypatch):
    assert notify.send_cookie_alert() is True
    # pretend the last send was 25h ago
    monkeypatch.setattr(notify, "_last_sent", lambda kind: time.time() - 25 * 3600)
    assert notify.send_cookie_alert() is True
    assert len(smtp) == 2


def test_message_is_well_formed(smtp):
    msg = notify._cookie_alert_message()
    assert msg["To"] == "me@gmail.com"
    assert "cookies" in msg["Subject"].lower()
    # multipart: plain + html alternatives
    body = msg.get_body(preferencelist=("html",)).get_content()
    assert "cookies.txt" in body
    assert config.BASE_URL in body
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Upload it here" in plain


def test_expiry_warning_sends_when_configured(smtp):
    assert notify.send_cookie_expiry_warning(5, "2026-12-24 12:45 UTC") is True
    assert len(smtp) == 1


def test_expiry_warning_debounced(smtp):
    assert notify.send_cookie_expiry_warning(5, "2026-12-24 12:45 UTC") is True
    assert notify.send_cookie_expiry_warning(5, "2026-12-24 12:45 UTC") is False
    assert len(smtp) == 1


def test_expiry_warning_separate_cooldown_from_missing_alert(smtp):
    # The two alert kinds must not suppress each other.
    assert notify.send_cookie_alert() is True
    assert notify.send_cookie_expiry_warning(3, "2026-12-24 12:45 UTC") is True
    assert len(smtp) == 2


def test_expiry_warning_message_is_well_formed(smtp):
    msg = notify._cookie_expiry_message(3, "2026-12-24 12:45 UTC")
    assert msg["To"] == "me@gmail.com"
    assert "expire" in msg["Subject"].lower()
    body = msg.get_body(preferencelist=("html",)).get_content()
    assert "2026-12-24" in body
    assert "in 3 days" in body
    assert config.BASE_URL in body


def test_expiry_warning_message_singular_and_today(smtp):
    assert "in 1 day" in notify._cookie_expiry_message(1, "x")["Subject"] \
        or "in 1 day" in notify._cookie_expiry_message(1, "x").get_body(preferencelist=("plain",)).get_content()
    assert "today" in notify._cookie_expiry_message(0, "x")["Subject"]


def test_send_failure_returns_false(smtp, monkeypatch):
    def boom(msg):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(notify, "_send", boom)
    assert notify.send_cookie_alert() is False


def test_state_not_recorded_on_failure(smtp, monkeypatch):
    monkeypatch.setattr(notify, "_send", lambda msg: (_ for _ in ()).throw(RuntimeError("x")))
    notify.send_cookie_alert()
    # cooldown not started, so a later good send still works
    monkeypatch.setattr(notify, "_send", lambda msg: smtp.append(msg))
    assert notify.send_cookie_alert() is True


# --- disk-pressure prune alert ----------------------------------------------

def test_disk_prune_alert_sends_and_lists_episodes(smtp):
    assert notify.send_disk_prune_alert(["C — old one", "D — older one"],
                                        512 * 1_048_576, 1.4) is True
    body = smtp[0].get_content()
    assert "old one" in body and "older one" in body
    assert "1.4 GB" in body


def test_disk_prune_alert_noop_on_empty_list(smtp):
    assert notify.send_disk_prune_alert([], 0, 1.0) is False
    assert smtp == []


def test_disk_prune_alert_truncates_long_list(smtp):
    notify.send_disk_prune_alert([f"C — ep{i}" for i in range(35)], 0, 1.0)
    body = smtp[0].get_content()
    assert "…and 15 more" in body
    assert "ep19" in body and "ep20" not in body


def test_disk_prune_alert_debounced_and_forceable(smtp):
    assert notify.send_disk_prune_alert(["a"], 1, 1.0) is True
    assert notify.send_disk_prune_alert(["a"], 1, 1.0) is False  # cooldown
    assert notify.send_disk_prune_alert(["a"], 1, 1.0, force=True) is True
    assert len(smtp) == 2


# --- backup failure alert ----------------------------------------------------

def test_backup_failure_alert_states_reason(smtp):
    assert notify.send_backup_failure_alert("PRAGMA integrity_check returned: bad") is True
    assert "integrity_check returned: bad" in smtp[0].get_content()


def test_backup_failure_alert_debounced_and_forceable(smtp):
    assert notify.send_backup_failure_alert("x") is True
    assert notify.send_backup_failure_alert("x") is False
    assert notify.send_backup_failure_alert("x", force=True) is True
    assert len(smtp) == 2


def test_new_alerts_use_independent_cooldown_keys(smtp):
    """Each alert kind debounces on its own key — one must not mute another."""
    assert notify.send_disk_prune_alert(["a"], 1, 1.0) is True
    assert notify.send_backup_failure_alert("x") is True  # not suppressed
    assert notify.send_cookie_alert() is True             # nor is the old one
    assert len(smtp) == 3


def test_new_alerts_not_sent_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    monkeypatch.setattr(config, "SMTP_USER", "")
    monkeypatch.setattr(config, "SMTP_PASS", "")
    monkeypatch.setattr(notify, "_ALERT_STATE_FILE", str(tmp_path / ".alert_state"))
    assert notify.send_disk_prune_alert(["a"], 1, 1.0) is False
    assert notify.send_backup_failure_alert("x") is False
