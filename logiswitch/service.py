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
import tempfile
from pathlib import Path

from .paths import (
    APP_NAME,
    LEGACY_TASK_NAME,
    SERVICE_LABEL,
    is_macos,
    is_windows,
    log_path,
    python_executable,
)

log = logging.getLogger(__name__)

TASK_NAME = "LogiSwitch"

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
    return f"gui/{os.getuid()}"  # type: ignore[attr-defined]  # macOS-only path


def _macos_install(target_os: str | None) -> str:
    executable = str(python_executable())
    arguments = [executable, "-m", APP_NAME, "watch"]
    if target_os:
        arguments += ["--os", target_os]
    logfile = str(log_path())

    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardErrorPath": logfile,
        "StandardOutPath": logfile,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle)

    domain = _launchctl_domain()
    _run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"], check=False)
    _run(["launchctl", "bootstrap", domain, str(path)])
    _run(["launchctl", "kickstart", "-k", f"{domain}/{SERVICE_LABEL}"], check=False)
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
    result = _run(["launchctl", "print", f"{_launchctl_domain()}/{SERVICE_LABEL}"], check=False)
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
    if is_windows():
        return _windows_install(target_os)
    if is_macos():
        return _macos_install(target_os)
    raise ServiceError(
        "automatic service installation is only implemented for Windows and macOS; "
        f"run '{APP_NAME} watch' from your own supervisor instead"
    )


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
