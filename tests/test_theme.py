from types import SimpleNamespace

import pytest

import theme


def snapshot(**fields):
    """Stand-in for metrics.Snapshot, which a later task will define."""
    return SimpleNamespace(**fields)


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


def test_state_color_hands_out_a_copy_not_the_shared_token():
    assert theme.state_color(theme.NORMAL) is not theme.CALM
    assert theme.state_color(theme.WARN) is not theme.WARN_COLOR
    assert theme.state_color(theme.CRITICAL) is not theme.CRITICAL_COLOR


def test_mutating_a_returned_colour_leaves_the_tokens_alone():
    before = [theme.CALM.alpha(), theme.WARN_COLOR.alpha(), theme.CRITICAL_COLOR.alpha()]
    for state in (theme.NORMAL, theme.WARN, theme.CRITICAL):
        theme.state_color(state).setAlpha(1)
    assert [theme.CALM.alpha(), theme.WARN_COLOR.alpha(), theme.CRITICAL_COLOR.alpha()] == before


def test_gpu_temperature_limits_are_70_and_80():
    assert (theme.GPU_TEMP_WARN, theme.GPU_TEMP_CRITICAL) == (70.0, 80.0)


@pytest.mark.parametrize(
    ("temp", "expected"),
    [
        (None, theme.NORMAL),
        (0.0, theme.NORMAL),
        (69.9, theme.NORMAL),
        (70.0, theme.WARN),
        (79.9, theme.WARN),
        (80.0, theme.CRITICAL),
        (120.0, theme.CRITICAL),
    ],
)
def test_gpu_temperature_boundaries(temp, expected):
    assert theme.state_for(temp, theme.GPU_TEMP_WARN, theme.GPU_TEMP_CRITICAL) == expected


def test_panel_width_is_pinned_to_280():
    assert theme.WIDTH == 280
    assert theme.panel_rect().width() == 280.0


def test_canvas_is_the_panel_plus_a_bleed_margin():
    assert theme.CANVAS_W == theme.WIDTH + 2 * theme.BLEED
    assert theme.CANVAS_H == theme.HEIGHT + 2 * theme.BLEED


def test_canvas_is_larger_than_the_panel_on_every_side():
    assert theme.BLEED > 0
    assert theme.CANVAS_W > theme.WIDTH
    assert theme.CANVAS_H > theme.HEIGHT


def test_the_bleed_does_not_move_the_panel():
    """280 x 280 is the panel, not the window: its rect stays at the origin."""
    panel = theme.panel_rect()
    assert (panel.left(), panel.top()) == (0.0, 0.0)
    assert (panel.width(), panel.height()) == (theme.WIDTH, theme.HEIGHT)


def test_row_text_inset_is_a_theme_constant_and_sits_inside_the_padding():
    """Row text geometry belongs in theme, not beside it in the painter."""
    assert 0 < theme.ROW_TEXT_INSET < theme.PAD_X
    assert theme.ROW_TEXT_INSET + theme.ROW_TEXT_INSET < theme.WIDTH - 2 * theme.PAD_X


def test_the_header_dot_fits_to_the_left_of_the_aligned_text():
    """The header text lines up with the row labels, so the dot has to squeeze in.

    Placing the dot first and the text after it left the two left edges 6 px
    apart, with the header starting further right than the CPU/RAM/GPU/VRAM
    labels beneath it. The dot now hangs off the text's left edge by a fixed gap,
    and both edges are stated once in theme so the renderer and its tests cannot
    disagree about where they are.
    """
    assert theme.header_text_x() == theme.PAD_X + theme.ROW_TEXT_INSET
    # The gap is a gap plus the dot's own diameter, so it is the *edges* that
    # are separated by DOT_GAP, not the centre and the edge.
    assert theme.header_text_x() - theme.header_dot_x() == theme.DOT_GAP + 2 * theme.DOT_R
    assert theme.header_dot_x() - theme.DOT_R > theme.panel_rect().left(), (
        "the dot hangs off the panel itself"
    )
    assert theme.DOT_GAP > 0


