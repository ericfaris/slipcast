# Implementation Plan: Feed & Episode Management (Group 3 of 4)

**Companion brief:** `/home/eric/projects/slipcast/.claude/plans/feed-management-brief.md`
(read it too — this plan is the executable version of it, but the brief carries
the "why" behind a few decisions).

## Recommended executor model

**Opus 5.** This group touches 10+ files across DB schema migration, a
security-relevant shared-secret mechanism, a routing-order trap that fails
silently if gotten wrong, a new background job path, and two new UI modals —
more cross-system coupling and more "wrong in a way tests won't obviously
catch" surface than a routine feature.

---

## Summary

Slipcast currently serves every channel at a guessable public URL
(`/feed/<youtube_channel_id>.xml`), has no combined feed, no way to remove or
re-fetch a single bad episode, and hardcodes iTunes category/language/explicit
for every feed. This group adds four things: (1) opt-in per-channel feed access
tokens plus an install-wide token for the combined feed, gated behind a new
`REQUIRE_FEED_TOKENS` env var that defaults to **off** so existing podcast-app
subscriptions keep working; (2) a combined `GET /feed/all.xml` across all
*subscribed* channels, newest first, capped by `ALL_FEED_MAX_EPISODES`;
(3) `POST /episodes/delete` and `POST /episodes/redownload` with buttons in the
existing episode modal; (4) per-channel `itunes_category` / `itunes_language` /
`itunes_explicit` overrides with a new "Feed settings" modal, falling back to
today's hardcoded defaults when unset. All schema changes are additive
(three new nullable columns on `channels`, one on `unsubscribed_channels`, one
new `settings` key/value table) so nothing existing changes meaning.

---

## Branch & starting point

```bash
cd /home/eric/projects/slipcast
git fetch --all
git checkout main && git pull        # confirm you are on main, clean tree
git checkout -b feat/feed-episode-management
```

Do **not** branch from `feat/storage-retention` (Group 2, unrelated,
concurrently in flight). **Re-read every file before editing it** — Group 2 may
have merged into `main` between this plan being written and you executing it,
and it also edits `app/config.py` and `app/downloader.py`. This plan describes
`main` as of v1.12.0 (commit `158b2e3`); treat any difference you find in the
real file as the truth, and keep your edits additive so they merge cleanly.

---

## Approach & key decisions

### A. Route collision: `/feed/all.xml` vs `/feed/{channel_id}.xml` — VERIFIED

I tested this against the repo's actual pinned versions (FastAPI 0.138.0 /
Starlette 1.3.1, in `.venv`):

```
param route registered first  → GET /feed/all.xml  ==> matched {channel_id}, channel_id="all"
literal route registered first → GET /feed/all.xml  ==> matched the literal route
                                  GET /feed/UC123.xml ==> matched {channel_id}
```

Starlette matches routes **in registration order**, first match wins; there is
no literal-before-parameter preference. So:

> **Define `@app.get("/feed/all.xml")` physically ABOVE
> `@app.get("/feed/{channel_id}.xml")` in `app/main.py`.**

Rejected alternatives: (a) handling `"all"` as a special case *inside*
`get_feed()` — conflates two different token sources and two different response
builders in one handler, and makes the 404-vs-token logic muddier; (b) a
separate path prefix like `/feed/all/index.xml` — uglier URL, no benefit.
Ordering is the whole fix, but it is invisible and easy to regress, so it gets
its own test (see Testing §T3).

Also note `/feed/` is in `_PUBLIC_PREFIXES` in `app/main.py`, so both routes
bypass Basic Auth — that stays true and is the whole reason feed tokens exist.

### B. What feedgen actually validates — VERIFIED

Read from the installed
`.venv/lib/python3.12/site-packages/feedgen/ext/podcast.py` and
`feedgen/util.py`:

- **`fg.podcast.itunes_explicit(value)`** raises
  `ValueError('Invalid value for explicit tag')` for anything not in
  `('', 'yes', 'no', 'clean')`. So an unvalidated value reaching it would be a
  500. Validate against `{"yes", "no", "clean"}` server-side before storing.
- **`fg.podcast.itunes_category(value)`** does **NOT** validate against Apple's
  category list at all. A plain string goes through the deprecated
  string-argument path (`{'cat': value}`) into `util.ensure_format()`, which
  only checks that the dict's *keys* are within `{'cat','sub'}` and that `cat`
  is present. **Any string is accepted and emitted verbatim**, including an
  empty string (which would emit `<itunes:category text=""/>`). Therefore:
  - The **only** category validation is ours. Validate against an explicit
    tuple of Apple's top-level categories in `app/main.py` and return a clean
    **400** on anything else — do not rely on feedgen raising.
  - Treat an empty/whitespace value as "unset" (store `NULL`), never pass `""`
    to `itunes_category()`.
- **`fg.language(value)`** does no validation either — validate the language
  tag ourselves with a conservative BCP-47-ish regex.
- Defensive read path: `app/feed.py` must also fall back to the hardcoded
  defaults if a stored value is not in the allowed set (a hand-edited DB or a
  future category-list trim must not be able to 500 a live feed).

### C. Token model

- One `feed_token TEXT` column on `channels` **and** on
  `unsubscribed_channels` (the feed route serves both kinds of channel_id), and
  one row in the new `settings` table for the combined feed
  (`key='all_feed_token'`).
