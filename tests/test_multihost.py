"""Ten machines, one keyboard, one platform slot.

The topology that caused the original bug: several computers behind a KVM all see
the same receiver, and each runs an agent that wants its own OS. They cannot all
win, and there is no channel between them to negotiate over. What keeps it sane is
that a keyboard delivers keystrokes to one machine at a time, so at most one agent
can see recent typing -- and that one takes the keyboard while the rest stand down.

Each fleet's receiver is filled to its six-device limit, so every scenario also
checks the agents drive both keyboards and the DUALPLATFORM device while never
touching the mice.
"""

from __future__ import annotations

import random
import threading
import time

import multihost
import pytest
from multihost import DUALPLATFORM, KEYBOARD_A, KEYBOARD_B, Fleet

from logiswitch import agent as agent_module
from logiswitch.hidpp import protocol as p


@pytest.fixture
def fleet(monkeypatch):
    made = Fleet(monkeypatch)
    yield made
    made.close_all()


@pytest.fixture
def mixed_fleet(monkeypatch):
    """Five macOS machines and five Windows ones, sharing one keyboard."""
    made = Fleet(monkeypatch, targets=["macos", "windows"])
    yield made
    made.close_all()


# -- the fleet itself ----------------------------------------------------------


def test_the_receiver_is_full_and_shared(fleet):
    assert len(fleet.keyboard.devices) == 6, "a receiver holds six devices, no more"
    assert fleet.keyboard.shared, "machines behind a KVM see each other's traffic"
    assert len(fleet.machines) == 10


def test_every_supported_device_is_driven_and_the_mice_are_not(fleet):
    fleet.type_on("m0").apply()
    assert fleet.keyboard.devices[KEYBOARD_A].set_calls
    assert fleet.keyboard.devices[KEYBOARD_B].set_calls
    assert fleet.keyboard.devices[DUALPLATFORM].set_calls
    assert fleet.mice_untouched(), "a mouse has no layout to set"


# -- convergence ---------------------------------------------------------------


def test_only_the_machine_being_typed_on_writes(fleet):
    fleet.tell_everyone_about_the_peer()
    fleet.type_on("m3")
    fleet.apply_all(rounds=3)
    assert fleet.writers() == {"m3"}, f"idle machines wrote too: {fleet.writers()}"


def test_the_layout_follows_whoever_is_typing(mixed_fleet):
    """Five Macs and five PCs; the active machine's OS wins each time."""
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    macs = [m.name for m in fleet.machines if m.target_os == "macos"]
    pcs = [m.name for m in fleet.machines if m.target_os == "windows"]

    fleet.type_on(macs[0])
    fleet.apply_all(rounds=2)
    assert fleet.platform_of() == 1, "macOS"

    fleet.type_on(pcs[0])
    fleet.apply_all(rounds=2)
    assert fleet.platform_of() == 0, "windows/android/linux"


def test_a_lone_machine_still_works_with_nobody_typing(fleet):
    """No peer means no arbitration: a single machine must never stand down."""
    fleet.nobody_typing()
    fleet.by_name["m0"].apply()
    assert fleet.by_name["m0"].wrote, "one machine alone always corrects the layout"


def test_nobody_writes_when_nobody_is_typing_and_a_peer_exists(fleet):
    fleet.tell_everyone_about_the_peer()
    fleet.nobody_typing()
    fleet.apply_all(rounds=2)
    assert fleet.writers() == set(), "an idle fleet leaves the keyboard alone"


# -- fast switching ------------------------------------------------------------


def test_rapid_switching_around_all_ten_machines(mixed_fleet):
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    for _round in range(3):
        for machine in fleet.machines:
            fleet.type_on(machine.name)
            fleet.clear_ledger()
            fleet.apply_all(rounds=2)
            wrote = fleet.writers()
            assert wrote <= {machine.name}, (
                f"while typing on {machine.name}, these also wrote: {wrote - {machine.name}}"
            )
            expected = 1 if machine.target_os == "macos" else 0
            assert fleet.platform_of() == expected, (
                f"layout did not follow {machine.name} ({machine.target_os})"
            )


def test_switching_back_and_forth_does_not_accumulate_writes(mixed_fleet):
    """Each hop should cost one correction, not a burst per machine."""
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    hops = 12
    for i in range(hops):
        fleet.type_on(fleet.machines[i % 2].name)
        fleet.apply_all(rounds=2)
    # Three supported devices, one correction each per genuine change of OS.
    assert len(fleet.ledger) <= hops * 3, f"{len(fleet.ledger)} writes for {hops} hops"


