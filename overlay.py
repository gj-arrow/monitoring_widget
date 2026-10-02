"""The panel window: placement, mouse handling, and painting delegation.

Dragging works from the first press. The previous version required a double
click to arm a drag mode, which needed its own double-click detector, two
QTimers and a per-click timer allocation -- thirteen commits of fixes grew out
of that one extra step.

The widget is theme.CANVAS_W x theme.CANVAS_H, not theme.WIDTH x theme.HEIGHT.
The panel is 280 x 280 and the canvas is that plus a bleed on every side,
because paint() draws the drop shadow *outside* panel_rect(): a widget the size
of the panel has nowhere for the halo to go, clips it, and leaves the panel's
edge reading as a hard wall against the desktop.
"""

from __future__ import annotations

import logging
import time
import traceback

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

import theme
from history import History
from painter import paint
from settings import Settings

logger = logging.getLogger("widget.overlay")

WINDOW_TITLE = "System Monitor"
CORNER_MARGIN = 10

# Seconds between two render-failure records, whatever the faults are. update()
# fires every theme.TICK_MS, so 300 s is 150 frames of silence between records:
# a fault that never clears is announced at most 288 times a day instead of
# 43 200, and still often enough that "still broken" shows up several times in
# any working session. A fault that clears and comes back later does not wait
# for this at all -- the first frame that paints cleanly drops the memo, and the
# next failure is written out at once.
PAINT_ERROR_LOG_INTERVAL = 300.0


