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
import logging
import sys
from pathlib import Path

from .. import __version__, bundle, hidpp, notify, service, trace
from ..agent import Agent, AgentConfig
from ..doctor import doctor_report
from ..endpoints import _device_lines, _endpoints, _require_endpoints
from ..hidpp import protocol as p
from ..platform import (
    default_target_os,
    doctor_report_path,
    is_managed,
    log_path,
    setup_logging,
    state_path,
    trace_path,
)
from ._devices import cmd_probe, cmd_set, cmd_status
from ._diagnose import cmd_bundle, cmd_doctor
from ._lifecycle import cmd_install, cmd_service_status, cmd_uninstall, cmd_update
from ._run import cmd_notify_test, cmd_watch

#: Names re-exported so tests can keep patching ``cli.<name>`` after the CLI was
#: split into a package. Everything below is part of the public CLI surface; the
#: command handlers themselves are wired into ``build_parser`` and are private.
__all__ = [
    "GLOBAL_FLAG_DEFAULTS",
    "_device_lines",
    "_endpoints",
    "_require_endpoints",
    "Agent",
    "build_parser",
    "bundle",
    "default_target_os",
    "doctor_report",
    "doctor_report_path",
    "hidpp",
    "is_managed",
    "log",
    "log_path",
    "main",
    "notify",
    "service",
    "setup_logging",
    "state_path",
    "trace",
    "trace_path",
]

log = logging.getLogger("logiswitch")

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
