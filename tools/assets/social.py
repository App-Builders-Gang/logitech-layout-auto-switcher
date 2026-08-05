"""The social-preview card."""

from __future__ import annotations

from ._primitives import (
    APPLE_PATH,
    CARD,
    MONO,
    WINDOWS_PATH,
    _card_svg,
    _icon,
    _keyboard,
    _text,
)


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
