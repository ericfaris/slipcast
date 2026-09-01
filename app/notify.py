import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

from app import config

logger = logging.getLogger(__name__)

# Tracks the last time each alert kind was sent, to avoid spamming.
_ALERT_STATE_FILE = os.path.join(config.DATA_DIR, ".alert_state")


def _smtp_configured() -> bool:
    # Mirrors the mobilism-search Gmail SMTP contract (SMTP_HOST/USER/PASS).
    return bool(
        config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASS and config.ALERT_EMAIL
    )


def _last_sent(kind: str) -> float:
    try:
        with open(_ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                k, _, ts = line.strip().partition("=")
                if k == kind:
                    return float(ts)
    except (OSError, ValueError):
        pass
    return 0.0


def _record_sent(kind: str) -> None:
    state: dict[str, str] = {}
    try:
        with open(_ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                k, _, ts = line.strip().partition("=")
                if k:
                    state[k] = ts
    except OSError:
        pass
    state[kind] = str(time.time())
    try:
        os.makedirs(os.path.dirname(_ALERT_STATE_FILE), exist_ok=True)
        with open(_ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            for k, ts in state.items():
                f.write(f"{k}={ts}\n")
    except OSError as exc:
        logger.warning("Could not persist alert state: %s", exc)


def _send(msg: EmailMessage) -> None:
    """Send via Gmail SMTP. Port 465 = implicit SSL, otherwise STARTTLS (587)."""
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, timeout=20,
            context=ssl.create_default_context(),
        ) as server:
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.send_message(msg)


def _cookie_alert_message() -> EmailMessage:
    upload_url = config.BASE_URL.rstrip("/") + "/"
    msg = EmailMessage()
    msg["Subject"] = "⚠️ Slipcast: cookies need updating"
    msg["From"] = config.SMTP_FROM
    msg["To"] = config.ALERT_EMAIL

    plain = f"""\
Slipcast — action needed

Channel polling has stopped because the YouTube cookies file is missing,
empty, or expired. No new episodes will download until it's refreshed.

How to fix (takes ~2 minutes):

1. In Chrome, install "Get cookies.txt LOCALLY":
   https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
   (Firefox: https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
2. Open https://www.youtube.com and make sure you're signed in.
3. Click the extension icon and export as cookies.txt
4. Upload it here: {upload_url}

Once a valid cookies.txt is uploaded, the next scheduled poll picks up the
backlog automatically (or click "Poll Now").
"""

    html = f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#eef2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2d3d">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f5;padding:24px 0">
    <tr><td align="center">
      <table role="presentation" width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08)">
        <tr>
          <td style="background:#36558f;padding:20px 28px;color:#ffffff;font-size:18px;font-weight:600">
            ⚠️ Slipcast — cookies need updating
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px">
            <p style="margin:0 0 16px;font-size:15px;line-height:1.5">
              Channel polling has <strong>stopped</strong> because the YouTube cookies file is
              missing, empty, or expired. No new episodes will download until it's refreshed.
            </p>
            <p style="margin:0 0 10px;font-size:13px;font-weight:700;color:#36558f;text-transform:uppercase;letter-spacing:.03em">
              How to fix &nbsp;·&nbsp; ~2 minutes
            </p>
            <ol style="margin:0 0 22px;padding-left:20px;font-size:14px;line-height:1.7">
              <li>Install
                <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" style="color:#36558f">Get cookies.txt LOCALLY</a>
                in Chrome
                (or <a href="https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/" style="color:#36558f">cookies.txt</a> in Firefox).
              </li>
              <li>Open <a href="https://www.youtube.com" style="color:#36558f">youtube.com</a> and confirm you're signed in.</li>
              <li>Click the extension icon and export as <strong>cookies.txt</strong>.</li>
              <li>Upload it in the management UI.</li>
            </ol>
            <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 8px">
              <tr><td style="border-radius:8px;background:#36558f">
                <a href="{upload_url}" style="display:inline-block;padding:12px 26px;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none">
                  Open management UI &rarr;
                </a>
              </td></tr>
            </table>
            <p style="margin:16px 0 0;font-size:12px;color:#8295a3;line-height:1.5">
              Once a valid cookies.txt is uploaded, the next scheduled poll picks up the backlog
              automatically — or click <em>Poll Now</em>.
            </p>
          </td>
        </tr>
      </table>
      <p style="margin:16px 0 0;font-size:11px;color:#aab6c0">Sent automatically by your self-hosted Slipcast server.</p>
    </td></tr>
  </table>
</body>
</html>
"""
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def _cookie_expiry_message(days_left: int, expires_at: str) -> EmailMessage:
    upload_url = config.BASE_URL.rstrip("/") + "/"
    when = "today" if days_left <= 0 else f"in {days_left} day{'s' if days_left != 1 else ''}"
    msg = EmailMessage()
    msg["Subject"] = f"⏳ Slipcast: cookies expire {when}"
    msg["From"] = config.SMTP_FROM
    msg["To"] = config.ALERT_EMAIL

    plain = f"""\
Slipcast — heads up

Your YouTube cookies are still working, but they expire {when}
(on {expires_at}). When they lapse, channel polling will stop and no new
episodes will download until you upload a fresh cookies.txt.

Refresh now to avoid a gap (takes ~2 minutes):

1. In Chrome, install "Get cookies.txt LOCALLY":
   https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
   (Firefox: https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
2. Open https://www.youtube.com (signed in is best, but not required).
3. Click the extension icon and export as cookies.txt
4. Upload it here: {upload_url}
"""

    html = f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#eef2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2d3d">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2f5;padding:24px 0">
    <tr><td align="center">
      <table role="presentation" width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.08)">
        <tr>
          <td style="background:#b8860b;padding:20px 28px;color:#ffffff;font-size:18px;font-weight:600">
            ⏳ Slipcast — cookies expire {when}
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px">
            <p style="margin:0 0 16px;font-size:15px;line-height:1.5">
              Your YouTube cookies are <strong>still working</strong>, but they expire
              <strong>{when}</strong> (on {expires_at}). When they lapse, channel polling
              stops until you upload a fresh <strong>cookies.txt</strong>.
            </p>
            <p style="margin:0 0 10px;font-size:13px;font-weight:700;color:#36558f;text-transform:uppercase;letter-spacing:.03em">
              Refresh now &nbsp;·&nbsp; ~2 minutes
            </p>
            <ol style="margin:0 0 22px;padding-left:20px;font-size:14px;line-height:1.7">
              <li>Install
                <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" style="color:#36558f">Get cookies.txt LOCALLY</a>
                in Chrome
                (or <a href="https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/" style="color:#36558f">cookies.txt</a> in Firefox).
              </li>
              <li>Open <a href="https://www.youtube.com" style="color:#36558f">youtube.com</a>.</li>
              <li>Click the extension icon and export as <strong>cookies.txt</strong>.</li>
              <li>Upload it in the management UI.</li>
            </ol>
            <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 8px">
              <tr><td style="border-radius:8px;background:#36558f">
                <a href="{upload_url}" style="display:inline-block;padding:12px 26px;color:#ffffff;font-size:14px;font-weight:600;text-decoration:none">
                  Open management UI &rarr;
                </a>
              </td></tr>
            </table>
          </td>
        </tr>
      </table>
      <p style="margin:16px 0 0;font-size:11px;color:#aab6c0">Sent automatically by your self-hosted Slipcast server.</p>
    </td></tr>
  </table>
</body>
</html>
"""
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    return msg


def send_cookie_expiry_warning(days_left: int, expires_at: str, force: bool = False) -> bool:
    """Email a 'cookies expiring soon' heads-up while they still work.

    Distinct from send_cookie_alert (which fires once cookies are already
    missing/invalid). Debounced under its own key so the two don't suppress
    each other. Returns True if sent.
    """
    if not _smtp_configured():
        logger.info("Cookie expiry warning not sent: SMTP not configured")
        return False

    cooldown = config.ALERT_COOLDOWN_HOURS * 3600
    if not force and (time.time() - _last_sent("cookie_expiry")) < cooldown:
        logger.debug("Cookie expiry warning suppressed (within %dh cooldown)", config.ALERT_COOLDOWN_HOURS)
        return False

    msg = _cookie_expiry_message(days_left, expires_at)
    try:
        _send(msg)
        _record_sent("cookie_expiry")
        logger.info("Sent cookie-expiry warning to %s (%d days left)", config.ALERT_EMAIL, days_left)
        return True
    except Exception as exc:
        logger.error("Failed to send cookie expiry warning email: %s", exc)
        return False


def send_cookie_alert(force: bool = False) -> bool:
    """Email a 'refresh your cookies' notice. Debounced; returns True if sent."""
    if not _smtp_configured():
        logger.info("Cookie alert not sent: SMTP not configured (set SMTP_HOST/SMTP_FROM)")
        return False

    cooldown = config.ALERT_COOLDOWN_HOURS * 3600
    if not force and (time.time() - _last_sent("cookies")) < cooldown:
        logger.debug("Cookie alert suppressed (within %dh cooldown)", config.ALERT_COOLDOWN_HOURS)
        return False

    msg = _cookie_alert_message()
    try:
        _send(msg)
        _record_sent("cookies")
        logger.info("Sent cookie-refresh alert to %s", config.ALERT_EMAIL)
        return True
    except Exception as exc:
        logger.error("Failed to send cookie alert email: %s", exc)
        return False


def _poll_failure_message(problems: list[str]) -> EmailMessage:
    ui_url = config.BASE_URL.rstrip("/") + "/"
    msg = EmailMessage()
    msg["Subject"] = f"⚠️ Slipcast: {len(problems)} channel(s) had download failures"
    msg["From"] = config.SMTP_FROM
    msg["To"] = config.ALERT_EMAIL

    lines = "\n".join(f"  • {p}" for p in problems)
    msg.set_content(f"""\
Slipcast — poll completed with errors

The latest channel poll finished, but one or more videos failed to download.
Cookies look OK (that has its own alert), so this is likely a transient
YouTube error, a stale yt-dlp, or a video that needs a retry.

Affected:
{lines}

Open the management UI to retry ("Poll Now" on the channel):
{ui_url}
""")
    return msg


def send_poll_failure_alert(problems: list[str], force: bool = False) -> bool:
    """Email when a poll_all run finishes with per-video download failures.

    This is the 'silent failure' alert: polling keeps running and cookies are
    fine, but videos are quietly not downloading. Debounced under its own key.
    Returns True if sent.
    """
    if not problems:
        return False
    if not _smtp_configured():
        logger.info("Poll-failure alert not sent: SMTP not configured")
        return False

    cooldown = config.ALERT_COOLDOWN_HOURS * 3600
    if not force and (time.time() - _last_sent("poll_failures")) < cooldown:
        logger.debug("Poll-failure alert suppressed (within %dh cooldown)",
                     config.ALERT_COOLDOWN_HOURS)
        return False

    msg = _poll_failure_message(problems)
    try:
        _send(msg)
        _record_sent("poll_failures")
        logger.info("Sent poll-failure alert to %s (%d channels)",
                    config.ALERT_EMAIL, len(problems))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send poll-failure alert email: %s", exc)
        return False
