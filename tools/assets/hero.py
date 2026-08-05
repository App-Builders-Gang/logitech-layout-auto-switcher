"""The hero banner: one keycap, two legends, alternating on the shared clock."""

from __future__ import annotations

from ._primitives import (
    APPLE_PATH,
    CARD,
    KB_VIEWBOX,
    LOOP,
    MONO,
    WINDOWS_PATH,
    _card_svg,
    _icon,
    _keyboard,
    _kf,
    _text,
    _windows,
)

#: Which legend the hero shows as live, on the shared clock. Long holds and a slow
#: crossfade: this is a banner someone glances at, not a demo they sit through.
HERO_MAC = (0.0, 7.6)
HERO_WIN = (9.0, 16.6)


#: The dual-legend cluster on the bottom row: opt/start and cmd/alt share a keycap,
#: and which legend is live is exactly what this project switches. Measured off a
#: 1:1 render by scanning for the keycap edges, not eyeballed -- a ring that misses
#: the keys by ten units is obvious to every reader.
KB_DUAL_LEGEND = (274, 743, 208, 64)


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
