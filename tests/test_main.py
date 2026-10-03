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

import ast
import builtins
import ctypes
import json
import logging
import logging.handlers
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt, QThread
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication

import main as app_main
import metrics
import theme
from history import HistoryLog
from metrics import Snapshot
from overlay import MonitorPanel
from settings import Settings, load_settings


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

    build() takes a *factory* -- something Collector calls on its own thread to
    get a probe -- or takes a Collector the test assembled itself and adopts
    that one. A Collector passed as a factory would be sampled for as if it had
    a sample() method, and the AttributeError that raises inside run() is a
    qFatal, which takes the run with it instead of reporting. Either way the
    thread is joined at teardown, because a QThread still running when its
    Python wrapper is collected aborts the interpreter, and a red test is
    exactly when one is most likely to be left behind.

    The factory is the whole point of the signature rather than an
    inconvenience: Collector has to build the probe on the thread that queries
    it, because SystemProbe opens a COM connection as it is constructed and a
    COM connection belongs to the thread that opened it.
    """
    made = []

    def build(make_probe):
        collector = (
            make_probe
            if isinstance(make_probe, app_main.Collector)
            else app_main.Collector(make_probe)
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
    app._scale_actions = {}
    app._toggle_actions = {}
    return app


def submenu(menu, title):
    """The submenu with this title, or a failure that says what is there."""
    for action in menu.actions():
        if action.menu() is not None and action.text() == title:
            return action.menu()
    raise AssertionError(
        f"the menu has no {title!r} submenu; it has "
        f"{[a.text() for a in menu.actions()]}"
    )


def checked_labels(menu):
    return [action.text() for action in menu.actions() if action.isChecked()]


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
    app._topmost_timer = StubTimer()
    app.collector = collectors(RecordingProbe)
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


# --- where the WMI connection is opened ------------------------------------


class NoNvidia:
    """pynvml as a machine with no NVIDIA driver presents it."""

    def nvmlInit(self):
        raise RuntimeError("no NVIDIA driver")


class QueryingProbe(metrics.SystemProbe):
    """The real probe, with its GPU source forced onto the WMI path.

    The GPU fallback is the only WMI caller left, and on a machine with an NVIDIA
    card it is never taken -- so a probe built normally would never reach WMI at
    all and this test would pass having measured nothing. GpuProbe is therefore
    built with an NVML stub that refuses to start, which is what puts it on the
    fallback, and metrics._wmi_video_controllers() is wrapped rather than
    replaced so the app still answers from the real WMI service while every call
    is recorded.

    The raw query in sample() is deliberately unguarded and outside the wrapper.
    metrics._wmi_video_controllers() catches everything and returns [], so a
    cross-apartment failure is otherwise invisible: the row reads `--` and
    nothing says why. Two separate facts are recorded because both are needed --
    "the call happened here" alone would pass with the wrong apartment, and "the
    query did not raise" alone would pass if the query never ran.
    """

    built_on: list = []
    queried_on: list = []
    outcomes: list = []
    callers: list = []

    def __init__(self):
        super().__init__(gpu=metrics.GpuProbe(nvml=NoNvidia()))
        QueryingProbe.built_on.append(QThread.currentThread())

    def sample(self):
        QueryingProbe.queried_on.append(QThread.currentThread())
        try:
            # The same unprojected fetch the fallback makes, uncaught.
            import wmi

            list(wmi.WMI().Win32_VideoController())
        except Exception as exc:  # noqa: BLE001 - the point is that it raised
            QueryingProbe.outcomes.append(f"{type(exc).__name__}: {exc}")
        else:
            QueryingProbe.outcomes.append(None)
        return super().sample()

    @classmethod
    def reset(cls):
        cls.built_on, cls.queried_on, cls.outcomes, cls.callers = [], [], [], []


@pytest.fixture
def querying_probe(qapp, tmp_path, monkeypatch, collectors):
    """QueryingProbe behind main.py's own wiring, torn down after.

    The probe is the real one on purpose: every WMI test in test_metrics.py
    monkeypatches `_wmi_video_controllers`, so nothing else in this repository
    can tell a connection opened on the wrong thread from one opened on the
    right one.
    """
    QueryingProbe.reset()
    real_controllers = metrics._wmi_video_controllers

    def recording_controllers():
        QueryingProbe.callers.append(QThread.currentThread())
        return real_controllers()

    monkeypatch.setattr(metrics, "_wmi_video_controllers", recording_controllers)
    monkeypatch.setattr(app_main, "SystemProbe", QueryingProbe)
    app = bare_app(tmp_path)
    collector = collectors(app._make_collector())
    yield QueryingProbe, collector
    QueryingProbe.reset()


def test_the_wmi_connection_is_opened_on_the_thread_that_queries_it(
    qapp, querying_probe
):
    """The first query of every launch used to raise, and nothing noticed.

    main.py built SystemProbe on the GUI thread, which opened a wmi.WMI() COM
    connection there, and then every query ran on the collector's thread. COM
    refuses that call: WMI's object is an apartment-threaded proxy, and using it
    from a thread it was not created on fails with RPC_E_WRONG_THREAD
    (0x8001010E). The exception was logged at INFO on a logger main.py pins to
    WARNING, and the row read `--`.

    Measured before this test existed: tick 0 raised on every single launch.

    Two assertions, and the first is the one that would catch a regression now.
    Every call to metrics' WMI entry point has to come from the collector,
    because that is what both opens the connection and queries it; a second
    caller is a second connection, opened on whoever's thread it happened to be.
    The second says the collector's apartment can actually answer -- which it
    cannot if the CoInitializeEx in run() is removed, whatever thread the call
    comes from.
    """
    probe, collector = querying_probe
    collector.start()

    assert wait_until(lambda: probe.outcomes and probe.callers, 10.0), (
        "the collector never reached WMI: on a machine with no NVIDIA card the "
        "panel would show dashes with no attempt to measure anything"
    )
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(5000) is True

    assert set(probe.callers) == {collector}, (
        f"WMI was reached from {probe.callers} and not only from the collector: "
        "a connection opened on the GUI thread and queried on this one is a "
        "cross-apartment call"
    )
    assert probe.outcomes[0] is None, (
        f"the first WMI query on the collector's thread raised {probe.outcomes[0]}: "
        "the connection was opened somewhere else, or the collector's apartment "
        "was never initialised"
    )
    # The cache means one call per WMI_FALLBACK_REFRESH_S, not one per tick, so
    # this is the whole of it and it is not a single tick that got lucky.
    assert len(probe.callers) == 1, (
        f"{len(probe.callers)} WMI calls in one tick: the no-NVIDIA path opens a "
        "COM connection and re-reads Win32_VideoController every two seconds, "
        "which is the 44.6 ms a query costs times thirty a minute"
    )


def test_the_probe_itself_is_built_on_the_collector_thread(qapp, querying_probe):
    """The factory is what puts the probe on the thread that will query it.

    Initialising COM on the querying thread does *not* rescue a connection
    opened elsewhere -- see the characterisation test below -- so "the query no
    longer raises" on its own would be satisfied by anything that merely
    initialised COM. This pins the actual fix: the object that will reach WMI is
    constructed on the thread that will use it, so there is no connection for the
    GUI thread to have opened in the first place. `import wmi` runs GetObject at
    module scope, so a probe built here would bind one to this thread for the
    life of the process whatever later ran.
    """
    probe, collector = querying_probe
    collector.start()

    assert wait_until(lambda: probe.built_on, 10.0), "the collector never built a probe"
    built_on = probe.built_on[0]
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(5000) is True

    assert built_on is collector, (
        f"the probe was built on {built_on!r}, not the collector's thread: a COM "
        "connection opened on the GUI thread belongs to the GUI thread, and every "
        "later query against it is a cross-apartment call"
    )
    assert probe.queried_on[0] is collector, "the query did not run on the collector's thread"


class RecordingCom:
    """A stand-in for pythoncom that writes down what it was asked to do.

    Real CoInitializeEx/CoUninitialize cannot be observed from here, and the
    ordering is the part worth pinning: the probe has to go before the last
    CoUninitialize, or pywin32 releases it against an apartment that is already
    gone and writes "Win32 exception occurred releasing IUnknown" to stderr.
    """

    COINIT_APARTMENTTHREADED = 2

    def __init__(self):
        self.inits = []
        self.uninits = 0

    def CoInitializeEx(self, flag):
        self.inits.append(flag)

    def CoUninitialize(self):
        self.uninits += 1


def test_the_sampler_initialises_and_releases_com_on_its_own_thread(
    qapp, monkeypatch, collectors
):
    """COM in, COM out, exactly once, on the thread that uses it.

    Two separate obligations that one missing line each would break, and both
    are silent when broken: an uninitialised worker thread is what made
    `import wmi` fail outright off the GUI thread (wmi.py runs GetObject at
    module scope), and an apartment that is never uninitialised leaks one per
    launch on a thread that then ends.
    """
    import pythoncom

    recorder = RecordingCom()
    monkeypatch.setattr(app_main, "pythoncom", recorder)

    collector = collectors(RecordingProbe)
    collector.start()
    assert wait_until(lambda: collector.isRunning() and collector._probe is not None, 4.0), (
        "the collector never built its probe"
    )
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True
    # wait() returning only means run() is unwinding; the finally has to have
    # run before the counts can be believed.
    assert wait_until(lambda: recorder.uninits, 2.0)

    assert recorder.inits == [pythoncom.COINIT_APARTMENTTHREADED], (
        f"the sampler initialised COM {len(recorder.inits)} time(s) with "
        f"{recorder.inits}, not once with COINIT_APARTMENTTHREADED"
    )
    assert recorder.uninits == 1, (
        f"CoUninitialize ran {recorder.uninits} times against one "
        "CoInitializeEx: a thread that ends without matching its init leaks the "
        "apartment for the life of the process"
    )


def test_the_probe_is_released_before_com_goes_away(qapp, monkeypatch, collectors):
    """The ordering, because getting it wrong is only visible on stderr.

    A COM object released after its apartment is gone makes pywin32 write
    "Win32 exception occurred releasing IUnknown" to stderr on every shutdown.
    A frame-local cannot prevent it -- the frame is popped only after run()
    returns -- so the probe is held on the instance and dropped in the finally,
    ahead of the CoUninitialize.
    """
    seen = {}

    class WatchingProbe:
        def sample(self):
            return Snapshot(cpu_pct=1.0)

    class PeekingCom:
        COINIT_APARTMENTTHREADED = 2

        def CoInitializeEx(self, flag):
            pass

        def CoUninitialize(self):
            seen["probe_at_uninit"] = holder[0]._probe

    holder = []
    monkeypatch.setattr(app_main, "pythoncom", PeekingCom())

    collector = collectors(WatchingProbe)
    holder.append(collector)
    collector.start()
    assert wait_until(lambda: collector._probe is not None, 4.0)
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True
    assert wait_until(lambda: "probe_at_uninit" in seen, 2.0)

    assert seen["probe_at_uninit"] is None, (
        "the WMI connection was still held when the apartment was torn down"
    )


def test_a_probe_factory_that_raises_is_logged_and_the_run_returns(
    qapp, monkeypatch, caplog, collectors
):
    """The escape hatch, because an exception out of run() is fatal to the process.

    Building the probe here rather than on the GUI thread is what fixed the
    cross-apartment WMI fault, and it moved `import psutil` under main()'s
    try/except no more: it is unguarded, and an exception escaping a QThread
    virtual cannot be caught from the outside. Measured before the fix: the
    process died with -1073740791 (0xC0000409) and app_debug.log held **zero**
    records, so a missing dependency at startup killed the app with no
    explanation at all -- strictly worse than the failure the factory fixed.

    run() is called here rather than started on a thread, and that is the point:
    it makes a regression a failed test instead of a dead interpreter, the same
    reason tests/test_overlay.py drives paintEvent(None) for the paint-failure
    floor. The subprocess test below is what pins the real thread.
    """
    def refuse():
        raise ImportError("No module named 'psutil'")

    import pythoncom

    recorder = RecordingCom()
    monkeypatch.setattr(app_main, "pythoncom", recorder)
    collector = collectors(refuse)

    with caplog.at_level(logging.ERROR, logger="widget.main"):
        collector.run()

    assert recorder.inits == [pythoncom.COINIT_APARTMENTTHREADED], (
        f"COM was initialised {recorder.inits} before the factory ran"
    )
    assert recorder.uninits == 1, (
        "the COM pairing was not kept when the factory raised: the apartment is "
        f"left initialised ({recorder.inits} init(s), {recorder.uninits} uninit(s))"
    )
    records = [r for r in caplog.records if r.name == "widget.main"]
    assert len(records) == 1, (
        f"{len(records)} records for a probe factory that raised: the fault "
        "escapes the thread with nothing written down, so there is no trace to "
        "work from and no exit code to explain"
    )
    assert records[0].exc_info is not None, (
        "the record carries no traceback: a missing dependency is exactly the "
        "case where the stack is the answer"
    )
    # caplog.text, not getMessage(): logger.exception puts the fault in the
    # traceback, and the traceback is what a reader of the log file gets.
    assert "psutil" in caplog.text, (
        f"the record does not name the fault: {caplog.text!r}"
    )


def test_a_sample_that_raises_stops_the_loop_and_is_logged(qapp, monkeypatch, caplog, collectors):
    """The same guard on the loop, where the exception can arrive every tick.

    SystemProbe catches its own per-source faults, so what reaches run() is the
    unexpected one: a probe built for the app going wrong in a way no guard
    covers. The panel then goes stale -- the status dot goes neutral, which is
    the signal for exactly this -- and the process stays up with the reason on
    file. Also pins that the probe is released on this path too, since the
    finally has to cover the loop as well as the construction.
    """
    class Exploding:
        def sample(self):
            raise RuntimeError("the probe fell over")

    seen = {}

    class PeekingCom:
        COINIT_APARTMENTTHREADED = 2

        def CoInitializeEx(self, flag):
            pass

        def CoUninitialize(self):
            seen["probe_at_uninit"] = collector._probe

    monkeypatch.setattr(app_main, "pythoncom", PeekingCom())
    collector = collectors(Exploding)

    with caplog.at_level(logging.ERROR, logger="widget.main"):
        collector.run()

    assert seen.get("probe_at_uninit") is None, (
        "the probe was still held when the apartment was torn down"
    )
    messages = [r.getMessage() for r in caplog.records if r.name == "widget.main"]
    assert any("sampler thread stopped" in m for m in messages), (
        f"the fault that ended the sampler was not logged at all: {messages!r}"
    )
    assert "fell over" in caplog.text, (
        f"the record does not carry the exception that ended the thread: {caplog.text!r}"
    )


def test_an_exception_out_of_the_sampler_thread_leaves_the_process_running(tmp_path):
    """The claim itself, on the real thread, because the one above cannot see it.

    An exception out of QThread.run() is fatal: the process exits with
    -1073740791 (0xC0000409) and, measured on this machine, writes nothing to
    app_debug.log at all. So this runs a child process whose probe factory
    raises, and asks two questions of it -- what was the exit code, and what
    reached the log.
    """
    log = tmp_path / "app_debug.log"
    root = Path(app_main.__file__).parent
    script = (
        "import logging.handlers, sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "handler = logging.handlers.RotatingFileHandler(\n"
        f"    {str(log)!r}, maxBytes=512 * 1024, backupCount=2, encoding='utf-8'\n"
        ")\n"
        "handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))\n"
        "logging.getLogger().addHandler(handler)\n"
        "logging.getLogger().setLevel(logging.INFO)\n"
        "from PyQt6.QtCore import QCoreApplication, QThread\n"
        "import main as app_main\n"
        "def refuse():\n"
        "    raise ImportError(\"No module named 'psutil'\")\n"
        "app = QCoreApplication(sys.argv)\n"
        "collector = app_main.Collector(refuse)\n"
        "collector.finished.connect(app.quit)\n"
        "collector.start()\n"
        "collector.wait(30000)\n"
        "logging.shutdown()\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180
    )
    written = log.read_text(encoding="utf-8") if log.exists() else ""

    assert done.returncode == 0, (
        f"a probe factory that raised took the process down with it: exit "
        f"{done.returncode}, log {len(written)} bytes (stderr: {done.stderr[-300:]})"
    )
    assert "psutil" in written, (
        f"the process survived but nothing was written down, so the log is no "
        f"better than the crash: {written!r}"
    )


def test_com_init_on_the_querying_thread_does_not_fix_a_foreign_connection():
    """The alternative fix, measured rather than assumed: it does not work.

    `CoInitializeEx(COINIT_APARTMENTTHREADED)` at the top of Collector.run()
    looks like the textbook answer -- initialise COM where COM is used -- and it
    is not. A `wmi.WMI()` connection carries an apartment-threaded proxy created
    on the thread that called WMI(); initialising the *querying* thread puts it
    in an apartment, and the proxy still belongs to the other one. Both
    apartment models were measured on this machine and both fail identically
    with RPC_E_WRONG_THREAD (0x8001010E):

        STA: x_wmi on Win32_VideoController
        MTA: x_wmi on Win32_VideoController

    So the only correct fix is to open the connection where it is queried, which
    is why Collector takes a factory rather than a probe.

    Run in a subprocess because the failure is not a tidy one: pywin32 turns
    RPC_E_WRONG_THREAD into an unhandled structured exception on a worker thread
    with no message filter, which under pytest's faulthandler prints
    "Windows fatal exception: code 0x8001010e" and takes the whole run with it.
    A child process reports the same thing without owning the test session.
    """
    script = (
        "import threading, sys\n"
        "import pythoncom, wmi\n"
        "conn = wmi.WMI()\n"
        "for flag in (pythoncom.COINIT_APARTMENTTHREADED,\n"
        "             pythoncom.COINIT_MULTITHREADED):\n"
        "    box = {}\n"
        "    def run(box=box, flag=flag):\n"
        "        pythoncom.CoInitializeEx(flag)\n"
        "        try:\n"
        "            list(conn.query('SELECT Name FROM Win32_VideoController'))\n"
        "            box['r'] = 'no error'\n"
        "        except Exception as exc:\n"
        "            box['r'] = type(exc).__name__\n"
        "        finally:\n"
        "            pythoncom.CoUninitialize()\n"
        "    t = threading.Thread(target=run); t.start(); t.join(30)\n"
        "    print(box.get('r'))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180
    )
    results = [line.strip() for line in done.stdout.splitlines() if line.strip()]

    assert results == ["x_wmi", "x_wmi"], (
        f"a cross-thread query did not fail under both apartment models: "
        f"{results} (stderr: {done.stderr[-400:]}). If CoInitializeEx ever does "
        "fix it, the factory in Collector can be replaced by a CoInitializeEx "
        "call in run()."
    )


# --- the sampler thread ---------------------------------------------------


def test_sampling_runs_on_the_collector_thread(qapp, collectors):
    probe = RecordingProbe()
    collector = collectors(lambda: probe)
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
    collector = collectors(lambda: probe)
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
    collector = collectors(RecordingProbe)
    collector.start()
    qapp.processEvents()
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True


def test_collector_stops_promptly_on_interruption(qapp, collectors):
    probe = BlockingProbe()
    collector = collectors(lambda: probe)
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
    collector = collectors(lambda: probe)
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
    collector = collectors(lambda: probe)
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
    collector = collectors(lambda: probe)
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


def test_the_exit_is_written_down(shutting_down, caplog):
    """The log has to be able to tell a clean exit from an abrupt one.

    Startup is recorded and a failure to start is recorded with its traceback,
    but the way out wrote nothing at all: every run of this file ended with the
    last line reading "MonitorApp starting", whether the user quit from the tray
    or the process was killed where it stood. So the log could not answer the
    first question anyone asks of it after the panel disappears -- did it quit,
    or did it die -- and it could not be told apart from a log that had simply
    stopped being written to.

    One INFO line closes that. It is not a heartbeat and not a summary: the
    whole point is that its absence is meaningful.
    """
    app, _, _ = shutting_down

    with caplog.at_level(logging.INFO, logger="widget.main"):
        app.shutdown()

    records = [r for r in caplog.records if r.name == "widget.main"]
    assert records, (
        "shutdown() wrote nothing: a log whose last line is the startup one "
        "cannot say whether the app quit or died"
    )
    assert any("exiting" in r.getMessage() for r in records), (
        "no record names the exit: %r"
        % [r.getMessage() for r in records]
    )


def test_only_one_exit_record_for_two_shutdowns(shutting_down, caplog):
    """The second shutdown must not write a second exit line.

    shutdown() is reached from the panel, the tray and the aboutToQuit hook, and
    the guard that keeps it to one pass over the timers and the settings file
    has to cover the log the same way -- otherwise "one line per run" is not a
    number anything can rely on.
    """
    app, _, _ = shutting_down

    with caplog.at_level(logging.INFO, logger="widget.main"):
        app.shutdown()
        app.shutdown()

    exits = [r for r in caplog.records
             if r.name == "widget.main" and "exiting" in r.getMessage()]
    assert len(exits) == 1, f"two shutdowns wrote {len(exits)} exit records: {exits!r}"


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
    # The z-order timer too, not just the tick timer. Both are parented to the
    # panel so Qt stops them when the widget goes -- but shutdown() quits the
    # event loop rather than destroying the panel, and QApplication.quit() does
    # not close the window. A timer left running through quit() wakes the
    # process once a second until the interpreter tears down the event loop, and
    # the sequence is short enough that nothing observable goes wrong: a widget
    # installed to be left running, outliving the app that installed it.
    assert app._topmost_timer.stops == 1, (
        "the topmost re-assertion timer was not stopped: it is still armed for "
        f"{app_main.TOPMOST_REASSERT_MS} ms intervals after shutdown"
    )
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
    opacity = submenu(menu, "Opacity")

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


# --- the Scale submenu ------------------------------------------------------


def test_the_scale_submenu_offers_exactly_the_scales_the_panel_has(qapp, tmp_path):
    """Three rows, and the labels are derived from the same tuple the loader reads.

    theme.SCALE_STEPS is the one place a scale is listed, and the submenu's rows
    and settings.py's validator both read it. Asserted against the tuple rather
    than against a written-out list, so adding a step to theme cannot leave the
    menu offering something the loader refuses -- or offering three rows when
    four are on offer -- without failing here first. The labels are compared as
    a derivation too: what has to hold is that each row is named the way the
    Opacity submenu names its alphas, not that a percentage sign appears.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    scale_menu = submenu(menu, "Scale")

    rows = [(action.text(), app_main.scale_action_value(action))
            for action in scale_menu.actions()]

    assert [value for _, value in rows] == list(theme.SCALE_STEPS), (
        f"the Scale submenu offers {[v for _, v in rows]} against the panel's "
        f"{list(theme.SCALE_STEPS)}"
    )
    assert [label for label, _ in rows] == [
        f"{round(value * 100)}%" for value in theme.SCALE_STEPS
    ], [label for label, _ in rows]
    # No Custom row, and nothing above 1.00: scales larger than the current size
    # were offered once and declined, and no gesture in the interface can land on
    # a value between two steps.
    assert len(scale_menu.actions()) == len(theme.SCALE_STEPS), (
        f"the Scale submenu has {len(scale_menu.actions())} rows for "
        f"{len(theme.SCALE_STEPS)} scales: a row for a value the panel cannot take"
    )
    assert max(theme.SCALE_STEPS) <= 1.0


