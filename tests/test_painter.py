"""Renderer tests, half of them golden-image comparisons.

The goldens are near-exact matches: `max_channel_delta` compares the alpha
channel as well as RGB, so anything above a handful of counts is a real
rasterisation difference, not a rounding wobble. The usual cause of such a
difference is a Qt or font change on the machine that last generated them,
which is why the assertion says so. When the change is expected, regenerate
and commit the new PNGs:

    MONITOR_REGEN_GOLDEN=1 python -m pytest tests/test_painter.py

These tests must not run under QT_QPA_PLATFORM=offscreen: conftest strips it,
because under it Qt on Windows has no font database and every glyph would
rasterise as a tofu box, locking an unrenderable picture into the repository.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QTransform

import painter
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


def render(snapshot, values_by_key=None, alpha=theme.DEFAULT_ALPHA, histories=None):
    image = QImage(theme.CANVAS_W, theme.CANVAS_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    # Not named `painter`: that would shadow the module the monkeypatch tests use.
    canvas_painter = QPainter(image)
    try:
        paint(
            canvas_painter,
            snapshot,
            build_histories(values_by_key or {}) if histories is None else histories,
            alpha,
        )
    finally:
        canvas_painter.end()
    return image


def painter_state(p):
    """Everything a caller's paintEvent would mind paint() having changed."""
    pen = p.pen()
    transform = p.transform()
    return {
        "pen": (pen.color().rgba(), pen.widthF(), pen.style(), pen.joinStyle(), pen.capStyle()),
        "brush": p.brush().color().rgba(),
        "brush_style": p.brush().style(),
        "font": p.font().toString(),
        "transform": (
            transform.m11(), transform.m12(), transform.m13(),
            transform.m21(), transform.m22(), transform.m23(),
            transform.m31(), transform.m32(), transform.m33(),
        ),
        "clip": p.clipRegion(),
        "hints": p.renderHints(),
        "composition": p.compositionMode(),
    }


