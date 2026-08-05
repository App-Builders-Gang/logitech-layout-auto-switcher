"""Windows device notifications via CM_Register_Notification (cfgmgr32).

This is the modern replacement for ``RegisterDeviceNotification``: it needs no
window handle and no message pump, which is exactly right for a background agent
with no UI. Callbacks are delivered on OS thread-pool threads.

Requires Windows 8 or newer.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from .._comtypes import _GUID, _guid
from .base import DeviceEvent, WatcherCallback

log = logging.getLogger(__name__)

CR_SUCCESS = 0
MAX_DEVICE_ID_LEN = 200

CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE = 0
CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL = 0
CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL = 1

ERROR_SUCCESS = 0

#: Offset of CM_NOTIFY_EVENT_DATA.u.DeviceInterface.SymbolicLink:
#: FilterType (4) + Reserved (4) + ClassGuid (16).
_SYMLINK_OFFSET = 24

GUID_DEVINTERFACE_HID = _guid("4D1E55B2-F16F-11CF-88CB-001111000030")


class _FilterDeviceInterface(ctypes.Structure):
    _fields_ = [("ClassGuid", _GUID)]


class _FilterDeviceHandle(ctypes.Structure):
    _fields_ = [("hTarget", wintypes.HANDLE)]


class _FilterDeviceInstance(ctypes.Structure):
    _fields_ = [("InstanceId", ctypes.c_wchar * MAX_DEVICE_ID_LEN)]


class _FilterUnion(ctypes.Union):
    _fields_ = [
        ("DeviceInterface", _FilterDeviceInterface),
        ("DeviceHandle", _FilterDeviceHandle),
        ("DeviceInstance", _FilterDeviceInstance),
    ]


class CM_NOTIFY_FILTER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("FilterType", ctypes.c_int),
        ("Reserved", wintypes.DWORD),
        ("u", _FilterUnion),
    ]


CM_NOTIFY_CALLBACK = ctypes.WINFUNCTYPE(
    wintypes.DWORD,  # return: ERROR_SUCCESS
    wintypes.HANDLE,  # hNotify
    ctypes.c_void_p,  # Context
    ctypes.c_int,  # Action
    ctypes.c_void_p,  # EventData
    wintypes.DWORD,  # EventDataSize
)


class WindowsWatcher:
    name = "cfgmgr32"

    def __init__(self, vendor_id: int):
        self._vendor_token = f"vid_{vendor_id:04x}"
        self._callback: WatcherCallback | None = None
        self._handle = wintypes.HANDLE()
        self._registered = False

        self._cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)
        self._cfgmgr32.CM_Register_Notification.argtypes = [
            ctypes.POINTER(CM_NOTIFY_FILTER),
            ctypes.c_void_p,
            CM_NOTIFY_CALLBACK,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self._cfgmgr32.CM_Register_Notification.restype = wintypes.DWORD
        self._cfgmgr32.CM_Unregister_Notification.argtypes = [wintypes.HANDLE]
        self._cfgmgr32.CM_Unregister_Notification.restype = wintypes.DWORD

        # These two MUST stay referenced for as long as the registration lives.
        # Windows keeps raw pointers to both; if Python collects them the next
        # notification calls into freed memory and takes the process down.
        self._native_callback = CM_NOTIFY_CALLBACK(self._on_notify)
        self._filter = CM_NOTIFY_FILTER()
        self._filter.cbSize = ctypes.sizeof(CM_NOTIFY_FILTER)
        self._filter.Flags = 0
        self._filter.FilterType = CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
        self._filter.Reserved = 0
        self._filter.u.DeviceInterface.ClassGuid = GUID_DEVINTERFACE_HID

    # -- Watcher protocol -----------------------------------------------------

    def start(self, callback: WatcherCallback) -> None:
        self._callback = callback
        result = self._cfgmgr32.CM_Register_Notification(
            ctypes.byref(self._filter), None, self._native_callback, ctypes.byref(self._handle)
        )
        if result != CR_SUCCESS:
            raise OSError(f"CM_Register_Notification failed with CONFIGRET 0x{result:08X}")
        self._registered = True
        log.debug("subscribed to HID interface notifications (cfgmgr32)")

    def stop(self) -> None:
        if not self._registered:
            return
        self._registered = False
        self._callback = None
        result = self._cfgmgr32.CM_Unregister_Notification(self._handle)
        if result != CR_SUCCESS:  # pragma: no cover - nothing useful to do
            log.debug("CM_Unregister_Notification returned 0x%08X", result)
        self._handle = wintypes.HANDLE()

    # -- callback -------------------------------------------------------------

    def _on_notify(
        self,
        _h_notify: int,
        _context: int,
        action: int,
        event_data: int,
        event_data_size: int,
    ) -> int:
        # Runs on an OS thread-pool thread: do the minimum and return.
        try:
            if action == CM_NOTIFY_ACTION_DEVICEINTERFACEARRIVAL:
                event = DeviceEvent.ARRIVED
            elif action == CM_NOTIFY_ACTION_DEVICEINTERFACEREMOVAL:
                event = DeviceEvent.REMOVED
            else:
                return ERROR_SUCCESS
            link = self._symbolic_link(event_data, event_data_size)
            if self._vendor_token not in link.lower():
                return ERROR_SUCCESS
            callback = self._callback
            if callback is not None:
                callback(event, link)
        except Exception:  # never let an exception unwind into Windows
            log.exception("device notification handler failed")
        return ERROR_SUCCESS

    @staticmethod
    def _symbolic_link(event_data: int, event_data_size: int) -> str:
        if not event_data or event_data_size <= _SYMLINK_OFFSET:
            return ""
        try:
            return ctypes.wstring_at(event_data + _SYMLINK_OFFSET)
        except Exception:  # pragma: no cover - malformed payload
            return ""
