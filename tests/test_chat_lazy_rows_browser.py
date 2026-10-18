"""Lazy rows in the real app over real retained history (owner 18.10, 5A).

While the window narrows, the rows off the screen keep the size they were laid out at and the browser lays out
only the rows near the screen (style.css, chat_reading_position.js). Once the width holds, every row is laid out
again: the passage being read is where it was, scrolling up through the loaded history moves every row by exactly
the scroll, an older page lands above the passage without moving it, and a reader at the newest message stays
there. A rule that left rows lazy at rest fails here in WebKit: a row taking its real size only as it nears the
screen moves the passage being read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_chat_history_paging_browser import MAIN, _FRAMES, _append, _assert_at_newest, _idle, _open, _step
from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

_WORDS = ("history keeps every passage where the reader left it while the column narrows and widens again "
          "and the rows far above wait with the size they last had").split()

# What the feed's rows are doing: marked for a width move, rows under the lazy rule, and rows whose contents the
# browser actually skips (a first child that is shown, yet not rendered because its row is lazy), of all rows.
_LAZY = """feed => {
    const root = document.querySelector(feed), rows = [...root.children];
    const skipped = row => { const child = row.firstElementChild;
        return Boolean(child?.checkVisibility() && !child.checkVisibility({contentVisibilityAuto: true})); };
    return {marked: root.hasAttribute('data-resizing'), rows: rows.length, width: root.clientWidth,
        lazy: rows.filter(row => getComputedStyle(row).contentVisibility === 'auto').length,
        skipped: rows.filter(skipped).length};
}"""
# The row the reading owner anchors on: the first one whose bottom is inside the feed's box.
_PASSAGE = """feed => {
    const root = document.querySelector(feed), top = root.getBoundingClientRect().top;
    const node = [...root.querySelectorAll(':scope > [data-history-id]')].find(n => n.getBoundingClientRect().bottom > top);
    return {id: node.dataset.historyId, offset: node.getBoundingClientRect().top - top, rows: root.children.length};
}"""
_OFFSET = """([feed, id]) => {
    const root = document.querySelector(feed);
    const node = [...root.querySelectorAll(':scope > [data-history-id]')].find(n => n.dataset.historyId === id);
    return node ? node.getBoundingClientRect().top - root.getBoundingClientRect().top : null;
}"""
# Up through what is loaded, a step at a time, without a wheel (a gesture at the top would read an older page):
# the first row starting inside the feed must move by exactly the distance scrolled, two frames later.
_CLIMB = """async ([feed, step]) => {
    const root = document.querySelector(feed), frame = () => new Promise(r => requestAnimationFrame(r));
    const misses = [];
    let steps = 0;
    while (root.scrollTop > 0 && steps < 400) {
        const top = root.getBoundingClientRect().top;
        const row = [...root.children].find(n => n.getBoundingClientRect().top >= top);
        const before = row.getBoundingClientRect().top, from = root.scrollTop;
        root.scrollTop = Math.max(0, from - step);
        const moved = from - root.scrollTop;
        await frame(); await frame();
        const miss = row.getBoundingClientRect().top - before - moved;
        if (Math.abs(miss) > 1) misses.push({row: row.dataset.historyId || row.className, miss, scrollTop: root.scrollTop});
        steps += 1;
    }
    return {steps, misses, top: root.scrollTop};
}"""


def _seed(root, chat_id=1, count=420):
    """Owner messages from one line to several paragraphs: rows far from the 160px a never-laid-out row takes."""
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        words = " ".join(_WORDS[(index + k) % len(_WORDS)] for k in range(4 + (index * 37) % 170))
        text = f"Lazy row {index:04d}. {words}"
        if index % 7 == 0:
            text += "\n\n" + " ".join(_WORDS) + "\n\n" + " ".join(reversed(_WORDS))
        rows.append({"direction": "in", "chat_id": chat_id, "ts": (start + timedelta(minutes=index)).isoformat(),
                     "client_message_id": f"lazy-row-{chat_id}-{index}", "text": text})
    _append(root, "chat", rows)


def _read_at(page, feed, fraction):
    """The reader turns back into history and stops with a passage `fraction` of the way down the loaded rows."""
    page.locator(feed).evaluate("""(root, fraction) => {
        root.dispatchEvent(new WheelEvent('wheel', {deltaY: -1}));
        root.scrollTop = Math.round((root.scrollHeight - root.clientHeight) * fraction);
    }""", fraction)
    page.evaluate(_FRAMES)
    page.evaluate(_FRAMES)
    return page.evaluate(_PASSAGE, feed)


def _narrow(page, feed, widths, passage):
    """Each window width in turn. Once the feed's own width moved it is marked lazy, and the passage keeps its place
    (a step the feed's width did not follow, a Project panel at its widest, is recorded as not moved)."""
    seen = []
    for width in widths:
        before = page.locator(feed).evaluate("root => root.clientWidth")
        page.set_viewport_size({"width": width, "height": 850})
        page.evaluate(_FRAMES)  # the window's new width lands and the feed's resize observer reports it
        page.wait_for_function("""([feed, id, offset, before]) => {
            const root = document.querySelector(feed);
            const node = [...root.querySelectorAll(':scope > [data-history-id]')].find(n => n.dataset.historyId === id);
            const placed = node && Math.abs(node.getBoundingClientRect().top - root.getBoundingClientRect().top - offset) <= 2;
            return placed && (root.clientWidth === before || root.hasAttribute('data-resizing'));
        }""", arg=[feed, passage["id"], passage["offset"], before], timeout=5_000)
        state = page.evaluate(_LAZY, feed)
        seen.append({**state, "moved": state["width"] != before})
    return seen


def _held(page, feed):
    """At rest: the width held, the mark is gone, every row is laid out again."""
    page.wait_for_function("feed => !document.querySelector(feed).hasAttribute('data-resizing')", arg=feed,
                           timeout=10_000)
    page.evaluate(_FRAMES)
    return page.evaluate(_LAZY, feed)


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_lazy_rows_keep_the_passage_history_scroll_and_older_pages_exact(direct_server_with_data, browser_engine):
    from playwright.sync_api import sync_playwright

    _seed(direct_server_with_data["data_dir"])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            _open(page, direct_server_with_data["url"])
            rest = _held(page, MAIN)
            assert rest["rows"] > 100 and rest["lazy"] == rest["skipped"] == 0, rest

            passage = _read_at(page, MAIN, 0.55)
            during = _narrow(page, MAIN, range(1240, 990, -30), passage)
            moved = [state for state in during if state["moved"]]
            assert len(moved) >= 5 and all(state["marked"] and state["lazy"] == state["rows"] for state in moved), during
            assert max(state["skipped"] for state in during) > rest["rows"] // 2, (
                "the browser skipped the rows off the screen while the width moved", during)
            after = _held(page, MAIN)
            assert after["lazy"] == after["skipped"] == 0, after
            placed = page.evaluate(_OFFSET, [MAIN, passage["id"]])
            assert abs(placed - passage["offset"]) <= 1, (passage, placed)

            # Every row was laid out at the held width: nothing above the passage changes size as it nears the
            # screen, so scrolling up moves each row by exactly the scroll.
            climb = page.evaluate(_CLIMB, [MAIN, 240])
            assert climb["steps"] > 10 and climb["top"] == 0 and not climb["misses"], climb

            # An older page lands above the passage being read and moves nothing on the screen; its own rows
            # scroll exactly too.
            page.locator(MAIN).evaluate("root => { root.scrollTop = 600; }")
            page.evaluate(_FRAMES)
            reading = page.evaluate(_PASSAGE, MAIN)
            _step(page, MAIN)
            page.evaluate(_FRAMES)
            grown = page.evaluate(_PASSAGE, MAIN)
            assert grown["rows"] > reading["rows"] + 100, (reading, grown)
            assert abs(page.evaluate(_OFFSET, [MAIN, reading["id"]]) - reading["offset"]) <= 1, (reading, grown)
            climb = page.evaluate(_CLIMB, [MAIN, 240])
            assert climb["top"] == 0 and not climb["misses"], climb

            # Following the newest message, a resize keeps the reader there once the width holds.
            page.locator("#chat-scroll-bottom").evaluate("node => node.click()")
            _assert_at_newest(page, MAIN, "Lazy row 0419")
            for width in (1060, 1120, 1180, 1240, 1280):
                page.set_viewport_size({"width": width, "height": 850})
                page.evaluate(_FRAMES)
            _held(page, MAIN)
            _assert_at_newest(page, MAIN, "Lazy row 0419")
            _idle(page, MAIN)
        finally:
            browser.close()


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_a_project_panel_feed_is_lazy_only_while_its_width_moves(direct_server_with_data, browser_engine):
    """The Project panel's feed narrows with the window (its width follows the viewport): the same lazy rows, the
    same exact passage and history once the width holds."""
    from ouroboros.projects_registry import create_project
    from playwright.sync_api import sync_playwright

    from tests.test_chat_history_paging_browser import _open_project

    root = direct_server_with_data["data_dir"]
    project = create_project(root, "lazy-rows-room", name="Lazy rows room")
    _seed(root, project["chat_id"], 200)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            _open(page, direct_server_with_data["url"])
            feed = _open_project(page, project)
            rest = _held(page, feed)
            assert rest["rows"] > 100 and rest["lazy"] == rest["skipped"] == 0, rest

            passage = _read_at(page, feed, 0.5)
            during = _narrow(page, feed, range(1240, 990, -30), passage)
            moved = [state for state in during if state["moved"]]
            assert moved and all(state["marked"] and state["lazy"] == state["rows"] for state in moved), during
            assert max(state["skipped"] for state in during) > rest["rows"] // 2, during
            after = _held(page, feed)
            assert after["lazy"] == after["skipped"] == 0, after
            assert abs(page.evaluate(_OFFSET, [feed, passage["id"]]) - passage["offset"]) <= 1, passage
            climb = page.evaluate(_CLIMB, [feed, 240])
            assert climb["steps"] > 5 and climb["top"] == 0 and not climb["misses"], climb
        finally:
            browser.close()
