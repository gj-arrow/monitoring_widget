from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QFontMetricsF

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


def test_the_panel_is_the_documented_square_at_every_scale():
    """280, 238 and 210 -- the numbers, not a re-derivation of them.

    The identity this used to check at every scale (pad_top + header_h +
    n*row_h + (n-1)*row_gap + pad_bottom) is `Layout.height`'s own definition,
    so comparing the property against it could not fail whatever the two
    disagreed about; it was a check before the refactor and is a tautology
    after it. What it was standing in for is the thing worth pinning: three
    committed golden PNGs and a README both describe a 280 px square at scale
    1.00, and the tray menu offers exactly these three scales. If a base
    dimension moves, the panel is a different size and the saved art is stale --
    so the numbers are written out here and have to be edited on purpose.

    210 and 238 are the panel, not the window: the widget is those plus the
    bleed on every side, which is why `test_canvas_is_the_panel_plus_a_bleed_margin`
    is a separate assertion rather than folded in here.
    """
    assert [
        (scale, theme.Layout(scale).width, theme.Layout(scale).height)
        for scale in theme.SCALE_STEPS
    ] == [
        (0.75, 210, 210),
        (0.85, 238, 238),
        (1.00, 280, 280),
    ]


def test_the_panel_is_square_because_its_rows_fill_the_height_it_derives():
    """The *why* behind the numbers above, stated so a change cannot be silent.

    Width is a base dimension and height is the sum of the parts, so the two
    agreeing at all three scales is a property of the design rather than a
    coincidence -- and the one place a new row would show up. A fifth metric
    makes every height longer than the number pinned above, which is the
    intended way to notice: the test above fails, and the failure says the
    goldens are the thing that has to be regenerated.
    """
    for scale in theme.SCALE_STEPS:
        layout = theme.Layout(scale)
        summed = (
            layout.pad_top
            + layout.header_h
            + len(theme.METRICS) * layout.row_h
            + (len(theme.METRICS) - 1) * layout.row_gap
            + layout.pad_bottom
        )
        assert layout.height == summed, f"at scale {scale}: {layout.height} != {summed}"
        assert layout.width == layout.height, f"at scale {scale} the panel is not square"


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_the_rows_tile_the_panel_at_every_scale(scale):
    """Every gap is the gap, not merely a gap.

    `upper.bottom() < lower.top()` is satisfied by any spacing at all, so it
    cannot notice a row_gap that stopped being applied -- the rows would drift
    away from the foot of the panel one scale at a time, and nothing about a
    "no overlap" check would say so. Stated exactly: the first row starts one
    header below the top, every gap between rows is row_gap, and the last row
    ends pad_bottom above the foot.
    """
    layout = theme.Layout(scale)
    panel = layout.panel_rect()
    rects = [rect for _, rect in layout.metric_rects()]

    assert len(rects) == len(theme.METRICS)
    for rect in rects:
        assert panel.contains(rect), f"a row at {rect} is outside {panel}"
        assert rect.left() == layout.pad_x
        assert rect.right() == layout.width - layout.pad_x
    for upper, lower in zip(rects, rects[1:]):
        assert lower.top() - upper.bottom() == layout.row_gap
    assert rects[0].top() - panel.top() == layout.pad_top + layout.header_h
    assert panel.bottom() - rects[-1].bottom() == layout.pad_bottom


def test_metric_rects_never_overlap():
    rects = [rect for _, rect in theme.Layout(1.0).metric_rects()]
    for upper, lower in zip(rects, rects[1:]):
        assert upper.bottom() < lower.top()


def test_metric_rects_fit_inside_the_panel():
    panel = theme.Layout(1.0).panel_rect()
    for _, rect in theme.Layout(1.0).metric_rects():
        assert rect.left() >= panel.left()
        assert rect.right() <= panel.right()
        assert rect.bottom() <= panel.bottom()


