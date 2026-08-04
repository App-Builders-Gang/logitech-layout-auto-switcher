import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fakehid  # noqa: E402

#: Threads this project starts. Any of these still alive when a test ends is a leak.
OWNED_THREAD_PREFIXES = ("hidpp-reader", "logiswitch-")


def _owned_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(OWNED_THREAD_PREFIXES)
    ]


@pytest.fixture(autouse=True)
def no_leaked_threads():
    """Fail any test that leaves one of our threads running.

    The agent owns reader threads, a worker and sometimes a watcher thread. A test
    that forgets to close a transport or shut an agent down would otherwise pass
    while leaking a thread into the rest of the session, where it shows up later as
    an unrelated flake. Checking here attributes the leak to the test that caused it.

    Threads are given a moment to wind down first: shutdown is bounded by the
    reader's read timeout, not instantaneous.
    """
    assert not _owned_threads(), "a previous test leaked threads"
    yield
    deadline = time.time() + 3.0
    while _owned_threads() and time.time() < deadline:
        time.sleep(0.02)
    leaked = [thread.name for thread in _owned_threads()]
    assert not leaked, f"test leaked threads: {leaked}"


@pytest.fixture(autouse=True)
def isolated_logging():
    """Restore the package logger after every test.

    ``setup_logging`` clears handlers and sets ``propagate = False`` so the agent
    does not double-log through the root logger. Any test that runs the CLI would
    otherwise leave that in place, and a later test using ``caplog`` -- which
    listens on the root logger -- would silently record nothing. Snapshot and put
    it back so test order cannot matter.
    """
    import logging

    logger = logging.getLogger("logiswitch")
    handlers = logger.handlers[:]
    level, propagate = logger.level, logger.propagate
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


@pytest.fixture(autouse=True)
def clean_trace():
    """Reset the frame ring and health counters around every test.

    Both are process-wide singletons, so without this a test asserting on a counter
    would be reading whatever the tests before it happened to do.
    """
    from logiswitch import trace

    trace.clear()
    trace.HEALTH.reset()
    trace.set_dump_path(None)
    trace.set_echo(False)
    yield
    trace.clear()
    trace.HEALTH.reset()
    trace.set_dump_path(None)
    trace.set_echo(False)


@pytest.fixture(autouse=True)
def machine_in_use(monkeypatch):
    """Pretend somebody is at this computer, for every test that does not say otherwise.

    The agent gives the keyboard up when it has been idle *and* another machine is
    competing for it. An unattended test run is idle by definition, so without this
    the suite's behaviour would depend on whether a human happened to be typing
    while it ran -- which is exactly the kind of test that passes on a laptop and
    fails in CI. Tests about taking turns override it per agent.
    """
    from logiswitch import activity

    monkeypatch.setattr(activity, "seconds_since_input", lambda: 0.0)


@pytest.fixture
def receiver(monkeypatch):
    """A Bolt receiver with an MX Master 3S and an MX Keys S, as on real hardware."""
    fake = fakehid.install(
        monkeypatch, fakehid.FakeReceiver([fakehid.mx_master_3s(), fakehid.mx_keys_s()])
    )
    yield fake
    assert fake.handles == [], "the test left a HID handle open"


@pytest.fixture
def transport(receiver):
    from logiswitch import hidpp

    groups = hidpp.find_groups()
    assert len(groups) == 1
    transport = hidpp.open_transport(groups[0])
    yield transport
    transport.close()
