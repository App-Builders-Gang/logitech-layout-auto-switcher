"""Lifecycle commands: ``install``, ``uninstall``, ``service-status``, ``update``."""

from __future__ import annotations

import argparse
import contextlib
import sys

from .. import service
from ..hidpp import protocol as p


def cmd_install(args: argparse.Namespace) -> int:
    # Resolved through the cli package at call time so tests that monkeypatch
    # ``cli.log_path`` reach this call.
    from . import log_path

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
    from .. import updater

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
