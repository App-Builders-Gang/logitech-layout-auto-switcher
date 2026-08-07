"""The `hid` wrapper adapter.

Two incompatible libraries install under the same import name and differ between
platforms, so the adapter is the one place a wrong guess breaks everything with a
confusing AttributeError. Both shapes are exercised here.
"""

from __future__ import annotations

import pytest

from logiswitch.hidpp import backend


class PyhidapiDevice:
    """`hid` (pyhidapi): hid.Device(path=...), read(size, timeout=...)."""

    def __init__(self, path):
        self.path = path
        self.written: list[bytes] = []
        self.to_read: list = []
        self.closed = False
        self.read_calls: list[tuple] = []

    def write(self, data):
        self.written.append(bytes(data))

    def read(self, size, timeout):
        self.read_calls.append((size, timeout))
        return self.to_read.pop(0) if self.to_read else b""

    def close(self):
        self.closed = True


class CythonDevice:
    """`hidapi` (cython-hidapi): hid.device(), open_path(), read(size, timeout_ms)."""

    def __init__(self):
        self.path = None
        self.written: list[bytes] = []
        self.to_read: list = []
        self.closed = False
        self.nonblocking = None
        self.read_calls: list[tuple] = []

    def open_path(self, path):
        self.path = path

    def set_nonblocking(self, value):
        self.nonblocking = value

    def write(self, data):
        self.written.append(bytes(data))

    def read(self, size, timeout_ms):
        self.read_calls.append((size, timeout_ms))
        return self.to_read.pop(0) if self.to_read else []

    def close(self):
        self.closed = True


class FakeHidModule:
    def __init__(self, style: str):
        self.created: list = []
        self.enumerated: list[tuple] = []
        if style == "pyhidapi":
            self.Device = self._make_pyhidapi
        else:
            self.device = self._make_cython

    def _make_pyhidapi(self, path):
        device = PyhidapiDevice(path)
        self.created.append(device)
        return device

    def _make_cython(self):
        device = CythonDevice()
        self.created.append(device)
        return device

    def enumerate(self, vendor_id, product_id):
        self.enumerated.append((vendor_id, product_id))
        return [{"vendor_id": vendor_id, "product_id": product_id, "path": b"p"}]


@pytest.fixture(params=["pyhidapi", "cython"])
def hid_module(request, monkeypatch):
    module = FakeHidModule(request.param)
    monkeypatch.setattr(backend, "_hid", module)
    module.style = request.param
    return module


def test_open_path_works_with_either_wrapper(hid_module):
    handle = backend.open_path(b"/dev/thing")

    assert isinstance(handle, backend.HidHandle)
    assert hid_module.created[0].path == b"/dev/thing"


def test_cython_wrapper_is_put_into_blocking_mode(monkeypatch):
    """Non-blocking reads would turn the reader thread into a spin loop."""
    module = FakeHidModule("cython")
    monkeypatch.setattr(backend, "_hid", module)

    backend.open_path(b"p")

    assert module.created[0].nonblocking == 0


def test_write_reaches_the_device(hid_module):
    handle = backend.open_path(b"p")

    handle.write(b"\x10\x01\x02")

    assert hid_module.created[0].written == [b"\x10\x01\x02"]


def test_read_returns_bytes_from_either_wrapper(hid_module):
    handle = backend.open_path(b"p")
    hid_module.created[0].to_read = [[0x11, 0x05, 0x00]]

    data = handle.read(20, 500)

    assert isinstance(data, bytes)
    assert data == b"\x11\x05\x00"


def test_read_passes_the_timeout_through(hid_module):
    handle = backend.open_path(b"p")

    handle.read(20, 750)

    assert hid_module.created[0].read_calls == [(20, 750)]


def test_an_empty_read_is_normalised_to_empty_bytes(hid_module):
    """A timeout is the common case; it must not be confused with a frame."""
    handle = backend.open_path(b"p")

    assert handle.read(20, 1) == b""


def test_close_reaches_the_device(hid_module):
    handle = backend.open_path(b"p")

    handle.close()

    assert hid_module.created[0].closed is True


def test_enumerate_delegates_and_returns_a_list(hid_module):
    result = backend.enumerate_devices(0x046D)

    assert hid_module.enumerated == [(0x046D, 0)]
    assert isinstance(result, list)
    assert result[0]["vendor_id"] == 0x046D


def test_a_missing_hid_module_gives_an_actionable_error(monkeypatch):
    monkeypatch.setattr(backend, "_hid", None)

    with pytest.raises(backend.HidUnavailable) as excinfo:
        backend.enumerate_devices()

    assert "pip install hidapi" in str(excinfo.value)

    with pytest.raises(backend.HidUnavailable):
        backend.open_path(b"p")


class _FakeExclusiveSetter:
    """A stand-in for the loaded hidapi C library, recording the exclusive call."""

    def __init__(self):
        self.called_with: int | None = None

    @property
    def hid_darwin_set_open_exclusive(self):
        def setter(value):
            self.called_with = value

        setter.argtypes = None
        setter.restype = None
        return setter


def test_the_hid_open_is_made_non_exclusive_on_macos(monkeypatch):
    """The regression test for the calculator key.

    cython-hidapi opens IOHIDDevice with ``kIOHIDOptionsTypeSeizeDevice`` by default,
    which hands the device's input reports to the opener alone and starves every other
    client -- Logi Options+ above all, whose diverted keys arrive as HID++ notifications
    it must receive to act on. The fix is one call to
    ``hid_darwin_set_open_exclusive(0)`` before any open. This test pins that the call
    happens with 0, so the regression cannot return silently: drop the call, or pass
    anything but 0, and a key another program has diverted stops working again.
    """
    import ctypes

    fake_lib = _FakeExclusiveSetter()
    # `configure_non_exclusive_open` imports ctypes itself and resolves CDLL off that
    # module object, so the patch must land on the real ctypes module.
    monkeypatch.setattr(ctypes, "CDLL", lambda _path: fake_lib)
    monkeypatch.setattr(backend.sys, "platform", "darwin")

    fake_hid = type("FakeHid", (), {"__file__": "/fake/hid.so"})()
    applied = backend.configure_non_exclusive_open(fake_hid)

    assert applied is True
    assert fake_lib.called_with == 0, "the open must be non-exclusive, or diverted keys break"


def test_the_open_mode_is_left_alone_off_macos(monkeypatch):
    """On Windows and Linux there is no seize, so nothing to configure."""
    import ctypes

    monkeypatch.setattr(ctypes, "CDLL", lambda _path: pytest.fail("no ctypes call expected"))
    monkeypatch.setattr(backend.sys, "platform", "win32")

    assert backend.configure_non_exclusive_open(type("F", (), {"__file__": "x"})()) is False
