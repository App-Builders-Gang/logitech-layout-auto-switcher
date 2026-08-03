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
HOSTS_INFO_INDEX = 0x06

#: Which Easy-Switch channel the fake keyboard is parked on, as 0x1815 reports it.
CURRENT_HOST = 0

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
        buggy_current_host: bool = False,
        current_host: int = CURRENT_HOST,
    ):
        self.index = index
        self.name = name
        self.features = features or {}
        self.platform = platform
        self.dual = dual
        self.set_calls: list[int] = []
        #: Host index of every setHostPlatform, so a test can see what was addressed.
        self.set_hosts: list[int] = []
        #: When set, the device answers nothing (asleep / other channel).
        self.asleep = False
        #: Reproduce the MX Keys S firmware bug: a write addressed to host 0xFF is
        #: acknowledged and then silently discarded. Only a concrete host index
        #: actually changes the platform. This is the fault Solaar works around.
        self.buggy_current_host = buggy_current_host
        self.current_host = current_host


def mx_keys_s(index: int = MX_KEYS_INDEX, buggy_current_host: bool = False) -> FakeDevice:
    return FakeDevice(
        index,
        "MX Keys S",
        features={
            p.FEATURE_DEVICE_NAME: DEVICE_NAME_INDEX,
            p.FEATURE_MULTIPLATFORM: MULTIPLATFORM_INDEX,
            p.FEATURE_HOSTS_INFO: HOSTS_INFO_INDEX,
        },
        platform=0,
        buggy_current_host=buggy_current_host,
    )


def keyboard_without_hosts_info(index: int = MX_KEYS_INDEX) -> FakeDevice:
    """Older firmware with no 0x1815, which must keep working on 0xFF alone."""
    return FakeDevice(
        index,
        "MX Keys",
        features={
            p.FEATURE_DEVICE_NAME: DEVICE_NAME_INDEX,
            p.FEATURE_MULTIPLATFORM: MULTIPLATFORM_INDEX,
        },
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

    def __init__(
        self,
        devices: list[FakeDevice],
        product_id: int = BOLT_PID,
        truncate_replies: bool = False,
        shared: bool = False,
    ):
        self.devices = {d.index: d for d in devices}
        self.product_id = product_id
        self.handles: list[FakeHandle] = []
        self.writes = 0
        self.lock = threading.Lock()
        #: Model a receiver shared between machines, as through a KVM: every reply
        #: reaches every open handle. That is what real hardware does -- the peer's
        #: setHostPlatform reply is how another machine was identified at all -- and
        #: without it two agents on one receiver are invisible to each other.
        self.shared = shared
        #: Every setHostPlatform, as (owner, host_index, platform). Attributing a
        #: write to the machine that made it is the only measurement that matters
        #: when several are contending.
        self.ledger: list[tuple[str, int, int]] = []
        #: The handle whose write is currently being served, so `_reply` can record
        #: who asked without threading an argument through every helper.
        self._writer: str = "?"
        #: Reply with only the header, no payload. Firmware that answers a feature
        #: it half-implements looks like this, and it must not crash discovery.
        self.truncate_replies = truncate_replies

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

        if feature_index == device.features.get(p.FEATURE_HOSTS_INFO):
            if function == p.HI_GET_FEATURE_INFO:
                # [capabilities, ?, numHosts, currentHost] -- Solaar reads byte 3.
                return self._pad(frame, bytes([0x00, 0x00, 0x03, device.current_host]))
            return self._error20(frame, 7)

        if feature_index == device.features.get(p.FEATURE_MULTIPLATFORM):
            return self._multiplatform(frame, device, function, params)

        if feature_index == device.features.get(p.FEATURE_DUALPLATFORM):
            if function == p.DP_GET_PLATFORM:
                return self._pad(frame, bytes([device.platform or 0]))
            if function == p.DP_SET_PLATFORM:
                device.platform = params[0]
                device.set_calls.append(params[0])
                # Ledgered like MULTIPLATFORM: a write is a write, whichever feature
                # made it, and leaving these out understates what a machine did.
                self.ledger.append((self._writer, p.HOST_CURRENT, params[0]))
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
            host_index = device.current_host if host == p.HOST_CURRENT else host
            if host_index != device.current_host:
                return self._pad(frame, bytes([host_index, 0, 0xFF, 0]))
            return self._pad(frame, bytes([host_index, 1, device.platform or 0, 3]))
        if function == p.MP_SET_HOST_PLATFORM:
            host, platform = params[0], params[1]
            device.set_hosts.append(host)
            self.ledger.append((self._writer, host, platform))
            if device.buggy_current_host and host == p.HOST_CURRENT:
                # Acknowledged, and then quietly dropped -- the MX Keys S bug.
                return self._pad(frame, b"")
            device.platform = platform
            device.set_calls.append(platform)
            return self._pad(frame, b"")
        return self._error20(frame, 7)

    def _pad(self, frame: bytes, payload: bytes) -> bytes:
        if self.truncate_replies:
            return bytes([p.REPORT_LONG, frame[1], frame[2], frame[3]])
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
    def __init__(self, receiver: FakeReceiver, path: bytes, owner: str = "?"):
        self.receiver = receiver
        self.path = path
        #: Which machine opened this handle, for the receiver's write ledger.
        self.owner = owner
        self.inbox: queue.Queue = queue.Queue()
        self.closed = False
        receiver.handles.append(self)

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("write to a closed handle")
        with self.receiver.lock:
            self.receiver.writes += 1
            self.receiver._writer = self.owner
            reply = self.receiver._reply(bytes(data))
            shared = self.receiver.shared
            others = [h for h in self.receiver.handles if h is not self] if shared else []
        if reply is None:
            return
        self.inbox.put(reply)
        for handle in others:
            # A shared receiver puts device traffic in front of every host attached
            # to it. The requester matches this in its sink; to everyone else it is
            # an orphan -- which is exactly the signal that identifies a peer.
            handle.inbox.put(reply)

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


#: Set by the multi-machine harness so a handle can be attributed to the machine that
#: opened it. `backend.open_path` takes only a path, and threading an owner through it
#: would change production code to suit a test.
current_owner: list[str] = ["?"]


def install(monkeypatch, receiver: FakeReceiver, single_collection: bool = False) -> FakeReceiver:
    """Point the backend at `receiver` for the duration of a test."""
    from logiswitch.hidpp import backend

    def fake_enumerate(vendor_id: int = 0, product_id: int = 0) -> list[dict]:
        if vendor_id not in (0, p.LOGITECH_VID):
            return []
        return receiver.interfaces(single_collection)

    def fake_open(path: bytes) -> FakeHandle:
        return FakeHandle(receiver, path, owner=current_owner[0])

    monkeypatch.setattr(backend, "enumerate_devices", fake_enumerate)
    monkeypatch.setattr(backend, "open_path", fake_open)
    return receiver
