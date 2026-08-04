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
import signal
import sys
from pathlib import Path

from . import __version__, bundle, hidpp, notify, service, trace
from .agent import Agent, AgentConfig
from .doctor import doctor_report
from .endpoints import _device_lines, _endpoints, _require_endpoints
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
