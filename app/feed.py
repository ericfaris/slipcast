import logging
import os
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator

from app import database as db
from app.config import BASE_URL, MAX_EPISODES_PER_CHANNEL, THUMBNAIL_DIR
from app.safety import is_safe_media_name

logger = logging.getLogger(__name__)

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
    fg.language("en")
    fg.description(f"Audio podcast feed for {channel_name}")
    fg.podcast.itunes_author(channel_name)
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_category("Technology")
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
        # An item needs a valid enclosure; skip rather than emit a path built
        # from an unsafe filename (defense in depth — see app/safety.py).
        if not is_safe_media_name(ep["filename"]):
            logger.warning("Skipping feed item with unsafe filename for %s: %r",
                           channel_id, ep["filename"])
            continue
        fe = fg.add_entry()
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

    return fg.rss_str(pretty=True)
