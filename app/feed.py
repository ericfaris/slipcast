import logging
import os
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator

from app import database as db
from app.config import (
    ALL_FEED_MAX_EPISODES, BASE_URL, MAX_EPISODES_PER_CHANNEL, THUMBNAIL_DIR,
)
from app.safety import is_safe_media_name

logger = logging.getLogger(__name__)

# Apple's top-level podcast categories. Deliberately no subcategories — the
# dropdown stays a reasonable length, and a top-level category is enough for
# every directory that reads the tag. This lives here (the lower-level module)
# and is imported by app/main.py for request validation, so what we accept and
# what we render can never drift apart.
#
# feedgen validates none of this: itunes_category() accepts any string and
# emits it verbatim (it only checks the dict keys it builds internally), so our
# allow-list is the ONLY category validation there is.
ITUNES_CATEGORIES = (
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government",
    "Health & Fitness", "History", "Kids & Family", "Leisure", "Music",
    "News", "Religion & Spirituality", "Science", "Society & Culture",
    "Sports", "Technology", "True Crime", "TV & Film",
)

# What every feed emitted before per-channel overrides existed. A channel with
# no stored value keeps producing byte-identical output.
_DEFAULT_CATEGORY = "Technology"
_DEFAULT_LANGUAGE = "en"
_DEFAULT_EXPLICIT = "no"
# feedgen's itunes_explicit() raises ValueError on anything outside this set,
# which on a public endpoint would be a 500 rather than a bad-looking feed.
_EXPLICIT_VALUES = frozenset({"yes", "no", "clean"})

# The enclosure type must describe the file we actually stored, not whatever
# AUDIO_CODEC currently says — a feed can hold .mp3 episodes downloaded before
# the setting changed alongside newer .opus ones. yt-dlp's opus output is an Ogg
# container (RFC 7845), which is audio/ogg; audio/opus is an RTP payload type,
# not a file type.
_ENCLOSURE_TYPES = {".mp3": "audio/mpeg", ".opus": "audio/ogg", ".ogg": "audio/ogg",
                    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
                    ".wav": "audio/wav"}


def _enclosure_type(filename: str) -> str:
    return _ENCLOSURE_TYPES.get(os.path.splitext(filename)[1].lower(), "audio/mpeg")


def _channel_metadata(channel_id: str) -> tuple[str, str, str]:
    """(category, language, explicit) for a channel, falling back to defaults.

    The fallbacks are re-checked against the allow-lists rather than trusted:
    the values are validated when written, but a hand-edited database (or a
    future trim of ITUNES_CATEGORIES) must not be able to 500 a live public
    feed — feedgen's itunes_explicit() in particular raises on a bad value.
    """
    meta = db.get_channel_feed_settings(channel_id)
    category = (meta["itunes_category"] if meta else None) or _DEFAULT_CATEGORY
    if category not in ITUNES_CATEGORIES:
        logger.warning("Ignoring unknown stored itunes_category %r for %s",
                       category, channel_id)
        category = _DEFAULT_CATEGORY
    language = (meta["itunes_language"] if meta else None) or _DEFAULT_LANGUAGE
    explicit = (meta["itunes_explicit"] if meta else None) or _DEFAULT_EXPLICIT
    if explicit not in _EXPLICIT_VALUES:
        logger.warning("Ignoring invalid stored itunes_explicit %r for %s",
                       explicit, channel_id)
        explicit = _DEFAULT_EXPLICIT
    return category, language, explicit


