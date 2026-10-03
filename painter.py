"""Panel rendering: a pure function from snapshot to QPainter commands.

No widget state, no timers, no I/O. That is what lets the tests draw into a
QImage with no window on screen and compare the result against a reference.

Layout per row: a 52 px rounded slab holding the label on the left, the value
and its auxiliary reading on the right, and a 26 px graph strip along the
bottom whose filled area is the row's history. Every one of those numbers, and
every font size, comes from the `Layout` this function is handed rather than
from module state: `theme.Layout` holds one scale and derives the rest, so
drawing this panel at 0.75 is a different argument and not a different run.

The header carries the status dot -- the worst state across the rows -- and the
network throughput in both directions, which is the panel's one non-metric
reading and is therefore drawn in one neutral colour whatever its magnitude.

The panel is 280 x 280 at scale 1.0, and the canvas it is painted on is larger:
the drop shadow is drawn outside panel_rect(), and a canvas the same size as
the panel clips it away. paint() shifts everything by the layout's bleed and the
layout's rects stay 0-based, so no layout arithmetic had to change.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen

import theme
from history import History, resample

GRAPH_FILL_ALPHA = 0.22

NET_LABEL_GAP = "  "
# What goes in and what goes out of the machine, rather than the cable and its
# direction: "UP" reads as the upload on a panel whose other row says "DN", and
# as the faster of two cables on anything else. The pair is also uneven -- OUT is
# a character wider -- so the header fit is measured (tests/test_painter.py
# rasterises the widest string against a header with no text at all) rather than
# assumed from the labels being two letters each.
DOWN_LABEL = "IN"
UP_LABEL = "OUT"

# The drop shadow's three slabs: how far each one is grown on every side, and
# how much of the desktop it blacks out. Grown as a share of the bleed rather
# than in absolute pixels, because the bleed is the room the shadow has: it is
# scaled with the panel, and a shadow that stayed 6 px on a 210 px panel would
# be a heavier halo than the one the design shows at 280 px. As shares of 8 the
# three grows are exactly 6, 3 and 0.
#
# QRectF.right() and .bottom() are edge coordinates, so adjust() takes a plain
# `grow` for them: passing grow * 2 there pushed the right and bottom lips 12.0
# and 7.2 px out, which was 4 px past the canvas on the right and 0.8 px from
# clipping at the bottom, while the left side had all 8 px of the bleed to
# itself.
SHADOW_SLABS = ((0.75, 0.10), (0.375, 0.16), (0.0, 0.28))

# Decimal, unlike the binary GB the memory rows use. The unit has to change
# where the figure resets to 1.0 -- 999.9 KB/s, then 1.0 MB/s -- which is what
# every network tool on the machine does and what keeps "1024.0 KB/s" from ever
# being printed. Memory stays binary because that is what the OS reports.
_KB = 1000
_MB = 1000 ** 2
_TB = 1000 ** 4


def paint(
    painter: QPainter,
    layout: theme.Layout,
    snapshot,
    histories: dict[str, History],
    alpha: float,
    now: float | None = None,
) -> None:
    """Draw the whole panel. `histories` may be missing keys; rows degrade.

    `layout` is an argument and not a module lookup because it is the only thing
    that decides how large the panel is: a renderer that read its own geometry
    would have to be told the scale out of band, and two renders at two scales
    could not then be compared without whatever was drawn in between. Nothing
    here mutates it -- it is a frozen value object -- so the same function gives
    the same pixels for the same inputs at any scale.

    The paint target must be at least layout.canvas_w x layout.canvas_h; the
    translate below leaves room for the drop shadow outside the panel, and the
    matching restore() puts the caller's transform back.

    `now` is the wall clock the panel is being painted at, and it is an
    argument rather than a call to time.time() so the status dot's staleness is
    a function of the inputs like everything else here. Left None in
    production, where the answer is the clock; the tests pass the snapshot's own
    ts, which makes "as fresh as this snapshot" the default reading and keeps a
    wall clock from moving a pixel between two renders.
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.translate(layout.bleed, layout.bleed)

    _draw_panel(painter, layout, layout.panel_rect(), alpha)
    _draw_header(painter, layout, layout.header_rect(), snapshot, now)

    for spec, rect in layout.metric_rects():
        _draw_row(painter, layout, spec, rect, snapshot, histories.get(spec.key))

    painter.restore()


def _draw_panel(painter: QPainter, layout: theme.Layout, rect: QRectF, alpha: float) -> None:
    painter.setPen(Qt.PenStyle.NoPen)

    # Fake a soft drop shadow: three expanding slabs, biggest and faintest first.
    for share, strength in SHADOW_SLABS:
        grow = share * layout.bleed
        shadow = QRectF(rect)
        shadow.adjust(-grow, -grow, grow, grow)
        tint = QColor(0, 0, 0)
        tint.setAlphaF(strength)
        painter.setBrush(tint)
        painter.drawRoundedRect(shadow, layout.panel_radius + grow, layout.panel_radius + grow)

    fill = QColor(16, 18, 24)
    fill.setAlphaF(max(0.0, min(1.0, alpha)))
    painter.setBrush(fill)
    painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
    painter.drawRoundedRect(rect, layout.panel_radius, layout.panel_radius)
    painter.setPen(Qt.PenStyle.NoPen)


