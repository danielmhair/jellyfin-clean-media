import io
import subprocess
from pathlib import Path

import pytest

from worker.models import Segment, Timeline
from worker.render import approved_for_render, build_command, keep_intervals, render


def _tl(*segs):
    return Timeline(mediaFingerprint="fp", segments=list(segs))


def _seg(i, start, end, action, approved=None):
    return Segment(
        id=i,
        startMs=start,
        endMs=end,
        category="x",
        confidence=1.0,
        engine="e",
        recommendedAction=action,
        approved=approved,
    )


def test_mute_only_stream_copies_video():
    cmd, n_blur, n_mute = build_command(
        Path("in.mkv"), _tl(_seg(1, 1000, 2000, "mute")), Path("out.mkv")
    )
    assert (n_blur, n_mute) == (0, 1)
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "volume=enable='between(t,1.000,2.000)':volume=0" in cmd


def test_blur_triggers_video_encode():
    cmd, n_blur, n_mute = build_command(
        Path("in.mkv"), _tl(_seg(1, 5000, 7000, "blur")), Path("out.mkv")
    )
    assert (n_blur, n_mute) == (1, 0)
    assert "h264_nvenc" in cmd
    assert "gblur=sigma=30:enable='between(t,5.000,7.000)'" in cmd
    # audio untouched when nothing is muted
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_rejected_segments_are_excluded():
    cmd, n_blur, n_mute = build_command(
        Path("in.mkv"),
        _tl(
            _seg(1, 1000, 2000, "mute"),
            _seg(2, 3000, 4000, "mute", approved=False),
            _seg(3, 5000, 6000, "mute", approved=True),
        ),
        Path("out.mkv"),
    )
    assert n_mute == 2
    expr = cmd[cmd.index("-af") + 1]
    assert "between(t,1.000,2.000)" in expr
    assert "between(t,3.000,4.000)" not in expr
    assert "between(t,5.000,6.000)" in expr


def test_combined_blur_and_mute():
    cmd, n_blur, n_mute = build_command(
        Path("in.mkv"),
        _tl(_seg(1, 1000, 2000, "mute"), _seg(2, 5000, 7000, "blur")),
        Path("out.mkv"),
        use_nvenc=False,
    )
    assert (n_blur, n_mute) == (1, 1)
    assert "libx264" in cmd
    assert "-vf" in cmd and "-af" in cmd


def test_keep_intervals_inverts_skips():
    assert keep_intervals([_seg(1, 10_000, 12_000, "skip")], 60.0) == [
        (0.0, 10.0),
        (12.0, 60.0),
    ]
    # overlapping skips merge; a skip at t=0 produces no leading keep
    assert keep_intervals(
        [_seg(1, 0, 5_000, "skip"), _seg(2, 4_000, 8_000, "skip")], 20.0
    ) == [(8.0, 20.0)]


def test_skip_uses_trim_concat_not_setpts_framerate():
    """select+setpts=N/FRAME_RATE/TB desyncs telecined sources; use concat."""
    cmd, _, _ = build_command(
        Path("in.mkv"),
        _tl(_seg(1, 10_000, 12_000, "skip")),
        Path("out.mkv"),
        duration_s=60.0,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "N/FRAME_RATE/TB" not in fc
    assert "trim=start=0.000:end=10.000" in fc
    assert "trim=start=12.000:end=60.000" in fc
    assert "atrim=start=12.000:end=60.000" in fc
    assert "concat=n=2:v=1:a=1[outv][outa]" in fc
    assert "-map" in cmd and "[outv]" in cmd
    # subtitles would desync after a cut
    assert "-sn" in cmd
    assert "-c:s" not in cmd


def test_skip_requires_duration():
    with pytest.raises(ValueError):
        build_command(
            Path("in.mkv"), _tl(_seg(1, 10_000, 12_000, "skip")), Path("out.mkv")
        )


def test_skip_and_mute_combine():
    cmd, _, n_mute = build_command(
        Path("in.mkv"),
        _tl(_seg(1, 10_000, 12_000, "skip"), _seg(2, 30_000, 31_000, "mute")),
        Path("out.mkv"),
        duration_s=60.0,
    )
    assert n_mute == 1
    fc = cmd[cmd.index("-filter_complex") + 1]
    # mute is applied to the full stream before it is split and cut
    assert fc.index("volume=") < fc.index("asplit")
    assert "[0:a]volume=" in fc


def test_blur_and_skip_chain_before_cut():
    cmd, _, _ = build_command(
        Path("in.mkv"),
        _tl(_seg(1, 10_000, 12_000, "skip"), _seg(2, 30_000, 31_000, "blur")),
        Path("out.mkv"),
        duration_s=60.0,
    )
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]gblur=" in fc
    assert fc.index("gblur=") < fc.index("split=")


