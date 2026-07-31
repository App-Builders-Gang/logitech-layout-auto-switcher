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
        time.sleep(0.4)
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
        time.sleep(0.4)
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


@pytest.mark.parametrize("target", ["mac", "win", "macos", "windows"])
def test_target_os_aliases_work_end_to_end(receiver, tmp_path, target):
    agent = Agent(config(tmp_path, target_os=target))
    assert agent.assert_once()
