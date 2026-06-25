# Drag-and-Drop Widget Movement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add smooth left-click drag-and-drop movement to the system monitor widget.

**Architecture:** Add drag mode state and mouse tracking to `DraggableLabel`. Left-click enters drag mode, widget follows cursor, release locks position.

**Tech Stack:** PyQt6 (Qt, QWidget, QMouseEvent, QPoint, CursorShape)

## Global Constraints

- Follow existing code style (Russian comments, `self._` prefix for private attributes)
- No new dependencies
- Existing mouse events (middle click, right click, double click, wheel) must continue working
- Call `wid.adjustSize()` after UI changes per AGENTS.md gotchas

---

### Task 1: Add drag mode to DraggableLabel

**Files:**
- Modify: `overlay.py:9-111`

**Interfaces:**
- Consumes: existing `DraggableLabel` class
- Produces: `_drag_mode` flag, `_drag_offset` state, drag methods, mouse event handlers

- [ ] **Step 1: Add drag state attributes and imports**

Add to `__init__` after line 14 (`self._margin = 7`):

```python
import logging
from PyQt6.QtWidgets import QWidget, QFrame, QLabel, QApplication
from PyQt6.QtCore import Qt, QSize, QPoint
from PyQt6.QtGui import QMouseEvent, QFont, QCursor

# ... (logger stays the same)

class DraggableLabel(QWidget):
    def __init__(self, on_click_callback=None, on_double_click_callback=None) -> None:
        super().__init__()
        self.on_click_callback = on_click_callback
        self.on_double_click_callback = on_double_click_callback
        self._margin = 7
        self._drag_mode = False
        self._drag_offset: QPoint | None = None
        self._original_bg_alpha_percent = 40.0
```

Also add `QPoint` to the import from `PyQt6.QtCore` on line 3 (add `QPoint` to the existing import).
Also add `QCursor` to the import from `PyQt6.QtGui` on line 4 (add `QCursor` to the existing import).

- [ ] **Step 2: Add drag control methods**

Add these methods to `DraggableLabel` class (after `wheelEvent`, around line 111):

```python
    def _enter_drag_mode(self, event: QMouseEvent) -> None:
        """Enter drag mode, save offset, apply visual feedback."""
        self._drag_mode = True
        self._drag_offset = event.position().toPoint() - self.pos()
        self.setMouseTracking(True)
        self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
        # Reduce opacity to 50%
        self._bg_alpha_percent = self._original_bg_alpha_percent * 0.5
        self._update_bg_style()
        logger.info("Drag mode entered, offset=%s", self._drag_offset)

    def _exit_drag_mode(self) -> None:
        """Exit drag mode, restore visual state."""
        self._drag_mode = False
        self._drag_offset = None
        self.setMouseTracking(False)
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        # Restore original opacity
        self._bg_alpha_percent = self._original_bg_alpha_percent
        self._update_bg_style()
        logger.info("Drag mode exited")

    def _update_bg_style(self) -> None:
        """Update background frame style with current alpha."""
        alpha_value = int(max(0, min(255, self._bg_alpha_percent * 2.55)))
        style = f"background-color: rgba(0, 0, 139, {alpha_value}); "
        self._bg_frame.setStyleSheet(style)
        logger.debug("BgFrame stylesheet updated: alpha_val=%d, pct=%.1f", alpha_value, self._bg_alpha_percent)
```

- [ ] **Step 3: Modify mousePressEvent for drag trigger**

Replace the existing `mousePressEvent` (lines 84-95) with:

```python
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
        elif event.button() == Qt.MouseButton.LeftButton and not self._drag_mode:
            self._enter_drag_mode(event)
        super().mousePressEvent(event)
```

- [ ] **Step 4: Add mouseMoveEvent**

Add after `mouseDoubleClickEvent` (after line 104):

```python
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode and self._drag_offset is not None:
            new_pos = event.globalPosition().toPoint() - self._drag_offset
            self.move(new_pos)
```

- [ ] **Step 5: Add mouseReleaseEvent**

Add after `mouseMoveEvent`:

```python
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode and event.button() == Qt.MouseButton.LeftButton:
            self._exit_drag_mode()
        super().mouseReleaseEvent(event)
```

- [ ] **Step 6: Run the app to verify manually**

Run: `.venv\Scripts\python main.py`

Expected:
- Widget starts at top-right
- Left-click + drag: widget follows cursor smoothly, becomes semi-transparent, cursor changes to hand
- Release: widget stays at new position, opacity restores, cursor returns to arrow
- Right-click still toggles random color
- Middle-click still quits
- Scroll wheel still adjusts transparency

- [ ] **Step 7: Commit**

```bash
git add overlay.py
git commit -m "feat: add left-click drag-and-drop widget movement"
```
