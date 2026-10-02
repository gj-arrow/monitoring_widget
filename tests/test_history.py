import logging
import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QPointF

from history import History, resample
from metrics import Snapshot


def _row(*values):
    """A CSV row with its empty tail built from the column count.

    Written out -- "3,9,,,,,,,," -- these rows have to be edited by hand every
    time a metric joins the dataclass, which is how a row assertion quietly
    stops being about anything. The cells go through history._csv, the formatter
    the writer itself uses, so the helper cannot drift from it and leave these
    tests asserting a spelling nothing produces. What _csv *does* is pinned
    separately by test_a_trace_value_round_trips_through_repr.
    """
    from history import _csv

    columns = len(Snapshot.CSV_COLUMNS)
    assert len(values) <= columns, "more values than there are columns"
    return ",".join([_csv(value) for value in values] + [""] * (columns - len(values)))



def test_history_respects_maxlen():
    h = History(maxlen=3)
    for value in (0.1, 0.2, 0.3, 0.4):
        h.append(value)
    assert h.values() == [0.2, 0.3, 0.4]


def test_history_ignores_missing_samples():
    h = History()
    h.append(None)
    h.append(0.5)
    h.append(None)
    assert h.values() == [0.5]


def test_history_clamps_out_of_range_input():
    h = History()
    h.append(-5.0)
    h.append(5.0)
    assert h.values() == [0.0, 1.0]


def test_history_ignores_nan_rather_than_recording_it_as_zero():
    # NaN is "not measured", the same as None -- it must not become a sample.
    # Clamping alone cannot do this: `min(1.0, max(0.0, nan))` is 0.0,
    # because `max` drops the NaN comparison, so an unguarded `append` files
    # a failed reading as a real 0% data point and the graph draws it as one.
    h = History()
    h.append(float("nan"))
    h.append(0.5)
    h.append(float("nan"))
    assert h.values() == [0.5]


def test_history_still_clamps_infinity():
    # inf is an over-range reading, not a missing one, so clamping it to 1.0
    # is the honest answer and must survive the NaN guard.
    h = History()
    h.append(float("inf"))
    h.append(float("-inf"))
    assert h.values() == [1.0, 0.0]


