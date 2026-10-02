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