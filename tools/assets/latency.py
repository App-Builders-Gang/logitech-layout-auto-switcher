"""Recovery-time bars on a log scale."""

from __future__ import annotations

import math

from ._primitives import LOOP, MONO, _kf, _scene, _text


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
