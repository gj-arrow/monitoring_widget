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

import pytest
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


def render(snapshot, values_by_key=None, alpha=theme.DEFAULT_ALPHA):
    image = QImage(theme.CANVAS_W, theme.CANVAS_H, QImage.Format.Format_ARGB32_Premultiplied)
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


def test_the_bleed_is_wide_enough_for_the_shadow_painter_draws():
    """The outermost slab in _draw_panel grows 6.0 px out and 7.2 px down."""
    widest, deepest = 6.0, 6.0 * 1.2
    assert theme.BLEED >= widest
    assert theme.BLEED >= deepest


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
    assert len(colours) > 5


def test_single_sample_does_not_break_rendering():
    image = render(CALM, {"cpu_pct": [0.5]})
    assert not image.isNull()


def test_unknown_history_keys_are_ignored():
    histories = build_histories({"not_a_metric": [0.1, 0.2]})
    image = QImage(theme.CANVAS_W, theme.CANVAS_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        paint(painter, CALM, histories, 0.8)
    finally:
        painter.end()
    assert not image.isNull()


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