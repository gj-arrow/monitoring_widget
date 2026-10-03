"""Design tokens for the monitor panel.

Single source of truth for palette, thresholds, geometry and typography.
Imports nothing from the project, so both the renderer and its tests can use
it without pulling in widgets.

Sizes are logical pixels with no manual DPI multiplier: Qt 6 applies
per-monitor scaling itself. Fonts are in points so they scale with the panel
instead of staying pinned to the pixel grid.

Geometry and type are not module constants. They are reached through a
`Layout`, which holds one number -- a scale -- and derives every dimension and
font size from it. That is the whole reason they are not constants: a module
level `WIDTH` that the renderer reads is a value the renderer cannot be told
about, so a second render at a different size would have to mutate it first,
and every image the tests compare would then depend on what was drawn before it.
`painter.paint()` takes a Layout and mutates nothing, which is what lets it be
compared against a golden and drawn into a QImage with no window on screen.
"""

from __future__ import annotations

import math
import time
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

# --- the design at scale 1.00 ------------------------------------------------
#
# The panel as it was drawn before it could be resized, and the only place any
# of these numbers exist. Private on purpose: a public module constant of the
# same name would be a second answer to "how wide is the panel" that some module
# could reach and use unscaled, and the whole point of Layout is that there is
# one number and everything else follows from it.
#
# Sizes are logical pixels; the three font sizes are points.
_BASE_WIDTH = 280
_BASE_PAD_X = 14
_BASE_PAD_TOP = 13
_BASE_PAD_BOTTOM = 12
_BASE_HEADER_H = 23
_BASE_ROW_H = 52
_BASE_ROW_GAP = 8
_BASE_GRAPH_H = 26
_BASE_ROW_TEXT_INSET = 9
_BASE_BLEED = 8
_BASE_LABEL_PT = 7.5
_BASE_VALUE_PT = 12.0
_BASE_AUX_PT = 8.5

# Radii, the gaps beside them and the graph's pen width are shapes rather than
# positions, and are scaled as floats. Rounding them was tried and is worse: the
# status dot's 3 px radius comes out as 2, 3, 3 at the three steps -- 2.55 rounds
# to the same pixel as 3.00 -- so a 0.75 dot against a 0.85 one, and a rounding
# artefact of a three-pixel number rather than a decision. Nothing needs these to
# be whole: antialiasing is what fractional shapes are for, and the slab outlines
# and the stroke land where the design puts them.
_BASE_PANEL_RADIUS = 14.0
_BASE_ROW_RADIUS = 7.0
_BASE_DOT_R = 3.0
_BASE_DOT_GAP = 4.0
_BASE_VALUE_GAP = 7.0
_BASE_TEXT_TOP = 6.0
_BASE_TEXT_CLEARANCE = 2.0
_BASE_GRAPH_LINE_WIDTH = 1.4
# The panel's own hairline border. A shape like the rest, and scaled as a float
# rather than left at one physical pixel: the only drawing dimension that used to
# be a literal in painter.py was this one, which made the border a third heavier
# on a panel a third the size -- and invisible to the test that walks every
# Layout dimension, because a number that was never in the Layout cannot fail to
# shrink.
_BASE_OUTLINE_W = 1.0

FAMILY = "Segoe UI Variable Display"

# Placement, not panel geometry: how far from the screen corner the panel comes
# to rest. It lives here beside the other tokens a test quotes rather than in
# overlay.py, which is the same reasoning that put WHEEL_ALPHA_STEP here.
#
# It does not scale, and unlike the bleed it has no argument for doing so: this
# is the gap between the panel and the edge of the screen, not part of the
# panel's own artwork. 10 px reads as 10 px whichever size the panel is.
CORNER_MARGIN = 10


