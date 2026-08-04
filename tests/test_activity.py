"""Taking turns: N machines sharing one keyboard through a KVM.

The rule under test is that the machine being typed on owns the keyboard and the
rest stand down. It needs no negotiation because only one machine can be receiving
keystrokes at a time -- but it must be *conservative*: a lone agent, or one that
cannot measure activity at all, has to keep working exactly as before.
"""

from __future__ import annotations

import fakehid
import pytest

from logiswitch import activity, trace
from logiswitch import agent as agent_module
from logiswitch.agent import Agent, AgentConfig
from logiswitch.hidpp import protocol as p

#: Captured at import, before the autouse `machine_in_use` fixture stubs it.
REAL_SECONDS_SINCE_INPUT = activity.seconds_since_input


def config(tmp_path, **kwargs):
    defaults = dict(target_os="windows", debounce=0.0, reassert_interval=0.0, notify=False)
    defaults.update(kwargs)
    return AgentConfig(state_file=tmp_path / "state.json", **defaults)


def make_agent(tmp_path, idle=None, monkeypatch=None, **kwargs):
    if monkeypatch is not None:
        monkeypatch.setattr(activity, "seconds_since_input", lambda: idle)
        monkeypatch.setattr(agent_module.activity, "seconds_since_input", lambda: idle)
    return Agent(config(tmp_path, **kwargs))


def saw_peer(agent, sw_id=0x0E):
    """Simulate another machine writing the platform."""
    feature = fakehid.MULTIPLATFORM_INDEX
    agent._platform_features = {fakehid.MX_KEYS_INDEX: feature}
    frame = bytes(
        [0x11, fakehid.MX_KEYS_INDEX, feature, p.function_byte(p.MP_SET_HOST_PLATFORM, sw_id)]
    ) + bytes(16)
    agent._on_hidpp_frame(frame)


# -- reading activity ----------------------------------------------------------


def test_idle_time_never_raises(monkeypatch):
    monkeypatch.setattr(activity, "seconds_since_input", REAL_SECONDS_SINCE_INPUT)
    monkeypatch.setattr(activity, "is_macos", lambda: True)
    monkeypatch.setattr(
        activity, "_macos_idle", lambda: (_ for _ in ()).throw(OSError("no window server"))
    )
    assert activity.seconds_since_input() is None


def test_an_unsupported_platform_cannot_arbitrate(monkeypatch):
    monkeypatch.setattr(activity, "seconds_since_input", REAL_SECONDS_SINCE_INPUT)
    monkeypatch.setattr(activity, "is_macos", lambda: False)
    monkeypatch.setattr(activity, "is_windows", lambda: False)
    assert activity.seconds_since_input() is None
    assert activity.available() is False


def test_describe_reads_naturally():
    assert activity.describe(None) == "unknown"
    assert activity.describe(0.2) == "in use now"
    assert activity.describe(42.0) == "idle 42s"


# -- peer detection ------------------------------------------------------------


def test_a_foreign_platform_write_is_recognised(receiver, tmp_path, caplog):
    agent = Agent(config(tmp_path))
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        saw_peer(agent, sw_id=0x07)
    assert agent._peer_present()
    assert "another machine is setting" in caplog.text
    assert trace.HEALTH.get("foreign_platform_writes") == 1


def test_an_old_logiswitch_peer_is_named_as_such(receiver, tmp_path, caplog):
    """swId 0x0E is this project's own former fixed id -- say so, and say upgrade."""
    agent = Agent(config(tmp_path))
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        saw_peer(agent, sw_id=p.SW_ID)
    assert "OLD logiswitch" in caplog.text
    assert "update logiswitch on that machine" in caplog.text