def test_a_machine_returning_after_a_long_idle_takes_the_keyboard_back(mixed_fleet):
    """The handover has to be visible, so the two machines must want different OSes."""
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    first = fleet.machines[0]
    second = next(m for m in fleet.machines if m.target_os != first.target_os)

    fleet.type_on(first.name)
    fleet.apply_all()
    fleet.clear_ledger()

    fleet.type_on(second.name)  # the user walks over to another desk
    fleet.apply_all()
    assert fleet.writers() == {second.name}
    assert fleet.platform_of() == (1 if second.target_os == "macos" else 0)


# -- the herd ------------------------------------------------------------------


def test_ten_machines_starting_at_once_converge(mixed_fleet):
    """Nobody knows about anybody, and they disagree. They must settle, not argue.

    Mixed targets on purpose: in an all-macOS fleet the first machine to run fixes
    the layout for everyone and the herd never happens, so the test would prove
    nothing.
    """
    fleet = mixed_fleet
    typist = fleet.machines[4]
    fleet.type_on(typist.name)
    fleet.apply_all(rounds=1)  # no peer known yet, so each flips it to its own OS
    assert len(fleet.writers()) > 1, "with no peer knowledge they all try, as expected"

    fleet.clear_ledger()
    fleet.apply_all(rounds=3)  # by now they have all seen each other
    assert fleet.writers() <= {typist.name}, (
        f"after meeting each other only the typist should write, got {fleet.writers()}"
    )
    assert fleet.platform_of() == (1 if typist.target_os == "macos" else 0)


def test_the_quiet_spell_after_convergence_does_not_restart_the_fight(fleet):
    """The oscillation a short peer window would cause.

    Once the winner has the platform right it stops writing. If peers were only
    remembered for a couple of minutes, every idle machine would forget and start
    writing again -- a fight on a fixed cycle, forever.
    """
    fleet.tell_everyone_about_the_peer()
    fleet.type_on("m2")
    fleet.apply_all(rounds=2)
    fleet.clear_ledger()

    # A long quiet spell in which the winner has nothing to correct and therefore
    # sends nothing for the others to observe.
    for _ in range(20):
        fleet.apply_all(rounds=1)
    assert fleet.writers() <= {"m2"}, (
        f"idle machines resumed writing during a quiet spell: {fleet.writers()}"
    )


# -- a peer that will not cooperate --------------------------------------------


def test_an_old_build_is_named_and_not_raced(fleet, caplog):
    """Old builds do not take turns. Say so, and stop escalating."""
    victim = fleet.by_name["m0"]
    victim.agent._clean_checks = 0
    assert victim.agent._unsettled(), "normally it watches closely after a correction"

    with caplog.at_level("WARNING", logger="logiswitch.agent"):
        victim.saw_peer(sw_id=p.SW_ID)
    assert "OLD logiswitch" in caplog.text
    assert "update logiswitch on that machine" in caplog.text
    assert not victim.agent._unsettled(), "racing an unyielding peer only speeds it up"


def test_an_unyielding_peer_does_not_stop_the_active_machine_working(fleet):
    fleet.tell_everyone_about_the_peer(sw_id=p.SW_ID)
    fleet.type_on("m5")
    fleet.apply_all(rounds=2)
    assert fleet.writers() == {"m5"}, "the machine in use still corrects the layout"


# -- recovery ------------------------------------------------------------------


def test_the_keyboard_can_sleep_and_come_back(fleet):
    fleet.tell_everyone_about_the_peer()
    active = fleet.type_on("m6")
    for index in multihost.SUPPORTED:
        fleet.keyboard.devices[index].asleep = True
    active.apply()  # nothing answers; must not raise
    fleet.clear_ledger()

    for index in multihost.SUPPORTED:
        fleet.keyboard.devices[index].asleep = False
    fleet.keyboard.devices[KEYBOARD_A].platform = 0  # drifted while it was away
    active.apply()
    assert fleet.platform_of() == 1, "the layout is corrected once it answers again"


