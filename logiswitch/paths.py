"""Per-platform locations and logging setup."""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import platform
import sys
from pathlib import Path

APP_NAME = "logiswitch"
SERVICE_LABEL = "com.appbuildersgang.logiswitch"
#: v1 registered itself under this name; installers remove it on upgrade.
LEGACY_TASK_NAME = "MXSwitch"
#: Set in the LaunchAgent plist. Tells the agent its stderr is already being captured,
#: so it should not also echo to the console.
MANAGED_ENV_VAR = "LOGISWITCH_MANAGED"


def is_windows() -> bool:
    return platform.system() == "Windows"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def default_target_os() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    return "linux"


def data_dir() -> Path:
    if is_windows():
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LogiSwitch"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / APP_NAME


def log_path() -> Path:
    if is_macos():
        return Path.home() / "Library" / "Logs" / f"{APP_NAME}.log"
    return data_dir() / f"{APP_NAME}.log"


def launchd_stdio_path() -> Path:
    """Where launchd dumps the agent's raw stdout/stderr.

    Deliberately not :func:`log_path`: the agent logs there itself, and having launchd
    redirect the same stream into the same file writes every line twice. This one holds
    only what escapes the logger -- tracebacks from a failed import, mostly.
    """
    return log_path().with_name(f"{APP_NAME}.launchd.log")


def is_managed() -> bool:
    """True when running as the installed background agent."""
    return os.environ.get(MANAGED_ENV_VAR) == "1"


def state_path() -> Path:
    return data_dir() / "state.json"


def python_executable(windowless: bool = False) -> Path:
    """Interpreter to launch the background agent with.

    On Windows the console-less ``pythonw.exe`` avoids a flashing window at logon.
    """
    executable = Path(sys.executable)
    if windowless and is_windows():
        candidate = executable.with_name("pythonw.exe")
        if candidate.exists():
            return candidate
    return executable


def setup_logging(
    verbose: bool = False, log_file: Path | None = None, console: bool = True
) -> None:
    root = logging.getLogger("logiswitch")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Close before dropping: clearing the list alone orphans the rotating handler's
    # open file, leaking a descriptor every time logging is reconfigured.
    for existing in root.handlers[:]:
        # pragma: no cover - a handler that cannot close is not fatal
        with contextlib.suppress(Exception):
            existing.close()
    root.handlers.clear()
    root.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    if console:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
            )
            rotating.setFormatter(formatter)
            root.addHandler(rotating)
        except OSError as exc:  # pragma: no cover - read-only home, etc.
            root.warning("cannot write %s: %s", log_file, exc)

    if not root.handlers:
        root.addHandler(logging.NullHandler())
