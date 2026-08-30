"""The loop back from a rendered clean copy to the film it was made from.

Watching the clean copy is how a missed word actually gets found, so the copy
has to lead back to its film: a moment flagged in the copy belongs to the film
(at the matching moment in it, which is not the same number once a render has
cut footage out), reviewing the copy reviews the film, and re-rendering reads
the film rather than encoding an encode.

Without that, every flag made while watching the clean version is written to a
sidecar the next render throws away.
"""

import json

import pytest

from worker.cleancopy import (
    cuts_of,
    is_clean_copy,
    render_source,
    source_of,
    to_source_ms,
    write_origin_record,
)
from worker.review import create_segment, load_timeline, review_target, sidecar_for


def _film_with_clean_copy(tmp_path, cuts=None):
    """A film in its own folder, with a rendered "Clean" version beside it."""
    folder = tmp_path / "Some Film (2010)"
    folder.mkdir()
    film = folder / "Some Film (2010).mkv"
    film.write_bytes(b"original")
    clean = folder / "Some Film (2010) - Clean.mkv"
    clean.write_bytes(b"clean")
    if cuts is not None:
        write_origin_record(clean, film, cuts)
    return film, clean


# -- recognising a copy, and finding its film ---------------------------------


def test_a_render_records_the_film_and_the_cuts_it_applied(tmp_path):
    film, clean = _film_with_clean_copy(tmp_path, cuts=[(600_000, 720_000)])
    assert is_clean_copy(clean) and not is_clean_copy(film)
    assert source_of(clean) == film
    assert cuts_of(clean) == [(600_000, 720_000)]
    # The record sits beside the copy, not in the film's sidecar.
    assert json.loads(
        (clean.parent / "Some Film (2010) - Clean.cleanmedia-origin.json")
        .read_text(encoding="utf-8")
    )["source"] == str(film)


def test_a_copy_rendered_before_origin_records_still_leads_back_to_its_film(tmp_path):
    # No record on disk: the naming rule alone has to get there, or every flag
    # on an existing clean copy is stranded.
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    assert source_of(clean) == film
    assert cuts_of(clean) == []


def test_a_copy_whose_film_is_gone_is_left_alone(tmp_path):
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    film.unlink()
    assert source_of(clean) is None
    assert render_source(clean) == clean  # nothing better to offer


def test_an_ordinary_film_is_not_a_copy_of_anything(tmp_path):
    film, _ = _film_with_clean_copy(tmp_path, cuts=None)
    assert source_of(film) is None
    assert render_source(film) == film


# -- placing a moment from the copy in the film -------------------------------


@pytest.mark.parametrize(
    "clean_ms, expected",
    [
        (300_000, 300_000),      # before the cut: unchanged
        (600_000, 720_000),      # exactly at it: the whole cut is added back
        (1_800_000, 1_920_000),  # after it: shifted by the cut's length
    ],
)
def test_a_moment_in_the_copy_maps_to_the_same_moment_in_the_film(clean_ms, expected):
    assert to_source_ms(clean_ms, [(600_000, 720_000)]) == expected


def test_several_cuts_all_count_toward_the_shift():
    cuts = [(60_000, 90_000), (600_000, 720_000)]      # 30s, then 2m
    assert to_source_ms(30_000, cuts) == 30_000        # before both
    assert to_source_ms(120_000, cuts) == 150_000      # past the first only
    assert to_source_ms(1_000_000, cuts) == 1_150_000  # past both


# -- where a flag lands -------------------------------------------------------


def test_a_flag_on_a_clean_copy_is_written_against_the_film(tmp_path):
    # The bug this exists to stop: a render rebuilds the copy from the film's
    # approvals, so a finding recorded on the copy is thrown away by the next
    # render — silently, with nothing to show the reviewer it happened.
    film, clean = _film_with_clean_copy(tmp_path, cuts=[(600_000, 720_000)])

    segment = create_segment(
        clean, 1_800_000, 1_803_000, "manual", "skip",
        reasoning="Flagged from remote at 30:00",
    )

    assert segment is not None
    assert not sidecar_for(clean).exists()          # nothing stranded on the copy
    timeline = load_timeline(film)
    assert timeline is not None and len(timeline.segments) == 1
    landed = timeline.segments[0]
    # Two minutes were cut before this moment, so it is two minutes later in
    # the film than the copy's clock said.
    assert (landed.startMs, landed.endMs) == (1_920_000, 1_923_000)
    assert landed.approved is True
    # And the reviewer can see where it came from.
    assert "Some Film (2010) - Clean.mkv" in (landed.reasoning or "")