def test_picking_a_scale_resizes_the_panel_and_persists_it(qapp, tmp_path):
    """The row is wired to the panel and to the setting, not just to itself.

    Driven through the action rather than through _set_scale, because what a
    click has to do is the whole claim: a submenu whose rows are correct labels
    over handlers that do nothing would satisfy every other test in this file.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    scale_menu = submenu(menu, "Scale")
    before = app.panel.size()

    wanted = theme.SCALE_STEPS[0]
    row = next(action for action in scale_menu.actions()
               if app_main.scale_action_value(action) == wanted)
    row.trigger()

    assert app.panel.current_scale() == wanted
    assert app.settings.scale == wanted, "the scale has two sources"
    layout = theme.Layout(wanted)
    assert (app.panel.width(), app.panel.height()) == (layout.canvas_w, layout.canvas_h)
    assert app.panel.size() != before


def test_the_scale_menu_re_checks_itself_whenever_it_was_opened(qapp, tmp_path):
    """The checkmark follows the live value, not the value at startup.

    Emitted through aboutToShow rather than by calling _sync_menu(), because the
    menu is reached two ways -- the panel's own context menu and a right-click on
    the tray icon -- and the second never goes through _show_menu_at(). A menu
    synced on one of them lies on the other.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    scale_menu = submenu(menu, "Scale")

    menu.aboutToShow.emit()
    assert checked_labels(scale_menu) == ["100%"], (
        f"the panel starts at {theme.DEFAULT_SCALE} and the menu says "
        f"{checked_labels(scale_menu)}"
    )

    app._set_scale(theme.SCALE_STEPS[0])
    menu.aboutToShow.emit()
    assert checked_labels(scale_menu) == ["75%"], (
        f"the panel is at {theme.SCALE_STEPS[0]} and the menu still says "
        f"{checked_labels(scale_menu)}"
    )

    # Both directions: a row that cleared the others without ever setting itself
    # would pass the first half.
    app._set_scale(theme.DEFAULT_SCALE)
    menu.aboutToShow.emit()
    assert checked_labels(scale_menu) == ["100%"], (
        f"coming back up did not move the checkmark: {checked_labels(scale_menu)}"
    )


