"""Tests for RSS feed generation, including the per-feed episode cap."""
from xml.etree import ElementTree as ET

from app import database as db, feed

CID = "UCabc12345678901234567890"


def _setup_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(feed, "THUMBNAIL_DIR", str(tmp_path / "thumb"))
    monkeypatch.setattr(feed, "BASE_URL", "https://example.test")
    db.init_db()


def _ep(i, cid=CID):
    return {
        "id": f"v{i:03d}", "channel_id": cid, "channel_name": "Chan",
        "title": f"Episode {i}", "description": "",
        "published": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
        "duration": 1, "filename": f"v{i:03d}.mp3", "filesize": 123,
        "thumbnail": None,
    }


def _items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return root.findall("./channel/item")


def test_empty_feed_returns_empty_bytes(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert feed.build_feed(CID) == b""


def test_feed_caps_at_max(tmp_path, monkeypatch):
    """Even if the DB has drifted over the cap, the feed must not expose more
    than MAX_EPISODES_PER_CHANNEL items."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    for i in range(34):
        db.upsert_episode(_ep(i))
    assert len(db.get_episodes(CID)) == 34
    assert len(_items(feed.build_feed(CID))) == 20


def test_feed_includes_all_episodes_under_cap(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    for i in range(5):
        db.upsert_episode(_ep(i))
    items = _items(feed.build_feed(CID))
    titles = {it.find("title").text for it in items}
    assert titles == {f"Episode {i}" for i in range(5)}


def test_feed_includes_enclosure_and_metadata(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    db.upsert_episode(_ep(1))
    xml = feed.build_feed(CID)
    item = _items(xml)[0]
    enc = item.find("enclosure")
    assert enc.get("type") == "audio/mpeg"
    assert enc.get("url") == "https://example.test/audio/%s/v001.mp3" % CID
    assert enc.get("length") == "123"


def test_feed_skips_item_with_unsafe_filename(tmp_path, monkeypatch):
    # Defense in depth: an entry whose filename can't be safely placed in a
    # path is dropped rather than emitted with a traversal enclosure URL.
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    good = _ep(1)
    bad = _ep(2)
    bad["filename"] = "../../etc/passwd"
    db.upsert_episode(good)
    db.upsert_episode(bad)
    items = _items(feed.build_feed(CID))
    titles = {it.find("title").text for it in items}
    assert titles == {"Episode 1"}  # the unsafe one is skipped
    for it in items:
        assert "../" not in it.find("enclosure").get("url")


def test_feed_omits_unsafe_episode_thumbnail(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    ep = _ep(1)
    ep["thumbnail"] = "../../evil.jpg"  # filename itself is still safe
    db.upsert_episode(ep)
    xml = feed.build_feed(CID)
    assert b"../" not in xml          # no traversal anywhere in the feed
    assert len(_items(xml)) == 1      # item still present (only thumb dropped)


def test_feed_survives_bad_published_date(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    ep = _ep(1)
    ep["published"] = "not-a-date"
    db.upsert_episode(ep)
    # Should not raise; falls back to "now".
    assert len(_items(feed.build_feed(CID))) == 1


def test_feed_enclosure_type_matches_extension(tmp_path, monkeypatch):
    """A channel can legitimately hold both old .mp3 and new .opus episodes
    (AUDIO_CODEC changed mid-flight) — each item's enclosure MIME must match
    its own stored file, not a single global setting."""
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    mp3_ep = _ep(1)
    opus_ep = _ep(2)
    opus_ep["filename"] = "v002.opus"
    db.upsert_episode(mp3_ep)
    db.upsert_episode(opus_ep)

    items = _items(feed.build_feed(CID))
    by_title = {it.find("title").text: it.find("enclosure") for it in items}
    assert by_title["Episode 1"].get("type") == "audio/mpeg"
    assert by_title["Episode 1"].get("url") == "https://example.test/audio/%s/v001.mp3" % CID
    assert by_title["Episode 2"].get("type") == "audio/ogg"
    assert by_title["Episode 2"].get("url") == "https://example.test/audio/%s/v002.opus" % CID


def test_feed_unknown_extension_defaults_to_audio_mpeg(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "MAX_EPISODES_PER_CHANNEL", 20)
    ep = _ep(1)
    ep["filename"] = "v001.weird"
    db.upsert_episode(ep)
    items = _items(feed.build_feed(CID))
    assert len(items) == 1  # still emitted — is_safe_media_name accepts it
    assert items[0].find("enclosure").get("type") == "audio/mpeg"


# --- combined feed ----------------------------------------------------------

CID2 = "UCdef12345678901234567890"
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _subscribe(cid, name):
    url = f"https://www.youtube.com/channel/{cid}"
    db.add_channel(url)
    db.update_channel_meta(url, cid, name)
    return url


def _titles(xml_bytes):
    return [it.find("title").text for it in _items(xml_bytes)]


def test_combined_feed_empty_returns_empty_bytes(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    assert feed.build_combined_feed() == b""


def test_combined_feed_merges_subscribed_channels_newest_first(tmp_path, monkeypatch):
    """AC3: every subscribed channel, newest first, one-offs excluded."""
    _setup_tmp(tmp_path, monkeypatch)
    _subscribe(CID, "Chan A")
    _subscribe(CID2, "Chan B")
    db.upsert_unsubscribed_channel("UConeoff123456789012345678", "One-off")

    for i in (1, 5):
        db.upsert_episode(_ep(i, CID))
    for i in (3, 9):
        db.upsert_episode(_ep(i, CID2))
    oneoff = _ep(20, "UConeoff123456789012345678")
    oneoff["title"] = "Should not appear"
    db.upsert_episode(oneoff)

    titles = _titles(feed.build_combined_feed())
    # published is 2026-06-<i+1>, so descending order is 9, 5, 3, 1.
    assert titles == ["Episode 9", "Episode 5", "Episode 3", "Episode 1"]
    assert "Should not appear" not in titles


def test_combined_feed_respects_cap(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(feed, "ALL_FEED_MAX_EPISODES", 5)
    _subscribe(CID, "Chan A")
    for i in range(12):
        db.upsert_episode(_ep(i))
    titles = _titles(feed.build_combined_feed())
    assert len(titles) == 5
    assert titles == [f"Episode {i}" for i in (11, 10, 9, 8, 7)]  # the 5 newest


def test_combined_feed_does_not_duplicate_shared_channel_id(tmp_path, monkeypatch):
    """A second URL variant for an already-subscribed channel collapses into the
    existing row (1.15: channel_id is the PK), so its episodes appear once."""
    _setup_tmp(tmp_path, monkeypatch)
    url = _subscribe(CID, "Chan A")
    db.add_channel("https://www.youtube.com/@ChanA")
    db.update_channel_meta("https://www.youtube.com/@ChanA", CID, "Chan A")
    assert [r["url"] for r in db.get_channels()] == [url]
    db.upsert_episode(_ep(1))
    assert _titles(feed.build_combined_feed()) == ["Episode 1"]


def test_combined_feed_labels_each_item_with_its_channel(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    _subscribe(CID, "Chan A")
    _subscribe(CID2, "Chan B")
    ep_a, ep_b = _ep(1, CID), _ep(2, CID2)
    ep_b["channel_name"] = "Chan B"
    db.upsert_episode(ep_a)
    db.upsert_episode(ep_b)
    authors = {it.find("title").text: it.find(f"{ITUNES_NS}author").text
               for it in _items(feed.build_combined_feed())}
    assert authors == {"Episode 1": "Chan", "Episode 2": "Chan B"}


def test_combined_feed_skips_unsafe_filenames(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    _subscribe(CID, "Chan A")
    bad = _ep(2)
    bad["filename"] = "../../etc/passwd"
    db.upsert_episode(_ep(1))
    db.upsert_episode(bad)
    xml = feed.build_combined_feed()
    assert _titles(xml) == ["Episode 1"]
    assert b"../" not in xml


# --- per-channel iTunes metadata --------------------------------------------

def _channel_meta(xml_bytes):
    root = ET.fromstring(xml_bytes)
    ch = root.find("./channel")
    return (ch.find(f"{ITUNES_NS}category").get("text"),
            ch.find("language").text,
            ch.find(f"{ITUNES_NS}explicit").text)


def test_feed_uses_defaults_when_unset(tmp_path, monkeypatch):
    """AC6: an unconfigured channel keeps producing today's output."""
    _setup_tmp(tmp_path, monkeypatch)
    _subscribe(CID, "Chan A")
    db.upsert_episode(_ep(1))
    assert _channel_meta(feed.build_feed(CID)) == ("Technology", "en", "no")


def test_feed_uses_channel_overrides(tmp_path, monkeypatch):
    _setup_tmp(tmp_path, monkeypatch)
    _subscribe(CID, "Chan A")
    db.upsert_episode(_ep(1))
    db.set_channel_feed_settings(CID, "Comedy", "es", "clean")
    assert _channel_meta(feed.build_feed(CID)) == ("Comedy", "es", "clean")


def test_feed_falls_back_on_invalid_stored_values(tmp_path, monkeypatch):
    """A hand-edited DB must not be able to 500 a live public feed — feedgen's
    itunes_explicit() raises on anything outside yes/no/clean."""
    _setup_tmp(tmp_path, monkeypatch)
    _subscribe(CID, "Chan A")
    db.upsert_episode(_ep(1))
    with db.get_conn() as conn:
        conn.execute("UPDATE channels SET itunes_category = ?, itunes_explicit = ? "
                     "WHERE channel_id = ?", ("Underwater Basketweaving", "sorta", CID))
    assert _channel_meta(feed.build_feed(CID)) == ("Technology", "en", "no")


def test_unsubscribed_channel_feed_uses_defaults(tmp_path, monkeypatch):
    """One-off feeds have no overrides by design — no channels row, no settings."""
    _setup_tmp(tmp_path, monkeypatch)
    db.upsert_unsubscribed_channel(CID, "One-off")
    db.upsert_episode(_ep(1))
    assert _channel_meta(feed.build_feed(CID)) == ("Technology", "en", "no")
