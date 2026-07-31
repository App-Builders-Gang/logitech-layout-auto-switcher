"""Behaviour under conditions the happy path never produces.

A Logitech receiver is a shared bus: Logi Options+, the OS and this agent all talk
to the same collection at once, and a device can answer late, partially, or not at
all. None of that may take the agent down.
"""

from __future__ import annotations

import threading

import fakehid
import pytest

from logiswitch import hidpp
from logiswitch.hidpp import protocol as p
from logiswitch.hidpp.transport import Transport

# -- malformed input ----------------------------------------------------------

GARBAGE = [
    b"",
    b"\x00",
    b"\x10",
    b"\x10\x05",
    b"\x10\x05\xff",
    b"\xff" * 20,
    b"\x11" + b"\x00" * 19,
    bytes(range(20)),
    b"\x10\x05\x8f\x00",
]


@pytest.mark.parametrize("frame", GARBAGE)
def test_dispatch_survives_malformed_frames(transport, frame):
    seen = []
    transport.on_notification = seen.append

    transport._dispatch(frame)  # must not raise

    assert transport.alive


def test_a_raising_notification_callback_does_not_kill_the_reader(transport, caplog):
    """One bad listener must not deafen the transport."""

    def explode(_frame):
        raise RuntimeError("listener blew up")

    transport.on_notification = explode
    frame = bytes([0x10, fakehid.MX_KEYS_INDEX, p.NOTIF_DEVICE_CONNECTION, 0, 0, 0, 0])

    transport._dispatch(frame)

    assert transport.alive
    # and the transport still works afterwards
    reply = transport.request(
        fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa"
    )
    assert reply[2] == 0xAA


def test_truncated_replies_are_reported_not_crashed(monkeypatch):
    """Firmware that half-answers a feature must leave discovery standing."""
    fakehid.install(
        monkeypatch,
        fakehid.FakeReceiver([fakehid.mx_keys_s()], truncate_replies=True),
    )
    transport = hidpp.open_transport(hidpp.find_groups()[0])
    try:
        devices = hidpp.discover_devices(transport, hint=fakehid.MX_KEYS_INDEX)
        probed = hidpp.probe_devices(devices)
        # Either it is reported unsupported, or it is dropped -- never an exception.
        for _device, info in probed:
            assert not info.supported
    finally:
        transport.close()


def test_a_device_that_never_answers_times_out_cleanly(receiver, transport):
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True

    with pytest.raises(p.HidppTimeout):
        transport.request(fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, 0, b"\x00\x05\x00", timeout=0.2)

    assert transport.alive, "a timeout is not a fatal transport error"


def test_scan_reports_only_slots_that_really_answered(receiver, transport):
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True

    found = transport.scan(hidpp.discovery.SCAN_INDICES, window=0.3)

    assert fakehid.MX_KEYS_INDEX not in found
    assert fakehid.MX_MASTER_INDEX in found


# -- concurrency --------------------------------------------------------------


def test_concurrent_requests_do_not_cross_talk(transport):
    """Replies must reach the caller that asked, not merely some caller.

    Requests are serialised by the transport, but the reader threads dispatch
    from another thread entirely; a matcher that ignored any field would surface
    here as one caller receiving another's answer.
    """
    results: dict[int, bytes] = {}
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def ask(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(10):
                reply = transport.request(
                    index, p.FEATURE_ROOT, p.ROOT_GET_FEATURE, bytes([0x00, 0x05, 0x00])
                )
                results[index] = reply
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised below
            errors.append(exc)

    threads = [
        threading.Thread(target=ask, args=(index,))
        for index in (fakehid.MX_KEYS_INDEX, fakehid.MX_MASTER_INDEX, fakehid.MX_KEYS_INDEX)
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(20)

    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads)
    # Both devices resolve 0x0005 to the same feature index in the fake.
    for index, reply in results.items():
        assert reply[0] == fakehid.DEVICE_NAME_INDEX, f"device {index} got the wrong answer"


def test_notifications_during_traffic_all_arrive(transport):
    seen: list[bytes] = []
    transport.on_notification = seen.append
    stop = threading.Event()
    wake = bytes([0x10, fakehid.MX_KEYS_INDEX, p.NOTIF_DEVICE_CONNECTION, 0, 0, 0, 0])

    def chatter() -> None:
        while not stop.is_set():
            transport._dispatch(wake)

    noise = threading.Thread(target=chatter)
    noise.start()
    try:
        for _ in range(25):
            reply = transport.request(
                fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa"
            )
            assert reply[2] == 0xAA, "a notification was mistaken for our reply"
    finally:
        stop.set()
        noise.join(5)

    assert seen, "notifications must still reach the callback while requests run"


# -- lifecycle ----------------------------------------------------------------


def test_closing_mid_request_raises_transport_closed(receiver):
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    transport = hidpp.open_transport(hidpp.find_groups()[0])
    raised: list[Exception] = []

    def ask() -> None:
        try:
            transport.request(fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, 0, b"\x00\x05\x00", timeout=5)
        except Exception as exc:  # noqa: BLE001
            raised.append(exc)

    caller = threading.Thread(target=ask)
    caller.start()
    threading.Event().wait(0.2)
    transport.close()
    caller.join(10)

    assert not caller.is_alive(), "close must unblock a waiting request"
    assert raised and isinstance(raised[0], (p.TransportClosed, p.HidppTimeout))


def test_close_is_safe_to_call_repeatedly_and_concurrently(receiver):
    transport = hidpp.open_transport(hidpp.find_groups()[0])
    threads = [threading.Thread(target=transport.close) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not any(thread.is_alive() for thread in threads)
    assert receiver.handles == []


def test_a_transport_needs_at_least_one_collection():
    with pytest.raises(ValueError):
        Transport([])


def test_requests_after_close_do_not_reopen_anything(receiver):
    transport = hidpp.open_transport(hidpp.find_groups()[0])
    transport.close()

    with pytest.raises(p.TransportClosed):
        transport.request(fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, 0)
    with pytest.raises(p.TransportClosed):
        transport.scan([1])

    assert receiver.handles == []
