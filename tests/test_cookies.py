"""Tests for cookie-file validation (app.downloader.valid_cookie_file)."""
from app.downloader import (
    _looks_like_auth_error,
    cookie_file_expiry,
    valid_cookie_file,
)

NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n"
DATA_LINE = ".youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\tabc123\n"


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def _cookie(name, expiry, domain=".youtube.com"):
    return f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\tval\n"


def test_empty_file_is_invalid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", "")) is False


def test_whitespace_only_is_invalid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", "\n\n   \n")) is False


def test_header_only_is_valid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", NETSCAPE_HEADER)) is True


def test_alt_header_is_valid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", "# HTTP Cookie File\n")) is True


def test_data_line_without_header_is_valid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", DATA_LINE)) is True


def test_full_file_is_valid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", NETSCAPE_HEADER + DATA_LINE)) is True


def test_junk_text_is_invalid(tmp_path):
    assert valid_cookie_file(_write(tmp_path, "c.txt", "this is not a cookie file\n")) is False


def test_comment_only_is_invalid(tmp_path):
    # comments that aren't the Netscape header, no data lines
    assert valid_cookie_file(_write(tmp_path, "c.txt", "# some comment\n# another\n")) is False


def test_missing_file_is_invalid(tmp_path):
    assert valid_cookie_file(str(tmp_path / "nope.txt")) is False


def test_empty_path_is_invalid():
    assert valid_cookie_file("") is False


def test_data_line_too_few_fields_is_invalid(tmp_path):
    # only 4 tab-separated fields -> not a real cookie record
    assert valid_cookie_file(_write(tmp_path, "c.txt", "a\tb\tc\td\n")) is False


def test_expiry_returns_earliest_youtube_cookie(tmp_path):
    content = (NETSCAPE_HEADER
               + _cookie("VISITOR_INFO1_LIVE", 2000)
               + _cookie("__Secure-YNID", 1500)
               + _cookie("__Secure-ROLLOUT_TOKEN", 3000))
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 1500


def test_expiry_works_for_anonymous_visitor_cookies(tmp_path):
    # The common case: a not-logged-in export with only visitor cookies.
    content = (NETSCAPE_HEADER
               + _cookie("GPS", 100)                 # daily-rotating, excluded
               + _cookie("PREF", 0)                  # session, excluded
               + _cookie("VISITOR_INFO1_LIVE", 9999))
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 9999


def test_expiry_prefers_login_cookies_when_present(tmp_path):
    content = (NETSCAPE_HEADER
               + _cookie("VISITOR_INFO1_LIVE", 9999)
               + _cookie("SID", 5000)
               + _cookie("LOGIN_INFO", 4000))
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 4000


def test_expiry_ignores_volatile_cookies(tmp_path):
    # GPS / *PSIDTS rotate within ~a day and must not lower the deadline.
    content = (NETSCAPE_HEADER
               + _cookie("GPS", 100)
               + _cookie("__Secure-1PSIDTS", 200)
               + _cookie("SID", 9999))
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 9999


def test_expiry_ignores_unrelated_domains(tmp_path):
    # A full-browser export carries cookies for other sites; ignore them.
    content = (NETSCAPE_HEADER
               + _cookie("ts", 100, domain=".venmo.com")
               + _cookie("VISITOR_INFO1_LIVE", 8888))
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 8888


def test_expiry_ignores_session_cookies(tmp_path):
    content = NETSCAPE_HEADER + _cookie("YSC", 0) + _cookie("VISITOR_INFO1_LIVE", 4242)
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 4242


def test_expiry_none_when_no_youtube_cookies(tmp_path):
    content = NETSCAPE_HEADER + _cookie("ts", 9999, domain=".venmo.com")
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) is None


def test_expiry_skips_malformed_lines(tmp_path):
    content = (NETSCAPE_HEADER + "a\tb\tc\n"
               + _cookie("VISITOR_INFO1_LIVE", "notanint")
               + _cookie("__Secure-YNID", 7777))
    assert cookie_file_expiry(_write(tmp_path, "c.txt", content)) == 7777


def test_auth_error_detection():
    assert _looks_like_auth_error("ERROR: Sign in to confirm you're not a bot")
    assert _looks_like_auth_error("Please use --cookies for this video")
    assert _looks_like_auth_error("")  is False
    assert _looks_like_auth_error("HTTP Error 404: Not Found") is False


def test_auth_error_detection_ignores_incidental_cookies_mentions():
    # Regression: the signal used to be the bare substring "cookies", so any
    # error merely mentioning the word (not actually an auth failure) wrongly
    # triggered a cookie-expiry email.
    assert not _looks_like_auth_error("Unable to parse cookies from browser profile")
    assert not _looks_like_auth_error("This video mentions cookies in its description")
    assert _looks_like_auth_error("The provided YouTube account cookies are no longer valid")