def blank_canvas():
    """The same image with nothing painted on it at all."""
    image = QImage(theme.CANVAS_W, theme.CANVAS_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    return image


def differing_pixels(a, b):
    """Every (x, y) where two renders disagree, on the canvas grid."""
    return {
        (x, y)
        for y in range(a.height())
        for x in range(a.width())
        if a.pixelColor(x, y).rgba() != b.pixelColor(x, y).rgba()
    }


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
    assert delta <= TOLERANCE, (
        f"{name}.png drifted by {delta} (tolerance {TOLERANCE}).\n"
        f"This diff includes the alpha channel, so these are near-exact-match "
        f"images and {delta} counts means the panel really did render "
        f"differently -- not a rounding wobble. The usual cause is a Qt or "
        f"font change on this machine (a different Segoe UI Variable Display "
        f"build, a Qt patch release) altering rasterisation, or a DPI change. "
        f"If that is the expected change, regenerate with "
        f"MONITOR_REGEN_GOLDEN=1 and commit the new PNGs. If you were editing "
        f"theme.py or painter.py, this is the test doing its job."
    )


CALM = Snapshot(
    cpu_pct=34.0, cpu_live_mhz=4460.0, cpu_nominal_mhz=4501.0,
    ram_used_gb=11.4, ram_total_gb=32.0,
    gpu_pct=61.0, gpu_temp_c=58.0,
    vram_used_gb=6.2, vram_total_gb=20.0,
    net_down_bytes_per_sec=7300.0, net_up_bytes_per_sec=3700.0,
    ts=1000.0,
)

# The widest pair the panel can be asked to draw: 999.9 MB/s in both
# directions. It is in the hot panel on purpose, so the golden is also the
# proof that the widest string fits.
HOT = Snapshot(
    cpu_pct=97.0, cpu_live_mhz=4350.0, cpu_nominal_mhz=4501.0,
    ram_used_gb=29.4, ram_total_gb=32.0,
    gpu_pct=97.0, gpu_temp_c=84.0,
    vram_used_gb=18.1, vram_total_gb=20.0,
    net_down_bytes_per_sec=999_900_000.0, net_up_bytes_per_sec=999_900_000.0,
    ts=1000.0,
)

EMPTY = Snapshot(ts=1000.0)

RAMPS = {
    "cpu_pct": [0.30 + 0.08 * ((i * 7) % 5) / 5 for i in range(40)],
    "ram": [0.34, 0.35, 0.36, 0.36, 0.35, 0.34, 0.35, 0.36],
    "gpu_pct": [0.55 + 0.10 * ((i * 3) % 7) / 7 for i in range(40)],
    "vram": [0.30, 0.31, 0.31, 0.30, 0.32],
}


def without_network(snapshot):
    return Snapshot(
        **{name: getattr(snapshot, name)
           for name in ("cpu_pct", "cpu_live_mhz", "cpu_nominal_mhz",
                        "ram_used_gb", "ram_total_gb", "gpu_pct", "gpu_temp_c",
                        "vram_used_gb", "vram_total_gb", "ts")}
    )


def drawn_texts(monkeypatch, snapshot, values_by_key=None):
    """Every (text, colour) paint() hands to _draw_text, with the real draw."""
    seen = []
    real = painter._draw_text

    def spy(canvas_painter, text, rect, font, color, **kwargs):
        seen.append((text, color.rgba()))
        return real(canvas_painter, text, rect, font, color, **kwargs)

    monkeypatch.setattr(painter, "_draw_text", spy)
    render(snapshot, values_by_key)
    return seen


def test_shadow_bleeds_into_the_margin_outside_the_panel():
    """A faint dark halo must survive outside the panel's rounded outline.

    While the canvas was exactly panel_rect() there was nowhere for it to go:
    the shadow slabs were clipped by the image edge and every pixel outside the
    outline was transparent. This samples the left margin, away from the
    rounded corners, and insists something faint and dark landed there.
    """
    image = render(CALM, RAMPS)
    middle = theme.CANVAS_H // 2
    margin = [
        image.pixelColor(x, y)
        # The innermost margin column is skipped on purpose: it is the panel's
        # own antialiased outline, not shadow.
        for x in range(1, theme.BLEED - 1)
        for y in range(middle - 40, middle + 40)
    ]
    # A shadow, not a second copy of the panel: dark, and far fainter than the
    # panel fill behind it.
    faint = [c for c in margin if 0 < c.alpha() < 128
             and max(c.red(), c.green(), c.blue()) < 32]
    assert faint, (
        f"no faint dark pixels in the {theme.BLEED}px left margin: the drop "
        "shadow is still being clipped away by the canvas edge"
    )
    opaque = [c for c in margin if c.alpha() >= 128]
    assert not opaque, (
        f"{len(opaque)} of {len(margin)} margin pixels are panel-opaque: the "
        "panel is drawn at the canvas origin instead of being inset by the bleed"
    )


def test_the_shadow_does_not_reach_the_canvas_edge():
    """The property, not a copy of _draw_panel's slab table.

    A previous version of this test hardcoded the shadow's 6.0 px growth and
    compared it with theme.BLEED, which reads like coverage but is not: raise
    the growth to 10.0 and both numbers still pass while the halo is clipped
    away again. What actually has to hold is that the shadow stops short of the
    canvas edge, so this samples the outermost two pixels along each straight
    edge. Corners are skipped on purpose -- the shadow's own rounded corners
    leave them clear whatever its size.
    """
    image = render(CALM, RAMPS)
    inset = theme.CANVAS_W // 4
    span = range(inset, theme.CANVAS_W - inset)
    for x in span:
        for edge in range(2):
            assert image.pixelColor(x, edge).alpha() == 0, f"shadow reaches the top edge at x={x}"
            assert image.pixelColor(x, theme.CANVAS_H - 1 - edge).alpha() == 0, (
                f"shadow reaches the bottom edge at x={x}"
            )
    for y in range(inset, theme.CANVAS_H - inset):
        for edge in range(2):
            assert image.pixelColor(edge, y).alpha() == 0, f"shadow reaches the left edge at y={y}"
            assert image.pixelColor(theme.CANVAS_W - 1 - edge, y).alpha() == 0, (
                f"shadow reaches the right edge at y={y}"
            )


def test_the_panel_is_inset_by_the_bleed_rather_than_moved():
    """x=0 is margin now, and the panel fill starts one bleed in."""
    image = render(CALM, RAMPS)
    middle = theme.CANVAS_H // 2
    assert image.pixelColor(0, middle).alpha() == 0, (
        "x=0 is still opaque: the panel is drawn at the canvas origin instead "
        "of being inset by the bleed"
    )
    fill = image.pixelColor(theme.BLEED + 5, middle)
    assert fill.alpha() > 200, "panel fill is missing one bleed in from the edge"
    assert max(fill.red(), fill.green(), fill.blue()) < 96


def test_paint_restores_the_painter_it_was_given():
    """paint() must hand the painter back exactly as it found it.

    overlay.py calls paint() from a paintEvent on a painter it reuses every
    tick, so a missing save() or restore() is not a cosmetic problem: the
    translate(8, 8) would accumulate on every tick and walk the panel off-screen
    while the golden images, which each use a fresh painter, stayed green.

    Every mutable piece of painter state is armed with a distinctive value
    first, including a pre-existing clip -- paint() installs a nested one for
    the graph, and that has to come off again too.
    """
    image = blank_canvas()
    caller_painter = QPainter(image)
    try:
        caller_painter.setPen(
            QPen(QColor(12, 200, 90, 210), 3.5, Qt.PenStyle.DashDotLine,
                 Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.BevelJoin)
        )
        caller_painter.setBrush(QColor(200, 30, 30, 180))
        caller_painter.setFont(QFont("Courier New", 19, QFont.Weight.Bold, italic=True))
        caller_painter.setTransform(QTransform().translate(3.0, 4.0).scale(1.5, 0.5))
        caller_painter.setClipRect(QRectF(2.0, 3.0, 400.0, 400.0))
        caller_painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        before = painter_state(caller_painter)

        paint(caller_painter, CALM, build_histories(RAMPS), theme.DEFAULT_ALPHA)

        after = painter_state(caller_painter)
    finally:
        caller_painter.end()

    changed = {key: (before[key], after[key]) for key in before if after[key] != before[key]}
    assert not changed, f"paint() left the caller's painter state changed: {changed}"


def test_a_lost_restore_would_accumulate_the_bleed_translate(monkeypatch):
    """The failure this guards against, shown rather than asserted by proxy.

    overlay.py reuses one painter for every tick. A missing restore() leaves
    the translate on it, so tick two starts 8 px further right than tick one --
    which is why the restoration test above has to compare against state the
    caller set up, not against a default painter.
    """
    monkeypatch.setattr(painter, "_draw_panel", lambda *args, **kwargs: None)
    image = QImage(theme.CANVAS_W, theme.CANVAS_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    caller_painter = QPainter(image)
    try:
        origin = QTransform()
        paint(caller_painter, CALM, {}, 1.0)
        first = caller_painter.transform()
        assert first == origin, "the first tick already drifted; restore() is broken now"
        paint(caller_painter, CALM, {}, 1.0)
        second = caller_painter.transform()
    finally:
        caller_painter.end()
    assert second == origin, (
        "two ticks moved the painter: "
        f"{first.m31()}, {first.m32()} -> {second.m31()}, {second.m32()}"
    )


def test_calm_panel_matches_golden():
    assert_golden("calm", CALM, RAMPS)


def test_hot_panel_matches_golden():
    assert_golden("hot", HOT, RAMPS)


def test_missing_data_matches_golden():
    assert_golden("missing", EMPTY, {})


def test_render_is_never_blank():
    image = render(CALM, RAMPS)
    colours = {image.pixelColor(x, y).rgba() for y in range(0, theme.CANVAS_H, 3)
               for x in range(0, theme.CANVAS_W, 3)}
    assert len(colours) > 40


def test_render_is_never_blank_without_data():
    image = render(EMPTY, {})
    colours = {image.pixelColor(x, y).rgba() for y in range(0, theme.CANVAS_H, 3)
               for x in range(0, theme.CANVAS_W, 3)}
    # Measured 60 on this grid: a panel fill, four row slabs, a header dot and
    # the five "--" labels all contribute their own antialiased values. A blank
    # image yields exactly 1, so the floor below fails it by a wide margin
    # while leaving room for a font change.
    assert len(colours) > 20, f"only {len(colours)} distinct colours: the panel is nearly blank"


def test_each_rows_graph_sits_inside_its_own_slab():
    """A row's history must fill its slab: inside it *and* out to both edges.

    The graph is drawn from resample()'s column indices, which count from zero
    and are not panel coordinates. Used raw they put the fill 14 px left of the
    slab -- over the panel's own padding -- and leave the same width of bare
    slab on the right. Rendered with and without history, the difference is
    exactly the graph, so its extent is read straight off the pixels. Both
    bounds are asserted: an upper bound alone would not notice the graph
    quietly shrinking to a stub in the middle of the row.
    """
    graph_pixels = differing_pixels(render(CALM, RAMPS), render(CALM, {}))
    assert graph_pixels, "no graph was drawn at all, so this test proves nothing"
    for spec, rect in theme.metric_rects():
        slab = rect.translated(theme.BLEED, theme.BLEED)
        in_row = [point for point in graph_pixels if slab.top() <= point[1] <= slab.bottom()]
        assert in_row, f"{spec.key}: no graph drawn"
        left, right = min(x for x, _ in in_row), max(x for x, _ in in_row)
        assert left >= slab.left(), f"{spec.key}: graph starts at x={left}, slab at {slab.left()}"
        assert right <= slab.right(), f"{spec.key}: graph ends at x={right}, slab at {slab.right()}"
        assert left - slab.left() <= 1, (
            f"{spec.key}: graph starts at x={left}, {left - slab.left():.0f} px short of "
            f"the slab's left edge at {slab.left()}"
        )
        assert slab.right() - right <= 1, (
            f"{spec.key}: graph ends at x={right}, {slab.right() - right:.0f} px short of "
            f"the slab's right edge at {slab.right()}"
        )
        strip_top = slab.bottom() - theme.GRAPH_H
        assert min(y for _, y in in_row) >= strip_top, f"{spec.key}: graph climbs above its strip"


def inside_rounded(x, y, rect, radius, slack=1.0):
    """Is (x, y) within a rounded rect grown by `slack` on every side?

    Dilating a rounded rect by a disc gives a rounded rect again: the same
    rectangle grown by `slack`, with the corner radius grown by the same
    `slack`. Both have to move together, or the arcs sit in the wrong place.
    Points in the middle band only have to be inside the rectangle; points near
    a corner have to be inside that corner's arc.
    """
    left, right = rect.left() - slack, rect.right() + slack
    top, bottom = rect.top() - slack, rect.bottom() + slack
    if not (left <= x <= right and top <= y <= bottom):
        return False
    r = radius + slack
    cx = min(max(x, left + r), right - r)
    cy = min(max(y, top + r), bottom - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def test_graph_fill_never_paints_outside_the_rounded_row_slab():
    """Clipped to the slab, so no fill crosses a corner arc.

    Containment is tested against the rounded outline, not the bounding rect:
    the graph is meant to sit flush against the slab's straight edges, and only
    the corner arcs are out of bounds. 1 px of slack absorbs the clip's own
    antialiasing.
    """
    graph_pixels = differing_pixels(render(CALM, RAMPS), render(CALM, {}))
    assert graph_pixels, "no graph was drawn at all, so this test proves nothing"
    slabs = [(spec, rect.translated(theme.BLEED, theme.BLEED)) for spec, rect in theme.metric_rects()]
    attributed = set()
    for spec, slab in slabs:
        # Rows do not overlap vertically, so the band attributes every pixel to
        # exactly one row.
        in_band = [p for p in graph_pixels if slab.top() <= p[1] <= slab.bottom()]
        attributed.update(in_band)
        outside = sorted(p for p in in_band if not inside_rounded(*p, slab, theme.ROW_RADIUS))
        assert not outside, (
            f"{spec.key}: graph painted {len(outside)} px outside its rounded slab, "
            f"nearest at {outside[:3]}"
        )
    assert attributed == graph_pixels, (
        f"{len(graph_pixels - attributed)} graph pixels sit outside every row's "
        f"vertical band, e.g. {sorted(graph_pixels - attributed)[:3]}"
    )


def test_a_snapshot_without_a_gpu_temperature_renders_a_dash():
    """theme.gpu_temp() exists precisely for snapshots that lack the field.

    Reading snapshot.gpu_temp_c directly raises out of paint() instead, even
    though every other reading in the module tolerates the field being absent
    and _draw_header already renders such a snapshot without complaint.

    Two things are asserted, and both matter. The missing field must render
    exactly like an explicit None -- so it is not quietly invented -- and it
    must render *differently* from a snapshot that has a temperature, which is
    what pins the dash itself. Comparing only against None would still pass if
    the auxiliary returned an empty string.
    """
    no_temp = SimpleNamespace(
        cpu_pct=34.0, cpu_live_mhz=4460.0, cpu_nominal_mhz=4501.0,
        ram_used_gb=11.4, ram_total_gb=32.0, gpu_pct=61.0,
        vram_used_gb=6.2, vram_total_gb=20.0,
        net_down_bytes_per_sec=7300.0, net_up_bytes_per_sec=3700.0, ts=1000.0,
    )
    image = render(no_temp, RAMPS)
    explicit_none = Snapshot(
        cpu_pct=34.0, cpu_live_mhz=4460.0, cpu_nominal_mhz=4501.0,
        ram_used_gb=11.4, ram_total_gb=32.0, gpu_pct=61.0, gpu_temp_c=None,
        vram_used_gb=6.2, vram_total_gb=20.0,
        net_down_bytes_per_sec=7300.0, net_up_bytes_per_sec=3700.0, ts=1000.0,
    )
    assert max_channel_delta(image, render(explicit_none, RAMPS)) == 0, (
        "a missing gpu_temp_c rendered differently from an explicit None"
    )
    assert max_channel_delta(image, render(CALM, RAMPS)) > 60, (
        "a missing gpu_temp_c renders the same as a real temperature: the GPU "
        "auxiliary is not showing a dash"
    )


# --- the derived CPU clock -------------------------------------------------


def frequency(live, nominal):
    return SimpleNamespace(cpu_live_mhz=live, cpu_nominal_mhz=nominal)


@pytest.mark.parametrize(
    ("live", "nominal", "expected"),
    [
        # Two decimals, not one: the derived figure only moves in the
        # hundredths of a GHz (99.0% idle, 99.5% loaded on this machine), so
        # 4460 and 4501 both render as "4.5" at one place and the reading
        # looks frozen even though it is alive.
        (4460.0, 4501.0, "4.46 / 4.50 GHz"),
        (4464.0, 4501.0, "4.46 / 4.50 GHz"),
        (4350.0, 4501.0, "4.35 / 4.50 GHz"),
        # A nominal on its own is a reference, not a clock, so it cannot stand
        # in for the derived one.
        (None, 4501.0, "--"),
        (None, None, "--"),
        # With no reference to compare against there is still a live clock.
        (4460.0, None, "4.46 GHz"),
    ],
)
def test_the_frequency_formatter_shows_the_derived_clock_to_two_places(live, nominal, expected):
    assert painter._format_frequency(frequency(live, nominal)) == expected


def test_an_unmeasured_live_clock_renders_a_dash_and_never_the_nominal():
    """The bug this task exists for, asserted on the pixels.

    psutil's `current` on Windows is the nominal clock: 4501.0 on three
    consecutive calls, at idle, under a 12-thread load and after it. A row that
    printed the nominal in the live position therefore showed a number that
    looked measured and was in fact a constant. Two things are checked, and both
    matter: a snapshot carrying only a nominal must render exactly like one
    carrying no clock at all (so the nominal is not being borrowed), and it must
    render *differently* from a snapshot with a derived clock, which is what
    pins the dash itself rather than an empty string.
    """
    nominal_only = Snapshot(**{
        **{name: getattr(CALM, name) for name in ("cpu_pct", "ram_used_gb", "ram_total_gb",
                                                  "gpu_pct", "gpu_temp_c", "vram_used_gb",
                                                  "vram_total_gb", "ts")},
        "cpu_live_mhz": None, "cpu_nominal_mhz": 4501.0,
    })
    no_clock_at_all = Snapshot(**{
        **{name: getattr(CALM, name) for name in ("cpu_pct", "ram_used_gb", "ram_total_gb",
                                                  "gpu_pct", "gpu_temp_c", "vram_used_gb",
                                                  "vram_total_gb", "ts")},
    })
    assert max_channel_delta(render(nominal_only, RAMPS),
                             render(no_clock_at_all, RAMPS)) == 0, (
        "a snapshot with a nominal clock but no derived one rendered "
        "differently from one with no clock at all: the nominal is being "
        "printed as though it were live"
    )
    assert max_channel_delta(render(nominal_only, RAMPS), render(CALM, RAMPS)) > 60, (
        "an unmeasured live clock renders the same as a measured one: the CPU "
        "auxiliary is not showing a dash"
    )


def test_the_cpu_row_shows_the_derived_clock_beside_the_percentage(monkeypatch):
    """Both halves have to be on screen, or the derivation is invisible."""
    texts = [text for text, _ in drawn_texts(monkeypatch, CALM, RAMPS)]
    assert "34%" in texts
    assert "4.46 / 4.50 GHz" in texts


# --- the header ------------------------------------------------------------


def test_the_header_no_longer_shows_the_system_label_or_the_sample_age(monkeypatch):
    """Both were asked for as the space the throughput now occupies."""
    texts = [text for text, _ in drawn_texts(monkeypatch, CALM, RAMPS)]
    assert "SYSTEM" not in texts, "the header still draws the SYSTEM label"
    stale = [t for t in texts if t.endswith("ago")]
    assert not stale, f"the header still draws the sample age: {stale}"
    # The two direction labels live in one drawText call, so neither is a whole
    # string of its own: this says the check above is looking at real output.
    assert "DN 7.3 KB/s  UP 3.7 KB/s" in texts


def test_the_header_shows_both_directions_of_throughput(monkeypatch):
    texts = [text for text, _ in drawn_texts(monkeypatch, CALM, RAMPS)]
    (header,) = [t for t in texts if "DN" in t or "UP" in t]
    assert "DN 7.3 KB/s" in header
    assert "UP 3.7 KB/s" in header
    # Download before upload, so the header and the CSV agree on which is
    # which without a legend: the same order NET_KEYS declares.
    assert header.index("DN 7.3") < header.index("UP 3.7")
    # And it reaches the pixels rather than merely being formatted.
    assert max_channel_delta(render(CALM, RAMPS),
                             render(without_network(CALM), RAMPS)) > 60


def test_an_unmeasured_rate_renders_a_dash_in_both_directions(monkeypatch):
    texts = [text for text, _ in drawn_texts(monkeypatch, EMPTY, {})]
    (header,) = [t for t in texts if "DN" in t or "UP" in t]
    # Not "0 B/s": the first tick has no predecessor, which is not the same
    # claim as a link that moved nothing.
    assert header.count("--") == 2, header


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "--"),
        (0.0, "0 B/s"),
        (7.4, "7 B/s"),
        (512.6, "513 B/s"),
        (999.4, "999 B/s"),
        # The unit switches on the unrounded magnitude, so the half-unit below a
        # boundary rounds up into the next unit's own number. 999.6 bytes are
        # 1000 bytes, not a kilobyte: re-scaling it would be filing the value
        # under a unit ten times too coarse for the width it was just given.
        (999.6, "1000 B/s"),
        (7300.0, "7.3 KB/s"),
        (999_949.0, "999.9 KB/s"),
        (999_999.0, "1000.0 KB/s"),
        (1_000_000.0, "1.0 MB/s"),
        (1048576.0, "1.0 MB/s"),
        (1_250_000_000.0, "1250.0 MB/s"),
        (999_900_000.0, "999.9 MB/s"),
        # The top of the ladder. The unit list stops at MB/s, so a value whose
        # megabyte figure would read 1000.0 or more has no unit left to be
        # stated in -- 1e12 rendered a 302-character string and overran the
        # header by more than its whole width. "At least" is the honest
        # monotone statement about a number known to be enormous but with no
        # bound to quote against.
        (999_949_999.0, "999.9 MB/s"),
        # The top of the ladder, and the same documented artefact as the
        # half-unit boundaries below: the unit switches on the unrounded
        # magnitude, so 999.96 MB/s prints as "1000.0 MB/s". The saturating arm
        # starts at exactly one terabyte, where the figure would otherwise be a
        # megabyte count large enough to overrun the header.
        (999_960_000.0, "1000.0 MB/s"),
        (999_999_999_999.0, "1000000.0 MB/s"),
        (1e12, ">= 1000.0 TB/s"),
        (1e300, ">= 1000.0 TB/s"),
        # The arm is on the magnitude with the sign carried into the string, so
        # each side gets a statement true of it. Saturating a negative as
        # ">= 1000.0 TB/s" would be false of it; testing the signed value alone
        # would look more honest and would not be, since it bounds only the
        # positive side and hands -1e300 back to the megabyte branch as that same
        # 302-character string.
        (-1e300, "<= -1000.0 TB/s"),
        (-1e12, "<= -1000.0 TB/s"),
    ],
)
def test_the_rate_formatter_picks_the_unit_from_the_magnitude(value, expected):
    # Decimal units, unlike the binary GB the memory rows use. Either way the
    # unit switches where the figure resets to 1.0 -- 999.9 KB/s then 1.0 MB/s
    # -- which is what every network tool on the machine does, and it keeps
    # "1024.0 KB/s" from ever being printed. Whole bytes below a kilobyte: a
    # two-second delta that small moves in ones, so a decimal place claims a
    # resolution the counter does not have.
    assert painter._format_rate(value) == expected


