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
    flat = [0.2, 0.2, 0.2, 0.2, 0.2, 0.2]
    points = [0.0, 0.0, 0.0, 0.4, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    naive = [points[column * len(points) // 4] for column in range(4)]
    assert naive == [0.0, 0.0, 0.0, 0.0]  # uniform sampling loses it
    assert max(p.y() for p in resample(points, 4)) == 1.0
    # A flat series must still come out flat: no invented wobble.
    flat_out = [p.y() for p in resample(flat, 4)]
    assert flat_out == [flat_out[0]] * len(flat_out)


def test_resample_keeps_every_point_when_it_fits():
    points = [0.1, 0.4, 0.9]
    got = resample(points, 10)
    assert [p.y() for p in got] == points


def test_resample_single_point_starts_at_the_left_edge():
    assert resample([0.5], 10) == [QPointF(0.0, 0.5)]


def test_resample_empty_and_zero_width_are_empty():
    assert resample([], 10) == []
    assert resample([0.5], 0) == []


def test_resampled_fills_the_full_width_when_fewer_points():
    h = History()
    h.append(0.2)
    h.append(0.8)
    got = h.resampled(20)
    assert len(got) == 2
    assert got[-1].x() > 0.0


def test_default_history_length_comes_from_theme():
    import theme

    assert len(History()) == 0
    assert History()._values.maxlen == theme.HISTORY_LEN