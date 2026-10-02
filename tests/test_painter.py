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

from PyQt6.QtCore import QPointF
from PyQt6.QtGui import QColor, QImage, QPainter

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
    painter = QPainter(image)
    try:
        # now is pinned: the header shows the sample age, and a wall clock would
        # change the pixels on every run.
        paint(
            painter,
            snapshot,
            build_histories(values_by_key or {}) if histories is None else histories,
            alpha,
            now=snapshot.ts + 2.0,
        )
    finally:
        painter.end()
    return image


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
    # Measured 78 on this grid: a panel fill, four row slabs, a header dot and
    # the five "--" labels all contribute their own antialiased values. A blank
    # image yields exactly 1, so the floor below fails it by a wide margin
    # while leaving room for a font change.
    assert len(colours) > 20, f"only {len(colours)} distinct colours: the panel is nearly blank"


def test_each_rows_graph_sits_inside_its_own_slab():
    """A row's history belongs to that row, horizontally as well as vertically.

    The graph is drawn from resample()'s column indices, which count from zero
    and are not panel coordinates. Used raw they put the fill 14 px left of the
    slab -- over the panel's own padding -- and leave the same width of bare
    slab on the right. Rendered with and without history, the difference is
    exactly the graph, so its extent is read straight off the pixels.
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
    """
    no_temp = SimpleNamespace(
        cpu_pct=34.0, cpu_mhz=4500.0, cpu_max_mhz=4500.0,
        ram_used_gb=11.4, ram_total_gb=32.0, gpu_pct=61.0,
        vram_used_gb=6.2, vram_total_gb=20.0, ts=1000.0,
    )
    image = render(no_temp, RAMPS)
    explicit_none = Snapshot(
        cpu_pct=34.0, cpu_mhz=4500.0, cpu_max_mhz=4500.0,
        ram_used_gb=11.4, ram_total_gb=32.0, gpu_pct=61.0, gpu_temp_c=None,
        vram_used_gb=6.2, vram_total_gb=20.0, ts=1000.0,
    )
    assert max_channel_delta(image, render(explicit_none, RAMPS)) == 0, (
        "a missing gpu_temp_c rendered differently from an explicit None"
    )


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