def test_resample_keeps_a_one_sample_spike():
    # The spike has to share a column window with values close to the
    # previously drawn point, otherwise every window is single-valued and
    # uniform sampling returns the same answer -- the test would pass
    # without the tie-break rule doing anything.
    points = [0.0, 0.0, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    got = resample(points, 4)
    assert max(p.y() for p in got) == 1.0


def test_resample_beats_uniform_sampling_at_preserving_peaks():
    points = [0.0, 0.0, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    naive = [points[column * len(points) // 4] for column in range(4)]
    assert naive == [0.0, 0.0, 0.0, 0.0]  # uniform sampling loses it
    assert max(p.y() for p in resample(points, 4)) == 1.0


def test_resample_only_ever_draws_measured_values_and_keeps_the_spike():
    # Nearly flat but not constant. A constant series would prove nothing:
    # averaging, min, max and nearest-to-previous all return that same
    # constant, so the assertion passes whatever the rule is. These ten
    # samples land in windows of two whose averages (0.2025, 0.577, 0.1995)
    # were never measured, so a rule that averages or interpolates emits a
    # y no sample ever produced, and a rule that shrinks to the quiet value
    # throws away the 0.951 spike. Both naive rules fail; selection from
    # the window cannot do either.
    points = [
        0.201, 0.204, 0.198, 0.207, 0.951,
        0.203, 0.196, 0.209, 0.194, 0.205,
    ]
    ys = [p.y() for p in resample(points, 5)]
    assert len(ys) == 5
    assert set(ys) <= set(points)
    assert max(ys) == 0.951


def test_resample_tie_break_is_measured_from_the_previous_column():
    # The rule the docstring spends a paragraph on is "every column after the
    # first takes the value furthest from the point drawn just before it".
    # Deleting that key -- falling back to plain `max(window)` everywhere --
    # leaves every other test in this file green, so this is the only thing
    # standing between that refactor and a silent behaviour change.
    #
    # This input discriminates. At width 2 the windows are [1.0, 0.0] then
    # [0.8, 0.95]. Column 0 has no predecessor, so it falls back to plain
    # max(window) and yields 1.0. Column 1 then measures distance from that
    # 1.0: 0.8 is 0.2 away, 0.95 only 0.05, so the tie-break picks 0.8 --
    # the larger step, not the larger value. Plain max picks 0.95 instead,
    # so the two rules disagree on the same input and this test bites.
    points = [1.0, 0.0, 0.8, 0.95]
    ys = [p.y() for p in resample(points, 2)]
    assert ys == [1.0, 0.8]


def test_resample_first_column_uses_plain_max_with_no_predecessor():
    # The documented exception to the rule above, pinned so it cannot be
    # "tidied up" into the tie-break by accident. Column 0 has nothing to
    # measure a step from, so it takes the column's peak -- the only
    # spike-preserving choice left. [0.0, 1.0] must give 1.0, not 0.0.
    assert [p.y() for p in resample([0.0, 1.0], 1)] == [1.0]
    assert [p.y() for p in resample([1.0, 0.0, 0.8, 0.95], 2)][0] == 1.0


def test_resample_keeps_every_point_when_it_fits():
    points = [0.1, 0.4, 0.9]
    got = resample(points, 10)
    assert [p.y() for p in got] == points


def test_resample_single_point_starts_at_the_left_edge():
    assert resample([0.5], 10) == [QPointF(0.0, 0.5)]


def test_resample_empty_and_zero_width_are_empty():
    assert resample([], 10) == []
    assert resample([0.5], 0) == []


def test_resample_negative_width_is_empty():
    assert resample([0.5], -3) == []
    assert resample([0.5, 0.9], -1) == []


def test_resample_single_column_keeps_the_extreme():
    # One column is still one extreme, not just the first sample.
    assert resample([0.1, 0.9, 0.5], 1) == [QPointF(0.0, 0.9)]
    assert resample([0.9], 1) == [QPointF(0.0, 0.9)]


def test_resample_exactly_as_wide_as_the_data_is_one_pixel_per_point():
    points = [0.1, 0.4, 0.9]
    assert resample(points, 3) == [
        QPointF(0.0, 0.1),
        QPointF(1.0, 0.4),
        QPointF(2.0, 0.9),
    ]


def test_resampled_ends_at_the_right_edge_when_fewer_points():
    h = History()
    h.append(0.2)
    h.append(0.8)
    got = h.resampled(20)
    assert got == [QPointF(0.0, 0.2), QPointF(19.0, 0.8)]


def test_default_history_length_comes_from_theme():
    import theme

    h = History()
    for step in range(theme.HISTORY_LEN + 1):
        h.append(step / theme.HISTORY_LEN)
    assert len(h) == theme.HISTORY_LEN


def _write_trace(path, header, rows):
    path.write_text(header + "\n" + "".join(row + "\n" for row in rows), encoding="utf-8")


def test_history_log_rotates_a_stale_header_instead_of_mixing_columns(tmp_path, caplog):
    # A trace left by a build with other columns is not a header this build
    # can honour. Appending to it writes rows of one width under a header of
    # another, and every column after the first is silently wrong -- so the
    # old trace is rotated aside and the new one starts clean.
    import logging

    from history import HistoryLog

    path = tmp_path / "history.log"
    _write_trace(path, "t,cpu,gpu,temp", ["0,1,2,3", "1,4,5,6"])

    log = HistoryLog(path)
    assert log.enable() is True
    with caplog.at_level(logging.WARNING, logger="widget.history"):
        log.write(Snapshot(cpu_pct=7.0, ts=2.0))

    kept = tmp_path / "history.log.1"
    assert kept.read_text(encoding="utf-8") == "t,cpu,gpu,temp\n0,1,2,3\n1,4,5,6\n"
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert header == ",".join(Snapshot(cpu_pct=7.0, ts=2.0).as_dict().keys())
    assert row == _row(2.0, 7.0)
    assert any("history.log.1" in record.message for record in caplog.records)


def test_history_log_keeps_a_matching_header_and_only_appends_a_row(tmp_path):
    # The rotation must not fire on a trace this build could have written
    # itself, or a normal restart would strand a fresh file every time.
    from history import HistoryLog

    path = tmp_path / "history.log"
    header = ",".join(Snapshot().as_dict().keys())
    _write_trace(path, header, [_row(1.0, 2.0)])

    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=9.0, ts=3.0))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines == [header, _row(1.0, 2.0), _row(3.0, 9.0)]
    assert not (tmp_path / "history.log.1").exists()


def test_history_log_rotation_takes_the_next_free_suffix(tmp_path):
    # Rotating must never overwrite a trace that is already there: the whole
    # point of keeping the stale file is that it is somebody's data.
    from history import HistoryLog

    path = tmp_path / "history.log"
    _write_trace(path, "a,b", ["1,2"])
    _write_trace(tmp_path / "history.log.1", "earlier,still", ["9,9"])

    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=1.0, ts=1.0))

    assert (tmp_path / "history.log.1").read_text(encoding="utf-8") == "earlier,still\n9,9\n"
    assert (tmp_path / "history.log.2").read_text(encoding="utf-8") == "a,b\n1,2\n"


