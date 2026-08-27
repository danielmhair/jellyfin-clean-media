"""The review Studio at a phone viewport.

Reuses the ``worker``/``sidecar``/``_browser`` fixtures from conftest.py, but
with a phone-sized, touch-enabled browser context instead of the desktop one
``page`` gives every other e2e test — this is the one thing worth pinning down
separately from the desktop suite: that the app shell actually fits a phone
screen, that the Player/Findings tab switch works, and that a *touch* drag
(not a mouse drag) moves the playhead. See worker/review.py's touch-action /
@media(max-width:900px) rules and the Pointer Events conversion.
"""

from __future__ import annotations

import pytest

IPHONE_WIDTH, IPHONE_HEIGHT = 390, 844


@pytest.fixture()
def mpage(worker, sidecar, _browser):
    context = _browser.new_context(
        viewport={"width": IPHONE_WIDTH, "height": IPHONE_HEIGHT}, has_touch=True
    )
    page = context.new_page()
    page.goto(worker["review_url"])
    # Not "visible": the Player tab is the mobile default, so the findings rail
    # (and its rows) starts hidden — that's the behaviour under test, not a bug.
    # Wait for the row to exist in the DOM, and for the tab bar that's the actual
    # visible sign the page has hydrated.
    page.wait_for_selector("#D-list .drow", state="attached")
    page.wait_for_selector("#D-mtabbar")
    yield page
    context.close()


def test_no_horizontal_overflow_at_phone_width(mpage):
    """The recurring mobile failure mode: some fixed-width element (the old
    340px findings rail) forces the page wider than the viewport, so the whole
    app scrolls sideways. A few px of rounding slack, nothing more."""
    scroll_width = mpage.evaluate("() => document.documentElement.scrollWidth")
    assert scroll_width <= IPHONE_WIDTH + 4


def test_player_tab_shown_by_default_and_findings_hidden(mpage):
    # "You can scrub through on the video" is the default view — the findings
    # rail starts hidden behind the Findings tab, not competing for space.
    assert "mshow" in (mpage.locator("#D .dstage").get_attribute("class") or "")
    assert "mshow" not in (mpage.locator("#D .drail").get_attribute("class") or "")
    assert "on" in mpage.locator("#D-mtab-stage").get_attribute("class")


def test_tapping_findings_then_a_row_returns_to_the_player(mpage):
    mpage.click("#D-mtab-findings")
    assert "mshow" in (mpage.locator("#D .drail").get_attribute("class") or "")
    assert "mshow" not in (mpage.locator("#D .dstage").get_attribute("class") or "")

    mpage.locator("#D-list .drow").first.click()

    # Picking a finding jumps back to the Player tab automatically (D_select).
    assert "mshow" in (mpage.locator("#D .dstage").get_attribute("class") or "")
    assert "mshow" not in (mpage.locator("#D .drail").get_attribute("class") or "")


def test_zoom_buttons_change_the_visible_window(mpage):
    before = mpage.evaluate("() => D.viewEnd - D.viewStart")
    mpage.click("#D-zoomin")
    after_in = mpage.evaluate("() => D.viewEnd - D.viewStart")
    assert after_in < before  # zoomed in: a narrower window

    mpage.click("#D-zoomout")
    mpage.click("#D-zoomout")
    after_out = mpage.evaluate("() => D.viewEnd - D.viewStart")
    assert after_out > after_in  # zoomed back out: a wider window


def test_touch_drag_on_the_waveform_scrubs_the_playhead(mpage):
    """The core "scrub through the video" gesture, done as an actual touch
    (pointerType 'touch') rather than a mouse — this is what a phone sends, and
    is exactly what used to be silently ignored (mousedown-only listeners)."""
    # The monitor + transport + toolbar stack above the waveform easily exceed a
    # phone's short viewport, so it's off-screen until scrolled — elementFromPoint
    # below only hits genuinely visible pixels, not just anything in the DOM.
    mpage.locator("#D-edcard .edfilm").scroll_into_view_if_needed()
    box = mpage.eval_on_selector(
        "#D-edcard .edfilm",
        "el => { const r = el.getBoundingClientRect(); "
        "return {left:r.left, width:r.width, top:r.top, height:r.height}; }",
    )
    before = mpage.evaluate("() => D.playMs")
    mpage.evaluate(
        """({left,width,top,height}) => {
            const y = top + height/2;
            const x1 = left + width*0.15, x2 = left + width*0.8;
            const el = document.elementFromPoint(x1, y);
            const fire = (type,x) => el.dispatchEvent(new PointerEvent(type,
                {clientX:x, clientY:y, pointerType:'touch', bubbles:true, cancelable:true}));
            fire('pointerdown', x1);
            document.dispatchEvent(new PointerEvent('pointermove',
                {clientX:x2, clientY:y, pointerType:'touch', bubbles:true}));
            document.dispatchEvent(new PointerEvent('pointerup',
                {clientX:x2, clientY:y, pointerType:'touch', bubbles:true}));
        }""",
        box,
    )
    after = mpage.evaluate("() => D.playMs")
    assert after != before
