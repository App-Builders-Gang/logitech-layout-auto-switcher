import logging
import time

import fakehid
import pytest

from logiswitch import diagnostics
from logiswitch.agent import Agent, AgentConfig
from logiswitch.platform.watchers import DeviceEvent


def config(tmp_path, **kwargs):
    defaults = dict(target_os="windows", debounce=0.0, reassert_interval=0.0)
    defaults.update(kwargs)
    return AgentConfig(state_file=tmp_path / "state.json", **defaults)


def wait_for(predicate, timeout=10.0, interval=0.02):
    """Poll `predicate` until it holds. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_for_session(agent):
    """Block until the agent has probed the receiver and knows what it drives.

    A fixed settle sleep is not enough: building a session means a full device
    scan, and a loaded CI runner can take well over a second. Asserting before
    that finishes made two tests flaky -- and misleadingly so, because with no
    session established the agent deliberately treats *any* device chatter as
    "something came back", so an early assertion changed the behaviour it was
    trying to observe.
    """
    assert wait_for(lambda: bool(agent._driven)), "agent never established a session"


def wait_until_settled(agent, receiver=None, index=None, platform=None):
    """Block until the agent has *finished* a pass, not merely started one.

    Two weaker signals were tried first and both let the race through:

    * a fixed 400 ms sleep -- too short on a loaded CI runner;
    * ``_driven`` becoming non-empty -- populated at the end of session building,
      which is still a whole device scan before any platform is read.

    A test that flips the platform in that window is racing the agent's own first
    pass, which then quietly puts the platform back. Waiting on the completed-pass
    counter is the only signal that actually means "the agent is done".
    """
    # Both conditions matter: the counter also ticks for a pass that found no
    # endpoint at all, and the "ignore chatter" rule only engages once the agent
    # knows what it drives.
    assert wait_for(lambda: agent._apply_count >= 1 and bool(agent._driven)), (
        "the agent never completed a pass over a device it drives"
    )
    if receiver is not None and platform is not None:
        index = fakehid.MX_KEYS_INDEX if index is None else index
        assert wait_for(lambda: receiver.devices[index].platform == platform), (
            f"device {index} never reached platform {platform}"
        )


def test_assert_once_switches_and_cleans_up(receiver, tmp_path):
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 1  # currently macOS
    agent = Agent(config(tmp_path))
    assert agent.assert_once()
    assert keyboard.platform == 0
    assert receiver.handles == [], "one-shot must not leave a handle open"


def test_assert_once_is_a_no_op_when_already_correct(receiver, tmp_path):
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 0
    agent = Agent(config(tmp_path))
    assert agent.assert_once()
    assert keyboard.set_calls == []


def test_mouse_only_receiver_opens_no_handle(monkeypatch, tmp_path):
    receiver = fakehid.install(monkeypatch, fakehid.FakeReceiver([fakehid.mx_master_3s()]))
    agent = Agent(config(tmp_path))
    assert not agent.assert_once()
    assert receiver.handles == []


def test_every_supported_device_is_driven(monkeypatch, tmp_path):
    keys = fakehid.mx_keys_s(index=5)
    craft = fakehid.craft_dualplatform(index=2)
    keys.platform = 1
    craft.platform = 0
    fakehid.install(monkeypatch, fakehid.FakeReceiver([keys, craft, fakehid.mx_master_3s()]))
    agent = Agent(config(tmp_path))
    assert agent.assert_once()
    assert keys.platform == 0, "MULTIPLATFORM keyboard switched to Windows"
    assert craft.platform == 1, "DUALPLATFORM keyboard switched to Android/Windows"


def test_device_index_hint_is_persisted(receiver, tmp_path):
    cfg = config(tmp_path)
    Agent(cfg).assert_once()
    assert cfg.state_file.exists()
    reloaded = Agent(cfg)
    assert reloaded._hints["Logi Bolt receiver"] == [fakehid.MX_KEYS_INDEX]


def _revert_repeatedly(receiver, tmp_path, caplog, rounds=5, settle=False) -> None:
    """Drive the cycle the real world produces: revert, correct, revert, correct...

    With `settle`, a clean pass runs between every revert -- which is what actually
    happens, because the agent re-checks three seconds after each correction and
    finds its own work intact.
    """
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    agent = Agent(config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="logiswitch.agent"):
        for _ in range(rounds):
            keyboard.platform = 1  # something else reverted it to macOS
            agent.assert_once()
            if settle:
                agent.assert_once()  # the 3s re-check: nothing to do


def test_a_platform_reverting_forever_is_reported(receiver, tmp_path, caplog):
    """The regression behind 2178 silent corrections in a real log.

    Every correction is followed by a check that succeeds, so a counter demanding
    *consecutive* changes was reset by the agent's own success and never once
    reached its threshold. A keyboard reverted every twelve seconds for two days
    produced no warning at all.
    """
    _revert_repeatedly(receiver, tmp_path, caplog, rounds=6, settle=True)
    assert any("corrected the platform" in record.getMessage() for record in caplog.records), (
        "a revert that is corrected each time must still be reported"
    )


def test_the_revert_warning_is_logged_once_not_per_occurrence(receiver, tmp_path, caplog):
    _revert_repeatedly(receiver, tmp_path, caplog, rounds=12, settle=True)
    warnings = [r for r in caplog.records if "corrected the platform" in r.getMessage()]
    assert len(warnings) == 1


def _revert_three_times(receiver, tmp_path, caplog) -> None:
    _revert_repeatedly(receiver, tmp_path, caplog, rounds=5)


def test_contention_names_the_software_that_is_actually_running(
    monkeypatch, receiver, tmp_path, caplog
):
    """Simulate Logi Options+ pushing the platform straight back."""
    monkeypatch.setattr(diagnostics, "competing_software", lambda: ["logioptionsplus_agent"])
    _revert_three_times(receiver, tmp_path, caplog)
    assert any("corrected the platform" in record.getMessage() for record in caplog.records)
    assert any("logioptionsplus_agent" in record.getMessage() for record in caplog.records)


def test_contention_does_not_blame_software_that_is_absent(monkeypatch, receiver, tmp_path, caplog):
    """The old warning asserted Logi Options+ was the cause without ever looking."""
    monkeypatch.setattr(diagnostics, "competing_software", lambda: [])
    _revert_three_times(receiver, tmp_path, caplog)
    messages = [record.getMessage() for record in caplog.records]
    assert any("corrected the platform" in message for message in messages)
    assert not any("logioptions" in message.lower() for message in messages)


def test_no_contention_warning_in_steady_state(receiver, tmp_path, caplog):
    agent = Agent(config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="logiswitch.agent"):
        for _ in range(5):
            agent.assert_once()
    assert not caplog.records


def test_agent_starts_and_stops_cleanly(receiver, tmp_path):
    agent = Agent(config(tmp_path, force_polling=True))
    agent.start()
    try:
        deadline = time.time() + 5
        while receiver.devices[fakehid.MX_KEYS_INDEX].platform != 0 and time.time() < deadline:
            time.sleep(0.05)
    finally:
        agent.stop()
        agent.shutdown()
    assert receiver.handles == [], "shutdown must close every handle"


def test_device_event_rebuilds_the_session(receiver, tmp_path):
    agent = Agent(config(tmp_path, force_polling=True))
    agent.start()
    try:
        wait_until_settled(agent, receiver)
        receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
        agent._on_device_event(DeviceEvent.ARRIVED, "test")
        deadline = time.time() + 5
        while receiver.devices[fakehid.MX_KEYS_INDEX].platform != 0 and time.time() < deadline:
            time.sleep(0.05)
        assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 0
    finally:
        agent.stop()
        agent.shutdown()


def test_wake_notification_triggers_a_reassert(receiver, tmp_path):
    agent = Agent(config(tmp_path, force_polling=True))
    agent.start()
    try:
        wait_until_settled(agent, receiver)
        receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
        wake = bytes([0x10, fakehid.MX_KEYS_INDEX, 0x41, 0x04, 0, 0, 0])
        agent._on_hidpp_frame(wake)
        deadline = time.time() + 5
        while receiver.devices[fakehid.MX_KEYS_INDEX].platform != 0 and time.time() < deadline:
            time.sleep(0.05)
        assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 0
    finally:
        agent.stop()
        agent.shutdown()


def test_a_driven_device_talking_again_triggers_a_reassert(receiver, tmp_path):
    """The real Easy-Switch signal: no 0x41, just the keyboard speaking up.

    A Bolt receiver stays enumerated across a channel move and forwards no
    HID++ 1.0 connect notification, so the first sign the keyboard is back is
    an ordinary 2.0 event -- 0x4220 lock-key state on an MX Keys S.
    """
    agent = Agent(config(tmp_path, force_polling=True))
    agent.start()
    try:
        wait_until_settled(agent, receiver)
        assert fakehid.MX_KEYS_INDEX in agent._driven
        receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
        # [long report, device index, feature index 0x0E, function 0 | swId 0]
        agent._on_hidpp_frame(bytes([0x11, fakehid.MX_KEYS_INDEX, 0x0E, 0x00] + [0] * 16))
        deadline = time.time() + 5
        while receiver.devices[fakehid.MX_KEYS_INDEX].platform != 0 and time.time() < deadline:
            time.sleep(0.05)
        assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 0
    finally:
        agent.stop()
        agent.shutdown()


# -- chatter from a device that never left ------------------------------------
#
# The calculator-key regression. A key Logi Options+ diverts (HID++ 0x1B04) emits a
# notification instead of a keystroke, and an unsolicited notification from a driven
# device used to be treated as "the keyboard came back" -- scheduling a platform read
# 200ms later, on the same single-slot receiver Options+ was using to service the
# keypress. With the agent running the key did nothing; stopped, it worked at once.
# The gate: a return is only possible when the device was away, and only the heartbeat
# records that. These tests pin both halves of the question plus the escape hatch.


def _lock_key_state(index=fakehid.MX_KEYS_INDEX):
    """What an MX Keys S sends unprompted: long report, feature 0x0E, swId 0."""
    return bytes([0x11, index, 0x0E, 0x00] + [0] * 16)


def test_chatter_from_a_settled_keyboard_is_not_a_reconnect(receiver, tmp_path):
    """The regression test for the calculator key.

    A keyboard that is present, answering, and talking is chattering, not returning:
    nothing it says while it never left can mean it came back.
    """
    from logiswitch import trace

    trace.HEALTH.reset()
    # reassert_interval must be non-zero or the gate is bypassed (see the next test).
    agent = Agent(config(tmp_path, force_polling=True, reassert_interval=20.0))
    agent.start()
    try:
        wait_until_settled(agent, receiver, platform=0)
        assert agent._absent_since is None, "a device that answered is not absent"
        agent._on_hidpp_frame(_lock_key_state())
        assert agent._queue.empty(), "chatter from a present device must not wake the agent"
        assert trace.HEALTH.get("settled_chatter") == 1
    finally:
        agent.stop()
        agent.shutdown()


def test_chatter_from_a_keyboard_that_stopped_answering_is_still_a_reconnect(receiver, tmp_path):
    """The Easy-Switch guard: the gate must not swallow a genuine return.

    A Bolt receiver forwards no connect notification across a channel move, so the
    only sign a keyboard came back is that it starts talking -- and the heartbeat is
    what records that it went. With that record set, the same chatter that the
    previous test ignored must now wake the agent.
    """
    agent = Agent(config(tmp_path, force_polling=True, reassert_interval=20.0))
    agent.start()
    try:
        wait_until_settled(agent, receiver, platform=0)
        agent._absent_since = time.monotonic()  # the heartbeat found nothing answering
        agent._on_hidpp_frame(_lock_key_state())
        kind, payload = agent._queue.get_nowait()
        assert (kind.value, payload) == ("device_woke", fakehid.MX_KEYS_INDEX)
    finally:
        agent.stop()
        agent.shutdown()


def test_with_the_heartbeat_off_chatter_is_the_only_signal_there_is(receiver, tmp_path):
    """The escape hatch: reassert_interval=0 disables the only absence detector.

    With no heartbeat running, nothing records that a device went away, so chatter
    can no longer be distinguished from a return and is trusted exactly as before.
    This is also why every other test in the file passes unchanged: their configs
    default reassert_interval to 0.0.
    """
    agent = Agent(config(tmp_path, force_polling=True, reassert_interval=0.0))
    agent.start()
    try:
        wait_until_settled(agent, receiver, platform=0)
        agent._on_hidpp_frame(_lock_key_state())
        kind, payload = agent._queue.get_nowait()
        assert (kind.value, payload) == ("device_woke", fakehid.MX_KEYS_INDEX)
    finally:
        agent.stop()
        agent.shutdown()


def test_a_settled_agent_transmits_nothing_while_the_keyboard_chatters(receiver, tmp_path):
    """End-to-end: chatter moves neither the request counter nor the apply counter.

    The previous tests prove the queue stays empty; this one proves no frame reaches
    the device either, which is the property that actually stops the collision with
    Logi Options+.
    """
    from logiswitch import trace

    trace.HEALTH.reset()
    agent = Agent(config(tmp_path, force_polling=True, reassert_interval=60.0))
    agent.start()
    try:
        wait_until_settled(agent, receiver, platform=0)
        requests_before = trace.HEALTH.get("requests")
        applies_before = agent._apply_count
        # Twenty lock-key notifications -- three times the 0.2s a wake would have
        # scheduled a pass within, so a stray pass has every chance to land and be
        # caught.
        for _ in range(20):
            agent._on_hidpp_frame(_lock_key_state())
        time.sleep(0.6)
        assert trace.HEALTH.get("requests") == requests_before
        assert agent._apply_count == applies_before
    finally:
        agent.stop()
        agent.shutdown()


def test_a_write_that_would_not_confirm_is_not_logged_as_nothing_answering(receiver, tmp_path):
    """A pass that fails because a write would not confirm still reached the device.

    `applied > 0 and failed == 0` used to feed the presence record, so a keyboard that
    accepted and then dropped a write logged "nothing is answering" about a device
    sitting on the desk -- and left `_absent_since` set, which is now the record that
    decides whether the next notification means the keyboard returned.
    """
    from logiswitch.hidpp import protocol as p

    real = receiver._multiplatform

    def acknowledge_but_ignore(frame, dev, function, params):
        if function == p.MP_SET_HOST_PLATFORM:
            return receiver._pad(frame, b"")  # accepted; the platform does not change
        return real(frame, dev, function, params)

    agent = Agent(config(tmp_path, force_polling=True, reassert_interval=0.5))
    agent.start()
    try:
        wait_until_settled(agent, receiver, platform=0)
        receiver._multiplatform = acknowledge_but_ignore
        receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1  # wrong; the write won't stick
        assert wait_for(lambda: agent._apply_count >= 2), "the failing pass never ran"
        assert agent._reached is True, "the device answered even though the write did not stick"
        assert agent._absent_since is None, "an answering device is not absent"
    finally:
        receiver._multiplatform = real
        agent.stop()
        agent.shutdown()


def test_a_reconnect_restarts_the_backoff_instead_of_inheriting_it(receiver, tmp_path):
    """The 32-second lag: a device that was away leaves _retry at its 30s ceiling.

    A returning device announces itself before it will answer requests, so the
    check that follows the announcement usually fails. If that failure inherits
    the ceiling reached while the device was away, the layout stays wrong for
    another half minute -- which is exactly what the agent did.
    """
    agent = Agent(config(tmp_path, force_polling=True))
    agent._retry = 30.0
    agent._on_hidpp_frame(bytes([0x11, fakehid.MX_KEYS_INDEX, 0x0E, 0x00] + [0] * 16))
    kind, payload = agent._queue.get_nowait()
    assert (kind.value, payload) == ("device_woke", fakehid.MX_KEYS_INDEX)

    agent.start()
    try:
        deadline = time.time() + 5
        while agent._retry == 30.0 and time.time() < deadline:
            time.sleep(0.05)
        assert agent._retry < 30.0
    finally:
        agent.stop()
        agent.shutdown()


def test_an_unknown_device_is_heard_when_we_have_none_of_our_own(receiver, tmp_path):
    """With no session, anything talking is worth a look -- that is why we retry."""
    agent = Agent(config(tmp_path, force_polling=True))
    assert not agent._driven
    agent._on_hidpp_frame(bytes([0x11, fakehid.MX_MASTER_INDEX, 0x09, 0x20] + [0] * 16))
    kind, _payload = agent._queue.get_nowait()
    assert kind.value == "device_woke"


def test_chatter_from_a_device_we_do_not_drive_is_ignored(receiver, tmp_path):
    """A mouse sprays movement events; none of them mean the keyboard moved.

    Asserted on the queue rather than on the platform. Watching the platform would
    now be measuring the adaptive re-check instead -- the agent re-reads on its own
    schedule after a correction, so an unchanged platform would prove nothing about
    how the chatter was classified.
    """
    agent = Agent(config(tmp_path, force_polling=True))
    # Pretend a session exists, without the cost and timing of really building one.
    agent._driven = frozenset({fakehid.MX_KEYS_INDEX})

    for _ in range(5):
        agent._on_hidpp_frame(bytes([0x11, fakehid.MX_MASTER_INDEX, 0x09, 0x20] + [0] * 16))
    # A reply to one of our own requests must not count either (swId 0x0E).
    agent._on_hidpp_frame(bytes([0x11, fakehid.MX_KEYS_INDEX, 0x10, 0x1E] + [0] * 16))
    assert agent._queue.empty(), "neither mouse chatter nor our own reply is a wake-up"

    # ... whereas an unsolicited frame from the keyboard itself is.
    agent._on_hidpp_frame(bytes([0x11, fakehid.MX_KEYS_INDEX, 0x10, 0x00] + [0] * 16))
    kind, _payload = agent._queue.get_nowait()
    assert kind.value == "device_woke"


@pytest.mark.parametrize("target", ["mac", "win", "macos", "windows"])
def test_target_os_aliases_work_end_to_end(receiver, tmp_path, target):
    agent = Agent(config(tmp_path, target_os=target))
    assert agent.assert_once()


# -- link health ---------------------------------------------------------------


def _connect_notification(index=fakehid.MX_KEYS_INDEX, established=True):
    from logiswitch.hidpp import protocol as p

    flags = 0x00 if established else p.NOTIF_LINK_NOT_ESTABLISHED
    return bytes([0x10, index, p.NOTIF_DEVICE_CONNECTION, 0x04, flags, 0x00, 0x00])


def test_repeated_reconnects_are_reported_as_an_unstable_link(receiver, tmp_path, caplog):
    """The only handle the daemon has on genuinely garbled output.

    It never sees a keystroke, but a link that keeps collapsing is a link dropping
    and repeating them.
    """
    from logiswitch import agent as agent_module

    agent = Agent(config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="logiswitch.agent"):
        for _ in range(agent_module.CHURN_THRESHOLD + 1):
            agent._on_hidpp_frame(_connect_notification())
    assert any("wireless link has re-established" in r.getMessage() for r in caplog.records)


def test_a_single_reconnect_is_not_called_unstable(receiver, tmp_path, caplog):
    """Switching a KVM produces one reconnect and must stay silent."""
    agent = Agent(config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="logiswitch.agent"):
        agent._on_hidpp_frame(_connect_notification())
    assert not caplog.records


def test_a_dropped_link_is_counted_separately_from_a_reconnect(receiver, tmp_path):
    from logiswitch import trace

    agent = Agent(config(tmp_path))
    agent._on_hidpp_frame(_connect_notification(established=False))
    assert trace.HEALTH.get("link_drops") == 1
    assert trace.HEALTH.get("reconnects") == 0


def test_a_reply_from_feature_index_0x41_is_not_treated_as_a_reconnect(receiver, tmp_path):
    """Byte 2 is a feature index on a HID++ 2.0 reply, not a sub-id."""
    from logiswitch import trace

    agent = Agent(config(tmp_path))
    reply = bytes([0x11, fakehid.MX_KEYS_INDEX, 0x41, 0x2E]) + bytes(16)
    agent._on_hidpp_frame(reply)
    assert trace.HEALTH.get("reconnects") == 0


# -- the log stays a timeline --------------------------------------------------


def test_the_agent_reports_itself_alive_even_when_nothing_changes(receiver, tmp_path, caplog):
    """A healthy agent used to log one line and then fall silent forever.

    That left "it went wrong at 14:32" with nothing to be checked against.
    """
    agent = Agent(config(tmp_path))
    agent.assert_once()
    with caplog.at_level(logging.INFO, logger="logiswitch.agent"):
        agent._log_steady_summary()
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "steady on " in message
    assert "MX Keys S=" in message
    assert "input=" in message


def test_a_write_that_does_not_take_is_not_logged_as_a_switch(receiver, tmp_path, caplog):
    """The log must not announce a switch the device contradicted.

    Reporting success there is how a log ends up insisting everything is fine while
    the wrong characters keep appearing.
    """
    from logiswitch.hidpp import protocol as p

    real = receiver._multiplatform

    def acknowledge_but_ignore(frame, dev, function, params):
        if function == p.MP_SET_HOST_PLATFORM:
            return receiver._pad(frame, b"")  # accepted; nothing changes
        return real(frame, dev, function, params)

    receiver._multiplatform = acknowledge_but_ignore
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1  # not the windows target
    agent = Agent(config(tmp_path))
    with caplog.at_level(logging.INFO, logger="logiswitch.agent"):
        ok = agent.assert_once()
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "did NOT switch" in message
    assert "switched MX Keys S to" not in message
    assert not ok, "an unapplied write must count as a failed pass so it is retried"


# -- notifications -------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.sent = []

    def __call__(self, note):
        self.sent.append(note)


def _agent_with_recorder(tmp_path, **kwargs):
    from logiswitch import notify

    recorder = _Recorder()
    agent = Agent(config(tmp_path, **kwargs))
    agent.notifier = notify.Notifier(
        enabled=agent.cfg.notify, sender=recorder, cooldown=kwargs.pop("cooldown", None)
    )
    return agent, recorder


def test_a_switch_notifies_once_even_when_it_keeps_reverting(receiver, tmp_path):
    """The live behaviour on this hardware: correcting every 12s must not spam."""
    from logiswitch import notify

    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    agent, recorder = _agent_with_recorder(tmp_path)
    for _ in range(20):
        keyboard.platform = 1  # reverted again
        agent.assert_once()
    agent.notifier._drain()

    switched = [n for n in recorder.sent if n.kind == notify.SWITCHED]
    assert len(switched) == 1, f"expected one switch notification, got {len(switched)}"
    assert "MX Keys S" in switched[0].body
    assert "layout" in switched[0].body


def test_a_persistent_revert_is_reported_as_a_standing_condition(receiver, tmp_path):
    from logiswitch import notify

    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    agent, recorder = _agent_with_recorder(tmp_path)
    for _ in range(20):
        keyboard.platform = 1
        agent.assert_once()
        agent.assert_once()
    agent.notifier._drain()

    flapping = [n for n in recorder.sent if n.kind == notify.FLAPPING]
    assert len(flapping) == 1
    assert "keeps reverting" in flapping[0].body
    assert "doctor" in flapping[0].body, "it must say what to do next"


def test_a_write_that_does_not_take_notifies_differently(receiver, tmp_path):
    from logiswitch import notify
    from logiswitch.hidpp import protocol as p

    real = receiver._multiplatform

    def acknowledge_but_ignore(frame, dev, function, params):
        if function == p.MP_SET_HOST_PLATFORM:
            return receiver._pad(frame, b"")
        return real(frame, dev, function, params)

    receiver._multiplatform = acknowledge_but_ignore
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
    agent, recorder = _agent_with_recorder(tmp_path)
    agent.assert_once()
    agent.notifier._drain()

    kinds = {n.kind for n in recorder.sent}
    assert notify.FAILED in kinds
    assert notify.SWITCHED not in kinds, "a write that did not take is not a switch"


def test_notifications_can_be_turned_off(receiver, tmp_path):
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    agent, recorder = _agent_with_recorder(tmp_path, notify=False)
    keyboard.platform = 1
    agent.assert_once()
    agent.notifier._drain()
    assert recorder.sent == []


def test_a_broken_notifier_does_not_break_the_agent(receiver, tmp_path):
    """A notification that can take the agent down is worse than no notification."""
    from logiswitch import notify

    def explode(_note):
        raise OSError("the notification subsystem is on fire")

    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 1
    agent = Agent(config(tmp_path))
    agent.notifier = notify.Notifier(sender=explode)
    assert agent.assert_once(), "the pass must still succeed"
    assert agent.notifier._drain() == 0
    assert keyboard.platform == 0, "and the keyboard must still have been corrected"
