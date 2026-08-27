"""End-to-end coverage of the review "Studio" page, one test per feature area.

Each test reads as a reviewer's action → a persisted result: the page is driven
in a real browser against a real worker, and the assertion is on the observable
outcome (progress text, the monitor, the sidecar), never on internal DOM/JS
structure. See conftest.py for the fixture film and the canonical findings.
"""

from __future__ import annotations

import re

import pytest

# gore #1, nudity #2, profanity #3/#4 (both "damn"), suggestive #5 — see conftest.


def _select(page, sid):
    """Click a finding's rail row (not its decision buttons) to open it."""
    page.eval_on_selector(
        f'#D-list .drow[data-id="{sid}"]',
        "el => el.click()",
    )


# ---------------------------------------------------------------- discreet mode


def test_discreet_mode_default_blurs_and_hold_to_reveal_clears(page):
    monitor = page.locator("#D-monitor")
    # Discreet on by default: the picture is BLURRED but still shown (you can play
    # through a bad scene to find it), with a small badge; not hidden.
    assert "discreet" in (monitor.get_attribute("class") or "")
    assert page.locator("#D-veil").is_visible()
    assert page.locator("#D-mframe").is_visible()  # the (blurred) frame is up, not hidden

    # Hold the reveal button: the blur drops (the .discreet class comes off) and a
    # real frame is loaded. The button listens for pointerdown/up (touch + mouse
    # in one code path) — a real press fires both a mouse and a pointer event,
    # but dispatch_event only injects the exact one named, so pointerdown/up is
    # what must be dispatched here.
    page.dispatch_event("#D-reveal", "pointerdown")
    page.wait_for_function(
        "() => !document.getElementById('D-monitor').classList.contains('discreet')"
    )
    page.wait_for_function(
        "() => { const i=document.getElementById('D-mframe');"
        " return i && i.naturalWidth > 0; }"
    )
    page.dispatch_event("#D-reveal", "pointerup")
    # Releasing re-blurs (the class returns).
    page.wait_for_function(
        "() => document.getElementById('D-monitor').classList.contains('discreet')"
    )


# --------------------------------------------------------------- inline reasons


def test_descriptions_show_inline_without_opening_anything(page):
    # A parent must know what a finding is without displaying it.
    assert page.locator('#D-list .drow[data-id="1"] .rdesc').inner_text() \
        .startswith("blood/wound detail")
    # Profanity reads as the muted word, not a raw category.
    assert "damn" in page.locator('#D-list .drow[data-id="3"] .rdesc').inner_text()


# ------------------------------------------------------------------ decisions


def test_cut_out_from_row_persists_and_survives_reload(page, sidecar):
    page.click('#D-list .drow[data-id="1"] button.qd.cut')
    page.wait_for_function(
        "() => document.querySelector('#D-progstats').textContent.includes('1')"
    )
    assert sidecar.by_id(1)["approved"] is True

    page.reload()
    page.wait_for_selector("#D-list .drow")
    # The decision shows after a reload (loaded from the sidecar).
    row_class = page.get_attribute('#D-list .drow[data-id="1"]', "class")
    assert "cut" in row_class
    assert sidecar.by_id(1)["approved"] is True


def test_leave_in_persists_and_dims_the_lane(page, sidecar):
    _select(page, 5)  # suggestive, so its region is in the editor view
    page.click('#D-list .drow[data-id="5"] button.qd.leave')
    page.wait_for_function(
        "() => (document.querySelector('#D-edlane .region.leave')||{}) "
        "&& document.querySelectorAll('#D-edlane .region.leave').length >= 0"
    )
    assert sidecar.by_id(5)["approved"] is False
    # The editor lane reflects the decision, not just the action.
    dimmed = page.locator("#D-edlane .region.leave")
    assert dimmed.count() >= 1
    assert float(dimmed.first.evaluate("el => getComputedStyle(el).opacity")) < 0.6


def test_undecided_round_trip(page, sidecar):
    page.click('#D-list .drow[data-id="1"] button.qd.cut')
    page.wait_for_function("() => true")
    assert sidecar.by_id(1)["approved"] is True
    # Clicking the same decision again clears it back to undecided.
    page.click('#D-list .drow[data-id="1"] button.qd.cut')
    page.wait_for_function(
        "() => document.querySelector('#D-progstats').textContent.includes('0 cut out')"
    )
    assert sidecar.by_id(1)["approved"] is None


