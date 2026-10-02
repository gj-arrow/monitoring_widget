"""Panel rendering: a pure function from snapshot to QPainter commands.

No widget state, no timers, no I/O. That is what lets the tests draw into a
QImage with no window on screen and compare the result against a reference.

Layout per row: a 52 px rounded slab holding the label on the left, the value
and its auxiliary reading on the right, and a 26 px graph strip along the
bottom whose filled area is the row's history.

The panel is 280 x 280 but the canvas it is painted on is larger: the drop
shadow is drawn outside panel_rect(), and a canvas the same size as the panel
clips it away. paint() shifts everything by theme.BLEED and theme's rects stay
0-based, so no layout arithmetic had to change.
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

import theme
from history import History, resample

VALUE_GAP = 7.0
TEXT_TOP = 6.0
TEXT_CLEARANCE = 2.0
GRAPH_LINE_WIDTH = 1.4
GRAPH_FILL_ALPHA = 0.22


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

    The paint target must be at least theme.CANVAS_W x theme.CANVAS_H; the
    translate below leaves room for the drop shadow outside the panel, and the
    matching restore() puts the caller's transform back.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.translate(theme.BLEED, theme.BLEED)

    _draw_panel(painter, theme.panel_rect(), alpha)
    _draw_header(painter, theme.header_rect(), snapshot, now)

    for spec, rect in theme.metric_rects():
        _draw_row(painter, spec, rect, snapshot, histories.get(spec.key))

    painter.restore()


def _draw_panel(painter: QPainter, rect: QRectF, alpha: float) -> None:
    painter.setPen(Qt.PenStyle.NoPen)

    # Fake a soft drop shadow: three expanding slabs, biggest and faintest first.
    # Grown by `grow` on every side. QRectF.right() and .bottom() are edge
    # coordinates, so adjust() takes a plain `grow` for them: the old table
    # passed grow * 2 there and pushed the right and bottom lips 12.0 and 7.2 px
    # out, which is 4 px past the canvas on the right and 0.8 px from clipping
    # at the bottom, while the left side had all 8 px of the bleed to itself.
    for grow, strength in ((6.0, 0.10), (3.0, 0.16), (0.0, 0.28)):
        shadow = QRectF(rect)
        shadow.adjust(-grow, -grow, grow, grow)
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
        # Clipped to the slab's rounded outline. The fill is a polygon and the
        # line has round caps, so unclipped both paint straight across the
        # corner arcs and out over the panel padding. save()/restore() keeps the
        # clip, pen and brush from leaking into the text drawn afterwards.
        painter.save()
        _clip_to_rounded(painter, rect, theme.ROW_RADIUS)
        _draw_graph(painter, graph, history.values(), color)
        painter.restore()

    # The band stops short of the graph strip: the value font's descent reaches
    # 0.64 px past its baseline, so a band flush with the strip would put
    # descenders on the fill.
    text_rect = QRectF(
        rect.left() + theme.ROW_TEXT_INSET,
        rect.top() + TEXT_TOP,
        rect.width() - 2 * theme.ROW_TEXT_INSET,
        rect.height() - TEXT_TOP - theme.GRAPH_H - TEXT_CLEARANCE,
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


def _clip_to_rounded(painter: QPainter, rect: QRectF, radius: float) -> None:
    """Confine drawing to a rounded rect, corners included."""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.setClipPath(path, Qt.ClipOperation.IntersectClip)


def _draw_graph(painter: QPainter, rect: QRectF, values: list[float], color: QColor) -> None:
    """Fill and stroke the row's history inside `rect`.

    `values` holds at least two samples -- the caller checks -- so resample
    cannot come back short and there is nothing to guard against here.
    """
    points = resample(values, max(2, int(rect.width())))
    bottom = rect.bottom()
    left = rect.left()

    # resample() returns column indices counting from zero. They are not panel
    # coordinates: used raw they put the graph 14 px left of its slab and leave
    # the same width bare on the right.
    area = QPainterPath()
    area.moveTo(points[0].x() + left, bottom)
    for point in points:
        area.lineTo(point.x() + left, bottom - point.y() * rect.height())
    area.lineTo(points[-1].x() + left, bottom)
    area.closeSubpath()

    fill = QColor(color)
    fill.setAlphaF(GRAPH_FILL_ALPHA)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawPath(area)

    line = QPainterPath()
    line.moveTo(points[0].x() + left, bottom - points[0].y() * rect.height())
    for point in points[1:]:
        line.lineTo(point.x() + left, bottom - point.y() * rect.height())

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
            # theme.gpu_temp(), not snapshot.gpu_temp_c: the accessor exists so
            # a snapshot without the field degrades to "--" instead of raising
            # AttributeError out of paint().
            temp = theme.gpu_temp(snapshot)
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
