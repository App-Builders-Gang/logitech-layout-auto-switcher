"""HID++ 2.0 wire format: constants, framing and error decoding.

Frame layout, identical for both report sizes::

    [reportID, deviceIndex, featureIndex, (funcIndex << 4) | swId, p0, p1, p2, ...]

Nothing in this module touches hardware, so it is fully unit-testable.
"""

from __future__ import annotations

LOGITECH_VID = 0x046D

#: Receivers we can name. Any 046D device exposing the HID++ collection works --
#: this table only makes log output friendlier and is never used to gate support.
KNOWN_RECEIVERS = {
    0xC52B: "Unifying receiver",
    0xC52F: "Nano receiver",
    0xC534: "Nano receiver (dual)",
    0xC539: "Lightspeed receiver",
    0xC53A: "Lightspeed receiver",
    0xC53D: "Lightspeed receiver",
    0xC541: "Lightspeed receiver",
    0xC547: "Logi Bolt receiver",
    0xC548: "Logi Bolt receiver",
}

HIDPP_USAGE_PAGE = 0xFF00
USAGE_SHORT = 0x0001
USAGE_LONG = 0x0002

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
LEN_SHORT = 7
LEN_LONG = 20
REPORT_SIZES = {REPORT_SHORT: LEN_SHORT, REPORT_LONG: LEN_LONG}

#: Software id nibble. Non-zero marks a frame as *our* response rather than an
#: unsolicited event -- HID++ 2.0 notifications always carry swId 0.
SW_ID = 0x0E

#: The swIds a request may be stamped with, cycled one per request.
#:
#: A *fixed* swId is not merely untidy: the response sink matches a reply on
#: (deviceIndex, featureIndex, funcByte), and funcByte embeds the swId. With one
#: constant value the reply to a request that already timed out is byte-identical
#: in every matched field to the reply the next identical request is waiting for,
#: so the late frame is accepted as the new answer. Reading a stale platform that
#: way makes the agent conclude "already correct" and skip a write it needed --
#: the keyboard stays in the wrong mode and the log says everything is fine.
#: Rotating means a straggler carries a different nibble and cannot match.
#:
#: 0 is excluded: HID++ 2.0 notifications always carry swId 0, so using it would
#: make our own replies indistinguishable from unsolicited events.
#:
#: 0x0B is excluded too. Solaar pins every one of its requests to that value on
#: purpose, so that cooperating userspace HID++ clients sharing a device can each
#: pick a distinct id and filter their own traffic out of the shared stream. We
#: rotate because our reader-thread architecture needs to tell a late reply from a
#: fresh one, but there is no reason to ever impersonate another client while doing
#: it. (Rotation alone is not what makes the stale-reply fix sound -- see the
#: abandoned-request tracking in transport.py, which closes the race on its own.)
SOLAAR_SW_ID = 0x0B
SW_IDS = tuple(sw_id for sw_id in range(1, 16) if sw_id != SOLAAR_SW_ID)

#: Addresses the receiver itself, and also a device connected directly over USB
#: cable or Bluetooth (there is no receiver in that path).
INDEX_DIRECT = 0xFF
#: Receiver slots.
RECEIVER_SLOTS = range(1, 7)

#: HID++ 1.0 sub-id emitted by a receiver when a paired device connects or wakes.
NOTIF_DEVICE_CONNECTION = 0x41

#: Flags in the first parameter of a 0x41 notification. The link bit is reported
#: inverted -- set means *not* established -- so a connect and a disconnect arrive
#: as the same sub-id and are told apart only by this byte.
NOTIF_LINK_ENCRYPTED = 0x20
NOTIF_LINK_NOT_ESTABLISHED = 0x40

ERROR_HIDPP20 = 0xFF
ERROR_HIDPP10 = 0x8F

FEATURE_ROOT = 0x0000
FEATURE_FEATURE_SET = 0x0001
FEATURE_DEVICE_NAME = 0x0005
FEATURE_HOSTS_INFO = 0x1815
FEATURE_DUALPLATFORM = 0x4530
FEATURE_MULTIPLATFORM = 0x4531

# 0x1815 HOSTS_INFO functions.
HI_GET_FEATURE_INFO = 0

# Root feature (index 0) functions.
ROOT_GET_FEATURE = 0
ROOT_GET_PROTOCOL_VERSION = 1

# 0x4531 MULTIPLATFORM functions.
MP_GET_FEATURE_INFOS = 0
MP_GET_PLATFORM_DESCRIPTOR = 1
MP_GET_HOST_PLATFORM = 2
MP_SET_HOST_PLATFORM = 3

# 0x4530 DUALPLATFORM functions.
DP_GET_PLATFORM = 0
DP_SET_PLATFORM = 2

#: 0x4530 only knows two buckets.
DUALPLATFORM_CHOICES = {
    0x00: ("ios", "macos"),
    0x01: ("android", "windows"),
}

