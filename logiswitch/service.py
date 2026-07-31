"""Register the agent to start at logon.

Kept in Python rather than in the shell installers so the logic is one
implementation, testable and identical on both platforms.

Windows: a Scheduled Task at logon, running as the interactive user. No elevation
is needed -- HID++ access does not require admin.
macOS: a launchd LaunchAgent in the user's own ``~/Library/LaunchAgents``.
"""

from __future__ import annotations

import getpass
import logging
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .paths import (
    APP_NAME,
    LEGACY_TASK_NAME,
    MANAGED_ENV_VAR,
    SERVICE_LABEL,
    is_macos,
    is_windows,
    launchd_stdio_path,
    log_path,
    python_executable,
)

log = logging.getLogger(__name__)

TASK_NAME = "LogiSwitch"

#: How long to wait for launchd to finish tearing a booted-out job down. ``bootout``
#: returns as soon as SIGTERM is delivered, but the label stays registered in the
#: domain until the process is really gone -- and bootstrapping a still-registered
#: label fails with EIO.
UNLOAD_TIMEOUT = 10.0
UNLOAD_POLL_INTERVAL = 0.25
#: Sleep before each bootstrap attempt. The first is free; the rest cover a teardown
#: that outlives UNLOAD_TIMEOUT or a domain that is momentarily busy.
BOOTSTRAP_DELAYS = (0.0, 0.5, 1.0, 2.0, 4.0)

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keep Logitech keyboard layouts matched to this host when a KVM switches.</Description>
    <URI>\\{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>99</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


class ServiceError(RuntimeError):
    pass


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    log.debug("running %s", args)
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise ServiceError(
            f"{args[0]} failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
        )
    return result


def _current_user() -> str:
    if is_windows():
        domain = os.environ.get("USERDOMAIN")
        user = os.environ.get("USERNAME") or getpass.getuser()
        return f"{domain}\\{user}" if domain else user
    return getpass.getuser()


def _agent_command(target_os: str | None) -> tuple[str, str]:
    executable = python_executable(windowless=True)
    arguments = f"-m {APP_NAME} watch"
    if target_os:
        arguments += f" --os {target_os}"
    return str(executable), arguments


# -- Windows ------------------------------------------------------------------


def _windows_install(target_os: str | None) -> str:
    command, arguments = _agent_command(target_os)
    xml = _TASK_XML.format(
        task_name=TASK_NAME,
        user=_current_user(),
        command=command,
        arguments=arguments,
    )
    # schtasks wants UTF-16 for /XML input.
    handle, name = tempfile.mkstemp(suffix=".xml")
    os.close(handle)
    temp = Path(name)
    try:
        temp.write_bytes(xml.encode("utf-16"))
        # /Create /F replaces the registration but leaves a running instance alone,
        # so upgrading over a live agent kept the previous build resident until the
        # next logon -- it went on logging the old settings while the new code sat
        # unused on disk. End it first, then start the replacement.
        _run(["schtasks", "/End", "/TN", TASK_NAME], check=False)
        _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(temp), "/F"])
    finally:
        temp.unlink(missing_ok=True)
    _run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)
    return f"scheduled task '{TASK_NAME}'"


def _windows_uninstall() -> list[str]:
    removed = []
    for name in (TASK_NAME, LEGACY_TASK_NAME):
        result = _run(["schtasks", "/Query", "/TN", name], check=False)
        if result.returncode != 0:
            continue
        _run(["schtasks", "/End", "/TN", name], check=False)
        _run(["schtasks", "/Delete", "/TN", name, "/F"])
        removed.append(name)
    return removed


def _windows_status() -> dict:
    result = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], check=False)
    if result.returncode != 0:
        return {"installed": False}
    status = ""
    for line in result.stdout.splitlines():
        if line.lower().startswith("status:"):
            status = line.split(":", 1)[1].strip()
    return {"installed": True, "state": status or "unknown"}


# -- macOS --------------------------------------------------------------------


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def _launchctl_domain() -> str:
    # os.getuid does not exist on Windows; this function is only reached on macOS,
    # and getattr keeps it type-checkable whichever platform mypy assumes.
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - unreachable on macOS
        raise ServiceError("launchctl domains are a macOS concept")
    return f"gui/{getuid()}"


def _service_print(target: str) -> subprocess.CompletedProcess:
    return _run(["launchctl", "print", target], check=False)


def _service_registered(target: str) -> bool:
    """Is the label still known to the domain? Registered != running."""
    return _service_print(target).returncode == 0


