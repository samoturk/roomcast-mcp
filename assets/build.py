# /// script
# requires-python = ">=3.11"
# dependencies = ["fonttools>=4.55", "uharfbuzz>=0.45"]
# ///
"""Builds roomcast's icon, logo, README banner and social preview card.

    uv run assets/build.py

All text is converted to outlines, so the SVGs look the same wherever they are shown,
including GitHub's <img> tags, which load no fonts. Inter and JetBrains Mono (both OFL) are
downloaded once into ~/.cache/roomcast-assets. PNGs are rendered with headless Chrome; set
CHROME to its path if it is not found.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "roomcast-assets"

FONT_ZIPS = {
    "https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip": (
        "9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e",
        ["extras/ttf/Inter-ExtraBold.ttf", "extras/ttf/Inter-SemiBold.ttf", "extras/ttf/Inter-Regular.ttf"],
    ),
    "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip": (
        "6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf",
        ["fonts/ttf/JetBrainsMono-Regular.ttf"],
    ),
}

DARK = {
    "bg": "#0f1417",
    "panel": "#182126",
    "line": "#2a363d",
    "ink": "#eef3f1",
    "mute": "#93a4a0",
    "accent": "#f2a541",
}
LIGHT = {
    "bg": "#f7f4ef",
    "panel": "#ffffff",
    "line": "#ddd6cb",
    "ink": "#0f1417",
    "mute": "#5b6865",
    "accent": "#c4730c",
}

TAGLINE = "Let your AI assistant play podcasts, radio and playlists on the speakers in your home."


def fetch_fonts() -> dict[str, Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    fonts = {}
    for url, (sha256, members) in FONT_ZIPS.items():
        targets = {m: CACHE / Path(m).name for m in members}
        if not all(t.exists() for t in targets.values()):
            archive = CACHE / Path(url).name
            if not archive.exists():
                print(f"downloading {url}")
                urllib.request.urlretrieve(url, archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256:
                archive.unlink()
                raise SystemExit(f"{url}: checksum mismatch")
            with zipfile.ZipFile(archive) as z:
                for member, target in targets.items():
                    target.write_bytes(z.read(member))
        fonts.update({t.stem: t for t in targets.values()})
    return fonts


@dataclass
class Run:
    d: str
    width: float
    top: float
    bottom: float


class Face:
    def __init__(self, path: Path):
        self.tt = TTFont(path)
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.upem = self.tt["head"].unitsPerEm
        self.hb = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))

    def run(self, text: str, size: float, tracking: float = 0) -> Run:
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, {"kern": True, "liga": True})
        s = size / self.upem
        path = SVGPathPen(self.glyphs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
        bounds = BoundsPen(self.glyphs)
        x = 0.0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
            glyph = self.glyphs[self.order[info.codepoint]]
            matrix = (s, 0, 0, -s, x + pos.x_offset * s, -pos.y_offset * s)
            glyph.draw(TransformPen(path, matrix))
            glyph.draw(TransformPen(bounds, matrix))
            x += pos.x_advance * s + tracking
        _, top, _, bottom = bounds.bounds or (0, 0, 0, 0)
        return Run(path.getCommands(), x - tracking, top, bottom)


def text(run: Run, x: float, y: float, fill: str, anchor: str = "start") -> str:
    if anchor == "end":
        x -= run.width
    elif anchor == "middle":
        x -= run.width / 2
    return f'<path transform="translate({x:.2f} {y:.2f})" fill="{fill}" d="{run.d}"/>'


# The speaker glyph is drawn on a 56-unit grid; its visible extent is x 6..51, y 8..48.
GLYPH_BOX = (6, 8, 51, 48)


def speaker(x: float, y: float, scale: float, color: str) -> str:
    return (
        f'<g transform="translate({x:.2f} {y:.2f}) scale({scale:.4f})" fill="none" stroke="{color}" '
        'stroke-width="4" stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="8" y="10" width="22" height="36" rx="5"/>'
        '<circle cx="19" cy="34" r="5"/>'
        f'<circle cx="19" cy="20" r="2" fill="{color}" stroke="none"/>'
        '<path d="M38 20a12 12 0 0 1 0 16"/>'
        '<path d="M45 13a22 22 0 0 1 0 30"/>'
        "</g>"
    )


def glow(cx: float, cy: float, r: float, color: str, opacity: float, gid: str) -> str:
    return (
        f'<defs><radialGradient id="{gid}"><stop offset="0" stop-color="{color}" stop-opacity="{opacity}"/>'
        f'<stop offset="0.65" stop-color="{color}" stop-opacity="0"/></radialGradient></defs>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#{gid})"/>'
    )


def document(width: float, height: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.2f} {height:.2f}" role="img" aria-label="{title}">'
        f"<title>{title}</title>{body}</svg>\n"
    )


def logo(faces: dict[str, Face], theme: dict, size: float, x: float, top: float) -> tuple[str, float, float]:
    """Glyph plus "roomcast" wordmark at font size `size`, top-left at (x, top). Returns svg, width, height."""
    room = faces["Inter-ExtraBold"].run("room", size, tracking=-0.03 * size)
    cast = faces["Inter-ExtraBold"].run("cast", size, tracking=-0.03 * size)
    word_top = min(room.top, cast.top)
    word_h = -word_top
    scale = 1.15 * word_h / (GLYPH_BOX[3] - GLYPH_BOX[1])
    glyph_w = (GLYPH_BOX[2] - GLYPH_BOX[0]) * scale
    glyph_h = (GLYPH_BOX[3] - GLYPH_BOX[1]) * scale
    height = max(glyph_h, word_h)
    baseline = top + (height + word_h) / 2
    glyph_top = top + (height - glyph_h) / 2
    gap = 0.3 * size
    word_x = x + glyph_w + gap
    svg = (
        speaker(x - GLYPH_BOX[0] * scale, glyph_top - GLYPH_BOX[1] * scale, scale, theme["accent"])
        + text(room, word_x, baseline, theme["ink"])
        + text(cast, word_x + room.width - 0.03 * size, baseline, theme["accent"])
    )
    width = glyph_w + gap + room.width - 0.03 * size + cast.width
    return svg, width, height


def build_icon(theme: dict) -> str:
    size = 512
    w = GLYPH_BOX[2] - GLYPH_BOX[0]
    h = GLYPH_BOX[3] - GLYPH_BOX[1]
    scale = 300 / w
    x = (size - w * scale) / 2 - GLYPH_BOX[0] * scale
    y = (size - h * scale) / 2 - GLYPH_BOX[1] * scale
    body = (
        '<clipPath id="r"><rect width="512" height="512" rx="112"/></clipPath><g clip-path="url(#r)">'
        f'<rect width="512" height="512" fill="{theme["bg"]}"/>'
        + glow(430, 80, 420, theme["accent"], 0.22, "g")
        + "</g>"
        + speaker(x, y, scale, theme["accent"])
    )
    return document(size, size, body, "roomcast")


def build_logo(faces: dict[str, Face], theme: dict) -> str:
    pad = 8
    svg, w, h = logo(faces, theme, 96, pad, pad)
    return document(w + 2 * pad, h + 2 * pad, svg, "roomcast")


def wrap(face: Face, words: str, size: float, max_width: float) -> list[Run]:
    lines: list[str] = []
    for word in words.split():
        if lines and face.run(f"{lines[-1]} {word}", size).width <= max_width:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    return [face.run(line, size) for line in lines]


def chip(faces: dict[str, Face], theme: dict, label: str, right: float, top: float, width: float, height: float) -> str:
    run = faces["Inter-SemiBold"].run(label, 0.42 * height)
    x = right - width
    return (
        f'<rect x="{x:.2f}" y="{top:.2f}" width="{width:.2f}" height="{height}" rx="{height / 2 - 6}" '
        f'fill="{theme["panel"]}" stroke="{theme["line"]}" stroke-width="1.5"/>'
        + text(run, x + 0.42 * height, top + (height - run.top - run.bottom) / 2, theme["ink"])
    )


def fan(theme: dict, x0: float, y0: float, x1: float, ys: list[float], head: bool) -> str:
    """Lines from (x0, y0) to each (x1, y), curving out from a shared trunk."""
    mid = x0 + (x1 - x0) * 0.45
    out = []
    for y in ys:
        out.append(
            f'<path d="M{x0:.2f} {y0:.2f}H{mid:.2f}C{mid + 40:.2f} {y0:.2f} {mid + 20:.2f} {y:.2f} '
            f'{mid + 60:.2f} {y:.2f}H{x1 - (10 if head else 0):.2f}" fill="none" stroke="{theme["line"]}" '
            'stroke-width="2"/>'
        )
        if head:
            out.append(
                f'<path d="M{x1 - 11:.2f} {y - 6:.2f}L{x1:.2f} {y:.2f}L{x1 - 11:.2f} {y + 6:.2f}Z" '
                f'fill="{theme["line"]}"/>'
            )
    return "".join(out)


def build_banner(faces: dict[str, Face], theme: dict) -> str:
    w, h = 1280, 320
    chips = ["Sonos", "Google Cast", "DLNA"]
    chip_h, chip_gap = 52, 12
    chip_w = max(faces["Inter-SemiBold"].run(c, 0.42 * chip_h).width for c in chips) + 0.84 * chip_h
    chips_top = (h - (3 * chip_h + 2 * chip_gap)) / 2
    chip_ys = [chips_top + i * (chip_h + chip_gap) for i in range(3)]

    mark, _, mark_h = logo(faces, theme, 84, 72, 72)
    lines = wrap(faces["Inter-SemiBold"], TAGLINE, 30, 640)
    body = (
        '<clipPath id="r"><rect width="1280" height="320" rx="24"/></clipPath><g clip-path="url(#r)">'
        f'<rect width="{w}" height="{h}" fill="{theme["bg"]}"/>'
        + glow(1080, 40, 420, theme["accent"], 0.16, "g")
        + "</g>"
        + mark
        + "".join(text(run, 72, 72 + mark_h + 62 + i * 40, theme["mute"]) for i, run in enumerate(lines))
        + fan(theme, 780, h / 2, 1208 - chip_w, [y + chip_h / 2 for y in chip_ys], head=False)
        + f'<circle cx="780" cy="{h / 2}" r="6" fill="{theme["accent"]}"/>'
        + "".join(chip(faces, theme, c, 1208, y, chip_w, chip_h) for c, y in zip(chips, chip_ys, strict=True))
    )
    return document(w, h, body, "roomcast: " + TAGLINE)


def box(faces: dict[str, Face], theme: dict, title: str, note: str, x: float, y: float, hub: bool = False):
    t = faces["Inter-SemiBold"].run(title, 22)
    n = faces["Inter-Regular"].run(note, 16)
    width = max(t.width, n.width) + 48
    stroke = theme["accent"] if hub else theme["line"]
    halo = (
        f'<rect x="{x - 4}" y="{y - 4}" width="{width + 8:.2f}" height="96" rx="20" fill="none" '
        f'stroke="{theme["accent"]}" stroke-opacity="0.15" stroke-width="5"/>'
        if hub
        else ""
    )
    svg = (
        halo + f'<rect x="{x}" y="{y}" width="{width:.2f}" height="88" rx="16" fill="{theme["panel"]}" '
        f'stroke="{stroke}" stroke-width="1.5"/>'
        + text(t, x + 24, y + 40, theme["ink"])
        + text(n, x + 24, y + 66, theme["mute"])
    )
    return svg, width


def build_social(faces: dict[str, Face], theme: dict) -> str:
    w, h = 1280, 640
    mark, _, _ = logo(faces, theme, 64, 80, 78)
    tag = wrap(faces["Inter-SemiBold"], TAGLINE, 36, 880)
    sub = faces["Inter-Regular"].run("An MCP server for Sonos, Google Cast and DLNA. Local, no cloud, no accounts.", 22)
    cmd = faces["JetBrainsMono-Regular"].run("uvx roomcast-mcp", 18)

    flow_y = 438
    client, client_w = box(
        faces, theme, "“Play the news in the kitchen”", "Claude, ChatGPT, Cursor, any MCP client", 80, flow_y
    )
    hub_x = 660
    hub, hub_w = box(faces, theme, "roomcast", "on your LAN", hub_x, flow_y, hub=True)
    chips = ["Sonos", "Chromecast · Nest", "DLNA renderers"]
    chip_h, chip_gap = 50, 11
    chip_w = max(faces["Inter-SemiBold"].run(c, 0.42 * chip_h).width for c in chips) + 0.84 * chip_h
    mid = flow_y + 44
    chips_top = mid - (3 * chip_h + 2 * chip_gap) / 2
    chip_ys = [chips_top + i * (chip_h + chip_gap) for i in range(3)]

    body = (
        f'<rect width="{w}" height="{h}" fill="{theme["bg"]}"/>'
        + glow(1060, 120, 460, theme["accent"], 0.18, "g")
        + text(cmd, 1200, 102, theme["mute"], anchor="end")
        + mark
        + "".join(text(run, 80, 206 + i * 46, theme["ink"]) for i, run in enumerate(tag))
        + text(sub, 80, 206 + len(tag) * 46 + 18, theme["mute"])
        + fan(theme, 80 + client_w, mid, hub_x, [mid], head=True)
        + fan(theme, hub_x + hub_w, mid, 1200 - chip_w, [y + chip_h / 2 for y in chip_ys], head=True)
        + client
        + hub
        + "".join(chip(faces, theme, c, 1200, y, chip_w, chip_h) for c, y in zip(chips, chip_ys, strict=True))
    )
    return document(w, h, body, "roomcast: " + TAGLINE)


def chrome() -> str:
    candidates = [
        os.environ.get("CHROME"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise SystemExit("Chrome not found; set CHROME to its path")


def render(svg: str, png: Path, width: int, height: int) -> None:
    """Renders `svg` at width x height (it scales to fit) to a transparent PNG."""
    sized = re.sub(r'width="[^"]*" height="[^"]*"', f'width="{width}" height="{height}"', svg, count=1)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.svg"
        page.write_text(sized)
        subprocess.run(
            [
                chrome(),
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--default-background-color=00000000",
                f"--window-size={width},{height}",
                f"--screenshot={png}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
    print(f"wrote {png.relative_to(OUT.parent)}")


def main() -> None:
    faces = {name: Face(path) for name, path in fetch_fonts().items()}
    svgs = {
        "icon.svg": build_icon(DARK),
        "logo-dark.svg": build_logo(faces, DARK),
        "logo-light.svg": build_logo(faces, LIGHT),
        "banner-dark.svg": build_banner(faces, DARK),
        "banner-light.svg": build_banner(faces, LIGHT),
        "social-preview.svg": build_social(faces, DARK),
    }
    for name, svg in svgs.items():
        (OUT / name).write_text(svg)
        print(f"wrote assets/{name}")
    render(svgs["icon.svg"], OUT / "icon-512.png", 512, 512)
    render(svgs["social-preview.svg"], OUT / "social-preview.png", 1280, 640)


if __name__ == "__main__":
    main()
