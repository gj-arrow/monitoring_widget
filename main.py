"""Application entry point: DPI policy, sampling thread, tray menu.

Sampling runs on its own thread. psutil.cpu_percent(interval=0.1) blocks for
a tenth of a second, and doing that on the GUI thread froze the panel every
two seconds; here the GUI thread only ever paints.

The tray icon and the right-click menu are the only way out of a panel with
no title bar: without them the process lives until the task manager notices.
"""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import sys
from ctypes import wintypes
from pathlib import Path

from PyQt6.QtCore import (
    QDeadlineTimer,
    QMutex,
    QMutexLocker,
    QPoint,
    QThread,
    QTimer,
    QWaitCondition,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import pythoncom

import theme
from history import HistoryLog
from metrics import SystemProbe
from overlay import MonitorPanel
from settings import Settings, config_path, load_settings, save_settings

logger = logging.getLogger("widget.main")

LOG_NAME = "app_debug.log"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3

# How long shutdown waits for the sampler. Bounded because a sample already
# blocked inside psutil cannot be cancelled, and a widget on someone's desktop
# must never hang on it: past this the thread is abandoned, not waited for.
SHUTDOWN_WAIT_MS = 3000

TRAY_TITLE = "System Monitor"
ACRYLIC_REFUSED = "This Windows build refused the acrylic backdrop."
HISTORY_REFUSED = "Could not open metrics_history.log."

ALPHA_STEPS = (("35%", 0.35), ("50%", 0.50), ("65%", 0.65), ("80%", 0.80), ("100%", 1.00))

# How close an alpha has to be to count as one of the labels above. The wheel
# moves in theme.WHEEL_ALPHA_STEPs and the labels are further apart than that,
# so this is never a question of exact equality.
ALPHA_EPSILON = 0.001


def alpha_label(value: float) -> str:
    return f"{round(value * 100)}%"


def labelled_alpha(alpha: float) -> float | None:
    """The labelled step this alpha is, or None when it sits between two."""
    for _, value in ALPHA_STEPS:
        if abs(alpha - value) < ALPHA_EPSILON:
            return value
    return None


def log_path() -> Path:
    """Beside settings.json, which is beside the executable when frozen.

    Not the working directory: a frozen build started from a shortcut has a
    working directory nobody chose, and the log would be scattered across the
    desktop while the settings it explains stayed put.
    """
    return config_path().with_name(LOG_NAME)


def build_log_handler(name: str | Path | None = None) -> logging.handlers.RotatingFileHandler:
    """A log that cannot outgrow its welcome: at most 3 x LOG_MAX_BYTES."""
    handler = logging.handlers.RotatingFileHandler(
        str(name or log_path()),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(build_log_handler())
    # The per-metric warning lines are useful; the INFO chatter from failed
    # probes is not, and it was most of what filled the old log.
    logging.getLogger("widget.metrics").setLevel(logging.WARNING)


class Collector(QThread):
    """Samples on a worker thread and emits each Snapshot to the GUI thread.

    A poke is a request for one sample, not a queue entry. The timer pokes
    every theme.TICK_MS and the menu pokes again on the way in, so a poke that
    queued would let the sampler fall behind and catch up as fast as the CPU
    allows; 25 pokes during one in-flight sample buy exactly one extra
    reading, which is what the boolean flag below is for.

    `on_sampled` is handed each Snapshot here, on this thread, rather than
    through the `sampled` signal. That signal is queued, so its slots run on
    the GUI thread -- which is fine for painting a panel and wrong for
    appending a CSV row on every tick.

    `make_probe` is a factory, and it is called here rather than by the caller,
    because a probe is not just an object: SystemProbe opens a COM connection
    for the CPU clock, and a COM connection belongs to the thread that opened
    it. Building the probe on the GUI thread and querying it here made the first
    WMI query of every launch fail with RPC_E_WRONG_THREAD (0x8001010E), which
    only _reconnect() hid -- see the docstring on run().
    """

    sampled = pyqtSignal(object)

    def __init__(self, make_probe, on_sampled=None, parent=None) -> None:
        super().__init__(parent)
        self._make_probe = make_probe
        self._probe = None
        self._on_sampled = on_sampled
        self._mutex = QMutex()
        self._wake = QWaitCondition()
        self._pending = False

    def poke(self) -> None:
        """Ask for an out-of-band sample, e.g. right after the user opens the menu."""
        with QMutexLocker(self._mutex):
            self._pending = True
            self._wake.wakeAll()

    def _publish(self, snapshot) -> None:
        """Hand the sample to the GUI thread, and write the trace from here.

        The emit first: it only posts an event, so the panel starts repainting
        while the file is being appended to.
        """
        self.sampled.emit(snapshot)
        if self._on_sampled is not None:
            self._on_sampled(snapshot)

    def run(self) -> None:
        # COM for this thread, explicitly, before metrics.py can touch it. The
        # measured reason for it is thinner than an earlier version of this
        # comment claimed, and it is written down accurately because the call
        # stays:
        #
        #   `import wmi` is not a passive import. At module scope wmi.py runs
        #   GetObject("winmgmts:") to find its namespace, so the *first* import
        #   opens a connection from whichever thread asked for it. Measured on
        #   this machine: a bare threading.Thread whose first statement is
        #   `import wmi` *succeeds* -- pywin32 initialises COM on the calling
        #   thread by itself -- and then prints "Win32 exception occurred
        #   releasing IUnknown" as the apartment it never asked for is torn
        #   down. The same thread with the call below imports cleanly and prints
        #   nothing. So an earlier claim here, that a plain threading.Thread
        #   could not even import wmi, does not reproduce; what is true is that
        #   the apartment is then an accident of the call path rather than a
        #   decision, and the release is what needs it.
        #
        #   The apartment has to be uninitialised on the way out, or every
        #   launch leaks one on a thread that then ends.
        #
        # pywin32's signature is CoInitializeEx(flags) -- one argument. The
        # two-argument form in the COM headers is C's, and passing it here
        # raises TypeError.
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            self._probe = self._make_probe()
            self._sample_loop()
        except Exception:
            # An exception out of a QThread virtual cannot be caught from the
            # outside: it aborts the interpreter. Measured here, on this
            # machine, for exactly this handler's sake: -1073740791 (0xC0000409)
            # and **zero** bytes in app_debug.log. So a missing psutil or an
            # unavailable sensor took the app down with no explanation at all --
            # worse than the cross-apartment fault the factory above fixed,
            # which at least logged.
            #
            # It is here rather than in main()'s try/except because neither
            # SystemProbe.__init__ nor CpuClockProbe guards what it builds --
            # `import psutil` and a COM connection are both unguarded -- and the
            # factory that moved the construction onto this thread moved it out
            # of main()'s reach. What is left when this catches is a panel whose
            # status dot goes neutral, which is the signal for a collector that
            # stopped, and one record here saying why.
            logger.exception("sampler thread stopped on an unhandled fault")
        finally:
            # Released before CoUninitialize, on the thread that opened it. A
            # COM object released after its apartment is gone makes pywin32
            # release it into nothing and abort the process with 0xC0000409
            # ("Win32 exception occurred releasing IUnknown" on stderr is the
            # last thing it manages to say). Reversing the two lines reproduces
            # that on every shutdown.
            #
            # A frame-local cannot release early enough to avoid it: the frame
            # is popped only after run() returns.
            self._probe = None
            pythoncom.CoUninitialize()

    def _sample_loop(self) -> None:
        """Sample until interrupted. Runs entirely inside run()'s COM scope."""
        probe = self._probe
        while not self.isInterruptionRequested():
            # Cleared before the sample rather than after it: a sample takes
            # about 100 ms, and a poke arriving in that window would be erased
            # by a clear that runs afterwards. The flag is also what makes the
            # poke land at all -- wakeAll() on a QWaitCondition nobody is
            # waiting on is dropped, and it is dropped here, mid-sample.
            with QMutexLocker(self._mutex):
                self._pending = False
            self._publish(probe.sample())
            with QMutexLocker(self._mutex):
                # The three ways out of a wait: poked, the tick expiring, and
                # an interruption. The last one is checked here as well as at
                # the top of the loop, so shutdown does not have to sit out the
                # rest of the tick waiting for the deadline to expire.
                while not self._pending and not self.isInterruptionRequested():
                    if not self._wake.wait(self._mutex, QDeadlineTimer(theme.TICK_MS)):
                        break
                self._pending = False


def stop_collector(collector: Collector, timeout_ms: int = SHUTDOWN_WAIT_MS) -> bool:
    """Interrupt the sampler and wait for it, bounded. True when it stopped.

    The poke is what makes it prompt, and the timeout is what makes it finite:
    a sample already blocked inside psutil cannot be interrupted, so waiting on
    it forever is the one way this could hang.
    """
    collector.requestInterruption()
    collector.poke()
    return collector.wait(timeout_ms)


def _dwm_set_window_attribute():
    """DwmSetWindowAttribute with its prototype declared.

    ctypes guesses no argument types at all, and this call works only because
    wintypes.HWND happens to subclass c_void_p. A handle handed to an
    undeclared prototype is a number the marshaller has to guess the width of,
    and on 64-bit Windows a wrong guess truncates it and fails for a reason
    nobody reading the code can find. The function object is process-wide, so
    the declaration is done once, here.
    """
    setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
    setter.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    setter.restype = ctypes.c_long  # an HRESULT, and S_OK is 0
    return setter


def enable_acrylic(hwnd: int) -> bool:
    """Ask DWM for real backdrop blur. False when the OS refuses.

    Only reached when the user ticks the setting; the default translucent fill
    needs no ctypes and works from Windows 8 onwards.
    """
    if not sys.platform.startswith("win") or not hwnd:
        return False
    try:
        value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        result = _dwm_set_window_attribute()(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except Exception:
        logger.info("acrylic unavailable", exc_info=True)
        return False


def build_tray_icon() -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.CALM)
        painter.drawRoundedRect(4, 4, 24, 24, 7, 7)
        painter.setBrush(theme.VALUE)
        for index, height in enumerate((8, 13, 18)):
            painter.drawRoundedRect(9 + index * 6, 26 - height, 4, height, 2, 2)
    finally:
        painter.end()
    return QIcon(pixmap)


class MonitorApp:
    """The panel, the sampler feeding it, and the tray menu that owns both."""

    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.history_log = HistoryLog()
        if self.settings.log_history:
            self.settings.log_history = self.history_log.enable()

        self.panel = MonitorPanel(self.settings)
        self.panel.set_alpha(self.settings.alpha)

        # No parent: a QThread whose parent is destroyed while it is still
        # running aborts the process, and a widget must not be able to do that
        # by being collected. Nothing here can collect it -- this object holds
        # it and shutdown() joins it first.
        self.collector = self._make_collector()
        self.collector.sampled.connect(self.panel.apply_snapshot)

        self.panel.menu_requested.connect(self._show_menu_at)
        self.panel.quit_requested.connect(self.shutdown)

        self._stopped = False
        self._alpha_actions: dict[float, QAction] = {}
        self._toggle_actions: dict[str, QAction] = {}

        # Before the menu exists, so the checkmark shows what was actually
        # granted rather than what the file asked for.
        self._apply_acrylic(self.settings.acrylic)

        self.tray = QSystemTrayIcon(build_tray_icon(), self.panel)
        self.tray.setToolTip(TRAY_TITLE)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setContextMenu(self._build_menu())
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("no system tray: the right-click menu is the only way out")
        self.tray.show()

        self.timer = QTimer(self.panel)
        self.timer.timeout.connect(self.collector.poke)
        self.timer.start(theme.TICK_MS)

        self._restore_position()
        self.panel.show()

        self.collector.start()

    # --- wiring -----------------------------------------------------------

    def _make_collector(self) -> Collector:
        """The sampler, with the CSV trace hanging off its worker side.

        `SystemProbe` is passed as a class, not called here: the probe opens a
        WMI COM connection while it is being built, and that connection belongs
        to whichever thread did the building. Collector builds it on the worker
        thread that will query it. Building it on this one made the first query
        of every launch fail -- measured, every launch, x_wmi
        RPC_E_WRONG_THREAD -- and the app only survived because
        CpuClockProbe._reconnect() silently rebuilt the handle on the worker a
        few microseconds later.

        Initialising COM at the top of Collector.run() was the other candidate
        and it does not work, which is worth recording so nobody tries it again:
        the connection carries an apartment-threaded proxy belonging to the
        thread that opened it, and CoInitializeEx on the *querying* thread puts
        that thread in an apartment without moving the proxy into it. Both
        COINIT_APARTMENTTHREADED and COINIT_MULTITHREADED were measured on this
        machine and both still raise x_wmi 0x8001010E. pywin32 initialises COM
        on the calling thread by itself when wmi.WMI() is constructed, so there
        is nothing left to initialise by hand once the connection is opened
        where it is used.

        The append is a callback on the collector rather than a slot on
        `sampled`: that signal is queued to the GUI thread, so a file write
        every two seconds would land on the one thread this class exists to
        keep free. Panel painting is the only thing that belongs over there.
        """
        return Collector(SystemProbe, self._record_history)

    def _record_history(self, snapshot) -> None:
        self.history_log.write(snapshot)

    def _restore_position(self) -> None:
        if self.settings.x is not None and self.settings.y is not None:
            self.panel.move(self.panel.clamp_to_screen(QPoint(self.settings.x, self.settings.y)))
        else:
            self.panel.restore_default_position()
        self.panel.remember_position()

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.panel.restore_default_position()

    def _show_menu_at(self, global_pos) -> None:
        # The values the user is about to read are two seconds old at worst;
        # this makes the menu open on fresh ones.
        self.collector.poke()
        self.tray.contextMenu().exec(global_pos)

    def _sync_menu(self) -> None:
        """Re-check the menu against the settings before it opens.

        A menu built once at startup keeps claiming the state it had then: the
        wheel changes the alpha without coming through here, and the acrylic
        and history toggles can be refused by the OS or by the filesystem
        after the checkmark has already gone on. Wired to the menu's
        aboutToShow rather than called from the one place that opens it, because
        there are two: the panel's own context menu and a right-click on the
        tray icon, and a checkmark that is only refreshed on one of them lies
        on the other.
        """
        chosen = labelled_alpha(self.settings.alpha)
        for value, action in self._alpha_actions.items():
            action.setChecked(value == chosen)
        self._alpha_custom.setVisible(chosen is None)
        self._alpha_custom.setChecked(chosen is None)
        if chosen is None:
            self._alpha_custom.setText(f"Custom ({alpha_label(self.settings.alpha)})")
        for name, action in self._toggle_actions.items():
            action.setChecked(bool(getattr(self.settings, name)))

    # --- menu -------------------------------------------------------------

    def _build_menu(self) -> QMenu:
        menu = QMenu()
        menu.aboutToShow.connect(self._sync_menu)

        reset = QAction("Reset position", menu)
        reset.triggered.connect(self.panel.restore_default_position)
        menu.addAction(reset)

        opacity = menu.addMenu("Opacity")
        for label, value in ALPHA_STEPS:
            action = QAction(label, opacity)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, v=value: self._set_alpha(v))
            opacity.addAction(action)
            self._alpha_actions[value] = action

        # The wheel steps the alpha by theme.WHEEL_ALPHA_STEP, which is finer
        # than the gaps between the labels, so a notch nearly always lands on
        # one of them and not on a label. With nothing here to say otherwise,
        # every checkmark went false and the menu read as "no opacity chosen"
        # while the panel was showing one. This row is the honest answer to the
        # value the wheel left behind, and it is only there when no label is.
        custom = QAction("", opacity)
        custom.setCheckable(True)
        opacity.addAction(custom)
        self._alpha_custom: QAction = custom

        on_top = QAction("Always on top", menu)
        on_top.setCheckable(True)
        on_top.setChecked(self.settings.always_on_top)
        on_top.toggled.connect(self._set_always_on_top)
        menu.addAction(on_top)
        self._toggle_actions["always_on_top"] = on_top

        acrylic = QAction("Acrylic backdrop", menu)
        acrylic.setCheckable(True)
        acrylic.setChecked(self.settings.acrylic)
        acrylic.toggled.connect(self._set_acrylic)
        menu.addAction(acrylic)
        self._toggle_actions["acrylic"] = acrylic

        history = QAction("Write history to file", menu)
        history.setCheckable(True)
        history.setChecked(self.settings.log_history)
        history.toggled.connect(self._set_log_history)
        menu.addAction(history)
        self._toggle_actions["log_history"] = history

        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.shutdown)
        menu.addAction(quit_action)
        # Every checkmark, including the opacity rows, is decided in one place.
        self._sync_menu()
        return menu

    def _set_alpha(self, value: float) -> None:
        self.panel.set_alpha(value)
        self.collector.poke()

    def _set_always_on_top(self, enabled: bool) -> None:
        self.settings.always_on_top = enabled
        self.panel.apply_window_flags()

    def _apply_acrylic(self, enabled: bool) -> bool:
        """Try to turn the backdrop on, and record what was actually granted."""
        granted = bool(enabled) and enable_acrylic(int(self.panel.winId()))
        self.settings.acrylic = granted
        if enabled and not granted:
            logger.info("acrylic refused, using the translucent fill")
        return granted

    def _set_acrylic(self, enabled: bool) -> None:
        granted = self._apply_acrylic(enabled)
        if enabled and not granted:
            self.tray.showMessage(TRAY_TITLE, ACRYLIC_REFUSED)
        self.panel.update()

    def _set_log_history(self, enabled: bool) -> None:
        if not enabled:
            # HistoryLog keeps its own flag, and write() only checks that one:
            # clearing the setting alone would leave the trace growing.
            self.history_log.disable()
            self.settings.log_history = False
            return
        self.settings.log_history = self.history_log.enable()
        if not self.settings.log_history:
            self.tray.showMessage(TRAY_TITLE, HISTORY_REFUSED)

    # --- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        """Stop everything, once, however many things asked.

        Middle-click, the tray's Quit and an external quit all land here and
        any two of them can arrive together; a second call returns at once
        rather than waiting out another timeout or saving settings over a
        collector that is already gone.
        """
        if self._stopped:
            return
        self._stopped = True
        self.timer.stop()
        if not stop_collector(self.collector):
            logger.error("sampler thread did not stop within %d ms", SHUTDOWN_WAIT_MS)
        save_settings(self.settings)
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    setup_logging()
    logger.info("MonitorApp starting")
    try:
        monitor = MonitorApp()
    except Exception:
        # A frozen build has no console for the traceback to reach, so this is
        # the only record that startup ever happened and failed.
        logger.exception("MonitorApp failed to start")
        return 1
    # A quit nobody asked for -- a logoff, a session end -- does not come
    # through the tray menu, and the settings are only written on the way out.
    app.aboutToQuit.connect(monitor.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())