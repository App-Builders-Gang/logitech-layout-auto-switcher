"""Host-side context: the input source and who else is driving the keyboard.

These run on every OS in CI, so nothing here may depend on the platform-specific
readers actually working -- the contract is that they degrade to "unknown" instead
of raising, because a diagnostic that breaks the thing it diagnoses is worthless.
"""

from __future__ import annotations

import pytest

from logiswitch import diagnostics

MACOS_PS = """\
loginwindow
logind
login
LoginUserService
logioptionsplus_agent
logioptionsplus_updater
LogiRightSight
Finder
"""

WINDOWS_TASKLIST = """\
"explorer.exe","1234","Console","1","50,000 K"
"logioptionsplus_agent.exe","4321","Console","1","30,000 K"
"notepad.exe","1111","Console","1","10,000 K"
"""


# -- input source -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("com.apple.keylayout.ABC", None),
        ("com.apple.keylayout.US", None),
        ("com.apple.keylayout.British", None),
        ("com.apple.keylayout.Arabic", "Arabic"),
        ("com.apple.keylayout.Hebrew", "Hebrew"),
        ("com.apple.keylayout.Russian", "Cyrillic"),
        ("com.apple.inputmethod.SCIM.ITABC", "Han"),
        ("com.apple.inputmethod.Kotoeri.RomajiTyping.Japanese", "Japanese"),
        ("ar-EG (0x0401)", "Arabic"),
        ("en-US (0x0409)", None),
        ("ru-RU (0x0419)", "Cyrillic"),
        ("ko-KR (0x0412)", "Hangul"),
        ("", None),
        (diagnostics.UNKNOWN, None),
    ],
)
def test_non_latin_scripts_are_recognised(source, expected):
    assert diagnostics.non_latin_script(source) == expected


def test_an_unrecognised_input_method_is_flagged_but_not_named():
    """Vietnamese Telex is an IME producing Latin, so this must not overclaim."""
    result = diagnostics.non_latin_script("com.apple.inputmethod.VietnameseIM.VietnameseTelex")
    assert result == "an input method"


def test_input_source_never_raises(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_macos", lambda: True)
    monkeypatch.setattr(
        diagnostics, "_macos_input_source", lambda: (_ for _ in ()).throw(OSError("no framework"))
    )
    assert diagnostics.input_source() == diagnostics.UNKNOWN


def test_input_source_is_unknown_on_an_unsupported_platform(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_macos", lambda: False)
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    assert diagnostics.input_source() == diagnostics.UNKNOWN


# -- competing software -------------------------------------------------------


def test_logitech_processes_are_found_on_macos(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    found = diagnostics.competing_software(runner=lambda _cmd: MACOS_PS)
    assert found == ["LogiRightSight", "logioptionsplus_agent", "logioptionsplus_updater"]


def test_login_processes_are_not_mistaken_for_logitech(monkeypatch):
    """`login`, `logind`, `loginwindow` are on every Mac and mean nothing here."""
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    found = diagnostics.competing_software(runner=lambda _cmd: MACOS_PS)
    assert not any("login" in name.lower() for name in found)


def test_tasklist_csv_is_parsed_on_windows(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_windows", lambda: True)
    found = diagnostics.competing_software(runner=lambda _cmd: WINDOWS_TASKLIST)
    assert found == ["logioptionsplus_agent.exe"]


def test_a_clean_machine_reports_nothing(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    assert diagnostics.competing_software(runner=lambda _cmd: "Finder\nDock\n") == []


def test_a_failing_process_list_is_not_fatal(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)

    def explode(_cmd):
        raise OSError("ps is missing")

    assert diagnostics.competing_software(runner=explode) == []


def test_full_paths_are_reduced_to_the_process_name(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    output = "/Library/Application Support/Logitech/logioptionsplus_agent\n"
    assert diagnostics.competing_software(runner=lambda _cmd: output) == ["logioptionsplus_agent"]


# -- the whole picture --------------------------------------------------------


def test_host_summary_carries_everything_the_report_needs(monkeypatch):
    monkeypatch.setattr(diagnostics, "input_source", lambda: "com.apple.keylayout.Arabic")
    monkeypatch.setattr(diagnostics, "competing_software", lambda: ["logioptionsplus_agent"])
    summary = diagnostics.host_summary()
    assert summary["input_source"] == "com.apple.keylayout.Arabic"
    assert summary["non_latin_script"] == "Arabic"
    assert summary["competing_software"] == ["logioptionsplus_agent"]


def test_describe_host_is_one_line_and_mentions_only_what_applies():
    quiet = {"input_source": "com.apple.keylayout.ABC", "non_latin_script": None}
    assert diagnostics.describe_host(quiet) == "input=com.apple.keylayout.ABC"

    noisy = {
        "input_source": "com.apple.keylayout.Arabic",
        "non_latin_script": "Arabic",
        "competing_software": ["logioptionsplus_agent"],
    }
    line = diagnostics.describe_host(noisy)
    assert "script=Arabic" in line
    assert "also-running=logioptionsplus_agent" in line
    assert "\n" not in line


# -- why an endpoint would not open -------------------------------------------


def test_macos_names_input_monitoring(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_macos", lambda: True)
    hint = diagnostics.cannot_open_hint()
    assert hint and "Input Monitoring" in hint


def test_windows_points_at_exclusive_access(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_macos", lambda: False)
    monkeypatch.setattr(diagnostics, "is_windows", lambda: True)
    hint = diagnostics.cannot_open_hint()
    assert hint and "Options+" in hint


def test_linux_points_at_udev(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_macos", lambda: False)
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    hint = diagnostics.cannot_open_hint()
    assert hint and "udev" in hint


def test_openlogi_and_logiops_are_recognised(monkeypatch):
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    output = "openlogi-agent\nlogid\nFinder\n"
    assert diagnostics.competing_software(runner=lambda _cmd: output) == [
        "logid",
        "openlogi-agent",
    ]


def test_electron_helpers_collapse_into_their_app(monkeypatch):
    """Opening the Options+ window must not turn one program into seven.

    The line naming competing software is read by someone working out what is
    fighting for their keyboard; seven near-identical entries make it useless.
    """
    monkeypatch.setattr(diagnostics, "is_windows", lambda: False)
    output = (
        "logioptionsplus\n"
        "logioptionsplus Helper\n"
        "logioptionsplus Helper (GPU)\n"
        "logioptionsplus Helper (Renderer)\n"
        "logioptionsplus_agent\n"
        "logioptionsplus_updater\n"
        "Finder\n"
    )
    assert diagnostics.competing_software(runner=lambda _cmd: output) == [
        "logioptionsplus",
        "logioptionsplus_agent",
        "logioptionsplus_updater",
    ], "helpers fold in; separate products do not"
