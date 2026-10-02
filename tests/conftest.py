"""Test bootstrap: project on sys.path and one shared QApplication.

The offscreen platform is deliberately NOT used. Under it Qt on Windows has
no font database at all: QFontInfo resolves every family to an empty string
and all text rasterises as tofu boxes. The painter tests compare golden
images, so they would lock in an unrenderable picture. Setting
QT_QPA_FONTDIR does not rescue it either: only 59 families become available
and Segoe UI Variable Display is not among them.

Nothing here shows a window: the painter tests draw into a QImage, and the
overlay tests never call show(), so a test run leaves no window on screen.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
    del os.environ["QT_QPA_PLATFORM"]


@pytest.fixture(scope="session", autouse=True)
def qapp():
    """One QApplication for the whole run, with a real font database."""
    from PyQt6.QtGui import QFont, QFontInfo
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    resolved = QFontInfo(QFont("Segoe UI Variable Display")).family()
    assert resolved, (
        "no font database: the golden images would be tofu. "
        "Check that QT_QPA_PLATFORM is not 'offscreen'."
    )
    yield app
