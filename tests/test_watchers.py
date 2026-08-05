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

from logiswitch.platform.watchers import DeviceEvent, create_watcher
from logiswitch.platform.watchers.polling import PollingWatcher

windows_only = pytest.mark.skipif(platform.system() != "Windows", reason="Windows only")
macos_only = pytest.mark.skipif(platform.system() != "Darwin", reason="macOS only")


def _native_watcher_module():
    """The module and attribute name of this platform's native watcher, if any."""
    if platform.system() == "Windows":
        from logiswitch.platform.watchers import windows

        return windows, "WindowsWatcher"
    if platform.system() == "Darwin":
        from logiswitch.platform.watchers import darwin

        return darwin, "DarwinWatcher"
    return None, ""


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

        monkeypatch.setattr(backend, "enumerate_devices", lambda *a, **k: receiver.interfaces())
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


def test_polling_watcher_survives_a_failing_enumeration(monkeypatch, receiver):
    """A transient enumeration error must not kill the fallback watcher."""
    from logiswitch.hidpp import backend

    watcher = PollingWatcher(0x046D, interval=0.05)
    watcher.start(lambda *a: None)
    try:

        def boom(*_a, **_k):
            raise OSError("hid subsystem busy")

        monkeypatch.setattr(backend, "enumerate_devices", boom)
        time.sleep(0.25)
        monkeypatch.setattr(backend, "enumerate_devices", lambda *a, **k: receiver.interfaces())
        time.sleep(0.25)
    finally:
        watcher.stop()


def test_create_watcher_falls_back_when_the_native_one_cannot_be_built(monkeypatch):
    """A registration failure must degrade to polling, not take the agent down."""
    module, name = _native_watcher_module()
    if module is None:
        pytest.skip("no native watcher on this platform")

    def refuse(_vendor_id):
        raise OSError("registration refused")

    monkeypatch.setattr(module, name, refuse)

    assert create_watcher(0x046D).name == "polling"


def test_agent_falls_back_when_the_watcher_fails_to_start(monkeypatch, receiver, tmp_path):
    """create_watcher succeeded but start() blew up -- still must end up watching."""
    from logiswitch.agent import Agent, AgentConfig

    class Refuses:
        name = "explodes"

        def __init__(self, _vendor_id):
            pass

        def start(self, _callback):
            raise OSError("could not subscribe")

        def stop(self):
            pass

    monkeypatch.setattr("logiswitch.agent.create_watcher", lambda *a, **k: Refuses(0))

    agent = Agent(
        AgentConfig(
            target_os="windows",
            debounce=0.0,
            reassert_interval=0.0,
            state_file=tmp_path / "state.json",
        )
    )
    agent.start()
    try:
        assert agent._watcher is not None
        assert agent._watcher.name == "polling"
    finally:
        agent.stop()
        agent.shutdown()


@windows_only
def test_cm_notify_filter_matches_the_documented_size():
    from logiswitch.platform.watchers.windows import CM_NOTIFY_FILTER, WindowsWatcher

    watcher = WindowsWatcher(0x046D)
    assert watcher._filter.cbSize == ctypes.sizeof(CM_NOTIFY_FILTER)
    # FilterType 0 == CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
    assert watcher._filter.FilterType == 0
    assert watcher._filter.u.DeviceInterface.ClassGuid.Data1 == 0x4D1E55B2


@windows_only
def test_callback_decodes_the_symbolic_link_and_filters_by_vendor():
    from logiswitch.platform.watchers.windows import (
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
    from logiswitch.platform.watchers.windows import WindowsWatcher

    watcher = WindowsWatcher(0x046D)
    watcher.start(lambda *a: None)
    try:
        assert watcher._registered
    finally:
        watcher.stop()
    assert not watcher._registered
    watcher.stop()  # idempotent


@windows_only
def test_the_native_callback_stays_referenced_while_registered():
    """Windows holds a raw pointer to it; letting Python collect it faults the process."""
    from logiswitch.platform.watchers.windows import WindowsWatcher

    watcher = WindowsWatcher(0x046D)
    assert watcher._native_callback is not None
    assert watcher._filter is not None


# -- macOS --------------------------------------------------------------------


@macos_only
def test_iokit_frameworks_load_and_expose_what_we_call():
    from logiswitch.platform.watchers.darwin import _Frameworks

    frameworks = _Frameworks()

    assert frameworks.run_loop_default_mode.value, "kCFRunLoopDefaultMode must resolve"
    for name in ("IONotificationPortCreate", "IOServiceAddMatchingNotification", "IOIteratorNext"):
        assert hasattr(frameworks.iokit, name)


@macos_only
def test_matching_dictionary_is_built_for_the_vendor():
    from logiswitch.platform.watchers.darwin import DarwinWatcher

    watcher = DarwinWatcher(0x046D)
    matching = watcher._matching_dict()

    assert matching.value, "IOServiceMatching(IOHIDDevice) must return a dictionary"
    watcher._fw.cf.CFRelease(matching)


@macos_only
def test_the_terminate_notification_name_is_the_one_iokit_accepts():
    """Registering "IOServiceTerminated" returned kIOReturnUnsupported and silently
    disabled the whole watcher; IOKitKeys.h spells it without the trailing 'd'."""
    from logiswitch.platform.watchers import darwin

    assert darwin.kIOTerminatedNotification == b"IOServiceTerminate"
    assert darwin.kIOMatchedNotification == b"IOServiceMatched"


@macos_only
def test_iokit_registration_round_trip_against_the_real_api():
    """Start and stop the real watcher. No Logitech hardware needed -- registering
    for notifications is registry discovery, not device access."""
    from logiswitch.platform.watchers.darwin import DarwinWatcher

    watcher = DarwinWatcher(0x046D)
    watcher.start(lambda *a: None)
    try:
        assert watcher._thread is not None and watcher._thread.is_alive()
        assert watcher._error is None
        assert watcher._port
    finally:
        watcher.stop()
    assert watcher._thread is None
    watcher.stop()  # idempotent


@macos_only
def test_the_iokit_callbacks_stay_referenced():
    from logiswitch.platform.watchers.darwin import DarwinWatcher

    watcher = DarwinWatcher(0x046D)

    assert watcher._native_matched is not None
    assert watcher._native_terminated is not None
