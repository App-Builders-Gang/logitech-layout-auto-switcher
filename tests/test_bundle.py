"""`logiswitch bundle`: one file that answers a bug report.

Two machines sharing a keyboard produce two half-stories, and the missing half is
always the interesting one. The contract worth pinning is that the archive is
*complete enough to be useful* and *impossible to fail completely* -- a partial
bundle still answers most questions, and a bundle that raised would answer none.
"""

from __future__ import annotations

import zipfile

import pytest

from logiswitch import bundle, cli, doctor


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point every path the bundle reads at a tmp dir, with plausible contents."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "logiswitch.log").write_text("current log\n")
    (logs / "logiswitch.log.1").write_text("rotated log\n")
    (logs / "logiswitch.trace.log").write_text("frame trace\n")
    state = tmp_path / "state.json"
    state.write_text('{"hints": {"Logi Bolt receiver": [5]}}')

    monkeypatch.setattr(bundle, "log_path", lambda: logs / "logiswitch.log")
    monkeypatch.setattr(bundle, "trace_path", lambda: logs / "logiswitch.trace.log")
    monkeypatch.setattr(bundle, "launchd_stdio_path", lambda: logs / "absent.launchd.log")
    monkeypatch.setattr(bundle, "state_path", lambda: state)
    monkeypatch.setattr(bundle, "doctor_report_path", lambda: logs / "absent-doctor.txt")
    monkeypatch.setattr(bundle, "_service_definition", lambda: ("service/def.plist", "<plist/>"))
    monkeypatch.setattr(doctor, "doctor_report", lambda target=None: ("THE DIAGNOSIS", []))
    return tmp_path


def contents(archive) -> dict[str, str]:
    with zipfile.ZipFile(archive) as zf:
        return {name: zf.read(name).decode("utf-8", "replace") for name in zf.namelist()}


def test_the_bundle_carries_everything_a_diagnosis_needs(sandbox):
    files = contents(bundle.build(sandbox / "out.zip"))
    assert files["doctor.txt"].startswith("THE DIAGNOSIS")
    assert "current log" in files["logs/logiswitch.log"]
    assert "rotated log" in files["logs/logiswitch.log.1"], "rotations matter most"
    assert "frame trace" in files["logs/logiswitch.trace.log"]
    assert "Logi Bolt receiver" in files["state.json"]
    assert files["service/def.plist"] == "<plist/>"


def test_the_environment_names_the_machine(sandbox):
    """Two machines' bundles look identical without this."""
    import socket

    environment = contents(bundle.build(sandbox / "out.zip"))["environment.txt"]
    assert socket.gethostname() in environment
    assert "logiswitch     :" in environment
    assert "python" in environment


def test_the_manifest_says_what_is_inside_and_what_is_missing(sandbox):
    manifest = contents(bundle.build(sandbox / "out.zip"))["MANIFEST.txt"]
    assert "logs/logiswitch.log" in manifest
    assert "not present on this machine" in manifest, "absence is information too"
    assert "absent.launchd.log" in manifest


def test_the_manifest_is_honest_about_what_it_contains(sandbox):
    """Someone is about to send this to a stranger; say what is in it."""
    manifest = contents(bundle.build(sandbox / "out.zip"))["MANIFEST.txt"]
    assert "no keystrokes" in manifest
    assert "no credentials" in manifest
    assert "hostname" in manifest, "and be honest that this part is identifying"


def test_a_failing_doctor_does_not_lose_the_logs(sandbox, monkeypatch):
    """The probe needs hardware; the logs do not. Losing both would be perverse."""

    def explode(target=None):
        raise RuntimeError("no receiver attached")

    monkeypatch.setattr(doctor, "doctor_report", explode)
    files = contents(bundle.build(sandbox / "out.zip"))
    assert "no receiver attached" in files["doctor.txt"]
    assert "current log" in files["logs/logiswitch.log"], "the logs still made it"


def test_missing_files_are_noted_rather_than_fatal(sandbox, monkeypatch):
    monkeypatch.setattr(bundle, "log_path", lambda: sandbox / "logs" / "nothing-here.log")
    files = contents(bundle.build(sandbox / "out.zip"))
    assert "nothing-here.log" in files["MANIFEST.txt"]


def test_the_default_name_identifies_the_machine_and_the_moment(monkeypatch):
    monkeypatch.setattr(bundle.socket, "gethostname", lambda: "windows-box.local")
    name = bundle.default_destination().name
    assert name.startswith("logiswitch-diagnostics-windows-box-")
    assert name.endswith(".zip")


def test_the_archive_is_compressed(sandbox):
    """Logs are the bulk of it and compress roughly tenfold; do not ship them raw."""
    (sandbox / "logs" / "logiswitch.log").write_text("a very repetitive line\n" * 5000)
    archive = bundle.build(sandbox / "out.zip")
    with zipfile.ZipFile(archive) as zf:
        info = zf.getinfo("logs/logiswitch.log")
    assert info.compress_type == zipfile.ZIP_DEFLATED
    assert info.compress_size < info.file_size / 5


def test_the_command_reports_where_it_wrote(sandbox, capsys, monkeypatch):
    monkeypatch.setattr(bundle, "default_destination", lambda: sandbox / "auto.zip")
    code = cli.main(["bundle"])
    out = capsys.readouterr().out
    assert code == 0
    assert "auto.zip" in out
    assert (sandbox / "auto.zip").exists()


def test_an_explicit_output_path_is_honoured(sandbox, capsys):
    target = sandbox / "nested" / "mine.zip"
    assert cli.main(["bundle", "-o", str(target)]) == 0
    assert target.exists(), "and the parent directory is created"


# -- the diagnosis must not blame the wrong thing ------------------------------


def test_our_own_agent_holding_the_device_is_not_a_permission_problem():
    """The commonest reason the device will not open, and the least alarming."""
    from logiswitch import diagnostics

    hint = diagnostics.cannot_open_hint(agent_running=True)
    assert "logiswitch agent already has it" in hint
    assert "Input Monitoring" not in hint, "do not send someone to a system setting"
    assert "bootout" in hint and "schtasks" in hint, "say how to get the full dump"


def test_without_the_agent_running_it_still_names_the_permission(monkeypatch):
    from logiswitch import diagnostics

    monkeypatch.setattr(diagnostics, "is_macos", lambda: True)
    hint = diagnostics.cannot_open_hint(agent_running=False)
    assert "Input Monitoring" in hint
