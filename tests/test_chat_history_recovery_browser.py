"""Recovery through real history routes, retained files and project bindings."""
from __future__ import annotations

import os

import pytest

from tests.test_chat_history_paging_browser import (
    MAIN, _human, _idle, _open, _open_project, _reads, _screenshot, _step,
    _to_beginning, _write,
)
from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def _assert_unique_rows(page, feed):
    ids = page.locator(f"{feed} [data-history-id]").evaluate_all(
        "nodes => nodes.map(node => node.dataset.historyId)")
    assert len(ids) == len(set(ids)), "source rows must remain unique after recovery"


@pytest.mark.skipif(os.name == "nt", reason="POSIX archive permissions trigger the real read failure")
@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_unreadable_archive_keeps_recent_messages_and_retries_real_history(
    direct_server_with_data, browser_engine, tmp_path,
):
    from playwright.sync_api import sync_playwright

    root, url = direct_server_with_data["data_dir"], direct_server_with_data["url"]
    archive = root / "archive" / "chat_20260901T000000.jsonl"
    _write(archive, [_human(0)])
    _write(root / "logs" / "chat.jsonl", [_human(index) for index in range(1, 176)])
    mode = archive.stat().st_mode
    archive.chmod(0)
    try:
        # Do not count a run under an identity that bypasses this failure.
        try:
            with archive.open("rb"):
                pytest.skip("the current identity bypasses unreadable-file permissions")
        except PermissionError:
            pass
        with sync_playwright() as pw:
            browser = getattr(pw, browser_engine).launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 850})
                _open(page, url)
                fallback = _reads(page)[-1]
                assert fallback["status"] == 200
                payload = fallback["body"]
                assert payload["reason_code"] == "history_source_unavailable"
                assert payload["window"]["complete"] is False
                assert not payload.get("page_cursor") and not payload.get("next_cursor")
                recent = [row for row in payload["messages"] if row.get("role") == "user"]
                assert len(recent) == 150
                assert {row["text"] for row in recent} == {
                    f"history-human-{index:04d}" for index in range(26, 176)
                }
                assert page.locator(f"{MAIN} .message").filter(has_text="history-human-0175").count() == 1
                assert page.locator(f"{MAIN} .message").filter(has_text="history-human-0026").count() == 1
                assert page.locator(f"{MAIN} .chat-load-older button").inner_text() == "Retry loading messages"
                assert "Beginning of saved history" not in page.locator(MAIN).inner_text()
                _screenshot(page, tmp_path, f"archive-recent-readable-{browser_engine}")
                page.locator(MAIN).evaluate("root => { root.scrollTop = 0; }")
                _idle(page, MAIN)
                _screenshot(page, tmp_path, f"archive-unavailable-{browser_engine}")

                archive.chmod(mode)
                before_retry = len(_reads(page))
                _step(page, MAIN)
                # At the top edge a successful retry can immediately trigger
                # another real older-page fetch; assert the retry response itself.
                recovered = _reads(page)[before_retry]
                assert recovered["cursor"] is None and recovered["status"] == 200
                assert recovered["body"]["page_cursor"] and recovered["body"]["next_cursor"]
                _to_beginning(page, MAIN)
                assert page.locator(f"{MAIN} .message").filter(has_text="history-human-0000").count() == 1
                _assert_unique_rows(page, MAIN)
                _screenshot(page, tmp_path, f"archive-recovered-{browser_engine}")
            finally:
                browser.close()
    finally:
        archive.chmod(mode)


def _pin_reading_selection(page, feed):
    return page.locator(feed).evaluate("""root => {
        const rows = [...root.querySelectorAll('[data-history-id]')];
        const node = rows[Math.floor(rows.length / 2)];
        root.scrollTop += node.getBoundingClientRect().top - root.getBoundingClientRect().top - 180;
        const range = document.createRange(); range.selectNodeContents(node);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
        window.__recoveryReading = {node, text: selection.toString(),
            top: node.getBoundingClientRect().top - root.getBoundingClientRect().top};
        return {text: selection.toString(), remaining: root.scrollHeight - root.scrollTop - root.clientHeight};
    }""")


def _assert_reading_preserved(page, feed):
    kept = page.locator(feed).evaluate("""root => {
        const old = window.__recoveryReading;
        return {sameNode: root.contains(old.node), selected: getSelection().toString() === old.text,
            drift: Math.abs(old.node.getBoundingClientRect().top - root.getBoundingClientRect().top - old.top)};
    }""")
    assert kept["sameNode"] and kept["selected"], kept
    assert kept["drift"] <= 6, kept


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_project_membership_refresh_keeps_reading_anchor_and_replaces_cursor(
    direct_server_with_data, browser_engine, tmp_path,
):
    from playwright.sync_api import sync_playwright
    from ouroboros.projects_registry import bind_task_to_project, create_project

    root, url = direct_server_with_data["data_dir"], direct_server_with_data["url"]
    project = create_project(root, "history-recovery", name="History recovery room")
    _write(root / "archive" / "chat_20260901T000000.jsonl", [
        _human(0, task_id="newly-bound-old-task", text="NEWLY_BOUND_OLD_ROW"),
        *[_human(index, project["chat_id"]) for index in range(1, 901)],
    ])
    _write(root / "logs" / "chat.jsonl", [_human(901, project["chat_id"])])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            _open(page, url)
            feed = _open_project(page, project)
            for _ in range(3):
                _step(page, feed)
            reading = _pin_reading_selection(page, feed)
            assert reading["text"] and reading["remaining"] > 100, reading
            old_page = _reads(page, project["chat_id"])[-1]["body"]["page_cursor"]
            _screenshot(page, tmp_path, f"project-before-membership-change-{browser_engine}")

            bind_task_to_project(root, "newly-bound-old-task", project["id"], origin={"absent": "system"})
            _step(page, feed)
            changed = _reads(page, project["chat_id"])[-1]
            assert changed["status"] == 409
            assert changed["body"]["reason_code"] == "history_view_changed"
            assert page.locator(f"{feed} .chat-load-older button").inner_text() == "Refresh history"
            _assert_reading_preserved(page, feed)

            _step(page, feed)
            refreshed = _reads(page, project["chat_id"])[-1]
            assert refreshed["status"] == 200 and refreshed["cursor"] is None
            assert refreshed["body"]["page_cursor"] != old_page
            _assert_reading_preserved(page, feed)
            _screenshot(page, tmp_path, f"project-after-membership-refresh-{browser_engine}")
            # Follow the new handle through the same visible control. A renamed
            # button alone must not leave the reader retrying an invalid cursor.
            _step(page, feed)
            advanced = _reads(page, project["chat_id"])[-1]
            assert advanced["status"] == 200
            assert advanced["cursor"] == refreshed["body"]["next_cursor"]
            _assert_reading_preserved(page, feed)
            _assert_unique_rows(page, feed)
            page.evaluate("() => getSelection().removeAllRanges()")
            _to_beginning(page, feed)
            _idle(page, feed)
            assert page.locator(f"{feed} .message").filter(has_text="NEWLY_BOUND_OLD_ROW").count() == 1
        finally:
            browser.close()
