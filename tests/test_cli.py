"""The command line: exit codes, output, and argument handling.

This is the surface users actually touch, so the contract worth pinning is the
observable one -- what a command prints and what it returns to the shell.
"""

from __future__ import annotations

import fakehid
import pytest

from logiswitch import cli, service
from logiswitch.hidpp import backend


def run(capsys, *args) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- status -------------------------------------------------------------------


def test_status_reports_the_device_and_its_platform(receiver, capsys):
    code, out, _err = run(capsys, "status")

    assert code == 0
    assert "MX Keys S" in out
    assert "MULTIPLATFORM 0x4531" in out
    assert "platform 1: macos" in out
    assert "current: android/linux/windows" in out


def test_status_marks_a_device_that_cannot_switch(receiver, capsys):
    code, out, _err = run(capsys, "status")

    assert code == 0
    assert "MX Master 3S" in out
    assert "cannot switch layout" in out


def test_status_reports_the_easy_switch_channel(receiver, capsys):
    _code, out, _err = run(capsys, "status")

    assert "Easy-Switch channel 1" in out
    assert "set by host software" in out


def test_status_fails_when_nothing_can_switch(monkeypatch, capsys):
    fakehid.install(monkeypatch, fakehid.FakeReceiver([fakehid.mx_master_3s()]))

    code, _out, err = run(capsys, "status")

    assert code == 1
    assert "Nothing here can switch layout" in err


def test_status_exits_with_a_hint_when_no_receiver_is_present(monkeypatch, capsys):
    monkeypatch.setattr(backend, "enumerate_devices", lambda *a, **k: [])

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["status"])

    message = str(excinfo.value)
    assert "no Logitech HID++ receiver" in message
    assert "KVM" in message, "the KVM case is the most common cause; say so"


# -- set ----------------------------------------------------------------------


def test_set_switches_and_reports_what_changed(receiver, capsys):
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 0

    code, out, _err = run(capsys, "set", "mac")

    assert code == 0
    assert "switched to macos" in out
    assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 1


def test_set_is_idempotent_and_says_so(receiver, capsys):
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1

    code, out, _err = run(capsys, "set", "mac")

    assert code == 0
    assert "already on macos" in out
    assert receiver.devices[fakehid.MX_KEYS_INDEX].set_calls == []


@pytest.mark.parametrize("alias", ["win", "windows", "pc"])
def test_set_accepts_os_aliases(receiver, capsys, alias):
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1

    code, _out, _err = run(capsys, "set", alias)

    assert code == 0
    assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 0


def test_set_rejects_an_unknown_os_before_touching_hardware(receiver, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["set", "beos"])

    assert excinfo.value.code == 2, "argparse should reject the choice itself"
    assert receiver.writes == 0, "nothing should have been sent to the device"


def test_set_reports_when_no_device_supports_switching(monkeypatch, capsys):
    fakehid.install(monkeypatch, fakehid.FakeReceiver([fakehid.mx_master_3s()]))

    code, _out, err = run(capsys, "set", "mac")

    assert code == 1
    assert "no device supports layout switching" in err


# -- probe --------------------------------------------------------------------


def test_probe_dumps_enough_for_a_bug_report(receiver, capsys):
    code, out, _err = run(capsys, "probe")

    assert code == 0
    assert "usage_page=0xFF00" in out
    assert "MX Keys S" in out
    assert "mask 0x2000" in out
    assert "getHostPlatform" in out


def test_probe_fails_cleanly_with_no_hardware(monkeypatch, capsys):
    monkeypatch.setattr(backend, "enumerate_devices", lambda *a, **k: [])

    code, out, _err = run(capsys, "probe")

    assert code == 1
    assert "HID++ vendor collections: 0" in out


# -- watch --once -------------------------------------------------------------


def test_watch_once_applies_and_exits(receiver, capsys):
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1

    code, _out, _err = run(capsys, "watch", "--once", "--os", "windows")

    assert code == 0
    assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 0