def test_progress_bar_counts(page, sidecar):
    page.click('#D-list .drow[data-id="1"] button.qd.cut')
    page.click('#D-list .drow[data-id="2"] button.qd.leave')
    page.wait_for_function(
        "() => { const t=document.querySelector('#D-progstats').textContent;"
        " return t.includes('1 cut out') && t.includes('1 left in') && t.includes('3 to review'); }"
    )


# -------------------------------------------------------------------- minimap


def test_minimap_has_a_marker_per_finding_and_seeks_on_click(page):
    ticks = page.locator("#D-ftrack .ftick")
    assert ticks.count() == 5  # one per finding
    before = page.locator("#D-tt").inner_text()
    # Click near the right of the minimap → the playhead jumps far downstream.
    box = page.locator("#D-ftrack").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.9, box["y"] + box["height"] / 2)
    page.wait_for_function(
        "(prev) => document.querySelector('#D-tt').textContent !== prev",
        arg=before,
    )


def test_dragging_the_viewport_box_pans_the_editor(page):
    hint = page.locator("#D-edcard .zoomhint")
    before = hint.inner_text()
    box = page.locator("#D-fbox").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 220, box["y"] + box["height"] / 2, steps=8)
    page.mouse.up()
    # The editor now shows a different window.
    page.wait_for_function(
        "(prev) => document.querySelector('#D-edcard .zoomhint').textContent !== prev",
        arg=before,
    )


def test_wheel_zoom_resizes_the_minimap_box(page):
    box_el = page.locator("#D-fbox")
    before = box_el.evaluate("el => el.getBoundingClientRect().width")
    # Zoom in (wheel up) over the editor: the span shrinks, so the box narrows.
    ed = page.locator("#D-edscroll").bounding_box()
    page.mouse.move(ed["x"] + ed["width"] / 2, ed["y"] + ed["height"] / 2)
    page.mouse.wheel(0, -400)
    page.wait_for_function(
        "(prev) => document.querySelector('#D-fbox').getBoundingClientRect().width < prev - 1",
        arg=before,
    )


# ------------------------------------------------------- stacked film + wave


@pytest.mark.parametrize("sid", [1, 3])  # a visual finding and a spoken one
def test_filmstrip_and_waveform_present_for_any_finding(page, sid):
    _select(page, sid)
    # Filmstrip: a real tiled JPEG for the viewport.
    page.wait_for_function(
        "() => { const i=document.querySelector('.edfilmimg');"
        " return i && i.naturalWidth > 0; }"
    )
    # Waveform: a canvas fed by real peaks.
    assert page.locator("#D-edcard canvas").count() == 1


# ------------------------------------------------------------ carve: add/split


def test_add_cut_creates_an_approved_skip(page, sidecar):
    before = len(sidecar.read())
    _select(page, 5)
    page.click("#D-add")
    page.wait_for_function(
        "(n) => document.querySelectorAll('#D-list .drow').length === n + 1",
        arg=before,
    )
    newest = max(sidecar.read(), key=lambda s: s["id"])
    assert newest["approved"] is True
    assert newest["recommendedAction"] == "skip"
    assert newest["engine"] == "manual"


def test_split_makes_two_findings_covering_the_span(page, sidecar):
    _select(page, 2)  # nudity [20000, 24000]
    # Move the playhead inside the region, then split there.
    page.click("#D-fwd1")
    page.click("#D-fwd1")  # playhead ≈ 22000ms
    page.click("#D-split")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#D-list .drow'))"
        ".filter(r => r.querySelector('.cname').textContent.trim()==='nudity').length === 2"
    )
    nud = sidecar.by_category("nudity")
    assert len(nud) == 2
    # The two pieces together still cover the original span, split in the middle.
    assert nud[0]["startMs"] == 20000
    assert nud[1]["endMs"] == 24000
    assert nud[0]["endMs"] == nud[1]["startMs"]