def test_exactly_one_scale_is_ever_checked(qapp, tmp_path):
    """The three rows are exhaustive, so "none checked" is unreachable.

    This is what the membership validator buys. Alpha can sit between two
    labels -- the wheel puts it there -- and needed a Custom row to say so.
    Scale cannot, so if two rows could be checked, or none, the submenu would be
    describing a state of the panel that the panel cannot be in, and the value
    behind that state is one the loader refuses on the next start. Driven from
    every scale the loader accepts, through a full menu sync.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    scale_menu = submenu(menu, "Scale")

    for scale in theme.SCALE_STEPS:
        app.settings.scale = scale
        app._sync_menu()
        marked = checked_labels(scale_menu)
        assert marked == [f"{round(scale * 100)}%"], (
            f"at scale {scale} the submenu has {marked} checked, which is not "
            "exactly the one row for the scale the panel is at"
        )


def test_every_scale_the_loader_accepts_has_a_row_and_the_reverse(qapp, tmp_path):
    """Two readers of one tuple, neither of them wider than the other.

    A step offered but not loadable would be a row that silently reverts on the
    next start; a loadable value with no row would be a setting the user cannot
    undo from the menu. Both are silent, and both would be visible only from
    outside -- hence loading a file for each candidate rather than asking the
    validator what it thinks.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    offered = [app_main.scale_action_value(action)
               for action in submenu(menu, "Scale").actions()]

    for value in (0.5, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0, 1.2):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"scale": value}), encoding="utf-8")
        accepted = load_settings(path).scale == value

        assert accepted == (value in theme.SCALE_STEPS), (
            f"the loader accepted {value}: {accepted}, against the panel's "
            f"{list(theme.SCALE_STEPS)}"
        )
        assert (value in offered) == accepted, (
            f"{value} is offered by the menu: {value in offered}, and accepted "
            f"by the loader: {accepted}"
        )