def test_our_own_replies_are_not_mistaken_for_a_peer(receiver, tmp_path):
    """A reply with swId 0 is a notification, not somebody else's write."""
    agent = Agent(config(tmp_path))
    agent._platform_features = {fakehid.MX_KEYS_INDEX: fakehid.MULTIPLATFORM_INDEX}
    notification = bytes([0x11, fakehid.MX_KEYS_INDEX, fakehid.MULTIPLATFORM_INDEX, 0x00]) + bytes(
        16
    )
    agent._on_hidpp_frame(notification)
    assert not agent._peer_present()


def test_peer_status_eventually_decays(receiver, tmp_path, monkeypatch):
    """It is remembered for a long time, but not forever."""
    agent = Agent(config(tmp_path))
    saw_peer(agent)
    assert agent._peer_present()
    monkeypatch.setattr(agent_module, "PEER_MEMORY", 0.0)
    assert not agent._peer_present()


def test_peer_memory_outlasts_a_quiet_spell(receiver, tmp_path):
    """Short memory oscillates, and this is why.

    Once the winning machine has the platform right it stops writing. With a short
    window every idle machine then forgets the peer, starts writing again, collides,
    stands down -- forever, on a cycle one window long. Long memory is safe because
    standing down only applies while idle, and typing takes the keyboard back.
    """
    assert agent_module.PEER_MEMORY >= 600.0, "must outlast any realistic quiet spell"
    agent = Agent(config(tmp_path))
    saw_peer(agent)
    assert agent._peer_present(), "still remembered after the peer falls silent"


def test_peer_state_is_per_agent_not_process_wide(receiver, tmp_path):
    """Ten agents in one process must not share one belief about peers.

    The obvious implementation reads the process-global health counters, which is
    invisible with one agent per machine and completely wrong in a shared process.
    """
    seen_it = Agent(config(tmp_path))
    innocent = Agent(config(tmp_path))
    saw_peer(seen_it)
    assert seen_it._peer_present()
    assert not innocent._peer_present(), "one agent's observation is not another's"


# -- the ownership rule --------------------------------------------------------


def test_an_idle_machine_yields_to_a_peer(receiver, tmp_path, monkeypatch):
    agent = make_agent(tmp_path, idle=120.0, monkeypatch=monkeypatch)
    saw_peer(agent)
    assert agent._standing_down(), "idle, and someone else is using the keyboard"


def test_the_machine_being_typed_on_keeps_the_keyboard(receiver, tmp_path, monkeypatch):
    agent = make_agent(tmp_path, idle=0.5, monkeypatch=monkeypatch)
    saw_peer(agent)
    assert not agent._standing_down(), "in use here, so this machine wins"


def test_a_lone_agent_never_stands_down(receiver, tmp_path, monkeypatch):
    """The single-machine case must not regress: no peer, no gating, ever."""
    agent = make_agent(tmp_path, idle=99999.0, monkeypatch=monkeypatch)
    assert not agent._peer_present()
    assert not agent._standing_down()


def test_without_activity_data_the_agent_keeps_working(receiver, tmp_path, monkeypatch, caplog):
    """A machine that can never prove it is in use must not yield forever."""
    agent = make_agent(tmp_path, idle=None, monkeypatch=monkeypatch)
    saw_peer(agent)
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        assert not agent._standing_down()
    assert "cannot report input activity" in caplog.text
    assert "--observe" in caplog.text


def test_observe_mode_always_stands_down(receiver, tmp_path, monkeypatch):
    agent = make_agent(tmp_path, idle=0.0, monkeypatch=monkeypatch, observe=True)
    assert agent._standing_down(), "observe-only never writes, peer or not"


def test_observe_mode_writes_nothing_at_all(receiver, tmp_path):
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 1  # not the windows target
    agent = Agent(config(tmp_path, observe=True))
    agent.start()
    try:
        import time

        time.sleep(1.5)
    finally:
        agent.stop()
        agent.shutdown()
    assert keyboard.set_calls == [], "an observing agent must never touch the device"


