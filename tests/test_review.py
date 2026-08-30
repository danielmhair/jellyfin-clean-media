import json

from worker.models import Segment, Timeline
from worker.review import (
    clip_path,
    load_timeline,
    render_page,
    set_approval,
    set_approvals,
    sidecar_for,
    update_segment,
)


def _film(tmp_path):
    media = tmp_path / "Film.mkv"
    media.touch()
    timeline = Timeline(
        mediaFingerprint="fp",
        segments=[
            Segment(id=1, startMs=1000, endMs=2000, category="nudity",
                    confidence=0.9, engine="vlm", recommendedAction="skip"),
            Segment(id=2, startMs=5000, endMs=5800, category="profanity",
                    confidence=1.0, engine="subtitles", recommendedAction="mute"),
        ],
    )
    sidecar_for(media).write_text(json.dumps(timeline.model_dump()), encoding="utf-8")
    return media, timeline


def test_approval_persists_to_sidecar(tmp_path):
    media, _ = _film(tmp_path)

    assert set_approval(media, 1, True)
    assert load_timeline(media).segments[0].approved is True

    # rejecting is distinct from undecided — it must survive a reload
    assert set_approval(media, 2, False)
    reloaded = load_timeline(media)
    assert reloaded.segments[1].approved is False
    assert reloaded.segments[0].approved is True


def test_approval_can_be_cleared(tmp_path):
    media, _ = _film(tmp_path)
    set_approval(media, 1, True)
    set_approval(media, 1, None)
    assert load_timeline(media).segments[0].approved is None


def test_unknown_segment_is_rejected(tmp_path):
    media, _ = _film(tmp_path)
    assert not set_approval(media, 99, True)


def test_bulk_approval_sets_many_in_one_write(tmp_path):
    media, _ = _film(tmp_path)

    changed = set_approvals(media, [1, 2], True)

    assert changed == 2
    assert all(s.approved is True for s in load_timeline(media).segments)


def test_bulk_approval_ignores_unknown_ids(tmp_path):
    """A finding deleted in another tab must not fail the whole action."""
    media, _ = _film(tmp_path)

    changed = set_approvals(media, [1, 99], False)

    assert changed == 1
    reloaded = load_timeline(media)
    assert reloaded.segments[0].approved is False
    assert reloaded.segments[1].approved is None


def test_bulk_approval_can_clear_decisions(tmp_path):
    media, _ = _film(tmp_path)
    set_approvals(media, [1, 2], True)

    set_approvals(media, [1, 2], None)

    assert all(s.approved is None for s in load_timeline(media).segments)


def test_missing_analysis_returns_none(tmp_path):
    media = tmp_path / "Nothing.mkv"
    media.touch()
    assert load_timeline(media) is None


def test_page_embeds_segments_and_escapes_path(tmp_path):
    media, timeline = _film(tmp_path)
    html = render_page(media, timeline)
    assert "nudity" in html and "profanity" in html
    # The media path is embedded as a JS string literal (the MEDIA const the
    # page uses for every API call); backslash-heavy Windows paths must survive.
    assert json.dumps(str(media)) in html
    # Every finding's data is embedded for the client-side Studio model.
    assert '"category": "nudity"' in html


def test_page_is_the_studio_workspace(tmp_path):
    """The Studio page leads with discreet mode and the cut/leave language."""
    media, timeline = _film(tmp_path)
    html = render_page(media, timeline)
    assert "Discreet mode" in html  # picture blurred by default (a parent reviews)
    assert "Hold to reveal" in html  # the escape hatch
    assert "Cut it out" in html and "Leave it in" in html  # decision language
    assert "blurred · discreet" in html  # the corner badge (picture blurred, not hidden)


def test_page_has_minimap_editor_and_merge(tmp_path):
    """The full-film minimap, progress bar, editor and merge affordance ship."""
    media, timeline = _film(tmp_path)
    html = render_page(media, timeline)
    assert "D-ftrack" in html and "D-fbox" in html  # minimap track + viewport box
    assert "D-progbar" in html and "cut out" in html  # triage progress
    assert "D-edcard" in html  # the zoomable editor
    assert "Merge" in html and "D-mergego" in html
    # Persistence goes through the by-path segment endpoints.
    assert "/api/segments" in html and "method:'PATCH'" in html.replace(" ", "")


