"""`logiswitch doctor`: does the report actually name the fault?

The three causes of wrong characters are indistinguishable to the person typing,
so what is worth pinning here is the verdict -- that each cause produces a report
saying which one it is, and that a healthy machine is not told it has a problem.
"""

from __future__ import annotations

import fakehid
import pytest

from logiswitch import cli


@pytest.fixture
def doctor(monkeypatch, tmp_path, receiver, capsys):
    """Run `doctor` with every file it touches redirected into a tmp dir."""
    monkeypatch.setattr(cli, "log_path", lambda: tmp_path / "logiswitch.log")
    monkeypatch.setattr(cli, "trace_path", lambda: tmp_path / "logiswitch.trace.log")
    monkeypatch.setattr(cli, "doctor_report_path", lambda: tmp_path / "report.txt")
    monkeypatch.setattr(cli.service, "status", lambda: {"installed": False})
    monkeypatch.setattr(
        cli.diagnostics,
        "host_summary",
        lambda: {
            "input_source": "com.apple.keylayout.ABC",
            "non_latin_script": None,
            "competing_software": [],
        },
    )

    def _run(*args: str) -> tuple[int, str]:
        code = cli.main(["doctor", *args])
        return code, capsys.readouterr().out

    _run.tmp_path = tmp_path  # type: ignore[attr-defined]
    _run.receiver = receiver  # type: ignore[attr-defined]
    return _run


def test_a_healthy_host_is_not_told_it_has_a_problem(doctor):
    doctor.receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1  # macOS, as wanted
    code, out = doctor("--os", "mac")
    assert code == 0
    assert "Nothing is wrong at this moment" in out
    assert "SYMPTOM" not in out


def test_a_wrong_firmware_platform_is_named_as_symptom_one(doctor):
    doctor.receiver.devices[fakehid.MX_KEYS_INDEX].platform = 0  # Windows mode
    code, out = doctor("--os", "mac")
    assert code == 1
    assert "SYMPTOM 1" in out
    assert "logiswitch set macos" in out


def test_a_non_latin_input_source_is_named_as_symptom_two(monkeypatch, doctor):
    doctor.receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1  # firmware is fine
    monkeypatch.setattr(
        cli.diagnostics,
        "host_summary",
        lambda: {
            "input_source": "com.apple.keylayout.Arabic",
            "non_latin_script": "Arabic",
            "competing_software": [],
        },
    )
    code, out = doctor("--os", "mac")
    assert code == 1
    assert "SYMPTOM 2" in out
    assert "Arabic" in out
    # The point of the distinction: this one is not logiswitch's to fix.
    assert "does not manage this" in out
    assert "SYMPTOM 1" not in out


def test_competing_software_is_reported(monkeypatch, doctor):
    doctor.receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
    monkeypatch.setattr(
        cli.diagnostics,
        "host_summary",
        lambda: {
            "input_source": "com.apple.keylayout.ABC",
            "non_latin_script": None,
            "competing_software": ["logioptionsplus_agent"],
        },
    )
    code, out = doctor("--os", "mac")
    assert code == 1
    assert "logioptionsplus_agent" in out
    assert "is running" in out, "singular agreement for one process"
    # It must be named as a candidate, not convicted: on the machine this was
    # written against, quitting Options+ changed nothing at all.
    assert "confirm it before blaming it" in out


def test_a_sleeping_keyboard_is_reported_not_silently_skipped(doctor):
    """An asleep keyboard never even reaches discovery, so the receiver looks empty.

    Reporting that as "nothing to do" would be the most misleading answer possible
    for someone whose keyboard is at that moment typing the wrong characters.
    """
    doctor.receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    code, out = doctor("--os", "mac")
    assert code == 1
    assert "nothing behind it answered" in out
    assert "Easy-Switch" in out


def test_the_report_is_written_where_it_can_be_attached_to_a_bug(doctor):
    doctor.receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
    _code, out = doctor("--os", "mac")
    report = doctor.tmp_path / "report.txt"
    assert report.exists()
    assert "logiswitch" in report.read_text()
    assert str(report) in out


def test_the_report_includes_the_raw_platform_table_and_host_records(doctor):
    _code, out = doctor("--os", "mac")
    # The masks are what every later "switched to macos" claim rests on.
    assert "mask 0x2000 -> macos" in out
    assert "getHostPlatform(0xFF)" in out
    assert "set-by=" in out


def test_a_receiver_that_will_not_open_is_not_reported_as_missing(monkeypatch, doctor):
    """Enumerated-but-unopenable is a permission problem, not absent hardware.

    Calling it "no receiver found" sends someone hunting a hardware fault that is
    really a checkbox in System Settings.
    """
    from logiswitch import hidpp

    def refuse(_group):
        raise OSError("open failed")

    monkeypatch.setattr(hidpp, "open_transport", refuse)
    monkeypatch.setattr(cli.hidpp, "open_transport", refuse)
    code, out = doctor("--os", "mac")
    assert code == 1
    assert "REFUSED TO OPEN" in out
    assert "no Logitech HID++ endpoint found" not in out
    assert "Input Monitoring" in out or "will not open" in out