def test_graph_strip_is_shorter_than_a_row():
    for scale in theme.SCALE_STEPS:
        layout = theme.Layout(scale)
        assert layout.graph_h < layout.row_h, (
            f"at scale {scale} the graph strip is {layout.graph_h} px in a "
            f"{layout.row_h} px row: the row has no band left for its label"
        )


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
    layout = theme.Layout(1.0)
    assert layout.width == 280
    assert layout.panel_rect().width() == 280.0


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_canvas_is_the_panel_plus_a_bleed_margin(scale):
    layout = theme.Layout(scale)
    assert layout.canvas_w == layout.width + 2 * layout.bleed
    assert layout.canvas_h == layout.height + 2 * layout.bleed


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_canvas_is_larger_than_the_panel_on_every_side(scale):
    layout = theme.Layout(scale)
    assert layout.bleed > 0
    assert layout.canvas_w > layout.width
    assert layout.canvas_h > layout.height


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_the_bleed_does_not_move_the_panel(scale):
    """The panel's rect stays at the origin: the canvas is what grew.

    280 x 280 is the panel, not the window, and paint() shifts it by the bleed.
    """
    layout = theme.Layout(scale)
    panel = layout.panel_rect()
    assert (panel.left(), panel.top()) == (0.0, 0.0)
    assert (panel.width(), panel.height()) == (float(layout.width), float(layout.height))


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_row_text_inset_sits_inside_the_padding(scale):
    """Row text geometry belongs in the layout, not beside it in the painter."""
    layout = theme.Layout(scale)
    assert 0 < layout.row_text_inset < layout.pad_x
    assert layout.row_text_inset * 2 < layout.width - 2 * layout.pad_x


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_the_header_dot_fits_to_the_left_of_the_aligned_text(scale):
    """The header text lines up with the row labels, so the dot has to squeeze in.

    Placing the dot first and the text after it left the two left edges 6 px
    apart, with the header starting further right than the CPU/RAM/GPU/VRAM
    labels beneath it. The dot now hangs off the text's left edge by a fixed gap,
    and both edges are stated once in the layout so the renderer and its tests
    cannot disagree about where they are.
    """
    layout = theme.Layout(scale)
    assert layout.header_text_x() == layout.pad_x + layout.row_text_inset
    # The gap is a gap plus the dot's own diameter, so it is the *edges* that
    # are separated by dot_gap, not the centre and the edge. Approximate rather
    # than exact: the dot's radius is a scaled float, so this is 8.499999999999998
    # against 8.5 at 0.85 and the identity is arithmetic, not equality.
    assert layout.header_text_x() - layout.header_dot_x() == pytest.approx(
        layout.dot_gap + 2 * layout.dot_r
    )
    assert layout.header_dot_x() - layout.dot_r > layout.panel_rect().left(), (
        "the dot hangs off the panel itself"
    )
    assert layout.dot_gap > 0


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_the_header_text_edge_is_the_same_edge_the_row_labels_start_at(scale):
    """One alignment rule for every string in the panel, not two."""
    layout = theme.Layout(scale)
    first_label_left = layout.metric_rects()[0][1].left() + layout.row_text_inset
    assert layout.header_text_x() == first_label_left


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
    specs = [spec for spec, _ in theme.Layout(1.0).metric_rects()]
    states = [theme.metric_state(spec, snapshot(
        cpu_pct=1.0, net_down_bytes_per_sec=1e12, net_up_bytes_per_sec=1e12,
    )) for spec in specs]
    assert set(states) == {theme.NORMAL}


# --- staleness -------------------------------------------------------------


def test_a_snapshot_is_fresh_the_instant_it_is_taken():
    assert theme.snapshot_is_stale(snapshot(ts=1000.0), now=1000.0) is False


def test_staleness_starts_past_the_threshold_not_at_it():
    """The boundary is exclusive, and which side it falls on is a choice.

    A reading exactly STALE_AFTER_S old is *not* stale yet: the threshold is
    where the panel stops claiming currency, so it is the first age that counts,
    not the last one that does not. `>` rather than `>=`.
    """
    threshold = theme.STALE_AFTER_S
    assert theme.snapshot_is_stale(snapshot(ts=1000.0), now=1000.0 + threshold) is False
    assert theme.snapshot_is_stale(
        snapshot(ts=1000.0), now=1000.0 + threshold + 0.001
    ) is True


def test_the_staleness_threshold_is_three_ticks():
    # One tick is the interval the collector samples at, so a threshold of one
    # tick would call a panel stale between samples. Three is two missed ticks
    # of slack -- one late tick, or a sample that took as long as a tick --
    # before the dot changes, and 6 s is still well inside "this is broken"
    # rather than "this is a hiccup".
    assert theme.STALE_TICKS == 3
    assert theme.STALE_AFTER_S == theme.STALE_TICKS * theme.TICK_MS / 1000.0 == 6.0


def test_a_snapshot_with_no_timestamp_is_treated_as_fresh():
    # The field defaults to 0.0, so "no ts" and "epoch zero" are the same thing
    # and neither is a staleness claim -- a fresh panel should not go grey
    # because a hand-built snapshot in a test left ts alone.
    assert theme.snapshot_is_stale(snapshot(), now=1000.0) is False
    assert theme.snapshot_is_stale(snapshot(ts=0.0), now=1000.0) is False


# --- the scaled layout -------------------------------------------------------
#
# Every dimension and every font size the panel draws with. Layout derives all
# of them from one number, and a dimension that is not derived from it is a
# dimension that does not shrink -- so this list is walked twice: once to check
# that each name exists (a rename fails here rather than in a golden) and once
# to check that each one actually shrinks.
LAYOUT_DIMENSIONS = (
    "width", "height", "canvas_w", "canvas_h",
    "pad_x", "pad_top", "pad_bottom", "header_h",
    "row_h", "row_gap", "graph_h", "row_text_inset", "bleed",
    "panel_radius", "row_radius", "dot_r", "dot_gap",
    "value_gap", "text_top", "text_clearance", "graph_line_width", "outline_w",
    "label_pt", "value_pt", "aux_pt",
)