def test_editing_category_persists_and_leaves_other_fields_alone(tmp_path):
    """Story: correct a mis-categorised detection in review, not just its action.

    The Studio page sends `{category: ...}` on the same patch endpoint the
    timing/reasoning edits use; only the field actually sent may change.
    """
    media, _ = _film(tmp_path)

    updated = update_segment(media, 1, category="gore")

    assert updated.category == "gore"
    reloaded = load_timeline(media).segments[0]
    assert reloaded.category == "gore"
    # Re-categorising is neither a decision nor a retime.
    assert reloaded.approved is None
    assert (reloaded.startMs, reloaded.endMs) == (1000, 2000)
    assert reloaded.recommendedAction == "skip"


def test_muted_clip_has_its_own_cache_path(tmp_path):
    """Muted and plain clips of the same span must not collide in the cache."""
    media = tmp_path / "Film.mkv"
    plain = clip_path(media, 1000, 2000, 15.0, mute=False)
    muted = clip_path(media, 1000, 2000, 15.0, mute=True)
    assert plain != muted


def test_build_peaks_window_and_localization(tmp_path):
    """The waveform spans the finding ±pad, at 40 peaks/s, and the loud burst
    lands at the finding — so a reviewer can see the word to drag onto it."""
    import wave

    import numpy as np

    from worker.review import build_peaks

    sr = 8000
    wav = tmp_path / "tone.wav"
    buf = np.zeros(sr * 4, dtype=np.int16)
    buf[sr : sr + int(0.3 * sr)] = (15000 * np.sin(
        2 * np.pi * 900 * np.arange(int(0.3 * sr)) / sr)).astype(np.int16)  # burst at 1.0s
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(buf.tobytes())

    r = build_peaks(wav, 1000, 1300, pad_s=0.5)
    assert (r["winStartMs"], r["winEndMs"], r["perSec"]) == (500, 1800, 40)
    assert abs(len(r["peaks"]) - round(1.3 * 40)) <= 1  # 40 peaks/s over the window
    loud = max(range(len(r["peaks"])), key=lambda i: r["peaks"][i])
    loud_ms = r["winStartMs"] + loud * (1000 // r["perSec"])
    assert 900 <= loud_ms <= 1350  # burst localizes at the finding (1000–1300ms)


def test_build_preview_clip_cuts_skips_and_compresses(tmp_path):
    """A cleaned window preview removes the cut spans (so their footage is never
    transcoded) — the clip comes out shorter than the window by the cut length."""
    import subprocess

    from worker.review import build_preview_clip

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=24:duration=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)],
        check=True,
    )
    # window [5s,25s] = 20s; cut [10s,20s] = 10s removed → ~10s cleaned clip
    out = build_preview_clip(media, 5000, 25000, [(10000, 20000)], [(7000, 8000)])
    assert out is not None and out.is_file()
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True,
    ).stdout.strip())
    assert 8.0 < dur < 12.0  # 20s window minus the 10s cut


def test_stream_command_normal_is_a_straight_transcode(tmp_path):
    """Normal/Muted whole-film stream: from the start point to the end, no cuts,
    fragmented MP4 on stdout so a <video> plays it as it arrives."""
    from worker.review import stream_command

    cmd = stream_command(tmp_path / "f.mkv", 5000, 605000, [], [])
    assert "-filter_complex" not in cmd  # nothing to cut → cheapest path
    assert cmd[cmd.index("-ss") + 1] == "5.000"
    assert cmd[cmd.index("-t") + 1] == "600.000"  # start→end of a 605s film
    assert cmd[-2:] == ["-f", "mp4"] or cmd[-1] == "pipe:1"
    assert "frag_keyframe+empty_moov+default_base_moof" in cmd  # streamable, no faststart


def test_stream_command_cleaned_streams_a_playable_compressed_mp4(tmp_path):
    """The invariant, not the exit code: run the cleaned whole-film stream for
    real and confirm the piped bytes are a valid MP4 whose duration is the window
    minus the cut — the skipped footage never reaches the browser."""
    import subprocess

    from worker.review import stream_command

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=24:duration=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)],
        check=True,
    )
    # stream from 4s to the 30s end (26s), cutting 10s–20s → ~16s of playable film
    cmd = stream_command(media, 4000, 30000, [(10000, 20000)], [(6000, 7000)])
    out = tmp_path / "streamed.mp4"
    with out.open("wb") as fh:
        subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL, check=True)
    assert out.stat().st_size > 0
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True,
    ).stdout.strip())
    assert 14.0 < dur < 18.0  # 26s remaining window minus the 10s cut


