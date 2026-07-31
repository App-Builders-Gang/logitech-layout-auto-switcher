"""Per-platform locations and logging setup.

Every path here ends up in an installer, a plist or a scheduled task, so getting
one wrong means the agent writes somewhere nobody looks.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from logiswitch import paths


@pytest.fixture
def on_platform(monkeypatch):
    """Pretend to be a given OS, with a throwaway HOME and LOCALAPPDATA."""

    def apply(system: str, tmp_path: Path):
        monkeypatch.setattr(paths.platform, "system", lambda: system)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        return tmp_path

    return apply


@pytest.mark.parametrize(
    ("system", "expected"),
    [("Darwin", "macos"), ("Windows", "windows"), ("Linux", "linux"), ("FreeBSD", "linux")],
)
def test_default_target_os_follows_the_host(monkeypatch, system, expected):
    monkeypatch.setattr(paths.platform, "system", lambda: system)
    assert paths.default_target_os() == expected


def test_windows_paths_live_under_localappdata(on_platform, tmp_path):
    on_platform("Windows", tmp_path)

    assert paths.data_dir() == tmp_path / "AppData" / "Local" / "LogiSwitch"
    assert paths.log_path().name == "logiswitch.log"
    assert paths.log_path().parent == paths.data_dir()
    assert paths.state_path() == paths.data_dir() / "state.json"


def test_macos_logs_go_to_library_logs(on_platform, tmp_path):
    on_platform("Darwin", tmp_path)

    assert paths.log_path() == tmp_path / "Library" / "Logs" / "logiswitch.log"
    assert paths.data_dir() == tmp_path / "Library" / "Application Support" / "logiswitch"


def test_linux_honours_xdg_state_home(on_platform, tmp_path, monkeypatch):
    on_platform("Linux", tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    assert paths.data_dir() == tmp_path / "xdg" / "logiswitch"


def test_linux_falls_back_to_local_state(on_platform, tmp_path):
    on_platform("Linux", tmp_path)

    assert paths.data_dir() == tmp_path / ".local" / "state" / "logiswitch"


def test_launchd_capture_is_a_different_file_from_the_agent_log(on_platform, tmp_path):
    """Pointing both at one file is what made every line appear twice."""
    on_platform("Darwin", tmp_path)

    assert paths.launchd_stdio_path() != paths.log_path()
    assert paths.launchd_stdio_path().parent == paths.log_path().parent
    assert paths.launchd_stdio_path().name.endswith(".launchd.log")


def test_managed_flag_reads_the_environment(monkeypatch):
    monkeypatch.delenv(paths.MANAGED_ENV_VAR, raising=False)
    assert paths.is_managed() is False

    monkeypatch.setenv(paths.MANAGED_ENV_VAR, "1")
    assert paths.is_managed() is True

    monkeypatch.setenv(paths.MANAGED_ENV_VAR, "0")
    assert paths.is_managed() is False


def test_python_executable_is_the_running_interpreter_by_default():
    assert paths.python_executable() == Path(sys.executable)


def test_windowless_prefers_pythonw_when_it_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.platform, "system", lambda: "Windows")
    fake_python = tmp_path / "python.exe"
    fake_python.touch()
    (tmp_path / "pythonw.exe").touch()
    monkeypatch.setattr(paths.sys, "executable", str(fake_python))

    assert paths.python_executable(windowless=True).name == "pythonw.exe"


def test_windowless_falls_back_when_pythonw_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.platform, "system", lambda: "Windows")
    fake_python = tmp_path / "python.exe"
    fake_python.touch()
    monkeypatch.setattr(paths.sys, "executable", str(fake_python))

    assert paths.python_executable(windowless=True) == fake_python


def test_windowless_is_ignored_off_windows(monkeypatch):
    monkeypatch.setattr(paths.platform, "system", lambda: "Darwin")

    assert paths.python_executable(windowless=True) == Path(sys.executable)


# -- logging ------------------------------------------------------------------


def test_setup_logging_writes_to_the_requested_file(tmp_path):
    log_file = tmp_path / "nested" / "logiswitch.log"

    paths.setup_logging(log_file=log_file, console=False)
    logging.getLogger("logiswitch.test").info("hello from the test")
    for handler in logging.getLogger("logiswitch").handlers:
        handler.flush()

    assert log_file.exists()
    assert "hello from the test" in log_file.read_text("utf-8")


def test_setup_logging_replaces_handlers_rather_than_stacking_them(tmp_path):
    """Called twice, it must not double every line."""
    for _ in range(3):
        paths.setup_logging(log_file=tmp_path / "logiswitch.log", console=False)

    logger = logging.getLogger("logiswitch")
    assert len(logger.handlers) == 1


def test_setup_logging_stops_propagation(tmp_path):
    """Records must not also reach the root logger, or the managed agent double-logs."""
    paths.setup_logging(log_file=tmp_path / "logiswitch.log", console=False)

    assert logging.getLogger("logiswitch").propagate is False


def test_verbose_selects_debug_level(tmp_path):
    paths.setup_logging(verbose=True, log_file=tmp_path / "l.log", console=False)
    assert logging.getLogger("logiswitch").level == logging.DEBUG

    paths.setup_logging(verbose=False, log_file=tmp_path / "l.log", console=False)
    assert logging.getLogger("logiswitch").level == logging.INFO


def test_console_handler_is_optional(tmp_path):
    paths.setup_logging(log_file=tmp_path / "l.log", console=True)
    with_console = len(logging.getLogger("logiswitch").handlers)

    paths.setup_logging(log_file=tmp_path / "l.log", console=False)
    without_console = len(logging.getLogger("logiswitch").handlers)

    assert with_console == without_console + 1


def test_a_logger_with_nothing_configured_still_has_a_handler():
    """Never let logging fall back to lastResort and print to stderr uninvited."""
    paths.setup_logging(log_file=None, console=False)

    handlers = logging.getLogger("logiswitch").handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.NullHandler)
