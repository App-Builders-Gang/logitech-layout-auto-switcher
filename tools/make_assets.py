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
import html
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
    # Only the animated scenes use these: they carry their own background, so they
    # need a page colour distinct from the panels sitting on it, and a second panel
    # tone for a panel nested inside another.
    "bg": "#ffffff",
    "panel2": "#eaeef2",
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
    "bg": "#0d1117",
    "panel2": "#1c2128",
}

MONO = "ui-monospace,SFMono-Regular,SF Mono,Menlo,Consolas,monospace"
SANS = "-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif"


def _text(x, y, s, *, fill, size=13, family=SANS, weight=400, anchor="start", opacity=1.0):
    # Escaped, because SVG is XML: a shell line like `a && b` is a parse error, not a
    # rendering quirk, and the file simply fails to load.
    body = html.escape(str(s), quote=False)
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}" opacity="{opacity}">{body}</text>'
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
#: Which legend the hero shows as live, on the shared clock. Long holds and a slow
#: crossfade: this is a banner someone glances at, not a demo they sit through.
HERO_MAC = (0.0, 7.6)
HERO_WIN = (9.0, 16.6)

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


def _card_svg(width, height, body, *, css: str = ""):
    """A product card. *css* opts it into motion on the same terms as :func:`_scene`.

    The reduced-motion rule is appended here rather than left to the caller, so a card
    cannot ship animation without an off switch.
    """
    style = ""
    if css:
        css += "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        style = f"<style><![CDATA[{css}]]></style>"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">'
        f'<defs><marker id="kbhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{CARD["accent"]}"/></marker></defs>'
        f"{style}{body}</svg>"
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

    # The one thing the hero should *show* rather than caption: the same keycap
    # meaning one thing, then the other. A leader line runs from the ringed keys to
    # whichever card is live, and the two swap on the shared clock. Only opacity is
    # animated, and only on four small elements -- the 400 KB keyboard vector below
    # is never touched, so this costs the file almost nothing.
    css = []
    for name, window in (("hmac", HERO_MAC), ("hwin", HERO_WIN)):
        css.append(_kf(name, _windows([window], fade=0.6)))
        css.append(f".{name}{{animation:{name} {LOOP:g}s linear infinite}}")

    ring_x, ring_y = hx + hw / 2, hy + hh + 10
    for cls, target_x, rest in (("hmac", 313.0, "1"), ("hwin", 875.0, "0")):
        body.append(
            f'<path class="{cls}" opacity="{rest}" d="M {ring_x} {ring_y} '
            f'L {target_x} 748" fill="none" '
            f'stroke="{c["accent"]}" stroke-width="2" stroke-dasharray="6 5"/>'
        )

    cards = [
        (APPLE_PATH, "on macOS", "⌘ cmd   ⌥ opt", "platform 1", c["accent"], "hmac", "1"),
        (WINDOWS_PATH, "on Windows", "alt   start", "platform 0", c["accent"], "hwin", "0"),
        (None, "either way", "corrected in ~1 s", "measured, not claimed", c["good"], None, None),
    ]
    for i, (icon, title, keys, note, colour, cls, rest) in enumerate(cards):
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
        if cls:
            # Drawn over the card rather than replacing its border, so the resting
            # state -- and the reduced-motion state -- is still a complete card.
            body.append(
                f'<rect class="{cls}" opacity="{rest}" x="{x}" y="754" width="530" '
                f'height="104" rx="14" fill="none" stroke="{c["accent"]}" stroke-width="2.5"/>'
            )
    return _card_svg(w, h, "".join(body), css="".join(css))


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
    # Each bar grows from nothing, one after another, so the eye is walked down the
    # three rows in the order the story happened rather than being handed the answer.
    # The base `width` attribute is the final value, so with motion suppressed the
    # chart is simply the finished chart.
    css = []
    for i, (label, seconds, note, colour) in enumerate(rows):
        y = 46 + i * 52
        width = max(6.0, 640.0 * math.log(1 + seconds) / span)
        start = 0.4 + i * 0.8
        css.append(
            _kf(
                f"latb{i}",
                [
                    (0.0, {"width": "0px"}),
                    (start, {"width": "0px"}),
                    (start + 1.4, {"width": f"{width:.1f}px"}),
                    (LOOP, {"width": f"{width:.1f}px"}),
                ],
            )
        )
        css.append(f".latb{i}{{animation:latb{i} {LOOP:g}s linear infinite}}")
        body.append(_text(0, y + 20, label, fill=p["text"], size=13, family=MONO, weight=600))
        body.append(
            f'<rect x="72" y="{y}" width="{width:.1f}" height="26" rx="5" fill="{colour}" '
            f'opacity="0.85" class="latb{i}"/>'
        )
        value = f"{seconds:.1f} s" if seconds < 10 else f"{seconds:.0f} s"
        body.append(
            _text(72 + width + 12, y + 18, value, fill=p["text"], size=13, family=MONO, weight=600)
        )
        body.append(_text(72, y + 44, note, fill=p["muted"], size=11.5))
    return _scene(
        940,
        252,
        p,
        f'<g transform="translate(24,22)">{"".join(body)}</g>',
        ident="lat",
        title="How long an Easy-Switch return stays on the wrong layout",
        desc=(
            "Three bars on a log scale. Before the fix the worst case was the 600 second "
            "safety heartbeat; version 2.0.1 saw the reconnect but its backoff had already "
            "reached 32 seconds; version 2.0.2 believes the device when it announces itself "
            "and recovers in 1.1 seconds."
        ),
        css="".join(css),
    )


# -- architecture -------------------------------------------------------------


