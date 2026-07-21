import json

from worker.engines.pureframe_engine import PureFrameEngine

SAMPLE_PLAN = {
    "pureframe_version": "0.1.0b7",
    "plan_version": 1,
    "shots": [
        {"index": 0, "start_frame": 0, "end_frame": 100, "start_time": 0.0, "end_time": 4.17},
        {"index": 1, "start_frame": 101, "end_frame": 300, "start_time": 4.17, "end_time": 12.5},
        {"index": 2, "start_frame": 301, "end_frame": 500, "start_time": 12.5, "end_time": 20.8},
    ],
    "verdicts": [
        {"shot_index": 0, "action": "NONE", "category": "NONE", "confidence": 0.1},
        {
            "shot_index": 1,
            "action": "FULL_FRAME_BLUR",
            "category": "NUDITY",
            "confidence": 0.92,
            "reasoning": "nudenet: FEMALE_BREAST_EXPOSED 0.92",
        },
        {
            "shot_index": 2,
            "action": "BLACK_BOX",
            "category": "KISSING",
            "confidence": 0.71,
            "reasoning": "clip: intense kissing",
        },
    ],
}


def test_plan_to_timeline(tmp_path):
    plan_path = tmp_path / "movie.censorplan.json"
    plan_path.write_text(json.dumps(SAMPLE_PLAN))

    timeline = PureFrameEngine().plan_to_timeline(plan_path, "quickhash:abc")

    assert timeline.schemaVersion == 1
    assert timeline.mediaFingerprint == "quickhash:abc"
    # NONE verdicts are dropped
    assert len(timeline.segments) == 2

    nudity, kissing = timeline.segments
    assert nudity.category == "nudity"
    assert nudity.startMs == 4170
    assert nudity.endMs == 12500
    assert nudity.confidence == 0.92
    assert nudity.engine == "pureframe"
    assert nudity.approved is None
    assert kissing.category == "intense_kissing"
    assert kissing.engineRef == "2"