def test_keep_the_beat_split_then_delete_leaves_a_gap(page, sidecar):
    _select(page, 2)  # nudity [20000, 24000]
    page.click("#D-fwd1")
    page.click("#D-fwd1")  # ≈ 22000
    page.click("#D-split")
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 6"
    )
    # Delete the second half (the piece now covering 22000–24000): select it,
    # then delete. The gap it leaves is the beat that plays normally.
    later = [s for s in sidecar.by_category("nudity") if s["startMs"] == 22000][0]
    _select(page, later["id"])
    page.click("#D-delregion")
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 5"
    )
    nud = sidecar.by_category("nudity")
    assert len(nud) == 1
    assert (nud[0]["startMs"], nud[0]["endMs"]) == (20000, 22000)
    # 22000–24000 is now uncovered — the kept beat.
    assert not any(s["startMs"] == 22000 for s in sidecar.read())


def test_delete_finding_removes_it(page, sidecar):
    _select(page, 1)
    page.on("dialog", lambda d: d.accept())
    page.click("#D-trash")
    page.wait_for_function(
        "() => !document.querySelector('#D-list .drow[data-id=\"1\"]')"
    )
    assert sidecar.by_id(1) is None


# ----------------------------------------------------------- action / category


def test_action_switch_marks_render_only(page, sidecar):
    _select(page, 2)  # nudity, action skip → mute (render-only)
    page.select_option("#D-act", "mute")
    page.wait_for_function(
        "() => document.querySelector('#D-edform .ro') "
        "&& document.querySelector('#D-edform .ro').textContent.includes('render-only')"
    )
    assert sidecar.by_id(2)["recommendedAction"] == "mute"


def test_category_edit_persists(page, sidecar):
    _select(page, 1)
    page.select_option("#D-cat", "violence")
    page.wait_for_function(
        "() => document.querySelector('#D-list .drow[data-id=\"1\"] .cname')"
        ".textContent.trim() === 'violence'"
    )
    assert sidecar.by_id(1)["category"] == "violence"
    # Re-categorising is not a decision.
    assert sidecar.by_id(1)["approved"] is None


def test_description_edit_persists(page, sidecar):
    _select(page, 2)
    page.fill("#D-desc", "corrected note: shots 30-36")
    page.eval_on_selector("#D-desc", "el => el.blur()")
    page.wait_for_function(
        "() => document.querySelector('#D-list .drow[data-id=\"2\"] .rdesc')"
        ".textContent.includes('corrected note')"
    )
    assert sidecar.by_id(2)["reasoning"] == "corrected note: shots 30-36"


# ------------------------------------------------------------------ timing


def test_typed_timestamp_edit_persists(page, sidecar):
    _select(page, 1)
    page.fill("#D-start", "0:00:07.250")
    page.press("#D-start", "Enter")
    page.wait_for_function("() => true")
    # allow the patch to land
    page.wait_for_timeout(300)
    assert sidecar.by_id(1)["startMs"] == 7250


def test_nudge_buttons_move_the_bound(page, sidecar):
    _select(page, 1)  # start 8000
    page.click('#D-edform .nudge[data-edit="start"][data-d="-1000"]')  # −1s
    page.click('#D-edform .nudge[data-edit="start"][data-d="25"]')     # +25ms
    page.wait_for_timeout(300)
    assert sidecar.by_id(1)["startMs"] == 7025


def test_dragging_a_region_edge_retimes_without_jumping(page, sidecar):
    _select(page, 1)  # gore [8000, 11000] — a 3000ms region
    region = page.locator("#D-edlane .region.sel")
    region.scroll_into_view_if_needed()  # the editor can sit below the stage fold
    box = region.bounding_box()
    # Drag the LEFT edge inward (right) by ~50px, in several steps — the case that
    # used to make the edge leap to the far right after the first move (a stale
    # track ref) and collapse the region to its 200ms minimum.
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 2, y)
    page.mouse.down()
    for dx in (10, 18, 25):  # a small inward nudge of the left edge
        page.mouse.move(box["x"] + dx, y)
    page.mouse.up()
    page.wait_for_timeout(300)
    s = sidecar.by_id(1)
    dur = s["endMs"] - s["startMs"]
    assert s["startMs"] > 8000            # the left edge moved inward
    assert s["endMs"] == 11000            # the right edge is untouched
    # A small drag only trims a little; the old jump slammed the edge to the far
    # right, collapsing the region to its 200ms floor. Anything well above 200
    # proves the edge tracked the cursor instead of leaping.
    assert dur > 1500, f"region collapsed to {dur}ms — the edge jumped"


