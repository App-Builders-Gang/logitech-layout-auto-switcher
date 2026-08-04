"""What the *host* thinks the keyboard is, and who else is talking to it.

logiswitch sets the keyboard's firmware platform. That is only one of the two
things deciding which character a keypress produces -- the host's own input source
decides the rest, and nothing here manages it. A machine sitting in a non-Latin
layout types an entirely different alphabet no matter what the firmware is set to,
and the daemon had no way to see that: it would report success while the wrong
script appeared on screen.

So this module reads the host's current keyboard language and notes whether other
Logitech software is running, on macOS and Windows alike. Everything is read-only
and best-effort, and returns :data:`UNKNOWN` rather than raising -- a diagnostic
that can break the thing it is diagnosing is worse than no diagnostic.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import subprocess
from typing import Callable

from .paths import is_macos, is_windows

log = logging.getLogger(__name__)

UNKNOWN = "unknown"

kCFStringEncodingUTF8 = 0x08000100

#: Scripts whose layout produces something unreadable when you meant Latin. Named
#: rather than inferred: this is a *hint* for a human reading a report, never a
#: decision, so a short honest list beats a clever incomplete one.
NON_LATIN_MARKERS = {
    "arabic": "Arabic",
    "hebrew": "Hebrew",
    "russian": "Cyrillic",
    "ukrainian": "Cyrillic",
    "bulgarian": "Cyrillic",
    "serbian": "Cyrillic",
    "macedonian": "Cyrillic",
    "greek": "Greek",
    "thai": "Thai",
    "persian": "Persian",
    "farsi": "Persian",
    "urdu": "Urdu",
    "devanagari": "Devanagari",
    "hindi": "Devanagari",
    "bengali": "Bengali",
    "georgian": "Georgian",
    "armenian": "Armenian",
    "tibetan": "Tibetan",
    "khmer": "Khmer",
    "lao": "Lao",
    "myanmar": "Myanmar",
    "sinhala": "Sinhala",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "kannada": "Kannada",
    "malayalam": "Malayalam",
    "gujarati": "Gujarati",
    "gurmukhi": "Gurmukhi",
    "oriya": "Oriya",
    "amharic": "Ethiopic",
    "cherokee": "Cherokee",
    "inuktitut": "Inuktitut",
    "maldivian": "Thaana",
    # Input methods rather than layouts, named by their Apple source ids.
    "scim": "Han",
    "tcim": "Han",
    "pinyin": "Han",
    "zhuyin": "Han",
    "cangjie": "Han",
    "shuangpin": "Han",
    "wubi": "Han",
    "kotoeri": "Japanese",
    "japanese": "Japanese",
    "korean": "Hangul",
    "hangul": "Hangul",
}

#: Apple input-method ids all share this prefix. An IME being active does not by
#: itself mean non-Latin output -- Vietnamese Telex is Latin -- so an unrecognised
#: one is reported as "an input method", for a person to judge.
IME_PREFIX = "com.apple.inputmethod."

#: Two-letter language codes carrying the same meaning, for the Windows locale form.
NON_LATIN_LANGUAGE_CODES = {
    "ar": "Arabic",
    "he": "Hebrew",
    "iw": "Hebrew",
    "ru": "Cyrillic",
    "uk": "Cyrillic",
    "bg": "Cyrillic",
    "mk": "Cyrillic",
    "el": "Greek",
    "th": "Thai",
    "fa": "Persian",
    "ur": "Urdu",
    "hi": "Devanagari",
    "bn": "Bengali",
    "ka": "Georgian",
    "hy": "Armenian",
    "bo": "Tibetan",
    "km": "Khmer",
    "lo": "Lao",
    "my": "Myanmar",
    "si": "Sinhala",
    "ta": "Tamil",
    "te": "Telugu",
    "kn": "Kannada",
    "ml": "Malayalam",
    "gu": "Gujarati",
    "pa": "Gurmukhi",
    "or": "Oriya",
    "am": "Ethiopic",
    "zh": "Han",
    "ja": "Japanese",
    "ko": "Hangul",
    "dv": "Thaana",
}

#: Fragments of process names belonging to software that drives the same HID++
#: collection and sets its own host platform.
#:
#: Deliberately not the bare string "logi": ``login``, ``logind``, ``loginwindow``
#: and ``LoginUserService`` are all running on a normal Mac and none of them has
#: anything to do with Logitech.
COMPETING_MARKERS = (
    "logio",  # logioptionsplus_agent, logioptionsplus_updater, LogiOptions
    "logimgr",
    "logitech",
    "logirightsight",
    "lghub",  # Logitech G HUB
    "solaar",  # the Linux HID++ manager
    "openlogi",  # the Rust Options+ replacement; its README says to quit Options+ too
    "logid",  # logiops' daemon
)


# -- host keyboard language ---------------------------------------------------


def input_source() -> str:
    """The host's active keyboard input source, or :data:`UNKNOWN`."""
    try:
        if is_macos():
            return _macos_input_source()
        if is_windows():
            return _windows_input_source()
    except Exception as exc:  # pragma: no cover - platform specific
        log.debug("could not read the host input source: %s", exc)
    return UNKNOWN