def architecture(p: dict) -> str:
    body = [
        # _arrow points at a marker called "head", which _svg used to define and
        # _scene does not -- it names its own after the scene. Carry one here rather
        # than teach every other scene about a marker it will never draw.
        f'<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{p["muted"]}"/></marker></defs>',
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

    # One event walks watcher -> queue -> worker and nothing else moves, because the
    # claim being made is that nothing else *does* move: the threads are asleep in the
    # kernel until the hardware says otherwise.
    legs = [
        ("M 258 77 L 332 100", 0.0),
        ("M 258 171 L 332 148", 6.0),
        ("M 538 124 L 612 124", 0.0),
    ]
    css = [
        f"@keyframes apkt{{to{{stroke-dashoffset:{-(DASH + GAP):g}}}}}",
        ".ap{animation:apkt 0.9s linear infinite}",
    ]
    for i, (d, start) in enumerate(legs):
        gate = f"aleg{i}"
        css.append(_kf(gate, _windows([(start, start + 3.4)], fade=0.25)))
        css.append(f".{gate}{{animation:{gate} {LOOP:g}s linear infinite}}")
        body.append(_packets(d, p, scroll="ap", gate=gate))

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
    return _scene(
        944,
        288,
        p,
        f'<g transform="translate(22,22)">{"".join(body)}</g>',
        ident="arch",
        title="How the agent waits: every thread parked in the kernel until hardware moves",
        desc=(
            "A watcher thread on IOKit or cfgmgr32 reports a device arriving or leaving, and "
            "a reader thread per handle sits blocked in hid_read where an unsolicited frame "
            "means the device is back. Both feed a coalesced, bounded event queue that never "
            "blocks a reader, and the queue feeds a worker thread which reads the platform "
            "and writes only if it is wrong."
        ),
        css="".join(css),
    )


# -- animated scenes ----------------------------------------------------------
#
# The two scenes below move. Everything else in this file is a still.
#
# Motion is CSS ``@keyframes`` rather than SMIL for one reason: it can be turned
# off. ``prefers-reduced-motion`` is a user setting, not a document one, so unlike
# ``prefers-color-scheme`` it *does* apply to an SVG rendered inside an ``<img>``.
# The last rule in every scene is ``animation:none``, and every animated element
# carries a plain ``opacity``/``width`` attribute holding the value it ends the loop
# on -- so with motion suppressed the scene collapses to its final, still-meaningful
# frame instead of to a blank or half-built one.
#
# Data in flight is a dashed stroke whose ``stroke-dashoffset`` scrolls, drawn over
# a faint copy of the same path. That beats translating dots along the wire: it
# follows a Bezier for free, one element makes the whole train of packets, and the
# direction of travel is the sign of the offset.

#: Both scenes run on one clock, so any two things can be timed against each other
#: by writing down when they happen rather than by chaining delays.
LOOP = 18.0

#: Dash then gap. The scroll distance per period is their sum, or the packets jump.
#: A short period matters more than it looks: at one dash per 120 units the shorter
#: runs of wire hold no packet at all and read as dead.
DASH, GAP = 12.0, 40.0


def _kf(name: str, stops: list[tuple[float, dict[str, str]]], loop: float = LOOP) -> str:
    """A keyframes rule from (seconds, declarations) pairs on the shared clock."""
    merged: dict[float, dict[str, str]] = {}
    for t, decls in stops:
        merged.setdefault(round(min(max(t, 0.0), loop), 4), {}).update(decls)
    frames = "".join(
        f"{t / loop * 100:.4f}%{{{';'.join(f'{k}:{v}' for k, v in merged[t].items())}}}"
        for t in sorted(merged)
    )
    return f"@keyframes {name}{{{frames}}}"


def _windows(
    windows: list[tuple[float, float]],
    *,
    prop: str = "opacity",
    hi: str = "1",
    lo: str = "0",
    fade: float = 0.3,
    loop: float = LOOP,
) -> list[tuple[float, dict[str, str]]]:
    """Stops holding *lo* except during *windows*, where they hold *hi*.

    Written as intervals because that is how the storyboard reads: this label is up
    from 12.6 s to 14.0 s. A window touching an end of the loop stays *hi* there, so
    a state that survives the wrap does not flicker at the seam.
    """
    stops = [(0.0, {prop: lo}), (loop, {prop: lo})]
    for t0, t1 in windows:
        stops.append((0.0, {prop: hi}) if t0 <= 0 else (max(0.0, t0 - fade), {prop: lo}))
        stops.append((t0, {prop: hi}))
        stops.append((t1, {prop: hi}))
        if t1 < loop:
            stops.append((min(loop, t1 + fade), {prop: lo}))
        else:
            stops.append((loop, {prop: hi}))
    return stops


def _scene(width, height, p, body, *, title, desc, css, ident):
    """A self-contained card: its own background, its own stylesheet, one title."""
    css = css + "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-labelledby="{ident}t {ident}d">'
        f'<title id="{ident}t">{title}</title><desc id="{ident}d">{desc}</desc>'
        f"<style><![CDATA[{css}]]></style>"
        f'<defs><marker id="{ident}arrow" viewBox="0 0 10 10" refX="8.5" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0 L10 5 L0 10 z" fill="{p["accent"]}"/></marker></defs>'
        f'<rect width="{width}" height="{height}" rx="18" fill="{p["bg"]}"/>'
        f'<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="17.5" '
        f'fill="none" stroke="{p["line"]}"/>'
        f"{body}</svg>"
    )


def _wire(d, p, *, colour=None, width=1.4):
    """The static wire: always there, never the thing being looked at."""
    return f'<path d="{d}" fill="none" stroke="{colour or p["line"]}" stroke-width="{width}"/>'


def _packets(d, p, *, scroll, gate=None, colour=None, rest="0"):
    """Data in flight along *d*, as a train of dashes scrolling over the wire.

    The scroll and the gate that switches the stream on live on separate elements:
    ``animation`` is one declaration, so two classes on one element would mean one of
    them silently losing rather than both running.

    *rest* is the opacity to hold when motion is suppressed -- 0 for anything that
    only means something while it is moving.
    """
    path = (
        f'<path d="{d}" fill="none" stroke="{colour or p["accent"]}" stroke-width="3.5" '
        f'stroke-linecap="round" stroke-dasharray="{DASH:g} {GAP:g}" class="{scroll}"/>'
    )
    if gate is None:
        return path
    return f'<g class="{gate}" opacity="{rest}">{path}</g>'


def _chip(
    x, y, w, h, p, label, *, stroke=None, fill=None, size=12.5, colour=None, cls=None, opacity="0"
):
    """A pill label. Hidden by default because most chips are revealed by a class.

    *opacity* is the value it holds when nothing animates it -- ``"1"`` for a chip
    that is simply always there, which is also what it collapses to under
    ``prefers-reduced-motion``.
    """
    klass = f' class="{cls}"' if cls else ""
    return (
        f'<g{klass} opacity="{opacity}"><rect x="{x}" y="{y}" width="{w}" height="{h}" '
        f'rx="{h / 2:g}" fill="{fill or p["panel2"]}" stroke="{stroke or p["accent"]}" '
        f'stroke-width="1.5"/>'
        + _text(
            x + w / 2,
            y + h / 2 + size * 0.36,
            label,
            fill=colour or p["accent"],
            size=size,
            family=MONO,
            anchor="middle",
            weight=600,
        )
        + "</g>"
    )