def test_the_widest_rate_string_the_header_must_fit_is_bounded():
    """Measured, because it decides the header's font.

    Not the saturated arm: the ladder's *own* ceiling is wider. A value just
    under the terabyte renders as `1000000.0 MB/s`, so that pair -- not
    `>= 1000.0 TB/s` -- is the longest string _format_net can produce. Both are
    measured, and both are pinned in the parametrised test above, because if the
    bound ever moved the two could swap and the header would have to change
    rather than the bound.
    """
    def width(value):
        widest = painter._format_net(SimpleNamespace(net_down_bytes_per_sec=value,
                                                    net_up_bytes_per_sec=value))
        return QFontMetricsF(theme.aux_font()).horizontalAdvance(widest), widest

    available = theme.header_rect().right() - theme.header_text_x()
    for value, expected in ((999_999_999_999.0, "DN 1000000.0 MB/s  UP 1000000.0 MB/s"),
                            (1e300, "DN >= 1000.0 TB/s  UP >= 1000.0 TB/s"),
                            (-1e300, "DN <= -1000.0 TB/s  UP <= -1000.0 TB/s")):
        metrics_width, widest = width(value)
        assert widest == expected
        assert metrics_width <= available, (
            f"{widest!r} is {metrics_width:.0f} px against {available:.0f} px of header"
        )