def _library_film(root, collection, name, segments=None):
    """A film under root/collection, optionally with a sidecar of segments."""
    coll = root / collection
    coll.mkdir(exist_ok=True)
    media = coll / name
    media.write_bytes(b"video bytes")
    if segments is not None:
        sidecar_for(media).write_text(
            json.dumps({"mediaFingerprint": "fp", "segments": segments}), encoding="utf-8"
        )
    return media


def test_library_view_worklist_search_and_status(tmp_path, monkeypatch):
    """The switcher's data: default = analyzed films, needs-review first then
    reviewed; searching also finds unanalyzed films (which open for manual
    review). Status is derived from the undecided count."""
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    from worker.review import library_view, warm_media_index

    def seg(i, approved):
        return {"id": i, "startMs": i, "endMs": i + 1, "category": "profanity",
                "confidence": 1.0, "engine": "e", "recommendedAction": "mute",
                "approved": approved}

    _library_film(tmp_path, "Action", "Bang (2020).mkv", [seg(1, None), seg(2, None)])  # ready: 2 undecided
    _library_film(tmp_path, "Action", "Boom (2019).mkv", [seg(1, True)])                # reviewed
    _library_film(tmp_path, "Action", "Buzz (2021).mkv", [seg(1, True), seg(2, None)])  # in_progress
    _library_film(tmp_path, "Drama", "Quiet Film (2021).mkv")                            # unanalyzed (no sidecar)
    warm_media_index()

    # Default work-list: analyzed only, ordered ready -> in_progress -> reviewed.
    work = library_view()["items"]
    assert [it["status"] for it in work] == ["ready", "in_progress", "reviewed"]
    assert "Quiet Film (2021)" not in [it["name"] for it in work]  # untouched is search-only
    assert work[0]["undecidedCount"] == 2 and work[0]["collection"] == "Action"

    # Search finds the unanalyzed film (it opens for manual review).
    hits = library_view("quiet")["items"]
    assert [it["name"] for it in hits] == ["Quiet Film (2021)"]
    assert hits[0]["status"] == "unanalyzed"


def test_library_view_survives_one_corrupt_sidecar(tmp_path, monkeypatch):
    """A malformed sidecar (hand-edited, or a write cut short) must not 500 the
    whole switcher — every other film in the library stays listed and usable,
    and the broken one is flagged rather than silently dropped."""
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    from worker.review import library_view, warm_media_index

    def seg(i, approved):
        return {"id": i, "startMs": i, "endMs": i + 1, "category": "profanity",
                "confidence": 1.0, "engine": "e", "recommendedAction": "mute",
                "approved": approved}

    _library_film(tmp_path, "Action", "Bang (2020).mkv", [seg(1, None)])  # ready
    broken = _library_film(tmp_path, "Action", "Broken (2021).mkv", [seg(1, None)])
    sidecar_for(broken).write_text(
        sidecar_for(broken).read_text(encoding="utf-8") + "}", encoding="utf-8"
    )  # trailing garbage, same shape as a truncated/hand-edited write
    warm_media_index()

    work = library_view()["items"]
    assert [it["name"] for it in work] == ["Bang (2020)", "Broken (2021)"]
    assert work[0]["status"] == "ready"
    assert work[1]["status"] == "corrupt"


def test_library_view_summary_refreshes_after_a_decision(tmp_path, monkeypatch):
    """Deciding a finding must move a film out of 'ready' — the cached summary is
    invalidated on the sidecar write, so the switcher isn't stale."""
    monkeypatch.setenv("CLEANMEDIA_MEDIA_ROOTS", str(tmp_path))
    from worker.review import library_view, set_approval, warm_media_index

    def seg(i, approved):
        return {"id": i, "startMs": i, "endMs": i + 1, "category": "profanity",
                "confidence": 1.0, "engine": "e", "recommendedAction": "mute",
                "approved": approved}

    media = _library_film(tmp_path, "X", "One (2020).mkv", [seg(1, None)])
    warm_media_index()
    assert library_view()["items"][0]["status"] == "ready"

    set_approval(media, 1, True)  # decide it -> sidecar rewritten -> summary invalidated
    assert library_view()["items"][0]["status"] == "reviewed"


