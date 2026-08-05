"""Shared SVG primitives and constants for the asset generators.

Hand-written SVG rather than a diagramming tool: the output is small, crisp at any
zoom, diffable in review, and needs no toolchain to rebuild.
"""

from __future__ import annotations

import html
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent.parent / "assets"

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


OP_AXIS = (300.0, 1232.0)  #: x range of the time axis
OP_SPAN = OP_AXIS[1] - OP_AXIS[0]


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
