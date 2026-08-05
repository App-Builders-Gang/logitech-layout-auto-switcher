"""Scene 1: what a KVM does to the layout, and what logiswitch does about it."""

from __future__ import annotations

from ._primitives import (
    APPLE_PATH,
    DASH,
    GAP,
    LOOP,
    MONO,
    WINDOWS_PATH,
    _chip,
    _icon,
    _kf,
    _mini_keyboard,
    _packets,
    _scene,
    _text,
    _windows,
    _wire,
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