def test_build_preview_clip_blurs_a_blur_finding(tmp_path):
    """Cleaned preview applies the render's full-frame gblur to blur spans, so a
    reviewer sees the blur where the clean copy will have one."""
    import subprocess

    from worker.review import build_preview_clip

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=24:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)],
        check=True,
    )
    # a window with no cuts/mutes but one blur span → a full-length, playable clip
    out = build_preview_clip(media, 5000, 15000, [], [], [(8000, 11000)])
    assert out is not None and out.is_file()
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)],
        capture_output=True, text=True,
    ).stdout.strip())
    assert 9.0 < dur < 11.0  # blur doesn't cut — the whole 10s window survives


def _span_rms(clip, t0, t1):
    """RMS of a clip's audio across [t0, t1] seconds, via a raw-PCM extract."""
    import subprocess

    import numpy as np

    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-i", str(clip),
         "-t", f"{t1 - t0:.3f}", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
        capture_output=True,
    ).stdout
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def test_build_preview_clip_voice_removes_vocals_not_all_audio(tmp_path, monkeypatch):
    """Regression: the review page hard-muted voice-only findings ('muting
    everything'). A voice span must go through Demucs vocal removal, not a
    volume=0 mute — so non-vocal audio survives. With a fake separator that
    reports NO vocals, the tone plays through the voice span untouched; a hard
    mute on the same span would silence it."""
    import subprocess

    import numpy as np

    from worker.engines import voice_render
    from worker.review import build_preview_clip

    # Fake separator: nothing is vocal → mixture − vocals == mixture, so a
    # voice-removed span keeps all its audio. Isolates the routing from Demucs.
    monkeypatch.setattr(
        voice_render, "separate_vocals", lambda w, sr: np.zeros_like(w)
    )

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=24:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)],
        check=True,
    )
    # window [5s,15s]; the flagged span sits at 3–5 s into the clip.
    voiced = build_preview_clip(media, 5000, 15000, [], [], [], [(8000, 10000)])
    muted = build_preview_clip(media, 5000, 15000, [], [(8000, 10000)], [], [])
    assert voiced is not None and muted is not None and voiced != muted

    # The voice-removed span keeps the tone; the hard-muted span is silent.
    assert _span_rms(voiced, 3.2, 4.8) > 1000
    assert _span_rms(muted, 3.2, 4.8) < 50


def test_stream_command_applies_blur_over_the_film(tmp_path):
    """A blur decision in Cleaned whole-film streaming becomes the render's gblur,
    even with no cuts (so the straight-transcode path is not taken)."""
    from worker.review import BLUR_SIGMA, stream_command

    cmd = stream_command(tmp_path / "f.mkv", 0, 60000, [], [], [(10000, 20000)])
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert f"gblur=sigma={BLUR_SIGMA}" in fc
    assert "enable='between(t,10.000,20.000)'" in fc


def test_build_scrub_audio_extracts_a_decodable_wav(tmp_path):
    """Live scrub audio: a compact mono WAV of the window the browser decodes for
    grain playback. Verify the invariant — a real RIFF/WAVE file of the right
    length, not just a zero exit code."""
    import subprocess
    import wave

    from worker.review import build_scrub_audio

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=24:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(media)],
        check=True,
    )
    out = build_scrub_audio(media, 4000, 14000)  # a 10s window
    assert out is not None and out.is_file()
    with wave.open(str(out)) as w:
        assert w.getnchannels() == 1  # downmixed to mono
        secs = w.getnframes() / w.getframerate()
        assert 9.0 < secs < 11.0  # the 10s window


def test_build_filmstrip_returns_jpeg(tmp_path):
    """The visual timing editor's frame strip is one tiled JPEG for the window."""
    import subprocess

    from worker.review import build_filmstrip

    media = tmp_path / "clip.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=10:duration=12",
         "-c:v", "libx264", str(media)],
        check=True,
    )
    jpeg = build_filmstrip(media, 5000, 7000, pad_s=2.0)
    assert jpeg is not None and jpeg[:2] == b"\xff\xd8"  # JPEG magic
