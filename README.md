# Slipcast

A self-hosted server that turns YouTube channels into podcast RSS feeds. Subscribe to any YouTube channel in your podcast app and listen to new videos as audio episodes — automatically downloaded on a schedule. Slipcast slips YouTube content straight into your podcast app.

## Features

- **Podcast RSS feeds** — standard RSS with iTunes extensions, works with any podcast app
- **Automatic polling** — checks subscribed channels on a configurable schedule
- **Channel management UI** — add/remove channels, copy feed URLs, trigger manual polls
- **Shareable links** — add a channel or download an episode by clicking a URL (great for mobile)
- **One-off downloads** — download any specific YouTube video without subscribing to the channel
- **Cover art** — channel and per-episode thumbnails included in the feed
- **Member content filtering** — skips subscriber-only videos during automatic polls
- **Persistent storage** — SQLite database + audio files survive container restarts
- **Basic auth** — management UI is password protected; feeds and audio are publicly accessible
- **Security hardening** — CSRF protection on all POST endpoints, Content-Security-Policy header, rate limiting on failed auth attempts, path traversal prevention

---

## Quick Start

The image is published to Docker Hub — no need to build locally.

**1. Create a project folder and add a `docker-compose.yml`:**

```yaml
services:
  app:
    image: ericfaris/slipcast:latest
    ports:
      - "127.0.0.1:8000:8000"
    volumes:
      - ./data:/data
    environment:
      # External URL used in RSS feed links — must be reachable by your podcast app
      - BASE_URL=http://localhost:8000

      # Web UI login credentials (set in .env file — see below)
      - AUTH_USER=${AUTH_USER}
      - AUTH_PASS=${AUTH_PASS}

      # How many episodes to keep per channel (older ones are pruned)
      - MAX_EPISODES_PER_CHANNEL=20

      # How often to check channels for new videos (in hours)
      - POLL_INTERVAL_HOURS=2

      # Internal data directory — leave this as-is
      - DATA_DIR=/data

      # Uncomment after uploading cookies via the UI
      # - COOKIES_FILE=/data/cookies.txt
```

**2. Create a `.env` file** in the same folder (never commit this):

```
AUTH_USER=youruser
AUTH_PASS=yourpassword
```

**3. Run it:**

```bash
docker compose up -d
```

The app starts at `http://localhost:8000`.

---

## Configuration

All configuration is via environment variables in `docker-compose.yml`. Credentials are kept in `.env` so they are never committed to git.

