"""Commands that assemble a diagnosis: ``doctor`` and ``bundle``."""

from __future__ import annotations

import argparse
import sys

from .. import bundle
from ..doctor import doctor_report


def cmd_doctor(args: argparse.Namespace) -> int:
    """Everything bearing on "why did the keyboard type the wrong character".

    Deliberately one command with one output: the three causes look identical to
    the person at the keyboard, so a report that covers only the firmware platform
    would keep sending people to fix the wrong thing.
    """
    # Resolved through the cli package at call time so tests that monkeypatch
    # ``cli.doctor_report_path`` reach this call.
    from . import doctor_report_path

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