# -- scene 1: what a KVM does to the layout, and what logiswitch does about it --
#
# The storyboard, in seconds on the shared clock. Four beats: it works, the KVM
# hands over, it is broken, logiswitch fixes it. The one number that is not staging
# is the write: the packet leaves the PC at 12.6 and reaches the keyboard at 13.7,
# which is the measured 1.1 s and is why those two are 1.1 apart rather than
# whatever looked good.
KVM_ACTS = ((0.0, 4.6), (4.6, 8.2), (8.2, 12.2), (12.2, 18.0))
KVM_SWITCH = 5.0  #: the KVM changes which host owns the receiver
KVM_ARRIVE = 12.2  #: the OS reports the device arriving on the PC
KVM_WRITE = (12.6, 13.7)  #: setHostPlatform in flight -- 1.1 s, measured
KVM_TOOK = 13.7  #: the keyboard is in Windows mode from here

#: The bus runs through the vertical middle of the keyboard.
BUS_Y = 288.0
KVM_WIRE_IN = f"M 324 {BUS_Y:g} L 596 {BUS_Y:g}"
KVM_ROUTE_MAC = f"M 610 {BUS_Y:g} C 680 {BUS_Y:g} 690 252 746 252 C 800 252 800 209 856 209"
KVM_ROUTE_PC = f"M 610 {BUS_Y:g} C 680 {BUS_Y:g} 690 324 746 324 C 800 324 800 399 856 399"
#: The write goes home the way the keystrokes came: same receiver, same wire.
KVM_WRITE_PATH = (
    f"M 856 399 C 800 399 800 324 746 324 C 690 324 680 {BUS_Y:g} 610 {BUS_Y:g} L 330 {BUS_Y:g}"
)


def _mini_keyboard(p: dict) -> str:
    """A suggestion of an MX Keys, with the two keycaps this project owns ringed.

    Not the real vector: the hero already carries that at 400 KB, and here the
    keyboard is one station out of four. What has to survive the abstraction is the
    bottom row, because the two dual-legend caps are the whole subject.
    """
    x0, y0, w = 62.0, 246.0, 240.0
    body = [
        f'<rect x="48" y="232" width="268" height="112" rx="14" fill="{p["panel"]}" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>'
    ]
    for row in range(3):
        gap, count = 3.0, 12
        kw = (w - gap * (count - 1)) / count
        for i in range(count):
            body.append(
                f'<rect x="{x0 + i * (kw + gap):.1f}" y="{y0 + row * 22:g}" '
                f'width="{kw:.1f}" height="16" rx="3.5" fill="{p["bg"]}" '
                f'stroke="{p["line"]}" stroke-width="1"/>'
            )
    # The bottom row is laid out by hand: fn, ctrl, then the two caps that carry
    # opt/start and cmd/alt, then the spacebar.
    x, gap = x0, 3.4
    for i, kw in enumerate((16, 16, 26, 26, 74, 26, 16, 16)):
        ringed = i in (2, 3)
        body.append(
            f'<rect x="{x:.1f}" y="312" width="{kw}" height="16" rx="3.5" '
            f'fill="{p["bg"]}" stroke="{p["accent"] if ringed else p["line"]}" '
            f'stroke-width="1"/>'
        )
        x += kw + gap
    body.append(
        f'<rect x="97.8" y="309" width="61.4" height="22" rx="7" fill="none" '
        f'stroke="{p["accent"]}" stroke-width="2"/>'
    )
    body.append(
        f'<path d="M 128.5 331 L 128.5 364" fill="none" stroke="{p["accent"]}" '
        f'stroke-width="1.5" stroke-dasharray="4 4"/>'
    )
    return "".join(body)


