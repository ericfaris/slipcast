"""Tests for the cookie-upload validation and test-email endpoints.

These call the route handlers directly so we don't boot the scheduler/app.
"""
import asyncio
import io
import os
import time

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.requests import Request

from app import __version__, config, database as db, downloader, main, notify


def _req(method="GET", path="/", headers=None, client=("8.8.8.8", 1234)):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": method, "path": path, "headers": raw,
        "query_string": b"", "client": client, "scheme": "https",
        "server": ("slipcast.example", 443),
    }
    return Request(scope)

VALID = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tabc\n"


def _upload(content: bytes):
    headers = Headers({"content-type": "text/plain"})
    return StarletteUploadFile(
        file=io.BytesIO(content), size=len(content),
        filename="cookies.txt", headers=headers,
    )


def test_upload_valid_cookies_saved(tmp_path, monkeypatch):
    dest = tmp_path / "cookies.txt"
    monkeypatch.setattr(main, "COOKIES_FILE", str(dest))
    resp = asyncio.run(main.upload_cookies(_upload(VALID)))
    assert resp.status_code == 200  # JSON {ok: true} consumed by the dashboard
    assert dest.read_bytes() == VALID


def test_upload_empty_cookies_rejected(tmp_path, monkeypatch):
    dest = tmp_path / "cookies.txt"
    monkeypatch.setattr(main, "COOKIES_FILE", str(dest))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.upload_cookies(_upload(b"")))
    assert exc.value.status_code == 400
    assert not dest.exists()  # nothing written


def test_upload_invalid_does_not_clobber_existing(tmp_path, monkeypatch):
    dest = tmp_path / "cookies.txt"
    dest.write_bytes(VALID)  # a previously-working file
    monkeypatch.setattr(main, "COOKIES_FILE", str(dest))
    with pytest.raises(HTTPException):
        asyncio.run(main.upload_cookies(_upload(b"garbage, not cookies")))
    assert dest.read_bytes() == VALID  # original preserved
    assert not (tmp_path / "cookies.txt.upload").exists()  # temp cleaned up


def test_test_email_unconfigured_returns_400(monkeypatch):
    monkeypatch.setattr(notify, "_smtp_configured", lambda: False)
    with pytest.raises(HTTPException) as exc:
        main.test_email()
    assert exc.value.status_code == 400


def test_test_email_sends_when_configured(monkeypatch):
    monkeypatch.setattr(notify, "_smtp_configured", lambda: True)
    sent = {}
    monkeypatch.setattr(notify, "send_cookie_alert", lambda force=False: sent.setdefault("f", force) or True)
    resp = main.test_email()
    assert resp.status_code == 200  # JSON {ok: true}
    assert sent["f"] is True  # forced past the cooldown


def _health_body(resp):
    import json
    return json.loads(bytes(resp.body))


def test_health_reports_version_when_healthy(monkeypatch):
    # No channels configured -> polling check is a trivial pass; scheduler up;
    # cookies valid — every check should pass and report 200/"ok".
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [])

    class _FakeSched:
        running = True
    monkeypatch.setattr(main, "_scheduler", _FakeSched())
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": True, "expired": False})

    resp = main.health()
    body = _health_body(resp)
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["version"] == main.VERSION
    assert body["checks"]["scheduler"] == "running"


def test_health_degraded_when_scheduler_missing(monkeypatch):
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [])
    monkeypatch.setattr(main, "_scheduler", None)
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": True, "expired": False})

    resp = main.health()
    body = _health_body(resp)
    assert resp.status_code == 503
    assert body["status"] == "degraded"
    assert "scheduler is not running" in body["problems"]


def test_health_degraded_when_cookies_missing(monkeypatch):
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [])

    class _FakeSched:
        running = True
    monkeypatch.setattr(main, "_scheduler", _FakeSched())
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": False, "expired": False})

    resp = main.health()
    body = _health_body(resp)
    assert resp.status_code == 503
    assert body["status"] == "degraded"
    assert any("cookies" in p for p in body["problems"])


