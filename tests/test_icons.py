"""House app-icon standard (shared with redline/jambo).

favicon.ico (16/32/48), apple-touch-icon (180), rounded 192/512 and a
maskable 512, all rendered from scripts/icons/icon.svg by
scripts/icons/build.py and wired into the page head and web manifest.
"""
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "app/static"
PNGS = {"apple-touch-icon.png": 180, "icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512}


def test_pngs_at_declared_sizes():
    for name, size in PNGS.items():
        b = (PUBLIC / name).read_bytes()
        assert b[1:4] == b"PNG", name
        assert struct.unpack(">II", b[16:24]) == (size, size), name


def test_favicon_ico_has_16_32_48():
    ico = (PUBLIC / "favicon.ico").read_bytes()
    assert ico[:6] == bytes([0, 0, 1, 0, 3, 0])
    assert [ico[6], ico[22], ico[38]] == [16, 32, 48]


def test_manifest_lists_icon_set():
    m = json.loads((PUBLIC / "site.webmanifest").read_text())
    assert [i["src"] for i in m["icons"]] == ["/static/icon-192.png", "/static/icon-512.png", "/static/icon-maskable-512.png"]
    assert m["icons"][2]["purpose"] == "maskable"


def test_head_links_icon_set():
    head = (ROOT / "app/main.py").read_text()
    for tag in ('rel="icon" href="/static/favicon.ico" sizes="any"', 'rel="apple-touch-icon" href="/static/apple-touch-icon.png"',
                'rel="manifest" href="/static/site.webmanifest"'):
        assert tag in head, tag


def test_master_svg_layers():
    svg = (ROOT / "scripts/icons/icon.svg").read_text()
    assert 'id="bg"' in svg and 'id="mark"' in svg