def test_history_log_refuses_to_write_when_a_stale_trace_cannot_be_rotated(tmp_path, monkeypatch, caplog):
    # Rotation is what keeps two schemas out of one file. If it fails, the only
    # honest move left is to stop: appending would write a new header and new
    # rows into a file whose existing rows answer to different columns, and
    # nothing downstream can tell which is which afterwards.
    from history import HistoryLog

    stale = "t,cpu,gpu,temp\n0,1,2,3\n"
    path = tmp_path / "history.log"
    path.write_text(stale, encoding="utf-8")

    def locked(self, target):
        raise OSError("the file is open in another program")

    monkeypatch.setattr(Path, "replace", locked)

    log = HistoryLog(path)
    log.enable()
    with caplog.at_level(logging.WARNING, logger="widget.history"):
        log.write(Snapshot(cpu_pct=7.0, ts=2.0))

    assert path.read_text(encoding="utf-8") == stale
    assert log.enabled is False
    assert not (tmp_path / "history.log.1").exists()
    assert any("cannot be rotated" in r.getMessage() for r in caplog.records)


def test_history_log_treats_a_deleted_file_as_a_new_trace(tmp_path, caplog):
    # The file can vanish between enable() and the first write. That is not a
    # file to rotate, so the log must not claim otherwise: the append recreates
    # it with a correct header and nothing is reported as unreadable.
    from history import HistoryLog

    path = tmp_path / "history.log"
    log = HistoryLog(path)
    log.enable()
    path.unlink()

    with caplog.at_level(logging.WARNING, logger="widget.history"):
        log.write(Snapshot(cpu_pct=7.0, ts=2.0))

    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert header == ",".join(Snapshot(cpu_pct=7.0, ts=2.0).as_dict().keys())
    assert row == _row(2.0, 7.0)
    assert not [r for r in caplog.records if "rotat" in r.getMessage()]
    assert not [r for r in caplog.records if "unreadable" in r.getMessage()]