@pytest.mark.parametrize(
    "rate", [999_900_000.0, 1_250_000_000.0, 999_999_999_999.0, 1e300],
    ids=["999.9 MB/s", "1250.0 MB/s", "1000000.0 MB/s", "saturated"],
)
def test_the_longest_header_string_stays_clear_of_the_dot_and_inside_the_header(monkeypatch, rate):
    """Measured, not asserted from a metrics calculation.

    No glyph coordinates are hardcoded and no font is assumed. The control is a
    render with *no* header text at all -- not one with different text, whose
    leading "DN " would have rasterised identically and hidden every pixel of
    the string's left edge. So the differing pixels are the whole throughput
    string and are measured wherever it rasterised. Qt clips nothing here, so a
    string that overran the panel would show up outside it.

    Both figures are wider than the usual reading: 999.9 MB/s is the example in
    the brief, and 1250.0 MB/s is what a saturated 10 GbE link reports, which is
    the widest pair a machine of this class can actually produce.
    """
    widest = Snapshot(**{
        **{name: getattr(CALM, name) for name in ("cpu_pct", "cpu_live_mhz",
             "cpu_nominal_mhz", "ram_used_gb", "ram_total_gb", "gpu_pct",
             "gpu_temp_c", "vram_used_gb", "vram_total_gb", "ts")},
        "net_down_bytes_per_sec": rate,
        "net_up_bytes_per_sec": rate,
    })
    with_text = render(widest, RAMPS)
    monkeypatch.setattr(painter, "_format_net", lambda snapshot: "")
    without_text = render(CALM, RAMPS)
    monkeypatch.undo()

    pixels = differing_pixels(with_text, without_text)
    assert pixels, "the throughput reached no pixels, so this test proves nothing"

    header = theme.header_rect()
    dot_right = theme.header_dot_x() + theme.DOT_R
    xs = []
    for canvas_x, canvas_y in sorted(pixels):
        x, y = canvas_x - theme.BLEED, canvas_y - theme.BLEED
        xs.append(x)
        assert header.top() <= y <= header.bottom(), (
            f"the throughput was drawn at y={y}, outside the header band "
            f"{header.top()}..{header.bottom()}"
        )
        assert x > dot_right, (
            f"the throughput starts at x={x}, on top of the status dot which "
            f"ends at {dot_right}"
        )
        assert x <= header.right(), (
            f"the throughput reaches x={x}, past the header's right edge at "
            f"{header.right()} and the panel's padding"
        )
    # Both bounds: it starts on the edge every other string in the panel starts
    # on, so the dot keeps its companion and the header and the row labels stay
    # on one alignment -- and it does not start so far right that the alignment
    # is lost altogether. A glyph's leading is 1-2 px past the pen position.
    start = min(xs)
    assert theme.header_text_x() - 2 <= start <= theme.header_text_x() + 2, (
        f"the throughput starts at x={start}, not on the aligned text edge at "
        f"{theme.header_text_x()}: the header and the row labels no longer line up"
    )


