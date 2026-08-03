"""A fleet of machines sharing one keyboard, for testing contention.

Models the topology that caused the original bug: several computers behind a KVM,
all seeing the same receiver, each running its own agent, each wanting its own OS.
From the keyboard's side they are one host with one platform slot, so they genuinely
compete -- and the only thing stopping a permanent tug-of-war is that the machine
being typed on wins and the rest stand down.

Three limits matter and only one of them is six:

* machines behind a shared receiver -- unbounded, they share a single host slot
* devices paired to one receiver -- six (``RECEIVER_SLOTS``)
* Easy-Switch host slots on the keyboard -- three

So a fleet is ten machines, and its receiver is filled to all six device slots, which
means every scenario also proves the agents drive every supported device and leave the
mice alone while they are busy arguing.
"""

from __future__ import annotations

import threading
import time

import fakehid

from logiswitch.agent import Agent, AgentConfig

#: Ten machines: more than enough to shake out ordering, and small enough that ten
#: real agent loops still finish a test inside the suite's timeout.
FLEET_SIZE = 10

#: A receiver filled to its six-slot limit. Two keyboards and a DUALPLATFORM device
#: must all be driven; the three mice must never be written to.
KEYBOARD_A = 1
KEYBOARD_B = 2
DUALPLATFORM = 3
MICE = (4, 5, 6)
SUPPORTED = (KEYBOARD_A, KEYBOARD_B, DUALPLATFORM)


def full_receiver(shared: bool = True) -> fakehid.FakeReceiver:
    """One receiver, all six slots populated, shared between machines."""
    devices = [
        fakehid.mx_keys_s(index=KEYBOARD_A),
        fakehid.mx_keys_s(index=KEYBOARD_B),
        fakehid.craft_dualplatform(index=DUALPLATFORM),
        *(fakehid.mx_master_3s(index=i) for i in MICE),
    ]
    return fakehid.FakeReceiver(devices, shared=shared)


class Machine:
    """One computer in the fleet: an agent, a name, and a controllable idle time."""

    def __init__(self, fleet: Fleet, name: str, target_os: str, **config):
        self.fleet = fleet
        self.name = name
        self.target_os = target_os
        self.idle = 0.0
        settings = dict(
            target_os=target_os,
            debounce=0.0,
            reassert_interval=0.0,
            notify=False,
            force_polling=True,
            state_file=None,
        )
        settings.update(config)
        self.agent = Agent(AgentConfig(**settings))
        # Per-instance, because the module function answers for the whole process and
        # ten agents in one interpreter would otherwise share one idle time.
        self.agent._idle_seconds = lambda: self.idle  # type: ignore[method-assign]

    # -- lifecycle ------------------------------------------------------------

    def apply(self) -> bool:
        """One check-and-correct pass, attributed to this machine.

        Deliberately ``_apply`` rather than ``assert_once``: the latter is the
        ``--once`` path and tears the session down afterwards. Keeping sessions open
        is both what a running agent really does and what makes this harness
        faithful -- with every machine's handle open at the same time, the shared
        receiver puts each machine's traffic in front of all the others, so they
        discover each other exactly as they would through a KVM.
        """
        with self.fleet.lock:
            fakehid.current_owner[0] = self.name
            try:
                return self.agent._apply()
            finally:
                fakehid.current_owner[0] = "?"

    def close(self) -> None:
        """Drop this machine's session without stopping a (possibly unstarted) agent."""
        self.agent._teardown_sessions("test finished")

    def start(self) -> None:
        with self.fleet.lock:
            fakehid.current_owner[0] = self.name
            try:
                self.agent.start()
            finally:
                fakehid.current_owner[0] = "?"

    def stop(self) -> None:
        self.agent.stop()
        self.agent.shutdown()

    # -- what it did ----------------------------------------------------------

    @property
    def writes(self) -> list[tuple[str, int, int]]:
        return [row for row in self.fleet.keyboard.ledger if row[0] == self.name]

    @property
    def wrote(self) -> bool:
        return bool(self.writes)

    def saw_peer(self, sw_id: int = 0x0E, device_index: int = KEYBOARD_A) -> None:
        """Inject another machine's platform write, as the shared receiver would."""
        from logiswitch.hidpp import protocol as p

        feature = fakehid.MULTIPLATFORM_INDEX
        self.agent._platform_features = {device_index: feature}
        self.agent._on_hidpp_frame(
            bytes([0x11, device_index, feature, p.function_byte(p.MP_SET_HOST_PLATFORM, sw_id)])
            + bytes(16)
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Machine {self.name} {self.target_os} idle={self.idle:.0f}s>"


class Fleet:
    """N machines sharing one keyboard through one receiver."""

    #: Comfortably past `AgentConfig.active_window`, so a machine set to this is
    #: unambiguously not the one being used.
    IDLE = 600.0

    def __init__(self, monkeypatch, size: int = FLEET_SIZE, targets=None, **config):
        self.keyboard = full_receiver(shared=True)
        fakehid.install(monkeypatch, self.keyboard)
        # Ten agents means ten reader threads to join at teardown, and the real
        # half-second read timeout would dominate the test. The fake inbox is a
        # queue, so a short timeout costs nothing but wakeups.
        from logiswitch.hidpp import transport as transport_module

        monkeypatch.setattr(transport_module, "READ_TIMEOUT_MS", 20)
        self.lock = threading.Lock()
        targets = targets or ["macos"] * size
        self.machines = [
            Machine(self, f"m{i}", targets[i % len(targets)], **config) for i in range(size)
        ]
        self.by_name = {m.name: m for m in self.machines}
        # Nobody is typing anywhere until a test says so.
        for machine in self.machines:
            machine.idle = self.IDLE

    # -- driving --------------------------------------------------------------

    def type_on(self, name: str) -> Machine:
        """Exactly one machine is in use; every other has been idle for ages."""
        for machine in self.machines:
            machine.idle = 0.0 if machine.name == name else self.IDLE
        return self.by_name[name]

    def nobody_typing(self) -> None:
        for machine in self.machines:
            machine.idle = self.IDLE

    def apply_all(self, rounds: int = 1) -> None:
        """Give every machine `rounds` chances to act, in order."""
        for _ in range(rounds):
            for machine in self.machines:
                machine.apply()

    def tell_everyone_about_the_peer(self, sw_id: int = 0x0E) -> None:
        """Seed peer knowledge without waiting for a collision to produce it."""
        for machine in self.machines:
            machine.saw_peer(sw_id)

    # -- inspecting -----------------------------------------------------------

    @property
    def ledger(self) -> list[tuple[str, int, int]]:
        return list(self.keyboard.ledger)

    def writers(self) -> set[str]:
        return {row[0] for row in self.keyboard.ledger}

    def clear_ledger(self) -> None:
        self.keyboard.ledger.clear()

    def platform_of(self, index: int = KEYBOARD_A) -> int | None:
        return self.keyboard.devices[index].platform

    def mice_untouched(self) -> bool:
        return all(not self.keyboard.devices[i].set_calls for i in MICE)

    def start_all(self) -> None:
        for machine in self.machines:
            machine.start()

    def stop_all(self) -> None:
        for machine in self.machines:
            machine.stop()

    def close_all(self) -> None:
        """Release every session. The autouse leak detector checks we really did."""
        for machine in self.machines:
            machine.close()

    def settle(self, seconds: float = 1.0) -> None:
        time.sleep(seconds)
