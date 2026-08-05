"""How long since this machine was actually used.

This is how several machines sharing one keyboard avoid fighting over it.

A KVM puts one keyboard in front of N computers, each running its own copy of this
agent, each wanting its own OS layout. From the keyboard's point of view they are a
single host with a single platform slot, so they cannot all have their way -- and
with no network between them they cannot negotiate either.

They do not need to. A keyboard delivers keystrokes to exactly one machine at a
time, so at most one of them can observe recent typing. Each agent asks its own OS
"how long since somebody used me", and the one being typed on takes the keyboard
while the rest stand down. No configuration, no protocol, and it works for any
number of machines.

Read-only and permission-free on both platforms: macOS answers
``CGEventSourceSecondsSinceLastEventType`` -- a snapshot, not an event tap, so no
Accessibility or Input Monitoring prompt -- and Windows answers ``GetLastInputInfo``.
Returns ``None`` rather than guessing when it cannot tell, and the caller treats that
as "arbitration unavailable" instead of as "idle".
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

from .platform import is_macos, is_windows

log = logging.getLogger(__name__)

#: ``kCGEventSourceStateHIDSystemState`` -- the hardware event stream, which is what
#: "has anyone touched this machine" means. The combined session state would also
#: count events synthesised by software.
_HID_SYSTEM_STATE = 1

#: ``kCGEventKeyDown``. Preferred over "any input" because a keyboard is what is
#: being contended for: a mouse jiggling on one machine should not let it claim a
#: keyboard that is busy on another.
_EVENT_KEY_DOWN = 10
#: ``kCGAnyInputEventType``, the fallback when no key has ever been pressed on this
#: login session (a fresh boot reports a huge idle time for key-down alone).
_ANY_INPUT = 0xFFFFFFFF

_cg: ctypes.CDLL | None = None
_cg_loaded = False


def _application_services() -> ctypes.CDLL | None:  # pragma: no cover - macOS only
    global _cg, _cg_loaded
    if _cg_loaded:
        return _cg
    _cg_loaded = True
    try:
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        library = ctypes.cdll.LoadLibrary(path)
        # Without explicit types ctypes assumes int, and this one returns a double.
        library.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        library.CGEventSourceSecondsSinceLastEventType.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        _cg = library
    except OSError as exc:
        log.debug("cannot read input activity: %s", exc)
    return _cg


def _macos_idle() -> float | None:  # pragma: no cover - needs macOS frameworks
    library = _application_services()
    if library is None:
        return None
    typing = library.CGEventSourceSecondsSinceLastEventType(_HID_SYSTEM_STATE, _EVENT_KEY_DOWN)
    anything = library.CGEventSourceSecondsSinceLastEventType(_HID_SYSTEM_STATE, _ANY_INPUT)
    # Nothing has been typed here yet this session; fall back to any input rather
    # than reporting a machine as idle for days when someone is using it.
    return float(min(typing, anything)) if typing >= 0 else float(anything)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def _windows_idle() -> float | None:  # pragma: no cover - needs Windows
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    # Both are milliseconds since boot and both wrap at 2^32; the subtraction is
    # correct across a wrap only if it is done in 32-bit arithmetic.
    elapsed = (kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed / 1000.0


def seconds_since_input() -> float | None:
    """Seconds since this machine last saw input, or None if it cannot be known."""
    try:
        if is_macos():
            return _macos_idle()
        if is_windows():
            return _windows_idle()
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("could not read input activity: %s", exc)
    return None


def available() -> bool:
    """Can this platform arbitrate at all?

    Where it cannot, the caller must not gate on activity -- a machine that can never
    prove it is being used would otherwise stand down forever.
    """
    return seconds_since_input() is not None


def describe(idle: float | None) -> str:
    if idle is None:
        return "unknown"
    if idle < 1.0:
        return "in use now"
    return f"idle {idle:.0f}s"
