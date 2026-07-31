import logging
import time

import fakehid
import pytest

from logiswitch.agent import Agent, AgentConfig
from logiswitch.watchers import DeviceEvent


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


def wait_until_settled(agent, receiver, index=None, platform=0):
    """Block until the agent has finished its *first* assert, not merely started it.

    ``_driven`` is populated while the session is being built, before the initial
    ``ensure_os`` runs. A test that flips the platform the moment ``_driven``
    appears is racing that first assert, which then quietly puts the platform back
    -- so the test either fails, or passes for entirely the wrong reason.
    """
    index = fakehid.MX_KEYS_INDEX if index is None else index
    wait_for_session(agent)
    assert wait_for(lambda: receiver.devices[index].platform == platform), (
        "the agent never completed its initial assert"
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
    assert reloaded._hints["Logi Bolt receiver"] == fakehid.MX_KEYS_INDEX


def test_contention_is_reported_after_repeated_reverts(receiver, tmp_path, caplog):
    """Simulate Logi Options+ pushing the platform straight back."""
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    agent = Agent(config(tmp_path))
    with caplog.at_level(logging.WARNING, logger="logiswitch.agent"):
        for _ in range(3):
            keyboard.platform = 1  # something else reverted it to macOS
            agent.assert_once()
    assert any("fighting us" in record.message for record in caplog.records)
    assert any("Options+" in record.getMessage() for record in caplog.records)


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
    """A mouse sprays movement events; none of them mean the keyboard moved."""
    agent = Agent(config(tmp_path, force_polling=True))
    agent.start()
    try:
        wait_until_settled(agent, receiver)
        assert fakehid.MX_MASTER_INDEX not in agent._driven
        receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
        for _ in range(5):
            agent._on_hidpp_frame(bytes([0x11, fakehid.MX_MASTER_INDEX, 0x09, 0x20] + [0] * 16))
        # A reply to one of our own requests must not count either (swId 0x0E).
        agent._on_hidpp_frame(bytes([0x11, fakehid.MX_KEYS_INDEX, 0x10, 0x1E] + [0] * 16))
        time.sleep(1.0)
        assert receiver.devices[fakehid.MX_KEYS_INDEX].platform == 1
    finally:
        agent.stop()
        agent.shutdown()


@pytest.mark.parametrize("target", ["mac", "win", "macos", "windows"])
def test_target_os_aliases_work_end_to_end(receiver, tmp_path, target):
    agent = Agent(config(tmp_path, target_os=target))
    assert agent.assert_once()