def test_a_scale_change_refreshes_the_reading_behind_the_panel(qapp, tmp_path):
    """Every menu action that changes something pokes the sampler.

    The rule the opacity rows already follow: the numbers on screen are two
    seconds old at worst when the menu opens, and a panel the user has just
    resized is not the moment to show them a stale reading.
    """
    app = menu_app(tmp_path)
    app._build_menu()

    app._set_scale(theme.SCALE_STEPS[0])

    assert app.collector.pokes == 1, "the scale changed without refreshing the panel"


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


def test_tray_icon_is_not_null(qapp):
    icon = app_main.build_tray_icon()
    assert not icon.isNull()


def test_the_tray_menu_offers_no_always_on_top_toggle(qapp, tmp_path):
    """The panel is above other windows unconditionally, so there is no switch.

    It used to be a checkable row whose untick removed a flag that was not what
    was keeping the panel visible: a Qt.Tool window is topmost by design, so
    nothing on screen changed and the row looked broken. The window's z-order is
    not a preference, and the flag is now stated once in overlay.py.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()

    labels = [action.text() for action in clickable_actions(menu)]
    assert not [text for text in labels if "top" in text.lower()], (
        f"the menu still offers an always-on-top toggle: {labels}"
    )


# --- holding the top of the z-order ---------------------------------------


class RecordingUser32:
    """A user32 that answers SetWindowPos and writes down how it was asked.

    A function, not an object with a SetWindowPos attribute, because that is the
    shape ctypes hands back and `_user32_set_window_pos()` is declared once and
    cached -- so the app and the test agree about which object is the API.

    `succeeds=False` returns FALSE, which is what Windows does when the call is
    refused: the archetypal case is a game running elevated, where UIPI blocks a
    lower-integrity process from changing a higher-integrity window's z-order.
    """

    def __init__(self, succeeds=True):
        self.calls = []
        self.succeeds = succeeds

    def __call__(self, hwnd, insert_after, x, y, cx, cy, flags):
        self.calls.append((hwnd, insert_after, x, y, cx, cy, flags))
        return 1 if self.succeeds else 0  # BOOL: zero is failure


class FakeClock:
    """A clock the test moves, because metrics' throttle reads the module's own.

    Callable as well as monotonic()/time(), because the module is replaced with
    SimpleNamespace(monotonic=clock, time=clock) -- the shape
    tests/test_metrics.py uses, where `clock` has to answer to all three.
    """

    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def visible_off_screen(panel):
    """An hwnd for a panel Qt calls visible, with nothing on the desktop.

    WA_DontShowOnScreen is Qt's own mechanism for this: isVisible() is true,
    which is the predicate the re-assertion's guard reads, and winId() hands
    back a real handle -- while IsWindowVisible stays false and no window
    appears. show() alone would break the rule this file states in its
    docstring, and stubbing isVisible() would test the stub rather than the
    guard.
    """
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    panel.show()
    QApplication.processEvents()
    assert panel.isVisible(), "the test fixture is not visible, so the guard is untested"
    return int(panel.winId())


@pytest.fixture
def topmost(qapp, tmp_path, monkeypatch):
    """A visible panel whose window API records instead of moving.

    Yields (app, recorder, hwnd) with the timer already built, which is what the
    tests below drive.
    """
    app = bare_app(tmp_path)
    recorder = RecordingUser32()
    monkeypatch.setattr(app_main, "_user32_set_window_pos", lambda: recorder)
    hwnd = visible_off_screen(app.panel)
    app._watch_topmost()
    return app, recorder, hwnd


def test_the_topmost_position_is_re_asserted_on_its_timer(qapp, topmost):
    """Games take the z-order on their way up, so the panel has to take it back.

    A topmost window flag is a statement made when the window is created, not a
    promise Windows keeps: a fullscreen switch, a launcher or another
    always-on-top overlay all put something above the panel afterwards. In
    borderless fullscreen -- an ordinary maximised window, and what most modern
    titles use -- the panel's window flags are enough on their own; this is the
    half that survives a game disturbing the z-order.

    Every flag is asserted rather than the call as a whole, because each one is a
    decision: NOMOVE and NOSIZE say the panel keeps its position and size,
    NOACTIVATE is what keeps the panel from stealing focus from the game, and the
    absence of SHOWWINDOW is what stops a hidden panel being put on screen.
    """
    app, recorder, hwnd = topmost
    assert app._topmost_timer.interval() == app_main.TOPMOST_REASSERT_MS
    assert app._topmost_timer.isActive(), "the re-assertion timer is not running"

    app._topmost_timer.timeout.emit()

    assert recorder.calls, "nothing re-asserted the topmost position"
    (hwnd_arg, insert_after, x, y, cx, cy, flags), = recorder.calls
    # Compared through .value because the app hands over real HWND objects, not
    # bare integers: wrapping the handle as a pointer is part of what is being
    # asserted, since that is what the declared prototype marshals.
    assert hwnd_arg.value == hwnd, f"asked about {hwnd_arg} rather than the panel's own handle"
    assert insert_after.value == wintypes.HWND(app_main.HWND_TOPMOST).value, (
        f"hWndInsertAfter was {insert_after}: only HWND_TOPMOST puts the window "
        "back on top, and HWND_TOP only moves it to the top of the "
        "non-topmost band"
    )
    assert (x, y, cx, cy) == (0, 0, 0, 0), (
        "the call was given a position or a size, and NOMOVE/NOSIZE mean they "
        "would be ignored -- so the panel would move if those flags were ever lost"
    )
    assert flags == app_main.SWP_NOMOVE | app_main.SWP_NOSIZE | app_main.SWP_NOACTIVATE
    assert flags == 0x0013, f"the flag word is 0x{flags:04x}"
    assert flags & app_main.SWP_NOACTIVATE, (
        "without SWP_NOACTIVATE the panel takes focus, which is exactly what a "
        "game must not lose"
    )
    assert not flags & app_main.SWP_SHOWWINDOW, (
        "SWP_SHOWWINDOW shows the window: it would put a hidden panel on screen"
    )
    assert not flags & app_main.SWP_NOZORDER, (
        "SWP_NOZORDER leaves the z-order alone, which would make the whole call "
        "a no-op that looks like it is working"
    )


def test_the_re_assertion_does_not_run_while_the_panel_is_hidden(qapp, topmost):
    """A panel nobody can see must not be put on screen by its own timer.

    Asserted as one call followed by none rather than as zero calls, so a timer
    that was never wired cannot pass it: the first half is the control.
    """
    app, recorder, _ = topmost

    app._topmost_timer.timeout.emit()
    assert len(recorder.calls) == 1, (
        "the control did not fire: the timer is not reaching the call at all, so "
        "the hidden case below would prove nothing"
    )

    app.panel.hide()
    QApplication.processEvents()
    assert app.panel.isVisible() is False

    app._topmost_timer.timeout.emit()

    assert len(recorder.calls) == 1, (
        f"{len(recorder.calls) - 1} call(s) after the panel was hidden: a hidden "
        "panel must not be moved, shown or focused"
    )


def test_a_re_assertion_that_keeps_failing_writes_one_record(monkeypatch, caplog):
    """This runs once a second, so an unthrottled log line is 86,400 records a day.

    The panel calls it every TOPMOST_REASSERT_MS for as long as it is on screen,
    and a machine where the call fails would fail it forever: at roughly 100 bytes
    a record that is megabytes of churn a day, rotating away everything else in
    app_debug.log and holding the logging lock once a second besides. So the
    failure goes through metrics' own throttle -- the rule the rest of the
    application already obeys -- rather than a second one invented here.

    600 calls is ten minutes of panel at this cadence.
    """
    def no_user32():
        raise AttributeError("module 'ctypes' has no attribute 'windll'")

    monkeypatch.setattr(app_main, "_user32_set_window_pos", no_user32)
    monkeypatch.setattr(metrics, "_FAULT_LOG", {})

    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(600):
            app_main.assert_topmost(0x1234)

    records = [r for r in caplog.records if "topmost" in r.getMessage()]
    assert len(records) == 1, (
        f"600 failing calls wrote {len(records)} records: at one call a second "
        "the log grows with uptime again"
    )
    # The traceback on the first one, because this is the record that has to name
    # the fault -- here a missing user32.
    assert records[0].exc_info is not None
    assert "windll" in caplog.text


def test_a_re_assertion_that_starts_working_clears_its_fault(monkeypatch, caplog):
    """A source that answers must say so, or the one record above is all there is.

    Nothing else can teach the throttle a fault is over: the healthy case is the
    one that does not call log_fault, so clear_fault on the successful call is the
    only way a *later* failure can earn a record. Without it a source that broke
    once during a driver install would be silent for the rest of the session.
    """
    failing = RecordingUser32()

    def no_user32():
        raise AttributeError("no windll")

    # metrics.log_fault measures the healthy spell against metrics.LOG_INTERVAL,
    # so the clock has to be one the test can move -- the same reason
    # tests/test_metrics.py installs a fake one.
    clock = FakeClock(1_000.0)
    monkeypatch.setattr(
        metrics, "time", SimpleNamespace(monotonic=clock, time=clock)
    )
    monkeypatch.setattr(app_main, "_user32_set_window_pos", no_user32)
    monkeypatch.setattr(metrics, "_FAULT_LOG", {})
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        app_main.assert_topmost(0x1234)
    assert [r for r in caplog.records if "topmost" in r.getMessage()]

    # It heals, and stays healed for longer than LOG_INTERVAL.
    monkeypatch.setattr(app_main, "_user32_set_window_pos", lambda: failing)
    for _ in range(200):
        assert app_main.assert_topmost(0x1234) is True
    clock.advance(metrics.LOG_INTERVAL + 1.0)

    caplog.clear()
    monkeypatch.setattr(app_main, "_user32_set_window_pos", no_user32)
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        app_main.assert_topmost(0x1234)

    records = [r for r in caplog.records if "topmost" in r.getMessage()]
    assert len(records) == 1, (
        "a failure after a spell of health was not announced: the throttle "
        "still believes the fault is the one it already wrote about"
    )


def test_a_refused_set_window_pos_is_reported_through_the_throttle(monkeypatch, caplog):
    """SetWindowPos returns FALSE, it does not raise -- and that was the answer
    the feature exists to notice.

    A game run elevated is the case this whole mechanism is for: UIPI blocks a
    lower-integrity process from changing a higher-integrity window's z-order, and
    Windows says so by returning FALSE. An exception never arrives, so the
    `except` path alone would have logged nothing and the panel would have been
    quietly buried under the game it was supposed to sit above, once a second,
    silently, for the length of the session.

    So FALSE goes through the same throttled path an exception does: one record
    for the first failure, and no record per second after that -- the timer runs
    at TOPMOST_REASSERT_MS and an unthrottled line here is 86,400 a day.
    """
    monkeypatch.setattr(
        app_main, "_user32_set_window_pos",
        lambda: RecordingUser32(succeeds=False),
    )
    monkeypatch.setattr(metrics, "_FAULT_LOG", {})

    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        for _ in range(600):   # ten minutes of panel at this cadence
            assert app_main.assert_topmost(0x1234) is False

    records = [r for r in caplog.records if "topmost" in r.getMessage()]
    assert len(records) == 1, (
        f"600 refused calls wrote {len(records)} records: a FALSE return is the "
        "same one event as a raise, and it has to be throttled the same way"
    )
    assert "did not move" in records[0].getMessage() or "refus" in records[0].getMessage(), (
        f"the record does not say the call was refused rather than raised: "
        f"{records[0].getMessage()!r}"
    )
    # Nothing raised, so there is no traceback to give: the summary has to carry
    # the whole record.
    assert records[0].exc_info is None


def test_a_refusal_clears_its_fault_when_the_call_works_again(monkeypatch, caplog):
    """The other half of the throttle: a refusal that heals must be re-announced.

    Without a clear_fault on the successful path the one record above is all the
    log will ever hold for this source, and a failure that came back after a long
    healthy spell would be invisible -- which is the case the panel shows, a
    panel that was buried and then was not.
    """
    clock = FakeClock(1_000.0)
    monkeypatch.setattr(
        metrics, "time", SimpleNamespace(monotonic=clock, time=clock)
    )
    monkeypatch.setattr(metrics, "_FAULT_LOG", {})
    monkeypatch.setattr(
        app_main, "_user32_set_window_pos", lambda: RecordingUser32(succeeds=False)
    )
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        app_main.assert_topmost(0x1234)
    assert [r for r in caplog.records if "topmost" in r.getMessage()]

    monkeypatch.setattr(
        app_main, "_user32_set_window_pos", lambda: RecordingUser32(succeeds=True)
    )
    for _ in range(200):
        assert app_main.assert_topmost(0x1234) is True
    clock.advance(metrics.LOG_INTERVAL + 1.0)

    caplog.clear()
    monkeypatch.setattr(
        app_main, "_user32_set_window_pos", lambda: RecordingUser32(succeeds=False)
    )
    with caplog.at_level(logging.WARNING, logger="widget.metrics"):
        app_main.assert_topmost(0x1234)

    records = [r for r in caplog.records if "topmost" in r.getMessage()]
    assert len(records) == 1, (
        "a refusal after a spell of health was not announced: the throttle "
        "still believes the fault is the one it already wrote about"
    )


def test_the_topmost_placeholder_handle_is_not_the_other_one():
    """HWND_TOPMOST is -1 and HWND_TOP is -2, and swapping them is silent.

    HWND_TOP only moves a window to the top of the ordinary band, so a topmost
    panel sent there with HWND_TOP would keep its flag, keep looking right, and
    sit under every other topmost window -- which is the fault this whole change
    exists to fix. It is one character apart from the right value.
    """
    assert app_main.HWND_TOPMOST == -1
    # As the pointer SetWindowPos receives it: all ones, not the integer -1.
    assert wintypes.HWND(app_main.HWND_TOPMOST).value == 0xFFFFFFFFFFFFFFFF


def test_the_user32_call_declares_its_prototype():
    """Every argument type and the return type, because ctypes declares none.

    A handle handed to an undeclared prototype is a number the marshaller has to
    guess the width of, and on 64-bit Windows a wrong guess truncates it and the
    call fails for a reason nobody reading the code can find. The function object
    is process-wide, so the declaration is done once.
    """
    fn = app_main._user32_set_window_pos()

    assert fn.argtypes is not None and len(fn.argtypes) == 7, (
        f"{fn.argtypes}: an undeclared SetWindowPos argument list"
    )
    # hWnd, hWndInsertAfter, x, y, cx, cy, uFlags -- both handles are pointers,
    # so both have to be said rather than inherited from a c_int default.
    assert fn.argtypes[0] is wintypes.HWND
    assert fn.argtypes[1] is wintypes.HWND
    assert fn.restype is wintypes.BOOL, (
        "the return value is a BOOL; an undeclared restype defaults to c_int and "
        "happens to match, which is not a reason to rely on it"
    )


def test_the_re_assertion_refuses_what_it_cannot_move(qapp, monkeypatch):
    """No window, or no user32: a refresh timer must not be able to take the app down."""
    recorder = RecordingUser32()
    monkeypatch.setattr(app_main, "_user32_set_window_pos", lambda: recorder)

    assert app_main.assert_topmost(0) is False
    assert not recorder.calls, "a window handle of zero was passed to user32 anyway"

    def no_user32():
        raise AttributeError("module 'ctypes' has no attribute 'windll'")

    monkeypatch.setattr(app_main, "_user32_set_window_pos", no_user32)
    assert app_main.assert_topmost(0x1234) is False


def test_the_re_assertion_interval_is_justified_by_the_measured_cost_of_a_call():
    """The cadence is bounded by what a call costs, not chosen by taste.

    Measured on this machine over 20,000 real calls against the real window:
    45.07 us of wall time and 27.34 us of CPU each. The ceiling is 0.05 % of one
    core across a whole day -- the chosen interval spends 0.024 %, and an interval
    four times tighter (250 ms) would spend 0.09 % and fail. One second of a day
    is 0.0012 % of a core, so the ceiling is about forty seconds of CPU a day.
    """
    cpu_per_call = 27.34e-6
    cpu_per_day = 86_400_000 / app_main.TOPMOST_REASSERT_MS * cpu_per_call
    core_per_day = cpu_per_day / 86_400

    assert core_per_day < 0.0005, (
        f"every {app_main.TOPMOST_REASSERT_MS} ms is {cpu_per_day:.1f} s of CPU a "
        f"day, {core_per_day * 100:.3f} % of one core: the panel would be paying "
        "for a z-order it already holds"
    )


# --- the system backdrop, which this panel must never be given -------------

DWMWA_SYSTEMBACKDROP_TYPE = 38

# What the panel may be left with: never written (0, AUTO) and explicitly
# cleared (1, NONE). Mica (2) and acrylic (3) are the two that put a system
# material behind the window rect, and the reason neither is allowed is a
# measurement rather than a preference -- see the test below and the README.
BACKDROP_NONE = (0, 1)

_DWM_GET = None


def _dwm_get_window_attribute():
    """DwmGetWindowAttribute with its prototype declared, resolved once.

    The *read* side of DWM lives only here. The application does not call DWM at
    all any more, so this exists purely so a test can ask Windows what the live
    window actually has rather than trusting that no code wrote it.
    """
    global _DWM_GET
    if _DWM_GET is None:
        getter = ctypes.windll.dwmapi.DwmGetWindowAttribute
        # Declared because ctypes guesses nothing: an undeclared prototype
        # marshals the handle as a number whose width it has to invent, and on
        # 64-bit Windows a wrong guess truncates it.
        getter.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        getter.restype = ctypes.c_long
        _DWM_GET = getter
    return _DWM_GET


def backdrop_of(hwnd):
    """The window's live DWMWA_SYSTEMBACKDROP_TYPE, as DWM reports it."""
    out = ctypes.c_int(-1)
    result = _dwm_get_window_attribute()(
        wintypes.HWND(hwnd),
        ctypes.c_uint(DWMWA_SYSTEMBACKDROP_TYPE),
        ctypes.byref(out),
        ctypes.sizeof(out),
    )
    assert result == 0, (
        f"could not read the backdrop attribute of {hwnd}: HRESULT 0x{result & 0xffffffff:08x}"
    )
    return out.value


