"""Foreground-run commands: ``watch`` (the agent) and ``notify-test``."""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import sys

from .. import notify
from ..agent import Agent, AgentConfig
from ..hidpp import protocol as p
from ..platform import default_target_os

log = logging.getLogger("logiswitch")


def cmd_watch(args: argparse.Namespace) -> int:
    # Resolved through the cli package at call time so tests that monkeypatch
    # ``cli.state_path`` reach this call.
    from . import state_path

    config = AgentConfig(
        target_os=p.normalise_os(args.os or default_target_os()),
        reassert_interval=args.reassert,
        force_polling=args.polling,
        state_file=state_path(),
        notify=args.notify,
        observe=args.observe,
        active_window=args.active_window,
        claim_host=args.claim_host,
        event_only=args.event_only,
        event_only_reassert=args.event_only_reassert,
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
