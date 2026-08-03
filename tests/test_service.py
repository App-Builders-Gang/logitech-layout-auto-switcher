"""macOS LaunchAgent registration.

The interesting behaviour is all timing: ``launchctl bootout`` returns as soon as
SIGTERM is delivered, but the label stays registered until the process is really gone,
and bootstrapping a still-registered label fails with EIO (5). These tests drive
``service`` against a scripted launchctl so that sequence is reproducible.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from logiswitch import service

pytestmark = pytest.mark.skipif(not service.is_macos(), reason="launchd is macOS-only")

EIO = "Bootstrap failed: 5: Input/output error"


class FakeLaunchctl:
    """Records launchctl invocations and answers them from a script.

    ``print`` returns 0 while ``registered`` is true. ``bootstrap_results`` is consumed
    one entry per attempt; each entry is a return code, and a 0 flips ``registered``
    back on.
    """

    def __init__(self, registered: bool = True, bootstrap_results: list[int] | None = None):
        self.registered = registered
        self.bootstrap_results = list(bootstrap_results or [0])
        self.calls: list[list[str]] = []
        #: number of `print` calls to answer as registered before going quiet
        self.unload_after = 0

    def __call__(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        self.calls.append(args)
        verb = args[1]
        code, out = 0, ""
        if verb == "print":
            if self.registered and self.unload_after > 0:
                self.unload_after -= 1
            elif self.registered and self.unload_after == 0:
                self.registered = False
            code = 0 if self.registered else 1
            out = "\tstate = running\n" if code == 0 else "Could not find service"
        elif verb == "bootout":
            pass  # asynchronous: the label lingers, which is the whole point
        elif verb == "bootstrap":
            code = self.bootstrap_results.pop(0) if self.bootstrap_results else 0
            if code == 0:
                self.registered = True
            else:
                out = EIO
        result = subprocess.CompletedProcess(args, code, stdout=out, stderr=out)
        if check and code != 0:
            raise service.ServiceError(f"{args[0]} failed ({code}): {out}")
        return result

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture
def launchctl(monkeypatch, tmp_path):
    """A scripted launchctl, a throwaway HOME, and no real sleeping."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)
    fake = FakeLaunchctl()
    monkeypatch.setattr(service, "_run", fake)
    return fake


def test_install_waits_for_teardown_then_bootstraps(launchctl):
    """The happy path: bootout, poll until the label is gone, bootstrap once."""
    launchctl.unload_after = 3  # three polls still see it registered

    service.install()

    assert launchctl.verbs()[0] == "enable"  # before anything else
    assert launchctl.verbs()[1] == "bootout"
    assert launchctl.verbs().count("bootstrap") == 1
    assert launchctl.verbs()[-1] == "kickstart"
    # bootstrap must not be attempted while print still reports the label
    prints_before = launchctl.verbs().index("bootstrap")
    assert launchctl.verbs()[2:prints_before] == ["print"] * (prints_before - 2)


def test_install_retries_bootstrap_on_eio(launchctl):
    """EIO twice, then success -- this is the reported reinstall failure."""
    launchctl.bootstrap_results = [5, 5, 0]

    what = service.install()

    assert service.SERVICE_LABEL in what
    assert launchctl.verbs().count("bootstrap") == 3
    assert launchctl.verbs()[-1] == "kickstart"


def test_install_gives_up_with_an_actionable_error(launchctl):
    launchctl.bootstrap_results = [5] * len(service.BOOTSTRAP_DELAYS)

    with pytest.raises(service.ServiceError) as excinfo:
        service.install()

    message = str(excinfo.value)
    assert EIO in message
    assert "do NOT re-run as root" in message
    assert "launchctl bootout gui/" in message
    assert "kickstart" not in launchctl.verbs()


def test_bootstrap_failure_is_success_when_the_label_is_loaded(monkeypatch, launchctl):
    """Someone else won the race. Loaded is loaded."""
    launchctl.bootstrap_results = [5]
    calls = {"n": 0}
    real_print = service._service_print

    def print_registered_after_bootstrap(target):
        if "bootstrap" in launchctl.verbs():
            calls["n"] += 1
            return subprocess.CompletedProcess([], 0, stdout="\tstate = running\n", stderr="")
        return real_print(target)

    monkeypatch.setattr(service, "_service_print", print_registered_after_bootstrap)

    service.install()

    assert launchctl.verbs().count("bootstrap") == 1
    assert calls["n"] >= 1


def test_install_writes_a_plist_that_does_not_double_log(launchctl, tmp_path):
    service.install("windows")

    written = next((tmp_path / "Library" / "LaunchAgents").glob("*.plist"))
    plist = plistlib.loads(written.read_bytes())
    assert plist["Label"] == service.SERVICE_LABEL
    assert plist["ProgramArguments"][-2:] == ["--os", "windows"]
    assert plist["EnvironmentVariables"][service.MANAGED_ENV_VAR] == "1"
    # launchd's stdio capture must not point at the file the agent logs to itself
    assert plist["StandardOutPath"] == plist["StandardErrorPath"]
    assert plist["StandardOutPath"] != str(service.log_path())
    assert plist["StandardOutPath"].endswith(".launchd.log")


def test_wait_until_unloaded_stops_as_soon_as_the_label_is_gone(launchctl):
    launchctl.unload_after = 2

    assert service._wait_until_unloaded("gui/501/x") is True
    assert launchctl.verbs() == ["print"] * 3


def test_wait_until_unloaded_gives_up_without_raising(monkeypatch, launchctl):
    launchctl.unload_after = 10**6  # never unloads
    ticks = [0.0, 0.0, 5.0, 11.0]

    def clock() -> float:
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    monkeypatch.setattr(service.time, "monotonic", clock)

    assert service._wait_until_unloaded("gui/501/x") is False


def _installed_plist(tmp_path) -> dict:
    written = next((tmp_path / "Library" / "LaunchAgents").glob("*.plist"))
    return plistlib.loads(written.read_bytes())


def test_no_notify_is_baked_into_the_plist(launchctl, tmp_path):
    """Windows has no environment channel, so argv is the only portable way."""
    service.install("macos", notify=False)
    assert "--no-notify" in _installed_plist(tmp_path)["ProgramArguments"]


def test_notifications_add_no_argument_when_left_on(launchctl, tmp_path):
    service.install("macos")
    arguments = _installed_plist(tmp_path)["ProgramArguments"]
    assert "--no-notify" not in arguments
    assert arguments[-2:] == ["--os", "macos"]


def test_observe_mode_is_baked_into_the_plist(launchctl, tmp_path):
    """A secondary machine must stay observe-only across a reboot."""
    service.install("macos", observe=True)
    assert "--observe" in _installed_plist(tmp_path)["ProgramArguments"]


def test_a_normal_install_does_not_observe(launchctl, tmp_path):
    service.install("macos")
    assert "--observe" not in _installed_plist(tmp_path)["ProgramArguments"]
