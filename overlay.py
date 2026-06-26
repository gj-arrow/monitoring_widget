import logging
from PyQt6.QtWidgets import QWidget, QFrame, QLabel, QApplication
from PyQt6.QtCore import Qt, QSize, QPoint, QTimer
from PyQt6.QtGui import QMouseEvent, QFont

logger = logging.getLogger('widget.overlay')


class DraggableLabel(QWidget):
    def __init__(self, on_click_callback=None, on_double_click_callback=None) -> None:
        super().__init__()
        self.on_click_callback = on_click_callback
        self.on_double_click_callback = on_double_click_callback
        self._margin = 7

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Background frame — fills entire widget
        self._bg_frame = QFrame(self)
        self._bg_frame.setObjectName("BgFrame")

        # Label on top of background, no layout needed
        self._label = QLabel(self)
        self._label.setStyleSheet("background-color: transparent; color: white;")
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._bg_alpha_percent = 40.0
        self._update_bg_style()
        logger.info("DraggableLabel created, bg_alpha_initial=%.1f", self._bg_alpha_percent)

        # Drag-and-drop state
        self._drag_mode = False
        self._drag_enabled = False
        self._drag_offset: QPoint | None = None
        self._right_timer = QTimer(self)
        self._right_timer.setSingleShot(True)
        self._right_timer.timeout.connect(self._on_right_click_timeout)
        self._click_counter = 0

        self.setMouseTracking(True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        try:
            btn = event.button()
            if btn == Qt.MouseButton.MiddleButton:
                QApplication.quit()
                return
            elif btn == Qt.MouseButton.RightButton:
                if self._right_timer.isActive():
                    self._right_timer.stop()
                    if self.on_double_click_callback:
                        self.on_double_click_callback()
                else:
                    self._right_timer.start(300)
                return
            elif btn == Qt.MouseButton.LeftButton:
                self._click_counter += 1

                if self._click_counter == 1:
                    self._double_click_timer = QTimer(self)
                    self._double_click_timer.setSingleShot(True)
                    self._double_click_timer.timeout.connect(self._clear_click_counter)
                    self._double_click_timer.start(300)
                elif self._click_counter >= 2:
                    self._double_click_timer.stop()
                    self._double_click_timer.deleteLater()
                    self._click_counter = 0
                    self._drag_enabled = not self._drag_enabled
                    self._update_drag_hint_style()
                    if self._drag_enabled:
                        self._drag_mode = True
                        self._drag_offset = event.pos()
                        self.setCursor(Qt.CursorShape.ClosedHandCursor)
                        a = int(max(0, min(255, self._bg_alpha_percent * 2.55)))
                        self._bg_frame.setStyleSheet(f"background-color: rgba(0,0,139,{a}); border: 2px solid #6496ff; border-radius: 8px;")
                        logger.info("Drag mode: enabled (double-click)")
                    else:
                        logger.info("Drag mode: disabled (double-click)")
                    return

                if self._drag_enabled:
                    self._drag_mode = True
                    self._drag_offset = event.pos()
                    self.setCursor(Qt.CursorShape.ClosedHandCursor)
                    a = int(max(0, min(255, self._bg_alpha_percent * 2.55)))
                    self._bg_frame.setStyleSheet(f"background-color: rgba(0,0,139,{a}); border: 2px solid #6496ff; border-radius: 8px;")
        except Exception as e:
            logger.error("mousePressEvent error: %s", e, exc_info=True)
        super().mousePressEvent(event)
        event.accept()

    def _on_right_click_timeout(self) -> None:
        if self.on_click_callback:
            self.on_click_callback()

    def _clear_click_counter(self) -> None:
        self._click_counter = 0

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode and self._drag_offset is not None:
            screen = self.screen().availableGeometry()
            new_x = max(screen.left(), min(int(event.globalPosition().x()), screen.right() - self.width()))
            new_y = max(screen.top(), min(int(event.globalPosition().y()), screen.bottom() - self.height()))
            self.move(new_x, new_y)
        super().mouseMoveEvent(event)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_mode:
            self._drag_mode = False
            self._drag_offset = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._update_drag_hint_style()
        super().mouseReleaseEvent(event)
        event.accept()

    def wheelEvent(self, event: QMouseEvent) -> None:
        delta = event.angleDelta().y()
        change = 5.0 if delta > 0 else -5.0
        self._bg_alpha_percent = max(0.0, min(100.0, self._bg_alpha_percent + change))
        alpha_value = int(self._bg_alpha_percent * 2.55)
        self.bg_alpha = alpha_value
        event.accept()

    def sizeHint(self) -> QSize:
        return QSize(160, 90)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        h = self.height()
        m = self._margin
        # bg fills entire widget
        self._bg_frame.setGeometry(0, 0, w, h)
        # label sits inset by margin
        lw = max(10, w - 2 * m)
        lh = max(10, h - 2 * m)
        self._label.setGeometry(m, m, lw, lh)

    def adjustSize(self):
        old_w, old_h = self.width(), self.height()
        # force text to measure at sensible size
        self._label.setMinimumWidth(80)
        super().adjustSize()
        nw, nh = self.width(), self.height()
        self.setFixedSize(nw, nh)
        logger.debug("adjustSize: %dx%d -> %dx%d (fixed)", old_w, old_h, nw, nh)

    @property
    def bg_alpha(self) -> int:
        return int(self._bg_alpha_percent * 2.55)

    @bg_alpha.setter
    def bg_alpha(self, value: int) -> None:
        self._bg_alpha_percent = max(0.0, min(100.0, (value / 2.55)))
        self._update_bg_style()
        logger.info("bg_alpha changed to %d (%% %.1f)", value, self._bg_alpha_percent)

    def _update_bg_style(self) -> None:
        alpha_value = int(max(0, min(255, self._bg_alpha_percent * 2.55)))
        style = f"background-color: rgba(0, 0, 139, {alpha_value}); border-radius: 8px;"
        self._bg_frame.setStyleSheet(style)
        logger.debug("BgFrame stylesheet updated: alpha_val=%d, pct=%.1f", alpha_value, self._bg_alpha_percent)

    def _update_drag_hint_style(self) -> None:
        """Update border style to indicate drag mode is enabled/disabled."""
        alpha_value = int(max(0, min(255, self._bg_alpha_percent * 2.55)))
        if self._drag_enabled:
            style = f"background-color: rgba(0, 0, 139, {alpha_value}); border: 2px solid #ffaa00; border-radius: 8px;"
        else:
            style = f"background-color: rgba(0, 0, 139, {alpha_value}); border-radius: 8px;"
        self._bg_frame.setStyleSheet(style)

    def setText(self, html: str) -> None:
        self._label.setText(html)
        logger.debug("Label text set, length=%d", len(html))

    def setFont(self, font: QFont) -> None:
        self._label.setFont(font)
        logger.info("Label font set: %s %dpt", font.family(), font.pointSize())
