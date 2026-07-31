"""Watcher tests.

The ctypes plumbing itself cannot be exercised without real device events, but
the decode path and the registration lifecycle can be, and those are where the
mistakes actually live.
"""

from __future__ import annotations

import ctypes
import platform
import time

import pytest

from logiswitch.watchers import DeviceEvent, create_watcher
from logiswitch.watchers.polling import PollingWatcher

windows_only = pytest.mark.skipif(platform.system() != "Windows", reason="Windows only")


def test_create_watcher_returns_the_native_one_or_falls_back():
    watcher = create_watcher(0x046D)
    expected = {"Windows": "cfgmgr32", "Darwin": "iokit"}.get(platform.system(), "polling")
    assert watcher.name in (expected, "polling")


def test_force_polling_is_honoured():
    assert create_watcher(0x046D, force_polling=True).name == "polling"


def test_polling_watcher_reports_arrival_and_removal(monkeypatch, receiver):
    import fakehid

    from logiswitch.hidpp import backend

    events = []
    watcher = PollingWatcher(0x046D, interval=0.05)
    watcher.start(lambda event, description: events.append(event))
    try:
        monkeypatch.setattr(backend, "enumerate_devices", lambda *a, **k: [])
        deadline = time.time() + 3
        while DeviceEvent.REMOVED not in events and time.time() < deadline:
            time.sleep(0.05)
        assert DeviceEvent.REMOVED in events

        monkeypatch.setattr(
            backend, "enumerate_devices", lambda *a, **k: receiver.interfaces()
        )
        deadline = time.time() + 3
        while DeviceEvent.ARRIVED not in events and time.time() < deadline:
            time.sleep(0.05)
        assert DeviceEvent.ARRIVED in events
    finally:
        watcher.stop()
    assert fakehid  # keep the import meaningful


def test_polling_watcher_stop_is_idempotent():
    watcher = PollingWatcher(0x046D, interval=0.05)
    watcher.start(lambda *a: None)
    watcher.stop()
    watcher.stop()


@windows_only
def test_cm_notify_filter_matches_the_documented_size():
    from logiswitch.watchers.windows import CM_NOTIFY_FILTER, WindowsWatcher

    watcher = WindowsWatcher(0x046D)
    assert watcher._filter.cbSize == ctypes.sizeof(CM_NOTIFY_FILTER)
    # FilterType 0 == CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
    assert watcher._filter.FilterType == 0
    assert watcher._filter.u.DeviceInterface.ClassGuid.Data1 == 0x4D1E55B2


@windows_only
def test_callback_decodes_the_symbolic_link_and_filters_by_vendor():
    from logiswitch.watchers.windows import (
        _SYMLINK_OFFSET,
        CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL,
        CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL,
        WindowsWatcher,
    )

    watcher = WindowsWatcher(0x046D)
    seen = []
    watcher._callback = lambda event, link: seen.append((event, link))

    def event_data(link: str) -> tuple[int, int]:
        payload = link.encode("utf-16-le") + b"\x00\x00"
        buffer = ctypes.create_string_buffer(_SYMLINK_OFFSET + len(payload))
        ctypes.memmove(ctypes.byref(buffer, _SYMLINK_OFFSET), payload, len(payload))
        # The buffer must outlive the call; return it so it stays referenced.
        return buffer, ctypes.addressof(buffer), len(buffer)

    keep, address, size = event_data(r"\\?\HID#VID_046D&PID_C548&MI_02&Col01#e&1f16b90b")
    watcher._on_notify(0, 0, CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL, address, size)
    assert seen and seen[-1][0] is DeviceEvent.ARRIVED

    watcher._on_notify(0, 0, CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL, address, size)
    assert seen[-1][0] is DeviceEvent.REMOVED

    keep2, address2, size2 = event_data(r"\\?\HID#VID_1234&PID_0001#foo")
    before = len(seen)
    watcher._on_notify(0, 0, CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL, address2, size2)
    assert len(seen) == before, "a non-Logitech device must be ignored"
    assert keep and keep2


@windows_only
def test_registration_round_trip_against_the_real_api():
    from logiswitch.watchers.windows import WindowsWatcher

    watcher = WindowsWatcher(0x046D)
    watcher.start(lambda *a: None)
    try:
        assert watcher._registered
    finally:
        watcher.stop()
    assert not watcher._registered
    watcher.stop()  # idempotent