def _wait_until_unloaded(target: str, timeout: float = UNLOAD_TIMEOUT) -> bool:
    """Poll until launchd has dropped ``target``. False if it is still there."""
    deadline = time.monotonic() + timeout
    while _service_registered(target):
        if time.monotonic() >= deadline:
            return False
        time.sleep(UNLOAD_POLL_INTERVAL)
    return True


def _bootstrap(domain: str, target: str, path: Path) -> None:
    """Bootstrap the plist, retrying while launchd is still letting go of the label."""
    last: subprocess.CompletedProcess | None = None
    for delay in BOOTSTRAP_DELAYS:
        if delay:
            time.sleep(delay)
        last = _run(["launchctl", "bootstrap", domain, str(path)], check=False)
        if last.returncode == 0:
            return
        # A concurrent installer -- or launchd itself, reacting to the plist -- may
        # have registered the label between our bootout and this attempt. Loaded is
        # loaded; that is the outcome we wanted.
        if _service_registered(target):
            return
    detail = (last.stderr or last.stdout).strip() if last else ""
    raise ServiceError(
        f"launchctl bootstrap failed after {len(BOOTSTRAP_DELAYS)} attempts: {detail}\n"
        f"    This is a per-user LaunchAgent -- do NOT re-run as root.\n"
        f"    Try:  launchctl bootout {target}\n"
        f"    then re-run the install. 'launchctl print {domain}' lists what is loaded."
    )


def _macos_install(target_os: str | None) -> str:
    executable = str(python_executable())
    arguments = [executable, "-m", APP_NAME, "watch"]
    if target_os:
        arguments += ["--os", target_os]
    # launchd's own stdio capture goes to a separate file: the agent writes its log
    # itself, and pointing both at one path doubles every line. This one only ever
    # holds crashes that happen before logging is configured.
    stdio = str(launchd_stdio_path())

    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardErrorPath": stdio,
        "StandardOutPath": stdio,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", MANAGED_ENV_VAR: "1"},
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    Path(stdio).parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle)

    domain = _launchctl_domain()
    target = f"{domain}/{SERVICE_LABEL}"
    # A label that was ever `launchctl disable`d keeps a sticky override that fails
    # bootstrap with the same EIO; enabling is idempotent and harmless otherwise.
    _run(["launchctl", "enable", target], check=False)
    _run(["launchctl", "bootout", target], check=False)
    if not _wait_until_unloaded(target):
        log.warning("%s is still registered after bootout; bootstrapping anyway", target)
    _bootstrap(domain, target, path)
    _run(["launchctl", "kickstart", "-k", target], check=False)
    return f"launch agent '{SERVICE_LABEL}'"


def _macos_uninstall() -> list[str]:
    removed = []
    domain = _launchctl_domain()
    path = _plist_path()
    if path.exists():
        _run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"], check=False)
        path.unlink()
        removed.append(SERVICE_LABEL)
    legacy = Path.home() / "Library" / "LaunchAgents" / "com.abd3lraouf.mxswitch.plist"
    if legacy.exists():
        _run(["launchctl", "bootout", f"{domain}/com.abd3lraouf.mxswitch"], check=False)
        legacy.unlink()
        removed.append("com.abd3lraouf.mxswitch")
    return removed


def _macos_status() -> dict:
    if not _plist_path().exists():
        return {"installed": False}
    result = _service_print(f"{_launchctl_domain()}/{SERVICE_LABEL}")
    if result.returncode != 0:
        return {"installed": True, "state": "not loaded"}
    state = "running"
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            state = stripped.split("=", 1)[1].strip()
            break
    return {"installed": True, "state": state}


# -- public -------------------------------------------------------------------


def install(target_os: str | None = None) -> str:
    """Register the background service. Does not touch PATH (see ensure_on_path)."""
    if is_windows():
        return _windows_install(target_os)
    if is_macos():
        return _macos_install(target_os)
    raise ServiceError(
        "automatic service installation is only implemented for Windows and macOS; "
        f"run '{APP_NAME} watch' from your own supervisor instead"
    )


# -- put the command on PATH --------------------------------------------------


def _entry_point_dir() -> Path:
    """The directory that holds the runnable ``logiswitch`` for this install."""
    return Path(sys.prefix) / ("Scripts" if is_windows() else "bin")