def test_the_header_text_edge_is_the_same_edge_the_row_labels_start_at():
    """One alignment rule for every string in the panel, not two."""
    first_label_left = theme.metric_rects()[0][1].left() + theme.ROW_TEXT_INSET
    assert theme.header_text_x() == first_label_left


def test_row_value_reads_a_pct_row():
    spec = theme.METRICS_BY_KEY["cpu_pct"]
    assert theme.row_value(spec, snapshot(cpu_pct=42.0)) == 42.0
    assert theme.row_value(spec, snapshot(gpu_pct=42.0)) is None


def test_row_value_computes_a_gb_row():
    spec = theme.METRICS_BY_KEY["ram"]
    assert theme.row_value(spec, snapshot(ram_used_gb=6.0, ram_total_gb=16.0)) == 37.5


def test_row_value_is_none_when_the_gb_row_cannot_be_computed():
    spec = theme.METRICS_BY_KEY["ram"]
    assert theme.row_value(spec, snapshot()) is None
    assert theme.row_value(spec, snapshot(ram_used_gb=6.0)) is None
    assert theme.row_value(spec, snapshot(ram_total_gb=16.0)) is None
    assert theme.row_value(spec, snapshot(ram_used_gb=6.0, ram_total_gb=0.0)) is None


def test_row_fraction_clamps_at_zero_and_one():
    spec = theme.METRICS_BY_KEY["cpu_pct"]
    assert theme.row_fraction(spec, snapshot(cpu_pct=-20.0)) == 0.0
    assert theme.row_fraction(spec, snapshot(cpu_pct=0.0)) == 0.0
    assert theme.row_fraction(spec, snapshot(cpu_pct=100.0)) == 1.0
    assert theme.row_fraction(spec, snapshot(cpu_pct=140.0)) == 1.0
    assert theme.row_fraction(spec, snapshot()) is None


def test_metric_state_takes_the_worse_of_load_and_temperature():
    spec = theme.METRICS_BY_KEY["gpu_pct"]
    assert theme.metric_state(spec, snapshot(gpu_pct=50.0, gpu_temp_c=75.0)) == theme.WARN
    assert theme.metric_state(spec, snapshot(gpu_pct=50.0, gpu_temp_c=20.0)) == theme.NORMAL
    assert theme.metric_state(spec, snapshot(gpu_pct=96.0, gpu_temp_c=20.0)) == theme.CRITICAL
    assert theme.metric_state(spec, snapshot(gpu_pct=50.0, gpu_temp_c=85.0)) == theme.CRITICAL


def test_metric_state_survives_a_snapshot_without_a_temperature():
    spec = theme.METRICS_BY_KEY["gpu_pct"]
    assert theme.metric_state(spec, snapshot(gpu_pct=50.0)) == theme.NORMAL


def test_has_any_data_is_false_for_an_entirely_empty_snapshot():
    assert theme.has_any_data(snapshot()) is False


def test_has_any_data_is_true_for_a_temperature_alone():
    assert theme.has_any_data(snapshot(gpu_temp_c=45.0)) is True


def test_throughput_alone_is_not_data_for_the_status_dot():
    # The dot is the alarm, and it is lit from has_any_data(). A link moving
    # 1 GB/s is not the machine in trouble, so counting throughput would paint a
    # calm blue dot on a panel whose every row reads "--" -- an alarm light
    # with nothing behind it.
    assert theme.has_any_data(snapshot(
        net_down_bytes_per_sec=999_900_000.0, net_up_bytes_per_sec=999_900_000.0,
    )) is False


def test_throughput_never_reaches_a_row_state_or_the_worst_state():
    # Same reason, one level down: the rows colour themselves from their own
    # metrics, and there is no path by which a network rate could escalate one.
    specs = [spec for spec, _ in theme.metric_rects()]
    states = [theme.metric_state(spec, snapshot(
        cpu_pct=1.0, net_down_bytes_per_sec=1e12, net_up_bytes_per_sec=1e12,
    )) for spec in specs]
    assert set(states) == {theme.NORMAL}
