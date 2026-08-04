"""Pack everything a diagnosis needs into one file.

Two machines sharing a keyboard produce two half-stories, and the interesting
part is always the bit you do not have: the other machine's log, its device dump,
its idea of what the platform was at 03:41:16. Asking someone to find and send
four rotated log files, a frame trace, a service definition and a state file --
in different places on each OS -- is how a bug report arrives incomplete.

So: one command, one archive, named after the machine that produced it, with a
manifest saying what each file is. Nothing here is allowed to fail the whole
bundle; a missing file becomes a note in the manifest, because a partial bundle
still answers most questions and no bundle answers none.

What goes in is deliberately bounded: logs, the frame trace, the device dump and
the machine's name. No keystrokes -- this project never sees any -- and no
credentials.
"""

from __future__ import annotations

import io
import logging
import platform
import socket
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__, diagnostics, service
from .paths import (
    APP_NAME,
    data_dir,
    doctor_report_path,
    is_macos,
    is_windows,
    launchd_stdio_path,
    log_path,
    state_path,
    trace_path,
)

log = logging.getLogger(__name__)

#: Rotated siblings to look for alongside each log. RotatingFileHandler writes
#: ``.1``, ``.2``, ``.3``; the trace keeps a single ``.1``.
ROTATIONS = ("", ".1", ".2", ".3")


def default_destination() -> Path:
    """Somewhere the user can actually find it, named after this machine."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    host = socket.gethostname().split(".")[0]
    return Path.home() / f"{APP_NAME}-diagnostics-{host}-{stamp}.zip"


def _service_definition() -> tuple[str, str] | None:
    """The installed service as the OS holds it, so two machines can be compared."""
    if is_macos():
        plist = Path.home() / "Library" / "LaunchAgents" / f"{service.SERVICE_LABEL}.plist"
        try:
            return "service/LaunchAgent.plist", plist.read_text("utf-8", errors="replace")
        except OSError as exc:
            return "service/LaunchAgent.plist.missing", str(exc)
    if is_windows():
        try:
            completed = service._run(["schtasks", "/Query", "/TN", service.TASK_NAME, "/XML"])
            return "service/ScheduledTask.xml", getattr(completed, "stdout", "") or str(completed)
        except Exception as exc:  # noqa: BLE001 - a missing task is information too
            return "service/ScheduledTask.missing", str(exc)
    return None


def _environment() -> str:
    """The facts that decide which code path ran, gathered in one place."""
    lines = [
        f"logiswitch     : {__version__}",
        f"hostname       : {socket.gethostname()}",
        f"platform       : {platform.platform()}",
        f"python         : {sys.version.splitlines()[0]}",
        f"executable     : {sys.executable}",
        f"generated      : {datetime.now().isoformat(timespec='seconds')}",
    ]
    try:
        state = service.status()
        lines.append(f"service        : {state}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"service        : unavailable ({exc})")
    try:
        host = diagnostics.host_summary()
        lines.append(f"input source   : {host['input_source']}")
        lines.append(f"non-latin      : {host['non_latin_script']}")
        lines.append(f"also running   : {', '.join(host['competing_software']) or 'nothing known'}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"host summary   : unavailable ({exc})")
    return "\n".join(lines) + "\n"


def _collect() -> tuple[list[tuple[str, Path]], list[str]]:
    """(archive name, source) for every file worth having, plus what was missing."""
    wanted: list[tuple[str, Path]] = []
    for base, folder in ((log_path(), "logs"), (trace_path(), "logs")):
        for suffix in ROTATIONS:
            candidate = base.with_name(base.name + suffix)
            wanted.append((f"{folder}/{candidate.name}", candidate))
    wanted.append(("logs/launchd-stdio.log", launchd_stdio_path()))
    wanted.append(("state.json", state_path()))
    wanted.append(("doctor-previous.txt", doctor_report_path()))

    present, missing = [], []
    for arcname, source in wanted:
        if source.exists() and source.is_file():
            present.append((arcname, source))
        else:
            missing.append(str(source))
    return present, missing


def build(destination: Path | None = None, target_os: str | None = None) -> Path:
    """Write the diagnostics archive and return where it went."""
    from .cli import doctor_report  # imported here: cli imports this module

    archive = Path(destination) if destination else default_destination()
    archive.parent.mkdir(parents=True, exist_ok=True)

    present, missing = _collect()
    manifest = [
        "logiswitch diagnostics bundle",
        "",
        "environment.txt      versions, hostname, service state, host input source",
        "doctor.txt           the full diagnosis, same as `logiswitch doctor`",
        "logs/                the agent log and its rotations, and the frame trace",
        "state.json           remembered device indices",
        "service/             the installed service definition as the OS holds it",
        "",
        "Contains no keystrokes (this project never sees any) and no credentials.",
        "It does contain this machine's hostname and its Logitech device names.",
        "",
        "included:",
        *(f"  {name}" for name, _ in present),
    ]
    if missing:
        manifest += [
            "",
            "not present on this machine (usually fine):",
            *(f"  {m}" for m in missing),
        ]

    try:
        report, _findings = doctor_report(target_os)
    except Exception as exc:  # noqa: BLE001 - never let a probe failure lose the logs
        report = f"doctor failed to run: {exc}\n"
        log.debug("doctor failed while bundling", exc_info=True)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MANIFEST.txt", "\n".join(manifest) + "\n")
        zf.writestr("environment.txt", _environment())
        zf.writestr("doctor.txt", report + "\n")
        definition = _service_definition()
        if definition is not None:
            zf.writestr(definition[0], definition[1])
        for arcname, source in present:
            try:
                zf.writestr(arcname, source.read_bytes())
            except OSError as exc:
                # A file we could see but not read is worth recording, not fatal.
                zf.writestr(f"{arcname}.unreadable", str(exc))
    archive.write_bytes(buffer.getvalue())
    return archive


def describe(archive: Path) -> str:  # pragma: no cover - convenience for humans
    with zipfile.ZipFile(archive) as zf:
        return "\n".join(sorted(zf.namelist()))


__all__ = ["build", "default_destination", "describe", "data_dir"]