def test_dragging_a_region_edge_moves_the_playhead_and_scrubs(page):
    """Retiming a region edge is a scrub: the playhead follows the edge you're
    dragging and the scrub-audio buffer decodes, so you place the bound by ear
    and eye — 'scrubbing with the segment landing right.'"""
    _select(page, 1)  # gore [8000, 11000]
    region = page.locator("#D-edlane .region.sel")
    region.scroll_into_view_if_needed()
    box = region.bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] - 2, y)  # grab the RIGHT edge
    page.mouse.down()
    for dx in (10, 22, 34):  # drag it outward
        page.mouse.move(box["x"] + box["width"] + dx, y)
    # mid-drag: scrub audio for the moving edge decodes (the ear part)
    page.wait_for_function("() => D.sa && D.sa.buf !== null", timeout=10000)
    page.mouse.up()
    end = page.evaluate("() => D_get(1).endMs")
    playhead = page.evaluate("() => D.playMs")
    assert end > 11000, f"right edge did not extend: {end}"
    assert abs(playhead - end) < 400, f"playhead {playhead} did not follow the edge {end}"


# -------------------------------------------------------------------- merge


def test_same_type_merge_collapses_into_one(page, sidecar):
    page.click("#D-mergemode")
    page.click('.mpick[data-pick="3"]')  # damn
    page.click('.mpick[data-pick="4"]')  # second damn
    page.wait_for_function("() => !document.getElementById('D-mergego').disabled")
    page.click("#D-mergego")
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#D-list .drow'))"
        ".filter(r => r.querySelector('.cname').textContent.includes('“')).length <= 1 "
        "|| document.querySelectorAll('#D-list .drow').length === 4"
    )
    prof = sidecar.by_category("profanity")
    assert len(prof) == 1
    assert prof[0]["startMs"] == 40000 and prof[0]["endMs"] == 42200


def test_mixed_type_merge_is_refused(page):
    page.click("#D-mergemode")
    page.click('.mpick[data-pick="3"]')  # profanity
    page.click('.mpick[data-pick="2"]')  # nudity
    page.wait_for_function(
        "() => document.getElementById('D-mergehint').textContent.includes('ONE type')"
    )
    assert page.locator("#D-mergego").is_disabled()


# ---------------------------------------------------------------- keyboard


def test_keyboard_cut_and_navigation(page, sidecar):
    _select(page, 1)
    page.locator("body").click(position={"x": 5, "y": 5})  # focus off any field
    page.keyboard.press("c")  # cut out the selected finding
    page.wait_for_timeout(300)
    assert sidecar.by_id(1)["approved"] is True
    # J moves to the next finding.
    page.keyboard.press("j")
    page.wait_for_function(
        "() => document.querySelector('#D-list .drow[data-id=\"2\"]')"
        ".classList.contains('cur')"
    )


def test_scrubbing_highlights_the_nearest_finding(page):
    # Click on the minimap near finding #5 (~63s of 90s ≈ 0.7) and check the rail.
    box = page.locator("#D-ftrack").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.70, box["y"] + box["height"] / 2)
    page.wait_for_function(
        "() => document.querySelector('#D-list .drow[data-id=\"5\"]')"
        ".classList.contains('cur')"
    )


# ----------------------------------------------- type filter scopes the workspace


def test_type_filter_scopes_rail_and_minimap(page):
    # Filtering to a type must show only that type in the rail AND the minimap.
    assert page.locator("#D-ftrack .ftick").count() == 5
    page.click('#D-typechips button[data-type="gore"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 1"
    )
    assert page.locator("#D-ftrack .ftick").count() == 1  # minimap scoped too
    assert page.locator('#D-list .drow[data-id="1"]').count() == 1
    # Clearing restores everything.
    page.click('#D-typechips button[data-type="all"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 5"
    )


def test_type_filter_scopes_the_editor_regions(page):
    # The editor should only draw regions of the filtered type.
    _select(page, 3)  # a "damn" — filter to it
    page.click('#D-typechips button[data-type="damn"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 2"
    )
    # In a window that also overlaps other findings, only "damn" regions show.
    labels = page.locator("#D-edlane .region .rlabel").all_inner_texts()
    # both damn findings are mutes → MUTE regions, nothing else
    assert labels and all(l == "MUTE" for l in labels)


