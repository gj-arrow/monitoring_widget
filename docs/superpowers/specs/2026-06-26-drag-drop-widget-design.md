# Drag-and-Drop Widget Movement — Design Spec

## Overview

Add smooth drag-and-drop movement to the system monitor widget. Left-click and hold to grab the widget, it follows the cursor, and on release it stays at the new position.

## Approach

Approach 3: mouse tracking with drag mode flag. Simple, smooth, minimal changes.

## Architecture Changes

| File | Change |
|------|--------|
| `overlay.py` | Add drag mode, mouse tracking, move logic |
| `main.py` | No changes |

## State

### New attributes on `DraggableLabel`

- `_drag_mode: bool` — whether drag is active (default `False`)
- `_drag_offset: tuple[int, int] | None` — cursor offset from widget top-left during drag (default `None`)

## Events

### `mousePressEvent(event)`

1. If left button and not already in drag mode:
   - Set `_drag_mode = True`
   - Calculate `_drag_offset = (event.position().x(), event.position().y())`
   - Set `setMouseTracking(True)`
   - Apply visual feedback: reduce opacity to 50%, change cursor to `ClosedHandCursor`

### `mouseMoveEvent(event)`

1. If in drag mode:
   - Move widget: `self.move(event.globalPosition().toPoint() - QPoint(*self._drag_offset))`

### `mouseReleaseEvent(event)`

1. If left button and in drag mode:
   - Set `_drag_mode = False`
   - Set `setMouseTracking(False)`
   - Restore opacity to original value
   - Change cursor back to `ArrowCursor`
   - Clear `_drag_offset = None`

## Visual Feedback

- **Cursor:** `ClosedHandCursor` during drag, `ArrowCursor` otherwise
- **Opacity:** 50% background during drag, original value otherwise

## Error Handling

- No new error paths. Existing try/except in `_tick()` handles metric updates.
- Mouse events are simple coordinate math; exceptions unlikely.

## Testing

Manual testing only — move widget to various screen positions, verify smooth follow and correct release position.