def test_watch_once_reports_failure_when_nothing_is_there(monkeypatch, capsys):
    monkeypatch.setattr(backend, "enumerate_devices", lambda *a, **k: [])

    code, _out, _err = run(capsys, "watch", "--once")

    assert code == 1


def test_watch_rejects_an_unknown_target_os(receiver, capsys):
    code, _out, err = run(capsys, "watch", "--once", "--os", "beos")

    assert code == 1
    assert "unknown OS" in err


# -- service commands ---------------------------------------------------------


def test_install_reports_what_it_registered(monkeypatch, capsys):
    monkeypatch.setattr(service, "ensure_on_path", lambda: True)
    monkeypatch.setattr(service, "path_hint", lambda: "open a new terminal")
    monkeypatch.setattr(
        service,
        "install",
        lambda target=None, notify=True, observe=False, event_only=None: f"thing for {target}",
    )
    monkeypatch.setattr(service, "status", lambda: {"installed": True, "state": "Running"})

    code, out, _err = run(capsys, "install", "--os", "mac")

    assert code == 0
    assert "thing for macos" in out
    assert "added 'logiswitch' to PATH" in out
    assert "open a new terminal" in out
    assert "Running" in out


def test_install_does_not_mention_path_when_already_there(monkeypatch, capsys):
    monkeypatch.setattr(
        service,
        "install",
        lambda target=None, notify=True, observe=False, event_only=None: "scheduled task",
    )
    monkeypatch.setattr(service, "ensure_on_path", lambda: False)
    monkeypatch.setattr(service, "status", lambda: {"installed": True, "state": "Running"})

    code, out, _err = run(capsys, "install")

    assert code == 0
    assert "PATH" not in out


def test_install_surfaces_a_failure(monkeypatch, capsys):
    def boom(target=None, notify=True, observe=False, event_only=None):
        raise service.ServiceError("launchctl said no")

    monkeypatch.setattr(service, "install", boom)
    monkeypatch.setattr(service, "ensure_on_path", lambda: False)

    code, _out, err = run(capsys, "install")

    assert code == 1
    assert "launchctl said no" in err


def test_uninstall_lists_what_it_removed(monkeypatch, capsys):
    monkeypatch.setattr(service, "uninstall", lambda: ["LogiSwitch", "MXSwitch"])

    code, out, _err = run(capsys, "uninstall")

    assert code == 0
    assert "LogiSwitch" in out and "MXSwitch" in out


def test_uninstall_is_honest_when_nothing_was_installed(monkeypatch, capsys):
    monkeypatch.setattr(service, "uninstall", list)

    code, out, _err = run(capsys, "uninstall")

    assert code == 0
    assert "nothing was installed" in out


def test_service_status_returns_nonzero_when_absent(monkeypatch, capsys):
    monkeypatch.setattr(service, "status", lambda: {"installed": False})

    code, out, _err = run(capsys, "service-status")

    assert code == 1
    assert "not installed" in out


# -- parser contract ----------------------------------------------------------


