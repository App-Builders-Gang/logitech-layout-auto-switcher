import pytest

from logiswitch.hidpp import protocol as p


def test_short_frame_layout():
    frame = p.build_frame(0x05, 0x10, 3, bytes([0xFF, 0x01]))
    assert len(frame) == p.LEN_SHORT
    assert frame[:6] == bytes([0x10, 0x05, 0x10, 0x30 | p.SW_ID, 0xFF, 0x01])


def test_frame_promotes_to_long_when_params_do_not_fit():
    frame = p.build_frame(1, 2, 0, bytes(4))
    assert frame[0] == p.REPORT_LONG
    assert len(frame) == p.LEN_LONG


def test_frame_rejects_oversized_payload():
    with pytest.raises(ValueError):
        p.build_frame(1, 2, 0, bytes(17), long_report=True)


def test_function_byte_packs_function_and_software_id():
    assert p.function_byte(3) == 0x3E
    assert p.function_byte(1, sw_id=1) == 0x11


def test_hidpp20_error_is_recognised_only_for_its_own_request():
    func = p.function_byte(2)
    frame = bytes([0x10, 0x05, p.ERROR_HIDPP20, 0x10, func, 0x09, 0x00])
    assert p.is_error_for(frame, 0x05, 0x10, func) == 20
    assert p.is_error_for(frame, 0x05, 0x11, func) is None
    assert p.is_error_for(frame, 0x01, 0x10, func) is None


def test_hidpp10_error_is_recognised():
    func = p.function_byte(1)
    frame = bytes([0x10, 0x02, p.ERROR_HIDPP10, 0x00, func, 0x08, 0x00])
    assert p.is_error_for(frame, 0x02, 0x00, func) == 10


def test_error_messages_name_the_condition():
    assert "busy" in str(p.HidppError(8, 20))
    assert "unknown device" in str(p.HidppError(8, 10))


def test_response_matching_requires_device_feature_and_function():
    func = p.function_byte(2)
    frame = bytes([0x11, 0x05, 0x10, func]) + bytes(16)
    assert p.is_response_to(frame, 0x05, 0x10, func)
    assert not p.is_response_to(frame, 0x04, 0x10, func)
    assert not p.is_response_to(frame, 0x05, 0x10, p.function_byte(3))


def test_os_aliases_normalise():
    assert p.normalise_os("Mac") == "macos"
    assert p.normalise_os("WIN") == "windows"
    assert p.normalise_os("darwin") == "macos"
    with pytest.raises(ValueError):
        p.normalise_os("beos")


def test_mask_decoding_matches_the_mx_keys_s_table():
    assert p.os_names_for_mask(0x1500) == ["android", "linux", "windows"]
    assert p.os_names_for_mask(0x2000) == ["macos"]


def test_unsolicited_frames_are_told_apart_from_replies():
    # Captured from an MX Keys S the moment it came back on Easy-Switch channel 1:
    # feature index 0x0E (0x4220 lock-key state), function 0, swId 0.
    assert p.is_unsolicited(bytes([0x11, 0x05, 0x0E, 0x00]) + bytes(16))
    # A reply to one of our requests echoes our software id.
    assert not p.is_unsolicited(bytes([0x11, 0x05, 0x10, p.function_byte(2)]) + bytes(16))
    # Errors are replies too, and 0x8F/0xFF would otherwise look like swId 0.
    assert not p.is_unsolicited(bytes([0x10, 0x05, p.ERROR_HIDPP20, 0x10, 0x20, 0x09, 0x00]))
    assert not p.is_unsolicited(bytes([0x10, 0x05, p.ERROR_HIDPP10, 0x80, 0x00, 0x09, 0x00]))


# -- software ids --------------------------------------------------------------


def test_software_ids_never_include_the_notification_value():
    """swId 0 marks an unsolicited event, so a request may never use it."""
    assert 0 not in p.SW_IDS
    assert len(p.SW_IDS) == len(set(p.SW_IDS)) == 14
    assert all(0 < sw_id <= 0x0F for sw_id in p.SW_IDS)


def test_solaars_software_id_is_left_alone():
    """Solaar pins every request to 0x0B so it can filter its own traffic.

    Rotating through it would make our replies look like Solaar's to Solaar, and
    there is no reason to impersonate another client on a shared device.
    """
    assert p.SOLAAR_SW_ID == 0x0B
    assert p.SOLAAR_SW_ID not in p.SW_IDS


