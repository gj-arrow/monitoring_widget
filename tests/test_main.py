"""Tests for the entry point: the sampler thread, the log and the menu wiring.

No test here calls show(). MonitorApp's constructor shows the panel and the
tray icon, so the methods worth driving directly are run against an instance
assembled with __new__ and handed only the attributes they touch -- real
Settings, real MonitorPanel, real HistoryLog, real method bodies, no window on
the desktop.

The sampler tests use events rather than sleeps: a sample() the test holds
open is what makes "25 pokes buy exactly one extra sample" a fact instead of a
guess about timing.
"""

import ctypes
import logging
import logging.handlers
import threading
import time
from dataclasses import asdict

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt, QThread
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

import main as app_main
import theme
from history import HistoryLog
from metrics import Snapshot
from overlay import MonitorPanel
from settings import Settings


class RecordingProbe:
    """Records which Qt thread sample() ran on."""

    def __init__(self):
        self.sample_threads = []

    def sample(self):
        # QThread.currentThread(), not threading.current_thread(): inside
        # QThread.run() the standard library hands out a bare _DummyThread that
        # carries no identity, so a probe recording that cannot say *which*
        # thread it was on -- which is the whole question here.
        self.sample_threads.append(QThread.currentThread())
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


class CollapsingProbe:
    """Holds the first sample open, then answers instantly.

    The collapse can only be judged while the sampler is free to run. A probe
    that blocks on every call hides a backlog behind a release the test has not
    given yet, which is how one reading and twenty-five outstanding both come
    to look like two samples.
    """

    def __init__(self):
        self.calls = 0
        self.first_entered = threading.Event()
        self.release_first = threading.Event()

    def sample(self):
        self.calls += 1
        if self.calls == 1:
            self.first_entered.set()
            self.release_first.wait(5.0)
        return Snapshot(cpu_pct=1.0)


class RecordingLog:
    """A HistoryLog that remembers who wrote to it, and writes nothing."""

    def __init__(self):
        self.write_threads = []
        self.snapshots = []

    def enable(self):
        return True

    def disable(self):
        pass

    def write(self, snapshot):
        self.write_threads.append(QThread.currentThread())
        self.snapshots.append(snapshot)


class StubCollector:
    """Records the pokes a menu action sends the sampler."""

    def __init__(self):
        self.pokes = 0

    def poke(self):
        self.pokes += 1


class StubTimer:
    """The one call shutdown() makes on the tick timer."""

    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class StubTray:
    """Records the balloon messages an unavailable feature would raise."""

    def __init__(self):
        self.messages = []
        self.hides = 0

    def showMessage(self, *args):
        self.messages.append(args)

    def hide(self):
        self.hides += 1


class StubApplication:
    """QApplication.quit(), made observable.

    With no event loop running Qt's quit() is a no-op -- it does not even emit
    aboutToQuit() -- so the real one would leave "quit exactly once" untestable
    from a test. The stub keeps the real ordering, and the real guard: Qt emits
    aboutToQuit at most once per quit(), so a quit() triggered from inside the
    hook does not emit it again. Without that guard a shutdown missing its
    _stopped flag would recurse until the stack gave out, and the run would
    stop with nothing to read instead of a failure to read.
    """

    def __init__(self, qapp):
        self._qapp = qapp
        self.quits = 0
        self._emitting = False

    def quit(self):
        self.quits += 1
        if self._emitting:
            return
        self._emitting = True
        try:
            self._qapp.aboutToQuit.emit()
        finally:
            self._emitting = False


