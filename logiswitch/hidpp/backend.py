"""Indirection over the `hid` module.

Two incompatible wrappers ship under the same import name: `hidapi`
(cython-hidapi, exposing ``hid.device()`` + ``open_path()``) and `hid`
(pyhidapi, exposing ``hid.Device(path=...)``). Which one is present varies by
platform and by how the user installed it, so everything goes through here.

Tests swap :func:`enumerate_devices` and :func:`open_path` for a fake, which is
why no other module imports `hid` directly.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - trivial import shim
    import hid as _hid
except ImportError as exc:  # pragma: no cover
    _hid = None
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None


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