- **Both** a one-time backfill in `init_db()`'s migration block (for rows that
  already have a `channel_id`) **and** lazy get-or-create on access. Rationale:
  the backfill is trivially cheap (a handful of rows) and means the common case
  is already done; the lazy path covers rows added later and `channels` rows
  whose `channel_id` was still NULL at migration time (it is only populated by
  `update_channel_meta()` after the first successful poll). Every channel ends
  up with a token regardless of `REQUIRE_FEED_TOKENS`, so flipping enforcement
  on later needs no second migration — as the brief requires.
- Comparison uses `secrets.compare_digest` (already the pattern in
  `auth_middleware`), never `==`.
- **Fail closed:** when `REQUIRE_FEED_TOKENS` is true and no token can be found
  for the requested channel_id (e.g. orphaned episodes with no owning row),
  return 404 — never serve.
- 404 (not 401/403) on a bad or missing token, so an unauthenticated caller
  can't distinguish "wrong token" from "no such channel".
- Dashboard-surfaced URLs (`_feed_url()`) always append `?token=…` when a token
  exists, regardless of enforcement, so a copied URL is future-proof.

Rejected: a path segment (`/feed/<token>/<id>.xml`) — more routing complexity,
and it breaks every already-subscribed URL. Rejected: hashing tokens at rest —
this is a shared secret in a URL that we must be able to *display* in the share
modal, so it has to be stored in cleartext; the honest framing goes in the
README instead of a false sense of security.

### D. Combined feed shape

New `db.get_combined_episodes(limit)` returns episodes whose `channel_id` is in
`channels` (subscribed only), newest first. Use a subquery, **not** a JOIN:

```sql
SELECT * FROM episodes
WHERE channel_id IN (SELECT channel_id FROM channels WHERE channel_id IS NOT NULL)
ORDER BY published DESC
LIMIT ?
```

