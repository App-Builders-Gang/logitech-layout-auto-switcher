"""Indirection over the `hid` module.

Two incompatible wrappers ship under the same import name: `hidapi`
(cython-hidapi, exposing ``hid.device()`` + ``open_path()``) and `hid`
(pyhidapi, exposing ``hid.Device(path=...)``). Which one is present varies by
platform and by how the user installed it, so everything goes through here.

Tests swap :func:`enumerate_devices` and :func:`open_path` for a fake, which is
why no other module imports `hid` directly.
"""

from __future__ import annotations

import sys
from typing import Any


def configure_non_exclusive_open(hid_module: Any) -> bool:
    """Force the macOS HID open to be non-exclusive. Returns whether it applied.

    cython-hidapi (the ``hid`` package) links the C hidapi library, whose macOS backend
    opens IOHIDDevice with ``kIOHIDOptionsTypeSeizeDevice`` by default in the build this
    project ships. A seize gives the opener the device's input reports to itself alone:
    every other client -- Logi Options+, Solaar, anything reading the same vendor
    collection -- stops receiving them. That is silent to ordinary typing, which travels
    a different (boot/consumer) interface, but it breaks every HID++ 2.0 notification
    another program relies on. A key Logi Options+ has diverted (HID++ ``0x1B04``,
    control ``0x000A`` for the calculator key) emits its keypress as exactly such a
    notification; with this agent holding the interface exclusively the notification
    reached only us, and the key did nothing. Verified both ways: the notification
    arrives here and the agent sends nothing in reply, yet the key fails; opening
    non-exclusively restores it immediately.

    hidapi exposes the switch as ``hid_darwin_set_open_exclusive``, but cython-hidapi
    does not bind it, so reach the symbol through ctypes on the same compiled module. It
    is a process-wide global that must be set before any open, which is why this runs at
    import time. The reason it is a named function rather than inline is so a test can
    prove the call happens and the regression cannot return silently.
    """
    if sys.platform != "darwin":
        return False
    try:  # pragma: no cover - depends on a native build detail
        import ctypes

        darwin = ctypes.CDLL(hid_module.__file__)
        setter = darwin.hid_darwin_set_open_exclusive
        setter.argtypes = [ctypes.c_int]
        setter.restype = None
        setter(0)
    except (OSError, AttributeError):  # pragma: no cover - older/different build
        return False
    return True


try:  # pragma: no cover - trivial import shim
    import hid as _hid
except ImportError as exc:  # pragma: no cover
    _hid = None
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None
    # Applied once at import, before any open. See configure_non_exclusive_open above.
    configure_non_exclusive_open(_hid)


class HidUnavailable(RuntimeError):
    """The `hid` module is missing or failed to load its native library."""


def _require_hid() -> Any:
    if _hid is None:
        raise HidUnavailable(
            "the 'hid' module is not importable -- install it with 'pip install hidapi'"
        ) from _IMPORT_ERROR
    return _hid


def enumerate_devices(vendor_id: int = 0, product_id: int = 0) -> list[dict]:
    """All HID interfaces matching the filter, as hidapi dicts."""
    return list(_require_hid().enumerate(vendor_id, product_id))


class HidHandle:
    """One open HID interface.

    Reads and writes are whole reports; the first byte is always the report id.
    """

    def __init__(self, path: bytes):
        hid = _require_hid()
        self.path = path
        if hasattr(hid, "Device"):  # pyhidapi
            self._dev = hid.Device(path=path)
            self._pyhidapi = True
        else:  # cython-hidapi
            self._dev = hid.device()
            self._dev.open_path(path)
            self._dev.set_nonblocking(0)
            self._pyhidapi = False

    def write(self, data: bytes) -> None:
        self._dev.write(data)

    def read(self, size: int, timeout_ms: int) -> bytes:
        """Block up to `timeout_ms` for one report. Returns b"" on timeout.

        This is a kernel wait (overlapped I/O on Windows, a run-loop wait on
        macOS), not a spin -- it is what keeps the agent at ~0% CPU while idle.
        """
        if self._pyhidapi:
            data = self._dev.read(size, timeout=timeout_ms)
        else:
            data = self._dev.read(size, timeout_ms)
        return bytes(data) if data else b""

    def close(self) -> None:
        self._dev.close()


def open_path(path: bytes) -> HidHandle:
    return HidHandle(path)