def test_history_log_rechecks_the_header_after_being_switched_off(tmp_path, monkeypatch):
    # Switching off left the header-check flags as they were, so the next
    # enable() -- which is exactly what the tray menu does -- skipped the check
    # and appended this build's ten columns straight into the stale file. Only
    # the first attempt at a rotation is made to fail here, so the retry has to
    # notice the foreign header again and rotate for real.
    from history import HistoryLog

    stale = "t,cpu,gpu,temp\n0,1,2,3\n"
    path = tmp_path / "history.log"
    path.write_text(stale, encoding="utf-8")

    real_replace = Path.replace
    attempts = []

    def flaky_replace(self, target):
        attempts.append(target)
        if len(attempts) == 1:
            raise OSError("the file is open in another program")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=7.0, ts=2.0))
    assert log.enabled is False
    assert path.read_text(encoding="utf-8") == stale

    assert log.enable() is True
    log.write(Snapshot(cpu_pct=8.0, ts=3.0))

    assert len(attempts) == 2
    assert log.enabled is True
    # The stale trace is kept whole and unappended, in its rotated file.
    assert (tmp_path / "history.log.1").read_text(encoding="utf-8") == stale
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert header == ",".join(Snapshot(cpu_pct=8.0, ts=3.0).as_dict().keys())
    assert row == _row(3.0, 8.0)


def test_history_log_rechecks_the_header_after_an_explicit_disable(tmp_path):
    # The mirror of the test above: after a normal disable/enable the check
    # runs again and finds this build's own header, so nothing is rotated and
    # the trace simply continues.
    from history import HistoryLog

    path = tmp_path / "history.log"
    _write_trace(path, "t,cpu,gpu,temp", ["0,1,2,3"])

    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=7.0, ts=2.0))
    log.disable()
    log.enable()
    log.write(Snapshot(cpu_pct=8.0, ts=3.0))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[1:] == [_row(2.0, 7.0), _row(3.0, 8.0)]
    assert not (tmp_path / "history.log.2").exists()


def test_history_log_rechecks_when_adopting_the_header_did_not_finish(tmp_path, monkeypatch):
    # The check flag used to be set before the check ran, so an error part-way
    # through adopting it left the log believing the file had been vetted: the
    # retry after a re-enable skipped the check and appended to whatever was
    # on disk. An unexpected error still escapes write() -- that is out of
    # scope here -- but it must not be cached as a completed check.
    from history import HistoryLog

    stale = "t,cpu,gpu,temp\n0,1,2,3\n"
    path = tmp_path / "history.log"
    path.write_text(stale, encoding="utf-8")

    real_exists = Path.exists
    broken = {"now": True}

    def flaky_exists(self):
        if broken["now"]:
            broken["now"] = False
            raise RuntimeError("the filesystem is having a moment")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", flaky_exists)

    log = HistoryLog(path)
    log.enable()
    with pytest.raises(RuntimeError):
        log.write(Snapshot(cpu_pct=7.0, ts=2.0))

    log.write(Snapshot(cpu_pct=8.0, ts=3.0))

    assert (tmp_path / "history.log.1").read_text(encoding="utf-8") == stale
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert header == ",".join(Snapshot(cpu_pct=8.0, ts=3.0).as_dict().keys())
    assert row == _row(3.0, 8.0)


def test_history_log_rotates_a_trace_it_cannot_decode(tmp_path):
    # A log saved by Excel as UTF-16, or written as cp1251 by a build on a
    # Russian-locale machine, is not UTF-8. A decode failure is a ValueError
    # rather than an OSError, so it used to escape write() whole and land in
    # the sampling tick. Garbled bytes must degrade into the rotate branch:
    # the trace is kept, never appended to.
    from history import HistoryLog

    path = tmp_path / "history.log"
    saved = "ts,cpu_pct\r\n1,2\r\n".encode("utf-16")
    path.write_bytes(saved)

    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=7.0, ts=2.0))

    assert (tmp_path / "history.log.1").read_bytes() == saved
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert header == ",".join(Snapshot(cpu_pct=7.0, ts=2.0).as_dict().keys())
    assert row == _row(2.0, 7.0)


