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
