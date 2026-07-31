"""Generate the README's SVG visuals, light and dark.

Hand-written SVG rather than a diagramming tool: the output is small, crisp at any
zoom, diffable in review, and needs no toolchain to rebuild. GitHub ignores
``prefers-color-scheme`` inside an SVG referenced by ``<img>``, so every diagram is
emitted twice and the README picks one with ``<picture>``.

    python tools/make_assets.py            # writes assets/*.svg
    python tools/make_assets.py --check    # fails if the files on disk are stale

Numbers here are measured, not decorative. See CHANGELOG 2.0.2 for where the
600s -> 32s -> 1.1s recovery figures come from.
"""

from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# GitHub's own light/dark palettes, so the diagrams sit naturally in a README.
LIGHT = {
    "text": "#1f2328",
    "muted": "#59636e",
    "line": "#d1d9e0",
    "panel": "#f6f8fa",
    "accent": "#0969da",
    "good": "#1a7f37",
    "bad": "#cf222e",
    "shadow": "#00000010",
}
DARK = {
    "text": "#e6edf3",
    "muted": "#9198a1",
    "line": "#3d444d",
    "panel": "#151b23",
    "accent": "#4493f8",
    "good": "#3fb950",
    "bad": "#f85149",
    "shadow": "#00000040",
}

MONO = "ui-monospace,SFMono-Regular,SF Mono,Menlo,Consolas,monospace"
SANS = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif"


def _text(x, y, s, *, fill, size=13, family=SANS, weight=400, anchor="start", opacity=1.0):
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}" opacity="{opacity}">{s}</text>'
    )


def _box(x, y, w, h, p, *, stroke=None, radius=10, fill=None):
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
        f'fill="{fill or p["panel"]}" stroke="{stroke or p["line"]}" stroke-width="1.5"/>'
    )


def _arrow(x1, y1, x2, y2, p, *, colour=None, dashed=False):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour or p["muted"]}" '
        f'stroke-width="1.75" marker-end="url(#head)"{dash}/>'
    )


#: Half a stroke width would clip against the viewBox edge, so everything is drawn
#: inside a small inset.
PAD = 2


def _svg(width, height, p, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width + PAD * 2} '
        f'{height + PAD * 2}" width="{width + PAD * 2}" height="{height + PAD * 2}" role="img">'
        f'<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["muted"]}"/></marker></defs>'
        f'<g transform="translate({PAD},{PAD})">{body}</g></svg>'
    )


# -- keyboard, hero and social card -------------------------------------------

KEYBOARD = ASSETS / "keyboard-mx-keys.svg"
#: The committed vector's own coordinate system, so annotations can be placed
#: against real keycaps rather than guessed at.
KB_VIEWBOX = (145, 292, 1712, 552)
#: The dual-legend cluster on the bottom row: opt/start and cmd/alt share a keycap,
#: and which legend is live is exactly what this project switches. Measured off a
#: 1:1 render by scanning for the keycap edges, not eyeballed -- a ring that misses
#: the keys by ten units is obvious to every reader.
KB_DUAL_LEGEND = (274, 743, 208, 64)

#: Both logos are drawn on a 24x24 grid and scaled to wherever they are needed.
APPLE_PATH = (
    "M16.365 1.43c0 1.14-.467 2.24-1.23 3.04-.82.86-2.15 1.52-3.26 1.43-.13-1.1.42-2.27"
    "1.17-3.02.83-.85 2.27-1.47 3.32-1.45zM20.6 17.02c-.6 1.36-.89 1.97-1.66 3.17-1.08"
    "1.68-2.6 3.77-4.48 3.79-1.68.01-2.11-1.09-4.38-1.08-2.27.01-2.75 1.1-4.42 1.09-1.88"
    "-.02-3.32-1.91-4.4-3.59C-1.7 15.7-2 9.68.44 6.5c1.13-1.5 2.9-2.44 4.57-2.44 1.7 0"
    " 2.77 1.09 4.36 1.09 1.55 0 2.49-1.09 4.46-1.09 1.5 0 3.09.82 4.22 2.23-3.71 2.03"
    "-3.11 7.33.55 8.73z"
)
WINDOWS_PATH = (
    "M0 3.45 10.35 2.05 10.35 11.6 0 11.6z M11.6 1.85 24 0 24 11.5 11.6 11.5z "
    "M0 12.85 10.35 12.85 10.35 22.4 0 21z M11.6 12.95 24 12.95 24 24 11.6 22.2z"
)


def _icon(path: str, x: float, y: float, size: float, fill: str) -> str:
    """Place a 24x24 icon path with its top-left corner at (x, y)."""
    scale = size / 24.0
    return (
        f'<g transform="translate({x},{y}) scale({scale:.4f})"><path d="{path}" fill="{fill}"/></g>'
    )


