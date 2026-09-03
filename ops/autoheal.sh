#!/usr/bin/env bash
#
# Restart a wedged Slipcast container, bounded, and email a human when the
# bound is reached.
#
# Runs on the *host* (systemd timer, every 5 minutes — see
# slipcast-autoheal.timer), not inside the container: a container cannot
# restart itself, and when the whole stack is down something outside it has to
# be able to send the email.
#
# The restart decision is made on `curl /health/live`, NOT on Docker's own
# health status. Docker's status is reported here for the log only, because it
# lags the truth during the healthcheck's start_period/retries window and can
# disagree with a live probe. /health/live deliberately covers only conditions
# a restart could fix (scheduler stopped, polling stalled) — never cookie
# expiry or low disk, which restarting would not repair.
#
# NOT installed by the repo. See ops/README.md for the install steps.

# Deliberately not `set -e`: `curl -f` failing IS the unhealthy branch, and -e
# would abort the script before it ever ran.
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health/live}"
# Compose project name is "slipcast" and the service is "app" (docker-compose.yml).
CONTAINER="${CONTAINER:-slipcast-app-1}"
STATE_FILE="${STATE_FILE:-$PROJECT_DIR/data/.autoheal_state}"
PAUSE_FILE="${PAUSE_FILE:-$PROJECT_DIR/data/.autoheal_paused}"
MAX_RESTARTS="${MAX_RESTARTS:-3}"
WINDOW_SECONDS="${WINDOW_SECONDS:-3600}"
# While paused, keep reporting — but don't email more often than this.
PAUSE_EMAIL_INTERVAL="${PAUSE_EMAIL_INTERVAL:-86400}"

log() {
    printf '%s slipcast-autoheal: %s\n' "$(date -Is)" "$*"
}

# Sends via SMTP directly, reading the same credentials the app uses, so this
# still works when the container is down. Values come from $PROJECT_DIR/.env,
# which is gitignored — they are exported into the environment and read from
# os.environ inside the heredoc, never interpolated into the script text or
# echoed to the log.
send_email() {
    local subject="$1" body="$2"

    if ! command -v python3 >/dev/null 2>&1; then
        log "email not sent: python3 not found on PATH"
        return
    fi
    if [ -z "${SMTP_HOST:-}" ] || [ -z "${SMTP_USER:-}" ] || \
       [ -z "${SMTP_PASS:-}" ] || [ -z "${ALERT_EMAIL:-}" ]; then
        log "email not sent: SMTP_HOST/SMTP_USER/SMTP_PASS/ALERT_EMAIL not all set in $PROJECT_DIR/.env"
        return
    fi

    SUBJECT="$subject" BODY="$body" python3 - <<'PY'
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

# Mirrors app/notify.py::_send — port 465 is implicit SSL, anything else
# (typically 587) is STARTTLS.
host = os.environ["SMTP_HOST"]
port = int(os.environ.get("SMTP_PORT") or 587)
user = os.environ["SMTP_USER"]
password = os.environ["SMTP_PASS"]
sender = os.environ.get("SMTP_FROM") or user
to = os.environ["ALERT_EMAIL"]

msg = EmailMessage()
msg["Subject"] = os.environ["SUBJECT"]
msg["From"] = sender
msg["To"] = to
msg.set_content(os.environ["BODY"])

try:
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20,
                              context=ssl.create_default_context()) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(user, password)
            server.send_message(msg)
    print("email sent to %s" % to)
except Exception as exc:  # noqa: BLE001
    print("email FAILED: %s" % exc, file=sys.stderr)
    sys.exit(1)
PY
}

