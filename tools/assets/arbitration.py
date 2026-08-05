"""Scene 2: several machines, one keyboard."""

from __future__ import annotations

from ._primitives import (
    APPLE_PATH,
    DASH,
    GAP,
    LOOP,
    MONO,
    WINDOWS_PATH,
    _icon,
    _kf,
    _packets,
    _scene,
    _text,
    _windows,
    _wire,
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
