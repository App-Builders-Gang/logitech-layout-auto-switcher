"""The install transcript as it actually looks."""

from __future__ import annotations

from ._primitives import CARD, MONO, _text

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
    (
        0.0,
        "prompt",
        0,
        "curl -fsSL https://raw.githubusercontent.com/App-Builders-Gang/"
        "logitech-layout-auto-switcher/main/install.sh | bash",
    ),
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
        f'<desc id="termd">A terminal transcript: the one-line install script '
        f"puts the agent in place, it finds an MX Keys S on a Logi Bolt receiver and "
        f"reports it already on macOS; logiswitch doctor then prints the host, the sharing "
        f"state including turn-taking suspended while Logi Options+ is running, the device "
        f"and its 0x4531 capability, and finishes with nothing wrong.</desc>"
        f"<style><![CDATA[{''.join(css)}]]></style>"
        f"{''.join(body)}</svg>"
    )
