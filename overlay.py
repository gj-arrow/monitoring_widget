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

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

import theme
from history import History
from painter import paint
from settings import Settings

WINDOW_TITLE = "System Monitor"
CORNER_MARGIN = 10


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
        """
        if self._snapshot is None:
            return
        painter = QPainter(self)
        try:
            paint(painter, self._snapshot, self._histories, self._settings.alpha)
        finally:
            # end() in a finally, so a raise out of paint() cannot leave the
            # painter holding the widget's paint device for the next tick.
            painter.end()

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
            target = event.globalPosition().toPoint() - self._drag_origin
            self.move(self.clamp_to_screen(target))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._drag_origin = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.remember_position()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.restore_default_position()
        event.accept()

    def wheelEvent(self, event) -> None:
        step = 0.05 if event.angleDelta().y() > 0 else -0.05
        self.set_alpha(self._settings.alpha + step)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        """Report where the menu belongs; the app owns the menu itself."""
        self.menu_requested.emit(event.globalPos())
        event.accept()

    def closeEvent(self, event) -> None:
        self.remember_position()
        super().closeEvent(event)