def test_a_flag_on_a_copy_with_no_cuts_keeps_its_timing(tmp_path):
    # A mute or a blur moves nothing, so the copy's clock is the film's clock.
    film, clean = _film_with_clean_copy(tmp_path, cuts=[])

    create_segment(clean, 100_000, 103_000, "manual", "mute")

    timeline = load_timeline(film)
    assert timeline is not None
    assert (timeline.segments[0].startMs, timeline.segments[0].endMs) == (100_000, 103_000)


def test_a_flag_on_an_ordinary_film_is_untouched(tmp_path):
    film, _ = _film_with_clean_copy(tmp_path, cuts=[(600_000, 720_000)])

    create_segment(film, 1_800_000, 1_803_000, "manual", "skip", reasoning="by hand")

    timeline = load_timeline(film)
    assert timeline is not None
    landed = timeline.segments[0]
    assert (landed.startMs, landed.endMs) == (1_800_000, 1_803_000)
    assert landed.reasoning == "by hand"


# -- reviewing, and re-rendering ----------------------------------------------


def test_reviewing_a_clean_copy_reviews_the_film(tmp_path):
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    assert review_target(clean) == film   # the copy holds no decisions of its own
    assert review_target(film) == film


def test_reviewing_a_clean_copy_with_its_own_findings_reviews_the_copy(tmp_path):
    """Legacy data: a flag made on a clean copy before create_segment started
    redirecting to the source is stranded there on purpose (see
    _redirect_to_source's docstring — guess-migrating it risks a wrong
    timestamp with no origin record to map through). review_target must not
    bounce a reviewer away from findings that genuinely live on the copy."""
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    sidecar_for(clean).write_text(
        json.dumps({
            "mediaFingerprint": "fp",
            "segments": [{
                "id": 1, "startMs": 1000, "endMs": 2000, "category": "manual",
                "confidence": 1.0, "engine": "manual", "recommendedAction": "skip",
                "approved": True, "reasoning": "Flagged from remote at 0:01",
            }],
        }),
        encoding="utf-8",
    )
    assert review_target(clean) == clean
    assert review_target(film) == film


