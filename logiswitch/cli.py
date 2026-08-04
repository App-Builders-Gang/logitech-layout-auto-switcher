"""logiswitch command line.

logiswitch status              what each attached device is currently set to
logiswitch set mac|win|...     switch every supported device once
logiswitch watch               run the agent in the foreground
logiswitch install             start the agent at logon
logiswitch uninstall           remove it
logiswitch update              bring this installation up to the latest release
logiswitch update --check      report whether an update is available
logiswitch probe               full HID++ dump, for bug reports
logiswitch doctor              why is the keyboard typing the wrong characters?
logiswitch bundle              pack the logs and device dump into one file
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import platform
import signal
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

from . import __version__, activity, bundle, diagnostics, hidpp, notify, service, trace
from . import agent as agent_module
from .agent import Agent, AgentConfig
from .hidpp import protocol as p
from .paths import (
    default_target_os,
    doctor_report_path,
    is_managed,
    log_path,
    setup_logging,
    state_path,
    trace_path,
)

log = logging.getLogger("logiswitch")


@contextlib.contextmanager
def _endpoints(
    vendor_id: int = p.LOGITECH_VID, refused: list[str] | None = None
) -> Iterator[list[tuple]]:
    """Open every Logitech HID++ endpoint, probe it, and always close cleanly.

    `refused` collects endpoints that enumerated but would not open. That is a
    different condition from "nothing is plugged in" and callers that diagnose --
    ``doctor`` -- need to tell them apart.
    """
    groups = hidpp.find_groups(vendor_id)
    opened: list[tuple] = []
    transports = []
    try:
        for group in groups:
            try:
                transport = hidpp.open_transport(group)
            except Exception as exc:
                if refused is None:
                    # Nobody is collecting these, so say it here or not at all.
                    # When somebody is -- `doctor` -- it reports the same endpoint
                    # properly and explains why, and a bare "open failed" printed
                    # above its own report just reads as an unexplained error.
                    print(f"cannot open {group}: {exc}", file=sys.stderr)
                else:
                    refused.append(f"{group.label}: {exc}")
                continue
            transports.append(transport)
            devices = hidpp.discover_devices(transport)
            opened.append((group, transport, hidpp.probe_devices(devices)))
        yield opened
    finally:
        for transport in transports:
            with contextlib.suppress(Exception):
                transport.close()


def _require_endpoints(opened: list[tuple]) -> None:
    if not opened:
        raise SystemExit(
            "no Logitech HID++ receiver or device found on this host.\n"
            "If you use a KVM, switch it to this machine first."
        )


def cmd_status(_args: argparse.Namespace) -> int:
    with _endpoints() as opened:
        _require_endpoints(opened)
        found_any = False
        for group, _transport, devices in opened:
            print(f"{group.label}  ({group.vendor_id:04X}:{group.product_id:04X})")
            if not devices:
                print("    no devices answered")
            for device, info in devices:
                marker = "*" if info.supported else " "
                print(
                    f"  {marker} [{device.index}] {info.name}  HID++ {info.protocol[0]}.{info.protocol[1]}"
                )
                if not info.supported:
                    print("      cannot switch layout (no 0x4531 / 0x4530)")
                    continue
                found_any = True
                print(f"      via {info.kind}")
                for option in info.options:
                    print(f"        platform {option.index}: {option.label}")
                try:
                    current = device.current_platform()
                except Exception as exc:
                    print(f"      current: unavailable ({exc})")
                    continue
                label = next(
                    (o.label for o in info.options if o.index == current), f"platform {current}"
                )
                print(f"      current: {label}")
                if info.feature == p.FEATURE_MULTIPLATFORM:
                    with contextlib.suppress(Exception):
                        detail = device.host_platform_detail()
                        host = detail["host_index"]
                        channel = host + 1 if host is not None else "?"
                        print(
                            f"      host: Easy-Switch channel {channel}, "
                            f"set by {detail['source_name']}"
                        )
        if not found_any:
            print("\nNothing here can switch layout.", file=sys.stderr)
            return 1
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    target = p.normalise_os(args.os)
    changed = 0
    total = 0
    with _endpoints() as opened:
        _require_endpoints(opened)
        for _group, _transport, devices in opened:
            for device, info in devices:
                if not info.supported:
                    continue
                total += 1
                try:
                    result = device.ensure_os(target)
                except Exception as exc:
                    print(f"{info.name}: failed ({exc})", file=sys.stderr)
                    continue
                changed += int(result.changed)
                option = result.option
                if result.changed and result.confirmed is False:
                    print(
                        f"{info.name}: accepted the switch to {option.label} but still "
                        f"reads something else -- the write did not take",
                        file=sys.stderr,
                    )
                    continue
                verb = "switched to" if result.changed else "already on"
                print(f"{info.name}: {verb} {option.label} (platform {option.index})")
    if not total:
        print("no device supports layout switching", file=sys.stderr)
        return 1
    return 0


def _device_lines(devices: list[tuple], indent: str = "  ") -> list[str]:
    """The per-device dump shared by ``probe`` and ``doctor``.

    One implementation so the two can never drift into disagreeing about what the
    hardware said -- which, when the whole point is diagnosing a device that lies
    about its state, would be its own bug.
    """
    lines: list[str] = []
    for device, info in devices:
        lines.append(
            f"{indent}device index {device.index}: {info.name} "
            f"(HID++ {info.protocol[0]}.{info.protocol[1]})"
        )
        lines.append(f"{indent}  capability: {info.kind}")
        for option in info.options:
            lines.append(
                f"{indent}  platform {option.index}: mask 0x{option.os_mask:04X} -> {option.label}"
            )
        if info.feature != p.FEATURE_MULTIPLATFORM:
            continue
        for host in (p.HOST_CURRENT, 0, 1, 2):
            try:
                record = device.host_platform_detail(host)
                lines.append(
                    f"{indent}  getHostPlatform(0x{host:02X}): "
                    f"{p.describe_host_platform(record)} raw={record['raw']}"
                )
            except Exception as exc:
                lines.append(f"{indent}  getHostPlatform(0x{host:02X}): <{exc}>")
    return lines


def cmd_probe(_args: argparse.Namespace) -> int:
    print(f"logiswitch {__version__}")
    interfaces = hidpp.find_interfaces()
    print(f"\nHID++ vendor collections: {len(interfaces)}")
    for info in interfaces:
        print(
            f"  {info['vendor_id']:04X}:{info['product_id']:04X} "
            f"usage_page=0x{info['usage_page']:04X} usage=0x{info['usage']:04X} "
            f"iface={info.get('interface_number')} product={info.get('product_string')!r}"
        )
        print(f"    path={info['path']!r}")
    if not interfaces:
        return 1

    with _endpoints() as opened:
        for group, _transport, devices in opened:
            print(f"\n=== {group} ===")
            for line in _device_lines(devices):
                print(line)
    return 0


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


def doctor_report(target_os: str | None = None) -> tuple[str, list[str]]:
    """Build the diagnosis. Returns the report text and what it found wrong.

    Separated from :func:`cmd_doctor` so ``bundle`` can put the same report in its
    archive. Two implementations of "what is wrong with this keyboard" would drift,
    and a bundle that disagreed with the command would be worse than no bundle.
    """
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Everything bearing on "why did the keyboard type the wrong character".

    Deliberately one command with one output: the three causes look identical to
    the person at the keyboard, so a report that covers only the firmware platform
    would keep sending people to fix the wrong thing.
    """
    report, findings = doctor_report(args.os)
    print(report)
    destination = doctor_report_path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(report + "\n", "utf-8")
        print(f"\nwritten to {destination}")
    except OSError as exc:
        print(f"\ncould not write {destination}: {exc}", file=sys.stderr)
    return 1 if findings else 0