def test_bulk_cut_all_of_a_type(page, sidecar):
    page.click('#D-typechips button[data-type="damn"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 2"
    )
    assert "2 damn shown" in page.locator("#D-bulklabel").inner_text()
    page.click("#D-bulkcut")
    page.wait_for_function(
        "() => document.querySelector('#D-progstats').textContent.includes('2 cut out')"
    )
    prof = sidecar.by_category("profanity")
    assert all(s["approved"] is True for s in prof)


def test_bulk_leave_all_of_a_type(page, sidecar):
    page.click('#D-typechips button[data-type="damn"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 2"
    )
    page.click("#D-bulkleave")
    page.wait_for_function(
        "() => document.querySelector('#D-progstats').textContent.includes('2 left in')"
    )
    assert all(s["approved"] is False for s in sidecar.by_category("profanity"))


# ------------------------------------------------------ playback (Phase 1)


def test_picture_mode_does_not_defeat_discreet_blur(page):
    monitor = page.locator("#D-monitor")
    # Discreet on by default. The blur IS the privacy layer now — switching to
    # Video mode (moving picture) does NOT reveal; the picture stays blurred.
    assert "discreet" in (monitor.get_attribute("class") or "")
    page.click('#D-picmode button[data-pic="video"]')
    page.wait_for_timeout(150)
    assert "discreet" in (monitor.get_attribute("class") or "")  # still blurred
    # Only hold-to-reveal clears it, in any picture mode. (pointerdown/up: see
    # the comment in test_discreet_mode_default_blurs_and_hold_to_reveal_clears.)
    page.dispatch_event("#D-reveal", "pointerdown")
    page.wait_for_function(
        "() => !document.getElementById('D-monitor').classList.contains('discreet')"
    )
    page.dispatch_event("#D-reveal", "pointerup")
    page.wait_for_function(
        "() => document.getElementById('D-monitor').classList.contains('discreet')"
    )


def test_play_loads_the_clip_with_audio(page):
    _select(page, 3)  # a profanity mute finding
    page.click("#D-pp")
    # The monitor's <video> gets a real /api/clip src for this finding's window.
    page.wait_for_function(
        "() => { const v=document.getElementById('D-clip');"
        " return v && v.src.includes('/api/clip') && v.src.includes('startMs=40000'); }"
    )
    # Normal audio mode → not muted, no mute/voice flag on the clip.
    src = page.eval_on_selector("#D-clip", "v => v.src")
    assert "mute=true" not in src and "voice=true" not in src


def test_cleaned_audio_applies_the_findings_mute(page):
    _select(page, 3)  # a mute finding [40000, 40900]
    page.click('#D-audmode button[data-aud="cleaned"]')
    page.click("#D-pp")
    # Cleaned → a windowed render via the preview endpoint, with this finding's
    # span in the mute list, so you hear it as it will play once acted on.
    page.wait_for_function(
        "() => { const v=document.getElementById('D-clip');"
        " return v && v.src.includes('/api/preview_clip') && v.src.includes('mute=40000-40900'); }"
    )


def test_cleaned_cuts_a_skip_finding(page):
    _select(page, 2)  # a skip finding [20000, 24000]
    page.click('#D-audmode button[data-aud="cleaned"]')
    page.click("#D-pp")
    # A skip finding is CUT from the preview (its footage isn't transcoded), so
    # its span lands in the cut list, not played-then-jumped.
    page.wait_for_function(
        "() => { const v=document.getElementById('D-clip');"
        " return v && v.src.includes('/api/preview_clip') && v.src.includes('cut=20000-24000'); }"
    )


def test_cleaned_skips_every_cut_in_the_window_not_just_the_selected(page):
    # The bug: Cleaned only skipped the highlighted scene. Now it applies every
    # cut-out (approved) finding in the window too.
    _select(page, 2)                         # nudity skip [20000,24000]
    page.click('#D-list .drow[data-id="2"] button.qd.cut')  # approve it → a cut
    page.wait_for_timeout(200)
    page.click("#D-back1")                   # playhead 20000 → 19000
    page.click("#D-back1")                   # → 18000
    page.click("#D-add")                     # add a second cut at ~18000 (approved skip)
    page.wait_for_function(
        "() => document.querySelectorAll('#D-list .drow').length === 6"
    )
    page.click('#D-audmode button[data-aud="cleaned"]')
    page.click("#D-pp")
    # The preview's cut list now carries TWO spans: the added one and the
    # approved nudity (clipped to the window).
    src = page.eval_on_selector("#D-clip", "v => v.src")
    assert "/api/preview_clip" in src
    import urllib.parse as u
    cut = u.parse_qs(u.urlparse(src).query).get("cut", [""])[0]
    spans = [c for c in cut.split(",") if c]
    assert len(spans) >= 2, f"expected 2+ cuts in the window, got {spans}"


