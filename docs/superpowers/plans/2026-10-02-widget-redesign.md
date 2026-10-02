# Widget Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current `QLabel`-with-HTML monitor overlay with a `QPainter`-rendered panel that matches the approved Glass A2 design, and fix the metric collection underneath it.

**Architecture:** Seven root-level modules with one-directional dependencies. `theme` and `history` import nothing from the project; `painter` is a pure function that turns a snapshot plus history into `QPainter` commands, so it is testable by drawing into a `QImage` with no window on screen; `metrics` owns a single NVML handle for the process lifetime; a `QThread` samples so the 100 ms `psutil` interval cannot stutter the GUI thread.

**Tech Stack:** Python 3.13.14 (system interpreter), PyQt6 6.11.0, psutil 7.2.2, pynvml, wmi, pytest.

## Global Constraints

- All Python modules live at the repository root, never in a subdirectory (`AGENTS.md`).
- Every field of `Snapshot` is `float | None`. `None` means "not measured" and renders as `--`. Never fabricate, estimate, or default a measurement to a number.
- Panel geometry is fixed: 280 wide, 280 tall in logical pixels. Row height 52, row gap 8, header 23, padding 13/12, graph strip 26 tall.
- Sizes are logical pixels with **no** manual DPI multiplier. Fonts are set in points so Qt scales them. `QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)` is called before `QApplication` is constructed.
- `QColor` in PyQt6 has no two-argument string constructor. Use `QColor(r, g, b, a)` or `QColor("#rrggbb").setAlpha(...)`.
- `QFont.setTabularNumbers` does not exist in PyQt6. Use `font.setFeature(QFont.Tag("tnum"), 1)`.
- Thresholds live only in `theme.py`: percentages warn at 80 and critical at 95; GPU temperature warns at 70 °C and critical at 80 °C; CPU frequency carries no state.
- Run everything with the system `python`. There is no `.venv` in this checkout despite what `AGENTS.md` says.
- Test command is `python -m pytest` from the repository root.
- Commit after every task. Do not amend a failed commit — fix and commit again.

## File Structure

| File | Responsibility |
|---|---|
| `theme.py` | Palette, thresholds, geometry, fonts. No project imports. |
| `history.py` | `History` ring buffer, `resample`, optional `HistoryLog` CSV writer. Imports only `theme`. |
| `settings.py` | `Settings` dataclass, JSON load/save next to the executable. No project imports. |
| `metrics.py` | `Snapshot`, `GpuProbe`, `SystemProbe`, WMI fallback. |
| `painter.py` | `paint(painter, snapshot, histories, alpha)`. Imports `theme`, `history`. |
| `overlay.py` | `MonitorPanel` window: placement, mouse, context-menu signal. Imports `painter`, `theme`, `settings`. |
| `main.py` | Logging, DPI, `Collector` thread, tray icon and menu, `main()`. |
| `tests/conftest.py` | Path setup, font-database guard, `qapp` fixture. |
| `tests/test_theme.py` | Threshold boundaries, geometry arithmetic. |
| `tests/test_history.py` | Ring buffer, clamping, resampling. |
| `tests/test_settings.py` | Defaults, round trip, corrupt input, write failure. |
| `tests/test_metrics.py` | NVML init count, value extraction, honest `None`. |
| `tests/test_painter.py` | Golden images, non-blank render, degenerate history. |
| `tests/golden/*.png` | Reference renders. Regenerated with `MONITOR_REGEN_GOLDEN=1`. |

---

### Task 1: Test harness and `theme.py`

`theme` is the module every other module reads from, and the threshold table is the one piece of
the design that can silently be wrong, so it goes first with its tests.

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_theme.py`
- Create: `theme.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `theme.NORMAL|WRAN|CRITICAL` (`0|1|2`), `theme.METRICS` tuple of `MetricSpec(key, label, kind, warn, critical)`, `theme.METRICS_BY_KEY` dict, `theme.WIDTH`, `theme.HEIGHT`, `theme.PAD_X`, `theme.PAD_TOP`, `theme.PAD_BOTTOM`, `theme.HEADER_H`, `theme.ROW_H`, `theme.ROW_GAP`, `theme.GRAPH_H`, `theme.PANEL_RADIUS`, `theme.ROW_RADIUS`, `theme.DOT_R`, `theme.CALM`, `theme.WARN_COLOR`, `theme.CRITICAL_COLOR`, `theme.NEUTRAL`, `theme.SUBTLE`, `theme.VALUE`, `theme.ROW_BG`, `theme.LABEL_PT`, `theme.VALUE_PT`, `theme.AUX_PT`, `theme.HISTORY_LEN`, `theme.TICK_MS`, `theme.MIN_ALPHA`, `theme.MAX_ALPHA`, `theme.DEFAULT_ALPHA`, `theme.state_for(value, warn, critical) -> int`, `theme.metric_state(spec, snapshot) -> int`, `theme.row_value(spec, snapshot) -> float | None`, `theme.row_fraction(spec, snapshot) -> float | None`, `theme.has_any_data(snapshot) -> bool`, `theme.worst_state(states) -> int`, `theme.state_color(state) -> QColor`, `theme.panel_rect() -> QRectF`, `theme.header_rect() -> QRectF`, `theme.metric_rects() -> tuple[tuple[MetricSpec, QRectF], ...]`, `theme.label_font()`, `theme.value_font()`, `theme.aux_font()`.

- [ ] **Step 1: Install pytest**

Run: `python -m pip install pytest`

Expected: `Successfully installed pytest-x.y.z` or `Requirement already satisfied: pytest`.

- [ ] **Step 2: Create `tests/conftest.py`**

```python
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
```

- [ ] **Step 3: Write the failing threshold tests in `tests/test_theme.py`**

```python
import pytest

import theme


@pytest.mark.parametrize(
    ("value", "warn", "critical", "expected"),
    [
        (None, 80.0, 95.0, theme.NORMAL),
        (0.0, 80.0, 95.0, theme.NORMAL),
        (79.9, 80.0, 95.0, theme.NORMAL),
        (80.0, 80.0, 95.0, theme.WARN),
        (94.9, 80.0, 95.0, theme.WARN),
        (95.0, 80.0, 95.0, theme.CRITICAL),
        (100.0, 80.0, 95.0, theme.CRITICAL),
    ],
)
def test_state_boundaries(value, warn, critical, expected):
    assert theme.state_for(value, warn, critical) == expected


def test_unmeasured_never_raises_an_alarm():
    assert theme.state_for(None, 70.0, 80.0) == theme.NORMAL


def test_every_metric_has_ordered_thresholds():
    for spec in theme.METRICS:
        assert spec.warn is not None, spec.key
        assert spec.critical is not None, spec.key
        assert spec.warn < spec.critical, spec.key


def test_metric_keys_are_the_documented_four():
    assert [m.key for m in theme.METRICS] == ["cpu_pct", "ram", "gpu_pct", "vram"]


def test_geometry_heights_add_up_to_280():
    expected = (
        theme.PAD_TOP
        + theme.HEADER_H
        + len(theme.METRICS) * theme.ROW_H
        + (len(theme.METRICS) - 1) * theme.ROW_GAP
        + theme.PAD_BOTTOM
    )
    assert theme.HEIGHT == expected == 280


def test_metric_rects_never_overlap():
    rects = [rect for _, rect in theme.metric_rects()]
    for upper, lower in zip(rects, rects[1:]):
        assert upper.bottom() < lower.top()


def test_metric_rects_fit_inside_the_panel():
    panel = theme.panel_rect()
    for _, rect in theme.metric_rects():
        assert rect.left() >= panel.left()
        assert rect.right() <= panel.right()
        assert rect.bottom() <= panel.bottom()


def test_graph_strip_is_shorter_than_a_row():
    assert theme.GRAPH_H < theme.ROW_H


def test_worst_state_picks_the_most_severe():
    assert theme.worst_state([theme.NORMAL, theme.WARN, theme.CRITICAL]) == theme.CRITICAL
    assert theme.worst_state([]) == theme.NORMAL


def test_state_colors_are_distinct():
    assert len({theme.state_color(s).name() for s in (theme.NORMAL, theme.WARN, theme.CRITICAL)}) == 3
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m pytest tests/test_theme.py -q`

Expected: collection error `ModuleNotFoundError: No module named 'theme'`.

- [ ] **Step 5: Write `theme.py`**