def test_throughput_is_never_coloured_by_magnitude(monkeypatch):
    """Network speed is not a health metric, so it gets no alarm colour.

    The alarm channel is spent on rows that are actually wrong. Painting a
    999.9 MB/s download in the critical red would say the machine is in
    trouble when it is doing exactly what it was asked to do.
    """
    def net_colour(snapshot):
        for text, rgba in drawn_texts(monkeypatch, snapshot, RAMPS):
            if "DN" in text:
                return rgba
        raise AssertionError("the throughput was never drawn")

    quiet = net_colour(CALM)                                   # 7.3 KB/s
    loud = net_colour(HOT)                                     # 999.9 MB/s, and hot rows
    alarms = {theme.state_color(state).rgba()
              for state in (theme.WARN, theme.CRITICAL, theme.NORMAL)}
    assert quiet not in alarms, (
        "the throughput is painted in a state colour, which spends the alarm "
        "channel on a metric that is not wrong"
    )
    assert quiet == loud, (
        "the throughput changed colour with its magnitude: a quiet link and a "
        "saturated one are not a health difference"
    )


def test_the_status_dot_survives_the_header_change_and_still_reports_the_worst_state():
    """The header lost its label and its age; the dot is the alarm and stays.

    Rendered at alpha 1.0 so the panel fill behind the dot is opaque, which is
    what lets the pixel read back as the pure state colour instead of a blend
    with a translucent fill.
    """
    x = int(theme.BLEED + theme.header_dot_x())
    y = int(theme.BLEED + theme.header_rect().center().y())
    calm = render(CALM, RAMPS, alpha=1.0).pixelColor(x, y)
    hot = render(HOT, RAMPS, alpha=1.0).pixelColor(x, y)
    empty = render(EMPTY, {}, alpha=1.0).pixelColor(x, y)

    assert calm.name() == theme.CALM.name(), (
        f"the dot is {calm.name()} on a calm panel, not {theme.CALM.name()}"
    )
    assert hot.name() == theme.CRITICAL_COLOR.name(), (
        f"the dot is {hot.name()} on a hot panel, not "
        f"{theme.CRITICAL_COLOR.name()}: the dot no longer reports the worst state"
    )
    # Nothing measured at all is the neutral dot, not a calm blue one: a panel
    # with no readings has nothing to be calm about.
    assert empty.name() == theme.NEUTRAL.name()


