from worker.shots import Shot, sample_times


def test_shot_times_must_stay_inside_the_film():
    """Regression: telecined sources made later shots exceed the duration.

    PySceneDetect counts frames at the container rate while ffmpeg decodes
    at a lower one, so frame/decode_fps overshoots. Every sample past EOF
    silently produced no frame, dropping a fifth of a film from analysis.
    """
    duration = 100.0
    # what the bug produced: a shot starting after the film ends
    bogus = Shot(0, 3000, 3100, 125.0, 129.0)
    assert bogus.start_s > duration, "fixture should model the bug"

    # sampling such a shot yields timestamps no frame grab can satisfy
    assert all(t > duration for t in sample_times(bogus))


def test_sampling_a_valid_shot_stays_in_range():
    duration = 100.0
    shot = Shot(0, 0, 240, 10.0, 20.0)
    assert all(0 <= t <= duration for t in sample_times(shot))


def test_coverage_guard_rejects_a_partial_timeline(monkeypatch, tmp_path):
    """A shot list that stops early must fail loudly, not analyze 60% quietly."""
    import pytest

    from worker import shots as shots_mod

    media = tmp_path / "film.mkv"
    media.touch()
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (24.0, 1000.0, 24000))

    class _TC:
        def __init__(self, s):
            self._s = s

        def get_seconds(self):
            return self._s

        def get_frames(self):
            return int(self._s * 24)

    class _Mgr:
        def add_detector(self, _d):
            pass

        def detect_scenes(self, _v, show_progress=False):
            pass

        def get_scene_list(self):
            return [(_TC(0.0), _TC(500.0))]  # only half the film

    # detect_shots imports these lazily from scenedetect, so patch there
    import scenedetect

    monkeypatch.setattr(scenedetect, "SceneManager", _Mgr)
    monkeypatch.setattr(scenedetect, "open_video", lambda _p: object())
    monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold=27.0: None)

    with pytest.raises(RuntimeError, match="covered only"):
        shots_mod.detect_shots(media)