def cmd_bundle(args: argparse.Namespace) -> int:
    """Pack everything a diagnosis needs into one file."""
    try:
        archive = bundle.build(args.output, target_os=args.os)
    except OSError as exc:
        print(f"could not write the bundle: {exc}", file=sys.stderr)
        return 1
    size = archive.stat().st_size
    print(f"\nwrote {archive}  ({size / 1024:.0f} KiB)")
    print("Send this one file. It contains the logs, the frame trace, the device")
    print("dump and this machine's name -- and no credentials or keystrokes.")
    return 0


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


def cmd_watch(args: argparse.Namespace) -> int:
    config = AgentConfig(
        target_os=p.normalise_os(args.os or default_target_os()),
        reassert_interval=args.reassert,
        force_polling=args.polling,
        state_file=state_path(),
        notify=args.notify,
        observe=args.observe,
        active_window=args.active_window,
        claim_host=args.claim_host,
    )
    agent = Agent(config)

    if args.once:
        return 0 if agent.assert_once() else 1

    def handle_signal(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        agent.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handle_signal)

    agent.run_forever()
    return 0


def cmd_notify_test(_args: argparse.Namespace) -> int:
    """Prove a notification can actually reach the desktop.

    Worth its own command because the failure is silent: on macOS an ``osascript``
    notification is attributed to Script Editor, and if the user has not allowed
    that, nothing appears and nothing errors. Waiting for a real layout change to
    discover this is a poor way to find out.
    """
    notifier = notify.Notifier()
    print(f"backend: {notify.backend_name()}")
    if not notifier.enabled:
        print("no notification backend on this platform", file=sys.stderr)
        return 1
    note = notify.Notification(
        "test", "If you can see this, notifications are working.", notify.APP_TITLE
    )
    if notifier.deliver(note):
        print("sent -- if no notification appeared, it is being blocked:")
        print("  macOS:   System Settings > Notifications > Script Editor")
        print("  Windows: Settings > System > Notifications")
        return 0
    print("the notification command failed; re-run with -v for the reason", file=sys.stderr)
    return 1


