"""Tests for the entry point: the sampler thread, the log and the menu wiring.

No test here calls show(). MonitorApp's constructor shows the panel and the
tray icon, so the two methods worth driving directly are run against an
instance assembled with __new__ and handed only the attributes they touch --
real Settings, real HistoryLog, real method body, no window on the desktop.

The sampler tests use events rather than sleeps: a sample() the test holds
open is what makes "25 pokes buy exactly one extra sample" a fact instead of a
guess about timing.
"""

import logging
import logging.handlers
import threading
import time

import pytest
from PyQt6.QtCore import QThread

import main as app_main
import theme
from history import HistoryLog
from metrics import Snapshot
from settings import Settings


class RecordingProbe:
    """Records which thread sample() ran on."""

    def __init__(self):
        self.sample_threads = []

    def sample(self):
        self.sample_threads.append(threading.current_thread())
        return Snapshot(cpu_pct=1.0)


class BlockingProbe:
    """A sample() the test holds open, one call at a time.

    Every call waits on its own event, so a second sample cannot start until
    the first has been released: the test decides when the collector is
    between samples instead of racing it. The wait is bounded so a test that
    forgets to release cannot hang the run.
    """

    def __init__(self, depth=4):
        self.calls = 0
        self.entered = [threading.Event() for _ in range(depth)]
        self.releases = [threading.Event() for _ in range(depth)]

    def sample(self):
        index = self.calls
        self.calls += 1
        if index < len(self.entered):
            self.entered[index].set()
            self.releases[index].wait(5.0)
        return Snapshot(cpu_pct=1.0)


@pytest.fixture
def collectors():
    """Build collectors that get stopped even when the test fails half way.

    A QThread that is still running when its Python wrapper is collected
    aborts the interpreter, which would take the rest of the report with it --
    and a red test is exactly when one is most likely to be left behind.
    """
    made = []

    def build(probe):
        collector = app_main.Collector(probe)
        made.append(collector)
        return collector

    yield build

    for collector in made:
        if collector.isRunning():
            collector.requestInterruption()
            collector.poke()
            collector.wait(2000)


class StubTray:
    """Records the balloon messages an unavailable feature would raise."""

    def __init__(self):
        self.messages = []

    def showMessage(self, *args):
        self.messages.append(args)


def bare_app(tmp_path, log_name="metrics_history.log"):
    """A MonitorApp holding only what _set_log_history and _record_history use."""
    app = app_main.MonitorApp.__new__(app_main.MonitorApp)
    app.settings = Settings()
    app.history_log = HistoryLog(tmp_path / log_name)
    app.tray = StubTray()
    return app


# --- the sampler thread ---------------------------------------------------


def test_sampling_runs_off_the_gui_thread(qapp, collectors):
    probe = RecordingProbe()
    collector = collectors(probe)
    collector.start()

    deadline = 4000
    while not probe.sample_threads and deadline > 0:
        qapp.processEvents()
        QThread.msleep(10)
        deadline -= 10

    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True

    assert probe.sample_threads, "collector never sampled"
    # The blocking part must not be on the GUI thread: this is the 100 ms
    # freeze every two seconds that the worker thread exists to remove.
    assert probe.sample_threads[0] is not threading.current_thread()


def test_snapshot_is_delivered_back_on_the_gui_thread(qapp, collectors):
    probe = RecordingProbe()
    collector = collectors(probe)
    delivered = []
    collector.sampled.connect(lambda snap: delivered.append(threading.current_thread()))
    collector.start()

    deadline = 4000
    while not delivered and deadline > 0:
        qapp.processEvents()
        QThread.msleep(10)
        deadline -= 10

    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True

    assert delivered, "no snapshot reached the GUI thread"
    # Queued connections land on the receiver's thread, which is what lets
    # apply_snapshot touch widgets safely.
    assert delivered[0] is threading.current_thread()


def test_collector_stops_cleanly(qapp, collectors):
    collector = collectors(RecordingProbe())
    collector.start()
    qapp.processEvents()
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True


def test_collector_stops_promptly_on_interruption(qapp, collectors):
    probe = BlockingProbe()
    collector = collectors(probe)
    collector.start()
    assert probe.entered[0].wait(4.0), "collector never sampled"

    probe.releases[0].set()
    collector.requestInterruption()
    started = time.monotonic()
    collector.poke()
    assert collector.wait(3000) is True
    # It must not sit out the rest of the tick before noticing the request.
    assert time.monotonic() - started < 1.0
    assert probe.calls == 1


def test_pokes_collapse_into_one_extra_sample(qapp, collectors):
    probe = BlockingProbe()
    collector = collectors(probe)
    collector.start()
    assert probe.entered[0].wait(4.0), "collector never sampled"

    # Every one of these arrives while a sample is in flight, where a bare
    # wakeAll() has no waiter to wake and is dropped.
    for _ in range(25):
        collector.poke()
    probe.releases[0].set()

    # The window is well inside theme.TICK_MS on purpose: the tick would
    # sample anyway, and a generous deadline would let it satisfy this test
    # while the poke did nothing at all.
    assert probe.entered[1].wait(theme.TICK_MS / 2000.0), "a poke during a sample bought no sample"
    collector.requestInterruption()
    collector.poke()
    probe.releases[1].set()
    assert collector.wait(4000) is True

    # 25 pokes, one extra reading: a request, not a queue entry. Without the
    # collapse a menu opened repeatedly would leave the sampler behind by one
    # tick per poke, sampling as fast as the CPU allows.
    assert probe.calls == 2