#: Dark product-card palette. The keyboard is dark in every theme, so the hero and
#: the social card carry their own background and read identically in light and
#: dark READMEs -- one file each instead of a light/dark pair holding 400 KB twice.
CARD = {
    "bg": "#14171a",
    "edge": "#2a3038",
    "panel": "#1b2026",
    "text": "#e6edf3",
    "muted": "#9198a1",
    "accent": "#4493f8",
    "good": "#3fb950",
}


def _keyboard(scale: float, x: float, y: float) -> str:
    """Place the committed MX Keys vector at (x, y), scaled about its own origin."""
    inner = KEYBOARD.read_text("utf-8")
    inner = inner[inner.index(">", inner.index("<svg")) + 1 : inner.rindex("</svg>")]
    dx = x - KB_VIEWBOX[0] * scale
    dy = y - KB_VIEWBOX[1] * scale
    # Two things the source file gets for free as a standalone document and loses
    # the moment its content is embedded:
    #   fill="none" sits on its root <svg> and is inherited by every shape that
    #   sets no fill; without it those shapes default to black and paint slabs
    #   over the keycaps.
    #   Its viewBox only *clips* while it is the root. Embedded, everything
    #   outside that window -- the prototype header and the author's watermark --
    #   would render wherever the composition happens to put it.
    # The clip lives on an untransformed wrapper and the transform on a group inside
    # it. Renderers disagree about whether an element's own transform applies to its
    # clip-path, and putting the two on one element makes the crop depend on that
    # disagreement -- here it cost the bottom row and the numpad.
    _, _, vw, vh = KB_VIEWBOX
    return (
        f'<defs><clipPath id="kbclip" clipPathUnits="userSpaceOnUse">'
        f'<rect x="{x}" y="{y}" width="{vw * scale}" height="{vh * scale}"/>'
        f"</clipPath></defs>"
        f'<g clip-path="url(#kbclip)"><g fill="none" '
        f'transform="translate({dx:.1f},{dy:.1f}) scale({scale})">{inner}</g></g>'
    )


def _card_svg(width, height, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">'
        f'<defs><marker id="kbhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{CARD["accent"]}"/></marker></defs>'
        f"{body}</svg>"
    )


def hero() -> str:
    c = CARD
    w, h = 1760, 900
    kx, ky = 24, 168
    body = [
        f'<rect width="{w}" height="{h}" rx="26" fill="{c["bg"]}" stroke="{c["edge"]}" '
        f'stroke-width="2"/>',
        _text(48, 74, "One keycap. Two legends.", fill=c["text"], size=38, weight=700),
        _text(
            48,
            116,
            "Your MX Keys already carries both. logiswitch tells it which one is live.",
            fill=c["muted"],
            size=21,
        ),
        _text(
            1712,
            74,
            "HID++ 0x4531",
            fill=c["accent"],
            size=20,
            family=MONO,
            anchor="end",
        ),
        _text(1712, 108, "no remapping", fill=c["muted"], size=17, family=MONO, anchor="end"),
        _keyboard(1.0, kx, ky),
    ]

    # Ring the keys whose meaning changes, then lead the eye down to the cards.
    hx = kx + (KB_DUAL_LEGEND[0] - KB_VIEWBOX[0])
    hy = ky + (KB_DUAL_LEGEND[1] - KB_VIEWBOX[1])
    hw, hh = KB_DUAL_LEGEND[2], KB_DUAL_LEGEND[3]
    body.append(
        f'<rect x="{hx - 6}" y="{hy - 6}" width="{hw + 12}" height="{hh + 12}" rx="12" '
        f'fill="none" stroke="{c["accent"]}" stroke-width="3"/>'
    )
    body.append(
        f'<path d="M {hx + hw / 2} {hy + hh + 10} L {hx + hw / 2} 742" fill="none" '
        f'stroke="{c["accent"]}" stroke-width="2" stroke-dasharray="6 5"/>'
    )

    cards = [
        (APPLE_PATH, "on macOS", "⌘ cmd   ⌥ opt", "platform 1", c["accent"]),
        (WINDOWS_PATH, "on Windows", "alt   start", "platform 0", c["accent"]),
        (None, "either way", "corrected in ~1 s", "measured, not claimed", c["good"]),
    ]
    for i, (icon, title, keys, note, colour) in enumerate(cards):
        x = 48 + i * 562
        body.append(
            f'<rect x="{x}" y="{754}" width="530" height="104" rx="14" fill="{c["panel"]}" '
            f'stroke="{c["edge"]}" stroke-width="1.5"/>'
        )
        text_x = x + 26
        if icon is not None:
            body.append(_icon(icon, x + 26, 774, 26, c["text"]))
            text_x = x + 64
        body.append(_text(text_x, 790, title, fill=c["muted"], size=18, weight=600))
        body.append(_text(x + 26, 830, keys, fill=c["text"], size=25, family=MONO, weight=700))
        body.append(_text(x + 504, 830, note, fill=colour, size=15, family=MONO, anchor="end"))
    return _card_svg(w, h, "".join(body))