def test_health_not_degraded_before_first_poll_grace_period(monkeypatch):
    # A channel is configured but no poll_runs row exists yet, and the process
    # "just started" — must not report degraded before the first poll can run.
    monkeypatch.setattr(db, "get_channels", lambda: [{"url": "https://x", "channel_id": None}])
    monkeypatch.setattr(db, "get_recent_poll_runs", lambda limit=1: [])

    class _FakeSched:
        running = True
    monkeypatch.setattr(main, "_scheduler", _FakeSched())
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": True, "expired": False})
    monkeypatch.setattr(main, "_STARTED_MONOTONIC", time.monotonic())

    resp = main.health()
    body = _health_body(resp)
    assert resp.status_code == 200
    assert body["checks"]["polling"] == "starting up"


def test_health_degraded_when_no_poll_ever_completed_past_grace(monkeypatch):
    monkeypatch.setattr(db, "get_channels", lambda: [{"url": "https://x", "channel_id": None}])
    monkeypatch.setattr(db, "get_recent_poll_runs", lambda limit=1: [])
    monkeypatch.setattr(main, "POLL_INTERVAL_HOURS", 1)

    class _FakeSched:
        running = True
    monkeypatch.setattr(main, "_scheduler", _FakeSched())
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": True, "expired": False})
    # Well past the 3x POLL_INTERVAL_HOURS grace period.
    monkeypatch.setattr(main, "_STARTED_MONOTONIC", time.monotonic() - 999999)

    resp = main.health()
    body = _health_body(resp)
    assert resp.status_code == 503
    assert "no poll has completed since startup" in body["problems"]


def test_served_version_matches_package():
    # Guards against the package __version__ drifting from what /health serves
    # (VERSION may have a "+<gitsha>" suffix appended locally).
    assert main.VERSION.split("+", 1)[0] == __version__


def test_app_title_is_slipcast():
    assert main.app.title == "Slipcast"


def test_ui_shows_slipcast_branding():
    # The dashboard is a static shell hydrated client-side from /api/state.
    html = main._PAGE
    assert "<title>Slipcast</title>" in html
    assert 'class="brand-name">Slipcast<' in html


def test_api_state_shape():
    db.init_db()
    resp = main.api_state()
    import json
    data = json.loads(resp.body)
    for key in ("channels", "unsubscribed", "orphans", "cookies", "email", "next_poll", "jobs", "version"):
        assert key in data


def test_api_channel_episodes_rejects_bad_id():
    with pytest.raises(HTTPException) as exc:
        main.api_channel_episodes("../etc/passwd")
    assert exc.value.status_code == 400


def test_api_channel_episodes_shape(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ep.db"))
    db.init_db()
    cid = "UCabc12345678901234567890"
    db.upsert_episode({
        "id": "vid1", "channel_id": cid, "channel_name": "C", "title": "Hello",
        "description": "", "published": "2026-06-20T00:00:00+00:00", "duration": 185,
        "filename": "vid1.mp3", "filesize": 5_000_000, "thumbnail": None,
    })
    data = json.loads(main.api_channel_episodes(cid).body)
    assert data["channel_id"] == cid
    assert len(data["episodes"]) == 1
    ep = data["episodes"][0]
    for key in ("title", "published", "duration", "filesize", "audio_url"):
        assert key in ep
    assert ep["audio_url"].endswith(f"/audio/{cid}/vid1.mp3")


# --- header has no duplicate "Poll all" button ------------------------------

def test_header_has_no_poll_all_button():
    # The header "Poll all" button was removed as a duplicate of the polling
    # dashboard's "Poll all now"; guard against it creeping back.
    assert 'id="poll-all"' not in main._PAGE


def test_dashboard_poll_now_button_kept():
    # The dashboard's own poll button must remain (it took over the function).
    assert 'id="poll-now"' in main._PAGE


# --- in-browser asset URLs are same-origin (CSP img-src 'self') -------------
# Channel/episode thumbnails and the modal's audio are loaded into <img>/<audio>
# in the browser. They must be relative so they work under any host (e.g. via
# localhost while BASE_URL points at the public domain) and satisfy the
# default-src/img-src 'self' CSP. Only the feed URL (copied into podcast apps)
# stays absolute.

def test_thumb_url_is_relative_when_present(tmp_path, monkeypatch):
    cid = "UCabc12345678901234567890"
    tdir = tmp_path / cid
    tdir.mkdir(parents=True)
    (tdir / "channel.jpg").write_bytes(b"jpegbytes")
    monkeypatch.setattr(main, "THUMBNAIL_DIR", str(tmp_path))
    url = main._thumb_url(cid)
    assert url == f"/thumbnails/{cid}/channel.jpg"
    assert url.startswith("/")                  # same-origin, relative
    assert not url.startswith("//")             # not protocol-relative either


def test_thumb_url_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "THUMBNAIL_DIR", str(tmp_path))
    assert main._thumb_url("UCabc12345678901234567890") is None