# --- shutdown -------------------------------------------------------------


def test_stop_collector_is_bounded_while_a_sample_is_stuck(qapp, collectors):
    probe = BlockingProbe()
    collector = collectors(probe)
    collector.start()
    assert probe.entered[0].wait(4.0), "collector never sampled"

    started = time.monotonic()
    stopped = app_main.stop_collector(collector, 200)
    elapsed = time.monotonic() - started

    # A sample blocked inside psutil cannot be cancelled, so the wait has to
    # have a deadline rather than the collector's.
    assert stopped is False
    assert elapsed < 2.0, "shutdown waited on a sample that was never coming back"

    probe.releases[0].set()
    assert collector.wait(4000) is True


def test_stop_collector_is_safe_to_call_twice(qapp, collectors):
    probe = BlockingProbe()
    collector = collectors(probe)
    collector.start()
    assert probe.entered[0].wait(4.0), "collector never sampled"

    probe.releases[0].set()
    assert app_main.stop_collector(collector) is True
    started = time.monotonic()
    assert app_main.stop_collector(collector) is True
    # Middle-click, the tray's Quit and an external quit can all arrive
    # together; the second must return at once rather than wait again.
    assert time.monotonic() - started < 1.0
    assert probe.calls == 1


# --- the tray / menu settings ---------------------------------------------


def test_disabling_history_stops_the_writes(tmp_path):
    app = bare_app(tmp_path)
    app._set_log_history(True)
    assert app.settings.log_history is True

    app._record_history(Snapshot(cpu_pct=1.0, ts=1.0))
    app._set_log_history(False)
    app._record_history(Snapshot(cpu_pct=2.0, ts=2.0))

    assert app.settings.log_history is False
    rows = (tmp_path / "metrics_history.log").read_text(encoding="utf-8").splitlines()
    # Turning the setting off is not enough: the writer has its own flag, and
    # leaving it on keeps appending to a trace the user switched off.
    assert rows[0] == ",".join(Snapshot.CSV_COLUMNS)
    assert len(rows) == 2


def test_history_that_cannot_be_opened_is_reported(tmp_path):
    # A file where the trace's parent directory has to be: mkdir cannot make a
    # directory inside it, so enable() fails the way a locked-down profile does.
    (tmp_path / "blocker").write_text("not a directory", encoding="utf-8")
    app = bare_app(tmp_path, log_name="blocker/history.log")

    app._set_log_history(True)

    assert app.settings.log_history is False
    assert app.tray.messages, "a history log that would not open failed silently"


# --- logging --------------------------------------------------------------


def test_log_handler_rotates(tmp_path):
    handler = app_main.build_log_handler(tmp_path / "app_debug.log")
    try:
        assert handler.maxBytes == 512 * 1024
        assert handler.backupCount == 2
    finally:
        handler.close()


def test_log_handler_keeps_rotation_bounded(tmp_path):
    target = tmp_path / "app_debug.log"
    handler = app_main.build_log_handler(target)
    logger = logging.getLogger("widget.main.rotation")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        for _ in range(12000):  # ~2.7 MB, enough for three rollovers
            logger.info("x" * 200)
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True

    written = sorted(path.name for path in tmp_path.iterdir())
    # Three backups kept, not four: the size ceiling alone would still let the
    # log grow without bound over a long enough run.
    assert written == ["app_debug.log", "app_debug.log.1", "app_debug.log.2"]
    # A rollover is checked before the next write, so the live file may hold
    # one record past the ceiling; a whole record's worth is the allowance.
    assert max(path.stat().st_size for path in tmp_path.iterdir()) <= 512 * 1024 + 4096


def test_setup_logging_quietens_the_probe_chatter(tmp_path, monkeypatch):
    target = tmp_path / "app_debug.log"
    real_build = app_main.build_log_handler
    monkeypatch.setattr(app_main, "build_log_handler", lambda name=None: real_build(target))
    root = logging.getLogger()
    before = list(root.handlers)
    root_level = root.level
    metrics_level = logging.getLogger("widget.metrics").level
    try:
        app_main.setup_logging()
        app_main.setup_logging()
        rotating = [
            handler
            for handler in root.handlers
            if isinstance(handler, logging.handlers.RotatingFileHandler)
        ]
        assert len(rotating) == 1
        assert logging.getLogger("widget.metrics").level == logging.WARNING

        logging.getLogger("widget.main").info("a line worth keeping")
        logging.getLogger("widget.metrics").info("chatter that must stay out")
        for handler in root.handlers:
            handler.flush()
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(root_level)
        logging.getLogger("widget.metrics").setLevel(metrics_level)

    text = target.read_text(encoding="utf-8")
    assert "a line worth keeping" in text
    # Ten minutes at a tick every two seconds is 300 samples; the per-probe
    # INFO lines were most of what used to fill the file.
    assert "chatter that must stay out" not in text


# --- the parts that must exist at all -------------------------------------


def test_enable_acrylic_rejects_a_bogus_handle():
    assert app_main.enable_acrylic(0) is False


def test_tray_icon_is_not_null(qapp):
    icon = app_main.build_tray_icon()
    assert not icon.isNull()