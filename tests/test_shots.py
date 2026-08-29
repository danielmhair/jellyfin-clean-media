import pytest

from worker.shots import Shot, sample_times


class _TC:
    """A stand-in for a PySceneDetect FrameTimecode carrying just seconds."""

    def __init__(self, s, fps=24.0):
        self._s = s
        self._fps = fps

    def get_seconds(self):
        return self._s

    def get_frames(self):
        return int(self._s * self._fps)


def _install_fake_scenedetect(monkeypatch, scenes):
    """Make detect_shots run against a fixed scene list, no real video decode."""

    class _Mgr:
        def add_detector(self, _d):
            pass

        def detect_scenes(self, _v, show_progress=False):
            pass

        def get_scene_list(self):
            return scenes

    import scenedetect

    monkeypatch.setattr(scenedetect, "SceneManager", _Mgr)
    monkeypatch.setattr(scenedetect, "open_video", lambda _p: object())
    monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold=27.0: None)


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
    """A decode that stops far short (here half the film) on every attempt must
    fail loudly, not be silently stretched over the whole runtime."""
    from worker import retry as retry_mod
    from worker import shots as shots_mod

    monkeypatch.setattr(retry_mod, "BACKOFFS_S", ())  # single attempt, no sleeps
    media = tmp_path / "film.mkv"
    media.touch()
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (24.0, 1000.0, 24000))
    _install_fake_scenedetect(monkeypatch, [(_TC(0.0), _TC(500.0))])  # only half → scale 2.0

    with pytest.raises(RuntimeError, match="covered only"):
        shots_mod.detect_shots(media)


def test_partial_decode_is_retried_then_succeeds(monkeypatch, tmp_path):
    """A dropped share read shows up as a short decode; the next attempt reads
    the whole film. Shot detection must retry rather than fail the job."""
    from worker import retry as retry_mod
    from worker import shots as shots_mod

    monkeypatch.setattr(retry_mod, "BACKOFFS_S", (0.0,))  # retry once, no real wait
    media = tmp_path / "film.mkv"
    media.touch()
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (24.0, 1000.0, 24000))

    # First pass sees only 11% of the film (the Green Lantern failure); the
    # retry sees the whole thing.
    passes = iter([
        [(_TC(0.0), _TC(110.0))],           # partial → PartialTimeline → retry
        [(_TC(0.0), _TC(1000.0))],          # full timeline
    ])

    class _Mgr:
        def add_detector(self, _d):
            pass

        def detect_scenes(self, _v, show_progress=False):
            pass

        def get_scene_list(self):
            return next(passes)

    import scenedetect

    monkeypatch.setattr(scenedetect, "SceneManager", _Mgr)
    monkeypatch.setattr(scenedetect, "open_video", lambda _p: object())
    monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold=27.0: None)

    shots = shots_mod.detect_shots(media)
    assert shots and shots[-1].end_s == pytest.approx(1000.0)


def test_open_failure_is_retried_then_succeeds(monkeypatch, tmp_path):
    """A dropped SMB session at open() time (not mid-decode) raises
    VideoOpenFailure — the same flaky-NAS symptom as a short decode, so it
    must be retried too rather than failing the job on one bad moment."""
    from scenedetect.video_stream import VideoOpenFailure

    from worker import retry as retry_mod
    from worker import shots as shots_mod

    monkeypatch.setattr(retry_mod, "BACKOFFS_S", (0.0,))  # retry once, no real wait
    media = tmp_path / "film.mkv"
    media.touch()
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (24.0, 1000.0, 24000))

    opens = iter([
        VideoOpenFailure("Ensure file is valid video and system dependencies are up to date."),
        object(),
    ])

    def fake_open_video(_p):
        result = next(opens)
        if isinstance(result, Exception):
            raise result
        return result

    class _Mgr:
        def add_detector(self, _d):
            pass

        def detect_scenes(self, _v, show_progress=False):
            pass

        def get_scene_list(self):
            return [(_TC(0.0), _TC(1000.0))]

    import scenedetect

    monkeypatch.setattr(scenedetect, "SceneManager", _Mgr)
    monkeypatch.setattr(scenedetect, "open_video", fake_open_video)
    monkeypatch.setattr(scenedetect, "ContentDetector", lambda threshold=27.0: None)

    shots = shots_mod.detect_shots(media)
    assert shots and shots[-1].end_s == pytest.approx(1000.0)


def test_telecine_undercoverage_is_rescaled_not_rejected(monkeypatch, tmp_path):
    """A telecined rip whose timeline runs a few % short (like Salt at 93% or
    Jumper at 95%) is rescaled onto the true duration and analyzed in full,
    rather than rejected as partial."""
    from worker import shots as shots_mod

    media = tmp_path / "film.mkv"
    media.touch()
    # scenedetect sees the film as 930s; it is really 1000s (the frame-rate lie).
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (23.976, 1000.0, 23976))
    _install_fake_scenedetect(monkeypatch, [
        (_TC(0.0), _TC(465.0)),
        (_TC(465.0), _TC(930.0)),
    ])

    shots = shots_mod.detect_shots(media)

    assert shots, "a 93% telecined timeline must be analyzed, not rejected"
    # The whole film is covered: the last shot maps exactly to the true end.
    assert shots[-1].end_s == pytest.approx(1000.0)
    # Boundaries land proportionally: 465/930 of the film → 500/1000.
    assert shots[0].end_s == pytest.approx(500.0)
    # Nothing spills past EOF.
    assert all(s.end_s <= 1000.0 for s in shots)


def test_overshooting_timeline_is_shrunk_to_the_duration(monkeypatch, tmp_path):
    """If scenedetect overshoots the real duration, shots are scaled down to fit
    rather than sampling past EOF."""
    from worker import shots as shots_mod

    media = tmp_path / "film.mkv"
    media.touch()
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (30.0, 1000.0, 30000))
    _install_fake_scenedetect(monkeypatch, [(_TC(0.0), _TC(1250.0))])  # 1.25x too long

    shots = shots_mod.detect_shots(media)

    assert shots[-1].end_s == pytest.approx(1000.0)
    assert all(0 <= s.end_s <= 1000.0 for s in shots)


def test_no_scenes_falls_back_to_one_full_length_shot(monkeypatch, tmp_path):
    from worker import shots as shots_mod

    media = tmp_path / "film.mkv"
    media.touch()
    monkeypatch.setattr(shots_mod, "true_fps", lambda _p: (24.0, 1000.0, 24000))
    _install_fake_scenedetect(monkeypatch, [])

    shots = shots_mod.detect_shots(media)
    assert len(shots) == 1
    assert (shots[0].start_s, shots[0].end_s) == (0.0, 1000.0)