def _px(base: float, scale: float) -> int:
    """`base` scaled, as a whole number of pixels.

    Rounded, and ties going up rather than to even: a panel is drawn on the
    pixel grid, and 14 px of padding scaled to 10.5 is 11 rather than 10, which
    is the direction that leaves the header more room rather than less. The
    float error is the reason this is not int(base * scale) -- 280 * 0.85 is
    237.99999999999997, which would be a pixel short of the panel it is meant to
    be. Nothing here is negative, so the truncation in int() is towards zero and
    the half is the only thing being decided.
    """
    return int(base * scale + 0.5)


@dataclass(frozen=True, slots=True)
class Layout:
    """Every dimension and font size of the panel at one scale.

    One stored field and a property per dimension, rather than a field per
    dimension filled in by a factory. A factory that computed twenty numbers
    from a scale has twenty places where a dimension could be left behind at
    1.00, and each one of those still passes every "is the layout coherent"
    check -- the rows still tile the panel, the canvas is still the panel plus
    its margin -- because the only thing wrong is that a quarter of the panel is
    now padding. Deriving them cannot be wrong in that way, and it makes two
    layouts equal exactly when their scales are.

    Pixel dimensions that *position* things are whole numbers; the radii, gaps
    and pen widths that *shape* them are not, and font sizes stay in points. A
    pixel size would pin the panel to one monitor's DPI, and a rounded radius
    loses the small ones -- see the note on the base shapes.
    `scale` is any positive finite number: which scales the panel *offers* is
    SCALE_STEPS, and that is a product decision rather than a property of a box.
    """

    scale: float

    def __post_init__(self) -> None:
        # Zero collapses the panel to nothing and NaN compares false against
        # everything, so both would reach QPainter as geometry that cannot be
        # drawn and fail as a panel missing rather than as a bad number.
        if not math.isfinite(self.scale) or self.scale <= 0.0:
            raise ValueError(f"a panel cannot be laid out at scale {self.scale!r}")

    # --- the panel's box ----------------------------------------------------

    @property
    def width(self) -> int:
        return _px(_BASE_WIDTH, self.scale)

    @property
    def height(self) -> int:
        """Derived from the rows rather than stored beside them.

        Four rows of row_h with row_gap between them, under a header and between
        two paddings. Storing this as a number would be a second statement of
        the same sum, and the two could differ by a pixel at a scale where the
        parts round and the total does not.
        """
        return (
            _px(_BASE_PAD_TOP, self.scale)
            + _px(_BASE_HEADER_H, self.scale)
            + len(METRICS) * _px(_BASE_ROW_H, self.scale)
            + (len(METRICS) - 1) * _px(_BASE_ROW_GAP, self.scale)
            + _px(_BASE_PAD_BOTTOM, self.scale)
        )

    @property
    def pad_x(self) -> int:
        return _px(_BASE_PAD_X, self.scale)

    @property
    def pad_top(self) -> int:
        return _px(_BASE_PAD_TOP, self.scale)

    @property
    def pad_bottom(self) -> int:
        return _px(_BASE_PAD_BOTTOM, self.scale)

    @property
    def header_h(self) -> int:
        return _px(_BASE_HEADER_H, self.scale)

    @property
    def row_h(self) -> int:
        return _px(_BASE_ROW_H, self.scale)

    @property
    def row_gap(self) -> int:
        return _px(_BASE_ROW_GAP, self.scale)

    @property
    def graph_h(self) -> int:
        return _px(_BASE_GRAPH_H, self.scale)

    @property
    def row_text_inset(self) -> int:
        return _px(_BASE_ROW_TEXT_INSET, self.scale)

    @property
    def bleed(self) -> int:
        """The transparent margin the drop shadow is drawn into.

        Scales with everything else, and that is the argument for it rather than
        an accident: the bleed is not padding around the design, it is the room
        the panel's own drop shadow needs -- painter.py draws the shadow outside
        panel_rect() precisely so it can fade to nothing against the desktop. A
        margin sized for one particular shadow stops being that the moment the
        shadow shrinks, and the size it *does* keep is a constant nobody chose.
        Scaling both together holds the shadow-to-margin ratio, which is the
        relationship that was designed, at every scale.

        The cost is honest: at 0.75 the shadow's outer edge stops 1.5 px short
        of the canvas edge rather than 2 px, so the test that says the shadow
        does not reach the canvas is stated as a share of the bleed rather than
        as a fixed number of pixels.
        """
        return _px(_BASE_BLEED, self.scale)

    @property
    def canvas_w(self) -> int:
        return self.width + 2 * self.bleed

    @property
    def canvas_h(self) -> int:
        return self.height + 2 * self.bleed

    # --- what shapes a corner or a stroke -----------------------------------

    @property
    def panel_radius(self) -> float:
        return _BASE_PANEL_RADIUS * self.scale

    @property
    def row_radius(self) -> float:
        return _BASE_ROW_RADIUS * self.scale

    @property
    def dot_r(self) -> float:
        return _BASE_DOT_R * self.scale

    @property
    def dot_gap(self) -> float:
        return _BASE_DOT_GAP * self.scale

    @property
    def value_gap(self) -> float:
        """Between a row's value and the auxiliary reading that qualifies it."""
        return _BASE_VALUE_GAP * self.scale

    @property
    def text_top(self) -> float:
        """From the top of a row to the top of its band of text."""
        return _BASE_TEXT_TOP * self.scale

    @property
    def text_clearance(self) -> float:
        """What keeps a descender off the graph strip."""
        return _BASE_TEXT_CLEARANCE * self.scale

    @property
    def graph_line_width(self) -> float:
        return _BASE_GRAPH_LINE_WIDTH * self.scale

    @property
    def outline_w(self) -> float:
        """The panel's own hairline border, in the same units as a pen width."""
        return _BASE_OUTLINE_W * self.scale

    # --- type ---------------------------------------------------------------

    @property
    def label_pt(self) -> float:
        return _BASE_LABEL_PT * self.scale

    @property
    def value_pt(self) -> float:
        return _BASE_VALUE_PT * self.scale

    @property
    def aux_pt(self) -> float:
        return _BASE_AUX_PT * self.scale

    def label_font(self) -> QFont:
        return _font(self.label_pt, caps=True, bold=True)

    def value_font(self) -> QFont:
        return _font(self.value_pt, bold=True)

    def aux_font(self) -> QFont:
        return _font(self.aux_pt, bold=True)

    # --- rects --------------------------------------------------------------

    def panel_rect(self) -> QRectF:
        return QRectF(0.0, 0.0, float(self.width), float(self.height))

    def header_rect(self) -> QRectF:
        return QRectF(
            float(self.pad_x), float(self.pad_top),
            float(self.width - 2 * self.pad_x), float(self.header_h),
        )

    def header_text_x(self) -> float:
        """Left edge of the header's text, aligned with the row labels beneath it.

        Stated here rather than beside the renderer so the dot and the text cannot
        be placed against two different edges of the panel.
        """
        return float(self.pad_x + self.row_text_inset)

    def header_dot_x(self) -> float:
        """Centre of the status dot, hanging off the header text's left edge.

        The dot is the panel's alarm -- the worst state across the rows -- so it
        sits left of the header's one reading and is never coloured by that reading.
        """
        return self.header_text_x() - self.dot_gap - 2.0 * self.dot_r

    def metric_rects(self) -> tuple[tuple[MetricSpec, QRectF], ...]:
        out: list[tuple[MetricSpec, QRectF]] = []
        y = self.pad_top + self.header_h
        for spec in METRICS:
            out.append((spec, QRectF(
                float(self.pad_x), float(y),
                float(self.width - 2 * self.pad_x), float(self.row_h),
            )))
            y += self.row_h + self.row_gap
        return tuple(out)

    def row_text_rect(self, rect: QRectF) -> QRectF:
        """The band inside a row that its label, value and auxiliary reading sit in.

        Stops short of the graph strip by text_clearance: the value font's descent
        reaches 0.64 px past its baseline, so a band flush with the strip would put
        descenders on the fill.
        """
        return QRectF(
            rect.left() + self.row_text_inset,
            rect.top() + self.text_top,
            rect.width() - 2 * self.row_text_inset,
            rect.height() - self.text_top - self.graph_h - self.text_clearance,
        )