def clickable_actions(menu):
    """Every action in the menu tree, submenus included, minus Quit.

    Quit is skipped because it shuts the application down, and this is about what
    a *toggle* can do to the window rather than about every action in the menu.
    """
    found = []
    for action in menu.actions():
        if action.isSeparator() or action.text() == "Quit":
            continue
        if action.menu() is not None:
            found.extend(clickable_actions(action.menu()))
        else:
            found.append(action)
    return found


def test_no_menu_action_can_put_a_system_backdrop_behind_the_panel(qapp, tmp_path):
    """Nothing the user can click may put a system material behind the panel.

    Measured on this machine (Windows 11 25H2, the real panel, a saturated
    backdrop behind it): setting DWMSBT_TRANSIENTWINDOW changed 538,328 of the
    547,600 pixels inside the window rect. What appeared was an opaque,
    hard-edged, *square-cornered* slab filling the 8 px bleed margin outside the
    rounded panel -- and the panel's own drop shadow was gone. On a real desktop
    that slab is the dark border the user reported. DWMWA_COLOR_NONE on
    DWMWA_BORDER_COLOR changes 0 of those pixels, so there is no separate border
    to suppress: the slab *is* the backdrop material, composited behind the whole
    window rect, and this panel deliberately keeps that margin transparent so its
    own shadow can fade to nothing against the desktop. No backdrop value can
    respect that, Mica and AUTO included.

    So the property is that the window never carries one, and the assertion is
    the window's own state rather than the absence of a particular line of code:
    every action in the real menu is triggered and the attribute is read back
    from the live handle after each one.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()
    actions = clickable_actions(menu)
    assert actions, "the menu has nothing to click: this test would prove nothing"
    hwnd = int(app.panel.winId())
    assert backdrop_of(hwnd) in BACKDROP_NONE, (
        f"a freshly built panel already carries backdrop {backdrop_of(hwnd)}: "
        "something is writing it before the user has touched anything"
    )

    offenders = []
    for action in actions:
        # trigger() is what a click does, and it flips a checkable action on --
        # the only state in which a backdrop would ever be asked for.
        action.trigger()
        found = backdrop_of(hwnd)
        if found not in BACKDROP_NONE:
            offenders.append((action.text(), found))

    assert not offenders, (
        f"{offenders} asked DWM for a system backdrop. It is composited behind "
        "the whole window rect, so it appears as a square-cornered slab in the "
        "shadow's transparent margin and takes the drop shadow with it."
    )


def test_the_tray_menu_offers_no_acrylic_toggle(qapp, tmp_path):
    """The toggle went with the plumbing, rather than becoming a dead switch.

    The old one was worse than dead: it was also the only way to *leave* the
    backdrop on, because the untick path short-circuited on bool(False) and never
    wrote the attribute. So the tick was a promise the panel could not keep.
    """
    app = menu_app(tmp_path)
    menu = app._build_menu()

    labels = [action.text() for action in clickable_actions(menu)]
    assert not [text for text in labels if "crylic" in text.lower()], (
        f"the menu still offers an acrylic backdrop: {labels}"
    )


def test_no_application_module_can_reach_a_dwm_backdrop_attribute():
    """The half the live-window test above cannot see: code that nothing calls.

    The read-back pins what the *panel* carries. This pins that no module in the
    application holds a DWM entry point at all, because a re-added helper the
    menu no longer reaches would pass the test above and put the slab back the
    day something started calling it.

    Scanned through the AST rather than with a text search, so prose survives: a
    comment may say "acrylic" or "system backdrop" and say why it cannot be used,
    but a name, attribute or string that could reach the attribute cannot hide in
    a line of explanation.
    """
    forbidden = ("dwmapi", "DwmSetWindowAttribute", "DwmGetWindowAttribute",
                 "DWMWA", "DWMSBT", "SYSTEMBACKDROP")
    root = Path(app_main.__file__).resolve().parent
    modules = sorted(root.glob("*.py"))
    assert modules, f"no application modules under {root}: the scan would pass vacuously"
    assert any(path.name == "main.py" for path in modules), (
        "main.py is not among the modules being scanned"
    )

    offenders = {}
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        words = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                words.add(node.value)
            elif isinstance(node, ast.Name):
                words.add(node.id)
            elif isinstance(node, ast.Attribute):
                words.add(node.attr)
            elif isinstance(node, ast.arg):
                words.add(node.arg)
        hits = sorted(word for word in words if any(f in word for f in forbidden))
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        f"{offenders} can still reach a DWM backdrop attribute. Measured on this "
        "machine, the backdrop material is composited behind the entire window "
        "rect: it shows as a square-cornered slab in the 8 px transparent margin "
        "the panel keeps for its own drop shadow, and the shadow disappears. "
        "There is no value that avoids it -- Mica (2) and AUTO (0) fill the same "
        "rect -- so the setting cannot come back."
    )


def test_no_function_references_a_name_the_module_does_not_define():
    """The class of defect 526 tests cannot see: dead code that raises.

    theme.py kept `label_font()`, `value_font()` and `aux_font()` after the
    refactor that moved typography onto `Layout`. Nothing calls them, so no test
    calls them, so all three stayed green while `theme.label_font()` raised
    NameError -- the names they reach for (`LABEL_PT`, `VALUE_PT`, `AUX_PT`)
    were deleted with the constants they used to be derived from. A function that
    raises the moment anybody calls it is not dead code, it is a booby trap, and
    the only reason it was safe is that nobody had wanted the feature.

    Python resolves a bare name at call time, not at import time, so nothing about
    importing the module, running the suite or starting the app notices. This is
    the check that would have: every name a function body reads, resolved against
    the names the module itself binds, its imports, its parameters, its locals and
    the builtins.

    Deliberately not a linter. Nothing is installed here -- no ruff, flake8,
    pyflakes, pylint or mypy -- and one is not worth adding a dependency for a
    single undefined-name rule; ~40 lines of `ast` does it and fails on nothing
    else in this repository.
    """
    bound = set(dir(builtins)) | {
        # Module dunders, which the import machinery binds and no assignment in
        # the module ever names.
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__debug__", "__path__", "__all__",
        "__annotations__",
    }
    offenders = {}

    def bind(node):
        """Every name this node puts into a scope somewhere in the module."""
        if isinstance(node, ast.Name):
            # Store or Del only. Binding a Load would add the very name being
            # checked, and the check would resolve itself.
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)

    root = Path(app_main.__file__).resolve().parent
    modules = sorted(root.glob("*.py"))
    assert modules, f"no application modules under {root}: the scan would pass vacuously"

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Bound names are collected for the whole module first, including
        # anything bound inside a function: a local, a parameter or an import
        # below the use is a resolution, and pretending otherwise would report
        # perfectly good code.
        for node in ast.walk(tree):
            bind(node)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                missing = sorted({
                    name.id for name in ast.walk(node)
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
                    and name.id not in bound
                })
                if missing:
                    offenders.setdefault(path.name, []).append(
                        f"{node.name}() at line {node.lineno} reads {missing}"
                    )

    assert not offenders, (
        f"{offenders} name something the module never binds. A function whose body "
        "raises NameError is not dead code -- it is a booby trap, and Python does "
        "not resolve a bare name until it is called, so no test sees it until "
        "something wants the feature back. Delete the function or bind the name."
    )


def test_the_readme_does_not_claim_the_panel_hides_itself_during_a_game():
    """The README contradicted the code, three cells and a paragraph against it.

    The fullscreen table said the panel "is hidden while the game is running", and
    the Russian cell said the same. Nothing hides it: there is no hide(), no
    show() outside __init__ and shutdown, and no code anywhere that watches for a
    game -- the user asked for it never to disappear on its own, and three other
    places in the same document say so correctly.

    A document that contradicts its own implementation is worse than one that is
    merely incomplete, because the contradiction is the part a reader believes
    about the *other* claims. So both cells are checked, and so is the honest
    statement that replaces them: the panel never hides itself, and exclusive
    fullscreen owns the display's output so nothing composites over it, while
    borderless fullscreen is an ordinary window the topmost re-assertion holds.

    A claim checked against the code rather than against wording: the assertion
    is that no cell says the panel hides, and that each of the two cells does say
    the two things that are true.
    """
    readme = (Path(app_main.__file__).resolve().parent / "README.md").read_text(
        encoding="utf-8"
    )

    def rows():
        """(mode label, description) for every fullscreen row, both languages."""
        for line in readme.splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != 2 or "fullscreen" not in cells[0] and "полноэкран" not in cells[0]:
                continue
            yield cells[0], cells[1]

    table = list(rows())
    exclusive = [(a, b) for a, b in table if "xclusive" in a or "ксклюзивный" in a]
    borderless = [(a, b) for a, b in table if "orderless" in a or "ограничный" in a]
    assert len(exclusive) == 2 and len(borderless) == 2, (
        f"expected one row per fullscreen mode in each of the two languages, got "
        f"exclusive={exclusive} borderless={borderless}"
    )

    for label, cell in exclusive:
        # The old claim, in both languages.
        assert not cell.startswith("Is hidden"), (label, cell)
        assert not cell.startswith("Скрыта"), (label, cell)
        # The two things that are true, in both languages.
        assert "never hides" in cell.lower() or "никогда не прячется" in cell.lower(), (
            label, cell
        )
        assert "borderless" in cell.lower() or "пограничн" in cell.lower(), (label, cell)
        assert "output" in cell.lower() or "вывод" in cell.lower(), (label, cell)


def test_every_persisted_boolean_has_a_toggle_and_nothing_else_does(qapp, tmp_path):
    """One invariant instead of two absence checks, and it covers the next field.

    A setting with no menu row cannot be changed by the user; a menu row with no
    setting is a switch that goes somewhere else. Both are silent, and both were
    live in this project at once: a toggle whose value was written to the window
    flags and nowhere else, and a persisted field nothing read.
    """
    from dataclasses import fields

    app = menu_app(tmp_path)
    app._build_menu()

    persisted = {
        f.name for f in fields(Settings)
        # settings.py carries `from __future__ import annotations`, so a field's
        # declared type is the string that was written, not the class itself.
        if f.type == "bool"
    }
    assert set(app._toggle_actions) == persisted, (
        f"menu toggles {sorted(app._toggle_actions)} against persisted booleans "
        f"{sorted(persisted)}"
    )
