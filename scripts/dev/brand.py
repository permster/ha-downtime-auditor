"""Render the brand images (custom_components/downtime_auditor/brand/*.png) from SVG.

The icon: a clock (blue arc, dark hands) with orange "gap" dots for the downtime, and a
badge with a magnifying glass + check ("audited"), cut out of the clock so it reads on
any background. Light and dark variants, icon and logo, 1x and 2x. Edit the SVG below,
then run with the screenshots venv (see scripts/dev/README.md):

    python scripts/dev/brand.py            # writes the brand folder + scripts/dev/brand_preview.png
    python scripts/dev/brand.py out/dir    # or somewhere else first
"""
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
OUT = (Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "custom_components" / "downtime_auditor" / "brand").resolve()
OUT.mkdir(parents=True, exist_ok=True)

LIGHT = {"blue": "#2f7fc4", "hands": "#1f2b3a", "orange": "#ef8a22",
         "badge": "#1f2b3a", "glass": "#ffffff", "check": "#ef8a22",
         "text1": "#1f2b3a", "text2": "#2f7fc4"}
DARK = {"blue": "#5aaeea", "hands": "#e8edf3", "orange": "#f5a04a",
        "badge": "#e8edf3", "glass": "#1f2b3a", "check": "#ef8a22",
        "text1": "#e8edf3", "text2": "#5aaeea"}


def icon_svg(c: dict, size: int, uid: str) -> str:
    # The badge is cut out of the clock (mask), so there's a clean gap on any background.
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="{size}" height="{size}">
  <defs><mask id="cut-{uid}"><rect width="512" height="512" fill="#fff"/><circle cx="405" cy="402" r="124" fill="#000"/></mask></defs>
  <g mask="url(#cut-{uid})">
    <path d="M 283 36 A 222 222 0 1 0 270 476" fill="none" stroke="{c['blue']}" stroke-width="62" stroke-linecap="round"/>
    <line x1="242" y1="240" x2="142" y2="214" stroke="{c['hands']}" stroke-width="34" stroke-linecap="round"/>
    <line x1="242" y1="240" x2="346" y2="134" stroke="{c['hands']}" stroke-width="34" stroke-linecap="round"/>
    <circle cx="242" cy="240" r="31" fill="{c['hands']}"/>
    <circle cx="352" cy="52" r="16" fill="{c['orange']}"/>
    <circle cx="421" cy="118" r="16" fill="{c['orange']}"/>
    <circle cx="457" cy="205" r="16" fill="{c['orange']}"/>
  </g>
  <circle cx="405" cy="402" r="108" fill="{c['badge']}"/>
  <circle cx="392" cy="389" r="46" fill="none" stroke="{c['glass']}" stroke-width="20"/>
  <line x1="426" y1="423" x2="460" y2="457" stroke="{c['glass']}" stroke-width="24" stroke-linecap="round"/>
  <polyline points="372,390 387,405 412,376" fill="none" stroke="{c['check']}" stroke-width="13" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


def logo_html(c: dict, scale: int, uid: str) -> str:
    h = 128 * scale
    return f"""<div class="logo" style="width:{435 * scale}px;height:{h}px;display:flex;align-items:center;gap:{14 * scale}px">
  {icon_svg(c, h, uid)}
  <div style="font-family:'Segoe UI',system-ui,sans-serif;font-weight:700;line-height:1.02;letter-spacing:{0.5 * scale}px">
    <div style="font-size:{50 * scale}px;color:{c['text1']}">Downtime</div>
    <div style="font-size:{50 * scale}px;color:{c['text2']};font-weight:600">Auditor</div>
  </div></div>"""


FILES = [  # (file, kind, palette, scale)
    ("icon.png", "icon", LIGHT, 1), ("icon@2x.png", "icon", LIGHT, 2),
    ("dark_icon.png", "icon", DARK, 1), ("dark_icon@2x.png", "icon", DARK, 2),
    ("logo.png", "logo", LIGHT, 1), ("logo@2x.png", "logo", LIGHT, 2),
    ("dark_logo.png", "logo", DARK, 1), ("dark_logo@2x.png", "logo", DARK, 2),
]

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="msedge", headless=True)
    page = b.new_page(viewport={"width": 1000, "height": 600})
    for i, (name, kind, pal, scale) in enumerate(FILES):
        body = icon_svg(pal, 256 * scale, f"f{i}") if kind == "icon" else logo_html(pal, scale, f"f{i}")
        page.set_content(f'<html><body style="margin:0;background:transparent"><div id="x" style="display:inline-block">{body}</div></body></html>')
        page.locator("#x").screenshot(path=str(OUT / name), omit_background=True)
        print("wrote", name)
    # Preview: the new files on light and dark, at sizes HA uses (embedded: set_content can't load file://)
    import base64

    def src(name: str) -> str:
        return "data:image/png;base64," + base64.b64encode((OUT / name).read_bytes()).decode()

    sheet = "".join(
        f'<div style="background:{bg};padding:18px;display:flex;gap:22px;align-items:center">'
        f'<img src="{src(pre + "logo.png")}">'
        f'<img src="{src(pre + "icon.png")}" width="128">'
        f'<img src="{src(pre + "icon.png")}" width="48">'
        f'<img src="{src(pre + "icon.png")}" width="24"></div>'
        for bg, pre in (("#ffffff", ""), ("#f3f5f8", ""), ("#111418", "dark_"), ("#1c1f24", "dark_"))
    )
    page.set_content(f'<html><body style="margin:0">{sheet}</body></html>')
    page.locator("body").screenshot(path=str(Path(__file__).with_name("brand_preview.png")))
    b.close()
