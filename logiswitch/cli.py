"""logiswitch command line.

logiswitch status              what each attached device is currently set to
logiswitch set mac|win|...     switch every supported device once
logiswitch watch               run the agent in the foreground
logiswitch install             start the agent at logon
logiswitch uninstall           remove it
logiswitch update              bring this installation up to the latest release
logiswitch update --check      report whether an update is available
logiswitch probe               full HID++ dump, for bug reports
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import sys
from collections.abc import Iterator
from pathlib import Path

from . import __version__, hidpp, service
from .agent import Agent, AgentConfig
from .hidpp import protocol as p
from .paths import (
    default_target_os,
    is_managed,
    log_path,
    setup_logging,
    state_path,
)

log = logging.getLogger("logiswitch")


@contextlib.contextmanager
def _endpoints(vendor_id: int = p.LOGITECH_VID) -> Iterator[list[tuple]]:
    """Open every Logitech HID++ endpoint, probe it, and always close cleanly."""
    groups = hidpp.find_groups(vendor_id)
    opened: list[tuple] = []
    transports = []
    try:
        for group in groups:
            try:
                transport = hidpp.open_transport(group)
            except Exception as exc:
                print(f"cannot open {group}: {exc}", file=sys.stderr)
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
                        channel = detail["host_index"] + 1
                        source = {1: "keyboard", 2: "auto", 3: "host software"}.get(
                            detail["platform_source"], str(detail["platform_source"])
                        )
                        print(f"      host: Easy-Switch channel {channel}, set by {source}")
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
                    did_change, option = device.ensure_os(target)
                except Exception as exc:
                    print(f"{info.name}: failed ({exc})", file=sys.stderr)
                    continue
                changed += int(did_change)
                verb = "switched to" if did_change else "already on"
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
            for device, info in devices:
                print(
                    f"\n  device index {device.index}: {info.name} (HID++ {info.protocol[0]}.{info.protocol[1]})"
                )
                print(f"    capability: {info.kind}")
                for option in info.options:
                    print(
                        f"      platform {option.index}: mask 0x{option.os_mask:04X} -> {option.label}"
                    )
                if info.feature != p.FEATURE_MULTIPLATFORM:
                    continue
                for host in (p.HOST_CURRENT, 0, 1, 2):
                    try:
                        print(
                            f"      getHostPlatform(0x{host:02X}): {device.host_platform_detail(host)}"
                        )
                    except Exception as exc:
                        print(f"      getHostPlatform(0x{host:02X}): <{exc}>")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    config = AgentConfig(
        target_os=p.normalise_os(args.os or default_target_os()),
        reassert_interval=args.reassert,
        force_polling=args.polling,
        state_file=state_path(),
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


def cmd_install(args: argparse.Namespace) -> int:
    target = p.normalise_os(args.os) if args.os else None
    try:
        what = service.install(target)
    except service.ServiceError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    print(f"installed {what}")
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


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    common.add_argument(
        "--log-file", type=Path, default=None, help="also write a rotating log here"
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
        "--polling", action="store_true", help="force the polling watcher (diagnostics)"
    )
    p_watch.set_defaults(func=cmd_watch)

    p_install = sub.add_parser("install", help="start the agent at logon", parents=[common])
    p_install.add_argument("--os", default=None, help="pin the target OS instead of auto-detecting")
    p_install.set_defaults(func=cmd_install)

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
    file_log = args.log_file
    if file_log is None and args.command == "watch" and not args.once:
        file_log = log_path()
    # Under launchd our stderr is redirected to a file already; a console handler on
    # top of the file handler would log everything twice.
    setup_logging(args.verbose, file_log, console=not is_managed())
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