A `JOIN channels` would duplicate episode rows whenever two `channels` rows
share one `channel_id` (which really happens — see
`_resolve_channel_id_for_removal`'s comment about URL variants).

In `app/feed.py`, factor the per-episode entry building out of `build_feed()`
into a `_add_entry(fg, ep, channel_id)` helper (keeping the `is_safe_media_name`
checks, the `published` parse fallback, enclosure, duration, per-entry image)
and call it from both `build_feed()` and the new `build_combined_feed()`. The
combined feed additionally sets `fe.podcast.itunes_author(ep["channel_name"])`
per entry so a listener can tell channels apart; the per-channel feed's output
stays byte-identical to today for an unconfigured channel.

### E. Episode delete vs re-download

- The `episodes.id` column **is** the YouTube video id (see
  `_download_entry()`'s returned dict), so `episode_id` doubles as the video id
  for the re-download path. No extra lookup column needed.
- Delete removes the audio file, the thumbnail file, and the DB row, and
  deliberately does **not** write a `skip_videos` row — unlike `_prune_channel`,
  which does, because a *pruned* episode must not come back. An explicit user
  delete should be re-downloadable on the next poll.
- Re-download bypasses `_download_entry()`'s
  `if os.path.exists(expected_file): return None` short-circuit by deleting the
  expected file first (via the existing `_remove_if_exists`), then calling
  `_download_entry()` normally. It works whether or not the file/row still
  exists, which makes "delete, then re-download" and "just re-download a
  corrupt file" the same code path.
- Re-download runs on a background thread wired through `app/jobs.py`
  (`jobs.start("download", …)` / `jobs.finish(...)`) exactly like `_run_download`,
  so the dashboard spinner and toast work with no client changes.
- We do **not** clear any existing `skip_videos` row on re-download. Skips are
  poll-time policy; an explicit re-download already bypasses them, and deleting
  a "pruned" skip row would re-open the prune/re-download loop that
  `_prune_channel`'s comment documents.

---

## Data / model / API changes

### Schema (all additive, in `init_db()`'s migration block)

| Table | Column | Type | Notes |
|---|---|---|---|
| `channels` | `feed_token` | `TEXT` NULL | `secrets.token_urlsafe(24)` |
| `channels` | `itunes_category` | `TEXT` NULL | NULL ⇒ `"Technology"` |
| `channels` | `itunes_language` | `TEXT` NULL | NULL ⇒ `"en"` |
| `channels` | `itunes_explicit` | `TEXT` NULL | NULL ⇒ `"no"` |
| `unsubscribed_channels` | `feed_token` | `TEXT` NULL | token only — no metadata overrides (out of scope) |

New table:

```sql
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
```

### New env vars (`app/config.py`)

| Var | Default | Meaning |
|---|---|---|
| `REQUIRE_FEED_TOKENS` | `false` | When true, feed requests must carry the matching `?token=` |
| `ALL_FEED_MAX_EPISODES` | `100` | Cap on items in `/feed/all.xml` |

Parse the boolean the same conservative way as the rest of the file:
`REQUIRE_FEED_TOKENS = os.environ.get("REQUIRE_FEED_TOKENS", "").strip().lower() in ("1", "true", "yes", "on")`.

### Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/feed/all.xml?token=` | public prefix | **must be registered before** `/feed/{channel_id}.xml` |
| `GET` | `/feed/{channel_id}.xml?token=` | public prefix | gains optional `token` query param |
| `POST` | `/episodes/delete` | Basic Auth + CSRF | Form: `episode_id`; 404 if unknown |
| `POST` | `/episodes/redownload` | Basic Auth + CSRF | Form: `episode_id`; 404 if unknown; returns immediately, work on a thread |
| `POST` | `/channels/feed-settings` | Basic Auth + CSRF | Form: `channel_id`, `category`, `language`, `explicit`; 400 invalid, 404 unknown channel |

`/api/state` gains a top-level `"all_feed_url"` string, and each channel's
`feed_url` now carries `?token=…` when a token exists.

---

## Step-by-step tasks

Each step should leave the suite green (`.venv/bin/python -m pytest -q`).

### 1. `app/config.py` — new env vars

Append `REQUIRE_FEED_TOKENS` and `ALL_FEED_MAX_EPISODES` under a new
`# --- Feed access / combined feed ---` banner comment, matching the existing
comment density (a short paragraph explaining *why*, like `MIN_FREE_DISK_GB`
has). Read the current file first — Group 2 may have added sections below
`COOKIE_EXPIRY_WARN_DAYS`; append after whatever is last.

### 2. `app/database.py` — migration + helpers

In `init_db()`, after the existing `episodes.thumbnail` migration:

```python
# migrate existing DBs — new columns are always nullable so an older DB
# keeps working untouched until something writes to them.
ch_cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
for col in ("feed_token", "itunes_category", "itunes_language", "itunes_explicit"):
    if col not in ch_cols:
        conn.execute(f"ALTER TABLE channels ADD COLUMN {col} TEXT")
un_cols = {r[1] for r in conn.execute("PRAGMA table_info(unsubscribed_channels)").fetchall()}
if "feed_token" not in un_cols:
    conn.execute("ALTER TABLE unsubscribed_channels ADD COLUMN feed_token TEXT")
conn.execute("""CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
```

then a one-time backfill in the same transaction (each row needs its **own**
token, so this is a per-row loop, not one `UPDATE`):

```python
for row in conn.execute("SELECT url FROM channels WHERE feed_token IS NULL").fetchall():
    conn.execute("UPDATE channels SET feed_token = ? WHERE url = ?",
                 (secrets.token_urlsafe(24), row["url"]))
for row in conn.execute(
        "SELECT channel_id FROM unsubscribed_channels WHERE feed_token IS NULL").fetchall():
    conn.execute("UPDATE unsubscribed_channels SET feed_token = ? WHERE channel_id = ?",
                 (secrets.token_urlsafe(24), row["channel_id"]))
```

(`secrets` is already imported in `app/database.py` — used by `backup_db()`.)

New helpers, grouped under a `# --- feed access tokens ---` banner following
the file's existing sectioning style:

- `get_feed_token(channel_id) -> str | None` — looks in `channels` first, then
  `unsubscribed_channels`. Returns `None` if neither has a row.
- `get_or_create_feed_token(channel_id) -> str | None` — inside **one**
  `get_conn()` block: SELECT; if the row exists and the token is NULL,
  `UPDATE … SET feed_token = ? WHERE channel_id = ? AND feed_token IS NULL`,
  then re-SELECT and return the stored value. The `AND feed_token IS NULL`
  guard plus the re-read means two racing threads converge on one token rather
  than the second clobbering the first (writers serialize under WAL +
  `busy_timeout`). Returns `None` when no row owns that channel_id.
- `get_setting(key) -> str | None`, `set_setting(key, value)`.
- `get_or_create_all_feed_token() -> str` — same create-if-missing pattern
  against `settings` with key `"all_feed_token"`, using
  `INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)` then re-SELECT
  so a race can't produce two different values.
- `get_episode(episode_id) -> sqlite3.Row | None`.
- `get_combined_episodes(limit) -> list[sqlite3.Row]` — the subquery SQL from
  §D above, with the "why not a JOIN" reason as a short comment.
- `get_channel_feed_settings(channel_id) -> sqlite3.Row | None` — selects the
  three itunes columns from `channels` (returns `None` for an unsubscribed or
  unknown channel_id, which is what makes the defaults apply).
- `set_channel_feed_settings(channel_id, category, language, explicit)` —
  `UPDATE channels SET … WHERE channel_id = ?`; callers pass `None` to clear.

Also add `feed_token` to nothing else — `get_channels()` is `SELECT *`, so the
new columns flow to `/api/state` callers automatically; make sure nothing
serializes the raw row to JSON (it doesn't today — `api_state` builds explicit
dicts — **keep it that way; never put `feed_token` in a response body except
inside the feed URL itself**).

### 3. `app/feed.py` — per-channel metadata + shared entry builder + combined feed

- Import `ALL_FEED_MAX_EPISODES` from `app.config`.
- Add module-level defaults + allow-lists so a bad stored value can't break a
  live feed:
  ```python
  _DEFAULT_CATEGORY = "Technology"
  _DEFAULT_LANGUAGE = "en"
  _DEFAULT_EXPLICIT = "no"
  ```
  Import the shared category tuple rather than duplicating it — put
  `ITUNES_CATEGORIES` (see step 4) in **`app/feed.py`** and import it into
  `app/main.py`, so validation and rendering can never drift. (Feed module is
  the lower-level one; `main` already imports from `feed`.)
- Extract `_add_entry(fg, ep, channel_id)` from the existing `for ep in
  episodes:` loop — verbatim logic, returning the created `fe` (or `None` when
  the filename is unsafe and the item is skipped).
- `build_feed()` keeps its current structure but resolves metadata:
  ```python
  meta = db.get_channel_feed_settings(channel_id)
  category = (meta["itunes_category"] if meta else None) or _DEFAULT_CATEGORY
  if category not in ITUNES_CATEGORIES:   # hand-edited DB — never 500 a feed
      category = _DEFAULT_CATEGORY
  ```
  same shape for language (`or _DEFAULT_LANGUAGE`) and explicit (fall back
  unless in `{"yes","no","clean"}`).
- New `build_combined_feed() -> bytes`:
  - `episodes = db.get_combined_episodes(ALL_FEED_MAX_EPISODES)`; return `b""`
    when empty (same contract as `build_feed`, so the route's 404 logic is
    uniform).
  - Feed-level: id/self link `f"{BASE_URL}/feed/all.xml"`, title
    `"Slipcast — All Channels"`, description
    `"Every subscribed channel in one feed."` (adjust wording freely),
    `fg.language(_DEFAULT_LANGUAGE)`, `itunes_explicit(_DEFAULT_EXPLICIT)`,
    `itunes_category(_DEFAULT_CATEGORY)`, image
    `f"{BASE_URL}/static/cover-512.png"`.
  - Per entry: `_add_entry(fg, ep, ep["channel_id"])`, then
    `fe.podcast.itunes_author(ep["channel_name"])` when `fe` is not None.

### 4. `app/main.py` — token plumbing, routes, validation

Imports: add `ALL_FEED_MAX_EPISODES`, `REQUIRE_FEED_TOKENS` to the
`app.config` import; add `build_combined_feed`, `ITUNES_CATEGORIES` to the
`app.feed` import; add `redownload_episode`, `delete_episode_files` to the
`app.downloader` import.

Helpers (near `_feed_url`):

```python
def _feed_url(channel_id: str) -> str:
    """Absolute feed URL, carrying the channel's access token when it has one.

    The token is appended even when REQUIRE_FEED_TOKENS is off, so a URL copied
    from the dashboard today keeps working the moment enforcement is turned on.
    """
    url = f"{BASE_URL}/feed/{channel_id}.xml"
    token = db.get_or_create_feed_token(channel_id)
    return f"{url}?token={token}" if token else url


def _all_feed_url() -> str: ...            # same shape, /feed/all.xml + settings token


def _token_ok(expected: str | None, provided: str | None) -> bool:
    """Constant-time token check. Inert (always True) until REQUIRE_FEED_TOKENS
    is on; fails closed once it is — a channel with no stored token is not
    servable rather than servable by anyone."""
    if not REQUIRE_FEED_TOKENS:
        return True
    if not expected or not provided:
        return False
    return secrets.compare_digest(expected, provided)
```

Validation constants near `_CHANNEL_ID_RE`:

```python
_EXPLICIT_VALUES = frozenset({"yes", "no", "clean"})
# Loose BCP-47: "en", "en-US", "pt-BR". Deliberately not exhaustive.
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
```

`ITUNES_CATEGORIES` lives in `app/feed.py` (Apple's top-level list; no
subcategories — deliberately):

```python
ITUNES_CATEGORIES = (
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government",
    "Health & Fitness", "History", "Kids & Family", "Leisure", "Music",
    "News", "Religion & Spirituality", "Science", "Society & Culture",
    "Sports", "Technology", "True Crime", "TV & Film",
)
```

Routes — **in this order**, in the "Feed endpoints" section:

```python
@app.get("/feed/all.xml", response_class=Response)
def get_combined_feed(token: str | None = None):
    # Registered BEFORE /feed/{channel_id}.xml on purpose: Starlette matches in
    # registration order with no literal-over-parameter preference, so the
    # parameterised route would otherwise swallow this with channel_id="all".
    if not _token_ok(db.get_or_create_all_feed_token(), token):
        raise HTTPException(status_code=404, detail="Not found")
    rss = build_combined_feed()
    if not rss:
        raise HTTPException(status_code=404, detail="No episodes yet")
    return Response(content=rss, media_type="application/rss+xml")


@app.get("/feed/{channel_id}.xml", response_class=Response)
def get_feed(channel_id: str, token: str | None = None):
    if not _CHANNEL_ID_RE.match(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    # 404 rather than 401 on a bad token: don't confirm to an unauthenticated
    # caller that the channel_id is even real.
    if not _token_ok(db.get_feed_token(channel_id), token):
        raise HTTPException(status_code=404, detail="Channel not found or no episodes yet")
    rss = build_feed(channel_id)
    ...
```

Use `db.get_feed_token` (read-only) on the request path, not the
get-or-create variant — a feed fetch shouldn't write; the backfill and
`_feed_url()` already guarantee a token exists.

New mutating endpoints (place them next to the existing `/episodes/download`
and `/channels/*` handlers):

```python
@app.post("/episodes/delete")
def delete_episode_endpoint(episode_id: str = Form(...)):
    ep = db.get_episode(episode_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    delete_episode_files(ep["channel_id"], ep["filename"], ep["thumbnail"])
    db.delete_episode(episode_id)
    # Deliberately no skip_videos row — unlike a prune or a members-only skip,
    # an explicit delete should be re-downloadable on the next poll.
    return _ok("Episode deleted")


@app.post("/episodes/redownload")
def redownload_episode_endpoint(episode_id: str = Form(...)):
    ep = db.get_episode(episode_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Episode not found")
    threading.Thread(target=_run_redownload,
                     args=[ep["id"], ep["channel_id"], ep["channel_name"], ep["title"]],
                     daemon=True).start()
    return _ok("Re-downloading episode")


@app.post("/channels/feed-settings")
def set_feed_settings(channel_id: str = Form(...), category: str = Form(""),
                      language: str = Form(""), explicit: str = Form("")):
    # empty string == "use the default" (stores NULL), so the form can clear a value
```
Validation order: channel_id regex → 400; each non-empty value checked
(`category in ITUNES_CATEGORIES`, `explicit in _EXPLICIT_VALUES`,
`_LANGUAGE_RE.match(language)`) → 400 with a specific `detail`;
`db.get_channel_meta(channel_id)` missing → 404; then
`db.set_channel_feed_settings(...)` with `None` for blanks →
`_ok("Feed settings saved")`.

Add `_run_redownload(video_id, channel_id, channel_name, title)` beside
`_run_download` in the "Background job wrappers" section: `jid = jobs.start(
"download", title or video_id)`, call `redownload_episode(...)`, `db.upsert_episode(result)`
when it returns a row, `jobs.finish(jid, "success"/"error", …)`, wrapped in
try/except with `logger.exception` — mirror `_run_download` exactly.

`api_state()`: add `"all_feed_url": _all_feed_url()` to the returned JSON
(next to `"version"`), and add `"itunes"` metadata to each subscribed channel
dict so the settings modal can pre-fill:
`"feed_settings": {"category": ch["itunes_category"], "language": ch["itunes_language"], "explicit": ch["itunes_explicit"]}`
(`db.get_channels()` is `SELECT *`, so the columns are already on the row —
but guard with `ch["itunes_category"] if "itunes_category" in ch.keys() else None`
only if you find a code path that can serve rows from a pre-migration DB; it
should not exist, since `init_db()` runs at startup).

Add the modal markup to `_PAGE`, following the `#share-modal` / `#ep-modal`
pattern exactly (backdrop + `modal-card`, `data-close` button, `role="dialog"`,
`aria-modal`, `aria-labelledby`):

```html
<!-- Per-channel feed settings dialog -->
<div id="fs-modal" class="modal" hidden>
  … <h3 id="fs-title">Feed settings</h3>
  <p id="fs-name" class="share-name"></p>
  <form id="fs-form"> … <select id="fs-category">…</select>
      <input id="fs-language" type="text" placeholder="en">
      <select id="fs-explicit"><option value="no">No</option>
        <option value="yes">Yes</option><option value="clean">Clean</option></select>
      <button class="btn btn-primary" type="submit">Save</button> </form>
</div>
```

The category `<option>`s can be static HTML in `_PAGE` (simplest, no new API)
— keep the list identical to `ITUNES_CATEGORIES`, with a first
`<option value="">Default (Technology)</option>`. If you'd rather not
hand-sync two lists, render the `<select>` server-side by string-formatting
`ITUNES_CATEGORIES` into `_PAGE` at import time; either is acceptable, but pick
one and note it in a comment.

### 5. `app/downloader.py` — file removal helper + `redownload_episode`

Purely additive; do not refactor `_prune_channel`, `_ydl_opts`,
`_enforce_disk_floor`, or anything else (Group 2 territory).

```python
def delete_episode_files(channel_id: str, filename: str | None,
                         thumbnail: str | None = None) -> None:
    """Remove one episode's audio + thumbnail from disk (safe-name guarded)."""
    if not _CHANNEL_ID_RE.match(channel_id or ""):
        logger.error("Refusing to delete files for suspicious channel_id: %r", channel_id)
        return
    if is_safe_media_name(filename):
        _remove_if_exists(os.path.join(_audio_dir_for(channel_id), filename))
    if is_safe_media_name(thumbnail):
        _remove_if_exists(os.path.join(_thumbnail_dir_for(channel_id), thumbnail))


def redownload_episode(video_id: str, channel_id: str, channel_name: str) -> dict | None:
    """Force a fresh download of one video, ignoring _download_entry's
    "already on disk" short-circuit.

    That check exists so a normal poll doesn't redo work; an explicit user
    re-download (a corrupt or truncated file) has to override it, so the
    existing file is removed first.
    """
```

Import `is_safe_media_name` from `app.safety` at the top of
`app/downloader.py` (it is not imported there today — check before adding).

`redownload_episode` body: validate `video_id` against `_VIDEO_ID_RE` (return
`None` + warn if not), remove the expected audio file, then
`return _download_entry({"id": video_id}, channel_id, channel_name)` inside a
`try/except MemberOnlyError` that logs and returns `None`.

> **Watch-out:** `_download_entry()` computes `expected_file` as
> `os.path.join(audio_dir, f"{video_id}.mp3")` on today's `main`. **Read the
> current file** — if Group 2 has merged and made the extension config-driven
> (`AUDIO_CODEC`), derive the path exactly the way `_download_entry` does
> (extract a tiny `_expected_audio_path(channel_id, video_id)` helper and use it
> in both places) rather than hardcoding `.mp3` here.

### 6. `app/static/app.js` — three UI additions

All following the existing patterns: `el()` builder, `act()` for
POST+refresh+toast, `fd()` for form bodies, `btn btn-ghost btn-sm` /
`btn btn-danger-ghost btn-sm`.

1. **Episode row actions** — in `episodeRow(ep)`, append to the row (after
   `playWrap`) an actions container with:
   - `Re-download` → `act(() => postForm('/episodes/redownload', fd({ episode_id: ep.id })))`
   - `Delete` (danger-ghost) → `confirm(...)` then
     `act(() => postForm('/episodes/delete', fd({ episode_id: ep.id })))`
   `act()` calls `loadState()`, which refreshes counts but **not** the open
   episode modal — so after either action, also re-run the modal's fetch. The
   clean way: store the channel currently shown (`state.epChannel = ch` in
   `openEpisodes`) and add `async function refreshEpisodes()` that re-fetches
   and re-renders `#ep-list`; call it after the `act()` promise resolves.
2. **Feed settings** — a `Feed settings` button in `subscribedCard(ch)`'s
   `.ch-actions` (subscribed cards only, not `oneoffCard`), opening
   `openFeedSettings(ch)`: pre-fill the selects/input from
   `ch.feed_settings`, submit via
   `act(() => postForm('/channels/feed-settings', fd({channel_id, category, language, explicit})))`,
   then close. Wire `data-close` handlers and add `closeFeedSettings()` to the
   `Escape` keydown handler alongside `closeShare/closeEpisodes/closeSettings`.
   Disable the button when `!ch.channel_id` (same reasoning as the Share
   button's `Feed appears after the first successful poll`).
3. **Combined feed affordance** — in the "Subscribed channels" `.section-head`
   (`_PAGE`), add `<button id="all-feed-share" class="btn btn-ghost btn-sm"
   type="button" hidden>Share all-channels feed</button>`; in `render()`, unhide
   it when `d.all_feed_url && d.channels.length` and wire it in `init()` to
   `openShare({ name: 'All channels', feed_url: state.data.all_feed_url })` —
   `openShare` already only reads `.name` and `.feed_url`, so no changes there.

`app/static/styles.css`: add only what's needed — an `.ep-actions { flex: none;
display: flex; gap: 6px; }` and, if the row gets crowded, allow `.ep-row` to
wrap on narrow widths. Keep it minimal and in the file's existing terse style.

### 7. Version, changelog, README, docker-compose

- `app/__init__.py`: **read the current value**, bump MINOR, reset PATCH
  (on `main` at v1.12.0 that means `1.13.0`; if Group 2 landed 1.13.0 first,
  use `1.14.0`).
- `app/changelog.py`: new entry at the **top** of `CHANGELOG` with the matching
  version, `"date": "2026-09-03"` (or the actual date at execution), and 4
  user-facing bullets in the existing prose voice (full sentences, explaining
  the user-visible problem and the fix — not commit-message style): feed
  tokens (and that they're off by default and don't break existing
  subscriptions), the all-channels feed, per-episode delete/re-download, and
  per-channel feed metadata.
- `README.md`:
  - Config table: `REQUIRE_FEED_TOKENS` (`false`) and `ALL_FEED_MAX_EPISODES`
    (`100`) rows.
  - API Endpoints table: `/feed/all.xml`, `/episodes/delete`,
    `/episodes/redownload`, `/channels/feed-settings` rows with the right Auth
    column values (`None` for the feed, `Required` for the three POSTs).
  - "RSS Feeds" section: a new **"Feed access tokens"** subsection stating
    plainly: it protects against someone *guessing or enumerating* a feed URL;
    it does **not** protect a URL that has been shared, logged by a proxy, or
    copied — it is a shared secret in a URL, like most podcast-app private
    feeds, not per-listener authentication; how to enable it
    (`REQUIRE_FEED_TOKENS=true`, restart, and copy fresh URLs from the
    dashboard); and that enabling it does **not** invalidate URLs already saved
    in a podcast app, because the token they embedded is the same one.
  - Mention the combined feed URL and the new episode Delete / Re-download
    buttons in the Management UI section.
- `docker-compose.yml`: add both env vars with a short comment each, in the
  style of the existing `MIN_FREE_DISK_GB` entry. Set
  `REQUIRE_FEED_TOKENS=false` explicitly so the knob is discoverable.

### 8. Tests (see next section), then full suite, then deploy.

---

## Testing & verification

Run everything from the repo root with the repo venv:

```bash
cd /home/eric/projects/slipcast
.venv/bin/python -m pytest -q
```

Follow the existing fixture style: `_setup_tmp(tmp_path, monkeypatch)`
monkeypatching `db.DB_PATH` then `db.init_db()`; the `yt_dlp` stub in
`tests/conftest.py`; route handlers called **directly** (as
`tests/test_endpoints.py` does) rather than through a server, except for the
one routing test that genuinely needs the router.

Mapping to the brief's acceptance criteria:

**AC1 + AC2 — token enforcement** (`tests/test_endpoints.py`)
- `test_feed_without_token_works_when_enforcement_off`: seed a channel +
  episode, `monkeypatch.setattr(main, "REQUIRE_FEED_TOKENS", False)`, call
  `main.get_feed(CID)` → 200-ish `Response` with `application/rss+xml` and a
  non-empty body.
- `test_feed_requires_token_when_enforcement_on`: enforcement True; no token →
  `HTTPException` **404**; wrong token → 404; correct token
  (`db.get_or_create_feed_token(CID)`) → feed body.
- `test_feed_token_404s_when_channel_has_no_row`: enforcement on, episodes
  exist for a channel_id with no owning row → 404 (fail-closed).
- `test_feed_url_includes_token`: `main._feed_url(CID)` contains `?token=` and
  the value equals the stored token.

**AC3 — combined feed** (`tests/test_feed.py`)
- `test_combined_feed_merges_subscribed_channels_newest_first`: two subscribed
  channels (`db.add_channel(url)` + `db.update_channel_meta(url, cid, name)`)
  plus one `upsert_unsubscribed_channel` one-off with its own episodes; assert
  the item titles are exactly the subscribed ones, in descending `published`
  order, and that no one-off title appears.
- `test_combined_feed_respects_cap`: `monkeypatch.setattr(feed,
  "ALL_FEED_MAX_EPISODES", 5)`; seed 12 → exactly 5 items, and they are the 5
  newest.
- `test_combined_feed_empty_returns_empty_bytes`.

**AC3 (routing) — T3** (`tests/test_endpoints.py`)
- `test_all_feed_route_is_not_swallowed_by_channel_route`: assert on the router
  itself (cheap and precise):
  ```python
  paths = [getattr(r, "path", None) for r in main.app.router.routes]
  assert paths.index("/feed/all.xml") < paths.index("/feed/{channel_id}.xml")
  ```
  plus a live check with `starlette.testclient.TestClient(main.app)` —
  **verified working without entering the `with` block**, so the app's lifespan
  (scheduler + initial poll) does not start: `client.get("/feed/all.xml")`
  must reach the combined handler (404 "No episodes yet" on an empty DB is a
  fine assertion — the point is it must not report *channel* not-found; assert
  on the resolved endpoint or on a seeded non-empty body to be unambiguous).

**AC4 — delete** (`tests/test_endpoints.py`)
- `test_delete_episode_removes_row_and_files`: create real temp audio +
  thumbnail files (monkeypatch `downloader.AUDIO_DIR` / `downloader.THUMBNAIL_DIR`
  to `tmp_path`), insert the row, call `main.delete_episode_endpoint(episode_id=...)`;
  assert both files are gone, `db.get_episode(...)` is `None`, and
  `db.get_skip_video_ids(CID) == set()`.
- `test_delete_episode_404s_for_unknown_id`.

**AC5 — re-download** (`tests/test_endpoints.py` or `tests/test_polling.py`)
- `test_redownload_replaces_existing_file`: write a file at the expected path
  with junk contents; monkeypatch `downloader._download_entry` with a stub that
  asserts the file is **already gone** when it is called and writes a fresh
  file + returns a row dict; call `downloader.redownload_episode(...)`; assert
  the returned row and the new file contents. This proves the short-circuit
  bypass without touching yt-dlp.
- `test_redownload_endpoint_404s_for_unknown_id`.
- Optionally `test_redownload_endpoint_starts_job`: monkeypatch
  `main.redownload_episode` and `threading.Thread` (or just assert the 200 body)
  — keep it simple; the thread wrapper is thin.

**AC6 — per-channel feed metadata** (`tests/test_feed.py`)
- `test_feed_uses_defaults_when_unset`: parse the XML with `ElementTree` and
  assert `<language>en</language>`, `itunes:explicit == "no"`,
  `itunes:category text="Technology"` (namespace URI is
  `http://www.itunes.com/dtds/podcast-1.0.dtd`).
- `test_feed_uses_channel_overrides`: `db.set_channel_feed_settings(CID,
  "Comedy", "es", "clean")` → the same three assertions with the new values.
- `test_feed_falls_back_on_invalid_stored_category`: write a junk category
  straight into the DB → feed still builds and emits `Technology`.
- `tests/test_endpoints.py`: `test_feed_settings_rejects_bad_values` (400 for a
  bogus category, a bogus explicit, a bogus language),
  `test_feed_settings_404s_for_unknown_channel`, and
  `test_feed_settings_blank_clears_to_null`.

**Database-level** (`tests/test_database.py`)
- `test_migration_adds_columns_and_backfills_tokens`: create a DB the *old* way
  (build the pre-migration `channels`/`unsubscribed_channels` tables by hand
  with raw SQL in a temp file, insert a row), then run `db.init_db()` and assert
  the new columns exist and every row got a distinct non-empty token.
- `test_get_or_create_feed_token_is_stable`: two calls return the same value.
- `test_all_feed_token_is_stable`.
- `test_get_combined_episodes_excludes_unsubscribed_and_dedupes`: include the
  two-`channels`-rows-one-`channel_id` case, asserting no duplicate items.
- `test_get_episode_returns_none_for_unknown`.

**AC7**: `.venv/bin/python -m pytest -q` — all green, no new warnings that
look like real breakage.

**AC8**: confirm by inspection — `grep -n "REQUIRE_FEED_TOKENS\|ALL_FEED_MAX_EPISODES" README.md docker-compose.yml app/config.py`,
and `tests/test_changelog.py` already asserts changelog/version consistency, so
a mismatch there fails the suite.

**AC9 — local deploy against real data** (deploy is local-only; there is no
Docker Hub / CI step for this project):

```bash
cd /home/eric/projects/slipcast
docker compose build && docker compose up -d
sleep 20
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost:8000/health
curl -sS http://localhost:8000/feed/all.xml | head -30          # expect RSS, many channels
CID=$(sqlite3 data/episodes.db "select channel_id from channels where channel_id is not null limit 1")
curl -sS "http://localhost:8000/feed/$CID.xml" | head -20        # existing per-channel feed still resolves
sqlite3 data/episodes.db "select url, substr(feed_token,1,6) from channels;"   # every row has a token
docker compose logs --tail=50 app
```
With `REQUIRE_FEED_TOKENS` left at `false`, the token-less curl above **must**
still return a feed — that is AC1 proven against production data. Optionally
flip it to `true` in `docker-compose.yml`, `docker compose up -d`, confirm the
token-less URL 404s and the dashboard-copied URL works, then flip it back.

Also open `http://localhost:8000/` and click through: the all-channels share
button, a channel's Feed settings modal (save + reopen shows the saved values),
and an episode's Re-download and Delete buttons.

---

## Risks & watch-outs

1. **Route order is silent when wrong.** `/feed/all.xml` defined after
   `/feed/{channel_id}.xml` returns a *plausible* 404 ("channel not found"),
   not an error — you will think the combined feed is broken and debug the
   wrong file. The router-order assertion test is the guard; keep it.
2. **Never leak `feed_token` into an API response** except embedded in the feed
   URL itself. `api_state` builds explicit dicts today — don't "simplify" it to
   `dict(row)`.
3. **Fail closed.** `_token_ok` must return False when `expected` is falsy and
   enforcement is on. A `secrets.compare_digest(None, x)` also raises — the
   early `if not expected or not provided` guard prevents a 500.
4. **`compare_digest` type mismatch:** both arguments must be `str` (or both
   `bytes`). A `None` from the query param is handled by the guard above.
5. **`_feed_url()` now writes to the DB** (get-or-create) and is called once per
   channel inside `api_state`, which the dashboard polls every few seconds.
   After the first call every row has a token so it's a pure read, but keep the
   get-or-create query cheap and don't move it inside a loop-over-episodes.
6. **`channels.channel_id` can be NULL** until the first successful poll — token
   lookup by channel_id simply finds nothing then, and `api_state` already
   guards with `if cid else None`. Don't assume every `channels` row is
   addressable by channel_id.
7. **Don't JOIN for the combined feed** — duplicate `channels` rows sharing one
   `channel_id` are a real, documented condition in this codebase.
8. **Delete must not add a `skip_videos` row**; `_prune_channel` does and that
   difference is deliberate. Do not "unify" the two paths.
9. **`_download_entry`'s hardcoded `.mp3`**: if Group 2 has merged, the audio
   extension may now be config-driven. Read the file; derive the expected path
   the same way it does.
10. **Additive-only in `app/downloader.py` and `app/config.py`** — these are the
    two files Group 2 also edits. Append new functions/vars at the end of their
    section rather than restructuring, so the merge is trivial.
11. **feedgen `itunes_explicit` raises `ValueError`** on a bad value — an
    unvalidated string reaching `build_feed` would 500 a *public* endpoint.
    Validate at write time **and** fall back defensively at read time.
12. **feedgen `itunes_category` validates nothing** — do not skip your own
    allow-list check on the assumption the library covers it.
13. **CSRF**: the three new POSTs go through `auth_middleware`'s CSRF check
    automatically (they're POSTs and not under `_PUBLIC_PREFIXES`). Don't add
    them to `_MUTATING_GET_PATHS` or `_PUBLIC_PREFIXES`.
14. **CSP is strict** (`script-src 'self'`, no eval) — the new modal JS must be
    plain DOM code in `app/static/app.js`, never inline `<script>` or
    `onclick="..."` attributes in `_PAGE`.
15. **Migration idempotency**: `init_db()` runs on every startup. The
    `PRAGMA table_info` guards and `WHERE feed_token IS NULL` backfill make it
    safe to re-run; test that calling `init_db()` twice doesn't rotate tokens
    (a rotated token silently breaks every subscribed podcast app once
    enforcement is on — the single worst failure mode in this group).

---

## Out of scope (do not build)

- Feed-metadata overrides for `unsubscribed_channels` (one-off feeds keep the
  hardcoded defaults).
- A combined feed for one-off/unsubscribed channels.
- Any change to the identity model or `channels.url` as primary key — that's
  Group 4.
- Group 1 (resilience, already shipped) and Group 2 (storage/codec/retention):
  do not touch `_ydl_opts()`, disk-pressure pruning, or backup code.
- Real per-viewer authentication on feeds. Tokens are a shared secret in a URL.
  Document that honestly; don't build accounts, per-device tokens, rotation UI,
  or expiry.
- Refactoring `_prune_channel`, `poll_channel`, or the existing share modal
  beyond what's listed above.
