"""Which modifier keys the host currently believes are held down.

This exists because of a failure mode that is invisible from everywhere else in
this codebase: **switching the platform remaps the bottom row**. On a macOS
platform the key left of the space bar is Command; on a Windows platform the same
physical key is Alt. If the platform changes while that key is held, the host saw
the key *press* under one mapping and will see the *release* under the other -- so
the modifier it registered as down is never released, and it sticks. The user is
then left holding an invisible Command key.

logiswitch never sees keystrokes and cannot, but it does not need to: both
platforms will report the *current modifier state* to any process that asks, with
no permission at all. macOS answers ``CGEventSourceFlagsState``, which reads a
snapshot rather than tapping the event stream (a tap would need Accessibility);
Windows answers ``GetAsyncKeyState``. That is enough to do the two things that
matter -- refuse to remap the keyboard mid-chord, and notice when a modifier has
been stuck down for longer than any human holds one.

Read-only, best-effort, and silent on failure: this is a safety check, and a safety
check that raises is worse than none.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

from .platform import is_macos, is_windows

log = logging.getLogger(__name__)

#: ``CGEventSourceStateCombinedSessionState`` -- what the session as a whole thinks
#: is held, which is what actually decides the character a key produces.
_COMBINED_SESSION_STATE = 0

#: CGEventFlags masks for the modifiers a platform switch can strand.
#:
#: Fn is deliberately absent. It is not on the bottom row that a platform change
#: remaps, so it cannot be stranded by one -- and macOS reports it set for reasons
#: that have nothing to do with anyone holding a key (function-row and media keys
#: carry the flag). Including it made the agent believe Fn had been held for thirty
#: seconds and defer real corrections twice in as many minutes, on a keyboard that
#: nobody was touching.
_CG_FLAGS = {
    "shift": 0x00020000,
    "control": 0x00040000,
    "option": 0x00080000,
    "command": 0x00100000,
}

#: Virtual-key codes, paired so a left/right key reports under one name.
_VK_MODIFIERS = {
    "shift": (0xA0, 0xA1),  # VK_LSHIFT, VK_RSHIFT
    "control": (0xA2, 0xA3),  # VK_LCONTROL, VK_RCONTROL
    "option": (0xA4, 0xA5),  # VK_LMENU, VK_RMENU  (Alt)
    "command": (0x5B, 0x5C),  # VK_LWIN, VK_RWIN
}

#: Set by GetAsyncKeyState when the key is down *now* (as opposed to "was pressed
#: since the last call", which is the low bit and is not what we want).
_KEY_DOWN = 0x8000

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
        library.CGEventSourceFlagsState.restype = ctypes.c_uint64
        library.CGEventSourceFlagsState.argtypes = [ctypes.c_uint32]
        _cg = library
    except OSError as exc:
        log.debug("cannot read modifier state: %s", exc)
    return _cg


def _macos_modifiers() -> set[str]:  # pragma: no cover - needs macOS frameworks
    library = _application_services()
    if library is None:
        return set()
    flags = library.CGEventSourceFlagsState(_COMBINED_SESSION_STATE)
    return {name for name, mask in _CG_FLAGS.items() if flags & mask}


def _windows_modifiers() -> set[str]:  # pragma: no cover - needs Windows
    user32 = ctypes.windll.user32
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    held = set()
    for name, codes in _VK_MODIFIERS.items():
        if any(user32.GetAsyncKeyState(code) & _KEY_DOWN for code in codes):
            held.add(name)
    return held


def modifiers_held() -> set[str]:
    """Modifier names the host currently reports as down. Empty if unknowable."""
    try:
        if is_macos():
            return _macos_modifiers()
        if is_windows():
            return _windows_modifiers()
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("could not read modifier state: %s", exc)
    return set()


def available() -> bool:
    """Can this platform answer at all? Used to decide whether to guard switches."""
    if is_macos():
        return _application_services() is not None
    return is_windows()


def describe(modifiers: set[str]) -> str:
    return "+".join(sorted(modifiers)) if modifiers else "none"
