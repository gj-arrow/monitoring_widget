# Drag-and-Drop Widget Movement — Design Spec

## Overview

Add smooth drag-and-drop movement to the system monitor widget. Left-click and hold to grab the widget, it follows the cursor during drag, and on release it stays at the new position within the current monitor boundaries.

## Approach

Approach A: mouse tracking with drag mode flag. Simple, smooth, minimal changes to `overlay.py`.

## Architecture Changes

| File | Change |
|------|--------|
| `overlay.py` | Add drag mode, mouse tracking, move logic, visual feedback |
| `main.py` | No changes |

## State

### New attributes on `DraggableLabel`

- `_drag_mode: bool` — whether drag is active (default `False`)
- `_drag_offset: tuple[int, int] | None` — cursor offset from widget top-left during drag (default `None`)

## Events

### `mousePressEvent(event)`

1. If left button and not already in drag mode:
   - Set `_drag_mode = True`
   - Calculate `_drag_offset = (int(event.position().x()), int(event.position().y()))`
   - Set `setMouseTracking(True)`
   - Apply visual feedback: add raised frame border via `setFrameStyle(QFrame.Shape.Box | QFrame.Style.Shadow.Raised)`, change cursor to `ClosedHandCursor`

### `mouseMoveEvent(event)`

1. If in drag mode:
   - Get current screen via `QGuiApplication.screenAt(event.globalPosition())`
   - Compute target position: `global_pos - QPoint(*_drag_offset)`
   - Clamp to monitor geometry: `x` clamped to `[screen.left(), screen.right() - widget.width()]`, `y` clamped to `[screen.top(), screen.bottom() - widget.height()]`
   - Move widget: `self.move(clamped_pos)`

### `mouseReleaseEvent(event)`

1. If left button and in drag mode:
   - Set `_drag_mode = False`
   - Set `setMouseTracking(False)`
   - Restore frame style: remove border via `setFrameStyle(QFrame.Shape.NoFrame | QFrame.Style.Shadow.Plain)`
   - Change cursor back to `ArrowCursor`
   - Clear `_drag_offset = None`

## Visual Feedback

- **Cursor:** `ClosedHandCursor` during drag, `ArrowCursor` otherwise
- **Frame:** Box border with raised shadow during drag, no frame otherwise

## Constraints

- **No edge snapping** — widget stays exactly where released
- **Monitor-boundary clamping** — widget cannot be dragged outside the current monitor during drag
- **Instant placement** — no animation on release

## Error Handling

- No new error paths. Existing try/except in `_tick()` handles metric updates.
- Mouse events are simple coordinate math; exceptions unlikely.
- Screen geometry lookup via `QGuiApplication.screenAt()` returns `None` if cursor is between monitors — fall back to `QGuiApplication.primaryScreen()` geometry.

## Dependencies

- New import: `from PyQt6.QtGui import QGuiApplication` (for `screenAt()`)
- Existing imports in `overlay.py` already include `QPoint`, `Qt`, `QMouseEvent`

## Testing

Manual testing only — move widget to various screen positions including monitor edges, verify smooth follow, boundary clamping, and correct release position.
