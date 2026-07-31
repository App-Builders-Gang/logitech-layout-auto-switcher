"""A fake HID++ receiver, faithful to the bytes recorded from real hardware.

Replays the exact behaviour captured in docs/PROTOCOL.md: a Logi Bolt receiver
with an MX Master 3S at index 1 (no platform feature) and an MX Keys S at
index 5 (MULTIPLATFORM at feature index 0x10, four platform descriptors).

Lets everything except the ctypes watchers be tested with no hardware.
"""

from __future__ import annotations

import queue
import threading

from logiswitch.hidpp import protocol as p

BOLT_PID = 0xC548
PATH_SHORT = b"fake:bolt:col01"
PATH_LONG = b"fake:bolt:col02"

MX_KEYS_INDEX = 5
MX_MASTER_INDEX = 1
MULTIPLATFORM_INDEX = 0x10
DEVICE_NAME_INDEX = 0x03

#: platform index -> OS mask, exactly as the MX Keys S reports it.
PLATFORM_TABLE = [
    (0, 0x1500),  # android / linux / windows
    (1, 0x2000),  # macos
    (2, 0x4000),  # ios
    (3, 0x0800),  # chrome
]


class FakeDevice:
    """One simulated device behind the receiver."""

    def __init__(
        self,
        index: int,
        name: str,
        features: dict[int, int] | None = None,
        platform: int | None = None,
        dual: bool = False,
    ):
        self.index = index
        self.name = name
        self.features = features or {}
        self.platform = platform
        self.dual = dual
        self.set_calls: list[int] = []
        #: When set, the device answers nothing (asleep / other channel).
        self.asleep = False


def mx_keys_s(index: int = MX_KEYS_INDEX) -> FakeDevice:
    return FakeDevice(
        index,
        "MX Keys S",
        features={p.FEATURE_DEVICE_NAME: DEVICE_NAME_INDEX, p.FEATURE_MULTIPLATFORM: MULTIPLATFORM_INDEX},
        platform=0,
    )


def mx_master_3s(index: int = MX_MASTER_INDEX) -> FakeDevice:
    return FakeDevice(index, "MX Master 3S", features={p.FEATURE_DEVICE_NAME: DEVICE_NAME_INDEX})


def craft_dualplatform(index: int = 2) -> FakeDevice:
    return FakeDevice(
        index,
        "Craft",
        features={p.FEATURE_DEVICE_NAME: DEVICE_NAME_INDEX, p.FEATURE_DUALPLATFORM: 0x08},
        platform=1,
        dual=True,
    )


