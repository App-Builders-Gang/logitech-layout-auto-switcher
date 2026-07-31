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

#: Addresses the receiver itself, and also a device connected directly over USB
#: cable or Bluetooth (there is no receiver in that path).
INDEX_DIRECT = 0xFF
#: Receiver slots.
RECEIVER_SLOTS = range(1, 7)

#: HID++ 1.0 sub-id emitted by a receiver when a paired device connects or wakes.
NOTIF_DEVICE_CONNECTION = 0x41

ERROR_HIDPP20 = 0xFF
ERROR_HIDPP10 = 0x8F

FEATURE_ROOT = 0x0000
FEATURE_FEATURE_SET = 0x0001
FEATURE_DEVICE_NAME = 0x0005
FEATURE_DUALPLATFORM = 0x4530
FEATURE_MULTIPLATFORM = 0x4531

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

#: Address the platform of whichever host is currently active.
HOST_CURRENT = 0xFF

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
