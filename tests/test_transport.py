import threading
import time

import fakehid
import pytest

from logiswitch import hidpp, trace
from logiswitch.hidpp import protocol as p
from logiswitch.hidpp import transport as transport_module


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
            fakehid.MX_KEYS_INDEX,
            p.FEATURE_ROOT,
            p.ROOT_GET_PROTOCOL_VERSION,
            b"\x00\x00\xaa",
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


# -- the stale-reply race -----------------------------------------------------


def _reply_stamped(device_index: int, feature_index: int, function: int, sw_id: int) -> bytes:
    """A well-formed reply carrying a chosen software id."""
    out = bytearray(p.LEN_LONG)
    out[0] = p.REPORT_LONG
    out[1] = device_index
    out[2] = feature_index
    out[3] = p.function_byte(function, sw_id)
    out[4:8] = bytes([0x00, 0x01, 0x01, 0x03])  # host 0, active, platform 1, by software
    return bytes(out)


def test_requests_do_not_all_carry_the_same_software_id(transport):
    sent = []
    handle = transport._handles[0][1]
    real_write = handle.write

    def capture(data):
        sent.append(bytes(data))
        real_write(data)

    handle.write = capture  # type: ignore[method-assign]
    for _ in range(3):
        transport.request(
            fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa"
        )
    ids = [frame[3] & 0x0F for frame in sent]
    assert len(set(ids)) == len(ids), f"software ids repeated within one burst: {ids}"
    assert 0 not in ids, "swId 0 is reserved for notifications"


def test_a_late_reply_is_not_handed_to_the_next_request(receiver, transport):
    """The race that silently leaves the keyboard in the wrong layout.

    A reply that missed its deadline must not be accepted as the answer to the
    identical request that follows, or the agent acts on a stale platform value,
    decides nothing needs changing, and logs success while the layout is wrong.
    """
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    feature, function = fakehid.MULTIPLATFORM_INDEX, p.MP_GET_HOST_PLATFORM

    with pytest.raises(p.HidppTimeout):
        transport.request(fakehid.MX_KEYS_INDEX, feature, function, b"\xff", timeout=0.2)

    # ... and now its answer finally turns up, stamped with the id that request used.
    stale = _reply_stamped(fakehid.MX_KEYS_INDEX, feature, function, p.SW_IDS[0])

    outcome: dict[str, object] = {}

    def second_request():
        try:
            outcome["payload"] = transport.request(
                fakehid.MX_KEYS_INDEX, feature, function, b"\xff", timeout=0.8
            )
        except p.HidppTimeout:
            outcome["timed_out"] = True

    thread = threading.Thread(target=second_request)
    thread.start()
    time.sleep(0.1)  # let the second request install its sink
    transport._dispatch(stale)
    thread.join()

    assert outcome.get("timed_out"), (
        f"the stale reply was accepted as a fresh answer: {outcome.get('payload')!r}"
    )
    assert trace.HEALTH.get("orphans") == 1


def test_an_orphan_reply_is_counted_and_warned_about(transport, caplog):
    caplog.set_level("WARNING", logger="logiswitch.hidpp.transport")
    orphan = _reply_stamped(
        fakehid.MX_KEYS_INDEX, fakehid.MULTIPLATFORM_INDEX, p.MP_GET_HOST_PLATFORM, p.SW_IDS[3]
    )
    transport._dispatch(orphan)
    assert trace.HEALTH.get("orphans") == 1
    assert "nothing waiting for it" in caplog.text
    assert any(record.direction == trace.ORPHAN for record in trace.snapshot())


def test_a_fixed_software_id_can_be_forced_back(monkeypatch, receiver):
    monkeypatch.setenv(transport_module.FIXED_SWID_ENV, "1")
    forced = hidpp.open_transport(hidpp.find_groups()[0])
    sent = []
    try:
        handle = forced._handles[0][1]
        real_write = handle.write

        def capture(data):
            sent.append(bytes(data))
            real_write(data)

        handle.write = capture  # type: ignore[method-assign]
        for _ in range(2):
            forced.request(
                fakehid.MX_KEYS_INDEX, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa"
            )
    finally:
        forced.close()
    assert {frame[3] & 0x0F for frame in sent} == {p.SW_ID}