def test_throughput_alone_does_not_light_the_status_dot(monkeypatch):
    net_only = Snapshot(net_down_bytes_per_sec=999_900_000.0,
                        net_up_bytes_per_sec=999_900_000.0, ts=1000.0)
    texts = [text for text, _ in drawn_texts(monkeypatch, net_only, {})]
    (header,) = [t for t in texts if "DN" in t or "UP" in t]
    assert "999.9 MB/s" in header
    # has_any_data() chooses between the neutral dot and a state colour, so
    # counting the throughput here would paint a calm blue dot on a panel whose
    # every row reads "--": a healthy-looking alarm light over nothing.
    assert theme.has_any_data(net_only) is False
    x = int(theme.BLEED + theme.header_dot_x())
    y = int(theme.BLEED + theme.header_rect().center().y())
    assert render(net_only, {}, alpha=1.0).pixelColor(x, y).name() == theme.NEUTRAL.name()


def test_single_sample_does_not_break_rendering():
    """One sample is not a trend: it must draw exactly what no history draws.

    Both halves matter. The equality pins the behaviour, and the diff against a
    blank canvas is what stops the test passing vacuously -- `not image.isNull()`
    was true whatever paint() did, because the test itself allocated the image.
    """
    image = render(CALM, {"cpu_pct": [0.5]})
    assert max_channel_delta(image, render(CALM, {})) == 0, (
        "a single-sample history drew a graph; it should draw none"
    )
    assert max_channel_delta(image, blank_canvas()) > 60, (
        "the render is blank: paint() produced nothing to compare"
    )


