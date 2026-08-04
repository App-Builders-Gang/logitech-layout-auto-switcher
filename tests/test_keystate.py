"""Reading the host's modifier state, and the guard that rests on it.

Why this exists: changing the platform remaps the bottom row, so the key left of
the space bar stops being Command and becomes Alt. Do that between a key's press
and its release and the host never sees the release for the modifier it registered
-- the modifier sticks down and the user is left holding an invisible Command key.

So the contract is: read-only, permission-free, and never raising, because a safety
check that can fail is worse than no safety check.
"""

from __future__ import annotations

import fakehid
import pytest

from logiswitch import agent as agent_module
from logiswitch import keystate, trace
from logiswitch.agent import Agent, AgentConfig


def config(tmp_path, **kwargs):
    defaults = dict(target_os="windows", debounce=0.0, reassert_interval=0.0, notify=False)
    defaults.update(kwargs)
    return AgentConfig(state_file=tmp_path / "state.json", **defaults)


# -- reading the state --------------------------------------------------------


def test_reading_modifiers_never_raises(monkeypatch):
    def explode():
        raise OSError("no window server")

    monkeypatch.setattr(keystate, "is_macos", lambda: True)
    monkeypatch.setattr(keystate, "_macos_modifiers", explode)
    assert keystate.modifiers_held() == set()


def test_an_unsupported_platform_reports_nothing_held(monkeypatch):
    monkeypatch.setattr(keystate, "is_macos", lambda: False)
    monkeypatch.setattr(keystate, "is_windows", lambda: False)
    assert keystate.modifiers_held() == set()
    assert keystate.available() is False


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (0x00000000, set()),
        (0x00100000, {"command"}),
        (0x00020000, {"shift"}),
        (0x00120000, {"command", "shift"}),
        (0x00000100, set()),  # a device-dependent bit, not a modifier we track
    ],
)
def test_cg_flags_decode(flags, expected):
    decoded = {name for name, mask in keystate._CG_FLAGS.items() if flags & mask}
    assert decoded == expected


def test_describe_is_readable():
    assert keystate.describe(set()) == "none"
    assert keystate.describe({"command"}) == "command"
    assert keystate.describe({"shift", "command"}) == "command+shift"


# -- the guard ----------------------------------------------------------------


def test_a_platform_write_waits_while_a_key_is_held(monkeypatch, receiver, tmp_path):
    """The fix for the stuck Command key: do not remap mid-chord."""
    monkeypatch.setattr(keystate, "modifiers_held", lambda: {"command"})
    agent = Agent(config(tmp_path))
    assert agent._hold_off() == {"command"}, "a held modifier defers the correction"


def test_the_write_proceeds_once_the_keys_come_up(monkeypatch, receiver, tmp_path):
    held = {"command"}
    monkeypatch.setattr(keystate, "modifiers_held", lambda: set(held))
    agent = Agent(config(tmp_path))
    assert agent._hold_off() is not None
    held.clear()
    assert agent._hold_off() is None, "nothing held means go ahead"


def test_a_genuinely_stuck_modifier_does_not_block_forever(monkeypatch, receiver, tmp_path, caplog):
    """A key held for ages is stuck, not in use -- deferring for it helps nobody."""
    monkeypatch.setattr(keystate, "modifiers_held", lambda: {"command"})
    monkeypatch.setattr(agent_module, "MAX_DEFER", 0.0)
    agent = Agent(config(tmp_path))
    agent._hold_off()  # starts the clock
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        assert agent._hold_off() is None, "it must give up waiting and correct anyway"
    assert "stuck modifier" in caplog.text
    assert trace.HEALTH.get("stuck_modifiers") == 1


def test_a_stuck_modifier_is_notified(monkeypatch, receiver, tmp_path):
    from logiswitch import notify

    sent = []
    monkeypatch.setattr(keystate, "modifiers_held", lambda: {"command"})
    monkeypatch.setattr(agent_module, "MAX_DEFER", 0.0)
    agent = Agent(config(tmp_path, notify=True))
    agent.notifier = notify.Notifier(sender=sent.append)
    agent._hold_off()
    agent._hold_off()
    agent.notifier._drain()
    assert [n for n in sent if n.kind == notify.STUCK_MODIFIER]
    assert "Tap it once" in sent[-1].body, "tell the user how to clear it"


def test_a_switch_that_lands_on_a_held_key_is_recorded(monkeypatch, receiver, tmp_path, caplog):
    """If the guard is ever bypassed, the log must say so -- that is the evidence."""
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
    # Nothing held when the guard runs, but held by the time the write lands.
    monkeypatch.setattr(keystate, "modifiers_held", lambda: {"command"})
    agent = Agent(config(tmp_path))
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        agent.assert_once()
    assert "while command was held" in caplog.text
    assert trace.HEALTH.get("switched_while_held") == 1


def test_nothing_is_recorded_when_no_key_was_held(monkeypatch, receiver, tmp_path, caplog):
    receiver.devices[fakehid.MX_KEYS_INDEX].platform = 1
    monkeypatch.setattr(keystate, "modifiers_held", lambda: set())
    agent = Agent(config(tmp_path))
    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        agent.assert_once()
    assert "was held" not in caplog.text
    assert trace.HEALTH.get("switched_while_held") == 0


def test_fn_is_not_treated_as_a_strandable_modifier():
    """Fn is not on the row a platform switch remaps, and macOS over-reports it.

    Including it made the agent believe Fn had been held for thirty seconds and
    defer real corrections, on a keyboard nobody was touching.
    """
    assert "fn" not in keystate._CG_FLAGS
    assert {"command", "option", "control", "shift"} == set(keystate._CG_FLAGS)
    # The bit macOS uses for Fn must not register as anything held.
    decoded = {name for name, mask in keystate._CG_FLAGS.items() if 0x00800000 & mask}
    assert decoded == set()