def wheel_notch(delta_y):
    return QWheelEvent(
        QPointF(50.0, 50.0),
        QPointF(50.0, 50.0),
        QPoint(0, 0),
        QPoint(0, delta_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def wait_until(predicate, timeout_s):
    """Poll for something a worker thread produced, pumping the GUI thread.

    The signals under test are queued to the GUI thread, so a wait that does
    not process events is a wait that cannot see them land.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        QApplication.processEvents()
        QThread.msleep(2)
    return bool(predicate())


@pytest.fixture
def collectors():
    """Build collectors that get stopped even when the test fails half way.

    build() takes a probe and returns a Collector, or takes a Collector the
    test assembled itself and adopts that one -- a Collector passed as a probe
    would be sampled for as if it had a sample() method, and the AttributeError
    that raises inside run() is a qFatal, which takes the run with it instead
    of reporting. Either way the thread is joined at teardown, because a
    QThread still running when its Python wrapper is collected aborts the
    interpreter, and a red test is exactly when one is most likely to be left
    behind.
    """
    made = []

    def build(probe):
        collector = (
            probe if isinstance(probe, app_main.Collector) else app_main.Collector(probe)
        )
        made.append(collector)
        return collector

    yield build

    for collector in made:
        if collector.isRunning():
            collector.requestInterruption()
            collector.poke()
            collector.wait(2000)


class StubbornCollector:
    """A sampler that will not stop, and counts how often it was waited on.

    The worst case for a second shutdown: the thread is still there, and the
    only thing standing between the caller and another full timeout is the
    guard that says this work is already done.
    """

    def __init__(self):
        self.waited = []

    def isRunning(self):
        return True

    def requestInterruption(self):
        pass

    def poke(self):
        pass

    def wait(self, timeout_ms=None):
        # Honours the deadline it is given, so a regression costs a timeout
        # worth of wall clock and a readable failure, not a stalled run.
        self.waited.append(timeout_ms)
        QThread.msleep(timeout_ms or 60000)
        return False


def bare_app(tmp_path, log_name="metrics_history.log"):
    """A MonitorApp holding only what the methods under test touch."""
    app = app_main.MonitorApp.__new__(app_main.MonitorApp)
    app.settings = Settings()
    app.history_log = HistoryLog(tmp_path / log_name)
    app.tray = StubTray()
    app.panel = MonitorPanel(app.settings)
    return app


def menu_app(tmp_path):
    """The same, plus the state _build_menu needs and _sync_menu writes back."""
    app = bare_app(tmp_path)
    app.collector = StubCollector()
    app._alpha_actions = {}
    app._toggle_actions = {}
    return app


@pytest.fixture
def shutting_down(qapp, tmp_path, monkeypatch, collectors):
    """A MonitorApp with everything shutdown() reaches for, torn down after.

    Yields (app, saved, application). save_settings is intercepted because
    shutdown() writes the real settings.json, which belongs to the user and not
    to the test run.
    """
    app = bare_app(tmp_path)
    app._stopped = False
    app.timer = StubTimer()
    app.collector = collectors(RecordingProbe())
    app.collector.start()

    saved = []
    monkeypatch.setattr(
        app_main, "save_settings", lambda settings, path=None: bool(saved.append(asdict(settings)))
    )
    application = StubApplication(qapp)
    monkeypatch.setattr(app_main, "QApplication", application)

    yield app, saved, application

    if app.collector.isRunning():
        app.collector.requestInterruption()
        app.collector.poke()
        app.collector.wait(2000)


# --- the sampler thread ---------------------------------------------------


def test_sampling_runs_on_the_collector_thread(qapp, collectors):
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
    sampled_on = probe.sample_threads[0]
    # Not merely "some thread that is not this one" -- an unrelated third
    # thread would satisfy that. The blocking part belongs to the collector's
    # own QThread: that is the thread whose existence took the 100 ms psutil
    # freeze off the GUI thread every two seconds, and a sampler that was
    # merely somewhere else would put it straight back.
    assert sampled_on is collector, f"sampled on {sampled_on!r}, not the collector's thread"


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
    probe = CollapsingProbe()
    collector = collectors(probe)
    collector.start()
    assert probe.first_entered.wait(4.0), "collector never sampled"

    # Every one of these arrives while a sample is in flight, where a bare
    # wakeAll() has no waiter to wake and is dropped.
    for _ in range(25):
        collector.poke()
    probe.release_first.set()

    # The first extra reading is the collapse working: the tick has not expired
    # yet, so nothing else could have produced it.
    assert wait_until(lambda: probe.calls >= 2, 4.0), "a poke during a sample bought no sample"

    # And that is the whole of it. Measured over a window longer than a tick,
    # *before* anything asks the collector to stop: after requestInterruption()
    # the loop leaves at the top without servicing the queue, so a test that
    # interrupts first cannot tell one reading from twenty-five.
    #
    # Two readings is the answer -- the first, plus the one the pokes bought --
    # and one more is the collector's own clock going round, because the wait
    # at the bottom of the loop expires after a tick and samples whether or not
    # anyone asked. Three is the bound. A backlog drains as fast as the CPU
    # allows: twenty-five further readings inside this window, not one.
    quiet_window = theme.TICK_MS / 1000.0
    deadline = time.monotonic() + quiet_window
    while time.monotonic() < deadline:
        QThread.msleep(5)

    collector.requestInterruption()
    collector.poke()
    assert collector.wait(4000) is True

    assert probe.calls <= 3, (
        f"{probe.calls} samples for 25 pokes in {quiet_window:.1f}s: "
        "the pokes queued instead of collapsing"
    )


# --- shutdown -------------------------------------------------------------


def test_stop_collector_is_bounded_while_a_sample_is_stuck(qapp, collectors):
    probe = BlockingProbe()
    collector = collectors(probe)
    collector.start()
    assert probe.entered[0].wait(4.0), "collector never sampled"

    started = time.monotonic()
    stopped = app_main.stop_collector(collector, 200)
    elapsed = time.monotonic() - started

    # The deadline first, because it is the diagnostic: a wait without one
    # stalls the run instead of failing it, and an assert on `stopped` is only
    # reached long after the reader wanted to know why.
    assert elapsed < 2.0, "shutdown waited on a sample that was never coming back"
    # A sample blocked inside psutil cannot be cancelled, so the wait has to
    # have a deadline rather than the collector's.
    assert stopped is False

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


def test_shutdown_twice_leaves_one_of_everything(shutting_down):
    app, saved, application = shutting_down

    started = time.monotonic()
    app.shutdown()
    first = time.monotonic() - started
    started = time.monotonic()
    app.shutdown()
    second = time.monotonic() - started

    # Middle-click, the tray's Quit and an external quit can all arrive
    # together. The second call has to return at once rather than wait out
    # another timeout, and it must not do any of the work again.
    assert second < 1.0, f"the second shutdown took {second:.3f}s"
    assert first < 1.0, f"the first shutdown took {first:.3f}s"
    assert application.quits == 1, "the app asked to quit twice"
    assert saved == [asdict(app.settings)], "settings written more than once"
    assert app.tray.hides == 1, "the tray was hidden more than once"
    assert app.timer.stops == 1, "the tick timer was stopped more than once"
    assert app.collector.isRunning() is False, "shutdown returned with the sampler still running"


def test_the_second_shutdown_does_not_wait_on_the_sampler(shutting_down):
    app, _, application = shutting_down
    app.shutdown()

    # A sampler that will not stop, put back after the fact: the first call
    # joined the real one, so anything this second call does to a collector is
    # work it had no reason to repeat.
    stubborn = StubbornCollector()
    app.collector = stubborn

    started = time.monotonic()
    app.shutdown()
    elapsed = time.monotonic() - started

    assert not stubborn.waited, (
        f"the second shutdown waited {stubborn.waited} ms on a sampler it had "
        "already stopped"
    )
    assert elapsed < 1.0, f"the second shutdown took {elapsed:.3f}s"
    assert application.quits == 1


def test_shutdown_runs_once_from_the_panel_and_the_quit_hook(qapp, shutting_down):
    app, saved, application = shutting_down
    # The two wiring points main() and __init__ set up.
    app.panel.quit_requested.connect(app.shutdown)
    qapp.aboutToQuit.connect(app.shutdown)
    try:
        # Middle-click. shutdown() asks to quit, and StubApplication.quit()
        # emits the hook on its way out -- so this one gesture already
        # re-enters shutdown() from aboutToQuit while the first call is still
        # on the stack. That is the case the _stopped flag exists for.
        app.panel.quit_requested.emit()

        assert application.quits == 1, "the panel's quit request never reached shutdown"
        assert app.collector.isRunning() is False

        # A logoff or a session end arriving afterwards has nothing to do.
        qapp.aboutToQuit.emit()

        assert application.quits == 1, "the quit hook ran the shutdown a second time"
        assert saved == [asdict(app.settings)], "settings written more than once"
        assert app.tray.hides == 1
        assert app.timer.stops == 1
    finally:
        qapp.aboutToQuit.disconnect(app.shutdown)
        app.panel.quit_requested.disconnect(app.shutdown)


# --- the tray / menu settings ---------------------------------------------


def test_the_history_append_happens_on_the_collector_thread(
    qapp, tmp_path, monkeypatch, collectors
):
    """The CSV trace is a file write on every tick, so it cannot be queued.

    `sampled` is a queued connection, which means its slots run on the GUI
    thread -- the thread this application exists to keep free. The append rides
    the collector's own callback instead, next to the sample that caused it.
    """
    app = bare_app(tmp_path)
    log = RecordingLog()
    app.history_log = log
    monkeypatch.setattr(app_main, "SystemProbe", RecordingProbe)

    collector = collectors(app._make_collector())
    collector.start()

    assert wait_until(lambda: log.write_threads, 4.0), "the collector never wrote a row"
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True

    assert log.write_threads[0] is collector, (
        f"history appended on {log.write_threads[0]!r}: "
        "a queued slot runs on the GUI thread"
    )
    assert log.snapshots[0].cpu_pct == 1.0, "the wrong snapshot reached the trace"


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


def test_the_opacity_menu_shows_the_alpha_the_wheel_left_behind(qapp, tmp_path):
    """One wheel notch must not leave the menu claiming nothing is set.

    The wheel moves in theme.WHEEL_ALPHA_STEPs and the menu's labels are further
    apart than one step, so a notch lands between two of them. With nothing in
    the submenu to say otherwise, every checkmark went false and the menu read
    as "no opacity chosen" while the panel was showing one.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    opacity = next(action.menu() for action in menu.actions() if action.menu() is not None)

    def checked():
        return [action.text() for action in opacity.actions() if action.isChecked()]

    app._sync_menu()
    assert checked() == ["80%"], "the alpha the app starts with is not in the menu"

    # A real notch rather than a hand-set number: the wheel is what produces an
    # alpha no label names.
    app.panel.wheelEvent(wheel_notch(120))
    assert app.panel.current_alpha() == pytest.approx(
        theme.DEFAULT_ALPHA + theme.WHEEL_ALPHA_STEP
    )
    assert app.settings.alpha == app.panel.current_alpha(), "alpha has two sources"

    app._sync_menu()
    assert checked() == ["Custom (85%)"], (
        f"nothing in the opacity menu says the panel is at {app.settings.alpha}"
    )
    assert app._alpha_custom.isVisible()

    app._set_alpha(0.65)
    app._sync_menu()
    assert checked() == ["65%"], "the custom row outlived the alpha that needed it"
    assert not app._alpha_custom.isVisible(), "a row for an alpha nobody chose"
    # And the reading behind the new opacity is refreshed, as every menu action
    # that changes something does.
    assert app.collector.pokes == 1


def test_the_menu_re_checks_itself_however_it_was_opened(qapp, tmp_path):
    """Right-clicking the tray icon shows the menu without going through
    _show_menu_at(), so the re-check has to live on the menu itself.

    A menu synced on one of its two ways in is a menu that lies on the other:
    the wheel moves the alpha without coming near the panel's own menu, and a
    logoff or a session end reaches shutdown() without coming near it either.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    app.panel.wheelEvent(wheel_notch(120))

    menu.aboutToShow.emit()

    assert app._alpha_custom.isVisible(), "the menu kept the checkmarks it was built with"
    assert app._alpha_custom.isChecked()


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


def test_the_dwm_call_declares_its_prototype():
    fn = app_main._dwm_set_window_attribute()
    # ctypes guesses no argument types at all, and this only works at all
    # because wintypes.HWND happens to subclass c_void_p. A handle passed
    # through an undeclared prototype is a number the marshaller has to guess
    # the width of: on 64-bit Windows a wrong guess truncates the handle and
    # the call fails for a reason nobody reading the code can find.
    assert fn.argtypes is not None and len(fn.argtypes) == 4
    assert fn.restype is ctypes.c_long, "an HRESULT is a 32-bit signed value"


def test_enable_acrylic_refuses_handles_that_are_not_windows():
    # 1, -1 (0xFFFFFFFFFFFFFFFF) and 0x7FFFFFFF are the shapes a stale or
    # already-destroyed handle has. Declaring the prototype is new code on the
    # path that builds the argument, so it is new ways to raise.
    for hwnd in (1, -1, 0x7FFFFFFF, 0xFFFFFFFFFFFFFFFF):
        assert app_main.enable_acrylic(hwnd) is False


def test_enable_acrylic_survives_a_windows_without_the_dll(monkeypatch):
    # No dwmapi.dll, or anything else that makes the lookup fail: the widget has
    # to stay on its translucent fill rather than take the tray menu down with
    # it, which is the whole reason this returns False instead of raising.
    monkeypatch.delattr(app_main.ctypes, "windll", raising=False)
    assert app_main.enable_acrylic(0x1234) is False


def test_tray_icon_is_not_null(qapp):
    icon = app_main.build_tray_icon()
    assert not icon.isNull()