CALM = QColor("#5ac8fa")
WARN_COLOR = QColor("#ffb454")
CRITICAL_COLOR = QColor("#ff5c5c")
NEUTRAL = QColor("#8b8f99")
SUBTLE = QColor("#9aa3b0")
VALUE = QColor("#f2f3f5")

ROW_BG = QColor(255, 255, 255, 9)

HISTORY_LEN = 60
TICK_MS = 2000
MIN_ALPHA = 0.35
MAX_ALPHA = 1.0
DEFAULT_ALPHA = 0.80
WHEEL_ALPHA_STEP = 0.05

# The scales the panel can be drawn at. One tuple, read by the tray menu that
# builds the Scale submenu's rows and by the loader that validates the saved
# setting, so a step cannot be offered without being loadable or loaded without
# being offered.
#
# Below 1.00 only. Steps larger than the current size were offered once and
# declined, so max() here is a decision rather than the end of a list: 1.00 is
# the design the three committed golden images were rendered at, and it is the
# default.
SCALE_STEPS = (0.75, 0.85, 1.00)
DEFAULT_SCALE = 1.00

# How old a snapshot may be before the panel stops claiming its numbers are
# current, in ticks. Three, not one: the collector samples every TICK_MS, so a
# one-tick threshold would call the panel stale in the ordinary gap between two
# samples and on any frame the GUI thread was late delivering. Three is two
# missed ticks of slack -- a late tick, or one sample that took as long as a
# tick -- and 6 s is still plainly "broken" rather than "hiccuped" to anyone
# watching. Two would be the same argument one tick tighter; one is wrong.
STALE_TICKS = 3
STALE_AFTER_S = STALE_TICKS * TICK_MS / 1000.0

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


