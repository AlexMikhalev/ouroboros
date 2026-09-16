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
                assert "Open Project ↗" in summary_text
                assert project["id"] not in summary_text

                annotation = page.locator('#chat-messages .msg-routing-annotation').filter(has_text=target_label)
                annotation.wait_for(state="visible", timeout=10_000)
                assert project["id"] not in annotation.inner_text()
                main_text = page.locator("#chat-messages").inner_text()
                assert "Project progress: nested delegation is running." not in main_text
                assert "Project task summary" not in main_text

                summary.locator(".system-message-action").click()
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
                assert panel.locator(".system-message-action").count() == 0
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
