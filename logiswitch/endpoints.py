"""Opening and walking every connected HID++ endpoint.

Shared by the commands that look at real hardware -- ``status``, ``set``,
``probe`` and ``doctor`` -- so they can never drift apart on what "the devices on
this host" means. Lives outside :mod:`logiswitch.cli` so :mod:`logiswitch.doctor`
can reach it without importing the CLI, which would close an import cycle
(``cli`` imports ``bundle``, which needs the doctor report).
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator

from . import hidpp
from .hidpp import protocol as p


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