def _draw_header(
    painter: QPainter, layout: theme.Layout, rect: QRectF, snapshot, now: float | None
) -> None:
    dot_color = _dot_color(snapshot, now)

    centre_y = rect.center().y()
    # Both edges come from the layout, so the dot and the text cannot end up
    # placed against different lines. The dot hangs off the text's left edge by a
    # fixed gap, which is what kept the header and the row labels on one
    # alignment.
    text_x = layout.header_text_x()
    painter.setBrush(dot_color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(layout.header_dot_x(), centre_y), layout.dot_r, layout.dot_r)

    text = _format_net(snapshot)
    if text:
        _draw_text(painter, text, QRectF(text_x, rect.top(), rect.right() - text_x, rect.height()),
                   layout.aux_font(), theme.NEUTRAL)


def _dot_color(snapshot, now: float | None) -> QColor:
    """The status dot: the worst state across the rows, or neutral.

    Two ways to draw nothing alarming, and both are the same grey. No data at
    all, and -- the reason `now` exists -- data that has stopped arriving. The
    header carries no age figure, so without this a wedged collector leaves the
    last readings on screen with a lit dot: a panel reporting a machine nobody
    has measured, in the colours of whenever it was last measured.

    Staleness is checked before the state, not after, so it costs no extra
    branch on the common path and cannot be argued with by a row that happens
    to be critical.
    """
    if theme.snapshot_is_stale(snapshot, now):
        return theme.NEUTRAL
    if theme.has_any_data(snapshot):
        return theme.state_color(
            theme.worst_state(theme.metric_state(spec, snapshot) for spec in theme.METRICS)
        )
    return theme.NEUTRAL


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
    own width. The arm is on the magnitude, with the sign carried into the string, so
    each side gets a statement that is true of it: `>= 1000.0 TB/s` above and
    `<= -1000.0 TB/s` below. Testing the signed value alone would look more
    honest and would not be -- it bounds only the positive side and hands the
    negative one straight back to the megabyte branch, where -1e300 is that same
    302-character string. The probe cannot produce a negative rate at all
    (`moved < 0` is caught upstream), so nothing real is lost either way.
    """
    if value is None:
        return "--"
    magnitude = abs(value)
    if magnitude >= _TB:
        return ">= 1000.0 TB/s" if value > 0 else "<= -1000.0 TB/s"
    if magnitude >= _MB:
        return f"{value / _MB:.1f} MB/s"
    if magnitude >= _KB:
        return f"{value / _KB:.1f} KB/s"
    return f"{value:.0f} B/s"


def _draw_row(
    painter: QPainter, layout: theme.Layout, spec, rect: QRectF, snapshot, history
) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.ROW_BG)
    painter.drawRoundedRect(rect, layout.row_radius, layout.row_radius)

    state = theme.metric_state(spec, snapshot)
    color = theme.state_color(state)

    if history is not None and len(history) > 1:
        graph = QRectF(rect)
        graph.setTop(rect.bottom() - layout.graph_h)
        # Clipped to the slab's rounded outline. The fill is a polygon and the
        # line has round caps, so unclipped both paint straight across the
        # corner arcs and out over the panel padding. save()/restore() keeps the
        # clip, pen and brush from leaking into the text drawn afterwards.
        painter.save()
        _clip_to_rounded(painter, rect, layout.row_radius)
        _draw_graph(painter, layout, graph, history.values(), color)
        painter.restore()

    text_rect = layout.row_text_rect(rect)
    _draw_text(painter, spec.label, text_rect, layout.label_font(), theme.NEUTRAL)

    value_text = _format_value(spec, snapshot)
    aux_text = _format_aux(spec, snapshot)
    value_font = layout.value_font()
    aux_font = layout.aux_font()

    # The value is placed first, then the auxiliary reading is pushed out to the
    # right edge, so left to right it reads "29.4  / 32.0 GB": the dimmer text
    # trails the number it qualifies instead of preceding it. With no auxiliary
    # the offset is zero and the value sits on the right edge alone, which is
    # what the CPU row does -- its value is the row, and it is not padded out
    # into a second slot to look like the others.
    aux_width = QFontMetricsF(aux_font).horizontalAdvance(aux_text) if aux_text else 0.0
    _draw_text(
        painter, value_text, text_rect, value_font, color,
        right=True, right_offset=-(aux_width + layout.value_gap) if aux_text else 0.0,
    )
    if aux_text:
        _draw_text(painter, aux_text, text_rect, aux_font, theme.SUBTLE, right=True)


def _clip_to_rounded(painter: QPainter, rect: QRectF, radius: float) -> None:
    """Confine drawing to a rounded rect, corners included."""
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.setClipPath(path, Qt.ClipOperation.IntersectClip)


def _draw_graph(
    painter: QPainter, layout: theme.Layout, rect: QRectF, values: list[float], color: QColor
) -> None:
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

    pen = QPen(color, layout.graph_line_width)
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
    """The reading that qualifies the row's value, or "" when it has none.

    Three of the four rows have one. The CPU row does not: it used to carry the
    derived clock, and that is gone because the number did not follow load --
    PercentProcessorPerformance reads about 99 % of nominal at 9 % load and at
    100 % alike, since a Ryzen 5 5600X drops voltage rather than frequency in
    proportion to the work it has been given. An empty string here is what the
    CPU row says, and `_draw_text` returns on one, so nothing is drawn and no
    gap is left where a reading used to sit.
    """
    if spec.kind == theme.PCT:
        if spec.key == "gpu_pct":
            # theme.gpu_temp(), not snapshot.gpu_temp_c: the accessor exists so
            # a snapshot without the field degrades to "--" instead of raising
            # AttributeError out of paint().
            temp = theme.gpu_temp(snapshot)
            return "--" if temp is None else f"{temp:.0f}°C"
        return ""
    total = getattr(snapshot, f"{spec.key}_total_gb", None)
    return "--" if total is None else f"/ {total:.1f} GB"