def kvm(p: dict) -> str:
    w, h = 1280, 600
    a1, a2, a3, a4 = KVM_ACTS
    css = [
        # One period of scroll equals one dash plus one gap, or the train stutters.
        f"@keyframes pkt{{to{{stroke-dashoffset:{-(DASH + GAP):g}}}}}",
        f"@keyframes pktr{{to{{stroke-dashoffset:{DASH + GAP:g}}}}}",
        ".p{animation:pkt 0.75s linear infinite}",
        ".pr{animation:pktr 0.7s linear infinite}",
    ]

    def cls(name, windows, **kw):
        css.append(_kf(name, _windows(windows, **kw)))
        css.append(f".{name}{{animation:{name} {LOOP:g}s linear infinite}}")
        return name

    # Typing: to the Mac, then to the PC. The gap in the middle of the PC's stream is
    # the write -- nothing is being typed while the layout is being corrected.
    cls("kbMac", [a1[:2]], hi="1", lo="0.34")
    cls("kbPC", [(KVM_SWITCH, LOOP)], hi="1", lo="0.34")
    cls("flowIn", [(0.3, 4.8), (5.4, 12.4), (14.2, 17.9)])
    cls("flowMac", [(0.3, 4.8)])
    cls("flowPC", [(5.4, 12.4), (14.2, 17.9)])
    # The selected branch is only a hint that the wire is live. It has to stay well
    # under the packets, which are the same colour: at equal weight the two merge and
    # the traffic disappears into the highlight.
    cls("liveMac", [(0.0, KVM_SWITCH)], hi="0.22", lo="0.06")
    cls("livePC", [(KVM_SWITCH, LOOP)], hi="0.22", lo="0.06")
    cls("dotMac", [(0.0, KVM_SWITCH)])
    cls("dotPC", [(KVM_SWITCH, LOOP)])
    cls("flowWrite", [KVM_WRITE], fade=0.15)
    cls("writeTag", [(KVM_WRITE[0] - 0.2, KVM_TOOK + 0.4)])
    cls("tookTag", [(KVM_TOOK, LOOP)])
    cls("agent", [(KVM_ARRIVE, LOOP)])
    cls("legMac", [(0.0, KVM_TOOK)])
    cls("legWin", [(KVM_TOOK, LOOP)])
    cls("pcIdle", [(0.0, KVM_SWITCH)])
    cls("pcWrong", [(KVM_SWITCH, KVM_ARRIVE)])
    cls("pcBusy", [(KVM_ARRIVE, KVM_TOOK)])
    cls("pcRight", [(KVM_TOOK, LOOP)])
    for i, (t0, t1) in enumerate(KVM_ACTS):
        cls(f"act{i}", [(t0, t1)])
    css.append(
        _kf(
            "ping",
            [
                (0.0, {"r": "6px", "opacity": "0"}),
                (KVM_ARRIVE, {"r": "6px", "opacity": "0.9"}),
                (KVM_ARRIVE + 1.2, {"r": "26px", "opacity": "0"}),
                (LOOP, {"r": "6px", "opacity": "0"}),
            ],
        )
    )
    css.append(f".ping{{animation:ping {LOOP:g}s linear infinite}}")

    body = [
        _text(
            48,
            56,
            "How a KVM breaks your keyboard — and what logiswitch does about it",
            fill=p["text"],
            size=21,
            weight=700,
        ),
        _text(
            48,
            84,
            "The keyboard holds one platform value for every machine on the switch. "
            "Whoever wrote it last wins — until logiswitch settles it.",
            fill=p["muted"],
            size=14.5,
        ),
    ]

    # -- the wire, drawn before the boxes so it runs behind them ----------------
    body += [
        _wire(f"M 316 {BUS_Y:g} L 380 {BUS_Y:g}", p),
        f'<path d="M 316 {BUS_Y:g} L 380 {BUS_Y:g}" fill="none" stroke="{p["line"]}" '
        f'stroke-width="4" stroke-dasharray="2 5"/>',
        _wire(f"M 380 {BUS_Y:g} L 596 {BUS_Y:g}", p, width=2),
        _wire(KVM_ROUTE_MAC, p),
        _wire(KVM_ROUTE_PC, p),
        f'<g class="liveMac" opacity="0.06">{_wire(KVM_ROUTE_MAC, p, colour=p["accent"], width=2.4)}</g>',
        f'<g class="livePC" opacity="0.22">{_wire(KVM_ROUTE_PC, p, colour=p["accent"], width=2.4)}</g>',
        _packets(KVM_WIRE_IN, p, scroll="p", gate="flowIn"),
        _packets(KVM_ROUTE_MAC, p, scroll="p", gate="flowMac"),
        _packets(KVM_ROUTE_PC, p, scroll="p", gate="flowPC"),
        f'<g class="flowWrite" opacity="0">'
        f'<path d="{KVM_WRITE_PATH}" fill="none" stroke="{p["accent"]}" stroke-width="1.6" '
        f'opacity="0.35" marker-end="url(#kvmarrow)"/>'
        + _packets(KVM_WRITE_PATH, p, scroll="pr")
        + "</g>",
        _text(348, 272, "2.4 GHz", fill=p["muted"], size=10.5, family=MONO, anchor="middle"),
        _text(563, 272, "USB", fill=p["muted"], size=10.5, family=MONO, anchor="middle"),
    ]

    # -- keyboard ---------------------------------------------------------------
    body += [
        _text(48, 218, "MX Keys S", fill=p["text"], size=15, weight=700),
        '<g class="legMac" opacity="0">'
        + _text(
            316, 218, "platform 1 · macOS", fill=p["accent"], size=12.5, family=MONO, anchor="end"
        )
        + "</g>",
        '<g class="legWin" opacity="1">'
        + _text(
            316, 218, "platform 0 · Windows", fill=p["accent"], size=12.5, family=MONO, anchor="end"
        )
        + "</g>",
        _mini_keyboard(p),
        f'<rect x="48" y="364" width="268" height="44" rx="12" fill="{p["panel2"]}" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>',
        '<g class="legMac" opacity="0">'
        + _text(
            182,
            393,
            "⌘ cmd     ⌥ opt",
            fill=p["text"],
            size=15,
            family=MONO,
            weight=700,
            anchor="middle",
        )
        + "</g>",
        '<g class="legWin" opacity="1">'
        + _text(160, 393, "alt", fill=p["text"], size=15, family=MONO, weight=700, anchor="end")
        + _icon(WINDOWS_PATH, 172, 381, 15, p["text"])
        + _text(196, 393, "start", fill=p["text"], size=15, family=MONO, weight=700)
        + "</g>",
        _text(
            182,
            428,
            "one keycap, two legends — this is what changes",
            fill=p["muted"],
            size=11.5,
            anchor="middle",
        ),
    ]

    # -- receiver and KVM -------------------------------------------------------
    body += [
        f'<rect x="380" y="258" width="150" height="60" rx="12" fill="{p["panel"]}" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>',
        _text(455, 284, "Bolt receiver", fill=p["text"], size=14, weight=650, anchor="middle"),
        _text(455, 304, "one USB dongle", fill=p["muted"], size=11.5, family=MONO, anchor="middle"),
        f'<rect x="596" y="196" width="150" height="180" rx="12" fill="{p["panel"]}" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>',
        _text(671, 224, "KVM", fill=p["text"], size=15, weight=700, anchor="middle"),
        _text(671, 244, "video + USB", fill=p["muted"], size=11.5, family=MONO, anchor="middle"),
    ]
    # The routing inside the switch is drawn on top of it: which port is live is the
    # only thing the KVM contributes to this story.
    body += [
        f'<g class="liveMac" opacity="0.06">'
        f'<path d="M 610 {BUS_Y:g} C 680 {BUS_Y:g} 690 252 746 252" fill="none" '
        f'stroke="{p["accent"]}" stroke-width="2.4"/></g>',
        f'<g class="livePC" opacity="0.22">'
        f'<path d="M 610 {BUS_Y:g} C 680 {BUS_Y:g} 690 324 746 324" fill="none" '
        f'stroke="{p["accent"]}" stroke-width="2.4"/></g>',
        f'<circle cx="746" cy="252" r="6" fill="{p["bg"]}" stroke="{p["line"]}" stroke-width="1.5"/>',
        f'<circle cx="746" cy="324" r="6" fill="{p["bg"]}" stroke="{p["line"]}" stroke-width="1.5"/>',
        f'<g class="dotMac" opacity="0"><circle cx="746" cy="252" r="6" fill="{p["accent"]}"/></g>',
        f'<g class="dotPC" opacity="1"><circle cx="746" cy="324" r="6" fill="{p["accent"]}"/></g>',
    ]

    # -- the write, labelled ----------------------------------------------------
    body += [
        _chip(
            392,
            148,
            300,
            34,
            p,
            "0x4531 · setHostPlatform → 0",
            cls="writeTag",
        ),
        _chip(
            392,
            148,
            108,
            34,
            p,
            "~1.1 s",
            stroke=p["good"],
            colour=p["good"],
            cls="tookTag",
        ),
    ]

    # -- machines ---------------------------------------------------------------
    def machine(y0, icon, name, cls_panel, lines):
        out = [
            f'<g class="{cls_panel}" opacity="{"0.34" if cls_panel == "kbMac" else "1"}">',
            f'<rect x="856" y="{y0}" width="376" height="138" rx="14" fill="{p["panel"]}" '
            f'stroke="{p["line"]}" stroke-width="1.5"/>',
            _icon(icon, 880, y0 + 22, 22, p["text"]),
            _text(916, y0 + 42, name, fill=p["text"], size=16, weight=700),
            f'<line x1="856" y1="{y0 + 62}" x2="1232" y2="{y0 + 62}" stroke="{p["line"]}"/>',
        ]
        for klass, base, state, result, colour in lines:
            wrap = f'<g class="{klass}" opacity="{base}">' if klass else "<g>"
            out += [
                wrap,
                _text(880, y0 + 88, state, fill=p["muted"], size=12.5, family=MONO),
                _text(880, y0 + 116, result, fill=colour, size=14, family=MONO, weight=650),
                "</g>",
            ]
        out.append("</g>")
        return "".join(out)

    body.append(
        machine(
            140,
            APPLE_PATH,
            "Mac",
            "kbMac",
            [(None, "1", "keyboard mode: macOS ✓", "⌘C copies · @ types @", p["good"])],
        )
    )
    body.append(
        machine(
            330,
            WINDOWS_PATH,
            "Windows PC",
            "kbPC",
            [
                (
                    "pcIdle",
                    "0",
                    "the receiver is on the Mac",
                    "no keystrokes arrive here",
                    p["muted"],
                ),
                (
                    "pcWrong",
                    "0",
                    "keyboard mode: macOS — wrong host",
                    '⌘C → Alt+C · @ types "',
                    p["bad"],
                ),
                ("pcBusy", "0", "device arrived · reading 0x4531", "correcting…", p["accent"]),
                (
                    "pcRight",
                    "1",
                    "keyboard mode: Windows ✓",
                    "Ctrl+C copies · @ types @",
                    p["good"],
                ),
            ],
        )
    )
    # Arrival is the event this whole project hangs on, so it gets a ping.
    body.append(
        f'<g class="agent" opacity="1">'
        f'<circle cx="856" cy="399" r="6" fill="none" stroke="{p["accent"]}" '
        f'stroke-width="2" class="ping"/>'
        f'<rect x="1068" y="358" width="144" height="28" rx="14" fill="{p["panel2"]}" '
        f'stroke="{p["accent"]}" stroke-width="1.5"/>'
        f'<circle cx="1088" cy="372" r="4" fill="{p["good"]}"/>'
        + _text(1102, 377, "logiswitch", fill=p["accent"], size=12.5, family=MONO, weight=600)
        + "</g>"
    )

    # -- the caption that carries the story -------------------------------------
    captions = [
        (
            "1",
            p["accent"],
            "You type on the Mac. The keyboard is in macOS mode.",
            "⌘ copies, ⌥ is Option, and the keyboard reports 0x4531 platform 1.",
        ),
        (
            "2",
            p["accent"],
            "The KVM hands the receiver to the PC.",
            "Nothing unplugs. The keyboard is never told it changed hands — and neither is "
            "Logi Options+, which only reverts a change it observes.",
        ),
        (
            "3",
            p["bad"],
            "So the PC inherits a keyboard still in macOS mode.",
            "⌘ acts as Alt and @ types a quote. Without logiswitch it stays that way until "
            "you hold Fn+P for seven seconds.",
        ),
        (
            "✓",
            p["good"],
            "logiswitch hears the device arrive and writes 0x4531 back down the same wire.",
            "setHostPlatform 0, then read back to prove it took. No remapping — the keyboard "
            "itself changes mode. Measured: ~1.1 s.",
        ),
    ]
    body.append(
        f'<rect x="48" y="486" width="1184" height="66" rx="14" fill="{p["panel2"]}" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>'
    )
    for i, (badge, colour, head, sub) in enumerate(captions):
        body.append(
            f'<g class="act{i}" opacity="{1 if i == 3 else 0}">'
            f'<circle cx="82" cy="519" r="17" fill="{colour}"/>'
            + _text(82, 525, badge, fill=p["bg"], size=15, weight=700, anchor="middle")
            + _text(116, 514, head, fill=p["text"], size=15.5, weight=650)
            + _text(116, 537, sub, fill=p["muted"], size=13)
            + "</g>"
        )

    return _scene(
        w,
        h,
        p,
        "".join(body),
        ident="kvm",
        title="How a KVM leaves a Logitech keyboard on the wrong layout, and how logiswitch fixes it",
        desc=(
            "One MX Keys S talks to a Bolt receiver plugged into a KVM, which feeds a Mac and "
            "a Windows PC. Keystrokes flow to the Mac while the keyboard is in macOS mode. The "
            "KVM hands the receiver to the PC without anything unplugging, so no change event "
            "exists and the keyboard stays in macOS mode: Command acts as Alt and @ types a "
            "quote. logiswitch on the PC sees the device arrive and sends HID++ 0x4531 "
            "setHostPlatform 0 back along the same wire, taking about 1.1 seconds, after which "
            "the keycap's alt and start legends are the live ones and the PC types correctly."
        ),
        css="".join(css),
    )