| Variable | Default | Description |
|---|---|---|
| `BASE_URL` | `http://localhost:8000` | Public URL of your app — used in feed and audio URLs |
| `AUTH_USER` | *(none)* | Management UI username (single-user; ignored if `AUTH_USERS` is set) |
| `AUTH_PASS` | *(none)* | Management UI password (single-user; ignored if `AUTH_USERS` is set) |
| `AUTH_USERS` | *(none)* | Multi-user credentials, e.g. `alice:pass1,bob:pass2` — takes precedence over `AUTH_USER`/`AUTH_PASS` |
| `DATA_DIR` | `/data` | Where audio, thumbnails, and the database are stored |
| `MAX_EPISODES_PER_CHANNEL` | `20` | How many episodes to keep per channel |
| `MIN_FREE_DISK_GB` | `2` | Below this many GB free on the `DATA_DIR` filesystem, the globally oldest episodes are deleted before polling (`0` disables); always emails what it removed |
| `POLL_INTERVAL_HOURS` | `2` | How often to check subscribed channels for new videos |
| `POLL_CONCURRENCY` | `2` | Max channels polled at once by "poll all"/"poll selected" |
| `COOKIES_FILE` | *(none)* | Path to YouTube cookies file (upload via UI, then uncomment) |
| `SMTP_HOST` | *(none)* | SMTP server for cookie-expiry email alerts (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` | *(none)* | SMTP username |
| `SMTP_PASS` | *(none)* | SMTP password (for Gmail, an [App Password](https://myaccount.google.com/apppasswords)) |
| `SMTP_FROM` | `SMTP_USER` | From address for alert emails |
| `ALERT_EMAIL` | `ericfaris@gmail.com` | Where cookie-expiry and poll-failure alerts are sent |

### Important notes
- ⚠️ **Always set credentials before exposing the app.** If neither `AUTH_USERS` nor `AUTH_USER`/`AUTH_PASS` is set, authentication is **disabled** and the entire management UI — including channel management and cookie upload — is open to anyone who can reach it. Set `AUTH_USERS=alice:pass1,bob:pass2` (preferred) or `AUTH_USER`/`AUTH_PASS` whenever the app is reachable beyond localhost.
- `BASE_URL` must be reachable by your podcast app. If using Pocket Casts or another server-side app, this must be a public URL. See [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md) for how to expose the app publicly using Cloudflare Tunnel.
- The port is bound to `127.0.0.1` so the app is only reachable from localhost — external traffic must go through a reverse proxy or tunnel (e.g. Cloudflare Tunnel or Tailscale).
- The management UI (`/`) requires Basic Auth. Feed and audio endpoints (`/feed/`, `/audio/`) are public so podcast apps can access them without credentials.
- YouTube cookies expire every few weeks. When downloads start failing, re-upload cookies via the management UI and uncomment `COOKIES_FILE`.
- The container has a Docker `HEALTHCHECK` (mirrored in `docker-compose.yml`) that hits `/health/live` — the restart-fixable subset of the health report, deliberately not the full `/health` (which also fails on expired cookies, something no restart repairs). With `restart: unless-stopped`, Docker Compose does **not** automatically restart a container just because it's marked unhealthy — the healthcheck only makes the status visible (`docker compose ps`, `docker inspect`). For an actual restart-on-unhealthy, install the host-side autoheal timer: see [ops/README.md](ops/README.md).

---

## Management UI

Visit `https://yourapp/` and log in with your `AUTH_USER` / `AUTH_PASS`.

### Subscribed Channels
Channels being polled automatically on your schedule.

| Action | Description |
|---|---|
| **Copy** | Copies the RSS feed URL to your clipboard |
| **Poll Now** | Triggers an immediate download check in the background |
| **Remove** | Unsubscribes and deletes all downloaded files for that channel |

### One-off Downloads
Episodes downloaded individually without subscribing to the channel. These have a feed URL you can use in your podcast app, but the channel won't be polled automatically.

| Action | Description |
|---|---|
| **Copy** | Copies the RSS feed URL to your clipboard |
| **Subscribe** | Promotes the channel to a full subscription and starts polling it |

### Orphaned Data
Removing a channel resolves its internal `channel_id` to delete the matching episodes and files — if that resolution ever fails (a URL variant, or a channel removed before it was first successfully polled), the episodes/files are left behind with no channel row pointing at them. Slipcast checks for this on startup (logged, never auto-deleted) and lists anything found under **Orphaned data** in the dashboard, with a **Delete** button per entry.

### Add Channel
Paste any YouTube channel URL or handle and click **Add Channel**. The channel is immediately polled in the background.

Supported URL formats:
```
https://www.youtube.com/@ChannelHandle
https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxxxx
```

### Download Episode
Paste any YouTube video URL and click **Download**. The episode is downloaded immediately.

- **Subscribe to channel** unchecked *(default)* — downloads the episode only
- **Subscribe to channel** checked — downloads the episode and subscribes the channel

Supported URL formats:
```
https://youtu.be/xxxxxxxxxxx
https://www.youtube.com/watch?v=xxxxxxxxxxx
```

---

## Shareable Links

Add channels or download episodes via URL — useful from your phone without opening a laptop.

### Add a channel
```
https://yourapp/add?channel=https://www.youtube.com/@ChannelHandle
```

### Download a specific episode
```
https://yourapp/download?url=https://youtu.be/xxxxxxxxxxx
```

### Download and subscribe
```
https://yourapp/download?url=https://youtu.be/xxxxxxxxxxx&subscribe=true
```

---

## RSS Feeds

Each channel has its own RSS feed at:
```
https://yourapp/feed/<channel_id>.xml
```

Feed and audio URLs are **publicly accessible** — no credentials required. This is necessary for podcast apps to fetch and stream content. The management UI remains password protected.

Subscribe to feed URLs in any podcast app (Pocket Casts, AntennaPod, Overcast, Apple Podcasts, etc.).

> **Note:** Some podcast apps (including Pocket Casts) fetch feeds through their own servers rather than directly from your device. In this case `BASE_URL` must be a publicly reachable URL. See [CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md).

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | Required | Management UI |
| `GET` | `/feed/<channel_id>.xml` | None | RSS feed for a channel |
| `GET` | `/audio/<channel_id>/<file>.mp3` | None | Audio file stream |
| `GET` | `/thumbnails/<channel_id>/<file>.jpg` | None | Thumbnail image |
| `GET` | `/health` | None | Full health report — 200 `{"status":"ok",...}` when healthy, 503 `{"status":"degraded",...}` (with a `checks`/`problems` breakdown) if the scheduler isn't running, polling has gone stale (no run in ~3x `POLL_INTERVAL_HOURS`, past an initial startup grace period), or cookies are missing/expired. This is the one to read yourself; automation should use `/health/live` |
| `GET` | `/health/live` | None | Liveness check — the same report **minus** the cookie check: 200 only when a restart would plausibly help (scheduler running, polling not stalled). Expired cookies and low disk deliberately do **not** fail it, since restarting fixes neither. This is what the Docker healthcheck and [ops/autoheal.sh](ops/README.md) watch |
| `GET` | `/add?channel=<url>` | Required | Add a channel via shareable link |
| `GET` | `/download?url=<url>` | Required | Download an episode via shareable link |
| `POST` | `/channels/add` | Required | Add a channel (form) |
| `POST` | `/channels/remove` | Required | Remove a channel (form) |
| `POST` | `/channels/poll` | Required | Trigger immediate poll (form) |
| `POST` | `/channels/subscribe` | Required | Promote one-off to subscription (form) |
| `POST` | `/channels/remove-unsubscribed` | Required | Remove a one-off (unsubscribed) channel and its files (form) |
| `POST` | `/channels/remove-orphan` | Required | Delete leftover data for a channel with no owning row — see Orphaned Data below (form) |
| `POST` | `/episodes/download` | Required | Download a specific episode (form) |
| `POST` | `/auth/cookies` | Required | Upload YouTube cookies file (max 5 MB) |

---

## How It Works

1. **Polling** — on startup and every `POLL_INTERVAL_HOURS`, yt-dlp fetches the `/videos` tab of each subscribed channel
2. **Filtering** — member-only, subscriber-only, and premium videos are skipped during automatic polls
3. **Downloading** — new videos are downloaded as MP3 (128kbps) to `DATA_DIR/audio/<channel_id>/`
4. **Thumbnails** — channel cover art and per-episode thumbnails are downloaded and converted to JPEG (YouTube often serves WebP; ffmpeg converts them for podcast app compatibility)
5. **Pruning** — once a channel exceeds `MAX_EPISODES_PER_CHANNEL`, the oldest episodes are deleted. Separately, if free disk falls below `MIN_FREE_DISK_GB`, the oldest episodes **across all channels** are deleted at the start of a poll until it's back above the line (you get an email listing them)
6. **Feed generation** — RSS feeds are built dynamically from the SQLite database on each request
7. **Deduplication** — already-downloaded files are skipped by file existence check

---

## Data Layout

```
./data/
├── episodes.db              # SQLite database
├── cookies.txt              # YouTube cookies (uploaded via UI)
├── backups/                 # nightly VACUUM INTO snapshots, last 7 kept
│   └── episodes-YYYYMMDD-HHMMSS.db
├── audio/
│   └── <channel_id>/
│       ├── <video_id>.mp3
│       └── ...
└── thumbnails/
    └── <channel_id>/
        ├── channel.jpg      # Channel cover art
        ├── <video_id>.jpg   # Per-episode thumbnails
        └── ...
```

### Database backup and restore

`episodes.db` holds every subscription and episode record — the audio files on
disk are meaningless without it. A snapshot is taken automatically every night
at **03:00** into `DATA_DIR/backups/episodes-YYYYMMDD-HHMMSS.db`, and the **7
most recent** are kept. The snapshot uses SQLite's `VACUUM INTO`, so it is safe
to take while a poll is writing and produces a single self-contained file with
no `-wal`/`-shm` sidecars to keep alongside it.

The same job runs `PRAGMA integrity_check`. If the database reports corruption,
or a backup can't be taken at all, you get an email (`ALERT_EMAIL`).

Restoring is **deliberately manual** — Slipcast will never overwrite your live
database on its own, since whatever corrupted it may still be doing so. Pick a
snapshot from before the problem and:

```bash
docker compose stop app
cp data/backups/episodes-YYYYMMDD-HHMMSS.db data/episodes.db
rm -f data/episodes.db-wal data/episodes.db-shm   # stale WAL from the old DB
docker compose start app
```

Removing the `-wal`/`-shm` pair matters: they belong to the database you just
replaced, and SQLite would otherwise replay that old write-ahead log over your
restored snapshot.

---

## Deploying

### Docker Hub (recommended)

The image is published to Docker Hub at `ericfaris/slipcast:latest`. Pull it with:

```bash
docker compose pull && docker compose up -d
```

No local build required.

> **Upgrading from `youtube-rss`?** This project's Compose project name is now `slipcast` (see `name:` in `docker-compose.yml`), so `docker compose up -d` creates fresh `slipcast-*` containers and leaves your old `youtube-rss-*` containers orphaned. Remove the old one with `docker rm -f youtube-rss-app-1` (or `docker compose -p youtube-rss down`). Your `./data` volume is unaffected — it's a host bind-mount, so audio, thumbnails, and the database carry over.

### Building locally

```bash
docker compose up --build
```

### Restart-on-unhealthy (autoheal)

The Docker healthcheck makes a wedged container *visible*, but Compose won't
restart one on its own. A host-side systemd timer that does — bounded to 3
restarts per hour, then emailing you instead of looping — ships in `ops/`.
It isn't installed by default; see **[ops/README.md](ops/README.md)** for the
install steps and how to check on it.

### Making the app publicly accessible

If your podcast app fetches feeds through its own servers (Pocket Casts does this), you need a public URL. The recommended approach is a **Cloudflare Tunnel** — free, no port forwarding required, works from any network including WSL2.

See **[CLOUDFLARE_TUNNEL.md](CLOUDFLARE_TUNNEL.md)** for full step-by-step instructions.

### Important: Do not deploy to Railway or other datacenter hosts

YouTube blocks requests from datacenter IP ranges. The app must run on your own hardware (home server, PC, NAS, etc.) where requests originate from a residential IP.

---

## YouTube Cookies

YouTube increasingly requires authentication to avoid rate limiting and to access some content. Upload a cookies file via the management UI:

1. Export cookies from your browser using a browser extension (e.g. "Get cookies.txt LOCALLY" for Chrome)
2. Go to the management UI → **YouTube Cookies** section → upload the file
3. Uncomment `COOKIES_FILE=/data/cookies.txt` in `docker-compose.yml`
4. Restart the container

Cookies expire every few weeks. Re-upload when downloads start failing.