def test_episode_assets_are_relative(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ep.db"))
    db.init_db()
    cid = "UCabc12345678901234567890"
    db.upsert_episode({
        "id": "vid1", "channel_id": cid, "channel_name": "C", "title": "Hello",
        "description": "", "published": "2026-06-20T00:00:00+00:00", "duration": 185,
        "filename": "vid1.mp3", "filesize": 5_000_000, "thumbnail": "vid1.jpg",
    })
    ep = json.loads(main.api_channel_episodes(cid).body)["episodes"][0]
    assert ep["audio_url"] == f"/audio/{cid}/vid1.mp3"
    assert ep["thumbnail"] == f"/thumbnails/{cid}/vid1.jpg"
    # same-origin relative, not absolute and not protocol-relative
    for u in (ep["audio_url"], ep["thumbnail"]):
        assert u.startswith("/") and not u.startswith("//")


def test_episode_unsafe_filenames_yield_null_urls(tmp_path, monkeypatch):
    # Defense in depth: a traversal-shaped filename/thumbnail must never be
    # interpolated into a path — the endpoint emits null instead.
    import json
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ep.db"))
    db.init_db()
    cid = "UCabc12345678901234567890"
    db.upsert_episode({
        "id": "vid1", "channel_id": cid, "channel_name": "C", "title": "Hello",
        "description": "", "published": "2026-06-20T00:00:00+00:00", "duration": 1,
        "filename": "../../../etc/passwd", "filesize": 1, "thumbnail": "../secret.jpg",
    })
    ep = json.loads(main.api_channel_episodes(cid).body)["episodes"][0]
    assert ep["audio_url"] is None
    assert ep["thumbnail"] is None


def test_feed_url_stays_absolute():
    # Copied/shared into podcast apps — must keep the full BASE_URL.
    url = main._feed_url("UCabc12345678901234567890")
    assert url.startswith(main.BASE_URL)
    assert url.endswith(".xml")


def test_csp_img_src_is_self_only():
    # The relative-URL choice above depends on the CSP staying same-origin for
    # images (no remote hosts allowed). If this loosens, revisit the asset URLs.
    csp = main.index().headers["Content-Security-Policy"]
    assert "img-src 'self' data:" in csp
    assert "default-src 'self'" in csp


# --- CSRF -------------------------------------------------------------------

def test_state_changing_detection():
    assert main._is_state_changing(_req("POST", "/channels/add"))
    assert main._is_state_changing(_req("GET", "/add"))       # mutating shareable link
    assert main._is_state_changing(_req("GET", "/download"))
    assert not main._is_state_changing(_req("GET", "/feed/UC123.xml"))
    assert not main._is_state_changing(_req("GET", "/"))


def test_csrf_post_fails_closed_without_origin():
    # A POST with no Origin/Referer is rejected (CSRF fail-closed).
    assert not main._csrf_ok(_req("POST", "/channels/add", {"Host": "slipcast.example"}))


def test_csrf_post_allows_matching_origin():
    assert main._csrf_ok(_req("POST", "/channels/add", {
        "Host": "slipcast.example", "Origin": "https://slipcast.example",
    }))


def test_csrf_post_blocks_cross_origin():
    assert not main._csrf_ok(_req("POST", "/channels/add", {
        "Host": "slipcast.example", "Origin": "https://evil.example",
    }))


def test_csrf_get_link_allows_no_referer_but_blocks_cross_site():
    # Top-level navigation / bookmark (no Referer) is allowed for GET links...
    assert main._csrf_ok(_req("GET", "/add", {"Host": "slipcast.example"}))
    # ...but an embedded cross-site request carrying a foreign Referer is blocked.
    assert not main._csrf_ok(_req("GET", "/add", {
        "Host": "slipcast.example", "Referer": "https://evil.example/page",
    }))


# --- Client IP / rate-limit spoofing ----------------------------------------

def test_trusted_proxy_classification():
    assert main._is_trusted_proxy("127.0.0.1")
    assert main._is_trusted_proxy("::1")
    assert main._is_trusted_proxy("172.21.0.1")  # docker bridge
    assert not main._is_trusted_proxy("8.8.8.8")
    assert not main._is_trusted_proxy("not-an-ip")


def test_client_ip_trusts_forwarded_only_from_private_peer():
    # Behind the tunnel: private peer, real client in CF-Connecting-IP.
    r = _req(client=("172.21.0.1", 5), headers={"CF-Connecting-IP": "203.0.113.9"})
    assert main._client_ip(r) == "203.0.113.9"
    # Direct public peer: forwarded header is NOT trusted (anti-spoofing).
    r = _req(client=("8.8.8.8", 5), headers={"X-Forwarded-For": "10.0.0.1"})
    assert main._client_ip(r) == "8.8.8.8"


# --- SSRF / thumbnail URL allowlist -----------------------------------------

@pytest.mark.parametrize("url", [
    "https://i.ytimg.com/vi/abc/hqdefault.jpg",
    "https://yt3.ggpht.com/abc=s900",
    "https://lh3.googleusercontent.com/abc",
])
def test_thumbnail_allows_youtube_hosts(url):
    assert downloader._allowed_thumbnail_url(url)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata SSRF
    "file:///etc/passwd",                         # local file read
    "https://evil.example/x.jpg",                 # arbitrary host
    "https://evilytimg.com/x.jpg",                # suffix-confusion
    "https://i.ytimg.com.evil.example/x.jpg",     # subdomain-confusion
])
def test_thumbnail_blocks_disallowed_urls(url):
    assert not downloader._allowed_thumbnail_url(url)


