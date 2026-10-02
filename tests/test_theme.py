import pytest

import theme


@pytest.mark.parametrize(
    ("value", "warn", "critical", "expected"),
    [
        (None, 80.0, 95.0, theme.NORMAL),
        (0.0, 80.0, 95.0, theme.NORMAL),
        (79.9, 80.0, 95.0, theme.NORMAL),
        (80.0, 80.0, 95.0, theme.WARN),
        (94.9, 80.0, 95.0, theme.WARN),
        (95.0, 80.0, 95.0, theme.CRITICAL),
        (100.0, 80.0, 95.0, theme.CRITICAL),
    ],
)
def test_state_boundaries(value, warn, critical, expected):
    assert theme.state_for(value, warn, critical) == expected


def test_unmeasured_never_raises_an_alarm():
    assert theme.state_for(None, 70.0, 80.0) == theme.NORMAL


def test_every_metric_has_ordered_thresholds():
    for spec in theme.METRICS:
        assert spec.warn is not None, spec.key
        assert spec.critical is not None, spec.key
        assert spec.warn < spec.critical, spec.key


def test_metric_keys_are_the_documented_four():
    assert [m.key for m in theme.METRICS] == ["cpu_pct", "ram", "gpu_pct", "vram"]


def test_geometry_heights_add_up_to_280():
    expected = (
        theme.PAD_TOP
        + theme.HEADER_H
        + len(theme.METRICS) * theme.ROW_H
        + (len(theme.METRICS) - 1) * theme.ROW_GAP
        + theme.PAD_BOTTOM
    )
    assert theme.HEIGHT == expected == 280


def test_metric_rects_never_overlap():
    rects = [rect for _, rect in theme.metric_rects()]
    for upper, lower in zip(rects, rects[1:]):
        assert upper.bottom() < lower.top()


def test_metric_rects_fit_inside_the_panel():
    panel = theme.panel_rect()
    for _, rect in theme.metric_rects():
        assert rect.left() >= panel.left()
        assert rect.right() <= panel.right()
        assert rect.bottom() <= panel.bottom()


def test_graph_strip_is_shorter_than_a_row():
    assert theme.GRAPH_H < theme.ROW_H


def test_worst_state_picks_the_most_severe():
    assert theme.worst_state([theme.NORMAL, theme.WARN, theme.CRITICAL]) == theme.CRITICAL
    assert theme.worst_state([]) == theme.NORMAL


def test_state_colors_are_distinct():
    assert len({theme.state_color(s).name() for s in (theme.NORMAL, theme.WARN, theme.CRITICAL)}) == 3