def test_the_function_byte_carries_the_software_id():
    for sw_id in p.SW_IDS:
        byte = p.function_byte(p.MP_GET_HOST_PLATFORM, sw_id)
        assert byte >> 4 == p.MP_GET_HOST_PLATFORM
        assert byte & 0x0F == sw_id


def test_replies_with_different_software_ids_do_not_match_each_other():
    """The property the stale-reply fix rests on."""
    frame = bytes([0x11, 0x05, 0x10, p.function_byte(2, 3)]) + bytes(16)
    assert p.is_response_to(frame, 0x05, 0x10, p.function_byte(2, 3))
    assert not p.is_response_to(frame, 0x05, 0x10, p.function_byte(2, 4))


# -- connection notifications --------------------------------------------------


def test_a_connection_notification_is_recognised():
    frame = bytes([0x10, 0x05, p.NOTIF_DEVICE_CONNECTION, 0x04, 0x00, 0x00, 0x00])
    assert p.is_connection_notification(frame)
    assert p.connection_flags(frame) == (True, False)


def test_a_reply_from_feature_index_0x41_is_not_a_connection_notification():
    """Byte 2 is a sub-id on a 1.0 notification but a feature index on a 2.0 reply."""
    reply = bytes([0x11, 0x05, 0x41, p.function_byte(2)]) + bytes(16)
    assert not p.is_connection_notification(reply)


def test_a_notification_for_a_non_slot_index_is_rejected():
    assert not p.is_connection_notification(
        bytes([0x10, 0xFF, p.NOTIF_DEVICE_CONNECTION, 0x04, 0x00, 0x00, 0x00])
    )


def test_link_flags_are_decoded_including_the_inverted_bit():
    down = bytes([0x10, 0x05, p.NOTIF_DEVICE_CONNECTION, 0x04, p.NOTIF_LINK_NOT_ESTABLISHED, 0, 0])
    assert p.connection_flags(down) == (False, False)
    encrypted = bytes([0x10, 0x05, p.NOTIF_DEVICE_CONNECTION, 0x04, p.NOTIF_LINK_ENCRYPTED, 0, 0])
    assert p.connection_flags(encrypted) == (True, True)


# -- host platform records -----------------------------------------------------


def test_a_host_platform_record_is_decoded():
    record = p.decode_host_platform(bytes([0x00, 0x01, 0x01, 0x03]))
    assert record["host_index"] == 0
    assert record["status_name"] == "paired"
    assert record["platform_index"] == 1
    assert record["source_name"] == "host software"
    assert record["raw"] == "00010103"
    assert "set-by=host software" in p.describe_host_platform(record)


def test_a_short_host_platform_record_reads_as_unknown_rather_than_raising():
    """Firmware that half-implements the feature answers with the header alone."""
    record = p.decode_host_platform(b"")
    assert record["platform_index"] is None
    assert record["source_name"] == "?"
    p.describe_host_platform(record)  # must not raise


def test_a_manual_switch_is_distinguishable_from_a_software_one():
    by_hand = p.decode_host_platform(bytes([0x00, 0x01, 0x00, 0x01]))
    assert by_hand["source_name"] == "keyboard"


# -- frame descriptions --------------------------------------------------------


def test_frames_describe_themselves_for_the_trace():
    request = p.build_frame(5, 0x10, p.MP_GET_HOST_PLATFORM, b"\xff", sw_id=3)
    described = p.describe_frame(request)
    assert "dev5" in described
    assert "feat0x10.fn2" in described
    assert "sw0x3" in described


def test_an_error_frame_names_its_error():
    frame = bytes([0x10, 0x05, p.ERROR_HIDPP20, 0x10, p.function_byte(2), 0x08, 0x00])
    assert "busy" in p.describe_frame(frame)
    assert "ERROR" in p.describe_frame(frame)


def test_a_notification_is_described_as_one_not_as_a_reply():
    frame = bytes([0x11, 0x05, 0x0E, 0x00]) + bytes(16)
    assert "notif" in p.describe_frame(frame)


def test_the_root_feature_is_named():
    assert "root.ping" in p.describe_frame(
        p.build_frame(5, p.FEATURE_ROOT, p.ROOT_GET_PROTOCOL_VERSION, b"\x00\x00\xaa")
    )


def test_a_runt_frame_is_described_not_dropped():
    assert "runt" in p.describe_frame(b"\x10\x05")