def social() -> str:
    c = CARD
    w, h = 1280, 640
    body = [
        f'<rect width="{w}" height="{h}" fill="{c["bg"]}"/>',
        f'<rect x="72" y="70" width="64" height="6" rx="3" fill="{c["accent"]}"/>',
        _text(72, 142, "Logitech Layout Auto Switcher", fill=c["text"], size=52, weight=700),
        _text(
            72,
            192,
            "Your MX Keys should know which computer it is plugged into.",
            fill=c["muted"],
            size=25,
        ),
        _text(72, 228, "Now it does.", fill=c["muted"], size=25),
        _icon(APPLE_PATH, 72, 268, 26, c["text"]),
        _text(112, 289, "⇄", fill=c["muted"], size=26, family=MONO),
        _icon(WINDOWS_PATH, 148, 269, 25, c["text"]),
        _text(
            192,
            289,
            "HID++ 0x4531 · no remapping",
            fill=c["accent"],
            size=22,
            family=MONO,
        ),
        _keyboard(0.50, 212, 348),
        _text(
            72,
            330,
            "corrected in ~1 s, measured",
            fill=c["good"],
            size=22,
            family=MONO,
            weight=700,
        ),
    ]
    return _card_svg(w, h, "".join(body))


# -- flow ---------------------------------------------------------------------


def flow(p: dict) -> str:
    body = []
    stages = [
        (0, "KVM / Easy-Switch", "hands the keyboard over"),
        (330, "logiswitch", "hears the device arrive"),
        (660, "MX Keys S", "writes 0x4531"),
    ]
    for x, title, sub in stages:
        body.append(_box(x, 30, 240, 74, p))
        body.append(_text(x + 20, 58, title, fill=p["text"], size=14, weight=600))
        body.append(_text(x + 20, 80, sub, fill=p["muted"], size=12, family=MONO))

    body.append(_arrow(248, 67, 322, 67, p, colour=p["accent"]))
    body.append(_text(285, 52, "arrival", fill=p["accent"], size=11, family=MONO, anchor="middle"))
    body.append(_arrow(578, 67, 652, 67, p, colour=p["accent"]))
    body.append(_text(615, 52, "setHost", fill=p["accent"], size=11, family=MONO, anchor="middle"))

    # A self-loop on the agent: it re-checks on its own when nothing announces.
    body.append(
        f'<path d="M 500 106 L 500 140 L 400 140 L 400 108" fill="none" stroke="{p["muted"]}" '
        f'stroke-width="1.5" stroke-dasharray="5 4" marker-end="url(#head)"/>'
    )
    body.append(
        _text(
            524,
            144,
            "20 s backstop — for hardware that announces nothing",
            fill=p["muted"],
            size=11,
            family=MONO,
        )
    )
    body.append(
        _text(
            898,
            20,
            "no remapping — the keyboard changes mode",
            fill=p["muted"],
            size=11,
            family=MONO,
            anchor="end",
        )
    )
    return _svg(900, 165, p, "".join(body))


# -- latency ------------------------------------------------------------------


def latency(p: dict) -> str:
    rows = [
        ("before", 600.0, "worst case: the 600 s safety heartbeat", p["bad"]),
        ("v2.0.1", 32.0, "reconnect seen, but the backoff was already at 30 s", p["muted"]),
        ("v2.0.2", 1.1, "the device announces itself and is believed", p["good"]),
    ]
    # Log scale: linear would render 1.1 s as a hairline against 600 s.
    span = math.log(601.0)
    body = [
        _text(
            0,
            18,
            "Time an Easy-Switch return stays on the wrong layout",
            fill=p["text"],
            size=14,
            weight=600,
        ),
        _text(900, 18, "log scale", fill=p["muted"], size=11, family=MONO, anchor="end"),
    ]
    for i, (label, seconds, note, colour) in enumerate(rows):
        y = 46 + i * 52
        width = max(6.0, 640.0 * math.log(1 + seconds) / span)
        body.append(_text(0, y + 20, label, fill=p["text"], size=13, family=MONO, weight=600))
        body.append(
            f'<rect x="72" y="{y}" width="{width:.1f}" height="26" rx="5" fill="{colour}" '
            f'opacity="0.85"/>'
        )
        value = f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
        body.append(
            _text(72 + width + 12, y + 18, value, fill=p["text"], size=13, family=MONO, weight=600)
        )
        body.append(_text(72, y + 44, note, fill=p["muted"], size=11.5))
    return _svg(900, 212, p, "".join(body))