def test_unknown_history_keys_are_ignored():
    """A history the panel does not draw must not reach the pixels.

    `not image.isNull()` passed even with paint() stubbed out, so this asserts
    that the render is identical to one built without the stray key, and that
    it is not blank in the first place.
    """
    histories = build_histories({**RAMPS, "not_a_metric": [0.1, 0.2]})
    image = render(CALM, histories=histories)
    assert max_channel_delta(image, render(CALM, RAMPS)) == 0, (
        "an unknown history key changed the render"
    )
    assert max_channel_delta(image, blank_canvas()) > 60, (
        "the render is blank: paint() produced nothing to compare"
    )


def test_alpha_zero_still_draws_the_text(monkeypatch):
    """At alpha 0 the panel background vanishes but the glyphs must remain.

    The obvious version of this test -- diff an alpha=0 render against an
    alpha=1.0 one -- is vacuous: the two backgrounds differ by ~200 alpha
    counts, so it passes even if every drawText call were deleted. Instead the
    same render is compared against one where text drawing is stubbed out, so
    the only possible source of difference is the glyphs themselves.

    No glyph coordinates are hardcoded. That would need a font-aware probe to
    survive a font change; this measures whatever actually rasterised.
    """
    clear = render(CALM, RAMPS, alpha=0.0)
    monkeypatch.setattr(painter, "_draw_text", lambda *args, **kwargs: None)
    untexted = render(CALM, RAMPS, alpha=0.0)

    cpu_rect = next(rect for spec, rect in theme.metric_rects() if spec.key == "cpu_pct")
    differing = in_cpu_row = 0
    for y in range(theme.CANVAS_H):
        for x in range(theme.CANVAS_W):
            if clear.pixelColor(x, y).rgba() == untexted.pixelColor(x, y).rgba():
                continue
            differing += 1
            if cpu_rect.contains(QPointF(x - theme.BLEED, y - theme.BLEED)):
                in_cpu_row += 1

    assert differing > 300, (
        f"only {differing} pixels changed when text drawing was stubbed out: "
        "the alpha=0 render carries no glyphs"
    )
    assert in_cpu_row > 30, (
        f"{differing} pixels differ but only {in_cpu_row} inside the CPU row: "
        "row values are not being drawn, only the header"
    )
    assert max_channel_delta(clear, untexted) > 60
