"""Windows Scheduled Task registration.

The behaviour worth pinning down is ordering. ``schtasks /Create /F`` replaces the
registration but does not stop an instance that is already running, so upgrading
over a live agent used to leave the previous build resident -- still logging its
old settings -- until the next logon. These tests drive ``service`` against a
scripted schtasks so the sequence is reproducible off Windows too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from logiswitch import service

pytestmark = pytest.mark.skipif(not service.is_windows(), reason="Scheduled Tasks are Windows-only")


MISSING = "ERROR: The system cannot find the file specified."


class FakeSchtasks:
    """Records schtasks invocations and answers them from a script.

    Existence is tracked per task name: uninstall walks both the current and the
    legacy task, so a single global flag would make the second one look absent as
    soon as the first was deleted.
    """

    def __init__(self, existing: set[str] | None = None):
        self.existing = set(
            existing if existing is not None else {service.TASK_NAME, service.LEGACY_TASK_NAME}
        )
        self.calls: list[list[str]] = []

    @staticmethod
    def _task_name(args: list[str]) -> str:
        return args[args.index("/TN") + 1] if "/TN" in args else ""

    def __call__(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        self.calls.append(args)
        verb = args[1].lstrip("/").lower()
        name = self._task_name(args)
        code, out = 0, ""
        if verb == "create":
            self.existing.add(name)
        elif verb == "delete":
            self.existing.discard(name)
        elif verb in ("query", "end", "run") and name not in self.existing:
            code, out = 1, MISSING
        result = subprocess.CompletedProcess(args, code, stdout=out, stderr=out)
        if check and code != 0:
            raise service.ServiceError(f"{args[0]} failed ({code}): {out}")
        return result

    def verbs(self) -> list[str]:
        return [call[1].lstrip("/").lower() for call in self.calls]


@pytest.fixture
def schtasks(monkeypatch):
    fake = FakeSchtasks()
    monkeypatch.setattr(service, "_run", fake)
    return fake


def test_install_ends_the_running_instance_before_replacing_it(schtasks):
    """The upgrade defect: a live agent kept running the previous build."""
    service.install()

    verbs = schtasks.verbs()
    assert "end" in verbs, "a running instance must be stopped before /Create"
    assert verbs.index("end") < verbs.index("create")
    assert verbs.index("create") < verbs.index("run")
    assert verbs[-1] == "run"


def test_install_is_fine_when_nothing_was_installed_before(monkeypatch):
    fake = FakeSchtasks(existing=set())
    monkeypatch.setattr(service, "_run", fake)

    what = service.install()

    assert service.TASK_NAME in what
    assert fake.verbs()[-1] == "run"


def test_install_passes_the_target_os_through(schtasks, monkeypatch):
    written: dict[str, bytes] = {}
    real_write = Path.write_bytes

    def capture(self: Path, data: bytes) -> int:
        written["xml"] = data
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", capture)

    service.install("macos")

    xml = written["xml"].decode("utf-16")
    assert "-m logiswitch watch --os macos" in xml
    assert "<LogonTrigger>" in xml


def test_uninstall_removes_the_current_and_legacy_tasks(schtasks):
    removed = service.uninstall()

    assert service.TASK_NAME in removed
    assert service.LEGACY_TASK_NAME in removed
    assert schtasks.verbs().count("delete") == 2


def test_uninstall_reports_nothing_when_no_task_exists(monkeypatch):
    fake = FakeSchtasks(existing=set())
    monkeypatch.setattr(service, "_run", fake)

    assert service.uninstall() == []
    assert "delete" not in fake.verbs()


def test_no_notify_reaches_the_task_xml(schtasks, monkeypatch):
    written = {}
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: written.__setitem__("xml", data))
    service.install("macos", notify=False)
    xml = written["xml"].decode("utf-16")
    assert "--no-notify" in xml


def test_the_task_xml_omits_the_flag_when_notifications_stay_on(schtasks, monkeypatch):
    written = {}
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: written.__setitem__("xml", data))
    service.install("macos")
    xml = written["xml"].decode("utf-16")
    assert "--no-notify" not in xml
    assert "-m logiswitch watch --os macos" in xml


def test_observe_mode_reaches_the_task_xml(schtasks, monkeypatch):
    written = {}
    monkeypatch.setattr(Path, "write_bytes", lambda self, data: written.__setitem__("xml", data))
    service.install("windows", observe=True)
    assert "--observe" in written["xml"].decode("utf-16")
