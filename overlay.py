import logging
from PyQt6.QtWidgets import QWidget, QFrame, QLabel, QApplication
from PyQt6.QtCore import Qt, QSize
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

        self._bg_alpha_percent = 40.0
        self._update_bg_style()
        logger.info("DraggableLabel created, bg_alpha_initial=%.1f", self._bg_alpha_percent)

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
        style = f"background-color: rgba(0, 0, 139, {alpha_value}); "
        self._bg_frame.setStyleSheet(style)
        logger.debug("BgFrame stylesheet updated: alpha_val=%d, pct=%.1f", alpha_value, self._bg_alpha_percent)

    def setText(self, html: str) -> None:
        self._label.setText(html)
        logger.debug("Label text set, length=%d", len(html))

    def setFont(self, font: QFont) -> None:
        self._label.setFont(font)
        logger.info("Label font set: %s %dpt", font.family(), font.pointSize())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            logger.info("Middle click detected - quitting")
            QApplication.quit()
        elif event.button() == Qt.MouseButton.RightButton:
            logger.info("Right click detected - calling callback")
            if self.on_click_callback:
                try:
                    self.on_click_callback()
                except Exception as e:
                    logger.error("on_click_callback error: %s", e)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        logger.info("Double right click detected")
        if self.on_double_click_callback:
            try:
                self.on_double_click_callback()
            except Exception as e:
                logger.error("on_double_click_callback error: %s", e)
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QMouseEvent) -> None:
        delta = event.angleDelta().y()
        change = 5.0 if delta > 0 else -5.0
        self._bg_alpha_percent = max(0.0, min(100.0, self._bg_alpha_percent + change))
        alpha_value = int(self._bg_alpha_percent * 2.55)
        self.bg_alpha = alpha_value