# --- two threads, one log -------------------------------------------------
#
# The collector writes a row every tick and the tray toggle calls
# enable()/disable() from the GUI thread, so enable(), disable() and write()
# race. Two threads hammering those in a loop reproduce nothing: the window
# inside write() is one open-and-append wide, so the loop would have to be
# enormous and even then it would only fail sometimes.
#
# So the interleaving is driven from outside. PausingPath is a Path that stops
# the writer at a chosen instant inside write(), which turns the race into a
# schedule instead of a probability -- and needs no seam in history.py, because
# the filesystem is already part of what write() is doing while it is exposed.
#
# Each test's one wait is for something that must NOT happen: a caller
# returning while a write is still in flight. With the lock it cannot return at
# all, so the wait always expires and the assertion is one-sided -- correct code
# cannot turn it red, and unlocked code is three attribute stores and returns
# well inside the window.
ESCAPE_WINDOW = 1.0


class PausingHandle:
    """Wraps the append handle and pauses once, just after the first line.

    That first line is the header, and write() has not recorded writing it yet
    when the pause happens: a second thread arriving here sees a file with no
    header and writes its own.
    """

    def __init__(self, handle, paused, resume):
        self._handle = handle
        self._paused = paused
        self._resume = resume

    def write(self, text):
        written = self._handle.write(text)
        if not self._paused.is_set():
            self._paused.set()
            assert self._resume.wait(10.0), "the paused writer was never released"
        return written

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._handle.__exit__(*exc_info)


class PausingPath:
    """A Path that pauses the first appending writer at `stop_at`.

    "before_append" -- inside write(), after it has read the enabled flag and
    settled the header question, immediately before the file is opened to
    append. A disable() that completes here is a row written into a trace the
    user has just switched off.

    "after_header" -- inside that same append, between the header line and the
    first row, which is the only moment a second header can be produced.
    """

    def __init__(self, path, stop_at):
        self._path = Path(path)
        self._stop_at = stop_at
        self.paused = threading.Event()
        self.resume = threading.Event()

    def open(self, mode="r", *args, **kwargs):
        handle = self._path.open(mode, *args, **kwargs)
        if "a" not in mode:
            return handle
        if self._stop_at == "after_header":
            return PausingHandle(handle, self.paused, self.resume)
        self.paused.set()
        assert self.resume.wait(10.0), "the paused writer was never released"
        return handle

    def __getattr__(self, name):
        return getattr(self._path, name)


def hooked_log(tmp_path, stop_at="before_append"):
    """An enabled HistoryLog whose path pauses its first writer, and that path."""
    from history import HistoryLog

    path = tmp_path / "history.log"
    hook = PausingPath(path, stop_at)
    log = HistoryLog(path)
    log._path = hook
    assert log.enable() is True
    return log, hook


def run_guarded(failures, call):
    """Run `call` on a thread, keeping its exception instead of losing it.

    A thread that dies quietly is indistinguishable from a thread that finished
    quickly, and the difference between those two is this test's whole subject.
    """

    def run():
        try:
            call()
        except BaseException as exc:
            failures.append(exc)

    return run


def test_a_byte_rate_in_the_trace_is_not_written_in_scientific_notation(tmp_path):
    # {:g} switches to exponent form at 1e6. Every column the trace carried
    # before this task stayed under it -- RAM is divided by 1024**3 on the way in
    # and lands at 32.0 -- and the byte rates are the first that do not: a
    # 100 Mbit link is 12,500,000.0 B/s, which {:g} wrote as "1.25e+07".
    # Parseable, and not what a human wants from a file whose whole point is
    # that they can read it.
    from history import HistoryLog

    path = tmp_path / "history.log"
    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(ts=1.0, cpu_pct=5.0, ram_total_gb=32.0,
                      net_down_bytes_per_sec=123456789.0, net_up_bytes_per_sec=12500000.0))
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    columns = row.split(",")
    assert "e" not in row and "E" not in row, f"scientific notation in the trace: {row}"
    assert columns[Snapshot.CSV_COLUMNS.index("net_down_bytes_per_sec")] == "123456789.0"
    assert columns[Snapshot.CSV_COLUMNS.index("net_up_bytes_per_sec")] == "12500000.0"
    # The other columns keep the shape they always had: repr is the same
    # shortest round-tripping spelling everywhere, not a per-column rule.
    assert columns[Snapshot.CSV_COLUMNS.index("ram_total_gb")] == "32.0"
    assert columns[0] == "1.0" and columns[1] == "5.0"
    assert header.split(",") == list(Snapshot.CSV_COLUMNS)


