"""Точка входа для системного монитора."""
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import os
import sys
import logging

logging.getLogger().setLevel(logging.DEBUG)
for h in logging.getLogger().handlers:
    h.setLevel(logging.DEBUG)

# Настройка логирования ошибок в app_debug.log
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_debug.log"),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Добавляем лог о запуске приложения
logging.info("Приложение MonitorApp запускается...")

# Add the current script's directory to sys.path to ensure imports work from any location
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont

import random
import metrics as m
from overlay import DraggableLabel

CPU_GREEN = "#33ff99"
GPU_PURPLE = "#d79eff"
VRAM_BLUE = "#82aaff"
RAM_ORANGE = "#f78c6c"
ALERT_RED = "#ff3333"
WIDGET_ALPHA_PERCENT = 0

class MonitorApp:
    """Запуск и поддержка жизненного цикла приложения."""

    def __init__(self) -> None:
        self.app = QApplication(sys.argv)
        self.random_mode = False
        self.current_random_color = "#FFFFFF"

        wid = DraggableLabel(
            on_click_callback=self._handle_single_click,
            on_double_click_callback=self._handle_double_click
        )
        wid.bg_alpha = int(WIDGET_ALPHA_PERCENT * 2.55)

        # Set font and style - text stays bright always
        font = QFont("Consolas", 12, QFont.Weight.Bold)
        wid.setFont(font)
        self.widget_alpha = WIDGET_ALPHA_PERCENT

        # Setup timer for updates
        timer = QTimer(self.app)
        timer.timeout.connect(lambda: self._tick(wid))
        timer.start(2000)

        self._tick(wid)
        wid.adjustSize()
        
        screen = wid.screen().availableGeometry()
        margin = 10
        # Position in top-right corner with margin
        x = screen.right() - wid.width() - margin
        y = screen.top() + margin
        wid.move(x, y)
        
        wid.show()
        
        wid.show()

    def _handle_single_click(self) -> None:
        logging.info("_handle_single_click START")
        try:
            self.random_mode = True
            self.current_random_color = f"#{random.randint(0, 0xFFFFFF):06x}"
            logging.info("_handle_single_click OK, color=%s", self.current_random_color)
        except Exception as e:
            logging.error("_handle_single_click ERROR: %s", e, exc_info=True)

    def _handle_double_click(self) -> None:
        logging.info("_handle_double_click START")
        try:
            self.random_mode = False
            self.current_random_color = "#FFFFFF"
            logging.info("_handle_double_click OK")
        except Exception as e:
            logging.error("_handle_double_click ERROR: %s", e, exc_info=True)

    def _get_color(self, value: float, threshold: float = 80.0) -> str:
        return ALERT_RED if value >= threshold else ""

    def update_transparency(self, alpha_percent: float) -> None:
        """Update the background transparency percentage (0-100)."""
        self.widget_alpha = max(0.0, min(100.0, alpha_percent))
        bg_value = int(self.widget_alpha * 2.55)
        for widget in QApplication.allWidgets():
            if isinstance(widget, DraggableLabel):
                widget.bg_alpha = bg_value
                break

    def _tick(self, wid: DraggableLabel) -> None:
        try:
            cpu = float(m.cpu_pct())
            gpu_u = float(m.gpu_utilization())
            gpu_uv = float(m.gpu_vram_used_gb())
            gpu_tv = float(m.gpu_vram_total_gb())
            ram_u = float(m.ram_used_gb())
            ram_t = float(m.ram_total_gb())
            gpu_temp = float(m.gpu_temp())

            def get_element_color(base_color: str, value_percent: float) -> str:
                if self._get_color(value_percent):
                    return ALERT_RED
                if self.random_mode:
                    return self.current_random_color
                return base_color

            cpu_color = get_element_color(CPU_GREEN, cpu)
            gpu_color = get_element_color(GPU_PURPLE, gpu_u)
            vram_color = get_element_color(GPU_PURPLE, (gpu_uv / gpu_tv) * 100 if gpu_tv > 0 else 0)
            ram_color = get_element_color(RAM_ORANGE, (ram_u / ram_t) * 100 if ram_t > 0 else 0)

            html = (
                f"<span style='color:{cpu_color}'>CPU </span>"
                f"<span style='font-weight:bold;color:{cpu_color}'>{cpu}%</span><br/>"
                f"<span style='color:{ram_color}'>RAM </span>"
                f"<span style='font-weight:bold;color:{ram_color}'>{ram_u}</span>"
                f"<span style='color:{ram_color}'>/{ram_t}GB</span><br/>"
                f"<span style='color:{gpu_color}'>GPU </span>"
                f"<span style='font-weight:bold;color:{gpu_color}'>{gpu_u}% {gpu_temp}°C</span><br/>"
                f"<span style='color:{vram_color}'>VRAM </span>"
                f"<span style='font-weight:bold;color:{vram_color}'>{gpu_uv}</span>"
                f"<span style='color:{vram_color}'>/{gpu_tv}GB</span>"
            )
            wid.setText(html)
            wid.adjustSize() 
        except Exception as e:
            logging.error(f"Error updating metrics: {e}", exc_info=True)

    def run(self) -> None:
        sys.exit(self.app.exec())


if __name__ == "__main__":
    MonitorApp().run()