def test_empty_timeline_raises():
    with pytest.raises(RuntimeError):
        build_command(Path("in.mkv"), _tl(), Path("out.mkv"))


def test_approved_for_render_selects_only_approved():
    tl = _tl(
        _seg(1, 0, 1000, "mute", approved=True),
        _seg(2, 2000, 3000, "mute", approved=False),
        _seg(3, 4000, 5000, "skip", approved=None),
        _seg(4, 6000, 7000, "blur", approved=True),
    )
    assert [s.id for s in approved_for_render(tl)] == [1, 4]


def test_unreviewed_timeline_renders_nothing():
    # An analysis-time timeline leaves every finding approved=None. The
    # by-path render must act on none of them — review → approve → act means an
    # unreviewed film renders nothing, never every detection at once.
    tl = _tl(_seg(1, 0, 1000, "mute"), _seg(2, 2000, 3000, "mute"))
    assert approved_for_render(tl) == []


# -- overwriting an existing clean copy ---------------------------------------
# ffmpeg's -y truncates its output the instant it starts, so rendering straight
# over a clean copy destroys a good file the moment anything goes wrong — and
# re-rendering *from* that copy (read and write the same path) cannot work at
# all. The renderer writes beside the target and swaps in only on success.


class _FakePopen:
    """Stand in for ffmpeg: writes the file it was told to, then exits."""

    def __init__(self, cmd, returncode=0, **kwargs):
        self.out = Path(cmd[-1])
        self.returncode = returncode
        if returncode == 0:
            self.out.write_bytes(b"rendered")
        else:
            self.out.write_bytes(b"half a f")  # a partial file, as ffmpeg leaves
        self.stdout = io.StringIO("")

    def wait(self):
        return self.returncode


def _mute_timeline():
    return Timeline(
        mediaFingerprint="f",
        segments=[
            Segment(id=1, startMs=1000, endMs=2000, category="profanity",
                    confidence=1.0, engine="subtitles", recommendedAction="mute",
                    approved=True),
        ],
    )


def _clean_film(tmp_path):
    folder = tmp_path / "Some Film (2010)"
    folder.mkdir()
    (folder / "Some Film (2010).mkv").write_bytes(b"original")
    clean = folder / "Some Film (2010) - Clean.mkv"
    clean.write_bytes(b"the copy being watched")
    return folder, clean


def test_re_rendering_a_clean_copy_over_itself_swaps_in_the_new_file(tmp_path, monkeypatch):
    _, clean = _clean_film(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    out = render(clean, _mute_timeline(), clean, lambda f, s: None, use_nvenc=False)

    assert out == clean
    assert clean.read_bytes() == b"rendered"
    # No partial left over next to it.
    assert sorted(p.name for p in clean.parent.iterdir()) == [
        "Some Film (2010) - Clean.mkv", "Some Film (2010).mkv",
    ]


def test_a_failed_render_leaves_the_existing_clean_copy_playable(tmp_path, monkeypatch):
    _, clean = _clean_film(tmp_path)
    monkeypatch.setattr(
        subprocess, "Popen", lambda cmd, **kw: _FakePopen(cmd, returncode=1, **kw)
    )

    with pytest.raises(RuntimeError):
        render(clean, _mute_timeline(), clean, lambda f, s: None, use_nvenc=False)

    assert clean.read_bytes() == b"the copy being watched"
    assert sorted(p.name for p in clean.parent.iterdir()) == [
        "Some Film (2010) - Clean.mkv", "Some Film (2010).mkv",
    ]