def test_the_change_of_turn_is_logged_once_each_way(receiver, tmp_path, caplog):
    agent = Agent(config(tmp_path))
    with caplog.at_level("INFO", logger="logiswitch.agent"):
        agent._note_arbitration(True)
        agent._note_arbitration(True)
        agent._note_arbitration(True)
        assert caplog.text.count("standing down") == 1
        agent._note_arbitration(False)
        agent._note_arbitration(False)
        assert caplog.text.count("taking the keyboard back") == 1


def test_a_peer_stops_the_fast_recheck_escalating(receiver, tmp_path):
    """Racing a competitor at 2 Hz makes the tug-of-war faster, not winnable."""
    agent = Agent(config(tmp_path))
    agent._clean_checks = 0
    assert agent._unsettled(), "normally it watches closely after a correction"
    saw_peer(agent)
    assert not agent._unsettled(), "but not while another machine is writing too"


# -- claiming a fixed host -----------------------------------------------------


def test_claim_host_pins_the_slot_that_is_written(monkeypatch):
    """For the topology where every machine has its own receiver."""
    keys = fakehid.mx_keys_s()
    keys.current_host = 0
    keys.platform = 0
    fakehid.install(monkeypatch, fakehid.FakeReceiver([keys]))
    from logiswitch import hidpp

    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        device = hidpp.HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        device.claim_host(2)
        assert device.current_host() == 2, "configuration overrides what the device says"
    finally:
        transport.close()


def test_no_claim_means_ask_the_device(monkeypatch):
    keys = fakehid.mx_keys_s()
    keys.current_host = 1
    fakehid.install(monkeypatch, fakehid.FakeReceiver([keys]))
    from logiswitch import hidpp

    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        device = hidpp.HidppDevice(transport, fakehid.MX_KEYS_INDEX)
        device.claim_host(None)
        assert device.current_host() == 1
    finally:
        transport.close()


@pytest.mark.parametrize("claimed", [0, 1, 2])
def test_the_agent_passes_the_claim_to_the_device(receiver, tmp_path, claimed):
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 1  # macOS, while the agent targets windows, so a write is due
    agent = Agent(config(tmp_path, claim_host=claimed))
    agent.assert_once()
    assert keyboard.set_hosts, "it wrote something"
    assert set(keyboard.set_hosts) == {claimed}, "only ever the claimed slot"


# -- detecting a peer from what it did, not from catching it in the act --------


def test_a_platform_set_by_other_software_is_a_peer_sighting(receiver, tmp_path, caplog):
    """The detection that actually works on a shared receiver.

    Waiting to catch another host's setHostPlatform *reply* left the peer unseen:
    each machine reliably sees the other's reads and only sometimes its writes. But
    "the platform is now something we did not write, set by host software" is
    conclusive, and the device layer already works it out.
    """
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 1  # not the windows target, so the first pass really writes
    agent = Agent(config(tmp_path))
    agent.assert_once()  # we write the windows target, and remember that we did
    assert not agent._peer_present()

    keyboard.platform = 1  # another machine sets macOS, reported as "host software"
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        agent.assert_once()

    assert agent._peer_present(), "the platform moved under us; that is a peer"
    assert "another machine is setting" in caplog.text


def test_our_own_writes_are_never_mistaken_for_a_peer(receiver, tmp_path):
    """The agent changing the platform is not evidence of anybody else."""
    agent = Agent(config(tmp_path))
    for _ in range(4):
        agent.assert_once()
    assert not agent._peer_present()


def test_a_peer_sighting_is_only_counted_once(receiver, tmp_path, caplog):
    keyboard = receiver.devices[fakehid.MX_KEYS_INDEX]
    keyboard.platform = 1
    agent = Agent(config(tmp_path))
    agent.assert_once()
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        for _ in range(5):
            keyboard.platform = 1
            agent.assert_once()
    warnings = [r for r in caplog.records if "another machine is setting" in r.getMessage()]
    assert len(warnings) == 1, "say it once, not once per pass"
