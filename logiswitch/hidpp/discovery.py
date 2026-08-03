"""Finding Logitech HID++ endpoints and the devices behind them.

Nothing here is model-specific. Any Logitech interface exposing the HID++ vendor
collection is a candidate -- Bolt, Unifying, Nano and Lightspeed receivers, and
devices connected straight over USB cable or Bluetooth. Capability is then asked
of the device itself rather than looked up in a table, so a keyboard released
tomorrow works without a code change.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from . import backend
from . import protocol as p
from .device import DeviceInfo, HidppDevice
from .transport import Transport

log = logging.getLogger(__name__)

#: Slots to probe: every receiver slot, plus the direct-connection index used by
#: Bluetooth and cable-attached devices.
SCAN_INDICES = tuple(p.RECEIVER_SLOTS) + (p.INDEX_DIRECT,)


@dataclass
class InterfaceGroup:
    """The short and long HID++ collections belonging to one physical endpoint."""

    vendor_id: int
    product_id: int
    serial: str
    interface_number: int
    product_string: str = ""
    paths: list[tuple[int, bytes]] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        return (self.vendor_id, self.product_id, self.serial, self.interface_number)

    @property
    def label(self) -> str:
        known = p.KNOWN_RECEIVERS.get(self.product_id)
        if known:
            return known
        return self.product_string or f"{self.vendor_id:04X}:{self.product_id:04X}"

    def __str__(self) -> str:  # pragma: no cover - logging
        return f"{self.label} ({self.vendor_id:04X}:{self.product_id:04X})"


def find_interfaces(vendor_id: int = p.LOGITECH_VID) -> list[dict]:
    """Every HID interface on `vendor_id` exposing the HID++ vendor collection."""
    return [
        info
        for info in backend.enumerate_devices(vendor_id)
        if info.get("usage_page") == p.HIDPP_USAGE_PAGE
        and info.get("usage") in (p.USAGE_SHORT, p.USAGE_LONG)
    ]


def group_interfaces(interfaces: list[dict]) -> list[InterfaceGroup]:
    """Collapse the per-collection entries into one group per endpoint.

    Windows reports the short and long collections as two entries sharing an
    interface number; macOS may report a single entry covering both report ids.
    Grouping on (vid, pid, serial, interface) holds in both cases.
    """
    groups: dict[tuple, InterfaceGroup] = {}
    for info in interfaces:
        group = InterfaceGroup(
            vendor_id=info.get("vendor_id", 0),
            product_id=info.get("product_id", 0),
            serial=info.get("serial_number") or "",
            interface_number=info.get("interface_number", -1),
            product_string=info.get("product_string") or "",
        )
        group = groups.setdefault(group.key, group)
        usage = info["usage"]
        if any(existing_usage == usage for existing_usage, _ in group.paths):
            continue  # duplicate collection; first one wins
        group.paths.append((usage, info["path"]))

    result = []
    for group in groups.values():
        if not group.paths:
            continue
        usages = {usage for usage, _ in group.paths}
        # A single interface can back both report ids; alias the missing usage to
        # it so the transport never has to special-case the platform.
        if p.USAGE_LONG not in usages:
            group.paths.append((p.USAGE_LONG, group.paths[0][1]))
        if p.USAGE_SHORT not in usages:
            group.paths.append((p.USAGE_SHORT, group.paths[0][1]))
        group.paths.sort(key=lambda item: item[0])
        result.append(group)
    return result


def find_groups(vendor_id: int = p.LOGITECH_VID) -> list[InterfaceGroup]:
    return group_interfaces(find_interfaces(vendor_id))


def open_transport(group: InterfaceGroup) -> Transport:
    return Transport(group.paths, label=group.label).open()


def discover_devices(
    transport: Transport,
    hint: int | Sequence[int] | None = None,
    indices: tuple[int, ...] = SCAN_INDICES,
) -> list[HidppDevice]:
    """Return every HID++ 2.0 device reachable through `transport`.

    `hint` is the device indices seen last time -- one, or several. Pinging them
    first turns the common reconnect into a couple of sub-second pings instead of a
    full scan; if none answer it falls through to the fan-out.

    Every hinted index is tried, not just the first that answers. Returning early
    with a single device silently dropped every other device on the receiver, so a
    reconnect left a second keyboard unmanaged until something forced a full scan --
    invisible with one device, and wrong with two.
    """
    wanted = [hint] if isinstance(hint, int) else list(hint or ())
    found = []
    for index in wanted:
        device = HidppDevice(transport, index)
        try:
            device.ping(timeout=0.8)
        except (p.HidppTimeout, p.HidppError, p.TransportClosed):
            continue
        found.append(device)
    if found:
        log.debug("hint hit: device indices %s answered directly", [d.index for d in found])
        return found

    answers = transport.scan(indices)
    devices = [
        HidppDevice(transport, index, protocol_version=version)
        for index, version in sorted(answers.items())
    ]
    log.debug("scan found device indices %s", [d.index for d in devices])
    return devices


def probe_devices(devices: list[HidppDevice]) -> list[tuple[HidppDevice, DeviceInfo]]:
    """Probe each device once and pair it with what was learned."""
    results = []
    for device in devices:
        try:
            info = device.probe()
        except (p.TransportClosed, OSError) as exc:
            log.debug("probe aborted for device %d: %s", device.index, exc)
            break
        except Exception as exc:  # a broken device must not sink the rest
            log.debug("probe failed for device %d: %s", device.index, exc)
            continue
        results.append((device, info))
    return results