def test_a_subcommand_is_required(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 2


def test_version_is_reported(capsys):
    from logiswitch import __version__

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("position", ["before", "after"])
def test_verbose_works_on_either_side_of_the_subcommand(receiver, capsys, position):
    args = ["-v", "status"] if position == "before" else ["status", "-v"]

    assert cli.main(args) == 0


def test_unexpected_errors_are_reported_not_raised(monkeypatch, receiver, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("something deep broke")

    monkeypatch.setattr(cli.hidpp, "find_groups", boom)

    code, _out, err = run(capsys, "status")

    assert code == 1
    assert "something deep broke" in err


# -- tracing ------------------------------------------------------------------


def test_trace_turns_on_frame_logging_and_a_dump_destination(monkeypatch, receiver, tmp_path):
    from logiswitch import trace

    monkeypatch.setattr(cli, "trace_path", lambda: tmp_path / "t.log")
    cli.main(["--trace", "status"])
    assert trace.echoing()
    assert trace.anomaly("something looked wrong") == tmp_path / "t.log"


def test_a_plain_command_leaves_no_files_behind(monkeypatch, receiver, tmp_path):
    """`status` has no business writing into the log directory."""
    from logiswitch import trace

    monkeypatch.setattr(cli, "trace_path", lambda: tmp_path / "t.log")
    cli.main(["status"])
    assert not trace.echoing()
    assert trace.anomaly("nowhere to go") is None
    assert not (tmp_path / "t.log").exists()


def test_watch_records_a_trace_destination_without_being_asked(monkeypatch, receiver, tmp_path):
    from logiswitch import trace

    monkeypatch.setattr(cli, "trace_path", lambda: tmp_path / "t.log")
    monkeypatch.setattr(cli, "state_path", lambda: tmp_path / "state.json")
    cli.main(["watch", "--once", "--os", "mac"])
    assert trace.anomaly("the agent found something") == tmp_path / "t.log"


@pytest.mark.parametrize(
    "argv",
    [
        ["-v", "status"],
        ["status", "-v"],
        ["--verbose", "status"],
    ],
)
def test_global_flags_work_on_either_side_of_the_subcommand(argv):
    """argparse copies a subparser's defaults over what the top level already parsed.

    Without suppressed defaults `logiswitch -v status` silently ran without debug
    logging, which is a poor way to find out about an intermittent fault.
    """
    args = cli.build_parser().parse_args(argv)
    resolved = {
        name: getattr(args, name, fallback) for name, fallback in cli.GLOBAL_FLAG_DEFAULTS.items()
    }
    assert resolved["verbose"] is True


def test_flags_that_are_not_given_fall_back(monkeypatch, receiver):
    cli.main(["status"])
    args = cli.build_parser().parse_args(["status"])
    assert getattr(args, "trace", cli.GLOBAL_FLAG_DEFAULTS["trace"]) is False


# -- notifications -------------------------------------------------------------


def test_install_passes_the_notification_choice_through(monkeypatch, capsys):
    seen = {}

    def fake_install(target=None, notify=True, observe=False, event_only=None):
        seen["notify"] = notify
        seen["event_only"] = event_only
        return "a service"

    monkeypatch.setattr(service, "install", fake_install)
    monkeypatch.setattr(service, "status", lambda: {"installed": False})

    run(capsys, "install", "--no-notify")
    assert seen["notify"] is False
    run(capsys, "install")
    assert seen["notify"] is True, "notifications are on unless asked otherwise"
    run(capsys, "install", "--event-only")
    assert seen["event_only"] is True
    run(capsys, "install", "--no-event-only")
    assert seen["event_only"] is False
    run(capsys, "install")
    assert seen["event_only"] is None, "default install leaves the mode to auto-detect at runtime"


def test_watch_defaults_to_notifying():
    args = cli.build_parser().parse_args(["watch"])
    assert args.notify is True
    assert cli.build_parser().parse_args(["watch", "--no-notify"]).notify is False


def test_notify_test_reports_the_backend(monkeypatch, capsys):
    from logiswitch import notify as notify_module

    monkeypatch.setattr(notify_module.Notifier, "deliver", lambda _self, _note: True)
    code, out, _err = run(capsys, "notify-test")
    assert code == 0
    assert "backend:" in out
    # The whole point of the command is telling someone what to do when nothing
    # appeared, so the permission path has to be in the output.
    assert "Notifications" in out


def test_notify_test_fails_loudly_when_the_backend_errors(monkeypatch, capsys):
    from logiswitch import notify as notify_module

    monkeypatch.setattr(notify_module.Notifier, "deliver", lambda _self, _note: False)
    code, _out, err = run(capsys, "notify-test")
    assert code == 1
    assert "failed" in err