def test_the_winner_disappearing_lets_another_machine_take_over(mixed_fleet):
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    winner = fleet.machines[0]
    successor = next(m for m in fleet.machines[1:] if m.target_os != winner.target_os)

    fleet.type_on(winner.name)
    fleet.apply_all()
    winner.close()  # that machine is switched off

    fleet.clear_ledger()
    fleet.type_on(successor.name)  # the user moves to another desk
    for machine in fleet.machines[1:]:
        machine.apply()
    assert fleet.writers() == {successor.name}


def test_a_transport_lost_underneath_is_rebuilt(fleet):
    fleet.type_on("m1")
    machine = fleet.by_name["m1"]
    machine.apply()
    for handle in list(fleet.keyboard.handles):
        handle.close()  # the receiver is yanked out
    fleet.keyboard.devices[KEYBOARD_A].platform = 0
    machine.apply()  # must not raise
    assert machine.apply(), "the session is rebuilt and the pass succeeds"
    assert fleet.platform_of() == 1


def test_a_restarted_agent_has_no_stale_beliefs(fleet, monkeypatch):
    fleet.tell_everyone_about_the_peer()
    fleet.type_on("m3")
    fleet.apply_all()

    fresh = multihost.Machine(fleet, "m3-restarted", "macos")
    assert not fresh.agent._peer_present(), "a new process starts with no peer knowledge"
    fresh.idle = Fleet.IDLE
    assert not fresh.agent._standing_down(), "and therefore does not stand down"


# -- stress --------------------------------------------------------------------


@pytest.mark.slow
def test_sustained_random_switching_with_a_flapping_device(fleet):
    """Randomised hops plus a device that keeps dropping the setting.

    Deterministic seed: a stress test that fails differently every run is not a test.
    """
    rng = random.Random(20260804)
    fleet.tell_everyone_about_the_peer()
    keyboard = fleet.keyboard.devices[KEYBOARD_A]

    # Every step is 10 agents x 2 passes over 3 devices, and a shared receiver puts
    # every frame in front of every machine, so the cost grows with the square of the
    # fleet. Sized to stay well inside the suite's 120 s per-test ceiling: a stress
    # test that times out on a busy CI runner teaches nobody anything.
    for step in range(50):
        machine = fleet.machines[rng.randrange(len(fleet.machines))]
        fleet.type_on(machine.name)
        if rng.random() < 0.3:
            keyboard.platform = rng.choice([0, 1, 2])  # firmware drops the setting
        if rng.random() < 0.1:
            keyboard.asleep = True
        fleet.clear_ledger()
        for _ in range(2):
            for candidate in fleet.machines:
                candidate.apply()
        keyboard.asleep = False

        strays = fleet.writers() - {machine.name}
        assert not strays, f"step {step}: idle machines wrote: {strays}"

    fleet.type_on("m0")
    fleet.apply_all(rounds=2)
    assert fleet.platform_of() == 1
    assert fleet.mice_untouched()


@pytest.mark.slow
def test_ten_real_agents_running_concurrently(monkeypatch, mixed_fleet):
    """The genuinely concurrent case: ten event loops, real threads, real handovers.

    The deterministic tests pin the logic; this one covers what only concurrency
    produces -- races between reader threads, and thread leaks (the autouse
    no_leaked_threads fixture asserts the latter for us).
    """
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    errors: list[BaseException] = []

    def watch_for_errors(args):
        errors.append(args.exc_value)

    monkeypatch.setattr(threading, "excepthook", watch_for_errors)

    fleet.type_on("m0")
    fleet.start_all()
    try:
        for machine in fleet.machines[:6]:
            fleet.type_on(machine.name)
            time.sleep(0.6)
    finally:
        fleet.stop_all()

    assert not errors, f"agent threads raised: {errors}"
    assert fleet.mice_untouched()
    assert fleet.platform_of() in (0, 1), "the keyboard is left in a sane state"


@pytest.mark.slow
def test_the_fleet_leaves_the_keyboard_on_the_last_typists_layout(mixed_fleet):
    fleet = mixed_fleet
    fleet.tell_everyone_about_the_peer()
    last = fleet.machines[-1]
    for machine in fleet.machines:
        fleet.type_on(machine.name)
        fleet.apply_all(rounds=2)
    expected = 1 if last.target_os == "macos" else 0
    assert fleet.platform_of() == expected
    assert fleet.platform_of(KEYBOARD_B) == expected
    assert agent_module.PEER_MEMORY > agent_module.ACTIVE_WINDOW
