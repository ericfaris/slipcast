"""Feed, media serving, health and dashboard state in both storage modes.

Serving is driven through the full ASGI stack of ``main.app`` (middleware,
router, mount) by hand — no TestClient, which needs httpx and CI installs only
requirements.txt + pytest (see tests/test_endpoints.py).
"""
import asyncio
import json
import xml.etree.ElementTree as ET

import pytest

from app import database as db, downloader, feed, main

CID = "UCfeed12345678901234567890"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _asgi(app, method, path, headers=()):
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 1), "server": ("t", 80), "scheme": "http",
        "http_version": "1.1",
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], hdrs, body


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    for mod in (main, downloader):
        monkeypatch.setattr(mod, "AUDIO_DIR", str(tmp_path / "audio"))
        monkeypatch.setattr(mod, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    monkeypatch.setattr(feed, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    monkeypatch.setattr(feed, "BASE_URL", "https://example.test")
    db.init_db()
    return tmp_path


def _episode(i, filesize=123, thumb=None):
    return {
        "id": f"v{i:03d}", "channel_id": CID, "channel_name": "Feed Chan",
        "title": f"t{i}", "description": "",
        "published": f"2026-06-{i + 1:02d}T00:00:00+00:00", "duration": 60,
        "filename": f"v{i:03d}.mp3", "filesize": filesize, "thumbnail": thumb,
    }


def _items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return root, {i.findtext("guid"): i.find("enclosure").attrib for i in root.iter("item")}


# --- feed --------------------------------------------------------------------------

def test_feed_r2_length_from_object_size_and_unchanged_url(dirs, fake_r2):
    drv, fake = fake_r2
    db.upsert_episode(_episode(0))
    db.upsert_episode(_episode(1))
    fake.seed(f"audio/{CID}/v000.mp3", b"x" * 4321)   # v001 absent from the index
    drv.ensure_index()

    _, items = _items(feed.build_feed(CID))
    assert items["v000"]["url"] == f"https://example.test/audio/{CID}/v000.mp3"
    assert items["v000"]["length"] == "4321"
    assert items["v001"]["length"] == "123"           # DB fallback


def test_feed_r2_channel_art(dirs, fake_r2):
    drv, fake = fake_r2
    db.upsert_episode(_episode(0, thumb="v000.jpg"))
    root, _ = _items(feed.build_feed(CID))
    # not seeded -> first episode thumb, as today
    assert root.find(f"channel/{ITUNES}image").get("href").endswith(f"/thumbnails/{CID}/v000.jpg")
    fake.seed(f"thumbnails/{CID}/channel.jpg", b"art")
    drv.refresh()
    root, _ = _items(feed.build_feed(CID))
    assert root.find(f"channel/{ITUNES}image").get("href") == \
        f"https://example.test/thumbnails/{CID}/channel.jpg"


def test_feed_r2_cover_fallback(dirs, fake_r2):
    db.upsert_episode(_episode(0))
    root, _ = _items(feed.build_feed(CID))
    assert root.find(f"channel/{ITUNES}image").get("href").endswith("/static/cover-512.png")


def test_feed_renders_when_index_failing(dirs, fake_r2):
    drv, fake = fake_r2
    fake.fail_next("list_objects_v2", times=None)
    db.upsert_episode(_episode(0))
    _, items = _items(feed.build_feed(CID))
    assert items["v000"]["length"] == "123"


def test_feed_urls_identical_local_vs_r2(dirs, fake_r2, monkeypatch):
    from app import storage
    drv, fake = fake_r2
    for i in range(3):
        db.upsert_episode(_episode(i, filesize=10 + i))
        fake.seed(f"audio/{CID}/v{i:03d}.mp3", b"x" * (10 + i))
    drv.ensure_index()
    r2_xml = feed.build_feed(CID)
    storage._set_driver(storage.LocalDriver())
    local_xml = feed.build_feed(CID)
    assert _items(r2_xml)[1] == _items(local_xml)[1]


# --- serving: local ------------------------------------------------------------------

def test_local_serving_unchanged(dirs):
    adir = dirs / "audio" / CID
    adir.mkdir(parents=True)
    (adir / "v.mp3").write_bytes(b"0123456789")
    tdir = dirs / "thumb" / CID
    tdir.mkdir(parents=True)
    (tdir / "channel.jpg").write_bytes(b"jpeg")

    status, hdrs, body = _asgi(main.app, "GET", f"/audio/{CID}/v.mp3")
    assert status == 200 and body == b"0123456789"
    assert hdrs["content-type"] == "audio/mpeg"
    status, hdrs, body = _asgi(main.app, "GET", f"/audio/{CID}/v.mp3", [("Range", "bytes=0-3")])
    assert status == 206 and body == b"0123"
    status, _, body = _asgi(main.app, "HEAD", f"/audio/{CID}/v.mp3")
    assert status == 200 and body == b""
    assert _asgi(main.app, "GET", f"/audio/{CID}/missing.mp3")[0] == 404
    status, _, body = _asgi(main.app, "GET", f"/thumbnails/{CID}/channel.jpg")
    assert status == 200 and body == b"jpeg"


# --- serving: r2 ---------------------------------------------------------------------

@pytest.mark.parametrize("kind,name", [("audio", "v.mp3"), ("thumbnails", "channel.jpg")])
def test_r2_serving_redirects_to_presigned(dirs, fake_r2, kind, name):
    drv, fake = fake_r2
    fake.seed(f"{kind}/{CID}/{name}", b"bytes")

    status, hdrs, _ = _asgi(main.app, "GET", f"/{kind}/{CID}/{name}")
    assert status == 302
    assert hdrs["location"] == (f"https://fake-r2.test/test-bucket/{kind}/{CID}/{name}"
                                f"?X-Amz-Expires=3600&X-Amz-Signature=fake&method=get_object")
    assert "no-store" in hdrs["cache-control"]
    assert fake.calls_of("head_object") == []

    status, hdrs, _ = _asgi(main.app, "HEAD", f"/{kind}/{CID}/{name}")
    assert status == 302 and "method=head_object" in hdrs["location"]

    assert _asgi(main.app, "GET", f"/{kind}/{CID}/missing.mp3")[0] == 404
    assert _asgi(main.app, "POST", f"/{kind}/{CID}/{name}")[0] == 405
    assert not (dirs / "audio").exists()


@pytest.mark.parametrize("path", [
    f"/audio/{CID}/../x.mp3",
    "/audio/bad id/x.mp3",
    f"/audio/{CID}/.hidden.mp3",
    f"/audio/{CID}/sub/x.mp3",
    "/audio/x.mp3",
    "/audio/",
])
def test_r2_serving_rejects_unsafe_paths(dirs, fake_r2, path):
    drv, fake = fake_r2
    assert _asgi(main.app, "GET", path)[0] == 404
    assert fake.calls_of("generate_presigned_url") == []


@pytest.mark.parametrize("kind", ["audio", "thumbnails"])
def test_r2_serving_503_when_index_failing(dirs, fake_r2, kind):
    drv, fake = fake_r2
    fake.fail_next("list_objects_v2", times=None)
    status, hdrs, _ = _asgi(main.app, "GET", f"/{kind}/{CID}/v.mp3")
    assert status == 503 and hdrs["retry-after"] == "30"


def test_thumb_url_r2(dirs, fake_r2):
    drv, fake = fake_r2
    fake.seed(f"thumbnails/{CID}/channel.jpg", b"art")
    assert main._thumb_url(CID) == f"/thumbnails/{CID}/channel.jpg"
    assert main._thumb_url("OTHER") is None


def test_thumb_url_r2_index_failing_is_none(dirs, fake_r2):
    drv, fake = fake_r2
    fake.fail_next("list_objects_v2", times=None)
    assert main._thumb_url(CID) is None


# --- health / state ----------------------------------------------------------------

def _live_ok(monkeypatch):
    class _Sched:
        running = True
    monkeypatch.setattr(main, "_scheduler", _Sched())
    monkeypatch.setattr(db, "get_channels", lambda: [])


def test_health_r2_unavailable(dirs, fake_r2, monkeypatch):
    drv, fake = fake_r2
    _live_ok(monkeypatch)
    fake.fail_next("list_objects_v2", times=None)
    assert drv.ensure_index() is False

    resp = main.health()
    body = json.loads(resp.body)
    assert resp.status_code == 503
    assert "media storage (R2) is unreachable" in body["problems"]
    assert body["checks"]["storage"] == "r2 unavailable"
    assert body["storage"] == {"mode": "r2", "ok": False, "last_refresh_at": None}
    assert b"test-bucket" not in resp.body and b"acct-test" not in resp.body

    live = main.health_live()   # a restart can't fix a remote outage
    assert live.status_code == 200
    assert "storage" not in json.loads(live.body)["checks"]


def test_health_r2_ok(dirs, fake_r2, monkeypatch):
    drv, fake = fake_r2
    drv.ensure_index()
    resp = main.health()
    body = json.loads(resp.body)
    assert body["checks"]["storage"] == "r2 ok"
    assert body["storage"]["ok"] is True and body["storage"]["last_refresh_at"]


def test_health_r2_starting_up_is_not_a_problem(dirs, fake_r2):
    body = json.loads(main.health().body)
    assert body["checks"]["storage"] == "r2 starting up"
    assert not any("R2" in p for p in body["problems"])


def test_health_local(dirs):
    body = json.loads(main.health().body)
    assert body["checks"]["storage"] == "local"
    assert body["storage"]["mode"] == "local"


def test_api_state_storage_local(dirs):
    data = json.loads(main.api_state().body)
    assert data["storage"] == {"mode": "local", "ok": True}


def test_api_state_storage_r2(dirs, fake_r2):
    drv, fake = fake_r2
    url = "https://www.youtube.com/@Feed"
    db.add_channel(url)
    db.update_channel_meta(url, CID, "Feed Chan")
    fake.seed(f"audio/{CID}/v000.mp3", b"a" * 1000)
    fake.seed(f"thumbnails/{CID}/v000.jpg", b"t" * 500)

    data = json.loads(main.api_state().body)
    assert data["storage"]["mode"] == "r2"
    assert data["storage"]["bucket"] == "test-bucket"
    assert data["storage"]["object_count"] == 2
    assert data["total_bytes"] == 1500
    ch = next(c for c in data["channels"] if c["channel_id"] == CID)
    assert ch["bytes"] == 1500
    assert not (dirs / "audio").exists()
