from PyQt6.QtWidgets import QLabel, QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QMouseEvent


class DraggableLabel(QLabel):
    def __init__(self, on_click_callback=None, on_double_click_callback=None) -> None:
        super().__init__()
        self.on_click_callback = on_click_callback
        self.on_double_click_callback = on_double_click_callback
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            QApplication.quit()
        elif event.button() == Qt.MouseButton.RightButton:
            if self.on_click_callback:
                self.on_click_callback()
        pass

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if self.on_double_click_callback:
            self.on_double_click_callback()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pass

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        pass

    def moveEvent(self, event: QMouseEvent) -> None:
        pass
