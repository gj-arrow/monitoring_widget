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

    # The value is placed first, then the auxiliary reading is pushed out to the
    # right edge, so left to right it reads "34%  4.5 / 4.5 GHz": the dimmer text
    # trails the number it qualifies instead of preceding it.
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