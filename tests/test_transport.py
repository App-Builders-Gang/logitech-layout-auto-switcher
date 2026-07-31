import threading
import time

import fakehid
import pytest

from logiswitch import hidpp
from logiswitch.hidpp import protocol as p


def test_request_returns_the_payload(transport):
    reply = transport.request(
        fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa"
    )
    assert reply[:3] == bytes([4, 5, 0xAA])


def test_request_raises_the_reported_error(transport):
    with pytest.raises(p.HidppError) as excinfo:
        transport.request(fakehid.MX_KEYS_INDEX, 0x7E, 0)
    assert excinfo.value.code == 6  # invalid feature index


def test_request_times_out_when_the_device_is_asleep(receiver, transport):
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    with pytest.raises(p.HidppTimeout):
        transport.request(
            fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa",
            timeout=0.2,
        )


def test_scan_finds_every_populated_slot_in_one_window(transport):
    found = transport.scan(hidpp.discovery.SCAN_INDICES)
    assert set(found) == {fakehid.MX_MASTER_INDEX, fakehid.MX_KEYS_INDEX}
    assert found[fakehid.MX_KEYS_INDEX] == (4, 5)


def test_scan_ignores_slots_that_answer_with_unknown_device(transport):
    found = transport.scan((3, 4))
    assert found == {}


def test_notifications_reach_the_callback_and_not_the_request(transport):
    seen = []
    transport.on_notification = seen.append
    frame = bytes([0x10, fakehid.MX_KEYS_INDEX, p.NOTIF_DEVICE_CONNECTION, 0x04, 0x00, 0x00, 0x00])
    transport._dispatch(frame)
    assert seen == [frame]


def test_notification_during_a_request_is_not_swallowed(transport):
    """v1 lost wake events because a pre-request drain ate them."""
    seen = []
    transport.on_notification = seen.append
    wake = bytes([0x10, fakehid.MX_KEYS_INDEX, p.NOTIF_DEVICE_CONNECTION, 0x04, 0, 0, 0])

    def inject():
        time.sleep(0.02)
        transport._dispatch(wake)

    thread = threading.Thread(target=inject)
    thread.start()
    transport.request(
        fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa"
    )
    thread.join()
    assert seen == [wake]


def test_close_is_idempotent_and_releases_handles(receiver):
    groups = hidpp.find_groups()
    transport = hidpp.open_transport(groups[0])
    assert receiver.handles
    transport.close()
    transport.close()
    assert receiver.handles == []
    assert not transport.alive


def test_partial_open_failure_does_not_leak(monkeypatch, receiver):
    from logiswitch.hidpp import backend

    real_open = backend.open_path
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("second collection refuses to open")
        return real_open(path)

    monkeypatch.setattr(backend, "open_path", flaky)
    group = hidpp.find_groups()[0]
    with pytest.raises(OSError):
        hidpp.open_transport(group)
    assert receiver.handles == [], "the first handle must be closed when the second fails"


def test_request_on_a_closed_transport_raises(transport):
    transport.close()
    with pytest.raises(p.TransportClosed):
        transport.request(fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION)
