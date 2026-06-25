import sys
import os

# Add current directory to path to allow imports from widget/
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtCore import Qt
from widget.main import MonitorApp

def main():
    app = QApplication(sys.argv)
    window = MonitorApp()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