@pytest.mark.parametrize(
    "value", [0.0, 1.0, 5.5, 32.0, 61.25, 999.9, 1e6 - 1, 1e6, 1.0e12, 0.1 + 0.2],
)
def test_a_trace_value_round_trips_through_repr(value):
    # repr is the shortest spelling that parses back to the same float, so the
    # trace keeps every digit instead of {:g}'s six significant figures -- which
    # is the other half of why {:g} was the wrong formatter here.
    from history import _csv

    assert float(_csv(value)) == value
    assert _csv(value) == repr(value)
    assert "e" not in _csv(value)


def test_no_row_is_appended_after_disable_has_returned(tmp_path):
    """A row must not land in a trace that has already been switched off.

    Unlocked, the writer reads the enabled flag, the toggle clears it and
    returns, and the writer appends anyway -- one row past the point where the
    user believes the trace stopped, which is the whole reason disable() exists.
    """

    log, hook = hooked_log(tmp_path)
    failures = []
    row = Snapshot(cpu_pct=1.0, ts=1.0)

    writer = threading.Thread(target=run_guarded(failures, lambda: log.write(row)))
    writer.start()
    assert hook.paused.wait(5.0), "the writer never reached the append"

    toggler = threading.Thread(target=run_guarded(failures, log.disable))
    toggler.start()
    toggler.join(ESCAPE_WINDOW)
    waited = toggler.is_alive()

    hook.resume.set()
    writer.join(5.0)
    toggler.join(5.0)
    assert not failures, f"a thread raised: {failures[0]!r}"

    assert waited, "disable() returned while a write was still in flight"
    # What the user asked for, now that the in-flight row has landed: the trace
    # is closed, and a later sample adds nothing.
    log.write(Snapshot(cpu_pct=9.0, ts=9.0))
    assert log.enabled is False
    lines = (tmp_path / "history.log").read_text(encoding="utf-8").strip().splitlines()
    assert lines == [",".join(row.as_dict().keys()), _row(1.0, 1.0)]


def test_the_header_is_written_once_when_two_writers_overlap(tmp_path):
    """Two writes must not both decide that the file has no header.

    Unlocked, the first writer pauses with its header line on disk and no memory
    of having written one, so the second writes a second header above a row that
    belongs to the first -- the schema mixing that _adopt_existing_header()
    exists to prevent, arriving from a different direction.
    """

    log, hook = hooked_log(tmp_path, stop_at="after_header")
    failures = []

    writer = threading.Thread(
        target=run_guarded(failures, lambda: log.write(Snapshot(cpu_pct=1.0, ts=1.0)))
    )
    writer.start()
    assert hook.paused.wait(5.0), "the writer never reached the header line"

    second = threading.Thread(
        target=run_guarded(failures, lambda: log.write(Snapshot(cpu_pct=2.0, ts=2.0)))
    )
    second.start()
    second.join(ESCAPE_WINDOW)
    waited = second.is_alive()

    hook.resume.set()
    writer.join(5.0)
    second.join(5.0)
    assert not failures, f"a thread raised: {failures[0]!r}"

    assert waited, "a second write ran inside the first one's append"
    header = ",".join(Snapshot(cpu_pct=1.0, ts=1.0).as_dict().keys())
    lines = (tmp_path / "history.log").read_text(encoding="utf-8").strip().splitlines()
    assert lines.count(header) == 1, f"the header was written {lines.count(header)} times"
    assert lines == [header, _row(1.0, 1.0), _row(2.0, 2.0)]