```python
"""Design tokens for the monitor panel.

Single source of truth for palette, thresholds, geometry and typography.
Imports nothing from the project, so both the renderer and its tests can use
it without pulling in widgets.

Sizes are logical pixels with no manual DPI multiplier: Qt 6 applies
per-monitor scaling itself. Fonts are in points so they scale with the panel
instead of staying pinned to the pixel grid.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QColor, QFont

NORMAL = 0
WARN = 1
CRITICAL = 2

PCT = "pct"
GB = "gb"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    key: str
    label: str
    kind: str
    warn: float | None
    critical: float | None


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("cpu_pct", "CPU", PCT, 80.0, 95.0),
    MetricSpec("ram", "RAM", GB, 80.0, 95.0),
    MetricSpec("gpu_pct", "GPU", PCT, 80.0, 95.0),
    MetricSpec("vram", "VRAM", GB, 80.0, 95.0),
)

METRICS_BY_KEY = {m.key: m for m in METRICS}

GPU_TEMP_WARN = 70.0
GPU_TEMP_CRITICAL = 80.0

WIDTH = 280
PAD_X = 14
PAD_TOP = 13
PAD_BOTTOM = 12
HEADER_H = 23
ROW_H = 52
ROW_GAP = 8
GRAPH_H = 26

PANEL_RADIUS = 14.0
ROW_RADIUS = 7.0
DOT_R = 3.0

HEIGHT = (
    PAD_TOP
    + HEADER_H
    + len(METRICS) * ROW_H
    + (len(METRICS) - 1) * ROW_GAP
    + PAD_BOTTOM
)

CALM = QColor("#5ac8fa")
WARN_COLOR = QColor("#ffb454")
CRITICAL_COLOR = QColor("#ff5c5c")
NEUTRAL = QColor("#8b8f99")
SUBTLE = QColor("#9aa3b0")
VALUE = QColor("#f2f3f5")

ROW_BG = QColor(255, 255, 255, 9)
HEADER_LABEL = QColor("#7e8592")

LABEL_PT = 7.5
VALUE_PT = 12.0
AUX_PT = 8.5
FAMILY = "Segoe UI Variable Display"
HEADER_TITLE = "SYSTEM"

HISTORY_LEN = 60
TICK_MS = 2000
MIN_ALPHA = 0.35
MAX_ALPHA = 1.0
DEFAULT_ALPHA = 0.80

_STATE_COLORS = {NORMAL: CALM, WARN: WARN_COLOR, CRITICAL: CRITICAL_COLOR}


def state_for(value: float | None, warn: float | None, critical: float | None) -> int:
    """Map a measurement to NORMAL, WARN or CRITICAL.

    A value that was never measured is NORMAL, not CRITICAL: a missing
    reading must not raise a false alarm.
    """
    if value is None:
        return NORMAL
    if critical is not None and value >= critical:
        return CRITICAL
    if warn is not None and value >= warn:
        return WARN
    return NORMAL


def row_value(spec: MetricSpec, snapshot) -> float | None:
    """Row load as a percentage, or None when it cannot be computed."""
    if spec.kind == PCT:
        return getattr(snapshot, spec.key, None)
    used = getattr(snapshot, f"{spec.key}_used_gb", None)
    total = getattr(snapshot, f"{spec.key}_total_gb", None)
    if used is None or not total:
        return None
    return used / total * 100.0


def row_fraction(spec: MetricSpec, snapshot) -> float | None:
    """Row load normalised to 0.0-1.0 for the history buffer."""
    value = row_value(spec, snapshot)
    if value is None:
        return None
    return min(1.0, max(0.0, value / 100.0))


def metric_state(spec: MetricSpec, snapshot) -> int:
    """Worst state for a row, counting its auxiliary reading too."""
    state = state_for(row_value(spec, snapshot), spec.warn, spec.critical)
    if spec.key == "gpu_pct" and snapshot.gpu_temp_c is not None:
        state = max(state, state_for(snapshot.gpu_temp_c, GPU_TEMP_WARN, GPU_TEMP_CRITICAL))
    return state


def has_any_data(snapshot) -> bool:
    """True when at least one number will be on screen."""
    if any(row_value(spec, snapshot) is not None for spec in METRICS):
        return True
    return snapshot.gpu_temp_c is not None


def worst_state(states) -> int:
    return max(states, default=NORMAL)


def state_color(state: int) -> QColor:
    return _STATE_COLORS[state]


def panel_rect() -> QRectF:
    return QRectF(0.0, 0.0, float(WIDTH), float(HEIGHT))


def header_rect() -> QRectF:
    return QRectF(float(PAD_X), float(PAD_TOP), float(WIDTH - 2 * PAD_X), float(HEADER_H))


def metric_rects() -> tuple[tuple[MetricSpec, QRectF], ...]:
    out: list[tuple[MetricSpec, QRectF]] = []
    y = PAD_TOP + HEADER_H
    for spec in METRICS:
        out.append((spec, QRectF(float(PAD_X), float(y), float(WIDTH - 2 * PAD_X), float(ROW_H))))
        y += ROW_H + ROW_GAP
    return tuple(out)


def _font(size_pt: float, *, caps: bool = False, bold: bool = False) -> QFont:
    font = QFont(FAMILY)
    font.setPointSizeF(size_pt)
    if caps:
        font.setCapitalization(QFont.Capitalization.AllUppercase)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    font.setFeature(QFont.Tag("tnum"), 1)
    return font


def label_font() -> QFont:
    return _font(LABEL_PT, caps=True, bold=True)


def value_font() -> QFont:
    return _font(VALUE_PT, bold=True)


def aux_font() -> QFont:
    return _font(AUX_PT, bold=True)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_theme.py -q`

Expected: `16 passed`.

- [ ] **Step 7: Leave `requirements.txt` alone**

Do not touch it. Its four runtime entries are correct and pytest is not a runtime dependency;
Task 11 adds `requirements-dev.txt` for it.

- [ ] **Step 8: Commit**

```bash
git add theme.py tests/conftest.py tests/test_theme.py
git commit -m "feat: add theme module with thresholds and panel geometry

Single source of truth for palette, 80/95 percentage thresholds, GPU
temperature limits and the 280x280 panel layout. Sizes stay in logical
pixels with no manual DPI multiplier because Qt 6 already scales."
```

---

### Task 2: `history.py` with spike-preserving resampling

The row graphs depend on this, and getting the downsampling wrong silently
hides load spikes — the exact thing the graph exists to reveal.

**Files:**
- Create: `tests/test_history.py`
- Create: `history.py`

**Interfaces:**
- Consumes: `theme.HISTORY_LEN` (an `int`).
- Produces: `History(maxlen: int = theme.HISTORY_LEN)` with `__len__`, `append(value: float | None)`, `values() -> list[float]`, `resampled(width: int) -> list[QPointF]`. Module function `resample(points: list[float], width: int) -> list[QPointF]`. Class `HistoryLog(path: Path | str = "metrics_history.log")` with `enabled` property, `enable() -> bool`, `disable() -> None`, `write(snapshot) -> None`.

- [ ] **Step 1: Write the failing tests in `tests/test_history.py`**

```python
from PyQt6.QtCore import QPointF

from history import History, resample


def test_history_respects_maxlen():
    h = History(maxlen=3)
    for value in (0.1, 0.2, 0.3, 0.4):
        h.append(value)
    assert h.values() == [0.2, 0.3, 0.4]


def test_history_ignores_missing_samples():
    h = History()
    h.append(None)
    h.append(0.5)
    h.append(None)
    assert h.values() == [0.5]


def test_history_clamps_out_of_range_input():
    h = History()
    h.append(-5.0)
    h.append(5.0)
    assert h.values() == [0.0, 1.0]


def test_resample_keeps_a_one_sample_spike():
    # The peak must share a column window with a value near the previous
    # point, or uniform sampling gives the same answer and the test proves
    # nothing. Windows at width 4 are [0,0] [0,0.4,1.0] [0,0] [0,0,0].
    points = [0.0, 0.0, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    got = resample(points, 4)
    assert max(p.y() for p in got) == 1.0
    naive = [points[column * len(points) // 4] for column in range(4)]
    assert naive == [0.0, 0.0, 0.0, 0.0]


def test_resample_tie_break_is_measured_from_the_previous_column():
    # Column 1's window is [0.8, 0.95] and the previous point is 1.0, so the
    # furthest value is 0.8. A plain per-column max would pick 0.95.
    got = resample([1.0, 0.0, 0.8, 0.95], 2)
    assert [p.y() for p in got] == [1.0, 0.8]


def test_resample_only_draws_values_that_were_measured():
    points = [0.2, 0.25, 0.18, 0.22, 0.9, 0.2, 0.24, 0.19, 0.21, 0.2]
    ys = [p.y() for p in resample(points, 5)]
    assert set(ys) <= set(points)
    assert max(ys) == 0.9


def test_resample_keeps_every_point_when_it_fits():
    points = [0.1, 0.4, 0.9]
    got = resample(points, 10)
    assert [p.y() for p in got] == points


def test_resample_single_point_starts_at_the_left_edge():
    assert resample([0.5], 10) == [QPointF(0.0, 0.5)]


def test_resample_empty_and_zero_width_are_empty():
    assert resample([], 10) == []
    assert resample([0.5], 0) == []


def test_resampled_fills_the_full_width_when_fewer_points():
    h = History()
    h.append(0.2)
    h.append(0.8)
    got = h.resampled(20)
    assert len(got) == 2
    assert got[-1].x() > 0.0


def test_default_history_length_comes_from_theme():
    import theme

    assert len(History()) == 0
    assert History()._values.maxlen == theme.HISTORY_LEN
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_history.py -q`

Expected: collection error `ModuleNotFoundError: No module named 'history'`.

- [ ] **Step 3: Write `history.py`**

```python
"""Metric history and the optional CSV trace.

`resample` is the interesting part: the row graphs are a few hundred pixels
wide while the buffer holds a minute of samples, so something has to be
thrown away, and uniform sampling throws away spikes.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from pathlib import Path

from PyQt6.QtCore import QPointF

from theme import HISTORY_LEN

logger = logging.getLogger("widget.history")


class History:
    """Ring buffer of one normalised metric, clamped to 0.0-1.0."""

    __slots__ = ("_values",)

    def __init__(self, maxlen: int = HISTORY_LEN) -> None:
        self._values: deque[float] = deque(maxlen=maxlen)

    def __len__(self) -> int:
        return len(self._values)

    def append(self, value: float | None) -> None:
        # NaN is skipped, not clamped: min/max silently turn NaN into 0.0,
        # which would record an invented sample as a real reading.
        if value is None or math.isnan(value):
            return
        self._values.append(min(1.0, max(0.0, value)))

    def values(self) -> list[float]:
        return list(self._values)

    def resampled(self, width: int) -> list[QPointF]:
        return resample(self.values(), width)


def resample(points: list[float], width: int) -> list[QPointF]:
    """Fit points into `width` columns, keeping each column's extreme.

    Uniform sampling would drop spikes: with more samples than pixels a
    one-sample spike is easily averaged away, and the graph would lie about
    exactly the moment it exists to reveal. Instead each column picks the
    value furthest from the previously drawn point, so a peak survives.

    That deliberately breaks visual continuity at a spike, which is the
    trade being made. Column 0 has no predecessor, so it takes a plain
    per-column max: with nothing to be continuous with, the maximum is the
    only spike-preserving choice.

    Values are not clamped here; `History.append` clamps before anything
    reaches this function, and a NaN is dropped there rather than recorded.
    """
    if width <= 0 or not points:
        return []
    n = len(points)
    if n == 1:
        return [QPointF(0.0, points[0])]
    if n <= width:
        step = (width - 1) / (n - 1)
        return [QPointF(i * step, points[i]) for i in range(n)]

    out: list[QPointF] = []
    previous: float | None = None
    for column in range(width):
        lo = column * n // width
        hi = max(lo + 1, (column + 1) * n // width)
        window = points[lo:hi]
        chosen = max(window) if previous is None else max(window, key=lambda v: abs(v - previous))
        out.append(QPointF(float(column), chosen))
        previous = chosen
    return out


class HistoryLog:
    """Appends one CSV row per tick. Off by default; enabled from the tray."""

    def __init__(self, path: Path | str = "metrics_history.log") -> None:
        self._path = Path(path)
        self._enabled = False
        self._has_header = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self) -> bool:
        try:
            self._has_header = self._path.exists() and self._path.stat().st_size > 0
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(exist_ok=True)
        except OSError as exc:
            logger.warning("history log unavailable, staying off: %s", exc)
            return False
        self._enabled = True
        return True

    def disable(self) -> None:
        self._enabled = False

    def write(self, snapshot) -> None:
        if not self._enabled:
            return
        values = snapshot.as_dict()
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                # The header is derived from the same dict as the row, so the
                # two can never drift apart.
                if not self._has_header:
                    handle.write(",".join(values.keys()) + "\n")
                    self._has_header = True
                handle.write(",".join(_csv(v) for v in values.values()) + "\n")
        except OSError as exc:
            logger.warning("history log write failed, switching off: %s", exc)
            self._enabled = False


def _csv(value: float | None) -> str:
    return "" if value is None else f"{value:g}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_history.py -q`

Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
git add history.py tests/test_history.py
git commit -m "feat: add history ring buffer with spike-preserving resampling