def _add_entry(fg: FeedGenerator, ep, channel_id: str, order: str = "prepend"):
    """Add one episode to a feed. Returns the entry, or None if it was skipped.

    Shared by the per-channel and combined feeds so the safety checks and the
    enclosure/duration/image handling can't drift between the two.

    `order` is feedgen's insertion order. The per-channel feed keeps feedgen's
    default (prepend), which — fed a newest-first episode list — emits items
    oldest-first; that has always been this feed's output and every podcast app
    sorts by pubDate anyway, so it stays as-is. The combined feed passes
    "append" instead, so its document order really is newest-first: reading it
    top-down as one merged stream is the entire point of that feed.
    """
    # An item needs a valid enclosure; skip rather than emit a path built
    # from an unsafe filename (defense in depth — see app/safety.py).
    if not is_safe_media_name(ep["filename"]):
        logger.warning("Skipping feed item with unsafe filename for %s: %r",
                       channel_id, ep["filename"])
        return None
    fe = fg.add_entry(order=order)
    fe.id(ep["id"])
    fe.title(ep["title"])
    fe.description(ep["description"] or ep["title"])

    try:
        pub = datetime.fromisoformat(ep["published"])
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        pub = datetime.now(timezone.utc)
    fe.published(pub)

    audio_url = f"{BASE_URL}/audio/{channel_id}/{ep['filename']}"
    fe.enclosure(audio_url, str(ep["filesize"] or 0), _enclosure_type(ep["filename"]))

    if ep["duration"]:
        fe.podcast.itunes_duration(ep["duration"])

    if is_safe_media_name(ep["thumbnail"]):
        fe.podcast.itunes_image(f"{BASE_URL}/thumbnails/{channel_id}/{ep['thumbnail']}")
    return fe


def build_feed(channel_id: str) -> bytes:
    # Defense in depth: cap the feed itself rather than trusting that pruning
    # has kept the DB at/under the limit. get_episodes returns newest-first.
    episodes = db.get_episodes(channel_id)[:MAX_EPISODES_PER_CHANNEL]
    if not episodes:
        return b""

    channel_name = episodes[0]["channel_name"]

    fg = FeedGenerator()
    fg.load_extension("podcast")

    feed_url = f"{BASE_URL}/feed/{channel_id}.xml"
    fg.id(feed_url)
    fg.title(channel_name)
    fg.link(href=f"https://www.youtube.com/channel/{channel_id}", rel="alternate")
    fg.link(href=feed_url, rel="self")
    category, language, explicit = _channel_metadata(channel_id)
    fg.language(language)
    fg.description(f"Audio podcast feed for {channel_name}")
    fg.podcast.itunes_author(channel_name)
    fg.podcast.itunes_explicit(explicit)
    fg.podcast.itunes_category(category)
    channel_jpg = os.path.join(THUMBNAIL_DIR, channel_id, "channel.jpg")
    if os.path.exists(channel_jpg):
        channel_image_url = f"{BASE_URL}/thumbnails/{channel_id}/channel.jpg"
    else:
        # fall back to first episode thumbnail that exists
        channel_image_url = next(
            (f"{BASE_URL}/thumbnails/{channel_id}/{ep['thumbnail']}"
             for ep in episodes if is_safe_media_name(ep["thumbnail"])),
            None,
        )
    # Fall back to the branded Slipcast cover so every feed has artwork.
    if not channel_image_url:
        channel_image_url = f"{BASE_URL}/static/cover-512.png"
    fg.podcast.itunes_image(channel_image_url)

    for ep in episodes:
        _add_entry(fg, ep, channel_id)

    return fg.rss_str(pretty=True)


def build_combined_feed() -> bytes:
    """One feed across every subscribed channel, newest first.

    Returns b"" when there is nothing to serve — the same contract build_feed()
    has, so the route's 404 handling is uniform for both.

    One-off (unsubscribed) channels are deliberately excluded: they're an
    explicit single-video download, not something the user subscribed to.
    """
    episodes = db.get_combined_episodes(ALL_FEED_MAX_EPISODES)
    if not episodes:
        return b""

    fg = FeedGenerator()
    fg.load_extension("podcast")

    feed_url = f"{BASE_URL}/feed/all.xml"
    fg.id(feed_url)
    fg.title("Slipcast — All Channels")
    fg.link(href=BASE_URL, rel="alternate")
    fg.link(href=feed_url, rel="self")
    fg.language(_DEFAULT_LANGUAGE)
    fg.description("Every subscribed channel in one feed, newest first.")
    fg.podcast.itunes_author("Slipcast")
    fg.podcast.itunes_explicit(_DEFAULT_EXPLICIT)
    fg.podcast.itunes_category(_DEFAULT_CATEGORY)
    # No single channel's artwork would be right here, so always the branded cover.
    fg.podcast.itunes_image(f"{BASE_URL}/static/cover-512.png")

    for ep in episodes:
        fe = _add_entry(fg, ep, ep["channel_id"], order="append")
        if fe is not None:
            # Per-entry author is the only thing that tells a listener which
            # channel an item came from once they're all merged together.
            fe.podcast.itunes_author(ep["channel_name"])

    return fg.rss_str(pretty=True)
