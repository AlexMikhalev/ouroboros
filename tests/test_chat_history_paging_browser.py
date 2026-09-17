"""Positive browser acceptance over real retained JSONL history and its gateway.

Only the request transport can be held/failed; every successful page and cursor
comes from the real history endpoint. The shared server uses its local mock LLM.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET, _SETTLE_RESTORE_FRAMES, _emit_ws_frame

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
MAIN = "#chat-messages"
HISTORY_URL = "/api/chat/history"
_FRAMES = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"
_EDGE_SCROLL = """(root, direction) => {
    root.scrollTop = direction === 'older' ? 0 : root.scrollHeight;
    root.dispatchEvent(new Event('scroll'));
}"""
_OBSERVE_HISTORY = """() => {
    const fetch = window.fetch.bind(window);
    window.__historyReads = [];
    window.__historyFault = '';
    window.fetch = async (input, init) => {
        const url = new URL(typeof input === 'string' ? input : input.url, location.href);
        if (url.pathname !== '/api/chat/history') return fetch(input, init);
        const read = {cursor: url.searchParams.get('cursor'),
            chatId: Number(url.searchParams.get('chat_id') || 1), done: false};
        window.__historyReads.push(read);
        if (read.cursor && window.__historyFault === 'hold') {
            window.__historyFault = '';
            await new Promise(resolve => { window.__releaseHistory = resolve; window.__heldHistory = read; });
        } else if (read.cursor && window.__historyFault === 'fail') {
            window.__historyFault = ''; read.error = 'injected transport failure'; read.done = true;
            throw new TypeError(read.error);
        }
        try {
            const response = await fetch(input, init);
            read.status = response.status;
            read.body = await response.clone().json();
            read.done = true;
            return response;
        } catch (error) { read.error = String(error); read.done = true; throw error; }
    };
}"""


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _result(root, task_id, **fields):
    _write(root / "task_results" / f"{task_id}.json", [{
        "_schema_version": 1, "task_id": task_id, "status": "completed", **fields,
    }])


def _human(index, chat_id=1, **extra):
    return {"ts": "2026-09-12T10:00:00Z", "chat_id": chat_id,
            "direction": "in", "text": f"history-human-{index:04d}", **extra}


def _progress(index, chat_id=1, **extra):
    return {"ts": "2026-09-12T10:00:00Z", "chat_id": chat_id,
            "task_id": f"history-task-{index // 145}",
            "content": f"history-progress-{index:04d}", **extra}


def _bulk_history(root):
    for segment in range(5):
        _write(root / "archive" / f"chat_20260901T00000{segment}.jsonl",
               [_human(segment * 360 + index) for index in range(360)])
        _write(root / "archive" / f"progress_20260901T00000{segment}.jsonl",
               [_progress(segment * 145 + index) for index in range(145)])
    _write(root / "logs" / "chat.jsonl", [_human(1800)])
    _write(root / "logs" / "progress.jsonl", [_progress(725)])
    for index in range(6):
        _result(root, f"history-task-{index}")


def _sparse_history(root):
    from ouroboros.projects_registry import create_project

    project = create_project(root, "history-sparse", name="Sparse archive room")
    foreign = create_project(root, "history-foreign", name="Other archive room")
    _write(root / "archive" / "chat_20260801T000000.jsonl", [
        _human(0, project["chat_id"], text="SPARSE_FIRST_SAVED_MESSAGE"),
    ])
    for segment in range(5):
        _write(root / "archive" / f"chat_20260902T00000{segment}.jsonl", [
            _human(index, foreign["chat_id"], text="OTHER_ROOM_ONLY " + "x" * 2000)
            for index in range(350)
        ])
    with (root / "logs" / "chat.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_human(1, project["chat_id"], text="SPARSE_LATEST_MESSAGE")) + "\n")
    return project


def _open(page, url):
    page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
    page.add_init_script(f"({_OBSERVE_HISTORY})()")
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function("() => window.__testSockets?.some(socket => socket.readyState === WebSocket.OPEN)")
    _idle(page, MAIN)


def _idle(page, feed):
    page.wait_for_function("""feed => {
        const root = document.querySelector(feed);
        return root?.querySelector('.chat-load-older')
            && !root.querySelector('.chat-load-older button')?.disabled
            && window.__historyReads.every(read => read.done);
    }""", arg=feed, timeout=30_000)
    page.evaluate(_FRAMES)


def _reads(page, chat_id=1):
    return page.evaluate("id => window.__historyReads.filter(read => read.chatId === id)", chat_id)


def _step(page, feed, direction="older", *, automatic=False):
    before = page.evaluate("() => window.__historyReads.length")
    if automatic:
        page.locator(feed).evaluate(_EDGE_SCROLL, direction)
    else:
        assert direction == "older", "`Load older messages` is the only paging button"
        # This exercises the visible button's handler without changing a reader's
        # selection or forcing an off-screen control into the reading viewport.
        page.locator(f"{feed} .chat-load-{direction} button").evaluate("node => node.click()")
    page.wait_for_function("n => window.__historyReads.length > n", arg=before, timeout=30_000)
    _idle(page, feed)


def _to_beginning(page, feed):
    for _ in range(80):
        _idle(page, feed)
        if page.locator(f"{feed} .chat-load-older button").is_hidden():
            assert page.locator(f"{feed} .chat-load-older-note").inner_text() == "Beginning of saved history"
            return
        _step(page, feed, automatic=True)
    pytest.fail("archive navigation did not reach its physical beginning")


def _open_project(page, project):
    row = page.locator(f'.nav-project-row[data-project-id="{project["id"]}"]')
    row.wait_for(state="attached", timeout=30_000)
    mobile_toggle = page.locator('#page-chat [data-mobile-nav-toggle]')
    # A translated-offscreen drawer is still CSS-visible to Playwright.
    if mobile_toggle.is_visible() and not page.locator('#primary-sidebar').evaluate("node => node.classList.contains('open')"):
        mobile_toggle.click()
    row.click()
    feed = f'#pchat-{project["id"]}-messages'
    page.locator(feed).wait_for(state="visible", timeout=30_000)
    _idle(page, feed)
    # Project show/reopen owns a bounded restoration lease before edge scrolling
    # is admitted. Use the existing viewport suite's frame settlement contract.
    page.evaluate(_SETTLE_RESTORE_FRAMES)
    return feed


def _screenshot(page, tmp_path, name):
    root = Path(os.environ.get("HISTORY_UI_EVIDENCE_DIR") or tmp_path / "history-ui-evidence")
    root.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(root / f"{name}.png"), full_page=False)


def _assert_main_beginning_visible(page):
    page.locator(MAIN).evaluate("node => { node.scrollTop = 0; node.dispatchEvent(new Event('scroll')); }")
    page.evaluate(_SETTLE_RESTORE_FRAMES)
    bounds = page.evaluate("""() => {
        const root = document.querySelector('#chat-messages');
        const first = [...root.querySelectorAll('.message')].find(node => node.textContent === 'history-human-0000');
        const note = root.querySelector('.chat-load-older-note');
        const header = document.querySelector('#page-chat .chat-page-header');
        const box = root.getBoundingClientRect();
        return {top: first?.getBoundingClientRect().top, noteTop: note?.getBoundingClientRect().top,
            floor: Math.max(box.top, header?.getBoundingClientRect().bottom || 0),
            bottom: box.bottom, scrollTop: root.scrollTop, note: note?.textContent};
    }""")
    assert bounds["scrollTop"] <= 1, bounds
    assert bounds["floor"] - 2 <= bounds["top"] < bounds["bottom"], bounds
    assert bounds["floor"] - 2 <= bounds["noteTop"] < bounds["bottom"], bounds
    assert bounds["note"] == "Beginning of saved history", bounds


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_history_archive_navigation_rotation_retry_and_sparse_project(
    direct_server_with_data, browser_engine, tmp_path,
):
    from playwright.sync_api import sync_playwright

    root, url = direct_server_with_data["data_dir"], direct_server_with_data["url"]
    _bulk_history(root)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            for viewport_index, (width, height, mobile) in enumerate([(1280, 850, False), (390, 844, True)]):
                context = browser.new_context(viewport={"width": width, "height": height},
                                              is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                try:
                    _open(page, url)
                    first = next(read["body"] for read in _reads(page) if read.get("body"))
                    assert first["page_cursor"] and first["has_more"]
                    assert first["window"]["complete"] is False
                    initial_id = next(row["history_id"] for row in first["messages"]
                                      if row.get("text") == "history-human-1800")
                    page.evaluate("() => { window.__historyFault = 'fail'; }")
                    _step(page, MAIN)
                    assert page.locator(f"{MAIN} .chat-load-older button").inner_text() == "Retry loading messages"
                    failed_cursor = _reads(page)[-1]["cursor"]
                    assert page.locator(f'{MAIN} [data-history-id="{initial_id}"]').count() == 1
                    _step(page, MAIN)
                    assert _reads(page)[-1]["cursor"] == failed_cursor

                    page.evaluate("() => { window.__historyFault = 'hold'; }")
                    page.locator(f"{MAIN} .chat-load-older button").evaluate("node => node.click()")
                    page.wait_for_function("() => Boolean(window.__heldHistory)")
                    live_text = f"LIVE_DURING_HISTORY_{browser_engine}_{width}"
                    for source in ("chat", "progress"):
                        source_path = root / "logs" / f"{source}.jsonl"
                        destination = root / "archive" / f"{source}_20260913T{viewport_index + 1:06d}.jsonl"
                        os.replace(source_path, destination)
                        _write(source_path, [])
                    live = _human(1900, text=live_text, direction="out", format="markdown",
                                  ts="2026-09-13T10:00:00Z")
                    _write(root / "logs" / "chat.jsonl", [live])
                    _emit_ws_frame(page, {"type": "chat", "role": "assistant", "chat_id": 1,
                                          "content": live_text, "ts": live["ts"]})
                    page.evaluate("() => window.__releaseHistory()")
                    _idle(page, MAIN)
                    assert page.locator(f"{MAIN} .message").filter(has_text=live_text).count() == 1
                    _to_beginning(page, MAIN)
                    assert page.locator(f"{MAIN} .chat-load-newer").count() == 0
                    page.locator(f"{MAIN} .message").filter(has_text="history-human-0000").wait_for(state="attached")
                    _assert_main_beginning_visible(page)
                    records = [row for read in _reads(page) if read.get("body", {}).get("messages")
                               for row in read["body"]["messages"]]
                    assert len({row["text"] for row in records if row.get("text", "").startswith("history-human-")}) == 1801
                    assert len({row["text"] for row in records if row.get("text", "").startswith("history-progress-")}) == 726
                    mounted = page.locator(f"{MAIN} [data-history-id]").evaluate_all(
                        "nodes => nodes.map(node => node.dataset.historyId)")
                    evidence = Path(os.environ.get("HISTORY_UI_EVIDENCE_DIR") or tmp_path / "history-ui-evidence")
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / f"archive-dom-{browser_engine}-{width}.json").write_text(json.dumps(
                        page.locator(f"{MAIN} [data-history-id]").evaluate_all("""nodes => nodes.map(node => ({
                            id: node.dataset.historyId, tag: node.tagName, className: node.className,
                            task: node.closest('.chat-live-card')?.dataset.taskId, text: node.textContent.slice(0, 180),
                        }))"""), indent=2), encoding="utf-8")
                    _screenshot(page, tmp_path, f"archive-beginning-{browser_engine}-{width}")
                    assert len(mounted) == len(set(mounted)), "physical source rows must not duplicate"
                    assert len(mounted) < 1200, "distant page bodies must leave the rendered window"
                    handles = {read["body"]["page_cursor"] for read in _reads(page)
                               if read.get("body", {}).get("page_cursor")}
                    seen = len(_reads(page))
                    # The live edge only refills the gap toward mounted rows; every
                    # read it makes replays an exact page handle, never a rebuild.
                    _step(page, MAIN, "newer", automatic=True)
                    returning = _reads(page)[seen:]
                    assert all(read.get("cursor") in handles for read in returning), returning
                    assert page.locator(f"{MAIN} .chat-load-newer").count() == 0
                    assert page.locator(f"{MAIN} .message").filter(has_text=live_text).count() == 1
                    page.reload(wait_until="domcontentloaded")
                    _idle(page, MAIN)
                    assert page.locator(f"{MAIN} .message").filter(has_text=live_text).count() == 1
                finally:
                    context.close()
            project = _sparse_history(root)
            for width, height, mobile in [(1280, 850, False), (390, 844, True)]:
                context = browser.new_context(viewport={"width": width, "height": height},
                                              is_mobile=mobile, has_touch=mobile)
                page = context.new_page()
                try:
                    _open(page, url)
                    feed = _open_project(page, project)
                    _to_beginning(page, feed)
                    # Every row of this room is mounted, so no edge control may claim
                    # that something newer waits beyond the rendered transcript, and
                    # further edge scrolling must not rescan the foreign-room pages.
                    assert page.locator(f"{feed} .chat-load-newer").count() == 0
                    settled = len(_reads(page, project["chat_id"]))
                    for direction in ("newer", "newer", "older"):
                        page.locator(feed).evaluate(_EDGE_SCROLL, direction)
                        _idle(page, feed)
                    assert len(_reads(page, project["chat_id"])) == settled, "a settled sparse room must not refetch"
                    assert page.locator(f"{feed} .message").filter(has_text="SPARSE_FIRST_SAVED_MESSAGE").count() == 1
                    assert "OTHER_ROOM_ONLY" not in page.locator(feed).inner_text()
                    assert any(read.get("body", {}).get("messages") == [] and read["body"]["has_more"]
                               for read in _reads(page, project["chat_id"]) if read.get("cursor"))
                    _screenshot(page, tmp_path, f"sparse-beginning-{browser_engine}-{width}")
                finally:
                    context.close()
        finally:
            browser.close()


def _feature_history(root):
    from ouroboros.artifacts import store_task_artifact_bytes
    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.projects_registry import create_project

    project = create_project(root, "history-details", name="History detail room")
    cid = project["chat_id"]
    quiz = {"quiz_id": "saved-choice", "question": "How should the retained history read?",
            "options": [{"label": "First", "detail": "Keep the first form"},
                        {"label": "Second", "detail": "Keep the second form", "recommended": True}],
            "state": "open", "assumption": "Keep inspecting the archive"}
    comment = "Use my own words\nKeep both lines."
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWQAAAABJRU5ErkJggg==")
    image_name = f"chat-media-{hashlib.sha256(image).hexdigest()}.png"
    for name, content in [(image_name, image), ("history-note.txt", b"Persisted history document\n")]:
        store_task_artifact_bytes(root, "history-media", name, content)
    old = [
        _human(0, cid, text="FEATURE_FIRST_SAVED_MESSAGE"),
        _human(1, cid, direction="out", type="quiz", task_id="quiz-owner", text=quiz["question"], quiz=quiz),
        _human(2, cid, direction="out", type="photo", task_id="history-media", text="Archived image", caption="Archived image",
               mime="image/png", download_url=f"/api/tasks/history-media/artifacts/{image_name}"),
        _human(3, cid, direction="out", type="document", task_id="history-media", text="Archived document", caption="Archived document",
               filename="history-note.txt", mime="text/plain", size_bytes=27,
               download_url="/api/tasks/history-media/artifacts/history-note.txt"),
        _human(4, cid, text="Routed archive message", client_message_id="routed-archive"),
    ]
    _write(root / "archive" / "chat_20260901T000000.jsonl", old)
    # Both source budgets reach these oldest companions in the same final page.
    _write(root / "logs" / "chat.jsonl", [*[_human(index, cid) for index in range(5, 1655)],
        _human(1656, cid, direction="system", type="quiz_answer", task_id="quiz-owner", text="",
               quiz={**quiz, "state": "answered", "comment": comment}),
        *[_human(1700 + index, cid, text=f"Following dialogue {index}", ts="2026-09-12T10:00:01Z")
          for index in range(20)]])
    append_chat_annotation(root, "routed-archive", action="route_to_project", status="delivered",
                           target="far-parent", target_label="History detail room", project_id=project["id"], project_chat_id=cid)
    child = {"task_id": "linked-child", "delegation_role": "subagent", "subagent_task_id": "linked-child",
             "parent_task_id": "far-parent", "root_task_id": "far-parent", "subagent_role": "Archive reader"}
    _write(root / "archive" / "progress_20260901T000000.jsonl", [
        _progress(0, cid, task_id="far-parent", content="FAR_PARENT_ORIGINAL_NARRATION"),
        _progress(1, cid, **child, subagent_event="progress", content="EARLY_CHILD_NARRATION"),
    ])
    for index in range(5):
        _write(root / "archive" / f"progress_20260902T00000{index}.jsonl", [
            _progress(2 + index * 130 + offset, cid) for offset in range(130)
        ])
    narration = "## Copyable heading\n\nParagraph after heading. " + "Reading preserved source. " * 24
    narration += "\n[Reference link](https://example.com/history-reference)"
    review = {"panels": [{"panel_id": "history-panel", "surface": "task_acceptance", "aggregate_signal": "PASS",
                           "transport_status": "success", "parse_status": "valid", "reason": "REVIEW_DETAIL_SENTINEL", "actors": []}]}
    child_result = "TERMINAL_CHILD_RESULT\n## Result heading\nResult paragraph. UNIQUE_CHILD_TAIL"
    _write(root / "logs" / "progress.jsonl", [
        _progress(700, cid, **child, subagent_event="completed", status="completed", content="Child finished",
                  result=child_result),
        _progress(701, cid, task_id="focus-root", content=narration, suggested_name="Selectable history title"),
    ])
    for task_id in ["far-parent", "linked-child", "focus-root", "history-media", "quiz-owner", *[f"history-task-{n}" for n in range(5)]]:
        _result(root, task_id, chat_id=cid, project_id=project["id"],
                **({"review_projection": review, "suggested_name": "Selectable history title"} if task_id == "focus-root" else {}))
    return project, comment


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_history_details_selection_replay_and_project_reopen(direct_server_with_data, browser_engine, tmp_path):
    from playwright.sync_api import sync_playwright

    root, url = direct_server_with_data["data_dir"], direct_server_with_data["url"]
    project, comment = _feature_history(root)
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            for width, height, mobile in [(1280, 900, False), (390, 844, True)]:
                context = browser.new_context(viewport={"width": width, "height": height},
                                              is_mobile=mobile, has_touch=mobile)
                context.route("https://example.com/history-reference", lambda route: route.fulfill(body="Reference opened"))
                page = context.new_page()
                try:
                    _open(page, url)
                    feed = _open_project(page, project)
                    card = page.locator(f'{feed} .chat-live-card[data-task-id="focus-root"]')
                    card.locator(':scope > [data-live-summary-button]').click()
                    line = card.locator(':scope > [data-live-timeline] .chat-live-line.expandable').filter(has_text="Copyable heading").first
                    toggle = line.locator('[data-live-line-toggle]')
                    title = line.locator('.chat-live-line-title')
                    title.scroll_into_view_if_needed()
                    box = title.bounding_box()
                    line_height = title.evaluate("node => parseFloat(getComputedStyle(node).lineHeight)")
                    page.mouse.move(box["x"] + 7, box["y"] + line_height / 2)
                    page.mouse.down()
                    page.mouse.move(box["x"] + min(box["width"] - 4, 100), box["y"] + line_height / 2, steps=10)
                    page.mouse.up()
                    assert page.evaluate("() => getSelection().toString().length > 0")
                    assert line.get_attribute("data-expanded") == "0", "drag selection must not activate the title"
                    page.evaluate("() => getSelection().removeAllRanges()")
                    toggle.click()
                    assert line.get_attribute("data-expanded") == "1"
                    toggle.press("Enter")
                    assert line.get_attribute("data-expanded") == "0"
                    toggle.press(" ")
                    assert line.get_attribute("data-expanded") == "1"
                    assert line.locator('.chat-live-line-title br').count() > 0
                    copied = title.evaluate("""node => {
                        const range = document.createRange(); range.selectNodeContents(node);
                        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
                        const copied = selection.toString(); selection.removeAllRanges(); return copied;
                    }""")
                    assert "Copyable heading\n" in copied and "Paragraph after heading" in copied
                    assert copied.count("Reading preserved source.") == 24
                    assert copied.rstrip().endswith("Reference link"), "copy must reach the full narration tail"
                    with page.expect_popup() as popup:
                        line.get_by_role("link", name="Reference link").click()
                    popup.value.wait_for_load_state()
                    popup.value.close()
                    assert line.get_attribute("data-expanded") == "1", "nested link must not toggle its title owner"

                    card.locator('[data-review-section-toggle]').click()
                    card.locator('[data-review-group-toggle]').click()
                    card.locator('[data-review-attempt-toggle]').first.click()
                    detail = card.locator('[data-review-attempt-detail]').first
                    assert "REVIEW_DETAIL_SENTINEL" in detail.inner_text()
                    page.evaluate("""({feed, lineKey}) => {
                        const root = document.querySelector(feed);
                        const line = root.querySelector(`[data-live-line-key="${lineKey}"]`);
                        const card = line.closest('.chat-live-card');
                        const detail = card.querySelector('[data-review-attempt-detail]');
                        const focus = card.querySelector('[data-review-attempt-toggle]');
                        focus.focus({preventScroll:true});
                        root.scrollTop += detail.getBoundingClientRect().top - root.getBoundingClientRect().top - 80;
                        const range = document.createRange(); range.selectNodeContents(line.querySelector('.chat-live-line-title'));
                        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
                        window.__historyKept = {line, detail, focus, selection: selection.toString(),
                            remaining: root.scrollHeight - root.scrollTop - root.clientHeight,
                            top: detail.getBoundingClientRect().top - root.getBoundingClientRect().top};
                    }""", {"feed": feed, "lineKey": line.get_attribute("data-live-line-key")})
                    assert page.evaluate("() => window.__historyKept.remaining > 100"), "fixture must place the review away from bottom-follow"
                    _screenshot(page, tmp_path, f"review-before-prepend-{browser_engine}-{width}")
                    _step(page, feed)
                    kept = page.evaluate("""feed => {
                        const root = document.querySelector(feed), old = window.__historyKept;
                        return {line: root.contains(old.line), detail: root.contains(old.detail),
                            focused: document.activeElement === old.focus,
                            selection: getSelection().toString() === old.selection,
                            drift: Math.abs(old.detail.getBoundingClientRect().top - root.getBoundingClientRect().top - old.top)};
                    }""", feed)
                    _screenshot(page, tmp_path, f"review-after-prepend-{browser_engine}-{width}")
                    assert kept["line"] and kept["detail"] and kept["focused"] and kept["selection"], kept
                    assert kept["drift"] <= 6, kept
                    _screenshot(page, tmp_path, f"selected-review-{browser_engine}-{width}")
                    page.evaluate("() => { getSelection().removeAllRanges(); document.activeElement?.blur(); }")
                    _to_beginning(page, feed)
                    child = page.locator(f'{feed} .chat-live-card[data-task-id="linked-child"]')
                    assert child.get_attribute("data-finished") == "1"
                    if child.get_attribute("data-expanded") != "1":
                        child.locator(':scope > [data-live-summary-button]').click()
                    assert "EARLY_CHILD_NARRATION" in child.inner_text()
                    assert "TERMINAL_CHILD_RESULT" in child.inner_text()
                    assert child.locator(':scope > [data-live-summary-button] [data-live-phase]').inner_text() == "Done"
                    evidence = Path(os.environ.get("HISTORY_UI_EVIDENCE_DIR") or tmp_path / "history-ui-evidence")
                    evidence.mkdir(parents=True, exist_ok=True)
                    (evidence / f"terminal-child-{browser_engine}-{width}.html").write_text(child.evaluate("node => node.outerHTML"), encoding="utf-8")
                    _screenshot(page, tmp_path, f"terminal-child-{browser_engine}-{width}")
                    result_line = child.locator(':scope > [data-live-timeline] .chat-live-line').filter(has_text="TERMINAL_CHILD_RESULT").first
                    assert result_line.count() == 1, "the full terminal result must remain accessible in the child timeline"
                    if result_line.locator('[data-live-line-toggle]').count() and result_line.get_attribute("data-expanded") != "1":
                        result_line.locator('[data-live-line-toggle]').click()
                    result_copy = result_line.locator('.chat-live-line-body').evaluate("""node => {
                        const range = document.createRange(); range.selectNodeContents(node);
                        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
                        const copied = selection.toString(); selection.removeAllRanges(); return copied;
                    }""")
                    assert "[RESULT]" in result_copy and "TERMINAL_CHILD_RESULT" in result_copy
                    assert "Result heading\n" in result_copy and "UNIQUE_CHILD_TAIL" in result_copy
                    quiz = page.locator(f'{feed} [data-quiz-id="saved-choice"]')
                    assert quiz.get_attribute("data-state") == "answered"
                    assert quiz.locator('.chat-quiz-answer').text_content() == f"Owner's answer: {comment}"
                    assert quiz.locator('.chat-quiz-option').count() == 2
                    assert quiz.locator('.chat-quiz-option.chosen').count() == 0
                    assert quiz.locator('.chat-quiz-option-recommended').count() == 1
                    image = page.locator(f'{feed} img')
                    page.wait_for_function("feed => [...document.querySelectorAll(`${feed} img`)].some(img => img.complete && img.naturalWidth > 0)", arg=feed)
                    assert image.count() > 0
                    assert page.locator(feed).get_by_text("history-note.txt", exact=True).count() > 0
                    anchor = page.locator(f'{feed} [data-client-message-id="routed-archive"]')
                    # The routing receipt's button lives in the shared action row between the note and the
                    # timestamp (never inside the nowrap note line): DESIGN "Quiz card", ARCHITECTURE 03.
                    assert anchor.locator('.msg-routing-actions').get_by_role("button", name="Open Project").count() == 1
                    assert anchor.locator('.msg-routing-annotation').get_by_role("button").count() == 0
                    assert anchor.evaluate("""node => {
                        const note = node.querySelector('.msg-routing-annotation'), row = node.querySelector('.msg-routing-actions'),
                            time = node.querySelector('.msg-time');
                        const follows = (a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
                        return Boolean(note && row && time) && follows(note, row) && follows(row, time);
                    }""")
                    anchor.scroll_into_view_if_needed()
                    identity = anchor.get_attribute("data-history-id")
                    before_top = anchor.evaluate("node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top")
                    _screenshot(page, tmp_path, f"archive-features-{browser_engine}-{width}")
                    page.locator('#project-panel-close').click()
                    reopened = _open_project(page, project)
                    restored = page.locator(f'{reopened} [data-history-id="{identity}"]')
                    restored.wait_for(state="attached", timeout=30_000)
                    page.evaluate(_FRAMES)
                    after_top = restored.evaluate("node => node.getBoundingClientRect().top - node.closest('.chat-messages').getBoundingClientRect().top")
                    assert abs(after_top - before_top) <= 8, (before_top, after_top)
                    assert page.locator(f'{reopened} [data-quiz-id="saved-choice"] .chat-quiz-answer').text_content() == f"Owner's answer: {comment}"
                finally:
                    context.close()
        finally:
            browser.close()
