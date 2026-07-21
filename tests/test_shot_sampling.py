import pytest

from worker.shots import Shot, sample_times


def _shot(start, end):
    return Shot(0, int(start * 24), int(end * 24), start, end)


def test_short_shot_gets_one_midpoint_sample():
    assert sample_times(_shot(10.0, 12.0), max_gap_s=2.5) == [11.0]


def test_long_shot_gets_multiple_samples():
    """A 25s take can pan or introduce a character; one frame would miss it."""
    times = sample_times(_shot(0.0, 25.0), max_gap_s=2.5)
    assert len(times) >= 10
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert max(gaps) <= 2.6


def test_samples_stay_inside_the_shot():
    for span in (0.5, 3.0, 25.0, 400.0):
        times = sample_times(_shot(100.0, 100.0 + span), max_gap_s=2.5)
        assert all(100.0 <= t <= 100.0 + span for t in times), span


def test_samples_are_inset_from_boundaries():
    """Frames on a cut are often mid-dissolve or motion-blurred."""
    times = sample_times(_shot(0.0, 10.0), max_gap_s=2.5)
    assert times[0] > 0.0
    assert times[-1] < 10.0


def test_zero_length_shot_is_safe():
    assert sample_times(_shot(5.0, 5.0)) == [5.0]


@pytest.mark.parametrize("span,expected_max", [(2.0, 1), (5.0, 3), (10.0, 5)])
def test_sample_count_scales_with_duration(span, expected_max):
    assert len(sample_times(_shot(0.0, span), max_gap_s=2.5)) <= expected_max