# -- architecture -------------------------------------------------------------


def architecture(p: dict) -> str:
    body = [
        _text(
            0,
            18,
            "Every thread sits in a kernel wait until hardware moves",
            fill=p["text"],
            size=14,
            weight=600,
        ),
    ]
    body.append(_box(0, 38, 250, 78, p))
    body.append(_text(20, 64, "watcher thread", fill=p["text"], size=13, weight=600))
    body.append(_text(20, 84, "IOKit / cfgmgr32", fill=p["muted"], size=11.5, family=MONO))
    body.append(_text(20, 102, "device arrived / left", fill=p["muted"], size=11.5, family=MONO))

    body.append(_box(0, 132, 250, 78, p))
    body.append(_text(20, 158, "reader thread × handle", fill=p["text"], size=13, weight=600))
    body.append(_text(20, 178, "blocked in hid_read", fill=p["muted"], size=11.5, family=MONO))
    body.append(_text(20, 196, "unsolicited frame = back", fill=p["muted"], size=11.5, family=MONO))

    body.append(_box(340, 85, 190, 78, p, stroke=p["accent"]))
    body.append(_text(360, 111, "event queue", fill=p["text"], size=13, weight=600))
    body.append(_text(360, 131, "coalesced, bounded", fill=p["muted"], size=11.5, family=MONO))
    body.append(_text(360, 149, "never blocks a reader", fill=p["muted"], size=11.5, family=MONO))

    body.append(_box(620, 85, 280, 78, p))
    body.append(_text(640, 111, "worker thread", fill=p["text"], size=13, weight=600))
    body.append(
        _text(640, 131, "read platform → write if wrong", fill=p["muted"], size=11.5, family=MONO)
    )
    body.append(
        _text(
            640, 149, "one cached read in the common case", fill=p["muted"], size=11.5, family=MONO
        )
    )

    body.append(_arrow(258, 77, 332, 100, p))
    body.append(_arrow(258, 171, 332, 148, p))
    body.append(_arrow(538, 124, 612, 124, p, colour=p["accent"]))
    body.append(
        _text(
            450,
            232,
            "handles are closed only after their readers are joined",
            fill=p["muted"],
            size=11.5,
            family=MONO,
            anchor="middle",
        )
    )
    return _svg(900, 244, p, "".join(body))


#: Diagrams that need a light and a dark variant, selected with <picture>.
THEMED = {"flow": flow, "latency": latency, "architecture": architecture}
#: Product cards: they carry their own dark background, so one file serves both
#: themes and the 400 KB keyboard vector is embedded once, not twice.
CARDS = {"hero": hero, "social-preview": social}


def build() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for name, fn in THEMED.items():
        for suffix, palette in (("light", LIGHT), ("dark", DARK)):
            out[ASSETS / f"{name}-{suffix}.svg"] = fn(palette) + "\n"
    for name, fn in CARDS.items():
        out[ASSETS / f"{name}.svg"] = fn() + "\n"
    return out


def _render_social_png() -> None:
    """GitHub's social-preview uploader takes PNG/JPG, not SVG.

    Optional: needs rsvg-convert (``brew install librsvg``). The committed PNG is
    what matters, so a machine without it just skips this step.
    """
    source = ASSETS / "social-preview.svg"
    target = ASSETS / "social-preview.png"
    tool = shutil.which("rsvg-convert")
    if tool is None:
        print("skipping social-preview.png: rsvg-convert not installed", file=sys.stderr)
        return
    subprocess.run(
        [tool, "-w", "1280", "-h", "640", str(source), "-o", str(target)],
        check=True,
        capture_output=True,
    )
    print(f"wrote {target.relative_to(ASSETS.parent)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if any file is stale")
    args = parser.parse_args()

    ASSETS.mkdir(exist_ok=True)
    stale = []
    for path, content in build().items():
        if args.check:
            if not path.exists() or path.read_text("utf-8") != content:
                stale.append(path.name)
            continue
        path.write_text(content, "utf-8")
        print(f"wrote {path.relative_to(ASSETS.parent)}")
    if stale:
        print("stale, re-run tools/make_assets.py: " + ", ".join(sorted(stale)), file=sys.stderr)
        return 1
    if not args.check:
        _render_social_png()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
