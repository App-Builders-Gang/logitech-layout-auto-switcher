"""Device-arrival watchers.

A watcher subscribes to the operating system's own device notifications and
calls back when a Logitech HID interface appears or disappears. It must never
poll, and its callback runs on an OS-owned thread, so the callback does nothing
but hand the event to the agent's queue and return immediately.
"""

from __future__ import annotations

import logging
import platform
from enum import Enum
from typing import Callable, Protocol

log = logging.getLogger(__name__)


class DeviceEvent(Enum):
    ARRIVED = "arrived"
    REMOVED = "removed"


#: ``callback(event, description)`` -- description is best-effort, for logs only.
WatcherCallback = Callable[[DeviceEvent, str], None]


class Watcher(Protocol):
    """Subscribes to OS device notifications."""

    name: str

    def start(self, callback: WatcherCallback) -> None:
        """Begin delivering events. Must return promptly."""

    def stop(self) -> None:
        """Unsubscribe and release every OS resource. Must be idempotent."""


def create_watcher(vendor_id: int, force_polling: bool = False) -> Watcher:
    """Best available watcher for this platform, falling back to polling.

    The fallback exists so an unexpected registration failure degrades to
    something that still works rather than taking the agent down with it.
    """
    from .polling import PollingWatcher

    if force_polling:
        return PollingWatcher(vendor_id)

    system = platform.system()
    try:
        if system == "Windows":
            from .windows import WindowsWatcher

            return WindowsWatcher(vendor_id)
        if system == "Darwin":
            from .darwin import DarwinWatcher

            return DarwinWatcher(vendor_id)
    except Exception as exc:
        log.warning("native device notifications unavailable (%s); falling back to polling", exc)
    else:
        if system not in ("Windows", "Darwin"):
            log.info("no native watcher for %s; using polling", system)
    return PollingWatcher(vendor_id)