def _windows_path_parts() -> tuple[list[str], int]:
    """The user's PATH entries and their registry type, or ([], REG_EXPAND_SZ)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            current, reg_type = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return [], winreg.REG_EXPAND_SZ
    return [p.strip() for p in current.split(";")], reg_type


def _add_to_list(parts: list[str], entry: str) -> tuple[list[str], bool]:
    """Append ``entry`` to ``parts`` if not already present (case-insensitive)."""
    lowered = {p.lower() for p in parts}
    if entry.lower() in lowered:
        return parts, False
    return [*parts, entry], True


def _windows_ensure_on_path() -> bool:
    """Add the venv's Scripts dir to the user PATH. Returns whether it changed."""
    import winreg

    target = str(_entry_point_dir())
    parts, reg_type = _windows_path_parts()
    parts, changed = _add_to_list(parts, target)
    if not changed:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, "Path", 0, reg_type or winreg.REG_EXPAND_SZ, ";".join(parts))
    except OSError as exc:  # pragma: no cover - needs a real registry to fail
        raise ServiceError(f"could not update the user PATH: {exc}") from exc
    # winreg writes silently; broadcast WM_SETTINGCHANGE so Explorer (and the
    # terminals it launches) pick up the new PATH without a logoff. This is what
    # `setx` and [Environment]::SetEnvironmentVariable do internally.
    _broadcast_environment_change()
    return True


def _broadcast_environment_change() -> None:
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    # SendMessageTimeoutW returns 0 on timeout; the return value is irrelevant --
    # a terminal that did not listen still reads the registry at launch.
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment", 0x2, 1000, None
    )


def _macos_ensure_on_path() -> bool:
    """Symlink ``logiswitch`` into ~/.local/bin. Returns whether a link was (re)made."""
    target = _entry_point_dir() / APP_NAME
    if not target.exists():
        return False
    bin_dir = Path.home() / ".local" / "bin"
    link = bin_dir / APP_NAME
    bin_dir.mkdir(parents=True, exist_ok=True)
    try:
        if link.is_symlink() or link.exists():
            if link.resolve() == target.resolve():
                return False
            link.unlink()
        link.symlink_to(target)
    except OSError as exc:  # pragma: no cover - filesystem-dependent
        raise ServiceError(f"could not link {link}: {exc}") from exc
    return True


def ensure_on_path() -> bool:
    """Make ``logiswitch`` callable by name from any new terminal.

    On Windows this appends the venv's ``Scripts`` directory to the persistent
    user PATH; on macOS it symlinks the entry point into ``~/.local/bin``. The
    change only affects terminals opened afterwards -- the running shell keeps
    the PATH it started with.
    """
    if is_windows():
        return _windows_ensure_on_path()
    if is_macos():
        return _macos_ensure_on_path()
    return False


def path_hint() -> str:
    """What to tell the user about opening a new shell, if anything."""
    if is_macos():
        bin_dir = Path.home() / ".local" / "bin"
        if str(bin_dir) not in os.environ.get("PATH", "").split(":"):
            return f"add {bin_dir} to your PATH, then open a new terminal"
    return "open a new terminal for 'logiswitch' to be on PATH"


def uninstall() -> list[str]:
    if is_windows():
        return _windows_uninstall()
    if is_macos():
        return _macos_uninstall()
    raise ServiceError("nothing to uninstall on this platform")


def status() -> dict:
    if is_windows():
        return _windows_status()
    if is_macos():
        return _macos_status()
    return {"installed": False}


def _windows_stop() -> bool:
    if not _windows_status().get("installed"):
        return False
    _run(["schtasks", "/End", "/TN", TASK_NAME], check=False)
    return True


def _windows_start() -> bool:
    if not _windows_status().get("installed"):
        return False
    _run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)
    return True


def _macos_stop() -> bool:
    if not _plist_path().exists():
        return False
    _run(["launchctl", "bootout", f"{_launchctl_domain()}/{SERVICE_LABEL}"], check=False)
    return True


def _macos_start() -> bool:
    path = _plist_path()
    if not path.exists():
        return False
    _run(["launchctl", "bootstrap", _launchctl_domain(), str(path)], check=False)
    return True


def stop() -> bool:
    """Stop the running agent if one is registered. Returns whether it existed."""
    if is_windows():
        return _windows_stop()
    if is_macos():
        return _macos_stop()
    return False


def start() -> bool:
    """Start the registered agent. Returns whether a service was found to start."""
    if is_windows():
        return _windows_start()
    if is_macos():
        return _macos_start()
    return False


def restart() -> bool:
    """Stop and start the registered agent. No-op (returns False) if uninstalled.

    Used by ``logiswitch update`` so a freshly installed build actually takes
    over from the process still running the old code.
    """
    had_service = stop()
    if had_service:
        # Give the OS a moment to release the process and, on Windows, the file
        # handles that block replacing the package. launchd's bootout and
        # schtasks /End are both asynchronous about the process actually exiting.
        time.sleep(1.0)
    return start() if had_service else False