Downsampling picks each column's extreme rather than an average, so a
one-sample load spike survives being squeezed into a few hundred pixels.
Also adds the optional CSV trace that README and AGENTS.md already
promised but nothing ever wrote."
```

`HistoryLog.write` is covered by Task 3, where `Snapshot` exists.

---

### Task 3: `metrics.py` rewrite

The single biggest source of waste: four `nvmlInit()` calls per tick, stringly-typed returns,
and a WMI fallback that invents a load percentage.

**Files:**
- Create: `tests/test_metrics.py`
- Rewrite: `metrics.py`

**Interfaces:**
- Consumes: nothing from the project.
- Produces: `Snapshot` frozen dataclass with fields `cpu_pct`, `cpu_mhz`, `cpu_max_mhz`, `ram_used_gb`, `ram_total_gb`, `gpu_pct`, `gpu_temp_c`, `vram_used_gb`, `vram_total_gb`, `ts`, all defaulting to `None` except `ts: float = 0.0`; method `as_dict() -> dict[str, float | None]`. `GpuProbe(nvml=None)` with `available` property and `read() -> dict[str, float | None]`. `SystemProbe(cpu_pct=None, cpu_freq=None, ram=None, gpu=None)` with `sample() -> Snapshot`. Module function `wmi_fallback() -> dict[str, float | None]` and `_wmi_video_controllers() -> list`.

- [ ] **Step 1: Write the failing tests in `tests/test_metrics.py`**

```python
from types import SimpleNamespace

import metrics
from metrics import GpuProbe, Snapshot, SystemProbe, wmi_fallback

GB = 1024 ** 3


class FakeUtil:
    def __init__(self, gpu):
        self.gpu = gpu


class FakeMemory:
    def __init__(self, used, total):
        self.used = used
        self.total = total


class FakeNvml:
    """Stands in for the pynvml module and counts every call."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self, fail_init=False):
        self.calls = {}
        self._fail_init = fail_init

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    def nvmlInit(self):
        self._count("nvmlInit")
        if self._fail_init:
            raise RuntimeError("no device")

    def nvmlDeviceGetHandleByIndex(self, index):
        self._count("handle")
        return f"handle-{index}"

    def nvmlDeviceGetName(self, handle):
        return "Fake GPU"

    def nvmlDeviceGetUtilizationRates(self, handle):
        self._count("util")
        return FakeUtil(42)

    def nvmlDeviceGetTemperature(self, handle, sensor):
        self._count("temp")
        return 61

    def nvmlDeviceGetMemoryInfo(self, handle):
        self._count("mem")
        return FakeMemory(2 * GB, 8 * GB)


class FakeGpu:
    def __init__(self, values):
        self.values = values

    def read(self):
        return dict(self.values)


def test_probe_initialises_nvml_exactly_once():
    nvml = FakeNvml()
    probe = GpuProbe(nvml=nvml)
    probe.read()
    probe.read()
    probe.read()
    assert nvml.calls["nvmlInit"] == 1
    assert nvml.calls["handle"] == 1


def test_probe_reads_every_gpu_number_in_one_pass():
    probe = GpuProbe(nvml=FakeNvml())
    assert probe.read() == {
        "gpu_pct": 42.0,
        "gpu_temp_c": 61.0,
        "vram_used_gb": 2.0,
        "vram_total_gb": 8.0,
    }


def test_probe_reports_unavailable_when_nvml_cannot_start(monkeypatch):
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [])
    probe = GpuProbe(nvml=FakeNvml(fail_init=True))
    assert probe.available is False
    assert probe.read() == {
        "gpu_pct": None,
        "gpu_temp_c": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
    }


def test_wmi_fallback_does_not_invent_a_load_percentage(monkeypatch):
    controller = SimpleNamespace(AdapterRAM=8 * GB, CurrentClockFrequency=900, MaxClockSpeed=1800)
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [controller])
    result = wmi_fallback()
    assert result["gpu_pct"] is None
    assert result["gpu_temp_c"] is None
    assert result["vram_used_gb"] is None
    assert result["vram_total_gb"] == 8.0


def test_wmi_fallback_handles_no_controllers(monkeypatch):
    monkeypatch.setattr(metrics, "_wmi_video_controllers", lambda: [])
    assert wmi_fallback()["vram_total_gb"] is None


def test_sample_fills_every_field():
    probe = SystemProbe(
        cpu_pct=lambda: 12.5,
        cpu_freq=lambda: SimpleNamespace(current=3600.0, max=4500.0),
        ram=lambda: SimpleNamespace(used=11.4 * GB, total=32 * GB),
        gpu=FakeGpu({"gpu_pct": 61.0, "gpu_temp_c": 58.0, "vram_used_gb": 6.2, "vram_total_gb": 20.0}),
    )
    snap = probe.sample()
    assert snap.cpu_pct == 12.5
    assert snap.cpu_mhz == 3600.0
    assert snap.cpu_max_mhz == 4500.0
    assert snap.ram_used_gb == 11.4
    assert snap.ram_total_gb == 32.0
    assert snap.gpu_pct == 61.0
    assert snap.gpu_temp_c == 58.0
    assert snap.vram_used_gb == 6.2
    assert snap.vram_total_gb == 20.0
    assert snap.ts > 0.0


def test_sample_drops_a_zero_maximum_frequency():
    probe = SystemProbe(
        cpu_pct=lambda: 1.0,
        cpu_freq=lambda: SimpleNamespace(current=3600.0, max=0.0),
        ram=lambda: SimpleNamespace(used=0, total=0),
        gpu=FakeGpu({}),
    )
    snap = probe.sample()
    assert snap.cpu_mhz == 3600.0
    assert snap.cpu_max_mhz is None


def test_sample_reports_none_when_a_source_raises():
    def boom():
        raise RuntimeError("sensor bus is on fire")

    probe = SystemProbe(
        cpu_pct=boom, cpu_freq=boom, ram=boom, gpu=FakeGpu({})
    )
    snap = probe.sample()
    assert snap.cpu_pct is None
    assert snap.cpu_mhz is None
    assert snap.ram_used_gb is None
    assert snap.ram_total_gb is None
    assert snap.gpu_pct is None
    assert snap.ts > 0.0


def test_snapshot_as_dict_round_trips():
    snap = Snapshot(cpu_pct=5.0)
    assert snap.as_dict()["cpu_pct"] == 5.0
    assert snap.as_dict()["gpu_temp_c"] is None


def test_real_probe_runs_on_this_machine():
    probe = SystemProbe()
    snap = probe.sample()
    assert snap.ram_total_gb and snap.ram_total_gb > 0


def test_history_log_stays_off_when_the_file_cannot_be_opened(tmp_path):
    from history import HistoryLog

    # A regular file where a directory needs to be: mkdir cannot succeed,
    # so this exercises the failure branch rather than the happy path.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    log = HistoryLog(blocker / "sub" / "history.log")
    assert log.enable() is False
    assert log.enabled is False


def test_history_log_writes_a_header_once(tmp_path):
    from history import HistoryLog

    path = tmp_path / "history.log"
    log = HistoryLog(path)
    assert log.enable() is True
    log.write(Snapshot(cpu_pct=10.0, ts=1.0))

    log.disable()
    assert log.enabled is False
    log.write(Snapshot(cpu_pct=20.0, ts=2.0))

    log.enable()
    log.write(Snapshot(cpu_pct=30.0, ts=3.0))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    # Header plus the two writes that happened while the log was enabled.
    assert len(lines) == 3
    assert lines[0].split(",")[0] == "ts"
    assert lines[1] == "1,10,,,,,,,," 
    assert lines[2] == "3,30,,,,,,,,"


def test_history_log_header_matches_the_row_width(tmp_path):
    from history import HistoryLog

    path = tmp_path / "history.log"
    log = HistoryLog(path)
    log.enable()
    log.write(Snapshot(cpu_pct=1.0, ts=2.0))
    header, row = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(header.split(",")) == len(row.split(","))


```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_metrics.py -q`

Expected: failures — `Snapshot` has no `as_dict`, `GpuProbe` does not take `nvml`, and
`test_probe_initialises_nvml_exactly_once` reports 3 `nvmlInit` calls under the old code.

- [ ] **Step 3: Write the new `metrics.py`**

```python
"""Reading hardware counters.

One Snapshot per tick. Every field is float | None, where None means "not
measured" and the renderer draws a dash. Nothing in here invents a number:
an estimate that looks like a measurement is worse than no reading at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from typing import Any, Callable

logger = logging.getLogger("widget.metrics")

_GB = 1024 ** 3

GPU_KEYS = ("gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb")


@dataclass(frozen=True, slots=True)
class Snapshot:
    cpu_pct: float | None = None
    cpu_mhz: float | None = None
    cpu_max_mhz: float | None = None
    ram_used_gb: float | None = None
    ram_total_gb: float | None = None
    gpu_pct: float | None = None
    gpu_temp_c: float | None = None
    vram_used_gb: float | None = None
    vram_total_gb: float | None = None
    ts: float = 0.0

    # asdict() follows declaration order, which would put ts last. The CSV
    # writer and this list must agree or the trace is silently column-shifted,
    # so the order is stated once here and the header is derived from it.
    CSV_COLUMNS = (
        "ts", "cpu_pct", "cpu_mhz", "cpu_max_mhz",
        "ram_used_gb", "ram_total_gb",
        "gpu_pct", "gpu_temp_c", "vram_used_gb", "vram_total_gb",
    )

    def as_dict(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in self.CSV_COLUMNS}


def _empty_gpu() -> dict[str, float | None]:
    return dict.fromkeys(GPU_KEYS)


def _wmi_video_controllers() -> list[Any]:
    """Win32_VideoController instances, or [] when WMI is unusable."""
    try:
        import wmi

        return list(wmi.WMI().Win32_VideoController())
    except Exception:
        logger.info("WMI unavailable")
        return []