def snapshot_is_stale(snapshot, now: float | None = None) -> bool:
    """True when the snapshot is older than STALE_AFTER_S.

    The panel's one piece of evidence that its numbers are current. The header
    carries no age figure -- that was asked for and is not coming back -- so a
    collector that wedged would otherwise leave the last readings on screen with
    the status dot still lit in whatever state those readings were in, which is
    a healthy-looking panel reporting a machine nobody has measured in minutes.

    The boundary is exclusive: a reading exactly STALE_AFTER_S old is not stale
    yet, because the threshold is where the panel stops claiming currency and
    not where it starts.

    A missing timestamp is never stale. `ts` defaults to 0.0, so "absent" and
    "epoch zero" are the same value, and treating either as an age of 56 years
    would grey the dot on a panel that has never claimed to have measured
    anything -- which is what has_any_data() is already for.
    """
    stamp = getattr(snapshot, "ts", 0.0)
    if not stamp:
        return False
    if now is None:
        now = time.time()
    return now - stamp > STALE_AFTER_S


def worst_state(states) -> int:
    return max(states, default=NORMAL)


def state_color(state: int) -> QColor:
    """A fresh copy: QColor is mutable, so callers may adjust alpha on it.

    Handing back _STATE_COLORS[state] would let one caller mutate theme.CALM
    for everyone else in the process.
    """
    return QColor(_STATE_COLORS[state])


def _font(size_pt: float, *, caps: bool = False, bold: bool = False) -> QFont:
    font = QFont(FAMILY)
    font.setPointSizeF(size_pt)
    if caps:
        font.setCapitalization(QFont.Capitalization.AllUppercase)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    font.setFeature(QFont.Tag("tnum"), 1)
    return font
