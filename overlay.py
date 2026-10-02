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

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

import theme
from history import History
from painter import paint
from settings import Settings

logger = logging.getLogger("widget.overlay")

WINDOW_TITLE = "System Monitor"

# Seconds between two render-failure records, whatever the faults are, and
# however many frames pass in between. update() fires every theme.TICK_MS, so
# 300 s is 150 frames of silence between records: 288 a day for a fault that
# never clears, for one that fails on every other frame, and for two faults
# alternating -- any of which the frame count alone would turn into 43 200.
# Long enough that the log stops growing with uptime, short enough that
# "still broken" appears several times in a working session.
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
        reads cpu_live_mhz, cpu_nominal_mhz and the two net columns straight off
        the dataclass rather than through an accessor, so any snapshot thinner
        than metrics.Snapshot would take the whole application down from inside
        a paint event. The guard belongs at this boundary rather than inside
        paint(): the renderer stays pure and testable, and it keeps raising
        where a test can see it.

        The fields named above are the reason this comment matters: they are read
        directly, so the cost of dropping one is not a missing number but a dead
        process, and the fields most likely to be renamed are the ones listed.
        """
        if self._snapshot is None:
            return
        painter = None
        try:
            painter = QPainter(self)
            paint(painter, self._snapshot, self._histories, self.current_alpha())
        except Exception:
            self._log_paint_failure()
        finally:
            # end() in a finally, so neither a raise nor the guard above can
            # leave the painter holding the widget's paint device. The
            # construction is inside the try too: "a frame costs a frame" covers
            # every statement of the frame, and a widget that cannot hand out a
            # painter is the one that has none left.
            if painter is not None:
                painter.end()

    def _log_paint_failure(self) -> None:
        """At most one record per PAINT_ERROR_LOG_INTERVAL. That is the whole rule.

        An earlier version kept a memo of the traceback and let a failure through
        immediately whenever the memo was empty, which a clean frame emptied.
        That bypass was the last unbounded case: a renderer failing on every
        other frame looks healthy half the time and still wrote 21 600 records
        a day -- one per bad frame of 43 200 -- which is the log growth this
        rewrite exists to remove. Nothing here is exempt from the floor.

        The cost, stated rather than discovered later: a *different* fault
        arriving while the floor is counting down is not reported until the floor
        expires. That is acceptable because the panel is visibly broken and the
        record on file already names a fault from this widget, and the record
        written when the floor is up always carries the fault as it stands
        then -- so the new one is delayed by at most one interval, never lost.
        """
        now = time.monotonic()
        if now - self._last_paint_log < PAINT_ERROR_LOG_INTERVAL:
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
            available.right() - self.width() - theme.CORNER_MARGIN,
            available.top() + theme.CORNER_MARGIN,
        )
        self.move(self.clamp_to_screen(target))
        self.remember_position()

    def remember_position(self) -> None:
        self._settings.x, self._settings.y = self.x(), self.y()

    # --- input ------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            # Every button path gives the grab back, this one included: the app
            # is about to quit, and a widget that has already started shutting
            # down has no business still claiming it is being dragged.
            self._cancel_drag()
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
                # the first sign of it. Give the state back rather than drag on,
                # and say the event was handled, as every other branch here does.
                self._cancel_drag()
                event.accept()
                return
            target = event.globalPosition().toPoint() - self._drag_origin
            self.move(self.clamp_to_screen(target))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._cancel_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _cancel_drag(self) -> None:
        """Forget the grab, give back the cursor, and write down where the panel is.

        A release that was swallowed leaves a grab armed against a panel nobody
        is holding, and the next move -- with no button down -- walks it across
        the screen. apply_window_flags() re-shows a visible widget, and Qt does
        not deliver the release that was in flight across the hide, so
        hideEvent cancels the drag for the same reason.

        The position is saved because the panel has already moved by the time any
        of these paths run: the cancel is about the grab, not about where the
        panel ended up. Leaving that to closeEvent lost the drop whenever a drag
        was cancelled rather than released -- dragged somewhere, then hidden, and
        settings.json still held the corner the panel started from.
        """
        self._drag_origin = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.remember_position()

    def hideEvent(self, event) -> None:
        self._cancel_drag()
        super().hideEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # The grab goes back whatever button the double click carried: a
        # double-click whose release was swallowed leaves _drag_origin armed, and
        # the closed-hand cursor outlives the hand that closed it. Restricting
        # this handler to the left button put that cancel behind an early
        # return, and the right button then leaked the grab it had no business
        # touching.
        self._cancel_drag()
        if event.button() != Qt.MouseButton.LeftButton:
            # The right button asks for the tray menu. It has no business moving
            # the panel, and none at all snapping it home.
            super().mouseDoubleClickEvent(event)
            return
        # Before the move, not after it: the corner the panel is about to jump
        # to would otherwise carry a closed-hand cursor claiming it is being
        # carried there.
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