def cmd_install(args: argparse.Namespace) -> int:
    target = p.normalise_os(args.os) if args.os else None
    try:
        what = service.install(target)
        on_path = service.ensure_on_path()
        what = service.install(target, notify=args.notify, observe=args.observe)
    except service.ServiceError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    print(f"installed {what}")
    if on_path:
        print(f"added 'logiswitch' to PATH ({service.path_hint()})")
    print(f"log: {log_path()}")
    state = service.status()
    if state.get("installed"):
        print(f"state: {state.get('state', 'unknown')}")
    return 0


def cmd_uninstall(_args: argparse.Namespace) -> int:
    try:
        removed = service.uninstall()
    except service.ServiceError as exc:
        print(f"uninstall failed: {exc}", file=sys.stderr)
        return 1
    if removed:
        print("removed: " + ", ".join(removed))
    else:
        print("nothing was installed")
    return 0


def cmd_service_status(_args: argparse.Namespace) -> int:
    state = service.status()
    if not state.get("installed"):
        print("not installed")
        return 1
    print(f"installed, state: {state.get('state', 'unknown')}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from . import updater

    if args.check:
        available, release = updater.check()
        if release is None:
            print(
                "could not determine the latest release (is the network reachable?)",
                file=sys.stderr,
            )
            return 1
        if available:
            print(f"update available: {updater.installed_version()} -> {release.version}")
            print(f"  {release.wheel_url}")
            return 0
        print(f"already on the latest release ({updater.installed_version()})")
        return 0

    if not updater.is_managed_environment():
        print(
            "this command is running outside the installed venv (a development "
            "checkout), so a self-update would overwrite an editable install. "
            "Re-run it as the installed entry point, or pull latest with git.",
            file=sys.stderr,
        )
        return 1

    try:
        new_version = updater.upgrade(force=args.force)
    except updater.UpdateError as exc:
        # The service, if any, may have been stopped before the failure; bring it
        # back so a botched update does not leave the machine unattended.
        with contextlib.suppress(Exception):
            service.start()
        print(f"update failed: {exc}", file=sys.stderr)
        return 1
    print(f"logiswitch is now {new_version}")
    return 0


#: Flags accepted both before and after the subcommand, with their fallbacks.
#:
#: They default to SUPPRESS rather than to these values, because `common` is a
#: parent of the top-level parser *and* of every subparser: argparse copies a
#: subparser's defaults over the namespace the top-level parse already filled in,
#: so an ordinary default silently discards `logiswitch -v status`. Suppressed
#: defaults leave the attribute unset unless the flag was actually given, and
#: :func:`main` fills in the rest.
GLOBAL_FLAG_DEFAULTS = {"verbose": False, "trace": False, "log_file": None}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging"
    )
    common.add_argument(
        "--log-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="also write a rotating log here",
    )
    common.add_argument(
        "--trace",
        action="store_true",
        default=argparse.SUPPRESS,
        help="log every HID++ frame, and dump the recent ones whenever something "
        "looks wrong; use this when chasing intermittent wrong characters",
    )

    parser = argparse.ArgumentParser(prog="logiswitch", description=__doc__, parents=[common])
    parser.add_argument("--version", action="version", version=f"logiswitch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show what each device is set to", parents=[common]).set_defaults(
        func=cmd_status
    )
    sub.add_parser("probe", help="full HID++ dump for bug reports", parents=[common]).set_defaults(
        func=cmd_probe
    )

    p_doctor = sub.add_parser(
        "doctor",
        help="diagnose wrong characters: layout, host input source and link health",
        parents=[common],
    )
    p_doctor.add_argument("--os", default=None, help="target OS (default: this host's)")
    p_doctor.set_defaults(func=cmd_doctor)

    p_set = sub.add_parser("set", help="switch every supported device once", parents=[common])
    p_set.add_argument("os", choices=sorted(p.OS_ALIASES), metavar="OS")
    p_set.set_defaults(func=cmd_set)

    p_watch = sub.add_parser("watch", help="run the agent", parents=[common])
    p_watch.add_argument("--os", default=None, help="target OS (default: this host's)")
    p_watch.add_argument(
        "--reassert",
        type=float,
        default=AgentConfig.reassert_interval,
        help="how often to re-check the devices, in seconds; this is what catches a "
        "keyboard returning on hardware that announces nothing. 0 disables it "
        f"(default: {AgentConfig.reassert_interval:.0f})",
    )
    p_watch.add_argument("--once", action="store_true", help="apply once and exit")
    p_watch.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show a desktop notification when the layout changes, and when it will "
        "not stay changed (throttled: repeats are coalesced)",
    )
    p_watch.add_argument(
        "--polling", action="store_true", help="force the polling watcher (diagnostics)"
    )
    p_watch.add_argument(
        "--observe",
        action="store_true",
        help="never change the layout, only watch and report. Use this on a machine "
        "that should always let another one have the keyboard",
    )
    p_watch.add_argument(
        "--active-window",
        type=float,
        default=AgentConfig.active_window,
        metavar="SECONDS",
        help="when another machine is competing for the keyboard, give it up after "
        f"this long without input here (default: {AgentConfig.active_window:.0f})",
    )
    p_watch.add_argument(
        "--claim-host",
        type=int,
        default=None,
        metavar="N",
        help="only ever set Easy-Switch host N. Use this when every machine has its "
        "own receiver, so each owns a different host slot",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_install = sub.add_parser("install", help="start the agent at logon", parents=[common])
    p_install.add_argument("--os", default=None, help="pin the target OS instead of auto-detecting")
    p_install.add_argument(
        "--observe",
        action="store_true",
        help="install the agent in observe-only mode (never changes the layout)",
    )
    p_install.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="let the background agent show desktop notifications (default: yes)",
    )
    p_install.set_defaults(func=cmd_install)

    p_bundle = sub.add_parser(
        "bundle",
        help="pack the logs, trace and device dump into one file for a bug report",
        parents=[common],
    )
    p_bundle.add_argument("-o", "--output", type=Path, default=None, metavar="PATH")
    p_bundle.add_argument("--os", default=None, help="target OS (default: this host's)")
    p_bundle.set_defaults(func=cmd_bundle)

    sub.add_parser(
        "notify-test",
        help="send one test notification, to check it is permitted",
        parents=[common],
    ).set_defaults(func=cmd_notify_test)

    sub.add_parser("uninstall", help="remove the logon agent", parents=[common]).set_defaults(
        func=cmd_uninstall
    )
    sub.add_parser(
        "service-status", help="is the logon agent installed and running?", parents=[common]
    ).set_defaults(func=cmd_service_status)

    p_update = sub.add_parser(
        "update", help="update this installation to the latest release", parents=[common]
    )
    p_update.add_argument(
        "--check",
        action="store_true",
        help="only report whether an update is available; do not change anything",
    )
    p_update.add_argument(
        "--force", action="store_true", help="reinstall even if already on the latest version"
    )
    p_update.set_defaults(func=cmd_update)
    # Conventional alias so muscle memory from other tools works.
    sub.add_parser("selfupdate", help="alias of update", parents=[common]).set_defaults(
        func=cmd_update, check=False, force=False
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name, fallback in GLOBAL_FLAG_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, fallback)
    file_log = args.log_file
    if file_log is None and args.command == "watch" and not args.once:
        file_log = log_path()
    # Under launchd our stderr is redirected to a file already; a console handler on
    # top of the file handler would log everything twice.
    setup_logging(args.verbose or args.trace, file_log, console=not is_managed())
    if args.trace:
        trace.set_echo(True)
    if args.trace or args.command in ("watch", "doctor"):
        # Only the long-running agent and an explicit diagnosis should leave files
        # behind; a one-shot `status` has no business writing to the log directory.
        trace.set_dump_path(trace_path())
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        if args.verbose:
            log.exception("unhandled error")
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