def non_latin_script(source: str) -> str | None:
    """Name the script if `source` looks like it types something other than Latin.

    A hint for a human, not a verdict: an unrecognised layout reads as "no idea",
    which is why this returns ``None`` rather than ``False``.
    """
    if not source or source == UNKNOWN:
        return None
    lowered = source.lower()
    for marker, script in NON_LATIN_MARKERS.items():
        if marker in lowered:
            return script
    # Windows reports a locale name such as "ar-EG"; take the language subtag.
    language = lowered.split("-", 1)[0].split("_", 1)[0].strip()
    if language in NON_LATIN_LANGUAGE_CODES:
        return NON_LATIN_LANGUAGE_CODES[language]
    if lowered.startswith(IME_PREFIX):
        return "an input method"
    return None


def _load(framework: str, path: str) -> ctypes.CDLL:
    try:
        return ctypes.cdll.LoadLibrary(path)
    except OSError:  # pragma: no cover - depends on the SDK layout
        found = ctypes.util.find_library(framework)
        if not found:
            raise
        return ctypes.cdll.LoadLibrary(found)


def _macos_input_source() -> str:  # pragma: no cover - needs macOS frameworks
    cf = _load(
        "CoreFoundation", "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    carbon = _load("Carbon", "/System/Library/Frameworks/Carbon.framework/Carbon")

    # Explicit restypes or ctypes truncates the 64-bit pointers to int.
    carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
    carbon.TISCopyCurrentKeyboardInputSource.argtypes = []
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    source = carbon.TISCopyCurrentKeyboardInputSource()
    if not source:
        return UNKNOWN
    try:
        prop = ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID")
        return _cfstring(cf, carbon.TISGetInputSourceProperty(source, prop))
    finally:
        cf.CFRelease(ctypes.c_void_p(source))


def _cfstring(cf: ctypes.CDLL, ref: int | None) -> str:  # pragma: no cover - macOS only
    if not ref:
        return UNKNOWN
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    direct = cf.CFStringGetCStringPtr(ctypes.c_void_p(ref), kCFStringEncodingUTF8)
    if direct:
        return direct.decode("utf-8", "replace")
    # No inline buffer available; ask for a copy instead.
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    size = cf.CFStringGetLength(ctypes.c_void_p(ref)) * 4 + 1
    buffer = ctypes.create_string_buffer(size)
    if cf.CFStringGetCString(ctypes.c_void_p(ref), buffer, size, kCFStringEncodingUTF8):
        return buffer.value.decode("utf-8", "replace")
    return UNKNOWN


def _windows_input_source() -> str:  # pragma: no cover - needs Windows
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # GetKeyboardLayout(0) answers for the *calling* thread, which for a background
    # agent is not the thread the user is typing into. Ask the foreground window's
    # thread instead -- that is the layout actually producing characters.
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.GetKeyboardLayout.argtypes = [ctypes.c_ulong]

    window = user32.GetForegroundWindow()
    thread_id = user32.GetWindowThreadProcessId(window, None) if window else 0
    layout = user32.GetKeyboardLayout(thread_id) or 0
    language_id = layout & 0xFFFF
    buffer = ctypes.create_unicode_buffer(85)
    if kernel32.LCIDToLocaleName(language_id, buffer, 85, 0) and buffer.value:
        return f"{buffer.value} (0x{language_id:04X})"
    return f"0x{language_id:04X}"


# -- other software on the same collection ------------------------------------


def competing_software(runner: Callable[[list[str]], str] | None = None) -> list[str]:
    """Names of running processes known to fight us for the host platform.

    Shells out, so it is called at start-up and by ``doctor`` -- never per event.
    The agent has warned about Logi Options+ for a while without ever checking
    whether it was actually there; this is what makes that claim honest.
    """
    run = runner or _run
    try:
        if is_windows():
            output = run(["tasklist", "/FO", "CSV", "/NH"])
        else:
            output = run(["ps", "-Ac", "-o", "comm="])
    except Exception as exc:
        log.debug("could not list processes: %s", exc)
        return []
    return _collapse_helpers(sorted({n for n in _process_names(output) if _is_competitor(n)}))


def _collapse_helpers(names: list[str]) -> list[str]:
    """Fold an app's helper processes into the app.

    Options+ is an Electron app: opening its window adds ``logioptionsplus Helper``,
    ``... Helper (GPU)`` and ``... Helper (Renderer)`` beside ``logioptionsplus``.
    Listing all of them turns one program into seven, and the line that says which
    software is running here -- read by a human trying to work out what is competing
    for their keyboard -- becomes unreadable at exactly the moment it matters.

    A name is a helper if a shorter retained name is a prefix of it at a word
    boundary. That keeps distinct products apart: ``logioptionsplus_updater`` is not
    ``logioptionsplus`` plus a space, so it survives on its own.
    """
    kept: list[str] = []
    for name in names:  # sorted, so any parent is already in `kept`
        if any(name.startswith(f"{parent} ") for parent in kept):
            continue
        kept.append(name)
    return kept


def _process_names(output: str) -> list[str]:
    """Process names out of ``ps -o comm=`` or ``tasklist /FO CSV /NH`` output."""
    names = []
    for line in output.splitlines():
        entry = line.strip()
        if not entry:
            continue
        if entry.startswith('"'):  # tasklist CSV: "name.exe","pid",...
            entry = entry.split('","', 1)[0].lstrip('"')
        # ps may print a full path for anything not in a standard location.
        names.append(entry.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    return names


def _is_competitor(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in COMPETING_MARKERS)


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=5.0, check=False)
    return completed.stdout or ""


# -- why can't we open the device? --------------------------------------------


def cannot_open_hint(agent_running: bool = False) -> str | None:
    """Explain an endpoint that enumerates but refuses to open.

    Order matters. The commonest cause by far is the most boring one: our own
    background agent already has the device, and only one process can hold it. If
    that is true, saying "grant Input Monitoring" sends someone to change a system
    setting that was never the problem -- the same class of confident misdiagnosis
    this whole diagnostic exists to stop.
    """
    if agent_running:
        return (
            "the receiver is present but will not open, because the logiswitch agent "
            "already has it -- only one process can. That is normal. For a full device "
            "dump, stop the agent first, then re-run:\n"
            "    macOS:   launchctl bootout gui/$(id -u)/com.appbuildersgang.logiswitch\n"
            "    Windows: schtasks /End /TN LogiSwitch\n"
            "  ...and start it again afterwards with `logiswitch install`."
        )
    if is_macos():
        return (
            "the receiver is present but will not open. On macOS this is the Input "
            "Monitoring permission: grant it to whichever program is running "
            "logiswitch (your terminal, or the installed agent) under System "
            "Settings > Privacy & Security > Input Monitoring, then try again."
        )
    if is_windows():
        return (
            "the receiver is present but will not open. Another process may hold it "
            "exclusively. Logi Options+ normally shares it happily -- it and logiswitch "
            "run side by side on the same collection -- so check for a second copy of "
            "logiswitch, or another HID++ tool, before quitting it."
        )
    return (
        "the receiver is present but will not open. On Linux this is usually udev "
        "permissions on the hidraw node."
    )


# -- the whole picture --------------------------------------------------------


def host_summary() -> dict:
    """Everything about the host that bears on which character a key produces."""
    source = input_source()
    return {
        "input_source": source,
        "non_latin_script": non_latin_script(source),
        "competing_software": competing_software(),
    }


def describe_host(summary: dict | None = None) -> str:
    """One-line rendering for the agent log."""
    data = summary if summary is not None else host_summary()
    parts = [f"input={data['input_source']}"]
    if data.get("non_latin_script"):
        parts.append(f"script={data['non_latin_script']}")
    if data.get("competing_software"):
        parts.append(f"also-running={'/'.join(data['competing_software'])}")
    return " ".join(parts)
