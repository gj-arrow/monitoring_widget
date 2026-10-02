from PyQt6.QtCore import QPointF

from history import History, resample


def test_history_respects_maxlen():
    h = History(maxlen=3)
    for value in (0.1, 0.2, 0.3, 0.4):
        h.append(value)
    assert h.values() == [0.2, 0.3, 0.4]


def test_history_ignores_missing_samples():
    h = History()
    h.append(None)
    h.append(0.5)
    h.append(None)
    assert h.values() == [0.5]


def test_history_clamps_out_of_range_input():
    h = History()
    h.append(-5.0)
    h.append(5.0)
    assert h.values() == [0.0, 1.0]


def test_history_ignores_nan_rather_than_recording_it_as_zero():
    # NaN is "not measured", the same as None -- it must not become a sample.
    # Clamping alone cannot do this: `min(1.0, max(0.0, nan))` is 0.0,
    # because `max` drops the NaN comparison, so an unguarded `append` files
    # a failed reading as a real 0% data point and the graph draws it as one.
    h = History()
    h.append(float("nan"))
    h.append(0.5)
    h.append(float("nan"))
    assert h.values() == [0.5]


def test_history_still_clamps_infinity():
    # inf is an over-range reading, not a missing one, so clamping it to 1.0
    # is the honest answer and must survive the NaN guard.
    h = History()
    h.append(float("inf"))
    h.append(float("-inf"))
    assert h.values() == [1.0, 0.0]


def test_resample_keeps_a_one_sample_spike():
    # The spike has to share a column window with values close to the
    # previously drawn point, otherwise every window is single-valued and
    # uniform sampling returns the same answer -- the test would pass
    # without the tie-break rule doing anything.
    points = [0.0, 0.0, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    got = resample(points, 4)
    assert max(p.y() for p in got) == 1.0


def test_resample_beats_uniform_sampling_at_preserving_peaks():
    points = [0.0, 0.0, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    naive = [points[column * len(points) // 4] for column in range(4)]
    assert naive == [0.0, 0.0, 0.0, 0.0]  # uniform sampling loses it
    assert max(p.y() for p in resample(points, 4)) == 1.0


def test_resample_only_ever_draws_measured_values_and_keeps_the_spike():
    # Nearly flat but not constant. A constant series would prove nothing:
    # averaging, min, max and nearest-to-previous all return that same
    # constant, so the assertion passes whatever the rule is. These ten
    # samples land in windows of two whose averages (0.2025, 0.577, 0.1995)
    # were never measured, so a rule that averages or interpolates emits a
    # y no sample ever produced, and a rule that shrinks to the quiet value
    # throws away the 0.951 spike. Both naive rules fail; selection from
    # the window cannot do either.
    points = [
        0.201, 0.204, 0.198, 0.207, 0.951,
        0.203, 0.196, 0.209, 0.194, 0.205,
    ]
    ys = [p.y() for p in resample(points, 5)]
    assert len(ys) == 5
    assert set(ys) <= set(points)
    assert max(ys) == 0.951


def test_resample_tie_break_is_measured_from_the_previous_column():
    # The rule the docstring spends a paragraph on is "every column after the
    # first takes the value furthest from the point drawn just before it".
    # Deleting that key -- falling back to plain `max(window)` everywhere --
    # leaves every other test in this file green, so this is the only thing
    # standing between that refactor and a silent behaviour change.
    #
    # This input discriminates. At width 2 the windows are [1.0, 0.0] then
    # [0.8, 0.95]. Column 0 has no predecessor, so it falls back to plain
    # max(window) and yields 1.0. Column 1 then measures distance from that
    # 1.0: 0.8 is 0.2 away, 0.95 only 0.05, so the tie-break picks 0.8 --
    # the larger step, not the larger value. Plain max picks 0.95 instead,
    # so the two rules disagree on the same input and this test bites.
    points = [1.0, 0.0, 0.8, 0.95]
    ys = [p.y() for p in resample(points, 2)]
    assert ys == [1.0, 0.8]


def test_resample_first_column_uses_plain_max_with_no_predecessor():
    # The documented exception to the rule above, pinned so it cannot be
    # "tidied up" into the tie-break by accident. Column 0 has nothing to
    # measure a step from, so it takes the column's peak -- the only
    # spike-preserving choice left. [0.0, 1.0] must give 1.0, not 0.0.
    assert [p.y() for p in resample([0.0, 1.0], 1)] == [1.0]
    assert [p.y() for p in resample([1.0, 0.0, 0.8, 0.95], 2)][0] == 1.0


def test_resample_keeps_every_point_when_it_fits():
    points = [0.1, 0.4, 0.9]
    got = resample(points, 10)
    assert [p.y() for p in got] == points


def test_resample_single_point_starts_at_the_left_edge():
    assert resample([0.5], 10) == [QPointF(0.0, 0.5)]


def test_resample_empty_and_zero_width_are_empty():
    assert resample([], 10) == []
    assert resample([0.5], 0) == []


def test_resample_negative_width_is_empty():
    assert resample([0.5], -3) == []
    assert resample([0.5, 0.9], -1) == []


def test_resample_single_column_keeps_the_extreme():
    # One column is still one extreme, not just the first sample.
    assert resample([0.1, 0.9, 0.5], 1) == [QPointF(0.0, 0.9)]
    assert resample([0.9], 1) == [QPointF(0.0, 0.9)]


def test_resample_exactly_as_wide_as_the_data_is_one_pixel_per_point():
    points = [0.1, 0.4, 0.9]
    assert resample(points, 3) == [
        QPointF(0.0, 0.1),
        QPointF(1.0, 0.4),
        QPointF(2.0, 0.9),
    ]


def test_resampled_ends_at_the_right_edge_when_fewer_points():
    h = History()
    h.append(0.2)
    h.append(0.8)
    got = h.resampled(20)
    assert got == [QPointF(0.0, 0.2), QPointF(19.0, 0.8)]


def test_default_history_length_comes_from_theme():
    import theme

    h = History()
    for step in range(theme.HISTORY_LEN + 1):
        h.append(step / theme.HISTORY_LEN)
    assert len(h) == theme.HISTORY_LEN
