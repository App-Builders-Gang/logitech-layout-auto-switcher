"""Property-based tests for the wire format.

Framing is the layer everything else trusts, and it is pure -- given bytes in,
bytes out, no hardware. That makes it the one place where generated inputs pay
off: these cover shapes no hand-written example would think to try, including the
malformed frames a receiver can emit when another program is talking to it.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from logiswitch.hidpp import protocol as p

device_indices = st.integers(min_value=0, max_value=0xFF)
feature_indices = st.integers(min_value=0, max_value=0xFF)
functions = st.integers(min_value=0, max_value=0x0F)
sw_ids = st.integers(min_value=1, max_value=0x0F)
short_params = st.binary(min_size=0, max_size=3)
long_params = st.binary(min_size=0, max_size=16)
any_bytes = st.binary(min_size=0, max_size=32)


@given(device_indices, feature_indices, functions, short_params, sw_ids)
def test_a_built_frame_reads_back_as_its_own_response(device, feature, function, params, sw_id):
    """The matcher must accept the echo of the frame it produced."""
    frame = p.build_frame(device, feature, function, params, sw_id=sw_id)
    func_byte = p.function_byte(function, sw_id)

    assert p.is_hidpp_frame(frame)
    assert frame[1] == device
    assert frame[2] == feature
    assert p.is_response_to(frame, device, feature, func_byte)


@given(device_indices, feature_indices, functions, short_params)
def test_frames_are_exactly_one_report_size(device, feature, function, params):
    frame = p.build_frame(device, feature, function, params)
    assert len(frame) in (p.LEN_SHORT, p.LEN_LONG)
    assert len(frame) == p.REPORT_SIZES[frame[0]]


@given(device_indices, feature_indices, functions, long_params)
def test_parameters_survive_the_round_trip(device, feature, function, params):
    frame = p.build_frame(device, feature, function, params, long_report=True)
    assert frame[4 : 4 + len(params)] == params
    assert set(frame[4 + len(params) :]) <= {0}, "unused bytes must be zero"


@given(functions, sw_ids)
def test_the_function_byte_packs_both_nibbles(function, sw_id):
    packed = p.function_byte(function, sw_id)
    assert packed >> 4 == function
    assert packed & 0x0F == sw_id


@given(device_indices, feature_indices, functions)
def test_a_response_is_not_confused_with_a_different_request(device, feature, function):
    """Every field must participate in matching, or replies cross-talk."""
    func_byte = p.function_byte(function)
    frame = p.build_frame(device, feature, function)

    assert not p.is_response_to(frame, (device + 1) & 0xFF, feature, func_byte)
    assert not p.is_response_to(frame, device, (feature + 1) & 0xFF, func_byte)
    assert not p.is_response_to(frame, device, feature, (func_byte + 0x10) & 0xFF)


@given(any_bytes, device_indices, feature_indices, functions)
def test_arbitrary_bytes_never_crash_the_matchers(data, device, feature, function):
    """A receiver shared with other software emits frames we did not ask for."""
    func_byte = p.function_byte(function)
    p.is_hidpp_frame(data)
    p.is_response_to(data, device, feature, func_byte)
    assert p.is_error_for(data, device, feature, func_byte) in (None, 10, 20)


@given(device_indices, feature_indices, functions, st.integers(0, 0xFF))
def test_error_frames_decode_to_their_code(device, feature, function, code):
    func_byte = p.function_byte(function)
    for marker, expected in ((p.ERROR_HIDPP20, 20), (p.ERROR_HIDPP10, 10)):
        frame = bytes([p.REPORT_SHORT, device, marker, feature, func_byte, code, 0])
        assert p.is_error_for(frame, device, feature, func_byte) == expected
        assert p.error_from(frame, expected).code == code


@given(st.integers(min_value=0, max_value=0xFFFF))
def test_os_mask_decoding_is_consistent_with_the_mask_table(mask):
    names = p.os_names_for_mask(mask)
    assert names == sorted(names), "callers rely on a stable order"
    for name in names:
        assert mask & p.OS_MASKS[name]
    for name, bit in p.OS_MASKS.items():
        if mask & bit:
            assert name in names


@given(st.sampled_from(sorted(p.OS_ALIASES)))
def test_every_alias_normalises_to_a_real_mask(alias):
    assert p.normalise_os(alias) in p.OS_MASKS


@settings(max_examples=200)
@given(st.text(max_size=12))
def test_normalise_os_never_invents_an_os(name):
    """Whatever the input, normalise_os either rejects it or returns a real mask.

    A name that is a key of OS_MASKS but not of OS_ALIASES (``tizen``, ``webos``,
    ``winemb``) is legitimately accepted -- it is a canonical OS -- so the guard
    must be "does it normalise to a known mask", not "is it an alias".
    """
    try:
        result = p.normalise_os(name)
    except ValueError as exc:
        # A rejection must name the input and suggest real choices.
        assert "unknown OS" in str(exc)
        return
    assert result in p.OS_MASKS
