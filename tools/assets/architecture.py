"""Thread-parking architecture diagram."""

from __future__ import annotations

from ._primitives import (
    DASH,
    GAP,
    LOOP,
    MONO,
    _arrow,
    _box,
    _kf,
    _packets,
    _scene,
    _text,
    _windows,
)


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