class FakeReceiver:
    """Answers HID++ frames the way a Bolt receiver does."""

    def __init__(self, devices: list[FakeDevice], product_id: int = BOLT_PID):
        self.devices = {d.index: d for d in devices}
        self.product_id = product_id
        self.handles: list[FakeHandle] = []
        self.writes = 0
        self.lock = threading.Lock()

    # -- enumeration ----------------------------------------------------------

    def interfaces(self, single_collection: bool = False) -> list[dict]:
        base = {
            "vendor_id": p.LOGITECH_VID,
            "product_id": self.product_id,
            "serial_number": "FAKE123",
            "interface_number": 2,
            "product_string": "USB Receiver",
            "usage_page": p.HIDPP_USAGE_PAGE,
        }
        if single_collection:  # macOS-style: one entry covering both report ids
            return [{**base, "usage": p.USAGE_SHORT, "path": PATH_SHORT}]
        return [
            {**base, "usage": p.USAGE_SHORT, "path": PATH_SHORT},
            {**base, "usage": p.USAGE_LONG, "path": PATH_LONG},
        ]

    # -- frame handling -------------------------------------------------------

    def broadcast(self, frame: bytes) -> None:
        """Push an unsolicited frame (e.g. a 0x41 wake) to every open handle."""
        for handle in list(self.handles):
            handle.inbox.put(frame)

    def _reply(self, frame: bytes) -> bytes | None:
        device_index = frame[1]
        feature_index = frame[2]
        func_byte = frame[3]
        function = func_byte >> 4
        params = frame[4:]
        device = self.devices.get(device_index)

        if device is None:
            return self._error10(frame, 0x08)  # unknown device
        if device.asleep:
            return None

        if feature_index == p.FEATURE_ROOT:
            if function == p.ROOT_GET_PROTOCOL_VERSION:
                return self._pad(frame, bytes([4, 5, params[2] if len(params) > 2 else 0]))
            if function == p.ROOT_GET_FEATURE:
                wanted = (params[0] << 8) | params[1]
                return self._pad(frame, bytes([device.features.get(wanted, 0), 0x00, 0x00]))
            return self._error20(frame, 7)

        if feature_index == device.features.get(p.FEATURE_DEVICE_NAME):
            if function == 0:
                return self._pad(frame, bytes([len(device.name)]))
            if function == 1:
                offset = params[0]
                return self._pad(frame, device.name.encode()[offset : offset + 16])
            return self._error20(frame, 7)

        if feature_index == device.features.get(p.FEATURE_MULTIPLATFORM):
            return self._multiplatform(frame, device, function, params)

        if feature_index == device.features.get(p.FEATURE_DUALPLATFORM):
            if function == p.DP_GET_PLATFORM:
                return self._pad(frame, bytes([device.platform or 0]))
            if function == p.DP_SET_PLATFORM:
                device.platform = params[0]
                device.set_calls.append(params[0])
                return self._pad(frame, b"")
            return self._error20(frame, 7)

        return self._error20(frame, 6)  # invalid feature index

    def _multiplatform(self, frame, device, function, params):
        if function == p.MP_GET_FEATURE_INFOS:
            return self._pad(frame, bytes([0x03, 0x00, len(PLATFORM_TABLE), len(PLATFORM_TABLE)]))
        if function == p.MP_GET_PLATFORM_DESCRIPTOR:
            i = params[0]
            if i >= len(PLATFORM_TABLE):
                return self._error20(frame, 3)  # out of range
            index, mask = PLATFORM_TABLE[i]
            return self._pad(frame, bytes([index, i, mask >> 8, mask & 0xFF, 0, 0, 0, 0]))
        if function == p.MP_GET_HOST_PLATFORM:
            host = params[0] if params else p.HOST_CURRENT
            host_index = 0 if host == p.HOST_CURRENT else host
            if host_index > 0:
                return self._pad(frame, bytes([host_index, 0, 0xFF, 0]))
            return self._pad(frame, bytes([0, 1, device.platform or 0, 3]))
        if function == p.MP_SET_HOST_PLATFORM:
            device.platform = params[1]
            device.set_calls.append(params[1])
            return self._pad(frame, b"")
        return self._error20(frame, 7)

    @staticmethod
    def _pad(frame: bytes, payload: bytes) -> bytes:
        out = bytearray(p.LEN_LONG)
        out[0] = p.REPORT_LONG
        out[1] = frame[1]
        out[2] = frame[2]
        out[3] = frame[3]
        out[4 : 4 + len(payload)] = payload
        return bytes(out)

    @staticmethod
    def _error20(frame: bytes, code: int) -> bytes:
        out = bytearray(p.LEN_SHORT)
        out[0] = p.REPORT_SHORT
        out[1] = frame[1]
        out[2] = p.ERROR_HIDPP20
        out[3] = frame[2]
        out[4] = frame[3]
        out[5] = code
        return bytes(out)

    @staticmethod
    def _error10(frame: bytes, code: int) -> bytes:
        out = bytearray(p.LEN_SHORT)
        out[0] = p.REPORT_SHORT
        out[1] = frame[1]
        out[2] = p.ERROR_HIDPP10
        out[3] = frame[2]
        out[4] = frame[3]
        out[5] = code
        return bytes(out)


class FakeHandle:
    def __init__(self, receiver: FakeReceiver, path: bytes):
        self.receiver = receiver
        self.path = path
        self.inbox: queue.Queue = queue.Queue()
        self.closed = False
        receiver.handles.append(self)

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("write to a closed handle")
        with self.receiver.lock:
            self.receiver.writes += 1
            reply = self.receiver._reply(bytes(data))
        if reply is not None:
            self.inbox.put(reply)

    def read(self, size: int, timeout_ms: int) -> bytes:
        if self.closed:
            raise OSError("read from a closed handle")
        try:
            return self.inbox.get(timeout=max(timeout_ms, 1) / 1000.0)
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.closed = True
        if self in self.receiver.handles:
            self.receiver.handles.remove(self)


def install(monkeypatch, receiver: FakeReceiver, single_collection: bool = False) -> FakeReceiver:
    """Point the backend at `receiver` for the duration of a test."""
    from logiswitch.hidpp import backend

    def fake_enumerate(vendor_id: int = 0, product_id: int = 0) -> list[dict]:
        if vendor_id not in (0, p.LOGITECH_VID):
            return []
        return receiver.interfaces(single_collection)

    def fake_open(path: bytes) -> FakeHandle:
        return FakeHandle(receiver, path)

    monkeypatch.setattr(backend, "enumerate_devices", fake_enumerate)
    monkeypatch.setattr(backend, "open_path", fake_open)
    return receiver
