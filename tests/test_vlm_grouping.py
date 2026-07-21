from worker.engines.vlm_engine import VLMEngine
from worker.shots import Shot, sample_times


def _shots(n):
    return {i: Shot(i, i * 24, (i + 1) * 24, float(i), float(i + 1)) for i in range(n)}


def _flagged(*indices):
    return [(i, {"category": "nudity", "confidence": 0.9, "at": float(i)}) for i in indices]


def test_adjacent_shots_group_into_one_scene():
    groups = VLMEngine._group_into_scenes(_flagged(5, 6, 7), _shots(20), bridge=2)
    assert len(groups) == 1
    assert [i for i, _ in groups[0]] == [5, 6, 7]


def test_small_gap_is_bridged():
    """Partial detection across a scene should still yield one segment."""
    groups = VLMEngine._group_into_scenes(_flagged(125, 127), _shots(200), bridge=2)
    assert len(groups) == 1
    assert [i for i, _ in groups[0]] == [125, 127]


def test_distant_shots_stay_separate():
    groups = VLMEngine._group_into_scenes(_flagged(10, 60), _shots(100), bridge=2)
    assert len(groups) == 2


def test_gap_larger_than_bridge_splits():
    groups = VLMEngine._group_into_scenes(_flagged(10, 14), _shots(100), bridge=2)
    assert len(groups) == 2


def test_min_samples_forces_extra_frames_on_short_shots():
    """One frame of a short shot can catch an unlucky moment."""
    shot = Shot(0, 0, 48, 10.0, 12.0)
    assert len(sample_times(shot, max_gap_s=2.5, min_samples=1)) == 1
    assert len(sample_times(shot, max_gap_s=2.5, min_samples=2)) == 2