# -- scene 2: several machines, one keyboard ----------------------------------
#
# The arbitration added in 2.1.0. Timings are staged -- a diagram cannot wait out a
# real 20 s idle window -- but the shape is the algorithm: the bar is time since the
# last keypress on that machine, the tick is --active-window, and crossing it is
# what makes a machine stand down.
ARB_HANDOVER = 8.0  #: typing moves from the Mac to the PC
ARB_CROSS = 10.4  #: the Mac's idle bar passes the threshold and it yields


def arbitration(p: dict) -> str:
    w, h = 1280, 450
    css = [
        f"@keyframes pkt{{to{{stroke-dashoffset:{-(DASH + GAP):g}}}}}",
        ".p{animation:pkt 0.8s linear infinite}",
    ]

    def cls(name, windows, **kw):
        css.append(_kf(name, _windows(windows, **kw)))
        css.append(f".{name}{{animation:{name} {LOOP:g}s linear infinite}}")
        return name

    cls("ownMac", [(0.0, ARB_CROSS)])
    cls("yieldMac", [(ARB_CROSS, LOOP)])
    cls("ownPC", [(ARB_HANDOVER + 0.3, LOOP)])
    cls("yieldPC", [(0.0, ARB_HANDOVER + 0.3)])
    cls("flowMac", [(0.6, ARB_CROSS)])
    cls("flowPC", [(ARB_HANDOVER + 0.6, 17.8)])

    # The bars are the argument, so they animate width and colour together: busy and
    # short, or long and grey. Nothing else in the scene needs to move.
    css.append(
        _kf(
            "barMac",
            [
                (0.0, {"width": "10px", "fill": p["good"]}),
                (2.0, {"width": "24px"}),
                (4.0, {"width": "12px"}),
                (6.0, {"width": "26px"}),
                (ARB_HANDOVER, {"width": "14px", "fill": p["good"]}),
                (ARB_CROSS, {"width": "160px", "fill": p["good"]}),
                (ARB_CROSS + 0.35, {"fill": p["muted"]}),
                (13.0, {"width": "320px", "fill": p["muted"]}),
                (LOOP, {"width": "320px", "fill": p["muted"]}),
            ],
        )
    )
    css.append(
        _kf(
            "barPC",
            [
                (0.0, {"width": "320px", "fill": p["muted"]}),
                (ARB_HANDOVER, {"width": "320px", "fill": p["muted"]}),
                (ARB_HANDOVER + 0.3, {"width": "12px", "fill": p["good"]}),
                (11.0, {"width": "26px"}),
                (14.0, {"width": "13px"}),
                (LOOP, {"width": "22px", "fill": p["good"]}),
            ],
        )
    )
    css.append(".bMac{animation:barMac 18s linear infinite}")
    css.append(".bPC{animation:barPC 18s linear infinite}")

    body = [
        _text(
            48,
            56,
            "Several machines, one keyboard: whoever is typing keeps it",
            fill=p["text"],
            size=21,
            weight=700,
        ),
        _text(
            48,
            84,
            "No configuration, no negotiation — the machines have no channel between them. "
            "Each just asks its own OS how long since anyone typed.",
            fill=p["muted"],
            size=14.5,
        ),
    ]

    #: (x, icon, name, platform written, bar class, owner class, yielder class,
    #: resting opacity of the owner caption, resting opacity of the yielder caption)
    #:
    #: The two resting values are the state at the *end* of the loop, which is what a
    #: still render and `prefers-reduced-motion` both show. By then typing has moved
    #: to the PC: barMac ends full-width and grey, barPC ends short and green. Having
    #: the Mac's caption rest on "typing now" therefore contradicted its own idle bar
    #: in every still frame of this diagram.
    cards = [
        (48, APPLE_PATH, "Mac", "platform 1", "bMac", "ownMac", "yieldMac", 0, 1),
        (456, WINDOWS_PATH, "Windows PC", "platform 0", "bPC", "ownPC", "yieldPC", 1, 0),
        (864, WINDOWS_PATH, "Windows laptop", "platform 0", None, None, None, 0, 1),
    ]
    for x, icon, name, plat, bar, own, yields, own_end, yield_end in cards:
        body += [
            f'<rect x="{x}" y="120" width="368" height="150" rx="14" fill="{p["panel"]}" '
            f'stroke="{p["line"]}" stroke-width="1.5"/>',
            _icon(icon, x + 24, 142, 20, p["text"]),
            _text(x + 58, 162, name, fill=p["text"], size=16, weight=700),
            f'<line x1="{x}" y1="186" x2="{x + 368}" y2="186" stroke="{p["line"]}"/>',
            _text(x + 24, 212, "idle time", fill=p["muted"], size=11.5, family=MONO),
            f'<rect x="{x + 24}" y="224" width="320" height="10" rx="5" fill="{p["line"]}"/>',
        ]
        if bar:
            body.append(
                f'<rect x="{x + 24}" y="224" width="320" height="10" rx="5" '
                f'fill="{p["muted"]}" class="{bar}"/>'
            )
        else:
            body.append(
                f'<rect x="{x + 24}" y="224" width="320" height="10" rx="5" fill="{p["muted"]}"/>'
            )
        # The threshold is --active-window: cross it and this machine stands down.
        body += [
            f'<line x1="{x + 184}" y1="219" x2="{x + 184}" y2="239" stroke="{p["text"]}" '
            f'stroke-width="1.5" opacity="0.55"/>',
            _text(x + 184, 213, "20 s", fill=p["text"], size=10.5, family=MONO, anchor="middle"),
        ]
        owner = _text(
            x + 24, 256, "typing now — it owns the keyboard", fill=p["good"], size=12, family=MONO
        ) + _text(
            x + 344, 162, f"writes {plat}", fill=p["good"], size=12, family=MONO, anchor="end"
        )
        yielder = _text(
            x + 24, 256, "idle past 20 s — standing down", fill=p["muted"], size=12, family=MONO
        ) + _text(x + 344, 162, "not writing", fill=p["muted"], size=12, family=MONO, anchor="end")
        if own:
            body.append(f'<g class="{own}" opacity="{own_end}">{owner}</g>')
            body.append(f'<g class="{yields}" opacity="{yield_end}">{yielder}</g>')
        else:
            body.append(f"<g>{yielder}</g>")

    # Each card drops to the keyboard; only the owner's drop carries anything.
    drops = [
        ("M 232 270 C 232 320 380 376 496 376", "flowMac"),
        ("M 640 270 L 640 344", "flowPC"),
        ("M 1048 270 C 1048 320 900 376 784 376", None),
    ]
    for d, flow_cls in drops:
        body.append(_wire(d, p))
        if flow_cls:
            body.append(_packets(d, p, scroll="p", gate=flow_cls))

    body += [
        f'<rect x="496" y="344" width="288" height="64" rx="12" fill="{p["panel"]}" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>',
        _text(640, 372, "MX Keys S", fill=p["text"], size=15, weight=700, anchor="middle"),
        _text(
            640,
            392,
            "one platform slot",
            fill=p["muted"],
            size=11.5,
            family=MONO,
            anchor="middle",
        ),
        _text(
            640,
            434,
            "Only one machine can be receiving your keystrokes, so each reaches the same "
            "answer on its own — for any number of machines.",
            fill=p["muted"],
            size=12.5,
            anchor="middle",
        ),
    ]

    return _scene(
        w,
        h,
        p,
        "".join(body),
        ident="arb",
        title="How several machines share one keyboard without fighting over it",
        desc=(
            "A Mac, a Windows PC and a Windows laptop each run the agent and each show a bar "
            "of how long since someone typed on them, against a 20 second threshold. The Mac "
            "is being typed on, so its bar stays near zero and it writes the platform; the "
            "other two are past the threshold and stand down. When typing moves to the PC, "
            "the Mac's bar grows past the threshold and it yields, and the PC's bar drops to "
            "zero and it takes over writing the platform to the one MX Keys S they share."
        ),
        css="".join(css),
    )