def test_clicking_timeline_while_playing_seeks_immediately(page):
    _select(page, 1)  # gore near 0:08
    page.click("#D-pp")
    page.wait_for_function(  # wait until it's actually playing
        "() => document.getElementById('D-pp').textContent === '❚❚'"
    )
    # Click ~75% along the full-film minimap. The old bug: nothing moved until you
    # paused. Now the seek wins — playback stops and the playhead jumps there.
    box = page.locator("#D-ftrack").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.75, box["y"] + box["height"] / 2)
    page.wait_for_function(
        "() => document.getElementById('D-pp').textContent === '▶'"  # stopped
    )
    # The playhead jumped to ~75% of the runtime, immediately — not stuck at ~0:08.
    pos = page.evaluate("() => D.playMs")
    assert pos > 40000, f"playhead did not seek while playing: {pos}ms"


def test_muted_audio_mutes_the_element(page):
    _select(page, 3)
    page.click('#D-audmode button[data-aud="muted"]')
    page.click("#D-pp")
    page.wait_for_function(
        "() => document.getElementById('D-clip').src.includes('/api/clip')"
    )
    assert page.eval_on_selector("#D-clip", "v => v.muted") is True


# ---------- Phase 2: whole-film continuous streaming ----------

def test_film_range_streams_the_whole_film_from_the_playhead(page):
    """Range=Film plays the /api/stream endpoint from the playhead to the end —
    a continuous stream, not a per-scene clip — and playback actually advances."""
    _select(page, 1)  # gore [8000,11000] → playhead at 8000
    page.click('#D-range button[data-range="film"]')
    page.click("#D-pp")
    # A whole-film stream from the playhead, not a windowed /api/clip.
    page.wait_for_function(
        "() => { const v=document.getElementById('D-clip');"
        " return v && v.src.includes('/api/stream') && v.src.includes('startMs=8000'); }"
    )
    src = page.eval_on_selector("#D-clip", "v => v.src")
    assert "/api/clip" not in src and "endMs=" not in src  # runs to the end, uncapped
    # It plays and the playhead advances past the finding into the next scene —
    # a scene clip would have been capped to the finding's window.
    page.wait_for_function(
        "() => document.getElementById('D-pp').textContent === '❚❚'"
    )
    page.wait_for_function("() => D.playMs > 11500", timeout=20000)


def test_film_cleaned_stream_cuts_every_approved_skip_ahead(page, sidecar):
    """Film + Cleaned removes *every* approved skip from the playhead onward,
    live over the whole remaining film — the verify-the-invariant preview at film
    scale, carried to the stream endpoint as the cut list."""
    page.click('#D-list .drow[data-id="2"] button.qd.cut')  # nudity skip [20000,24000] → cut
    page.click('#D-list .drow[data-id="5"] button.qd.cut')  # suggestive skip [60000,66000] → cut
    page.wait_for_timeout(200)
    _select(page, 1)  # playhead 8000 — both cuts are ahead
    page.click('#D-range button[data-range="film"]')
    page.click('#D-audmode button[data-aud="cleaned"]')
    page.click("#D-pp")
    page.wait_for_function(
        "() => document.getElementById('D-clip').src.includes('/api/stream')"
    )
    import urllib.parse as u

    src = page.eval_on_selector("#D-clip", "v => v.src")
    q = u.parse_qs(u.urlparse(src).query)
    spans = [c for c in q.get("cut", [""])[0].split(",") if c]
    assert any(s.startswith("20000-") for s in spans), spans  # the nudity cut
    assert any(s.startswith("60000-") for s in spans), spans  # the suggestive cut
    assert q.get("startMs", ["-1"])[0] == "8000"


