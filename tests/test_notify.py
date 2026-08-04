"""Desktop notifications: throttling, safety, and never breaking the agent.

The behaviour worth pinning is restraint. The keyboard this was written against
corrects its platform every twelve seconds, so a notifier that faithfully reports
every switch would be unusable; almost everything here is about what it declines
to show.

Nothing in this file spawns a process -- the sender is injected, following the
`runner` convention in test_diagnostics.py.
"""

from __future__ import annotations

import threading
import time

import pytest

from logiswitch import notify


class Recorder:
    """A sender that records instead of notifying."""

    def __init__(self, fail: bool = False):
        self.sent: list[notify.Notification] = []
        self.fail = fail

    def __call__(self, note: notify.Notification) -> None:
        if self.fail:
            raise OSError("osascript is not having it today")
        self.sent.append(note)


@pytest.fixture
def recorder():
    return Recorder()


def quiet(recorder, **kwargs) -> notify.Notifier:
    """A notifier that delivers synchronously, with no thread to wind down."""
    return notify.Notifier(sender=recorder, **kwargs)


# -- throttling ---------------------------------------------------------------


def test_a_flapping_layout_produces_one_notification_not_hundreds(recorder):
    """The headline requirement: ~300 switches an hour must not be ~300 toasts."""
    notifier = quiet(recorder)
    accepted = [notifier.send(notify.SWITCHED, f"switch {i}") for i in range(300)]
    assert accepted.count(True) == 1
    assert accepted[0] is True, "the first one gets through immediately"


def test_kinds_throttle_independently(recorder):
    """A flapping layout must not silence an unrelated warning about the link."""
    notifier = quiet(recorder)
    assert notifier.send(notify.SWITCHED, "a switch")
    assert not notifier.send(notify.SWITCHED, "another switch")
    assert notifier.send(notify.LINK, "the link is unstable")
    assert notifier.send(notify.FAILED, "it would not switch")


def test_the_cooldown_expires(recorder):
    notifier = quiet(recorder, cooldown=0.0)
    assert notifier.send(notify.SWITCHED, "one")
    assert notifier.send(notify.SWITCHED, "two"), "a lapsed cooldown lets the next one by"


def test_suppressed_messages_are_counted_not_forgotten(recorder):
    """Hiding repeats is fine; pretending they did not happen is not."""
    notifier = notify.Notifier(sender=recorder, cooldown=0.0)
    notifier.send(notify.SWITCHED, "first")
    notifier._muted_until[notify.SWITCHED] = time.monotonic() + 60
    for _ in range(9):
        notifier.send(notify.SWITCHED, "hidden")
    notifier._muted_until[notify.SWITCHED] = 0.0

    notifier.send(notify.SWITCHED, "next visible one")
    notifier._drain()
    assert "9 similar hidden" in recorder.sent[-1].body


def test_a_standing_condition_is_quieter_than_an_event():
    """ "The layout keeps reverting" describes a situation; it should not nag."""
    assert notify.Notification(notify.FLAPPING, "x").cooldown > (
        notify.Notification(notify.SWITCHED, "x").cooldown
    )
    for kind in (notify.FLAPPING, notify.LINK, notify.INPUT_SOURCE):
        assert kind in notify.STANDING


# -- it must never break the agent --------------------------------------------


def test_a_sender_that_raises_is_swallowed():
    notifier = notify.Notifier(sender=Recorder(fail=True))
    assert notifier.deliver(notify.Notification(notify.SWITCHED, "boom")) is False


def test_disabled_notifiers_send_nothing(recorder):
    notifier = notify.Notifier(enabled=False, sender=recorder)
    assert notifier.send(notify.SWITCHED, "should not appear") is False
    assert recorder.sent == []


def test_an_unsupported_platform_disables_itself(monkeypatch):
    monkeypatch.setattr(notify, "is_macos", lambda: False)
    monkeypatch.setattr(notify, "is_windows", lambda: False)
    assert notify.default_sender() is None
    assert notify.Notifier().enabled is False
    assert "unsupported" in notify.backend_name()


def test_a_full_queue_drops_rather_than_blocking(recorder):
    """The worker thread is correcting a keyboard; it may not be made to wait."""
    notifier = notify.Notifier(sender=recorder, cooldown=0.0)
    for i in range(notify.QUEUE_SIZE + 20):
        notifier.send(notify.SWITCHED, f"message {i}")  # never started, so never drained
    assert notifier._queue.qsize() <= notify.QUEUE_SIZE


# -- the text reaches the OS without being interpretable ----------------------

HOSTILE = 'He said "hi"; rm -rf / && $(whoami) `id` \\ end'