def test_notifications_are_traced_but_never_counted_as_orphans(transport):
    wake = bytes([0x10, fakehid.MX_KEYS_INDEX, p.NOTIF_DEVICE_CONNECTION, 0x04, 0x00, 0x00, 0x00])
    transport.on_notification = lambda _frame: None
    transport._dispatch(wake)
    assert trace.HEALTH.get("orphans") == 0
    assert trace.HEALTH.get("notifications") == 1


def test_a_straggler_is_rejected_even_when_its_id_comes_round_again(receiver, transport):
    """Belt and braces: rotation makes the straggler distinguishable, this rejects it.

    Rotation alone leaves a residual window -- after enough intervening requests the
    software id repeats, and a very late reply could match again. Remembering the
    request that gave up closes it without relying on the rotation period.
    """
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    feature, function = fakehid.MULTIPLATFORM_INDEX, p.MP_GET_HOST_PLATFORM

    with pytest.raises(p.HidppTimeout):
        transport.request(fakehid.MX_KEYS_INDEX, feature, function, b"\xff", timeout=0.2)

    stale = _reply_stamped(fakehid.MX_KEYS_INDEX, feature, function, p.SW_IDS[0])
    transport._dispatch(stale)  # arrives with no request in flight at all
    assert trace.HEALTH.get("orphans") == 1

    # The same frame a second time is no longer "abandoned" -- it was consumed --
    # but it is still an orphan, never a reply.
    transport._dispatch(stale)
    assert trace.HEALTH.get("orphans") == 2


def test_a_fresh_reply_after_a_timeout_is_still_accepted(receiver, transport):
    """The retry-after-timeout path must not be broken by the abandonment guard.

    A retry carries a different software id, so it has a different key and is
    unaffected by the record of the request that gave up.
    """
    fake = receiver.devices[fakehid.MX_KEYS_INDEX]
    fake.asleep = True
    feature, function = fakehid.MULTIPLATFORM_INDEX, p.MP_GET_HOST_PLATFORM
    with pytest.raises(p.HidppTimeout):
        transport.request(fakehid.MX_KEYS_INDEX, feature, function, b"\xff", timeout=0.2)

    fake.asleep = False
    payload = transport.request(fakehid.MX_KEYS_INDEX, feature, function, b"\xff", timeout=1.0)
    assert payload, "the retry must get its answer"


def test_an_abandoned_record_expires(receiver, transport, monkeypatch):
    monkeypatch.setattr(transport_module, "ABANDONED_MEMORY", 0.0)
    receiver.devices[fakehid.MX_KEYS_INDEX].asleep = True
    with pytest.raises(p.HidppTimeout):
        transport.request(
            fakehid.MX_KEYS_INDEX,
            fakehid.MULTIPLATFORM_INDEX,
            p.MP_GET_HOST_PLATFORM,
            b"\xff",
            timeout=0.2,
        )
    assert transport._abandoned, "recorded"
    assert not transport._is_abandoned(
        _reply_stamped(
            fakehid.MX_KEYS_INDEX,
            fakehid.MULTIPLATFORM_INDEX,
            p.MP_GET_HOST_PLATFORM,
            p.SW_IDS[0],
        )
    ), "an expired record must not reject a frame"


def test_error_frames_are_keyed_correctly():
    """An error frame carries feature/function one byte further along."""
    error = bytes([0x10, 0x05, p.ERROR_HIDPP20, 0x10, p.function_byte(2, 5), 0x08, 0x00])
    assert transport_module.Transport._reply_key(error) == (0x05, 0x10, p.function_byte(2, 5))
    reply = bytes([0x11, 0x05, 0x10, p.function_byte(2, 5)]) + bytes(16)
    assert transport_module.Transport._reply_key(reply) == (0x05, 0x10, p.function_byte(2, 5))