def test_library_switcher_lists_films_and_searches(page):
    """The top-left combobox loads the library work-list from /api/library, shows
    a status badge per film, and filters as you type (and empties on a miss)."""
    page.click("#D-swtrigger")
    page.wait_for_selector("#D-swpanel:not(.hidden)")
    page.wait_for_function("() => document.querySelectorAll('#D-swlist .swrow').length >= 1")
    assert page.locator("#D-swlist .swrow .swbadge").first.is_visible()

    page.fill("#D-swinput", "some")  # the fixture film is "Some Film (2010)"
    page.wait_for_function(
        "() => [...document.querySelectorAll('#D-swlist .swn')].some(n => /some/i.test(n.textContent))"
    )
    page.fill("#D-swinput", "zzznotarealfilm")
    page.wait_for_selector("#D-swlist .swempty")


def test_the_workspace_fits_the_viewport_without_a_page_scrollbar(page):
    """The studio is a fixed-viewport app shell: the page itself never scrolls
    (that page scrollbar made editing hard and hid the editor below the fold);
    only the findings rail and the stage scroll, inside their own bounds."""
    assert page.evaluate(
        "() => document.documentElement.scrollHeight <= window.innerHeight + 2"
    ), "the page scrolls — the app shell should fit 100vh"
    # the stage is the internal scroll surface for the monitor + editor
    assert page.evaluate(
        "() => { const s=document.querySelector('#D .dstage');"
        " return getComputedStyle(s).overflowY === 'auto'; }"
    )


def test_editor_scrollbar_pans_the_viewport(page):
    """The editor's horizontal scrollbar thumb pans the zoomed viewport without
    reaching for the minimap — dragging it right moves the view forward."""
    _select(page, 1)
    before = page.evaluate("() => D.viewStart")
    thumb = page.locator("#D-edthumb")
    thumb.scroll_into_view_if_needed()  # the editor bar can sit below a short viewport
    box = thumb.bounding_box()
    cy = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] / 2, cy)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 140, cy, steps=6)
    page.mouse.up()
    after = page.evaluate("() => D.viewStart")
    assert after > before + 1000, f"scrollbar did not pan the view: {before} -> {after}"


def test_scrollbar_step_buttons_scrub_the_playhead(page):
    """The ◀/▶ buttons flanking the editor scrollbar nudge the playhead a little
    bit each click — a fine scrub — right, then back left."""
    _select(page, 1)  # playhead at the finding's start (8000)
    before = page.evaluate("() => D.playMs")
    page.locator("#D-edright").scroll_into_view_if_needed()
    page.click("#D-edright")
    after = page.evaluate("() => D.playMs")
    assert after > before, f"right step did not advance the playhead: {before} -> {after}"
    page.click("#D-edleft")
    assert page.evaluate("() => D.playMs") < after  # left steps back


def test_cleaned_blurs_a_blur_finding(page, sidecar):
    """A finding set to blur, played in Cleaned, carries a blur span to the
    preview — so the picture is blurred where the render will blur it."""
    _select(page, 1)  # gore [8000,11000]
    page.select_option("#D-act", "blur")
    page.wait_for_function("() => D_get(1).recommendedAction === 'blur'")
    page.click('#D-audmode button[data-aud="cleaned"]')
    page.click("#D-pp")
    page.wait_for_function(
        "() => { const v=document.getElementById('D-clip');"
        " return v && v.src.includes('/api/preview_clip') && v.src.includes('blur=8000-11000'); }"
    )


def test_scrubbing_plays_live_audio_grains(page):
    """Dragging the playhead fetches + decodes a WAV window around the cursor so
    grains can be played at the drag position — you hear the film as you scrub.
    Grains aren't observable headless, but the decoded buffer is the proof the
    gesture→AudioContext→fetch→decode path ran end to end."""
    _select(page, 3)  # opens the editor around a finding
    film = page.locator("#D-edcard .edfilm")
    box = film.bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * 0.4, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.6, y, steps=8)
    page.wait_for_function("() => D.sa && D.sa.buf !== null", timeout=10000)
    page.mouse.up()
    ok = page.evaluate(
        "() => { const s=D.sa; return s.winEnd>s.winStart && s.buf.duration>1; }"
    )
    assert ok, "scrub audio window/buffer not established"