def wmi_fallback() -> dict[str, float | None]:
    """Only what WMI can actually measure, and nothing more.

    The old code estimated GPU load as clock_ratio * 30. That is a made-up
    percentage wearing the costume of a measurement, so it is gone: load and
    temperature come back as None and the panel shows a dash.
    """
    out = _empty_gpu()
    controllers = _wmi_video_controllers()
    if not controllers:
        return out
    adapter_ram = getattr(controllers[0], "AdapterRAM", 0) or 0
    if adapter_ram:
        out["vram_total_gb"] = round(float(adapter_ram) / _GB, 1)
    return out


class GpuProbe:
    """Holds one NVML handle for the life of the process.

    The previous code called nvmlInit() once per metric, so a tick cost four
    initialisations, and a machine without NVIDIA raised eight exceptions
    every two seconds instead of answering once at startup.
    """

    def __init__(self, nvml: Any | None = None) -> None:
        if nvml is None:
            try:
                import pynvml as nvml  # type: ignore[no-redef]
            except Exception:
                logger.info("pynvml not importable, GPU metrics disabled")
                nvml = None
        self._nvml = nvml
        self._handle: Any | None = None
        if nvml is not None:
            try:
                nvml.nvmlInit()
                self._handle = nvml.nvmlDeviceGetHandleByIndex(0)
                logger.info("NVML ready: %s", nvml.nvmlDeviceGetName(self._handle))
            except Exception:
                logger.info("NVML present but no usable device, falling back to WMI")
                self._handle = None

    @property
    def available(self) -> bool:
        return self._handle is not None

    def read(self) -> dict[str, float | None]:
        if self._handle is None:
            return wmi_fallback()
        nvml, handle = self._nvml, self._handle
        out = _empty_gpu()
        try:
            out["gpu_pct"] = float(nvml.nvmlDeviceGetUtilizationRates(handle).gpu)
        except Exception:
            logger.info("NVML utilization unavailable", exc_info=True)
        try:
            out["gpu_temp_c"] = float(
                nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:
            logger.info("NVML temperature unavailable", exc_info=True)
        try:
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            out["vram_used_gb"] = round(memory.used / _GB, 1)
            out["vram_total_gb"] = round(memory.total / _GB, 1)
        except Exception:
            logger.info("NVML memory info unavailable", exc_info=True)
        return out


class SystemProbe:
    """Builds one Snapshot. Every dependency is injectable for tests."""

    def __init__(
        self,
        cpu_pct: Callable[[], float] | None = None,
        cpu_freq: Callable[[], Any] | None = None,
        ram: Callable[[], Any] | None = None,
        gpu: Any | None = None,
    ) -> None:
        import psutil

        if cpu_pct is None:
            cpu_pct = lambda: psutil.cpu_percent(interval=0.1)  # noqa: E731
        if cpu_freq is None:
            cpu_freq = psutil.cpu_freq
        if ram is None:
            ram = psutil.virtual_memory
        self._cpu_pct = cpu_pct
        self._cpu_freq = cpu_freq
        self._ram = ram
        self._gpu = gpu if gpu is not None else GpuProbe()

    def sample(self) -> Snapshot:
        values: dict[str, float | None] = {}

        try:
            values["cpu_pct"] = float(self._cpu_pct())
        except Exception:
            logger.warning("cpu_percent failed", exc_info=True)

        try:
            freq = self._cpu_freq()
            current = getattr(freq, "current", None)
            maximum = getattr(freq, "max", None)
            values["cpu_mhz"] = float(current) if current else None
            # Some machines report max == 0; a ratio against it would divide by zero.
            values["cpu_max_mhz"] = float(maximum) if maximum else None
        except Exception:
            logger.warning("cpu_freq failed", exc_info=True)

        try:
            memory = self._ram()
            values["ram_used_gb"] = round(memory.used / _GB, 1)
            values["ram_total_gb"] = round(memory.total / _GB, 1)
        except Exception:
            logger.warning("virtual_memory failed", exc_info=True)

        try:
            for key, value in self._gpu.read().items():
                if key in GPU_KEYS:
                    values[key] = value
        except Exception:
            logger.warning("gpu read failed", exc_info=True)

        values["ts"] = time.time()
        return Snapshot(**values)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_metrics.py -q`

Expected: `13 passed`.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`

Expected: all tests pass, including the two `HistoryLog` tests added in Task 2 Step 5.

- [ ] **Step 6: Commit**

```bash
git add metrics.py tests/test_metrics.py
git commit -m "perf: reuse one NVML handle and stop fabricating GPU numbers

GpuProbe initialises NVML once per process instead of once per metric per
tick, so a machine without NVIDIA stops raising eight exceptions every two
seconds. WMI fallback no longer estimates load as clock_ratio * 30: what
cannot be measured comes back as None and renders as a dash. Snapshot is
typed end to end, removing the float(m.gpu_utilization()) string detour."
```

---

### Task 4: `settings.py`

Small, but it is what makes position and opacity survive a restart, and it is
the only place that has to know whether it runs frozen or from source.

**Files:**
- Create: `tests/test_settings.py`
- Create: `settings.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` dataclass with `x: int | None = None`, `y: int | None = None`, `alpha: float = 0.80`, `always_on_top: bool = True`, `acrylic: bool = False`, `log_history: bool = False`. `config_path() -> Path`. `load_settings(path: Path | None = None) -> Settings`. `save_settings(settings: Settings, path: Path | None = None) -> bool`.

- [ ] **Step 1: Write the failing tests in `tests/test_settings.py`**

```python
import json

from settings import Settings, load_settings, save_settings


def test_defaults_when_the_file_is_missing(tmp_path):
    assert load_settings(tmp_path / "nope.json") == Settings()


def test_defaults_when_the_file_is_corrupt(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_settings(path) == Settings()


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alpha": 0.5, "from_the_future": 1}), encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.alpha == 0.5
    assert not hasattr(loaded, "from_the_future")


def test_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    original = Settings(x=100, y=200, alpha=0.65, always_on_top=False, acrylic=True, log_history=True)
    assert save_settings(original, path) is True
    assert load_settings(path) == original


def test_save_failure_is_reported_not_raised(tmp_path):
    unwritable = tmp_path / "missing-dir" / "settings.json"
    assert save_settings(Settings(), unwritable) is False


def test_config_path_ends_with_the_expected_filename():
    from settings import CONFIG_NAME, config_path

    assert config_path().name == CONFIG_NAME == "settings.json"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_settings.py -q`

Expected: collection error `ModuleNotFoundError: No module named 'settings'`.

- [ ] **Step 3: Write `settings.py`**

```python
"""Persisted user preferences, stored beside the executable.

Under PyInstaller `__file__` points inside the bundle, which is unpacked to a
temp directory that is wiped on exit, so a frozen build must anchor on
sys.executable instead.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

logger = logging.getLogger("widget.settings")

CONFIG_NAME = "settings.json"


@dataclass
class Settings:
    x: int | None = None
    y: int | None = None
    alpha: float = 0.80
    always_on_top: bool = True
    acrylic: bool = False
    log_history: bool = False


def config_path() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / CONFIG_NAME


def load_settings(path: Path | None = None) -> Settings:
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("settings unreadable at %s (%s); using defaults", target, exc)
        return Settings()

    if not isinstance(raw, dict):
        logger.warning("settings at %s are not an object; using defaults", target)
        return Settings()

    known = {f.name for f in fields(Settings)}
    try:
        return Settings(**{k: v for k, v in raw.items() if k in known})
    except TypeError as exc:
        logger.warning("settings had unusable types (%s); using defaults", exc)
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> bool:
    target = path or config_path()
    try:
        target.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write settings to %s: %s", target, exc)
        return False
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_settings.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Commit**

```bash
git add settings.py tests/test_settings.py
git commit -m "feat: persist position, opacity and options as JSON

Anchors the config beside sys.executable when frozen, because __file__
inside a PyInstaller bundle points at a temp directory that is wiped on
exit. Corrupt or partial files fall back to defaults instead of raising."
```

---

### Task 5: `painter.py` and the golden images

The whole visual redesign lives in this one function. It is pure, which is
why it can be verified by comparing rendered pixels.

**Files:**
- Create: `tests/test_painter.py`
- Create: `painter.py`
- Create: `tests/golden/` (generated)

**Interfaces:**
- Consumes: `theme` (Task 1), `history.History` (Task 2), `metrics.Snapshot` (Task 3).
- Produces: `paint(painter: QPainter, snapshot, histories: dict[str, History], alpha: float) -> None`.

- [ ] **Step 1: Write the failing tests in `tests/test_painter.py`**

```python
import os
from pathlib import Path

import pytest
from PyQt6.QtGui import QColor, QImage, QPainter

import theme
from history import History
from metrics import Snapshot
from painter import paint

GOLDEN_DIR = Path(__file__).parent / "golden"
TOLERANCE = 10


def build_histories(values_by_key):
    histories = {key: History() for key in theme.METRICS_BY_KEY}
    for key, series in values_by_key.items():
        # setdefault, not []: callers may pass keys the panel does not draw.
        for value in series:
            histories.setdefault(key, History()).append(value)
    return histories


def render(snapshot, values_by_key=None, alpha=theme.DEFAULT_ALPHA):
    image = QImage(theme.WIDTH, theme.HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        # now is pinned: the header shows the sample age, and a wall clock would
        # change the pixels on every run.
        paint(painter, snapshot, build_histories(values_by_key or {}), alpha, now=snapshot.ts + 2.0)
    finally:
        painter.end()
    return image


def max_channel_delta(a, b):
    worst = 0
    for y in range(a.height()):
        for x in range(a.width()):
            left, right = a.pixelColor(x, y), b.pixelColor(x, y)
            worst = max(
                worst,
                abs(left.red() - right.red()),
                abs(left.green() - right.green()),
                abs(left.blue() - right.blue()),
                abs(left.alpha() - right.alpha()),
            )
    return worst


def assert_golden(name, snapshot, values_by_key=None):
    path = GOLDEN_DIR / f"{name}.png"
    actual = render(snapshot, values_by_key)
    if os.environ.get("MONITOR_REGEN_GOLDEN") or not path.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        actual.save(str(path))
        return
    golden = QImage(str(path))
    assert not golden.isNull(), f"{path} is unreadable"
    assert actual.size() == golden.size()
    delta = max_channel_delta(actual, golden)
    assert delta <= TOLERANCE, f"{name}.png drifted by {delta}"


CALM = Snapshot(
    cpu_pct=34.0, cpu_mhz=4500.0, cpu_max_mhz=4500.0,
    ram_used_gb=11.4, ram_total_gb=32.0,
    gpu_pct=61.0, gpu_temp_c=58.0,
    vram_used_gb=6.2, vram_total_gb=20.0,
    ts=1000.0,
)

HOT = Snapshot(
    cpu_pct=97.0, cpu_mhz=4200.0, cpu_max_mhz=4500.0,
    ram_used_gb=29.4, ram_total_gb=32.0,
    gpu_pct=97.0, gpu_temp_c=84.0,
    vram_used_gb=18.1, vram_total_gb=20.0,
    ts=1000.0,
)

EMPTY = Snapshot(ts=1000.0)

RAMPS = {
    "cpu_pct": [0.30 + 0.08 * ((i * 7) % 5) / 5 for i in range(40)],
    "ram": [0.34, 0.35, 0.36, 0.36, 0.35, 0.34, 0.35, 0.36],
    "gpu_pct": [0.55 + 0.10 * ((i * 3) % 7) / 7 for i in range(40)],
    "vram": [0.30, 0.31, 0.31, 0.30, 0.32],
}


def test_calm_panel_matches_golden():
    assert_golden("calm", CALM, RAMPS)


def test_hot_panel_matches_golden():
    assert_golden("hot", HOT, RAMPS)


def test_missing_data_matches_golden():
    assert_golden("missing", EMPTY, {})


def test_render_is_never_blank():
    image = render(CALM, RAMPS)
    colours = {image.pixelColor(x, y).rgba() for y in range(0, theme.HEIGHT, 3)
               for x in range(0, theme.WIDTH, 3)}
    assert len(colours) > 40


def test_render_is_never_blank_without_data():
    image = render(EMPTY, {})
    colours = {image.pixelColor(x, y).rgba() for y in range(0, theme.HEIGHT, 3)
               for x in range(0, theme.WIDTH, 3)}
    assert len(colours) > 5


def test_single_sample_does_not_break_rendering():
    image = render(CALM, {"cpu_pct": [0.5]})
    assert not image.isNull()


def test_unknown_history_keys_are_ignored():
    histories = build_histories({"not_a_metric": [0.1, 0.2]})
    image = QImage(theme.WIDTH, theme.HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        paint(painter, CALM, histories, 0.8)
    finally:
        painter.end()
    assert not image.isNull()


def test_alpha_zero_still_draws_the_text():
    opaque = render(CALM, RAMPS, alpha=1.0)
    clear = render(CALM, RAMPS, alpha=0.0)
    assert max_channel_delta(opaque, clear) > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_painter.py -q`

Expected: collection error `ModuleNotFoundError: No module named 'painter'`.

- [ ] **Step 3: Write `painter.py`**

```python
"""Panel rendering: a pure function from snapshot to QPainter commands.

No widget state, no timers, no I/O. That is what lets the tests draw into a
QImage with no window on screen and compare the result against a reference.

Layout per row: a 52 px rounded slab holding the label on the left, the value
and its auxiliary reading on the right, and a 26 px graph strip along the
bottom whose filled area is the row's history.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

import theme
from history import History, resample

HEADER_MARGIN = 9
GRAPH_LINE_WIDTH = 1.4
GRAPH_FILL_ALPHA = 0.22
VALUE_GAP = 7.0


def paint(
    painter: QPainter,
    snapshot,
    histories: dict[str, History],
    alpha: float,
    now: float | None = None,
) -> None:
    """Draw the whole panel. `histories` may be missing keys; rows degrade.

    `now` pins the clock used for the header's "Xs ago" age. It defaults to
    wall clock time in the app, but tests pass a fixed value: otherwise the
    age text changes every run and the golden images could never match.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    _draw_panel(painter, theme.panel_rect(), alpha)
    _draw_header(painter, theme.header_rect(), snapshot, now)

    for spec, rect in theme.metric_rects():
        _draw_row(painter, spec, rect, snapshot, histories.get(spec.key))

    painter.restore()


def _draw_panel(painter: QPainter, rect: QRectF, alpha: float) -> None:
    painter.setPen(Qt.PenStyle.NoPen)

    # Fake a soft drop shadow: three expanding slabs, biggest and faintest first.
    for grow, strength in ((6.0, 0.10), (3.0, 0.16), (0.0, 0.28)):
        shadow = QRectF(rect)
        shadow.adjust(-grow, -grow * 0.6, grow * 2.0, grow * 1.2)
        tint = QColor(0, 0, 0)
        tint.setAlphaF(strength)
        painter.setBrush(tint)
        painter.drawRoundedRect(shadow, theme.PANEL_RADIUS + grow, theme.PANEL_RADIUS + grow)

    fill = QColor(16, 18, 24)
    fill.setAlphaF(max(0.0, min(1.0, alpha)))
    painter.setBrush(fill)
    painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
    painter.drawRoundedRect(rect, theme.PANEL_RADIUS, theme.PANEL_RADIUS)
    painter.setPen(Qt.PenStyle.NoPen)


def _draw_header(painter: QPainter, rect: QRectF, snapshot, now: float | None = None) -> None:
    if theme.has_any_data(snapshot):
        state = theme.worst_state(theme.metric_state(spec, snapshot) for spec in theme.METRICS)
        dot_color = theme.state_color(state)
    else:
        dot_color = theme.NEUTRAL

    centre_y = rect.center().y()
    dot_x = rect.left() + theme.DOT_R + 1.0
    painter.setBrush(dot_color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(dot_x, centre_y), theme.DOT_R, theme.DOT_R)

    title_rect = QRectF(dot_x + theme.DOT_R * 2.0 + 5.0, rect.top(), rect.width(), rect.height())
    _draw_text(painter, theme.HEADER_TITLE, title_rect, theme.label_font(), theme.HEADER_LABEL)

    age = _format_age(snapshot, now)
    if age:
        _draw_text(painter, age, rect, theme.label_font(), theme.NEUTRAL, right=True)


def _format_age(snapshot, now: float | None = None) -> str:
    if not snapshot.ts:
        return ""
    moment = time.time() if now is None else now
    seconds = max(0, int(moment - snapshot.ts))
    return "now" if seconds < 1 else f"{seconds}s ago"


def _draw_row(painter: QPainter, spec, rect: QRectF, snapshot, history) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.ROW_BG)
    painter.drawRoundedRect(rect, theme.ROW_RADIUS, theme.ROW_RADIUS)

    state = theme.metric_state(spec, snapshot)
    color = theme.state_color(state)

    if history is not None and len(history) > 1:
        graph = QRectF(rect)
        graph.setTop(rect.bottom() - theme.GRAPH_H)
        _draw_graph(painter, graph, history.values(), color)

    text_rect = QRectF(
        rect.left() + HEADER_MARGIN,
        rect.top() + 6.0,
        rect.width() - 2 * HEADER_MARGIN,
        20.0,
    )
    _draw_text(painter, spec.label, text_rect, theme.label_font(), theme.NEUTRAL)

    value_text = _format_value(spec, snapshot)
    aux_text = _format_aux(spec, snapshot)
    value_font = theme.value_font()
    aux_font = theme.aux_font()

    # Value sits closest to the right edge, the auxiliary reading to its left:
    # "34% 4.5 / 4.5 GHz", with the dimmer text trailing the number it qualifies.
    aux_width = QFontMetricsF(aux_font).horizontalAdvance(aux_text) if aux_text else 0.0
    _draw_text(
        painter, value_text, text_rect, value_font, color,
        right=True, right_offset=-(aux_width + VALUE_GAP),
    )
    if aux_text:
        _draw_text(painter, aux_text, text_rect, aux_font, theme.SUBTLE, right=True)


def _draw_graph(painter: QPainter, rect: QRectF, values: list[float], color: QColor) -> None:
    points = resample(values, max(2, int(rect.width())))
    if len(points) < 2:
        return
    bottom = rect.bottom()

    area = QPainterPath()
    area.moveTo(points[0].x(), bottom)
    for point in points:
        area.lineTo(point.x(), bottom - point.y() * rect.height())
    area.lineTo(points[-1].x(), bottom)
    area.closeSubpath()

    fill = QColor(color)
    fill.setAlphaF(GRAPH_FILL_ALPHA)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawPath(area)

    line = QPainterPath()
    line.moveTo(points[0].x(), bottom - points[0].y() * rect.height())
    for point in points[1:]:
        line.lineTo(point.x(), bottom - point.y() * rect.height())

    pen = QPen(color, GRAPH_LINE_WIDTH)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(line)


def _draw_text(
    painter: QPainter,
    text: str,
    rect: QRectF,
    font,
    color: QColor,
    *,
    right: bool = False,
    right_offset: float = 0.0,
) -> None:
    if not text:
        return
    painter.setFont(font)
    painter.setPen(QPen(color))
    metrics = QFontMetricsF(font)
    baseline = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2.0
    if right:
        x = rect.right() + right_offset - metrics.horizontalAdvance(text)
    else:
        x = rect.left()
    painter.drawText(QPointF(x, baseline), text)


def _format_value(spec, snapshot) -> str:
    if spec.kind == theme.PCT:
        value = getattr(snapshot, spec.key, None)
        return "--" if value is None else f"{value:.0f}%"
    used = getattr(snapshot, f"{spec.key}_used_gb", None)
    return "--" if used is None else f"{used:.1f}"


def _format_aux(spec, snapshot) -> str:
    if spec.kind == theme.PCT:
        if spec.key == "cpu_pct":
            return _format_frequency(snapshot)
        if spec.key == "gpu_pct":
            temp = snapshot.gpu_temp_c
            return "--" if temp is None else f"{temp:.0f}°C"
        return ""
    total = getattr(snapshot, f"{spec.key}_total_gb", None)
    return "--" if total is None else f"/ {total:.1f} GB"


def _format_frequency(snapshot) -> str:
    current, maximum = snapshot.cpu_mhz, snapshot.cpu_max_mhz
    if current is None:
        return "--"
    if maximum:
        return f"{current / 1000:.1f} / {maximum / 1000:.1f} GHz"
    return f"{current / 1000:.1f} GHz"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_painter.py -q`

Expected: `8 passed`, and `tests/golden/` now holds `calm.png`, `hot.png`, `missing.png`.

- [ ] **Step 5: Look at the golden images**

Read `tests/golden/calm.png` with the Read tool and confirm all of this:

- 280 × 280, rounded dark panel with a soft shadow, four rows.
- Header reads `SYSTEM` on the left and `2S AGO` on the right, with a calm blue dot.
- CPU row: `34%` then `4.5 / 4.5 GHz` — value first, dimmer auxiliary to its right.
- RAM row: `11.4` then `/ 32.0 GB`.
- GPU row: `61%` then `58°C`.
- VRAM row: `6.2` then `/ 20.0 GB`.
- Each row carries its history as a filled area along the bottom of the row.

Then read `tests/golden/hot.png` and confirm every value has gone amber or red and the header
dot turned red. If anything is clipped, overlapping or in the wrong order, fix `painter.py` or
`theme.py` and re-run so the goldens regenerate.

- [ ] **Step 6: Verify the goldens are stable and that they actually bite**

First, determinism. Run twice and compare hashes:

```bash
python -m pytest tests/test_painter.py -q
```

Expected: `8 passed`, and no golden file was rewritten — the header age is pinned to
`snapshot.ts + 2.0`, so a wall-clock dependency would have rewritten them.

Then, that a regression is caught. Change `GRAPH_H` from 26 to 20 in `theme.py` and re-run:

Expected: `AssertionError: calm.png drifted by 229` and `hot.png drifted by 240`. Revert
`GRAPH_H` to 26 and confirm `8 passed` again.

- [ ] **Step 7: Commit**

```bash
git add painter.py tests/test_painter.py tests/golden
git commit -m "feat: render the Glass A2 panel with QPainter

Pure function from snapshot to painter commands: no widget state, so the
tests draw into a QImage offscreen and diff against golden references.
Each row carries its own history as a filled area at the bottom of the row,
with the label and value above it. Unmeasured numbers render as --."
```

---

### Task 6: `overlay.py` — window, drag, mouse, menu signal

Thirteen commits went into fixing mouse handling before. The rewrite removes
the causes rather than patching the symptoms: no manual double-click
detection, no `grabMouse`, no `setFixedSize` fighting the layout, one source of
truth for alpha.

**Files:**
- Create: `tests/test_overlay.py`
- Rewrite: `overlay.py`

**Interfaces:**
- Consumes: `theme`, `history.History`, `painter.paint`, `settings.Settings`.
- Produces: `MonitorPanel(settings: Settings)` — a `QWidget` with signals `menu_requested = pyqtSignal(QPoint)` and `quit_requested = pyqtSignal()`; methods `apply_snapshot(snapshot)`, `set_alpha(value: float)`, `restore_default_position()`, `clamp_to_screen(point: QPoint) -> QPoint`, `current_alpha() -> float`.

- [ ] **Step 1: Write the failing tests in `tests/test_overlay.py`**

```python
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent

import theme
from metrics import Snapshot
from overlay import MonitorPanel
from settings import Settings


def make_panel(qapp):
    panel = MonitorPanel(Settings())
    panel.show()
    qapp.processEvents()
    return panel


def mouse_event(kind, widget, local_at, global_at, button, buttons):
    """QMouseEvent takes QPointF in PyQt6, not QPoint."""
    return QMouseEvent(
        kind,
        QPointF(local_at),
        QPointF(global_at),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


def test_panel_is_the_documented_size(qapp):
    panel = make_panel(qapp)
    assert panel.width() == theme.WIDTH == 280
    assert panel.height() == theme.HEIGHT == 280


def test_panel_does_not_steal_focus(qapp):
    panel = make_panel(qapp)
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_snapshot_feeds_the_histories(qapp):
    panel = make_panel(qapp)
    for pct in (10.0, 50.0, 90.0):
        panel.apply_snapshot(Snapshot(cpu_pct=pct, ram_used_gb=1.0, ram_total_gb=2.0))
    assert len(panel._histories["cpu_pct"]) == 3
    assert panel._histories["cpu_pct"].values()[-1] == 0.9


def test_unmeasured_rows_do_not_extend_the_history(qapp):
    panel = make_panel(qapp)
    panel.apply_snapshot(Snapshot(cpu_pct=10.0))
    panel.apply_snapshot(Snapshot(cpu_pct=None))
    assert len(panel._histories["cpu_pct"]) == 1


def test_alpha_is_clamped(qapp):
    panel = make_panel(qapp)
    panel.set_alpha(5.0)
    assert panel.current_alpha() == theme.MAX_ALPHA
    panel.set_alpha(-2.0)
    assert panel.current_alpha() == theme.MIN_ALPHA


def test_drag_moves_the_panel(qapp):
    panel = make_panel(qapp)
    panel.move(400, 300)
    start = panel.pos()

    # Press where the cursor is, then move it: the panel must follow the delta.
    press_at, moved_at = QPoint(10, 10), QPoint(70, 50)
    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, press_at, QPoint(410, 310),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, moved_at, QPoint(470, 350),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseReleaseEvent(mouse_event(
        QMouseEvent.Type.MouseButtonRelease, panel, moved_at, QPoint(470, 350),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
    ))

    assert panel.pos() == start + QPoint(60, 40)
    # The drop position is remembered so a restart comes back to the same spot.
    assert panel._settings.x == panel.x()
    assert panel._settings.y == panel.y()


def test_drag_is_clamped_to_the_screen(qapp):
    panel = make_panel(qapp)
    available = panel.screen().availableGeometry()

    panel.mousePressEvent(mouse_event(
        QMouseEvent.Type.MouseButtonPress, panel, QPoint(10, 10), QPoint(20, 20),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    panel.mouseMoveEvent(mouse_event(
        QMouseEvent.Type.MouseMove, panel, QPoint(10, 10), QPoint(99999, 99999),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
    ))
    assert panel.x() <= available.right() - panel.width()
    assert panel.y() <= available.bottom() - panel.height()


def test_double_click_restores_the_corner(qapp):
    panel = make_panel(qapp)
    panel.move(10, 10)
    panel.restore_default_position()
    available = panel.screen().availableGeometry()
    assert panel.x() > available.left()


def test_clamp_keeps_the_panel_on_screen(qapp):
    panel = make_panel(qapp)
    available = panel.screen().availableGeometry()
    clamped = panel.clamp_to_screen(QPoint(-5000, -5000))
    assert clamped.x() >= available.left()
    assert clamped.y() >= available.top()
    clamped = panel.clamp_to_screen(QPoint(99999, 99999))
    assert clamped.x() <= available.right() - panel.width()
    assert clamped.y() <= available.bottom() - panel.height()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_overlay.py -q`

Expected: collection error `ModuleNotFoundError: No module named 'overlay'` — the current
`overlay.py` has no `MonitorPanel`.

- [ ] **Step 3: Write the new `overlay.py`**

```python
"""The panel window: placement, mouse handling, and painting delegation.

Dragging works from the first press. The previous version required a double
click to arm a drag mode, which needed its own double-click detector, two
QTimers and a per-click timer allocation — thirteen commits of fixes grew out
of that one extra step.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QApplication, QWidget

import theme
from history import History
from painter import paint
from settings import Settings

logger = logging.getLogger("widget.overlay")

WINDOW_TITLE = "System Monitor"
CORNER_MARGIN = 10


class MonitorPanel(QWidget):
    menu_requested = pyqtSignal(QPoint)
    quit_requested = pyqtSignal()

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._snapshot = None
        self._histories: dict[str, History] = {
            key: History() for key in theme.METRICS_BY_KEY
        }
        self._drag_origin: QPoint | None = None

        self.setWindowTitle(WINDOW_TITLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(theme.WIDTH, theme.HEIGHT)
        self.apply_window_flags()

    # --- configuration ----------------------------------------------------

    def apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()

    def current_alpha(self) -> float:
        return self._settings.alpha

    def set_alpha(self, value: float) -> None:
        self._settings.alpha = max(theme.MIN_ALPHA, min(theme.MAX_ALPHA, value))
        self.update()

    def apply_snapshot(self, snapshot) -> None:
        self._snapshot = snapshot
        for spec in theme.METRICS:
            self._histories[spec.key].append(theme.row_fraction(spec, snapshot))
        self.update()

    # --- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:
        if self._snapshot is None:
            return
        painter = QPainter(self)
        try:
            paint(painter, self._snapshot, self._histories, self._settings.alpha)
        finally:
            painter.end()

    # --- placement --------------------------------------------------------

    def clamp_to_screen(self, top_left: QPoint) -> QPoint:
        available = self.screen().availableGeometry()
        return QPoint(
            max(available.left(), min(int(top_left.x()), available.right() - self.width())),
            max(available.top(), min(int(top_left.y()), available.bottom() - self.height())),
        )

    def restore_default_position(self) -> None:
        available = self.screen().availableGeometry()
        target = QPoint(
            available.right() - self.width() - CORNER_MARGIN,
            available.top() + CORNER_MARGIN,
        )
        self.move(self.clamp_to_screen(target))
        self._settings.x, self._settings.y = self.x(), self.y()

    def remember_position(self) -> None:
        self._settings.x, self._settings.y = self.x(), self.y()

    # --- input ------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.quit_requested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            grab = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._drag_origin = grab
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is not None:
            target = event.globalPosition().toPoint() - self._drag_origin
            self.move(self.clamp_to_screen(target))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_origin is not None:
            self._drag_origin = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.remember_position()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.restore_default_position()
        event.accept()

    def wheelEvent(self, event) -> None:
        step = 0.05 if event.angleDelta().y() > 0 else -0.05
        self.set_alpha(self._settings.alpha + step)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        self.menu_requested.emit(event.globalPos())
        event.accept()

    def closeEvent(self, event) -> None:
        self.remember_position()
        super().closeEvent(event)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_overlay.py -q`

Expected: `11 passed`.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add overlay.py tests/test_overlay.py
git commit -m "refactor: rewrite the panel widget with immediate drag

Dragging starts on the first press instead of a double click that armed a
drag mode, which removes the manual double-click detector, the two QTimers
and the per-click timer allocation that thirteen commits had been patching.
Alpha now has one source of truth, and the widget paints through
QPainter instead of hosting a QLabel."
```

---

### Task 7: `main.py` — logging, DPI, sampling thread, tray

The sampling thread is the piece that stops the window stuttering: the old
`QTimer` called `psutil.cpu_percent(interval=0.1)` on the GUI thread, freezing
it for a tenth of a second every two seconds.

**Files:**
- Create: `tests/test_main.py`
- Rewrite: `main.py`

**Interfaces:**
- Consumes: `theme`, `metrics.SystemProbe`, `overlay.MonitorPanel`, `settings.load_settings/save_settings/Settings`, `history.HistoryLog`.
- Produces: `Collector(probe, parent=None)` — a `QThread` with signal `sampled = pyqtSignal(object)`, methods `poke()` and `run()`. `enable_acrylic(hwnd: int) -> bool`. `build_tray_icon() -> QIcon`. `MonitorApp` with `shutdown()`. `setup_logging()`. `main() -> int`.

- [ ] **Step 1: Write the failing tests in `tests/test_main.py`**

```python
import threading

from PyQt6.QtCore import QThread

import main as app_main
from metrics import Snapshot


class RecordingProbe:
    """Records which thread sample() ran on."""

    def __init__(self):
        self.sample_threads = []

    def sample(self):
        self.sample_threads.append(threading.current_thread())
        return Snapshot(cpu_pct=1.0)


def test_sampling_runs_off_the_gui_thread(qapp):
    probe = RecordingProbe()
    collector = app_main.Collector(probe)
    delivered = []
    collector.sampled.connect(lambda snap: delivered.append(threading.current_thread()))
    collector.start()

    deadline = 4000
    while not probe.sample_threads and deadline > 0:
        qapp.processEvents()
        QThread.msleep(10)
        deadline -= 10

    collector.requestInterruption()
    collector.poke()
    collector.wait(3000)

    assert probe.sample_threads, "collector never sampled"
    # The blocking part must not be on the GUI thread.
    assert probe.sample_threads[0] is not threading.current_thread()


def test_snapshot_is_delivered_back_on_the_gui_thread(qapp):
    probe = RecordingProbe()
    collector = app_main.Collector(probe)
    delivered = []
    collector.sampled.connect(lambda snap: delivered.append(threading.current_thread()))
    collector.start()

    deadline = 4000
    while not delivered and deadline > 0:
        qapp.processEvents()
        QThread.msleep(10)
        deadline -= 10

    collector.requestInterruption()
    collector.poke()
    collector.wait(3000)

    assert delivered, "no snapshot reached the GUI thread"
    # Queued connections land on the receiver's thread, which is what lets
    # apply_snapshot touch widgets safely.
    assert delivered[0] is threading.current_thread()


def test_collector_stops_cleanly(qapp):
    collector = app_main.Collector(RecordingProbe())
    collector.start()
    qapp.processEvents()
    collector.requestInterruption()
    collector.poke()
    assert collector.wait(3000) is True


def test_enable_acrylic_rejects_a_bogus_handle():
    assert app_main.enable_acrylic(0) is False


def test_tray_icon_is_not_null(qapp):
    icon = app_main.build_tray_icon()
    assert not icon.isNull()


def test_log_handler_rotates():
    handler = app_main.build_log_handler("app_debug.log")
    assert handler.maxBytes == 512 * 1024
    assert handler.backupCount == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_main.py -q`

Expected: failures — no `Collector`, no `enable_acrylic`, no `build_tray_icon`.

- [ ] **Step 3: Write the new `main.py`**

```python
"""Application entry point: DPI policy, sampling thread, tray menu.

Sampling runs on its own thread. psutil.cpu_percent(interval=0.1) blocks for
a tenth of a second, and doing that on the GUI thread froze the panel every
two seconds; here the GUI thread only ever paints.
"""

from __future__ import annotations

import ctypes
import logging
import logging.handlers
import sys
from ctypes import wintypes

from PyQt6.QtCore import (
    QDeadlineTimer,
    QMutex,
    QMutexLocker,
    QPoint,
    QThread,
    QTimer,
    QWaitCondition,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import theme
from history import HistoryLog
from metrics import SystemProbe
from overlay import MonitorPanel
from settings import Settings, load_settings, save_settings

LOG_NAME = "app_debug.log"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUPS = 2
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3

ALPHA_STEPS = (("35%", 0.35), ("50%", 0.50), ("65%", 0.65), ("80%", 0.80), ("100%", 1.00))


def build_log_handler(name: str = LOG_NAME) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        name, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(build_log_handler())
    # The per-metric warning lines are useful; the INFO chatter from failed
    # probes is not, and it was most of what filled the old log.
    logging.getLogger("widget.metrics").setLevel(logging.WARNING)


class Collector(QThread):
    """Samples on a worker thread and emits each Snapshot to the GUI thread."""

    sampled = pyqtSignal(object)

    def __init__(self, probe, parent=None) -> None:
        super().__init__(parent)
        self._probe = probe
        self._mutex = QMutex()
        self._wake = QWaitCondition()

    def poke(self) -> None:
        """Ask for an out-of-band sample, e.g. right after the user opens the menu."""
        with QMutexLocker(self._mutex):
            self._wake.wakeAll()

    def run(self) -> None:
        while not self.isInterruptionRequested():
            self.sampled.emit(self._probe.sample())
            with QMutexLocker(self._mutex):
                self._wake.wait(self._mutex, QDeadlineTimer(theme.TICK_MS))


def enable_acrylic(hwnd: int) -> bool:
    """Ask DWM for real backdrop blur. False when the OS refuses.

    Only reached when the user ticks the setting; the default translucent fill
    needs no ctypes and works from Windows 8 onwards.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        value = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_SYSTEMBACKDROP_TYPE),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0
    except Exception:
        logging.getLogger("widget.main").info("acrylic unavailable", exc_info=True)
        return False


def build_tray_icon() -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.CALM)
        painter.drawRoundedRect(4, 4, 24, 24, 7, 7)
        painter.setBrush(theme.VALUE)
        for index, height in enumerate((8, 13, 18)):
            painter.drawRoundedRect(9 + index * 6, 26 - height, 4, height, 2, 2)
    finally:
        painter.end()
    return QIcon(pixmap)


class MonitorApp:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.history_log = HistoryLog()
        if self.settings.log_history:
            self.settings.log_history = self.history_log.enable()

        self.panel = MonitorPanel(self.settings)
        self.panel.set_alpha(self.settings.alpha)

        self.collector = Collector(SystemProbe(), self.panel)
        self.collector.sampled.connect(self.panel.apply_snapshot)
        self.collector.sampled.connect(self._record_history)

        self.panel.menu_requested.connect(self._show_menu_at)
        self.panel.quit_requested.connect(self.shutdown)

        self.tray = QSystemTrayIcon(build_tray_icon(), self.panel)
        self.tray.setToolTip("System Monitor")
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setContextMenu(self._build_menu())
        self.tray.show()

        self.timer = QTimer(self.panel)
        self.timer.timeout.connect(self.collector.poke)
        self.timer.start(theme.TICK_MS)

        self._restore_position()
        self.panel.show()

        if self.settings.acrylic and not enable_acrylic(int(self.panel.winId())):
            logging.getLogger("widget.main").info("acrylic refused, using translucent fill")
            self.settings.acrylic = False

        self.collector.start()

    # --- wiring -----------------------------------------------------------

    def _record_history(self, snapshot) -> None:
        self.history_log.write(snapshot)

    def _restore_position(self) -> None:
        if self.settings.x is not None and self.settings.y is not None:
            self.panel.move(self.panel.clamp_to_screen(QPoint(self.settings.x, self.settings.y)))
        else:
            self.panel.restore_default_position()
        self.panel.remember_position()

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.panel.restore_default_position()

    def _show_menu_at(self, global_pos) -> None:
        self.collector.poke()
        self.tray.contextMenu().exec(global_pos)

    # --- menu -------------------------------------------------------------

    def _build_menu(self) -> QMenu:
        menu = QMenu()

        reset = QAction("Reset position", menu)
        reset.triggered.connect(self.panel.restore_default_position)
        menu.addAction(reset)

        opacity = menu.addMenu("Opacity")
        for label, value in ALPHA_STEPS:
            action = QAction(label, opacity)
            action.setCheckable(True)
            action.setChecked(abs(self.settings.alpha - value) < 0.001)
            action.triggered.connect(lambda _checked=False, v=value: self._set_alpha(v))
            opacity.addAction(action)

        on_top = QAction("Always on top", menu)
        on_top.setCheckable(True)
        on_top.setChecked(self.settings.always_on_top)
        on_top.toggled.connect(self._set_always_on_top)
        menu.addAction(on_top)

        acrylic = QAction("Acrylic backdrop", menu)
        acrylic.setCheckable(True)
        acrylic.setChecked(self.settings.acrylic)
        acrylic.toggled.connect(self._set_acrylic)
        menu.addAction(acrylic)

        history = QAction("Write history to file", menu)
        history.setCheckable(True)
        history.setChecked(self.settings.log_history)
        history.toggled.connect(self._set_log_history)
        menu.addAction(history)

        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.shutdown)
        menu.addAction(quit_action)
        return menu

    def _set_alpha(self, value: float) -> None:
        self.panel.set_alpha(value)
        self.collector.poke()

    def _set_always_on_top(self, enabled: bool) -> None:
        self.settings.always_on_top = enabled
        self.panel.apply_window_flags()

    def _set_acrylic(self, enabled: bool) -> None:
        self.settings.acrylic = enabled and enable_acrylic(int(self.panel.winId()))
        if enabled and not self.settings.acrylic:
            self.tray.showMessage(
                "System Monitor", "This Windows build refused the acrylic backdrop."
            )

    def _set_log_history(self, enabled: bool) -> None:
        self.settings.log_history = enabled and self.history_log.enable()
        if enabled and not self.settings.log_history:
            self.tray.showMessage("System Monitor", "Could not open metrics_history.log.")

    # --- shutdown ---------------------------------------------------------

    def shutdown(self) -> None:
        self.timer.stop()
        self.collector.requestInterruption()
        self.collector.poke()
        self.collector.wait(3000)
        save_settings(self.settings)
        self.tray.hide()
        QApplication.quit()


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    setup_logging()
    logging.getLogger("widget.main").info("MonitorApp starting")
    MonitorApp()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_main.py -q`

Expected: `6 passed`.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`

Expected: all pass.

- [ ] **Step 6: Run the app for five seconds and confirm it draws**

Run: `python main.py` and watch the top-right corner of the screen.

Expected: a rounded dark panel with four rows, values filled in, graphs building up. Confirm the
right-click context menu, the wheel opacity change, and that the panel is smooth rather than
stuttering. Then middle-click it to quit.

- [ ] **Step 7: Check the log stays small**

Run the app for ten minutes, then: `Get-Item app_debug.log | Select-Object Length`

Expected: under 5120 bytes.

- [ ] **Step 8: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "perf: sample on a worker thread and add a tray menu

psutil.cpu_percent(interval=0.1) used to run on the GUI thread and froze
the panel for 100 ms every two seconds; Collector now samples off-thread and
the GUI thread only paints. Logging rotates at 512 KB and the per-probe
INFO chatter is suppressed. Acrylic stays opt-in behind the documented DWM
call so the default path needs no ctypes."
```

---

### Task 8: Delete `run_widget.py`

It imports `widget.main`, a package that does not exist, and constructs a
second `QApplication`. It has never worked.

**Files:**
- Delete: `run_widget.py`

- [ ] **Step 1: Confirm nothing references it**

Run: `git grep -n "run_widget"` and `Select-String -Path *.md,*.py,*.bat,*.spec -Pattern run_widget`

Expected: only `run_widget.py` itself, or nothing at all.

- [ ] **Step 2: Delete it**

Run: `git rm run_widget.py`

Expected: `rm 'run_widget.py'`.

- [ ] **Step 3: Verify the entry point still works**

Run: `python -c "import ast,sys; ast.parse(open('main.py',encoding='utf-8').read()); print('main.py parses')"`

Expected: `main.py parses`.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore: remove the broken run_widget.py launcher

It imported widget.main, a package that does not exist in this layout, and
built a second QApplication. main.py is the only entry point."
```

---

### Task 9: Fix the PyInstaller spec and stop ignoring it

`Monitor.spec` lists `overlay.py` and `metrics.py` as `datas`, which copies
them next to the executable instead of bundling them as modules. Worse,
`*.spec` in `.gitignore` meant the build spec was never versioned, so the
build was not reproducible at all.

**Files:**
- Modify: `.gitignore`
- Modify: `Monitor.spec`

**Interfaces:**
- Consumes: nothing.
- Produces: a tracked, working `Monitor.spec`.

- [ ] **Step 1: Un-ignore the spec in `.gitignore`**

Replace:

```
# Build artifacts (PyInstaller)
dist/
build/
*.spec
*.exe
*.bin
```

with:

```
# Build artifacts (PyInstaller)
dist/
build/
# Monitor.spec is tracked on purpose: without it the build is not reproducible.
*.spec
!Monitor.spec
*.exe
*.bin
```

- [ ] **Step 2: Rewrite `Monitor.spec`**

```python
# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the system monitor widget.

Modules are collected by Analysis via their imports; nothing is listed in
`datas` except the empty folder marker, because copying .py files next to the
executable is not what a one-file bundle wants.
"""

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['wmi', 'pynvml'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt6.QtWebEngineCore', 'PyQt6.QtWebEngineWidgets', 'PyQt6.Qt3DCore'],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

- [ ] **Step 3: Build and verify**

Run: `build_exe.bat`

Expected: `dist\Monitor.exe` exists.

Then launch `dist\Monitor.exe` and confirm the panel appears with values.

- [ ] **Step 4: Confirm the spec is now tracked**

Run: `git check-ignore -v Monitor.spec` — expect exit code 1 with no output.

Run: `git status --short Monitor.spec` — expect `?? Monitor.spec` or `A  Monitor.spec`.

- [ ] **Step 5: Commit**

```bash
git add .gitignore Monitor.spec
git commit -m "build: track Monitor.spec and bundle modules correctly

The spec listed overlay.py and metrics.py under datas, which copies them
next to the executable instead of collecting them as modules, and the
'*.spec' ignore rule meant the build recipe was never versioned. Adds the
wmi and pynvml hidden imports the dynamic imports need and excludes the
web engine we never touch."
```

---

### Task 10: Update `README.md` and `AGENTS.md`

Both documents describe behaviour that no longer exists, and both mention a
history log that nothing wrote.

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Replace the README controls, colour and structure sections**

The `Controls` table becomes:

```markdown
| Action | Description |
|--------|-------------|
| Drag | Move the panel anywhere on screen |
| Double-click | Snap the panel back to the top-right corner |
| Scroll wheel | Adjust panel opacity |
| Right-click / tray icon | Menu: reset position, opacity, always on top, acrylic backdrop, history file, quit |
| Middle-click | Quit |
| Tray icon double-click | Snap back to the top-right corner |

The random-colour mode from earlier versions is gone: colour now means
load, so it only appears when a metric crosses a threshold.
```

The colour-coding table becomes:

```markdown
| Metric | Normal | Warn | Critical |
|--------|--------|------|----------|
| CPU / RAM / GPU / VRAM | < 80 % | 80–95 % | ≥ 95 % |
| GPU temperature | < 70 °C | 70–80 °C | ≥ 80 °C |
| CPU frequency | no state | — | — |

Colours: calm blue, amber, red. A metric that could not be measured shows
`--` rather than a guess.
```

The logging section becomes:

```markdown
- `app_debug.log` — rotating, 512 KB, 2 backups. Lifecycle and errors only.
- `metrics_history.log` — CSV trace, off by default. Enable from the tray menu.
```

The project-structure block becomes:

```
.
├── main.py          # Entry point, tray menu, sampling thread
├── metrics.py       # Snapshot, SystemProbe, GpuProbe
├── painter.py       # Pure QPainter rendering
├── overlay.py       # Panel window and input
├── theme.py         # Palette, thresholds, geometry
├── history.py       # Ring buffer, resampling, optional CSV
├── settings.py      # Persisted preferences
└── tests/           # pytest suite
```

- [ ] **Step 2: Fix `AGENTS.md`**

Replace the "Key Gotchas" bullet about GPU metrics with:

```markdown
- **GPU metrics**: NVML when an NVIDIA card is present, WMI otherwise. WMI cannot measure load
  or temperature, so those rows show `--`; nothing is estimated. One NVML handle is reused for
  the life of the process.
- **Sampling**: runs on a worker thread. Never call a blocking psutil function from the GUI
  thread — that is what caused the old 100 ms stutter.
- **Layout**: 280 × 280 logical pixels, no manual DPI multiplier. Fonts are in points so Qt
  scales them with the panel.
- **No `.venv`**: this checkout runs on the system interpreter. `AGENTS.md` previously claimed a
  virtualenv that does not exist.
```

- [ ] **Step 3: Verify the docs no longer contradict the code**

Run: `Select-String -Path README.md,AGENTS.md -Pattern "random color|random colour|\.venv\\\\Scripts"`

Expected: no matches for the random-colour claim. A `.venv` mention is acceptable only inside
the corrected bullet.

- [ ] **Step 4: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: bring README and AGENTS.md in line with the redesign

Removes the random-colour mode from the controls table, replaces the
80 percent red/green table with the real three-state thresholds, and
documents that the history CSV is opt-in rather than always written."
```

---

### Task 11: `requirements.txt` dev extras and a final full-suite run

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add a dev requirements file**

Create `requirements-dev.txt`:

```text
-r requirements.txt
pytest>=8.0.0
```

- [ ] **Step 2: Run the full suite fresh**

Run: `python -m pytest -q`

Expected: every test passes, count reported.

- [ ] **Step 3: Confirm no test depends on a real GPU**

Run: `python -m pytest -q -k "real_probe"`

Expected: `1 passed` — that one test intentionally touches the machine, and it asserts only that
RAM total is positive, so it is safe anywhere.

- [ ] **Step 4: Commit**

```bash
git add requirements-dev.txt requirements.txt
git commit -m "build: add requirements-dev.txt with pytest"
```

---

### Task 12: Verify the acceptance criteria end to end

This task changes no code. It runs the seven criteria from the design spec
and records what was actually observed. If a criterion fails, fix it and
commit the fix before claiming completion.

**Files:**
- None.

Two steps below are marked **HUMAN-ONLY**: no subagent can perform them. They are recorded, not
skipped, and the final report must surface them to the user.

- [ ] **Step 1: Panel matches the design at 100 % DPI** — **HUMAN-ONLY**

An agent can launch the app and screenshot it, but judging whether it "matches the design" needs
a human looking at a real desktop. The agent does run the automated half: `python main.py`, take
a screenshot, confirm the panel is 280 × 280 logical pixels with four rows and no clipping.

- [ ] **Step 2: Panel holds up at 150 % DPI** — **HUMAN-ONLY**

Requires changing Windows display scaling and logging off and on. Record as pending for the user;
do not attempt it.

- [ ] **Step 3: No stutter, low CPU use**

Run the app for two minutes, then in PowerShell:

```powershell
Get-Process python | Select-Object Id, CPU, WS
```

Expected: total CPU seconds far below 120 (that is two minutes of wall clock at 100 %), and the
window stays draggable throughout. To prove sampling is off-thread:

Run: `python -c "import main; print(main.Collector.__mro__[1].__name__)"`

Expected: `QThread`.

- [ ] **Step 4: Log stays under 5 KB after ten minutes**

Run: `python main.py`, leave it ten minutes, quit, then:

```powershell
Get-Item app_debug.log | Select-Object Length
```

Expected: under 5120.

- [ ] **Step 5: pytest green including goldens**

Run: `python -m pytest -q`

Expected: all pass. Deliberately break the panel to prove the goldens bite:

Run: change `theme.GRAPH_H` to 20, then `python -m pytest tests/test_painter.py -q`

Expected: FAIL with `drifted by <N>`. Revert to 26 and re-run to green.

- [ ] **Step 6: Behaves without an NVIDIA GPU**

This machine has an RTX 3060, so exercise the fallback directly instead:

Run: `python -c "from metrics import SystemProbe; from metrics import GpuProbe; p=SystemProbe(gpu=GpuProbe(nvml=object())); print(p.sample())"`

Expected: a Snapshot whose gpu fields are None rather than zeros, and no exception.

- [ ] **Step 7: The exe runs from a clean PATH**

Run: `build_exe.bat`, then launch `dist\Monitor.exe`.

Expected: the panel appears with live values.

- [ ] **Step 8: Position and opacity survive a restart**

Run: `python main.py`, drag the panel somewhere, scroll to change opacity, quit with middle-click.
Run `python main.py` again.

Expected: same position, same opacity, and `settings.json` present in the repository root.

- [ ] **Step 9: Commit any fixes the checks surfaced**

If any step needed a code change:

```bash
git add -A
git commit -m "fix: <what the acceptance run caught>"
```

If nothing needed fixing, do not create an empty commit. Report the observed numbers instead.

---

## Out of Scope

Deliberately not built, so it does not creep in unnoticed:

- **Click-through widget.** `Qt.WindowType.WindowTransparentForInput` would let clicks pass
  through the panel entirely, which is genuinely nicer for a desktop overlay, but it also removes
  dragging, the context menu, wheel opacity and middle-click quit. That contradicts the approved
  interaction design, so it stays a possible follow-up rather than a silent change.
- **Real CPU temperature.** Discussed and declined; CPU frequency covers the need without a .NET
  runtime or administrator rights.
- **GPU load on non-NVIDIA hardware.** Requires ETW performance counters; out of proportion for
  this widget. The rows show `--`.
- **A settings dialog.** The tray menu covers every option.
- **Multiple GPUs.** Device index 0 only, as before.
