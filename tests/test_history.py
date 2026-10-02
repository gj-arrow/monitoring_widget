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