def test_the_scales_offered_are_the_three_that_were_asked_for():
    """0.75, 0.85 and 1.00 -- and nothing above 1.00.

    The second assertion is the one that bites. SCALE_STEPS is read by the tray
    menu that builds its rows and by the loader that validates the saved
    setting, so a 1.25 added here would appear in both without anybody having
    decided to offer it. Steps larger than the current size were offered once
    and declined.
    """
    assert theme.SCALE_STEPS == (0.75, 0.85, 1.00)
    assert max(theme.SCALE_STEPS) <= 1.0
    assert theme.DEFAULT_SCALE == 1.00
    assert theme.DEFAULT_SCALE in theme.SCALE_STEPS


def test_the_layout_holds_one_number_and_nothing_else():
    """One stored field, everything else derived from it.

    Two stored fields for two dimensions is two chances to hold a value from a
    different scale; a dataclass with one field cannot be inconsistent at all,
    and equality is then just the scale -- which is what makes two layouts
    comparable without comparing twenty numbers.
    """
    layout = theme.Layout(0.85)
    assert layout.scale == 0.85
    assert layout == theme.Layout(0.85)
    assert layout != theme.Layout(1.0)


def test_the_layout_cannot_be_edited_after_it_is_built():
    """Immutability is what lets a renderer be a pure function of its inputs.

    A frozen dataclass raises rather than assigning, so a panel that handed the
    renderer something it could change underneath itself is not constructible.
    """
    layout = theme.Layout(1.0)
    with pytest.raises(AttributeError):
        layout.scale = 0.75


@pytest.mark.parametrize(
    "scale", [0.0, -0.5, float("nan"), float("inf"), float("-inf")],
    ids=["zero", "negative", "nan", "inf", "-inf"],
)
def test_a_layout_it_cannot_draw_at_all_is_refused(scale):
    """Zero collapses the panel to nothing and NaN poisons every comparison.

    Both would otherwise reach QPainter as a zero-sized or NaN geometry, and the
    failure would appear as a panel that draws nothing rather than as an error
    about the number that caused it.
    """
    with pytest.raises(ValueError):
        theme.Layout(scale)


def test_every_dimension_shrinks_with_the_scale():
    """The one test that catches a dimension somebody forgot to scale.

    A dimension left at its 1.0 value is *equal* at two scales rather than
    smaller, and every "the layout is self-consistent" check still passes: the
    rows still tile the panel, the canvas is still the panel plus its margin, and
    the only thing wrong is that a quarter of the panel is now padding. Compared
    across the three steps rather than to a copied table of expected numbers, so
    it says the property instead of restating the design.
    """
    layouts = [theme.Layout(scale) for scale in theme.SCALE_STEPS]
    for name in LAYOUT_DIMENSIONS:
        values = [getattr(layout, name) for layout in layouts]
        assert values == sorted(set(values)) and len(set(values)) == len(values), (
            f"{name} does not shrink with the scale: {values} at {theme.SCALE_STEPS}"
        )


@pytest.mark.parametrize("scale", theme.SCALE_STEPS)
def test_the_type_shrinks_with_the_panel_and_stays_measured_in_points(scale):
    """Points, not pixels -- the reason the panel follows the monitor's DPI.

    pixelSize() is -1 for a point-sized font and is set the moment anybody
    reaches for setPixelSize, which pins the panel to the pixel grid of whichever
    monitor it was built on. The rasterised advance is checked as well, because a
    point size that Qt declined to apply would leave every other assertion here
    true while the text stayed the size it was.
    """
    layout = theme.Layout(scale)
    for font in (layout.label_font(), layout.value_font(), layout.aux_font()):
        assert font.pixelSize() == -1, "a pixel size would not follow the monitor's DPI"
        assert font.pointSizeF() > 0

    small = QFontMetricsF(theme.Layout(0.75).label_font()).horizontalAdvance("CPU")
    full = QFontMetricsF(theme.Layout(1.0).label_font()).horizontalAdvance("CPU")
    assert small < full, f"'CPU' is {small} px wide at 0.75 and {full} px at 1.0"


def test_the_corner_margin_is_a_gap_to_the_screen_and_does_not_scale():
    """10 px from the screen edge is 10 px, whatever the panel measures.

    The bleed is part of the panel's own artwork and scales with it; this is the
    gap between the panel and something else entirely, and it is the one number
    here that a smaller panel has no reason to change. Stated rather than left
    implicit because "everything scales" is otherwise the natural assumption.
    """
    assert theme.CORNER_MARGIN == 10
