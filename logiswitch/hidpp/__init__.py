"""HID++ 2.0 client: protocol, transport, device model and discovery."""

from .device import DeviceInfo, HidppDevice, PlatformOption
from .discovery import (
    InterfaceGroup,
    discover_devices,
    find_groups,
    find_interfaces,
    group_interfaces,
    open_transport,
    probe_devices,
)
from .protocol import (
    OS_ALIASES,
    OS_MASKS,
    HidppError,
    HidppTimeout,
    TransportClosed,
    UnsupportedFeature,
    normalise_os,
)
from .transport import Transport

__all__ = [
    "DeviceInfo",
    "HidppDevice",
    "HidppError",
    "HidppTimeout",
    "InterfaceGroup",
    "OS_ALIASES",
    "OS_MASKS",
    "PlatformOption",
    "Transport",
    "TransportClosed",
    "UnsupportedFeature",
    "discover_devices",
    "find_groups",
    "find_interfaces",
    "group_interfaces",
    "normalise_os",
    "open_transport",
    "probe_devices",
]
