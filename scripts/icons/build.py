"""Rebuilds this app's favicon and app icons from scripts/icons/icon.svg.

House standard (same set as redline and jambo): one bold mark on a rich tile.
icon.svg is a 512x512 master with two layers:
  <g id="bg">    full-bleed background (may run edge to edge)
  <g id="mark">  the emblem, kept within ~210px of the centre
  <g id="mark-small"> optional simpler/bigger mark used only for favicons
Headless Chromium (Playwright) rasterizes each variant:

    pip install playwright && playwright install chromium
    python3 scripts/icons/build.py [out_dir]

Writes favicon.ico (16/32/48), apple-touch-icon.png (180, square: iOS rounds
it), icon-192.png and icon-512.png (rounded tile, transparent corners) and
icon-maskable-512.png (mark scaled into the 80% safe zone) into out_dir
(default: the OUT path below).
"""
import pathlib
import struct
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
OUT = (HERE / '../../app/static').resolve()
FAVICON_SIZES = (16, 32, 48)

# filename, pixel size, variant, transparent corners
JOBS = [
    *[(f'favicon-{n}.png', n, 'favicon', True) for n in FAVICON_SIZES],
    ('apple-touch-icon.png', 180, 'square', False),
    ('icon-192.png', 192, 'rounded', True),
    ('icon-512.png', 512, 'rounded', True),
    ('icon-maskable-512.png', 512, 'maskable', False),
]
# Project-specific extra renders: (path relative to two levels above this script, size, variant, transparent).
EXTRA = []

# Applied in the page to the inline master: clip to a rounded tile, shrink the
# mark into the maskable safe zone, or swap in the favicon mark.
VARIANT_JS = '''(v) => {
  const svg = document.querySelector('svg');
  const NS = 'http://www.w3.org/2000/svg';
  const mark = svg.querySelector('#mark'), small = svg.querySelector('#mark-small');
  if (small) small.style.display = v === 'favicon' ? '' : 'none';
  if (v === 'favicon' && small) mark.style.display = 'none';
  if (v === 'maskable') mark.setAttribute('transform', 'translate(256 256) scale(0.8) translate(-256 -256)');
  if (v === 'rounded' || v === 'favicon') {
    const defs = svg.querySelector('defs') || svg.insertBefore(document.createElementNS(NS, 'defs'), svg.firstChild);
    const cp = document.createElementNS(NS, 'clipPath'); cp.id = 'tile';
    const r = document.createElementNS(NS, 'rect');
    r.setAttribute('width', 512); r.setAttribute('height', 512); r.setAttribute('rx', v === 'favicon' ? 96 : 112);
    cp.append(r); defs.append(cp);
    const g = document.createElementNS(NS, 'g'); g.setAttribute('clip-path', 'url(#tile)');
    [...svg.childNodes].filter((n) => n !== defs).forEach((n) => g.append(n));
    svg.append(g);
  }
}'''


def render(out_dir: pathlib.Path) -> None:
    master = (HERE / 'icon.svg').read_text()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        jobs = [(out_dir / n, *rest) for n, *rest in JOBS] + [(HERE.parents[1] / n, *rest) for n, *rest in EXTRA]
        for path, size, variant, transparent in jobs:
            pg = browser.new_page(viewport={'width': size, 'height': size}, device_scale_factor=1)
            pg.set_content('<html><body style="margin:0"><style>svg{display:block;width:100%;height:100%}</style>'
                           f'<div style="width:{size}px;height:{size}px">{master}</div></body></html>')
            pg.evaluate('document.fonts.ready')
            pg.evaluate(VARIANT_JS, variant)
            pg.wait_for_timeout(100)
            pg.screenshot(path=str(path), omit_background=transparent,
                          clip={'x': 0, 'y': 0, 'width': size, 'height': size})
            pg.close()
        browser.close()


def pack_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    """An .ico whose entries are embedded PNGs (supported by every current browser)."""
    header = struct.pack('<HHH', 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    directory = b''
    for size, data in pngs:
        directory += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + directory + b''.join(data for _, data in pngs)


if __name__ == '__main__':
    out = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else OUT
    out.mkdir(parents=True, exist_ok=True)
    render(out)
    favicons = [(n, (out / f'favicon-{n}.png').read_bytes()) for n in FAVICON_SIZES]
    (out / 'favicon.ico').write_bytes(pack_ico(favicons))
    for n in FAVICON_SIZES:
        (out / f'favicon-{n}.png').unlink()
    print(f'Wrote favicon.ico, apple-touch-icon.png, icon-192.png, icon-512.png, icon-maskable-512.png to {out}')
