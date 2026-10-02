# System Monitor — Agent Instructions

## Project Structure
- **Root files**: `main.py` (app + collector thread), `metrics.py` (retrieval), `theme.py` (design tokens), `painter.py` (pure renderer), `history.py` (ring buffer + CSV), `overlay.py` (UI widget), `run_widget.py` (alternate launcher)
- **PyInstaller**: `build_exe.bat` (quick build), `Monitor.spec` / `spec_build.py` (build specs)
- All Python modules live at root, not in a subdirectory.

## Running
- **Setup**: `python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt`
- **Run**: `.venv\Scripts\python main.py` (or `run_widget.py`)
- **Build exe**: `build_exe.bat` → outputs `dist\Monitor.exe`
- **Dependencies**: `psutil`, `PyQt6`, `pynvml` (NVIDIA), `wmi` (GPU fallback, CPU clock, perf counters)

## Key Gotchas
- **Sampling is off the GUI thread**: `main.py`'s `Collector` is a `QThread`. The CPU clock costs ~140 ms of process CPU per sample (~7% of a core) and ~530 ms of wall time; the wall figure is mostly the thread *blocked* in the WMI provider, not computing. That is affordable because `ts` is stamped after the sample, so nothing is labelled fresher than it is.
- **Every WMI query must be a WQL projection.** `wmi`'s per-class convenience wrapper pulls every property of every instance — `Win32_Processor` cost 1044 ms that way against 6 ms projected. A projection naming a property the repository lacks is rejected outright.
- **Counter readings need range checks.** Perf counters hand over whatever they were handed: this code once rendered `255` as 11.48 GHz on a 4.5 GHz part. See `_speed()` and `_PERF_RATIO_MAX`.
- **psutil `cpu_freq()` is the nominal clock on Windows, not the live one** — three consecutive calls returned `current == max == 4501.0`. Never publish it as a live reading.
- **GPU metrics**: Require NVIDIA GPU. Falls back to WMI heuristics which are approximate.
- **sys.path**: `main.py` appends its own directory; `run_widget.py` does the same. If adding modules, ensure they're importable from root.
- **UI sizing**: Always call `wid.adjustSize()` after updating text to prevent clipping.
- **Mouse controls**: Right-click toggles random color, double-click reverts, middle-click quits, scroll wheel adjusts background transparency.
- **Logging**: `app_debug.log` for errors/startup, `metrics_history.log` for metric history (both gitignored).
- **Window**: Frameless, translucent background, stays on top, positioned top-right of screen.
- **Tests**: never run under `QT_QPA_PLATFORM=offscreen` — `tests/conftest.py` strips it, because Qt on Windows has no font database there and every glyph rasterises as tofu, which the golden images would lock in.

## Adding Metrics
Four places, and all four or the trace and the panel disagree:
1. `metrics.py` — the retrieval, plus a field on the frozen `Snapshot`.
2. `Snapshot.CSV_COLUMNS` — `test_csv_columns_covers_every_declared_field` fails if a declared field is missing from it, and every value is read back **by declared index** by `test_every_csv_column_holds_the_value_that_names_it`, so the row and the header cannot drift apart.
3. `theme.py` — a `MetricSpec` in `METRICS` if the row is a *metric* (i.e. it has warn/critical thresholds and gets coloured). Something informational belongs in the header instead and must stay out of `has_any_data()`, or it lights the status dot.
4. `painter.py` — the row's `_format_value` / `_format_aux`, or `_format_net` for a header reading.

Regenerate the goldens after any renderer change and **look at them**:
`MONITOR_REGEN_GOLDEN=1 python -m pytest tests/test_painter.py`

## Build Artifacts
- `dist/`, `build/`, `*.log`, `.venv/`, `.idea/` are all gitignored.
