# System Monitor — Agent Instructions

## Project Structure
- **Root files**: `main.py` (app + tray menu + collector thread), `metrics.py` (retrieval), `theme.py` (design tokens), `painter.py` (pure renderer), `history.py` (ring buffer + CSV), `overlay.py` (panel window), `settings.py` (persisted preferences), `tests/`
- **PyInstaller**: `Monitor.spec` is the build; `build_exe.bat` only invokes it. Do not add a second description of the build — a hand-rolled `--add-data` list beside the spec is how `overlay.py` and `metrics.py` came to be copied next to the exe as inert data files.
- All Python modules live at root, not in a subdirectory.

## Running
- **Setup**: `python -m venv .venv && .venv\Scripts\activate && pip install -r requirements-dev.txt` (runtime deps only: `pip install -r requirements.txt`)
- **Run**: `python main.py`
- **Build exe**: `build_exe.bat` → outputs `dist\Monitor.exe`
- **Dependencies**: `psutil`, `PyQt6`, `pynvml` (NVIDIA), `wmi` (GPU fallback, CPU clock, perf counters)

## Key Gotchas
- **Sampling is off the GUI thread**: `main.py`'s `Collector` is a `QThread`. The CPU clock costs ~140 ms of process CPU per sample (~7% of a core) and ~530 ms of wall time; the wall figure is mostly the thread *blocked* in the WMI provider, not computing. That is affordable because `ts` is stamped after the sample, so nothing is labelled fresher than it is.
- **Every WMI query must be a WQL projection.** `wmi`'s per-class convenience wrapper pulls every property of every instance — `Win32_Processor` cost 1044 ms that way against 6 ms projected. A projection naming a property the repository lacks is rejected outright.
- **Counter readings need range checks.** Perf counters hand over whatever they were handed: this code once rendered `255` as 11.48 GHz on a 4.5 GHz part.
- **`_reading(value, floor=, ceiling=)` is the one gate, and it covers only the CPU clock** — three call sites, all in `CpuClockProbe`: the nominal from WMI, the nominal from the psutil fallback, and the performance ratio. It requires finite and strictly positive, with the optional bounds named at the call site. The bounds in force: `_PERF_RATIO_MIN = 10` and `_PERF_RATIO_MAX = 200` on `PercentProcessorPerformance` (a percentage of nominal; 100 is nominal, above it is turbo, and nothing clocks a CPU at twice nominal), and `_NOMINAL_MAX_MHZ = 20_000` on the nominal clock (the fastest core in production is ~5.7 GHz). A row outside the ratio band is skipped, not voided, so one garbled core does not cost the others.
- **What `_reading()` does *not* cover**, so the gaps are known rather than assumed: net rates, GPU readings (`GpuProbe.read`), and the RAM/CPU percentages (`SystemProbe.sample`) all bypass it. Those sources are trusted rather than validated — psutil and NVML return percentages, not raw counters. The one derived value with no bound at all is the net rate, which is why `_format_rate` has a saturating arm at one terabyte instead.
- **A cumulative counter's discontinuity is `None`, never a number.** In both directions: a counter that goes backwards (adapter re-enumerated) and an adapter set that changed (one appeared carrying its boot-time total, or one vanished leaving a missing term). Both unmeasure that tick *and* drop the EMA average, or a pre-event peak blends into the next real reading at 0.35/0.65 and decays over five ticks. The set comparison comes **before** the elapsed-time guard, because an adapter arriving on a backwards clock is still a churn — and a non-churn tick with no elapsed time keeps its average, since nothing discontinuous happened.
- **Only the per-processor perf rows are averaged.** `_Total` is a summary *of* them and reads 100 on an idle machine, so using it as a substitute republishes the nominal as the live clock.
- **psutil `cpu_freq()` is the nominal clock on Windows, not the live one** — three consecutive calls returned `current == max == 4501.0`. Never publish it as a live reading.
- **GPU metrics**: Require NVIDIA GPU. Falls back to WMI heuristics which are approximate.
- **sys.path**: `main.py` appends its own directory, and `tests/conftest.py` puts the root on the path for the suite. If adding modules, ensure they're importable from root.
- **UI sizing**: there is nothing to size. `painter.py` is a pure function from snapshot to `QPainter` commands and `overlay.py` hands it a fixed-size canvas, so there is no widget whose text can clip and no `adjustSize()` to call. A metric that could not be measured renders `--`, which is a fixed-width string; a metric that was measured renders its own digits.
- **Mouse controls**: drag moves the panel, left double-click snaps it back to the top-right corner, wheel adjusts opacity, right-click opens the tray menu, middle-click quits. A tray double-click also snaps it back.
- **Colour encodes load, and only load**: a row is calm blue until it crosses a threshold, so there is no random-colour mode to preserve and none to reintroduce. Three states, not two — percentages and RAM/VRAM warn at 80 and go critical at 95, GPU temperature warns at 70 °C and goes critical at 80 °C, and CPU frequency carries no state at all because it is an auxiliary reading under CPU. An unmeasurable metric is `--`, never a guess.
- **Logging**: `app_debug.log` is rotating (512 KB, 2 backups) and carries lifecycle and errors only; the per-metric probe chatter is filtered to WARNING and above. `metrics_history.log` is an opt-in CSV trace, off by default, toggled from the tray menu. Both sit beside the executable, not in the working directory, and both are gitignored.
- **Window**: Frameless, translucent background, stays on top, positioned top-right of screen.
- **Tests**: never run under `QT_QPA_PLATFORM=offscreen` — `tests/conftest.py` strips it, because Qt on Windows has no font database there and every glyph rasterises as tofu, which the golden images would lock in.

## Adding Metrics
Four places, and all four or the trace and the panel disagree:
1. `metrics.py` — the retrieval, plus a field on the frozen `Snapshot`.
2. `Snapshot.CSV_COLUMNS` — `test_csv_columns_covers_every_declared_field` fails if a declared field is missing from it, and every value is read back **by declared index** by `test_every_csv_column_holds_the_value_that_names_it`, so the row and the header cannot drift apart.
3. `theme.py` — a `MetricSpec` in `METRICS` if the row is a *metric* (i.e. it has warn/critical thresholds and gets coloured). Something informational belongs in the header instead and must stay out of `has_any_data()`, or it lights the status dot.
4. `painter.py` — the row's `_format_value` / `_format_aux`, or `_format_net` for a header reading.

Regenerate the goldens after any renderer change and **look at them**:
`$env:MONITOR_REGEN_GOLDEN=1; python -m pytest tests/test_painter.py` (PowerShell — the
`VAR=1 cmd` form in the test docstrings is bash and does not work here).

## Build Artifacts
- `dist/`, `build/`, `*.log`, `.venv/`, `.idea/` are all gitignored.
