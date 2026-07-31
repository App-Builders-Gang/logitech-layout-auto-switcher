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


# -- hero ---------------------------------------------------------------------


def hero(p: dict) -> str:
    def keycap(x, y, label, w=44):
        return (
            f'<rect x="{x}" y="{y}" width="{w}" height="30" rx="6" fill="{p["panel"]}" '
            f'stroke="{p["line"]}" stroke-width="1.5"/>'
            + _text(x + w / 2, y + 20, label, fill=p["text"], size=13, family=MONO, anchor="middle")
        )

    body = ['<rect width="900" height="230" fill="none"/>']

    # macOS side
    body.append(_box(0, 34, 250, 132, p))
    body.append(_text(24, 64, "macOS", fill=p["muted"], size=13, weight=600))
    body.append(keycap(24, 82, "⌘"))
    body.append(keycap(76, 82, "⌥"))
    body.append(keycap(128, 82, "ctrl"))
    body.append(_text(24, 146, "platform 1", fill=p["accent"], size=12, family=MONO))

    # keyboard in the middle
    body.append(_box(310, 52, 280, 96, p, radius=14))
    for row in range(3):
        for col in range(9):
            body.append(
                f'<rect x="{330 + col * 27}" y="{70 + row * 24}" width="19" height="16" rx="3.5" '
                f'fill="{p["line"]}"/>'
            )
    body.append(
        _text(450, 176, "one keyboard", fill=p["text"], size=14, weight=600, anchor="middle")
    )
    body.append(
        _text(
            450,
            196,
            "KVM · Easy-Switch · cable",
            fill=p["muted"],
            size=12,
            family=MONO,
            anchor="middle",
        )
    )

    # Windows side
    body.append(_box(650, 34, 250, 132, p))
    body.append(_text(674, 64, "Windows", fill=p["muted"], size=13, weight=600, anchor="start"))
    body.append(keycap(674, 82, "ctrl"))
    body.append(keycap(726, 82, "alt"))
    body.append(keycap(778, 82, "win"))
    body.append(_text(674, 146, "platform 0", fill=p["accent"], size=12, family=MONO))

    # the arrows that make the point: the layout follows the machine
    body.append(_arrow(300, 100, 262, 100, p, colour=p["accent"]))
    body.append(_arrow(600, 100, 638, 100, p, colour=p["accent"]))
    body.append(
        _text(
            450,
            26,
            "the layout follows the machine",
            fill=p["text"],
            size=15,
            weight=600,
            anchor="middle",
        )
    )
    body.append(
        _text(
            450,
            220,
            "corrected in ~1 s, measured",
            fill=p["good"],
            size=12,
            family=MONO,
            anchor="middle",
        )
    )
    return _svg(900, 230, p, "".join(body))


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


# -- social preview -----------------------------------------------------------


def social(p: dict) -> str:
    body = [
        f'<rect width="1280" height="640" fill="{p["panel"]}"/>',
        _text(80, 210, "Logitech Layout Auto Switcher", fill=p["text"], size=58, weight=700),
        _text(
            80,
            280,
            "Your MX Keys should know which computer it is plugged into.",
            fill=p["muted"],
            size=27,
        ),
        _text(80, 320, "Now it does.", fill=p["muted"], size=27),
        _text(
            80,
            420,
            "Mac ⇄ Windows layout, switched by the keyboard itself",
            fill=p["accent"],
            size=25,
            family=MONO,
        ),
        _text(
            80,
            470,
            "HID++ 0x4531 · no remapping · no account",
            fill=p["muted"],
            size=22,
            family=MONO,
        ),
        _text(80, 556, "corrected in ~1 s", fill=p["good"], size=24, family=MONO, weight=700),
        f'<rect x="80" y="140" width="72" height="6" rx="3" fill="{p["accent"]}"/>',
    ]
    return _svg(1280, 640, p, "".join(body))


DIAGRAMS = {
    "hero": hero,
    "flow": flow,
    "latency": latency,
    "architecture": architecture,
    "social-preview": social,
}


def build() -> dict[Path, str]:
    out: dict[Path, str] = {}
    for name, fn in DIAGRAMS.items():
        for suffix, palette in (("light", LIGHT), ("dark", DARK)):
            out[ASSETS / f"{name}-{suffix}.svg"] = fn(palette) + "\n"
    return out


def _render_social_png() -> None:
    """GitHub's social-preview uploader takes PNG/JPG, not SVG.

    Optional: needs rsvg-convert (``brew install librsvg``). The committed PNG is
    what matters, so a machine without it just skips this step.
    """
    source = ASSETS / "social-preview-dark.svg"
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
