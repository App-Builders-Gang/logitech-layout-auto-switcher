"""``logiswitch doctor``: why is the keyboard typing the wrong characters?

Assembles the diagnosis -- firmware platform, the host's own input source, link
health, recent history -- into one report. Separated from the command
(:mod:`logiswitch.cli`) so :mod:`logiswitch.bundle` can put the same report in its
archive: two implementations of "what is wrong with this keyboard" would drift,
and a bundle that disagreed with the command would be worse than no bundle.

That is also why this is its own module rather than a function in ``cli``: the
bundle must be able to import it without importing the CLI (which imports the
bundle), so the alternative -- defining it in ``cli`` -- was an import cycle
papered over with a lazy import.
"""

from __future__ import annotations

import platform
import socket
import sys
from pathlib import Path

from . import __version__, activity, diagnostics, notify, service, trace
from . import agent as agent_module
from .endpoints import _device_lines, _endpoints
from .hidpp import protocol as p
from .platform import default_target_os, log_path, trace_path

#: Frames from the ring included in a report. Enough to cover a check-and-correct
#: pass and whatever preceded it, without burying the verdict.
DOCTOR_TRACE_FRAMES = 60
#: Trailing log lines included, for the same reason.
DOCTOR_LOG_LINES = 40


def _tail(path: Path, lines: int) -> list[str]:
    try:
        content = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return content[-lines:]


def _check_device(device, info, target: str, out: list[str]) -> list[str]:
    """Compare what one device reports against what this host needs."""
    findings: list[str] = []
    try:
        option = device.option_for_os(target)
    except Exception as exc:
        out.append(f"      cannot map {target} onto this device: {exc}")
        return [f"{info.name} advertises no platform for {target}: {exc}"]
    try:
        current = device.current_platform()
    except Exception as exc:
        out.append(f"      current: unavailable ({exc})")
        return [
            f"{info.name} did not answer when asked which platform it is on ({exc}). "
            "It is asleep, on another Easy-Switch channel, or out of range."
        ]
    label = next((o.label for o in info.options if o.index == current), f"platform {current}")
    out.append(f"      current: {label} (platform {current}); wants {option.label}")
    if current != option.index:
        detail = (
            "The key you press as Command sends Option, so Cmd+Shift+D arrives as "
            "Opt+Shift+D and types 'Î'. It reads like a stuck modifier and is not one."
            if target == "macos"
            else "Modifier keys and punctuation will be swapped."
        )
        findings.append(
            f"SYMPTOM 1 -- {info.name} is on {label} but this host is {target}. "
            f"{detail} Run `logiswitch set {target}`, and if it will not stay, set it "
            "on the keyboard itself by holding Fn+O (macOS) or Fn+P (Windows) for "
            "three seconds -- a platform set by the keyboard persists where one set "
            "by software may not."
        )
    return findings


