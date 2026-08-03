"""The frame ring and the health counters.

The behaviour worth pinning is that these survive being the thing you reach for
*after* something went wrong: bounded memory, no exception escaping a dump, and a
windowed rate that a recurring-but-corrected fault cannot reset.
"""

from __future__ import annotations

import time

from logiswitch import trace


def test_the_ring_is_bounded(monkeypatch):
    monkeypatch.setattr(trace, "RING_SIZE", 8)
    monkeypatch.setattr(trace, "_records", type(trace._records)(maxlen=8))
    for i in range(50):
        trace.record(trace.OUT, "test", bytes([0x10, i, 0, 0]))
    entries = trace.snapshot()
    assert len(entries) == 8
    assert entries[-1].hexbytes.startswith("1031")  # frame 49


def test_records_render_with_a_timestamp_and_direction():
    trace.record(trace.ORPHAN, "bolt", bytes([0x11, 5, 0x10, 0x2E]), "a summary")
    line = trace.snapshot()[-1].render()
    assert trace.ORPHAN in line
    assert "bolt" in line
    assert "a summary" in line
    assert "11051" in line.replace(" ", "")  # the hex survived


def test_notes_go_into_the_ring_alongside_frames():
    trace.record(trace.OUT, "bolt", b"\x10\x05\x00\x1e")
    trace.note("about to write platform 1")
    rendered = trace.render()
    assert "about to write platform 1" in rendered
    assert rendered.count("\n") == 1


def test_render_says_so_when_nothing_was_recorded():
    assert trace.render() == "(no frames recorded)"


def test_counters_add_up():
    assert trace.HEALTH.bump("orphans") == 1
    assert trace.HEALTH.bump("orphans") == 2
    assert trace.HEALTH.get("orphans") == 2
    assert trace.HEALTH.get("never-touched") == 0
    assert "orphans=2" in trace.HEALTH.summary()


def test_summary_hides_counters_that_are_zero():
    trace.HEALTH.bump("orphans", 0)
    assert "orphans" not in trace.HEALTH.summary()


def test_a_windowed_rate_is_not_reset_by_success():
    """The property the old consecutive counter lacked.

    A fault that is corrected between every occurrence still has to register, or a
    keyboard reverting every twelve seconds forever looks like it never recurred.
    """
    for _ in range(6):
        trace.HEALTH.mark("platform_corrections")
        # ... and here the agent succeeds, which is what used to zero the count.
    assert trace.HEALTH.rate("platform_corrections", window=300.0) == 6


def test_a_windowed_rate_forgets_what_is_out_of_window():
    trace.HEALTH.mark("reconnects")
    assert trace.HEALTH.churn(window=60.0) == 1
    assert trace.HEALTH.churn(window=0.0) == 0


def test_rate_of_an_unseen_event_is_zero():
    assert trace.HEALTH.rate("nothing-happened", window=60.0) == 0


def test_reset_clears_counts_and_windows():
    trace.HEALTH.mark("reconnects")
    trace.HEALTH.reset()
    assert trace.HEALTH.get("reconnects") == 0
    assert trace.HEALTH.churn(60.0) == 0
    assert trace.HEALTH.summary() == "no HID++ activity yet"


def test_dump_writes_the_ring_with_the_counters(tmp_path):
    trace.HEALTH.bump("orphans")
    trace.record(trace.IN, "bolt", b"\x11\x05\x10\x2e", "a reply")
    written = trace.dump("because a test asked", tmp_path / "t.log")
    assert written is not None
    content = written.read_text()
    assert "because a test asked" in content
    assert "orphans=1" in content
    assert "a reply" in content


def test_dump_appends_rather_than_replacing(tmp_path):
    path = tmp_path / "t.log"
    trace.dump("first", path)
    trace.dump("second", path)
    content = path.read_text()
    assert "first" in content and "second" in content


def test_dump_never_raises_when_the_destination_is_unwritable(tmp_path):
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory")
    assert trace.dump("nowhere to go", blocked / "nested" / "t.log") is None


def test_anomaly_is_a_no_op_until_a_destination_is_set(tmp_path):
    assert trace.anomaly("nobody is listening") is None
    trace.set_dump_path(tmp_path / "t.log")
    assert trace.anomaly("now someone is") is not None


def test_the_trace_file_is_rolled_aside_when_it_grows(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "TRACE_MAX_BYTES", 64)
    path = tmp_path / "t.log"
    path.write_text("x" * 200)
    trace.dump("after the roll", path)
    assert path.with_suffix(".log.1").exists()
    assert "after the roll" in path.read_text()


def test_echo_streams_frames_to_the_log(caplog):
    caplog.set_level("DEBUG", logger="logiswitch.trace")
    trace.record(trace.OUT, "bolt", b"\x10\x05\x00\x1e", "quiet")
    assert "quiet" not in caplog.text
    trace.set_echo(True)
    assert trace.echoing()
    trace.record(trace.OUT, "bolt", b"\x10\x05\x00\x1e", "loud")
    assert "loud" in caplog.text


def test_health_started_tracks_uptime():
    trace.HEALTH.reset()
    trace.HEALTH.bump("requests")
    time.sleep(0.01)
    assert "uptime=" in trace.HEALTH.summary()