def test_the_grid_lists_clean_copies_alongside_their_film(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    from worker.review import library_view, warm_media_index

    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    for media in (film, clean):
        sidecar_for(media).write_text(
            json.dumps({"mediaFingerprint": "fp", "segments": []}), encoding="utf-8"
        )
    warm_media_index()

    names = [it["name"] for it in library_view("some film")["items"]]
    # A copy is reachable from the grid like any other video — this is how a
    # reviewer gets back to one that holds findings of its own (legacy flags
    # made before create_segment started redirecting to the source), see
    # test_reviewing_a_clean_copy_with_its_own_findings_reviews_the_copy below.
    assert names == ["Some Film (2010)", "Some Film (2010) - Clean"]


# -- what a re-render reads, and what it writes --------------------------------


def _approved_sidecar(media):
    sidecar_for(media).write_text(
        json.dumps({
            "mediaFingerprint": "fp",
            "segments": [{
                "id": 1, "startMs": 1000, "endMs": 2000, "category": "profanity",
                "confidence": 1.0, "engine": "subtitles", "recommendedAction": "mute",
                "approved": True,
            }],
        }),
        encoding="utf-8",
    )


def _queue(tmp_path):
    from worker.queue import JobQueue
    from worker.store import Store

    # allowed_fn False keeps the worker thread from actually running the job:
    # this is about what submit decides, not about invoking ffmpeg.
    return JobQueue(Store(db_path=tmp_path / "jobs.db"),
                    allowed_fn=lambda now: False, poll_s=0.01)


def test_re_rendering_from_the_clean_copy_reads_the_film_and_overwrites_the_copy(tmp_path):
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    _approved_sidecar(film)

    job = _queue(tmp_path).submit_media_render(str(clean), mode="replace")

    # Reads the film — a copy is never encoded from another copy ...
    assert job.mediaPath == str(film)
    # ... and "overwrite" means the copy that was being watched.
    assert job.options["renderOutputPath"] == str(clean)


def test_keeping_the_existing_copy_writes_the_next_version_instead(tmp_path):
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    _approved_sidecar(film)

    job = _queue(tmp_path).submit_media_render(str(clean), mode="new")

    assert job.mediaPath == str(film)
    assert job.options["renderOutputPath"] == str(
        clean.with_name("Some Film (2010) - Clean 2.mkv")
    )
    assert clean.read_bytes() == b"clean"   # untouched


def test_overwriting_a_numbered_copy_means_that_copy(tmp_path):
    # Asking to re-render "Clean 2" and choosing overwrite must not quietly
    # rewrite "Clean" instead.
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    _approved_sidecar(film)
    second = clean.with_name("Some Film (2010) - Clean 2.mkv")
    second.write_bytes(b"clean two")

    job = _queue(tmp_path).submit_media_render(str(second), mode="replace")

    assert job.mediaPath == str(film)
    assert job.options["renderOutputPath"] == str(second)


# -- when the cuts are not on record ------------------------------------------
# Every copy rendered before origin records existed has to be placed some other
# way. Guessing from today's approvals alone is not safe — approvals change
# after a render, and each changed one moves the answer — so the two files'
# durations are measured and the guess is only used when it accounts for the
# footage actually missing.


@pytest.fixture
def probe(monkeypatch):
    """Stand in for ffprobe: hand each path a duration, in seconds."""
    lengths = {}

    def fake_duration(path):
        return lengths[str(path)]

    monkeypatch.setattr("worker.shots.media_duration", fake_duration)
    return lengths


def _skip_sidecar(media, spans, approved=True):
    sidecar_for(media).write_text(
        json.dumps({
            "mediaFingerprint": "fp",
            "segments": [{
                "id": i + 1, "startMs": a, "endMs": b, "category": "suggestive",
                "confidence": 1.0, "engine": "vlm", "recommendedAction": "skip",
                "approved": approved,
            } for i, (a, b) in enumerate(spans)],
        }),
        encoding="utf-8",
    )


def test_a_copy_the_same_length_as_the_film_keeps_its_timing(tmp_path, probe):
    # Mute-only: nothing was cut, so the copy's clock is the film's clock — even
    # though the sidecar happens to carry an approved skip that was never used.
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    _skip_sidecar(film, [(600_000, 720_000)])
    probe[str(film)] = probe[str(clean)] = 7200.0

    create_segment(clean, 1_800_000, 1_803_000, "manual", "mute")

    landed = load_timeline(film).segments[-1]
    assert (landed.startMs, landed.endMs) == (1_800_000, 1_803_000)
    assert landed.approved is True


def test_approvals_that_account_for_the_missing_footage_are_trusted(tmp_path, probe):
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    _skip_sidecar(film, [(600_000, 720_000)])          # two minutes approved
    probe[str(film)] = 7200.0
    probe[str(clean)] = 7080.0                          # ... and two minutes shorter

    create_segment(clean, 1_800_000, 1_803_000, "manual", "skip")

    landed = load_timeline(film).segments[-1]
    assert (landed.startMs, landed.endMs) == (1_920_000, 1_923_000)
    assert landed.approved is True                      # the numbers agree


def test_a_flag_whose_timing_cannot_be_established_arrives_undecided(tmp_path, probe):
    # The copy is short by five minutes, but the sidecar only accounts for two:
    # it was rendered from a different set of approvals. Placing the flag by
    # those approvals would land it three minutes off — so it goes on the film
    # (the only place it survives a render) but nothing acts on it until a human
    # has looked.
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    _skip_sidecar(film, [(600_000, 720_000)])
    probe[str(film)] = 7200.0
    probe[str(clean)] = 6900.0

    create_segment(clean, 1_800_000, 1_803_000, "manual", "skip")

    landed = load_timeline(film).segments[-1]
    assert landed.approved is None
    assert "timing unverified" in (landed.reasoning or "")


def test_an_unprobeable_copy_is_not_silently_trusted(tmp_path, probe):
    film, clean = _film_with_clean_copy(tmp_path, cuts=None)
    probe[str(film)] = 7200.0  # the copy's probe raises (KeyError) — share down

    create_segment(clean, 1_800_000, 1_803_000, "manual", "skip")

    assert load_timeline(film).segments[-1].approved is None


def test_a_recorded_copy_is_trusted_without_probing_anything(tmp_path):
    # The record is the exact answer, so no ffprobe fixture is needed here: if
    # the code reached for one it would hit the real binary and this would hang
    # or fail, which is the point.
    film, clean = _film_with_clean_copy(tmp_path, cuts=[(600_000, 720_000)])

    create_segment(clean, 1_800_000, 1_803_000, "manual", "skip")

    landed = load_timeline(film).segments[-1]
    assert (landed.startMs, landed.endMs) == (1_920_000, 1_923_000)
    assert landed.approved is True


def test_overlapping_cuts_are_counted_once(tmp_path):
    # A render removes the union of two overlapping skips, not their sum.
    # Adding both lengths back would push every later moment a minute too far.
    from worker.cleancopy import merge_spans

    film, clean = _film_with_clean_copy(
        tmp_path, cuts=[(600_000, 720_000), (660_000, 780_000)]
    )
    assert cuts_of(clean) == [(600_000, 780_000)]        # 3 minutes, not 4
    assert merge_spans([(0, 10), (10, 20), (40, 50)]) == [(0, 20), (40, 50)]
    assert to_source_ms(1_800_000, cuts_of(clean)) == 1_980_000