# -- scene 3: what Options+ does on a KVM, measured -----------------------------
#
# The one asset in this file whose subject is another program, so it is the one that
# has to be scrupulous. Every number below came off a real machine:
#
#   Logi Options+ 2.6.941708 on macOS, timed from the outside: set the platform,
#   stop our own agent so nothing else can correct it, then watch the clock.
#   Its correction is edge-triggered on start-up -- about seven seconds after its
#   own agent starts -- and never happens again. Setting
#   the keyboard to the wrong platform underneath a running Options+ produced no
#   reaction for 45 s, with its window open and with it closed. Restarting its agent
#   corrected the platform every time.
#
# So the claim being drawn is not "Options+ is broken". It is narrower and stronger:
# a KVM switch restarts nothing, and Options+ only acts when it starts.
#
# Unlike the other two scenes this timeline is *static* and only the playhead moves.
# That is deliberate: the argument is the shape of the whole 18 s, so a reader with
# reduced motion on should still get all of it rather than a single frame.

OP_AXIS = (300.0, 1232.0)  #: x range of the time axis
OP_SPAN = OP_AXIS[1] - OP_AXIS[0]
OP_START = 1.0  #: Options+ agent start, and its one and only write
OP_SWITCHES = (4.0, 8.0, 12.0, 16.0)  #: the KVM hands the keyboard to the other machine
OP_FIX = 1.1  #: what logiswitch takes to correct an arrival -- measured, same as KVM_WRITE