#: "Whichever host is currently active", per the HID++ 2.0 spec.
#:
#: Correct on paper and unreliable in practice: Solaar carries the note that you
#: "can't just use the first byte = 0xFF (for current host) because of a bug in the
#: firmware of the MX Keys S", and works around it by resolving the concrete host
#: index from 0x1815 HOSTS_INFO first. See :meth:`HidppDevice.current_host` and
#: docs/RESOURCES.md -- this constant is now only the fallback for devices that do
#: not implement 0x1815.
HOST_CURRENT = 0xFF

#: Who last set the platform, from a getHostPlatform record. This is the field that
#: separates "we did it", "the user held Fn+O", and "other software changed it" --
#: three causes of a wrong layout that are otherwise indistinguishable.
PLATFORM_SOURCES = {
    0: "default",
    1: "keyboard",
    2: "auto",
    3: "host software",
}

HOST_STATUSES = {
    0: "unpaired",
    1: "paired",
}

#: 16-bit OS mask bits from a MULTIPLATFORM platform descriptor.
OS_MASKS = {
    "tizen": 0x0001,
    "windows": 0x0100,
    "winemb": 0x0200,
    "linux": 0x0400,
    "chrome": 0x0800,
    "android": 0x1000,
    "macos": 0x2000,
    "ios": 0x4000,
    "webos": 0x8000,
}

#: Friendly names accepted on the CLI and in config.
OS_ALIASES = {
    "win": "windows",
    "windows": "windows",
    "pc": "windows",
    "mac": "macos",
    "macos": "macos",
    "osx": "macos",
    "darwin": "macos",
    "ios": "ios",
    "ipados": "ios",
    "android": "android",
    "linux": "linux",
    "chrome": "chrome",
    "chromeos": "chrome",
}

HIDPP20_ERRORS = {
    0: "no error",
    1: "unknown",
    2: "invalid argument",
    3: "out of range",
    4: "hardware error",
    5: "logitech internal",
    6: "invalid feature index",
    7: "invalid function id",
    8: "busy",
    9: "unsupported",
}

HIDPP10_ERRORS = {
    1: "invalid subid",
    2: "invalid address",
    3: "invalid value",
    4: "connection request failed",
    5: "too many devices",
    6: "already exists",
    7: "busy",
    8: "unknown device",
    9: "resource error",
    10: "request unavailable",
    11: "unsupported parameter value",
    12: "wrong pin code",
}


class HidppError(Exception):
    """The device answered with an error frame."""

    def __init__(self, code: int, protocol: int = 20, context: str = ""):
        table = HIDPP20_ERRORS if protocol == 20 else HIDPP10_ERRORS
        name = table.get(code, f"error {code}")
        super().__init__(f"HID++{protocol // 10}.0 {name} (0x{code:02X}){context}")
        self.code = code
        self.protocol = protocol


class HidppTimeout(Exception):
    """No matching response arrived within the deadline."""


class UnsupportedFeature(Exception):
    """The device does not implement the requested feature."""


class TransportClosed(Exception):
    """The receiver went away mid-request."""


def normalise_os(name: str) -> str:
    """Map a user-supplied OS name onto a canonical key in :data:`OS_MASKS`."""
    key = OS_ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in OS_MASKS:
        raise ValueError(f"unknown OS {name!r}; try one of {', '.join(sorted(OS_ALIASES))}")
    return key


def os_names_for_mask(mask: int) -> list[str]:
    return sorted(name for name, bit in OS_MASKS.items() if mask & bit)


def function_byte(function: int, sw_id: int = SW_ID) -> int:
    return ((function & 0x0F) << 4) | (sw_id & 0x0F)


def build_frame(
    device_index: int,
    feature_index: int,
    function: int,
    params: bytes = b"",
    sw_id: int = SW_ID,
    long_report: bool | None = None,
) -> bytes:
    """Encode one HID++ request.

    A short report carries three parameter bytes; anything larger is promoted to
    a long report automatically unless the caller forces a choice.
    """
    if long_report is None:
        long_report = len(params) > 3
    report_id = REPORT_LONG if long_report else REPORT_SHORT
    size = REPORT_SIZES[report_id]
    if len(params) > size - 4:
        raise ValueError(f"{len(params)} parameter bytes do not fit in a {size}-byte report")
    frame = bytearray(size)
    frame[0] = report_id
    frame[1] = device_index & 0xFF
    frame[2] = feature_index & 0xFF
    frame[3] = function_byte(function, sw_id)
    frame[4 : 4 + len(params)] = params
    return bytes(frame)


def is_hidpp_frame(frame: bytes) -> bool:
    return len(frame) >= 4 and frame[0] in REPORT_SIZES


def is_unsolicited(frame: bytes) -> bool:
    """Is this a HID++ 2.0 event rather than a reply to something we asked?

    Replies echo the software id we sent; events always carry swId 0. Error
    frames are excluded -- an error is a reply, just an unhappy one.
    """
    if len(frame) < 4 or frame[2] in (ERROR_HIDPP20, ERROR_HIDPP10):
        return False
    return (frame[3] & 0x0F) == 0


