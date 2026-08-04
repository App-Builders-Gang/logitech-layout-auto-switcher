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


def test_windows_passes_the_text_to_the_native_toast(monkeypatch):
    """The body still reaches the OS verbatim, now through the in-process COM call."""
    from logiswitch import _wintoast

    captured = {}

    def fake_show(title, body):
        captured["title"] = title
        captured["body"] = body

    monkeypatch.setattr(_wintoast, "show_toast", fake_show)
    notify._send_windows(notify.Notification(notify.SWITCHED, HOSTILE))

    assert captured["title"] == "logiswitch"
    assert captured["body"] == HOSTILE


def test_windows_toast_escapes_untrusted_text_into_xml():
    """A device name is untrusted input; it must become toast text, not markup.

    There is no command line any more, so the old worry -- the body reaching a
    shell -- is gone. The new one is the same shape: the body must not become XML
    structure. ``_toast_xml`` escapes it, and the result must still parse.
    """
    from xml.etree import ElementTree

    from logiswitch import _wintoast

    payload = _wintoast._toast_xml("logiswitch", HOSTILE)
    ElementTree.fromstring(payload)  # ill-formed XML would raise here
    assert HOSTILE not in payload, "the raw body must not survive unescaped"
    assert "&amp;" in payload, "the ampersands in HOSTILE are escaped"


def test_windows_toast_carries_an_application_id():
    """A toast with no AUMID does not display, and does not say why."""
    from logiswitch import _wintoast

    assert _wintoast._APP_USER_MODEL_ID, "a registered AUMID must be set"


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


# -- the COM wiring has to actually work, not merely look right ----------------


@pytest.mark.skipif(not notify.is_windows(), reason="needs a real Windows toast runtime")
def test_windows_can_actually_raise_a_toast():
    """The check that would have caught the PowerShell bug: do it for real.

    The previous version shipped broken because its tests only inspected the
    script's text. The native version is verified the same way the old one failed
    in production: by asking the OS to raise a toast and letting any COM error
    raise. Nothing is parsed or pattern-matched -- the COM chain either succeeds
    or it does not.
    """
    notify._send_windows(
        notify.Notification(notify.SWITCHED, "logiswitch self-test toast", "logiswitch")
    )
