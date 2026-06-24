# Agent instructions for the System Monitor project

## Project Structure
- Located in `widget/` directory.
- `main.py`: Entry point (PyQt6).
- `metrics.py`: Retrieval logic for CPU, RAM, and GPU metrics.
- `overlay.py`: UI component (`DraggableLabel`).

## Running and Testing
- **Execution**: Run from project root using `.venv\Scripts\python widget/main.py`.
- **Environment**: Uses a local virtual environment in `widget/.venv/`.
- **Dependencies**: Requires `psutil`, `PyQt6`, `pynvml` (NVIDIA), and `wmi` (fallback).

## Key Developer Notes
- **Logging**: 
  - Application errors and startup info are logged to `app_debug.log`.
  - Metric history is recorded in `metrics_history.log`.
- **Pathing**: `main.py` modifies `sys.path` to allow imports from the `widget/` subdirectory.
- **UI**: 
  - Always call `wid.adjustSize()` after updating text to prevent clipping.
  - Right-click (single click) toggles random color mode.
  - Double-click reverts to default colors.
  - Middle-click quits the application.
- **Maintenance**: When adding new metrics, update both retrieval logic in `metrics.py` and the HTML template in `main.py`.