def is_error_for(frame: bytes, device_index: int, feature_index: int, func_byte: int) -> int | None:
    """Return the protocol version (10 or 20) if `frame` is an error for this request.

    HID++ 2.0 error: ``[id, dev, 0xFF, featureIndex, funcByte, code]``
    HID++ 1.0 error: ``[id, dev, 0x8F, subId, address, code]``
    """
    if len(frame) < 6 or frame[1] != device_index:
        return None
    if frame[2] == ERROR_HIDPP20 and frame[3] == feature_index and frame[4] == func_byte:
        return 20
    if frame[2] == ERROR_HIDPP10 and frame[3] == feature_index and frame[4] == func_byte:
        return 10
    return None


def is_response_to(frame: bytes, device_index: int, feature_index: int, func_byte: int) -> bool:
    return (
        len(frame) >= 4
        and frame[1] == device_index
        and frame[2] == feature_index
        and frame[3] == func_byte
    )


def error_from(frame: bytes, protocol: int, context: str = "") -> HidppError:
    return HidppError(frame[5], protocol, context)


def decode_host_platform(payload: bytes) -> dict:
    """Decode a 0x4531 getHostPlatform record.

    Tolerates a short reply -- firmware that half-implements the feature answers
    with the header alone, and that must read as "unknown", not raise.
    """

    def at(offset: int) -> int | None:
        return payload[offset] if len(payload) > offset else None

    status, source = at(1), at(3)
    return {
        "host_index": at(0),
        "status": status,
        "status_name": HOST_STATUSES.get(status, "?") if status is not None else "?",
        "platform_index": at(2),
        "platform_source": source,
        "source_name": PLATFORM_SOURCES.get(source, f"source {source}")
        if source is not None
        else "?",
        "raw": bytes(payload).hex(),
    }


def describe_host_platform(record: dict) -> str:
    """One-line reading of :func:`decode_host_platform`, for logs."""
    return (
        f"host={record['host_index']} {record['status_name']} "
        f"platform={record['platform_index']} set-by={record['source_name']}"
    )


def is_connection_notification(frame: bytes) -> bool:
    """Is this really a HID++ 1.0 device-connection notification?

    Byte 2 is the sub-id on a HID++ 1.0 notification but the *featureIndex* on a
    HID++ 2.0 frame, so testing it alone misreads any 2.0 reply whose feature
    happens to sit at index 0x41. Notifications are short reports addressed to a
    receiver slot, which is enough to tell the two apart.
    """
    return (
        len(frame) >= 5
        and frame[0] == REPORT_SHORT
        and frame[2] == NOTIF_DEVICE_CONNECTION
        and frame[1] in RECEIVER_SLOTS
    )


def connection_flags(frame: bytes) -> tuple[bool, bool]:
    """``(link_established, encrypted)`` from a 0x41 notification."""
    flags = frame[4] if len(frame) > 4 else 0
    return (not flags & NOTIF_LINK_NOT_ESTABLISHED, bool(flags & NOTIF_LINK_ENCRYPTED))


def describe_frame(frame: bytes) -> str:
    """A one-line human reading of a frame, for the trace.

    Deliberately structural: this module knows feature *ids*, but a frame carries a
    per-device feature *index*, so naming anything beyond the root feature would be
    a guess. The semantic detail (which platform, set by whom) is logged by the
    caller that has the context.
    """
    if len(frame) < 4:
        return f"runt {bytes(frame).hex()}"
    size = {REPORT_SHORT: "short", REPORT_LONG: "long"}.get(frame[0], f"id0x{frame[0]:02X}")
    device = frame[1]

    if frame[2] in (ERROR_HIDPP20, ERROR_HIDPP10) and len(frame) >= 6:
        protocol = 20 if frame[2] == ERROR_HIDPP20 else 10
        table = HIDPP20_ERRORS if protocol == 20 else HIDPP10_ERRORS
        name = table.get(frame[5], f"error {frame[5]}")
        return (
            f"{size} dev{device} ERROR hidpp{protocol // 10}.0 feat0x{frame[3]:02X} "
            f"fn{frame[4] >> 4} sw0x{frame[4] & 0x0F:X}: {name} (0x{frame[5]:02X})"
        )

    if is_connection_notification(frame):
        established, encrypted = connection_flags(frame)
        return (
            f"{size} dev{device} 0x41 device-connection "
            f"link={'up' if established else 'DOWN'} "
            f"{'encrypted' if encrypted else 'plain'}"
        )

    feature, function, sw_id = frame[2], frame[3] >> 4, frame[3] & 0x0F
    if feature == FEATURE_ROOT:
        label = {
            ROOT_GET_FEATURE: "root.getFeature",
            ROOT_GET_PROTOCOL_VERSION: "root.ping",
        }.get(function, f"root.fn{function}")
    else:
        label = f"feat0x{feature:02X}.fn{function}"
    origin = "notif" if sw_id == 0 else f"sw0x{sw_id:X}"
    return f"{size} dev{device} {label} {origin} [{bytes(frame[4:]).hex()}]"