def doctor_report(target_os: str | None = None) -> tuple[str, list[str]]:
    """Build the diagnosis. Returns the report text and what it found wrong."""
    target = p.normalise_os(target_os or default_target_os())
    out: list[str] = []
    findings: list[str] = []

    out.append(f"logiswitch {__version__} doctor")
    out.append(f"host      : {platform.platform()}")
    out.append(f"python    : {sys.version.split()[0]}")
    out.append(f"target OS : {target}")
    state = service.status()
    if state.get("installed"):
        out.append(f"agent     : installed, {state.get('state', 'unknown')}")
    else:
        out.append("agent     : not installed (run `logiswitch install`)")

    # -- host side ------------------------------------------------------------
    host = diagnostics.host_summary()
    out.append("")
    out.append("host keyboard input")
    out.append(f"  input source : {host['input_source']}")
    if host["non_latin_script"]:
        out.append(f"  script       : {host['non_latin_script']} -- NOT Latin")
        findings.append(
            f"SYMPTOM 2 -- the host input source is {host['input_source']}, which types "
            f"{host['non_latin_script']}. logiswitch does not manage this: change it with "
            "Ctrl+Space (macOS) or Alt+Shift / Win+Space (Windows)."
        )
    else:
        out.append("  script       : Latin, or unrecognised")
    out.append(f"  also running : {', '.join(host['competing_software']) or 'nothing known'}")
    out.append(f"  notifications: {notify.backend_name()} (test with `logiswitch notify-test`)")

    # -- sharing this keyboard with other machines ----------------------------
    idle = activity.seconds_since_input()
    out.append("")
    out.append("sharing")
    out.append(f"  this machine : {socket.gethostname()}")
    out.append(f"  input        : {activity.describe(idle)}")
    rivals = host["competing_software"]
    # One line, not two: a rival suspends turn-taking outright, so saying "yes,
    # yields after 20s" above it states the opposite of what will happen.
    if idle is None:
        out.append("  taking turns : NOT possible here (cannot read input activity)")
        findings.append(
            "This platform cannot report input activity, so it cannot automatically "
            "take turns with another machine sharing the keyboard. If one is competing, "
            "run the agent with --observe on whichever machine should yield."
        )
    elif rivals:
        out.append(
            f"  taking turns : SUSPENDED while {', '.join(rivals)} "
            f"{'is' if len(rivals) == 1 else 'are'} running here"
        )
    else:
        out.append(f"  taking turns : yes, yields after {agent_module.ACTIVE_WINDOW:.0f}s idle")
    out.append(
        "  note         : a peer is only visible to a running agent; check the log "
        "for 'another machine is setting this keyboard's platform'"
    )
    if rivals:
        finding = (
            f"{', '.join(rivals)} {'is' if len(rivals) == 1 else 'are'} running and "
            "share this HID++ collection. Expected company rather than a fault, and "
            "their traffic is counted separately as 'other software' rather than "
            "blamed on the receiver."
        )
        if any("logio" in name.lower() for name in rivals):
            finding += (
                " Logi Options+ specifically was measured doing nothing but polling "
                "the receiver's device slots -- hours of traces, not one host platform "
                "write. It only writes to revert a change it disagrees with, so point "
                "it at the same OS and the two never collide."
            )
        findings.append(
            finding + "\n"
            "  One consequence: this machine will not hand the keyboard to another "
            "machine while that software is running, because the protocol reports both "
            "as simply 'host software' and yielding to a program nobody is typing on "
            "would leave the layout wrong. If you do share this keyboard over a KVM and "
            "want turn-taking back, quit it -- or run the agent with --observe on "
            "whichever machine should yield."
        )

    # -- devices --------------------------------------------------------------
    out.append("")
    out.append("devices")
    found_any = False
    refused: list[str] = []
    with _endpoints(refused=refused) as opened:
        for entry in refused:
            out.append(f"  REFUSED TO OPEN  {entry}")
        if refused:
            # Enumerated but unopenable is a permission problem, not a missing
            # receiver, and saying the latter sends people looking for a hardware
            # fault that does not exist.
            # Our own agent holding the device is the ordinary case, not a fault.
            agent_running = (
                bool(state.get("installed")) and "run" in str(state.get("state", "")).lower()
            )
            findings.append(
                diagnostics.cannot_open_hint(agent_running) or "an endpoint would not open"
            )
        if not opened and not refused:
            out.append("  no Logitech HID++ endpoint found")
            findings.append(
                "No receiver or device answered at all. If a KVM is in the path, switch it "
                "to this machine; otherwise the keyboard is on another Easy-Switch channel."
            )
        for group, _transport, devices in opened:
            out.append(f"  {group.label}  ({group.vendor_id:04X}:{group.product_id:04X})")
            out.extend(_device_lines(devices, indent="    "))
            for device, info in devices:
                if not info.supported:
                    continue
                found_any = True
                findings.extend(_check_device(device, info, target, out))

        if opened and not found_any:
            out.append("  no device here can switch layout")
            findings.append(
                "A receiver is present but nothing behind it answered as a keyboard that "
                "can switch layout. The keyboard is asleep, on another Easy-Switch "
                "channel, or out of range -- and while that is true logiswitch cannot "
                "correct anything."
            )

    # -- link health ----------------------------------------------------------
    out.append("")
    out.append("link health (this process only -- the agent keeps its own counters)")
    out.append(f"  {trace.HEALTH.summary()}")
    if trace.HEALTH.get("orphans"):
        findings.append(
            f"{trace.HEALTH.get('orphans')} replies arrived with nothing waiting for them. "
            "The device is answering more slowly than the request deadline."
        )

    # -- history --------------------------------------------------------------
    for label, path, lines in (
        ("agent log", log_path(), DOCTOR_LOG_LINES),
        ("frame trace", trace_path(), DOCTOR_TRACE_FRAMES),
    ):
        tail = _tail(path, lines)
        out.append("")
        out.append(f"{label}: {path}")
        if tail:
            out.extend(f"  {line}" for line in tail)
        else:
            out.append("  (empty or missing)")

    out.append("")
    out.append("frames seen by this command")
    out.extend(f"  {line}" for line in trace.render(DOCTOR_TRACE_FRAMES).splitlines())

    # -- verdict --------------------------------------------------------------
    out.append("")
    out.append("verdict")
    if findings:
        for number, finding in enumerate(findings, 1):
            out.append(f"  {number}. {finding}")
    else:
        out.append("  Nothing is wrong at this moment.")
        out.append("  The fault is intermittent, so a snapshot taken now may simply have")
        out.append("  missed it. Leave `logiswitch watch -v --trace` running, and re-run")
        out.append("  this command the moment the wrong characters appear.")

    return "\n".join(out), findings
