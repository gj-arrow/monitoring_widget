"""Panel rendering: a pure function from snapshot to QPainter commands.

No widget state, no timers, no I/O. That is what lets the tests draw into a
QImage with no window on screen and compare the result against a reference.

Layout per row: a 52 px rounded slab holding the label on the left, the value
and its auxiliary reading on the right, and a 26 px graph strip along the
bottom whose filled area is the row's history.

The header carries the status dot -- the worst state across the rows -- and the
network throughput in both directions, which is the panel's one non-metric
reading and is therefore drawn in one neutral colour whatever its magnitude.

The panel is 280 x 280 but the canvas it is painted on is larger: the drop
shadow is drawn outside panel_rect(), and a canvas the same size as the panel
clips it away. paint() shifts everything by theme.BLEED and theme's rects stay
0-based, so no layout arithmetic had to change.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

import theme
from history import History, resample

VALUE_GAP = 7.0
TEXT_TOP = 6.0
TEXT_CLEARANCE = 2.0
GRAPH_LINE_WIDTH = 1.4
GRAPH_FILL_ALPHA = 0.22

NET_LABEL_GAP = "  "
DOWN_LABEL = "DN"
UP_LABEL = "UP"

# Decimal, unlike the binary GB the memory rows use. The unit has to change
# where the figure resets to 1.0 -- 999.9 KB/s, then 1.0 MB/s -- which is what
# every network tool on the machine does and what keeps "1024.0 KB/s" from ever
# being printed. Memory stays binary because that is what the OS reports.
_KB = 1000
_MB = 1000 ** 2
_TB = 1000 ** 4


def paint(
    painter: QPainter,
    snapshot,
    histories: dict[str, History],
    alpha: float,
) -> None:
    """Draw the whole panel. `histories` may be missing keys; rows degrade.

    The paint target must be at least theme.CANVAS_W x theme.CANVAS_H; the
    translate below leaves room for the drop shadow outside the panel, and the
    matching restore() puts the caller's transform back.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.translate(theme.BLEED, theme.BLEED)

    _draw_panel(painter, theme.panel_rect(), alpha)
    _draw_header(painter, theme.header_rect(), snapshot)

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


def _draw_header(painter: QPainter, rect: QRectF, snapshot) -> None:
    if theme.has_any_data(snapshot):
        state = theme.worst_state(theme.metric_state(spec, snapshot) for spec in theme.METRICS)
        dot_color = theme.state_color(state)
    else:
        dot_color = theme.NEUTRAL

    centre_y = rect.center().y()
    # Both edges come from theme, so the dot and the text cannot end up placed
    # against different lines. The dot hangs off the text's left edge by a fixed
    # gap, which is what kept the header and the row labels on one alignment.
    text_x = theme.header_text_x()
    painter.setBrush(dot_color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(theme.header_dot_x(), centre_y), theme.DOT_R, theme.DOT_R)

    text = _format_net(snapshot)
    if text:
        _draw_text(painter, text, QRectF(text_x, rect.top(), rect.right() - text_x, rect.height()),
                   theme.aux_font(), theme.NEUTRAL)


def _format_net(snapshot) -> str:
    """Download and upload throughput, in that order, as one string.

    Download first, which is the order metrics.NET_KEYS declares and therefore
    the order the CSV records the two in: the panel and the trace agree on which
    figure is which without a legend. One drawText call rather than two -- the two
    directions belong together as a single reading, and one call means one string
    that either fits the header or does not.
    """
    return f"{DOWN_LABEL} {_format_rate(snapshot.net_down_bytes_per_sec)}" \
           f"{NET_LABEL_GAP}{UP_LABEL} {_format_rate(snapshot.net_up_bytes_per_sec)}"


def _format_rate(value: float | None) -> str:
    """Bytes per second in the unit its magnitude calls for.

    Whole bytes below a kilobyte, one decimal above it. A two-second delta that
    small moves in ones, so a decimal place claims a resolution the counter does
    not have; above a kilobyte the figure moves by whole units often enough that
    one decimal is the finest change which still reads as a change rather than
    as the smoothing factor's own noise.

    The unit list stops at MB/s, so there is a saturating arm from one terabyte
    up. Every other counter-derived number in metrics.py is bounded at the source,
    but this one cannot be: a rate is a difference of two monotonically rising
    counters divided by an interval that is checked for positivity, so nothing
    upstream can produce an absurd value and nothing upstream can rule one out
    either. The arm is here rather than in that code because an enormous rate is
    not *invalid* -- it is unrenderable, and "at least" is the honest monotone
    statement about a number known to be huge with no bound to quote it against.
    1e12 rendered a 302-character string, overrunning the header by more than its
    own width. The arm is on the magnitude, so a negative input saturates too:
    the probe cannot produce one, and `>=` is what the string already claims.
    """
    if value is None:
        return "--"
    magnitude = abs(value)
    if magnitude >= _TB:
        return ">= 1000.0 TB/s"
    if magnitude >= _MB:
        return f"{value / _MB:.1f} MB/s"
    if magnitude >= _KB:
        return f"{value / _KB:.1f} KB/s"
    return f"{value:.0f} B/s"


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
    """The derived clock beside the nominal one it was derived from.

    Two decimals, because the derived figure moves in the hundredths of a GHz:
    99.0% of nominal idle and 99.5% loaded on this machine, so 4460 and 4501
    both read "4.5" at one place and the row looks frozen even though the
    reading is alive.

    With no derived clock the answer is a dash. The nominal is a ceiling, not a
    measurement of what the core is running at, and printing it in the live
    position is the frozen-constant bug this replaces -- so it is only shown
    beside a clock that was actually derived.
    """
    live = snapshot.cpu_live_mhz
    if live is None:
        return "--"
    nominal = snapshot.cpu_nominal_mhz
    if nominal:
        return f"{live / 1000:.2f} / {nominal / 1000:.2f} GHz"
    return f"{live / 1000:.2f} GHz"