# --- video_id path-traversal guard ------------------------------------------

@pytest.mark.parametrize("vid,ok", [
    ("dQw4w9WgXcQ", True),
    ("abc-_123456", True),
    ("../../etc/passwd", False),
    ("abc/def", False),
    ("a b", False),
])
def test_video_id_validation(vid, ok):
    assert bool(downloader._VIDEO_ID_RE.match(vid)) is ok


# --- orphaned channel data / removal endpoints -------------------------------

CID = "UCabc12345678901234567890"


def _ep(i, cid=CID):
    return {
        "id": f"v{i:03d}", "channel_id": cid, "channel_name": "C",
        "title": f"t{i}", "description": "",
        "published": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
        "duration": 1, "filename": f"v{i:03d}.mp3", "filesize": 1, "thumbnail": None,
    }


def _setup_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(main, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    monkeypatch.setattr(downloader, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(downloader, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    db.init_db()


def test_resolve_channel_id_exact_url_match(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "A")
    assert main._resolve_channel_id_for_removal(url) == CID


def test_resolve_channel_id_falls_back_to_normalized_match(tmp_path, monkeypatch):
    """Regression: a trailing-slash/tracking-query URL variant must still
    resolve to the same channel_id as the one stored at add-time."""
    _setup_tmp_db(tmp_path, monkeypatch)
    stored_url = "https://www.youtube.com/@A"
    db.add_channel(stored_url)
    db.update_channel_meta(stored_url, CID, "A")
    variant = "https://www.youtube.com/@A?si=trackingjunk"
    assert main._resolve_channel_id_for_removal(variant) == CID


def test_resolve_channel_id_returns_none_when_unresolvable(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    assert main._resolve_channel_id_for_removal("https://www.youtube.com/@Ghost") is None


def test_remove_one_cleans_up_episodes_and_files(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    url = "https://www.youtube.com/@A"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "A")
    db.upsert_episode(_ep(0))
    audio_dir = downloader._audio_dir_for(CID)
    open(os.path.join(audio_dir, "v000.mp3"), "wb").close()

    main._remove_one(url)

    assert db.get_channels() == []
    assert db.get_episodes(CID) == []
    assert not os.path.exists(audio_dir)


def test_remove_unsubscribed_endpoint(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    db.upsert_unsubscribed_channel(CID, "A")
    db.upsert_episode(_ep(0))
    audio_dir = downloader._audio_dir_for(CID)
    open(os.path.join(audio_dir, "v000.mp3"), "wb").close()

    resp = main.remove_unsubscribed_channel_endpoint(channel_id=CID)
    assert resp.status_code == 200
    assert db.get_unsubscribed_channels() == []
    assert db.get_episodes(CID) == []
    assert not os.path.exists(audio_dir)


def test_remove_unsubscribed_endpoint_rejects_bad_id():
    with pytest.raises(HTTPException) as exc:
        main.remove_unsubscribed_channel_endpoint(channel_id="../etc/passwd")
    assert exc.value.status_code == 400


def test_remove_orphan_endpoint(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0))  # orphan: no channels/unsubscribed row at all
    audio_dir = downloader._audio_dir_for(CID)
    open(os.path.join(audio_dir, "v000.mp3"), "wb").close()
    assert downloader.find_orphan_channels() != []

    resp = main.remove_orphan_channel(channel_id=CID)
    assert resp.status_code == 200
    assert db.get_episodes(CID) == []
    assert not os.path.exists(audio_dir)
    assert downloader.find_orphan_channels() == []


def test_remove_orphan_endpoint_rejects_bad_id():
    with pytest.raises(HTTPException) as exc:
        main.remove_orphan_channel(channel_id="../etc/passwd")
    assert exc.value.status_code == 400


def test_api_state_includes_orphans(tmp_path, monkeypatch):
    import json
    _setup_tmp_db(tmp_path, monkeypatch)
    db.upsert_episode(_ep(0))
    data = json.loads(main.api_state().body)
    assert any(o["channel_id"] == CID for o in data["orphans"])


# --- poll-all/poll-bulk use a bounded executor, not a thread per channel ----

def test_poll_all_now_submits_to_executor_not_raw_threads(tmp_path, monkeypatch):
    _setup_tmp_db(tmp_path, monkeypatch)
    db.add_channel("https://www.youtube.com/@A")
    db.add_channel("https://www.youtube.com/@B")

    submitted = []
    monkeypatch.setattr(main._poll_executor, "submit", lambda fn, *a: submitted.append((fn, a)))

    resp = main.poll_all_now()
    assert resp.status_code == 200
    assert len(submitted) == 2
    assert all(fn is main._run_poll for fn, _ in submitted)


# --- /health/live: the restart-fixable subset --------------------------------

def _live_ok_scheduler(monkeypatch):
    class _FakeSched:
        running = True
    monkeypatch.setattr(main, "_scheduler", _FakeSched())


def test_health_live_ignores_expired_cookies(monkeypatch):
    """The single most important property in the autoheal design.

    Expired cookies make /health degraded, but no restart fixes them — so
    /health/live must stay 200 or the host restarter loops the container
    every five minutes until its budget is spent.
    """
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [])
    _live_ok_scheduler(monkeypatch)
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": False, "expired": True})

    live = main.health_live()
    full = main.health()
    assert live.status_code == 200
    assert _health_body(live)["status"] == "ok"
    assert "cookies" not in _health_body(live)["checks"]
    assert full.status_code == 503  # the full report still says degraded


def test_health_live_degraded_when_scheduler_down(monkeypatch):
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [])
    monkeypatch.setattr(main, "_scheduler", None)

    resp = main.health_live()
    assert resp.status_code == 503
    assert "scheduler is not running" in _health_body(resp)["problems"]


def test_health_live_degraded_when_polling_stalled(monkeypatch):
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [{"url": "https://x", "channel_id": None}])
    monkeypatch.setattr(main, "POLL_INTERVAL_HOURS", 1)
    _live_ok_scheduler(monkeypatch)
    monkeypatch.setattr(main, "_STARTED_MONOTONIC", time.monotonic() - 999999)
    old = "2020-01-01T00:00:00+00:00"
    monkeypatch.setattr(db, "get_recent_poll_runs",
                        lambda limit=1: [{"finished_at": old, "started_at": old}])

    resp = main.health_live()
    assert resp.status_code == 503
    assert "polling appears stalled" in _health_body(resp)["problems"]


def test_health_live_ok_when_polling_recent(monkeypatch):
    from datetime import datetime, timezone
    db.init_db()
    monkeypatch.setattr(db, "get_channels", lambda: [{"url": "https://x", "channel_id": None}])
    _live_ok_scheduler(monkeypatch)
    monkeypatch.setattr(main, "cookies_status", lambda: {"present": False, "expired": True})
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(db, "get_recent_poll_runs",
                        lambda limit=1: [{"finished_at": now, "started_at": now}])

    resp = main.health_live()
    assert resp.status_code == 200
    assert _health_body(resp)["checks"]["polling"] == "ok"


def test_health_live_is_public():
    """The Docker HEALTHCHECK and ops/autoheal.sh curl it without credentials."""
    assert "/health/live".startswith(main._PUBLIC_PREFIXES)
