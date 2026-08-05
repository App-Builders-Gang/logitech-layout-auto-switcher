"""Scene 3: what Options+ does on a KVM, measured."""

from __future__ import annotations

from ._primitives import (
    LOOP,
    MONO,
    OP_AXIS,
    OP_SPAN,
    _op_strip,
    _op_x,
    _scene,
    _text,
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


OP_START = 1.0  #: Options+ agent start, and its one and only write
OP_SWITCHES = (4.0, 8.0, 12.0, 16.0)  #: the KVM hands the keyboard to the other machine
OP_FIX = 1.1  #: what logiswitch takes to correct an arrival -- measured, same as KVM_WRITE


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