def _op_x(t: float) -> float:
    """Seconds on the shared clock to x on the timeline."""
    return OP_AXIS[0] + OP_SPAN * (t / LOOP)


def _op_strip(
    ident: str, y: float, h: float, spans: list[tuple[float, float, str]], p: dict
) -> str:
    """A state strip: one continuous bar whose colour changes over time.

    The segments are square-cornered and the whole strip is clipped to a rounded
    rectangle, so only the two outer ends are round. Rounding each segment instead
    reads as a row of separate pills -- which says "several things" when the point is
    that this is one keyboard, continuously, for eighteen seconds.
    """
    out = [
        f'<clipPath id="{ident}"><rect x="{OP_AXIS[0]:g}" y="{y:g}" width="{OP_SPAN:g}" '
        f'height="{h:g}" rx="{h / 2:g}"/></clipPath>',
        f'<g clip-path="url(#{ident})">',
        f'<rect x="{OP_AXIS[0]:g}" y="{y:g}" width="{OP_SPAN:g}" height="{h:g}" '
        f'fill="{p["panel2"]}"/>',
    ]
    for t0, t1, colour in spans:
        x0, x1 = _op_x(t0), _op_x(t1)
        out.append(
            f'<rect x="{x0:.1f}" y="{y:g}" width="{x1 - x0:.1f}" height="{h:g}" fill="{colour}"/>'
        )
    out.append("</g>")
    return "".join(out)


def optionsplus(p: dict) -> str:
    w, h = 1280, 500
    # The playhead carries its own opacity through the keyframes and rests at 0, so
    # suppressing motion removes it rather than parking it at the left edge. It is
    # the one element here that means nothing standing still: the timeline is the
    # argument and stays completely readable without it.
    css = [
        f"@keyframes opsweep{{from{{transform:translateX(0);opacity:.85}}"
        f"to{{transform:translateX({OP_SPAN:g}px);opacity:.85}}}}",
        f".ophead{{animation:opsweep {LOOP:g}s linear infinite}}",
    ]

    body = [
        _text(
            48,
            56,
            "Logi Options+ enforces the layout once, when its agent starts",
            fill=p["text"],
            size=21,
            weight=700,
        ),
        _text(
            48,
            84,
            "A KVM switch does not restart anything, so nothing re-asserts. "
            "Measured on Options+ 2.6.941708 for macOS, with its own "
            "“always keep the keyboard in Mac layout” switched on throughout.",
            fill=p["muted"],
            size=14.5,
        ),
    ]

    # Each switch drops a faint guide through both strips: without it the reader has
    # to measure across the gap to see that the same instant is red above and red
    # below, which is the entire comparison.
    for t in OP_SWITCHES:
        x = _op_x(t)
        body.append(
            f'<line x1="{x:.1f}" y1="136" x2="{x:.1f}" y2="342" stroke="{p["line"]}" '
            f'stroke-width="1.5" stroke-dasharray="4 5"/>'
        )

    # -- lane 1: Options+ alone --------------------------------------------------
    body += [
        _text(48, 148, "Logi Options+ only", fill=p["text"], size=16, weight=700),
        _text(48, 170, "one write, at start", fill=p["muted"], size=11.5, family=MONO),
        _op_strip(
            "opsA",
            136,
            26,
            [(0.0, OP_SWITCHES[0], p["good"]), (OP_SWITCHES[0], LOOP, p["bad"])],
            p,
        ),
    ]
    # Its single write, and then the silence that is the whole point.
    body.append(
        f'<line x1="{_op_x(OP_START):.1f}" y1="120" x2="{_op_x(OP_START):.1f}" y2="176" '
        f'stroke="{p["accent"]}" stroke-width="2"/>'
    )
    body.append(
        _text(
            _op_x(OP_START),
            112,
            "agent start — setHostPlatform",
            fill=p["accent"],
            size=11.5,
            family=MONO,
            anchor="middle",
        )
    )
    body.append(
        _text(
            (_op_x(OP_SWITCHES[0]) + OP_AXIS[1]) / 2,
            155,
            "wrong layout, and nothing comes to fix it",
            fill=p["bg"],
            size=13,
            weight=700,
            anchor="middle",
        )
    )

    # -- lane 2: the KVM itself --------------------------------------------------
    body.append(_text(48, 246, "KVM switch", fill=p["text"], size=16, weight=700))
    body.append(
        f'<line x1="{OP_AXIS[0]:g}" y1="240" x2="{OP_AXIS[1]:g}" y2="240" '
        f'stroke="{p["line"]}" stroke-width="1.5"/>'
    )
    for i, t in enumerate(OP_SWITCHES, start=1):
        x = _op_x(t)
        body.append(
            f'<line x1="{x:.1f}" y1="222" x2="{x:.1f}" y2="258" stroke="{p["text"]}" '
            f'stroke-width="2.5" opacity="0.75"/>'
        )
        body.append(
            _text(x, 214, f"switch {i}", fill=p["muted"], size=11, family=MONO, anchor="middle")
        )

    # -- lane 3: with logiswitch -------------------------------------------------
    spans: list[tuple[float, float, str]] = []
    edges = [0.0]
    for t in OP_SWITCHES:
        spans.append((edges[-1], t, p["good"]))
        spans.append((t, min(t + OP_FIX, LOOP), p["bad"]))
        edges.append(min(t + OP_FIX, LOOP))
    spans.append((edges[-1], LOOP, p["good"]))
    body += [
        _text(48, 328, "With logiswitch", fill=p["text"], size=16, weight=700),
        _text(48, 350, "corrects every arrival", fill=p["muted"], size=11.5, family=MONO),
        _op_strip("opsB", 316, 26, spans, p),
    ]
    for t in OP_SWITCHES:
        mid = _op_x(t + OP_FIX / 2)
        body.append(
            _text(mid, 308, f"{OP_FIX:.1f} s", fill=p["bad"], size=11, family=MONO, anchor="middle")
        )

    # -- the playhead ------------------------------------------------------------
    # One element crossing both strips: it is what makes the two lanes read as the
    # same eighteen seconds rather than two unrelated pictures.
    body.append(
        f'<g class="ophead" opacity="0"><line x1="{OP_AXIS[0]:g}" y1="118" '
        f'x2="{OP_AXIS[0]:g}" y2="356" stroke="{p["accent"]}" stroke-width="2"/>'
        f'<circle cx="{OP_AXIS[0]:g}" cy="118" r="4.5" fill="{p["accent"]}"/></g>'
    )

    # -- legend and the evidence -------------------------------------------------
    body += [
        f'<rect x="48" y="392" width="16" height="16" rx="4" fill="{p["good"]}"/>',
        _text(72, 405, "layout matches the machine", fill=p["muted"], size=12.5),
        f'<rect x="284" y="392" width="16" height="16" rx="4" fill="{p["bad"]}"/>',
        _text(308, 405, "Cmd and Option swapped", fill=p["muted"], size=12.5),
    ]
    body.append(
        _text(
            48,
            456,
            "Forcing the platform wrong underneath a running Options+ changed nothing for "
            "45 s, window open or closed. Restarting its agent corrected it every time — "
            "which a KVM switch never does.",
            fill=p["muted"],
            size=12.5,
        )
    )

    return _scene(
        w,
        h,
        p,
        "".join(body),
        ident="op",
        title="Why Logi Options+ cannot keep the layout right on a KVM",
        desc=(
            "Two eighteen-second timelines over the same four KVM switches. On the first, "
            "with only Logi Options+ installed, a single setHostPlatform write happens when "
            "its agent starts; from the first KVM switch onwards the strip is red for the "
            "rest of the run, because Options+ only acts at start-up and a KVM switch starts "
            "nothing. On the second, with logiswitch running, each switch turns the strip red "
            "for 1.1 seconds and then green again. Measured on Logi Options+ 2.6.941708 for "
            "macOS, with its own setting to always keep the keyboard in Mac layout switched "
            "on throughout."
        ),
        css="".join(css),
    )


