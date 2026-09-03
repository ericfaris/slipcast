# Host Operations — Autoheal Timer

This directory holds host-side operational tooling: files that run on the machine
*outside* the container, kept in version control so they don't drift or get lost.
Right now that's one thing — the autoheal timer.

Nothing here is installed automatically. The steps below are run once, by hand,
on the host.

---

## What autoheal does

`autoheal.sh` runs every 5 minutes under a systemd timer (the same pattern
`cloudflared` already uses on this host — see [CLOUDFLARE_TUNNEL.md](../CLOUDFLARE_TUNNEL.md)).
On each tick it:

1. Probes `http://127.0.0.1:8000/health/live`.
2. **Healthy** → clears the restart budget, releases any pause, exits.
3. **Unhealthy** → restarts the container (`docker compose restart app`), up to
   **3 times per rolling hour**.
4. **Cap reached** → stops restarting, writes a pause marker, and emails you.
   It keeps logging on every later tick and re-sends that email at most once a
   day, so a paused autoheal is never a silent one.
5. The pause clears itself the first time the probe passes again.

### Why `/health/live` and not `/health`

`/health` is the full report: scheduler, poll recency, **and cookie validity**.
Cookies expire every few weeks, and no number of restarts will fix that — an
automated restarter pointed at `/health` would bounce the container every 5
minutes for as long as the cookies were stale, burning through its budget on a
problem it cannot touch and burying the real signal.

`/health/live` reports only the restart-fixable subset: is the scheduler
running, and has polling stalled. Those are the conditions where "turn it off
and on again" is a genuine remedy. The Docker `HEALTHCHECK` in `Dockerfile` and
`docker-compose.yml` points at the same endpoint, so `docker compose ps` and
this script agree on what "unhealthy" means for restart purposes.

Docker's own health status *is* read by the script, but only to include in the
log line. It is never the basis for the restart decision: it lags reality during
the healthcheck's `start_period`/`retries` window and can disagree with a live
probe.

---

## Step 1 — Make the script executable

```bash
chmod +x /home/eric/projects/slipcast/ops/autoheal.sh
```

## Step 2 — Install the systemd units

```bash
sudo cp /home/eric/projects/slipcast/ops/slipcast-autoheal.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
```

## Step 3 — Enable and start the timer

```bash
sudo systemctl enable --now slipcast-autoheal.timer
```

Enable the **timer**, not the service — the service is `Type=oneshot` and has no
`[Install]` section; the timer is what triggers it.

## Step 4 — Verify

```bash
systemctl status slipcast-autoheal.timer
systemctl list-timers 'slipcast-autoheal*'
journalctl -u slipcast-autoheal.service -n 50
```

A safe dry run against the healthy running container — this logs and exits
without restarting anything:

```bash
/home/eric/projects/slipcast/ops/autoheal.sh
```

Expected output while the app is healthy:

```
2026-09-03T03:00:00-04:00 slipcast-autoheal: healthy (docker reports: healthy)
```

---

## State files

Both live in `./data/` (the durable, gitignored bind-mount the app already uses)
and are plain text:

| File | Contents |
|---|---|
| `data/.autoheal_state` | One unix timestamp per restart, pruned to the last hour on each read |
| `data/.autoheal_paused` | A single unix timestamp — present only while restarts are exhausted |

To re-arm restarts by hand without waiting for a healthy probe:

```bash
rm -f /home/eric/projects/slipcast/data/.autoheal_paused
```

---

## Configuration

Everything is overridable by environment variable (set them in the `.service`
file's `[Service]` section with `Environment=` if you want a permanent change):

| Variable | Default | Meaning |
|---|---|---|
| `PROJECT_DIR` | the repo root | Where `docker-compose.yml`, `.env`, and `data/` live |
| `HEALTH_URL` | `http://127.0.0.1:8000/health/live` | What gets probed |
| `CONTAINER` | `slipcast-app-1` | Container name for the `docker inspect`/fallback-restart path |
| `MAX_RESTARTS` | `3` | Restarts allowed per window |
| `WINDOW_SECONDS` | `3600` | The rolling window |
| `PAUSE_EMAIL_INTERVAL` | `86400` | Minimum gap between repeat "exhausted" emails |

Email uses the **same SMTP settings the app uses**, read from the project's
`.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`,
`ALERT_EMAIL`). That's deliberate: the script must be able to reach you when the
container — and therefore the app's own alerting — is down. If those values
aren't set, the script logs `email not sent: …` rather than failing silently.

---

## Notes & troubleshooting

- **WSL2 caveat.** systemd here runs inside WSL2, exactly as with `cloudflared`.
  The timer only fires while WSL2 is up — if Windows shuts the WSL VM down,
  nothing is watching Slipcast until it comes back.
- **`slipcast-app-1`** is the container name Compose derives from `name: slipcast`
  plus the `app` service. If you rename either, set `CONTAINER` to match. The
  script prefers `docker compose -f <project>/docker-compose.yml restart app`
  and only falls back to `docker restart "$CONTAINER"`.
- **Nothing happened after an outage?** Check `journalctl -u slipcast-autoheal.service`
  first, then whether `data/.autoheal_paused` exists — if it does, the budget was
  spent and the script is deliberately standing down.
- **The script never uses `set -e`.** A failing `curl -f` is its normal
  unhealthy path, and `-e` would exit before that branch ran. Keep it that way.
- **Testing the unhealthy path safely:** point it at a dead port with a scratch
  project dir, so nothing real is restarted:
  ```bash
  PROJECT_DIR=/tmp/autoheal-test HEALTH_URL=http://127.0.0.1:59999/ \
      /home/eric/projects/slipcast/ops/autoheal.sh
  ```
