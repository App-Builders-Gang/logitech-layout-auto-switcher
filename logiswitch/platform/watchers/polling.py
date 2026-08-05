"""Fallback watcher.

Only used when the platform has no native watcher, or when registering one
failed. It is deliberately slow-ticking: correctness matters more than latency
in a degraded mode, and a long interval keeps the CPU cost negligible.
"""

from __future__ import annotations

import logging
import threading

from ...hidpp import discovery
from .base import DeviceEvent, WatcherCallback

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = 2.0


class PollingWatcher:
    name = "polling"

    def __init__(self, vendor_id: int, interval: float = DEFAULT_INTERVAL):
        self._vendor_id = vendor_id
        self._interval = interval
        self._callback: WatcherCallback | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._present: set[tuple] = set()

    def start(self, callback: WatcherCallback) -> None:
        self._callback = callback
        self._stop.clear()
        self._present = self._snapshot()
        self._thread = threading.Thread(target=self._run, name="logiswitch-poll", daemon=True)
        self._thread.start()
        log.info("using the polling watcher (%.1fs interval)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(self._interval + 1.0)
        self._callback = None

    def _snapshot(self) -> set[tuple]:
        try:
            return {group.key for group in discovery.find_groups(self._vendor_id)}
        except Exception as exc:
            log.debug("enumeration failed: %s", exc)
            return set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            current = self._snapshot()
            if current == self._present:
                continue
            arrived = current - self._present
            removed = self._present - current
            self._present = current
            callback = self._callback
            if callback is None:
                continue
            for key in arrived:
                callback(DeviceEvent.ARRIVED, str(key))
            for key in removed:
                callback(DeviceEvent.REMOVED, str(key))
