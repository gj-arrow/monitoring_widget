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

# WIDTH and HEIGHT describe the panel, and every rect above is 0-based inside
# it. The canvas the renderer paints on is the panel plus a margin, because
# the drop shadow is drawn *outside* panel_rect() and a canvas equal to the
# panel clips it away. paint() shifts the whole panel by BLEED; nothing above
# this line moves.
BLEED = 8
CANVAS_W = WIDTH + 2 * BLEED
CANVAS_H = HEIGHT + 2 * BLEED

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


def gpu_temp(snapshot) -> float | None:
    """Auxiliary GPU temperature, or None when the reading is absent.

    Read with getattr so a snapshot that has no temperature field at all
    degrades to "no data" instead of raising AttributeError.
    """
    return getattr(snapshot, "gpu_temp_c", None)


def metric_state(spec: MetricSpec, snapshot) -> int:
    """Worst state for a row, counting its auxiliary reading too."""
    state = state_for(row_value(spec, snapshot), spec.warn, spec.critical)
    temp = gpu_temp(snapshot)
    if spec.key == "gpu_pct" and temp is not None:
        state = max(state, state_for(temp, GPU_TEMP_WARN, GPU_TEMP_CRITICAL))
    return state


def has_any_data(snapshot) -> bool:
    """True when at least one number will be on screen."""
    if any(row_value(spec, snapshot) is not None for spec in METRICS):
        return True
    return gpu_temp(snapshot) is not None


def worst_state(states) -> int:
    return max(states, default=NORMAL)


def state_color(state: int) -> QColor:
    """A fresh copy: QColor is mutable, so callers may adjust alpha on it.

    Handing back _STATE_COLORS[state] would let one caller mutate theme.CALM
    for everyone else in the process.
    """
    return QColor(_STATE_COLORS[state])


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
