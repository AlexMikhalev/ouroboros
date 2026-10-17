"""A held Continue in its Project room, read from the server's work projection in the real SPA.

The Continue waits for three helpers of its failed earlier attempt. The census rows, queue facts and stored
result are the ones ``tests/test_work_projection_web_fixture.py`` projects (its ``held_continue`` case), and the
page reads the server's own ``works``/``rooms`` for them over the controlled transport of
``tests/test_room_work_browser.py``: one work, one row, and every surface says the same thing.
"""
from __future__ import annotations

import time

import pytest

from tests import test_room_work_browser as rw
from tests.test_work_projection_web_fixture import CASES, FENCES

room_ui = rw.room_ui
subscription_ui = rw.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

REASON = "Continue starts when 3 helpers of the earlier attempt finish"
NAME = "Ouroboros 7.7.0 — finish the saved release"
MARKER = f".nav-project-row[data-project-id='{rw.PROJECT['id']}'] .nav-activity-marker"


def test_held_continue_with_three_helpers_is_one_working_row_with_stop_earlier_and_no_resume(room_ui):
    owners, raw, stored, _held = CASES["held_continue"]
    history = {1: rw.reading(4), 42: [*rw.reading(6, 0, "Project"),
                                       rw.progress("c", "Waiting for the earlier attempt's helpers.", 30)]}
    transport = rw.Transport(room_ui["page"], history=history, projects=[rw.PROJECT], activities=owners)
    transport.raw, transport.results, transport.fences = raw, stored, FENCES
    page = rw.open_app(room_ui, transport, viewport={"width": 1100, "height": 722})
    rw.open_project(page, rw.PROJECT)
    transport.refresh()

    # The room works: its word in the bar, never amber, and the sidebar's three dots move.
    closed = rw.wait_facts(page, "project", lambda f: f["badge"] == "Working" and not f["disabled"], "the room working")
    assert closed["tone"] == "live" and not closed["amber"], closed
    page.wait_for_function("(selector) => document.querySelector(selector)?.dataset.state === 'working'", arg=MARKER)
    marker = page.locator(MARKER)
    assert (marker.get_attribute("data-motion"), marker.get_attribute("data-amber")) == ("1", "0")

    # One work, one row: the Continue and its earlier attempt's helpers are one row, which says why it waits.
    rw.trigger(page, "project").click()
    shade = rw.wait_facts(page, "project", lambda f: len(f["rows"]) == 1 and f["rows"][0]["secondary"] == REASON,
                          "one row with its reason")
    rw.evidence("held-continue-room", shade)
    rw.capture(page, "held-continue-room-open")
    row = shade["rows"][0]
    assert (row["name"], row["word"], row["tone"]) == (NAME, "Working", "live"), row
    assert not row["resume"] and not row["question"] and row["more"], row

    # Its card is the Continue's: the same word and reason, and no Resume.
    card = page.locator("#project-panel .chat-live-card[data-task-id='c']")
    assert card.locator("[data-live-phase]").inner_text() == "Working"
    assert card.locator("[data-live-phase-secondary]").inner_text() == REASON
    assert card.locator("[data-resume-run]").count() == 0

    # Its controls: Stop for the whole work and Stop earlier attempt for what still runs of the failed attempt.
    page.locator("#project-panel .rw-row[data-id='p'] .rw-more").click()
    menu = page.locator("body > .task-control-menu")
    menu.wait_for(state="visible")
    assert menu.evaluate("n => [...n.querySelectorAll('.task-control-item')].map(i => i.dataset.taskControl)") == [
        "stop_earlier", "stop_now"]
    assert menu.locator('[data-task-control="stop_earlier"]').inner_text() == "Stop earlier attempt"
    menu.locator('[data-task-control="stop_earlier"]').click()
    deadline = time.monotonic() + 5
    while not transport.posts and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    # Only the earlier attempt is stopped, as one cascade; the held Continue itself stays.
    assert [path for path, _ in transport.posts] == ["/api/tasks/p/cancel"], transport.posts
    assert transport.posts[0][1].get("cascade") is True, transport.posts
    assert transport.decision_posts() == []
