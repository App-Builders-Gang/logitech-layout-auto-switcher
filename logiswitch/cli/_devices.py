"""Commands that inspect attached devices: ``status``, ``set``, ``probe``."""

from __future__ import annotations

import argparse
import contextlib
import sys

from .. import __version__, hidpp
from ..endpoints import _device_lines, _endpoints, _require_endpoints
from ..hidpp import protocol as p


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