def test_macos_passes_the_text_as_arguments_never_as_script():
    """A device name is untrusted input and must not become AppleScript."""
    note = notify.Notification(notify.SWITCHED, f"{HOSTILE} switched")
    command = notify.macos_command(note)
    assert command[-2] == note.body, "the body is one argv element, verbatim"
    assert command[-1] == note.title
    script = [command[i] for i in range(len(command)) if command[i - 1] == "-e"]
    assert script == list(notify._APPLESCRIPT), "the script is constant"
    assert not any(HOSTILE in line for line in script)
    assert "--" in command, "a body starting with a hyphen must not read as an option"


def test_windows_passes_the_text_in_the_environment(monkeypatch):
    monkeypatch.setattr(notify, "is_macos", lambda: False)
    monkeypatch.setattr(notify, "is_windows", lambda: True)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env", {})

    monkeypatch.setattr(notify.subprocess, "run", fake_run)
    notify._send_windows(notify.Notification(notify.SWITCHED, HOSTILE))

    assert captured["env"][notify.TOAST_BODY_ENV] == HOSTILE
    assert not any(HOSTILE in part for part in captured["command"]), (
        "the text must not appear in the command line at all"
    )


def test_the_windows_toast_carries_an_application_id():
    """A toast with no AUMID does not display, and does not say why."""
    command = " ".join(notify.windows_command())
    assert notify.POWERSHELL_AUMID in command
    assert "-NoProfile" in command and "-NonInteractive" in command


# -- lifecycle ----------------------------------------------------------------


def _notifier_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == "logiswitch-notifier"]


def test_the_delivery_thread_starts_and_stops_cleanly(recorder):
    notifier = notify.Notifier(sender=recorder, cooldown=0.0)
    notifier.start()
    assert _notifier_threads()
    notifier.send(notify.SWITCHED, "delivered off the worker thread")
    deadline = time.time() + 3.0
    while not recorder.sent and time.time() < deadline:
        time.sleep(0.01)
    assert recorder.sent, "the queued notification was delivered"
    notifier.stop()
    notifier.stop()  # idempotent
    assert not _notifier_threads()


def test_stopping_a_notifier_that_never_started_is_harmless(recorder):
    quiet(recorder).stop()


def test_a_disabled_notifier_starts_no_thread(recorder):
    notifier = notify.Notifier(enabled=False, sender=recorder)
    notifier.start()
    assert not _notifier_threads()
    notifier.stop()


# -- the script has to be valid PowerShell, not merely contain the right words ---


def test_the_script_uses_no_line_continuations():
    """A backtick continuation inside a type literal is a parse error.

    This shipped broken: the script wrapped `[Windows.UI.Notifications...]` across
    two lines for readability, PowerShell rejected it with "Missing ] at end of
    attribute or type literal", and every toast on every Windows machine failed --
    while these tests, which only checked the text was present, passed.
    """
    for number, line in enumerate(notify._POWERSHELL_SCRIPT.splitlines(), 1):
        assert not line.rstrip().endswith("`"), f"line {number} continues a statement"


def test_every_bracketed_type_literal_is_closed_on_its_own_line():
    for number, line in enumerate(notify._POWERSHELL_SCRIPT.splitlines(), 1):
        assert line.count("[") == line.count("]"), (
            f"line {number} splits a type literal across lines: {line!r}"
        )


@pytest.mark.skipif(not notify.is_windows(), reason="needs a real PowerShell")
def test_powershell_can_actually_parse_the_script():
    """The check that would have caught it: ask PowerShell, do not guess.

    Both out-parameters are declared before being passed. ``[ref]`` binds a *variable
    path*, so handing it a name that does not exist yet fails with
    ``NonExistingVariableReference`` -- and that failure looks exactly like the script
    being rejected. The first version of this test did that, so it failed on every
    Windows run for a reason that had nothing to do with the script it was checking:
    a test written to stop guessing about PowerShell, which guessed about PowerShell.
    """
    import os
    import subprocess

    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop';"
            "$tokens=$null; $errors=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseInput("
            "$env:LOGISWITCH_SCRIPT, [ref]$tokens, [ref]$errors);"
            "if ($errors) { $errors | ForEach-Object { $_.Message }; exit 1 }",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "LOGISWITCH_SCRIPT": notify._POWERSHELL_SCRIPT},
    )
    # Parse errors are printed to stdout by the loop above, but anything that stops
    # the harness itself lands on stderr -- and reporting only stdout is why the
    # original failure arrived as a blank message.
    assert completed.returncode == 0, (
        f"PowerShell rejected the script.\nstdout: {completed.stdout}\nstderr: {completed.stderr}"
    )