class MonitorPanel(QWidget):
    menu_requested = pyqtSignal(QPoint)
    quit_requested = pyqtSignal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._snapshot = None
        self._histories: dict[str, History] = {
            key: History() for key in theme.METRICS_BY_KEY
        }
        self._drag_origin: QPoint | None = None
        self._last_paint_error: str | None = None
        self._last_paint_log = float("-inf")

        self.setWindowTitle(WINDOW_TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(theme.CANVAS_W, theme.CANVAS_H)
        self.apply_window_flags()

    # --- configuration ----------------------------------------------------

    def apply_window_flags(self) -> None:
        """Re-apply the window flags, e.g. after always-on-top was toggled.

        Qt hides a visible widget when its window flags change and recreates it
        as a native window, so a panel that was on screen is put back.
        """
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()

    def current_alpha(self) -> float:
        return self._settings.alpha

    def set_alpha(self, value: float) -> None:
        self._settings.alpha = max(theme.MIN_ALPHA, min(theme.MAX_ALPHA, value))
        self.update()

    def apply_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        for spec in theme.METRICS:
            self._histories[spec.key].append(theme.row_fraction(spec, snapshot))
        self.update()

    # --- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:
        """Delegate to the pure renderer, on a painter that is ended here.

        Nothing is drawn before the first sample: an empty window beats a black
        rectangle for the fraction of a second between show() and the first tick.

        A renderer that raises costs one frame, not the process. PyQt calls
        qFatal() when an exception escapes a reimplemented virtual method, and
        this widget is meant to sit on someone's desktop all day; painter.py
        still reads snapshot.ts, cpu_mhz and cpu_max_mhz straight off the
        dataclass, so any snapshot thinner than metrics.Snapshot would take the
        whole application down from inside a paint event. The guard belongs at
        this boundary rather than inside paint(): the renderer stays pure and
        testable, and it keeps raising where a test can see it.
        """
        if self._snapshot is None:
            return
        painter = QPainter(self)
        try:
            paint(painter, self._snapshot, self._histories, self.current_alpha())
        except Exception:
            self._log_paint_failure()
        else:
            # A frame that painted cleanly makes the next fault news again.
            self._last_paint_error = None
        finally:
            # end() in a finally, so neither a raise nor the guard above can
            # leave the painter holding the widget's paint device.
            painter.end()

    def _log_paint_failure(self) -> None:
        """At most one record per PAINT_ERROR_LOG_INTERVAL, naming the newest fault.

        The identity alone cannot bound this. It says whether the traceback
        changed, and a changed traceback is exactly what two faults alternating
        every frame -- or one message carrying a varying value, `expected 37.4`
        -- produce on every single frame: 43 200 tracebacks a day, about 17 MB,
        into a plain basicConfig with no rotation. That is the log-growth
        problem this rewrite set out to fix, so the interval gates every record
        rather than only the repeats.

        What the identity still buys: the first failure after a healthy frame is
        reported at once, since a clean frame drops the memo, and the record
        written when the interval is up always carries the fault as it stands
        then -- so a new fault is delayed, never lost.
        """
        already_reported = self._last_paint_error is not None
        self._last_paint_error = traceback.format_exc()
        now = time.monotonic()
        if already_reported and now - self._last_paint_log < PAINT_ERROR_LOG_INTERVAL:
            return
        self._last_paint_log = now
        logger.exception("panel paint failed")

    # --- placement --------------------------------------------------------

    def clamp_to_screen(self, top_left: QPoint) -> QPoint:
        """The nearest on-screen top-left, measured against the widget's size.

        Width and height are the canvas, which is what gets clamped: a
        position stored by a build whose widget was only as big as the panel is
        still a top-left corner and still means a place on screen, so it is
        judged, not rewritten.
        """
        available = self.screen().availableGeometry()
        return QPoint(
            max(available.left(), min(int(top_left.x()), available.right() - self.width())),
            max(available.top(), min(int(top_left.y()), available.bottom() - self.height())),
        )

    def restore_default_position(self) -> None:
        available = self.screen().availableGeometry()
        target = QPoint(
            available.right() - self.width() - CORNER_MARGIN,
            available.top() + CORNER_MARGIN,
        )
        self.move(self.clamp_to_screen(target))
        self.remember_position()

    def remember_position(self) -> None:
        self._settings.x, self._settings.y = self.x(), self.y()

    # --- input ------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.quit_requested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # The grab is the cursor's offset from the window's corner, kept in
            # global coordinates: the panel then follows the cursor by exactly
            # the distance it was moved, wherever on the panel it was held.
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                # No button down, so nothing is being carried: a release went
                # missing (hidden and re-shown, a lost grab) and this move is
                # the first sign of it. Give the state back rather than drag on.
                self._cancel_drag()
            else:
                target = event.globalPosition().toPoint() - self._drag_origin
                self.move(self.clamp_to_screen(target))
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._cancel_drag()
            self.remember_position()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _cancel_drag(self) -> None:
        """Forget the grab and give back the cursor. No position is saved.

        A release that was swallowed leaves a grab armed against a panel
        nobody is holding, and the next move -- with no button down -- walks it
        across the screen. apply_window_flags() re-shows a visible widget, and
        Qt does not deliver the release that was in flight across the hide, so
        hideEvent cancels the drag for the same reason.
        """
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def hideEvent(self, event) -> None:
        self._cancel_drag()
        super().hideEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # Before the move, not after it: a double-click whose release was
        # swallowed leaves the grab armed, and the corner the panel is about to
        # jump to would carry a closed-hand cursor claiming it is being carried
        # there.
        self._cancel_drag()
        self.restore_default_position()
        event.accept()

    def wheelEvent(self, event) -> None:
        vertical = event.angleDelta().y()
        if vertical == 0:
            # A horizontal wheel notch, or a trackpad swipe that never left the
            # x axis. Spending a whole step of opacity on it would be the
            # gesture's only effect.
            event.accept()
            return
        step = theme.WHEEL_ALPHA_STEP if vertical > 0 else -theme.WHEEL_ALPHA_STEP
        self.set_alpha(self.current_alpha() + step)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        """Report where the menu belongs; the app owns the menu itself."""
        self.menu_requested.emit(event.globalPos())
        event.accept()

    def closeEvent(self, event) -> None:
        self.remember_position()
        super().closeEvent(event)
