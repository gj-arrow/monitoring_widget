# System Monitor — Agent Instructions

## Project Structure
- **Root files**: `main.py` (entry point), `metrics.py` (metric retrieval), `overlay.py` (UI widget), `run_widget.py` (alternate launcher)
- **PyInstaller**: `build_exe.bat` (quick build), `Monitor.spec` / `spec_build.py` (build specs)
- All Python modules live at root, not in a subdirectory.

## Running
- **Setup**: `python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt`
- **Run**: `.venv\Scripts\python main.py` (or `run_widget.py`)
- **Build exe**: `build_exe.bat` → outputs `dist\Monitor.exe`
- **Dependencies**: `psutil`, `PyQt6`, `pynvml` (NVIDIA), `wmi` (fallback)

## Key Gotchas
- **GPU metrics**: Require NVIDIA GPU. Falls back to WMI heuristics which are approximate.
- **sys.path**: `main.py` appends its own directory; `run_widget.py` does the same. If adding modules, ensure they're importable from root.
- **UI sizing**: Always call `wid.adjustSize()` after updating text to prevent clipping.
- **Mouse controls**: Right-click toggles random color, double-click reverts, middle-click quits, scroll wheel adjusts background transparency.
- **Logging**: `app_debug.log` for errors/startup, `metrics_history.log` for metric history (both gitignored).
- **Window**: Frameless, translucent background, stays on top, positioned top-right of screen.

## Adding Metrics
Update `metrics.py` (retrieval) and the HTML template in `main.py` `_tick()` method simultaneously.

## Build Artifacts
- `dist/`, `build/`, `*.log`, `.venv/`, `.idea/` are all gitignored.
