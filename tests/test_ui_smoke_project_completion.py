"""Browser proof for the terminal Project completion projection."""

from __future__ import annotations

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401


def _wait_status(page, expected, timeout=10_000):
    page.wait_for_function(
        """(expected) => {
            const el = document.querySelector('#chat-status');
            return el && el.textContent.trim() === expected;
        }""",
        arg=expected,
        timeout=timeout,
    )


@pytest.mark.ui_browser
def test_ui_project_completion_pointer_keeps_project_history_scoped(direct_server_with_data):  # noqa: F811
    """Main gets one human pointer; Project keeps its detailed terminal history."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, "tower-defence", name="Tower Defence")
    project_chat = int(project["chat_id"])
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    target_label = "Tower Defence › Fix nested delegation"
    task_id = "tower-root-1"
    append_jsonl(logs / "chat.jsonl", {
        "ts": "2026-08-22T10:00:00+00:00", "direction": "out", "chat_id": 1,
        "user_id": 1, "text": f"{target_label} · Completed\nOpen the Project for details.",
        "type": "project_completion_summary", "task_id": task_id,
        "project_id": project["id"], "project_name": project["name"],
        "target_label": target_label, "status": "completed",
    })
    append_jsonl(logs / "chat.jsonl", {
        "ts": "2026-08-22T09:58:00+00:00", "direction": "in", "chat_id": 1,
        "user_id": 1, "text": "Continue the Tower Defence task",
        "client_message_id": "route-to-tower",
    })
    append_chat_annotation(data_dir, "route-to-tower", action="route_to_project",
                           target=project["id"], target_label=target_label, status="delivered")
    append_jsonl(logs / "progress.jsonl", {
        "ts": "2026-08-22T09:59:00+00:00", "type": "send_message", "direction": "out",
        "chat_id": project_chat, "task_id": task_id, "is_progress": True,
        "content": "Project progress: nested delegation is running.",
    })
    append_jsonl(logs / "chat.jsonl", {
        "ts": "2026-08-22T10:00:01+00:00", "direction": "system", "chat_id": project_chat,
        "user_id": 1, "text": "Project task summary", "type": "task_summary",
        "task_id": task_id, "tool_calls": 1, "rounds": 2, "status": "completed",
    })

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)
                summary = page.locator('#chat-messages .chat-bubble[data-system-type="project_completion_summary"]')
                summary.wait_for(state="visible", timeout=30_000)
                summary_text = summary.inner_text()
                assert target_label in summary_text
                # The Main row is a pointer: one status word and the shared cause sentence,
                # never the answer excerpt (owner Q5=A); the answer stays in the Project.
                assert "Open the Project for details." in summary_text
                assert "Release shipped." not in summary_text
                # The row points at its Project with the one reference: the Project's name, never a
                # button of its own and never the raw id.
                reference = summary.locator('[data-intent="open-project"]')
                assert reference.count() == 1
                assert reference.get_attribute("aria-label") == f"Open project {project['name']}"
                assert "Open Project" not in summary_text
                assert project["id"] not in summary_text

                annotation = page.locator('#chat-messages .msg-routing-annotation').filter(has_text=target_label)
                annotation.wait_for(state="visible", timeout=10_000)
                assert project["id"] not in annotation.inner_text()
                main_text = page.locator("#chat-messages").inner_text()
                assert "Project progress: nested delegation is running." not in main_text
                assert "Project task summary" not in main_text

                reference.click()
                page.wait_for_selector("#project-panel:not([hidden])", timeout=30_000)
                assert page.locator("#project-panel-title").inner_text() == project["name"]
                panel = page.locator(f"#panel-pchat-{project['id']}")
                panel.wait_for(state="visible", timeout=30_000)
                task_card = panel.locator(f'.chat-live-card[data-task-id="{task_id}"]')
                task_card.wait_for(state="visible", timeout=10_000)
                assert task_card.locator(".chat-live-phase").inner_text().strip() == "Done"
                task_card.locator("[data-live-summary-button]").click()
                task_card.get_by_text(
                    "Project progress: nested delegation is running.",
                    exact=False,
                ).first.wait_for(state="visible", timeout=30_000)
                assert "Project progress: nested delegation is running." in task_card.inner_text()
                assert panel.locator('.chat-bubble[data-system-type="project_completion_summary"]').count() == 0
                assert panel.locator('.system-message-actions [data-intent="open-project"]').count() == 0
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


_LONG_ANSWER = (
    "**Done: release shipped.** PR #7 merged into `main`.\n\n"
    + "\n\n".join(f"Paragraph {i}: " + "detail " * 30 for i in range(1, 9))
)


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1280, 390])
def test_ui_project_completion_mirror_is_an_ordinary_folded_message(direct_server_with_data, engine, width):  # noqa: F811
    """A root that ended with its own answer reaches Main as Ouroboros's message; others stay pointers."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, f"mirror-{engine}-{width}", name="A rather long Project name that must yield on a phone")
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    base = {"direction": "system", "chat_id": 1, "user_id": 1, "type": "project_completion_summary",
            "project_id": project["id"], "project_name": project["name"], "status": "completed"}
    append_jsonl(logs / "chat.jsonl", {
        **base, "ts": "2026-08-22T10:00:00+00:00", "task_id": "root-long",
        "text": "P › Long · Done\nOpen the Project for details.", "target_label": "P › Long",
        "completion_answer": _LONG_ANSWER})
    append_jsonl(logs / "chat.jsonl", {
        **base, "ts": "2026-08-22T10:01:00+00:00", "task_id": "root-short",
        "text": "P › Short · Done\nOpen the Project for details.", "target_label": "P › Short",
        "completion_answer": "All good, nothing to report."})
    append_jsonl(logs / "chat.jsonl", {
        **base, "ts": "2026-08-22T10:02:00+00:00", "task_id": "root-pointer", "status": "failed",
        "text": "P › Broken · Failed\nOpen the Project for details.", "target_label": "P › Broken"})

    try:
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": 900})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)
                rows = page.locator('#chat-messages .chat-bubble[data-system-type="project_completion_summary"]')
                rows.nth(2).wait_for(state="visible", timeout=30_000)
                long_row, short_row, pointer = rows.nth(0), rows.nth(1), rows.nth(2)

                # The answered endings speak in Ouroboros's voice and say nothing in the host's.
                for row in (long_row, short_row):
                    classes = row.get_attribute("class")
                    assert "assistant" in classes and "project-answer" in classes and "system" not in classes.split()
                    text = row.inner_text()
                    assert "Open the Project for details." not in text and "Open Project" not in text
                    assert row.locator(".sender").inner_text().strip() == "Ouroboros"
                    assert row.locator(".system-message-actions .chat-quiz-project").count() == 1
                assert "release shipped" in long_row.inner_text()
                # The fold: the long answer is clamped and faded, the short one is neither.
                page.wait_for_function(
                    "() => document.querySelectorAll('.chat-bubble.project-answer.is-folded').length === 1",
                    timeout=10_000)
                geometry = long_row.locator(".message").evaluate(
                    "n => ({client: n.clientHeight, scroll: n.scrollHeight})")
                assert geometry["scroll"] > geometry["client"] > 0
                assert "is-folded" not in short_row.get_attribute("class")
                # The whole answer is in the node: the fold is a viewport, never a cut.
                assert "Paragraph 8" in long_row.locator(".message").evaluate("n => n.textContent")
                # No horizontal overflow of the transcript, the chip yields instead.
                assert page.locator("#chat-messages").evaluate("n => n.scrollWidth <= n.clientWidth + 1")

                # The ending without an answer is still the System pointer, and it points at the
                # Project with the SAME control as the answered ones: a row's voice never picks it.
                assert "system" in pointer.get_attribute("class").split()
                assert "Open the Project for details." in pointer.inner_text()
                shapes = [row.locator('[data-intent="open-project"]').evaluate(
                    "n => [n.className, n.getAttribute('aria-label'), [...n.children].map(c => c.className).join(' ')]")
                    for row in (long_row, pointer)]
                assert shapes[0] == shapes[1]

                # The chip names the Project and opens it.
                chip = long_row.locator(".chat-quiz-project")
                assert project["name"] in (chip.get_attribute("title") or "")
                chip.click()
                page.wait_for_selector("#project-panel:not([hidden])", timeout=30_000)
                assert page.locator("#project-panel-title").inner_text() == project["name"]
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ui_project_completion_mirror_folds_after_mounting_on_a_hidden_page(direct_server_with_data, engine):  # noqa: F811
    """A row mounted while another page is open has no layout box; the fade arrives when Chat is shown."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, f"hidden-{engine}", name="Hidden mount")
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    append_jsonl(logs / "chat.jsonl", {
        "direction": "system", "chat_id": 1, "user_id": 1, "type": "project_completion_summary",
        "project_id": project["id"], "project_name": project["name"], "status": "completed",
        "ts": "2026-08-22T11:00:00+00:00", "task_id": f"root-hidden-{engine}",
        "text": "Hidden mount › Long · Done\nOpen the Project for details.",
        "target_label": "Hidden mount › Long", "completion_answer": _LONG_ANSWER})

    try:
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                page.goto(url + "#dashboard", wait_until="domcontentloaded", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)
                row = page.locator("#chat-messages .chat-bubble.project-answer")
                row.wait_for(state="attached", timeout=30_000)
                assert not page.locator("#page-chat").evaluate("n => n.classList.contains('active')")
                # No box, no measurement: the row is left unmarked rather than guessed.
                assert row.locator(".message").evaluate("n => n.clientHeight") == 0
                assert "is-folded" not in row.get_attribute("class")

                page.locator('[data-nav-page="chat"]').first.click()
                page.wait_for_function(
                    "() => document.querySelectorAll('.chat-bubble.project-answer.is-folded').length === 1",
                    timeout=10_000)
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1280, 390])
def test_ui_project_completion_show_full_reads_the_whole_answer_in_main(direct_server_with_data, engine, width):  # noqa: F811
    """A long final is readable whole in Main: Show full/Show less in place, keyboard stops only on
    visible text, the reason and Files outside the fold, Copy the answer alone, default fold on reload."""
    import os
    from pathlib import Path

    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    evidence = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or "evidence") / f"show-full-{engine}-{width}"
    evidence.mkdir(parents=True, exist_ok=True)
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, f"show-full-{engine}-{width}", name="Long report")
    answer = _LONG_ANSWER + "\n\nThe [deep link](https://example.com/deep) and `TAIL_MARKER` end it."
    # Main history around the answer: folding it must keep its control where the reader is.
    for index in range(30):
        append_jsonl(data_dir / "logs" / "chat.jsonl", {
            "direction": "in", "chat_id": 1, "user_id": 1, "ts": f"2026-08-22T11:{index + 1:02d}:00+00:00",
            "client_message_id": f"before-{index}", "text": f"An earlier Main note {index}."})
    append_jsonl(data_dir / "logs" / "chat.jsonl", {
        "direction": "system", "chat_id": 1, "user_id": 1, "type": "project_completion_summary",
        "project_id": project["id"], "project_name": project["name"], "status": "completed",
        "ts": "2026-08-22T12:00:00+00:00", "task_id": f"root-show-{engine}-{width}",
        "text": "Show full › Long · Done with warnings\nOpen the Project for details.",
        "target_label": "Show full › Long", "completion_answer": answer,
        "completion_reason": "Done with warnings"})
    for index in range(30):
        append_jsonl(data_dir / "logs" / "chat.jsonl", {
            "direction": "in", "chat_id": 1, "user_id": 1, "ts": f"2026-08-22T12:{index + 1:02d}:00+00:00",
            "client_message_id": f"after-{index}", "text": f"A later Main note {index}."})
    # WebKit, like Safari, leaves links out of plain Tab; Alt+Tab includes them.
    tab = "Alt+Tab" if engine == "webkit" else "Tab"
    facts = """row => {
        const m = row.querySelector('.message'), clip = m.getBoundingClientRect(), a = document.activeElement;
        const tail = [...m.querySelectorAll('code')].find(c => c.textContent === 'TAIL_MARKER').getBoundingClientRect();
        const t = row.querySelector('[data-answer-fold]');
        return {expanded: row.classList.contains('is-expanded'), folded: row.classList.contains('is-folded'),
                whole: m.scrollHeight <= m.clientHeight + 1, tailShown: tail.bottom <= clip.bottom + 1,
                label: t.textContent, aria: t.getAttribute('aria-expanded'), hidden: t.hidden,
                stops: [...m.querySelectorAll('a[href], button, [tabindex]')].filter(n => n.tabIndex >= 0).length,
                focusInMessage: m.contains(a), reasonOutside: !m.contains(row.querySelector('.project-answer-limitation')),
                filesOutside: [...row.querySelectorAll(':scope > .system-message-actions > button')].some(b => b.textContent === 'Files')
                    && !m.querySelector('[data-answer-fold]')};
    }"""
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900}, has_touch=width < 980)
        page.add_init_script("""(() => { window.__copied = [];
            Object.defineProperty(navigator, 'clipboard', {configurable: true,
                value: {writeText: async text => { window.__copied.push(text); }}}); })()""")
        try:
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded", timeout=30_000)
            _wait_status(page, "Online", timeout=30_000)
            page.locator("#chat-messages .chat-bubble.project-answer.is-folded").wait_for(state="attached", timeout=30_000)
            row = page.locator(f'#chat-messages .chat-bubble.project-answer[data-task-id="root-show-{engine}-{width}"]')
            row.scroll_into_view_if_needed()
            toggle = row.locator("[data-answer-fold]")
            toggle.wait_for(state="visible", timeout=10_000)
            collapsed = row.evaluate(facts)
            assert collapsed["label"] == "Show full" and collapsed["aria"] == "false", collapsed
            assert not collapsed["whole"] and not collapsed["tailShown"] and collapsed["stops"] == 0, collapsed
            assert collapsed["reasonOutside"] and collapsed["filesOutside"], collapsed
            row.screenshot(path=str(evidence / "collapsed.png"))

            # Keyboard: no stop inside the clipped answer; the toggle is reachable and opens it in place.
            row.locator(".sender").click()
            seen = []
            for _ in range(8):
                page.keyboard.press(tab)
                seen.append(page.evaluate("() => document.activeElement?.dataset?.answerFold !== undefined"))
                assert not row.evaluate(facts)["focusInMessage"], "focus reached clipped text"
                if seen[-1]:
                    break
            assert seen[-1], "the Show full control is not reachable by keyboard"
            page.keyboard.press("Enter")
            expanded = row.evaluate(facts)
            assert expanded["expanded"] and not expanded["folded"] and expanded["whole"], expanded
            assert expanded["label"] == "Show less" and expanded["aria"] == "true" and expanded["stops"] >= 1, expanded
            page.evaluate("() => document.activeElement.blur()")
            row.locator(".sender").click()
            for _ in range(8):
                page.keyboard.press(tab)
                if page.evaluate("() => document.activeElement?.textContent === 'deep link'"):
                    break
            assert page.evaluate("() => document.activeElement?.textContent === 'deep link'")
            row.screenshot(path=str(evidence / "expanded-deep-link-focused.png"))

            # Pointer or touch folds it again; the control stays under the pointer. Center it first:
            # a control left under the composer would make the click itself scroll.
            toggle.evaluate("t => t.scrollIntoView({block: 'center'})")
            before = toggle.bounding_box()
            (toggle.tap if width < 980 else toggle.click)()
            refolded = row.evaluate(facts)
            assert refolded["folded"] and not refolded["expanded"] and refolded["stops"] == 0, refolded
            assert refolded["label"] == "Show full" and refolded["aria"] == "false", refolded
            after = toggle.bounding_box()
            assert after and abs(after["y"] - before["y"]) <= 4, (before, after)
            assert page.evaluate("() => { const r = document.querySelector('[data-answer-fold]').getBoundingClientRect();"
                                 " return r.top >= 0 && r.bottom <= innerHeight; }")
            (toggle.tap if width < 980 else toggle.click)()
            assert row.evaluate(facts)["whole"]

            # Copy copies the answer alone, never the control or the reason.
            row.locator(".chat-message-copy").click()
            page.wait_for_function("() => window.__copied.length === 1")
            assert page.evaluate("() => window.__copied[0]") == answer
            page.screenshot(path=str(evidence / "expanded-page.png"))

            # A reload starts folded again.
            page.reload(wait_until="domcontentloaded")
            _wait_status(page, "Online", timeout=30_000)
            page.locator("#chat-messages .chat-bubble.project-answer.is-folded").wait_for(state="attached", timeout=30_000)
            row.scroll_into_view_if_needed()
            reloaded = row.evaluate(facts)
            assert not reloaded["expanded"] and reloaded["label"] == "Show full" and reloaded["stops"] == 0, reloaded
            row.screenshot(path=str(evidence / "reloaded-collapsed.png"))

            # A stop added after the last measure (a late Markdown region): keyboard focus opens the answer.
            row.evaluate("""r => { const late = document.createElement('a'); late.href = '#late';
                late.textContent = 'late stop'; r.querySelector('.message').append(late); }""")
            row.locator(".sender").click()
            page.keyboard.press(tab)
            assert page.evaluate("() => document.activeElement?.textContent === 'late stop'")
            opened = row.evaluate(facts)
            assert opened["expanded"] and opened["whole"] and opened["label"] == "Show less", opened
        finally:
            browser.close()