exhausted_email() {
    local count="$1"
    send_email \
        "🚨 Slipcast: restarts exhausted — human needed" \
"Slipcast on $(hostname) is failing its liveness check and has already been
restarted ${count} time(s) within the last $((WINDOW_SECONDS / 60)) minutes.

Restarting is NOT being attempted any more. A restart clearly isn't fixing it,
and looping would only obscure the real problem.

  Container:  ${CONTAINER}
  Probe:      ${HEALTH_URL} (non-200)
  Project:    ${PROJECT_DIR}

To investigate:

  docker compose -f ${PROJECT_DIR}/docker-compose.yml logs --tail=200 app
  docker compose -f ${PROJECT_DIR}/docker-compose.yml ps
  curl -s http://127.0.0.1:8000/health

/health is the full report and will say whether cookies or the scheduler are
the problem. Automatic restarts resume by themselves as soon as the liveness
probe passes again; to re-arm them by hand, delete ${PAUSE_FILE}."
}

# --- probe ------------------------------------------------------------------

if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091  # path is host-specific and gitignored
    . "$PROJECT_DIR/.env"
    set +a
fi

docker_health="$(docker inspect --format='{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo unknown)"

if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; then
    log "healthy (docker reports: ${docker_health})"
    if [ -f "$PAUSE_FILE" ]; then
        rm -f "$PAUSE_FILE"
        log "recovered — restart budget released"
    fi
    # Clear the window so a later, unrelated incident starts with a full budget.
    : > "$STATE_FILE" 2>/dev/null || true
    exit 0
fi

log "UNHEALTHY: ${HEALTH_URL} did not return 200 (docker reports: ${docker_health})"

now="$(date +%s)"

if [ -f "$PAUSE_FILE" ]; then
    paused_at="$(head -n1 "$PAUSE_FILE" 2>/dev/null || echo 0)"
    case "$paused_at" in (*[!0-9]*|"") paused_at=0 ;; esac
    log "restarts exhausted; awaiting human (paused since $(date -Is -d "@${paused_at}" 2>/dev/null || echo unknown))"
    # Paused must never mean silent — re-report periodically until it's fixed.
    if [ "$((now - paused_at))" -ge "$PAUSE_EMAIL_INTERVAL" ]; then
        log "re-sending exhausted notification (>${PAUSE_EMAIL_INTERVAL}s since the last one)"
        exhausted_email "$MAX_RESTARTS"
        echo "$now" > "$PAUSE_FILE"
    fi
    exit 0
fi

# Rolling window: one unix timestamp per line, stale entries dropped on read
# and the file rewritten — the same shape as _check_rate_limit/_failed_attempts
# in app/main.py. Keeps the file bounded and needs no separate reset step.
recent=()
if [ -f "$STATE_FILE" ]; then
    while IFS= read -r line; do
        case "$line" in (*[!0-9]*|"") continue ;; esac
        if [ "$((now - line))" -lt "$WINDOW_SECONDS" ]; then
            recent+=("$line")
        fi
    done < "$STATE_FILE"
fi

mkdir -p "$(dirname "$STATE_FILE")"
if [ "${#recent[@]}" -gt 0 ]; then
    printf '%s\n' "${recent[@]}" > "$STATE_FILE"
else
    : > "$STATE_FILE"
fi

count="${#recent[@]}"
if [ "$count" -ge "$MAX_RESTARTS" ]; then
    log "restart cap reached (${count}/${MAX_RESTARTS} in the last $((WINDOW_SECONDS / 60))m) — NOT restarting; emailing instead"
    echo "$now" > "$PAUSE_FILE"
    exhausted_email "$count"
    exit 0
fi

echo "$now" >> "$STATE_FILE"
log "restarting ${CONTAINER} (attempt $((count + 1))/${MAX_RESTARTS} in the last $((WINDOW_SECONDS / 60))m)"

if docker compose -f "$PROJECT_DIR/docker-compose.yml" restart app; then
    log "compose restart issued"
elif docker restart "$CONTAINER"; then
    # Fallback for the case where compose isn't usable from this context
    # (missing plugin, a moved compose file) but the container itself is known.
    log "compose restart failed; fell back to 'docker restart ${CONTAINER}'"
else
    log "ERROR: could not restart the container by either method"
fi

exit 0