# -- the install, as it actually looks -----------------------------------------
#
# One file rather than a light/dark pair: a terminal is dark in both READMEs, the
# same reason the hero and the social card carry their own background.
#
# Every line below is real output, pasted from a run on the machine this was written
# on -- including the two lines nobody would invent, `taking turns : SUSPENDED` and
# the receiver refusing to open because our own agent already holds it. A mocked-up
# terminal that only ever shows success is the kind of screenshot people have learned
# to distrust.

TERM_PROMPT = "#7ee787"
#: (delay in seconds, kind, indent in characters, text)
TERM_LINES: tuple[tuple[float, str, int, str], ...] = (
    (0.0, "prompt", 0, "pipx install logiswitch && logiswitch install"),
    (0.9, "out", 0, "installed logiswitch 2.3.0"),
    (1.3, "good", 0, "agent running: com.appbuildersgang.logiswitch"),
    (1.9, "out", 0, "watching for device changes via iokit"),
    (2.3, "good", 0, "found MX Keys S on Logi Bolt receiver at index 5 via MULTIPLATFORM 0x4531"),
    (2.7, "good", 0, "MX Keys S already on macos"),
    (3.6, "prompt", 0, "logiswitch doctor"),
    (4.4, "out", 0, "logiswitch 2.3.0 doctor"),
    (4.7, "dim", 0, "target OS : macos"),
    (5.0, "dim", 0, "agent     : installed, running"),
    (5.6, "out", 0, "sharing"),
    (5.9, "dim", 2, "this machine : Abdelraoufs-MacBook-Pro.local"),
    (6.2, "dim", 2, "input        : in use now"),
    (6.5, "warn", 2, "taking turns : SUSPENDED while logioptionsplus_agent is running here"),
    (7.1, "out", 0, "devices"),
    (7.4, "dim", 2, "Logi Bolt receiver  (046D:C548)"),
    (7.7, "dim", 4, "device index 5: MX Keys S (HID++ 4.5)"),
    (8.0, "good", 6, "capability: MULTIPLATFORM 0x4531"),
    (8.6, "good", 0, "Nothing is wrong at this moment."),
)


def terminal() -> str:
    w, h = 1080, 560
    x0, y0, step = 34.0, 96.0, 23.0
    colours = {
        "prompt": CARD["text"],
        "out": CARD["text"],
        "dim": CARD["muted"],
        "good": CARD["good"],
        "warn": "#d29922",
    }
    css = [
        "@keyframes tln{from{opacity:0}to{opacity:1}}",
        ".tl{animation:tln .34s ease-out both}",
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}",
    ]
    body = [
        f'<rect width="{w}" height="{h}" rx="14" fill="#0b0d12"/>',
        f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="13.5" fill="none" '
        f'stroke="{CARD["edge"]}"/>',
        f'<path d="M0 14 a14 14 0 0 1 14 -14 h{w - 28} a14 14 0 0 1 14 14 v30 h-{w} z" '
        f'fill="#151a22"/>',
        '<circle cx="26" cy="22" r="6" fill="#ff5f57"/>',
        '<circle cx="46" cy="22" r="6" fill="#febc2e"/>',
        '<circle cx="66" cy="22" r="6" fill="#28c840"/>',
        _text(
            w / 2,
            27,
            "logiswitch — install and check",
            fill=CARD["muted"],
            size=12.5,
            family=MONO,
            anchor="middle",
        ),
    ]
    for i, (delay, kind, depth, line) in enumerate(TERM_LINES):
        y = y0 + i * step
        # Lines land in order and stay: this is a transcript, not a loop, so the
        # final frame -- which is also the reduced-motion frame -- is the whole run.
        prefix = ""
        if kind == "prompt":
            prefix = _text(x0, y, "$", fill=TERM_PROMPT, size=13.5, family=MONO, weight=700)
        # 8.13 px per character at 13.5 px in this mono stack, so doctor's own
        # indentation survives instead of every line stacking flush left.
        indent = x0 + (20 if kind == "prompt" else 34) + depth * 8.13
        body.append(
            f'<g class="tl" style="animation-delay:{delay:g}s">'
            + prefix
            + _text(indent, y, line, fill=colours[kind], size=13.5, family=MONO)
            + "</g>"
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
        f'height="{h}" role="img" aria-labelledby="termt termd">'
        f'<title id="termt">Installing logiswitch and checking it</title>'
        f'<desc id="termd">A terminal transcript: pipx install logiswitch and logiswitch '
        f"install put the agent in place, it finds an MX Keys S on a Logi Bolt receiver and "
        f"reports it already on macOS; logiswitch doctor then prints the host, the sharing "
        f"state including turn-taking suspended while Logi Options+ is running, the device "
        f"and its 0x4531 capability, and finishes with nothing wrong.</desc>"
        f"<style><![CDATA[{''.join(css)}]]></style>"
        f"{''.join(body)}</svg>"
    )


#: Diagrams that need a light and a dark variant, selected with <picture>.
THEMED = {
    "kvm": kvm,
    "optionsplus": optionsplus,
    "arbitration": arbitration,
    "latency": latency,
    "architecture": architecture,
}
#: Product cards: they carry their own dark background, so one file serves both
#: themes and the 400 KB keyboard vector is embedded once, not twice.
CARDS = {"hero": hero, "social-preview": social, "terminal": terminal}


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
