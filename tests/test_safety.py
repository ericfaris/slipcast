"""Tests for app.safety.is_safe_media_name — the path-segment guard used when
building filesystem paths / URLs from stored media filenames."""
import pytest

from app.safety import is_safe_media_name


@pytest.mark.parametrize("name", [
    "dQw4w9WgXcQ.mp3",          # our generated audio name
    "abc-_123.jpg",            # our generated thumbnail name
    "channel.jpg",
    "a",                        # single char
    "A1.b2.c3",                # multiple dots are fine
    "_KIXX9XxpoE.mp3",         # video IDs can start with underscore
    "-abc123.jpg",             # ...or with a dash
    "dQw4w9WgXcQ.opus",         # AUDIO_CODEC=opus output — no extension is hardcoded
])
def test_accepts_safe_names(name):
    assert is_safe_media_name(name) is True


@pytest.mark.parametrize("name", [
    None, "",                              # empty / missing
    "../etc/passwd",                       # traversal
    "..",                                  # parent ref
    "a/b.mp3", "a\\b.mp3",                # path separators
    "/etc/passwd",                         # absolute
    "foo/../bar",                          # embedded traversal
    ".hidden",                             # leading dot
    "a..b",                                # double-dot anywhere
    "x" * 129 + ".mp3",                  # over length limit
    "spa ce.mp3",                          # space (not in charset)
    "qu?ery.mp3",                          # query-ish chars
    "x%2e%2e.mp3",                         # percent (not in charset)
])
def test_rejects_unsafe_names(name):
    assert is_safe_media_name(name) is False
