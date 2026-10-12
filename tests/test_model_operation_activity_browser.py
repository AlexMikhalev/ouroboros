"""Real Main and Project components display model facts on desktop and touch."""
from copy import deepcopy

import pytest

from tests.test_project_work_card_browser import project_chat as project_chat, _evidence_dir

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("chat_id", [1, 7], ids=["main", "project"])
def test_model_phase_card_header_reconnect_and_terminal(project_chat, chat_id, tmp_path):
    ui, page, expect = project_chat, project_chat["page"], project_chat["expect"]
    ui["rows"].clear()
    row = {"activity_id": "phase-task", "chat_id": chat_id, "project_id": "project" if chat_id == 7 else "",
           "client_message_id": "message", "kind": "direct_chat", "phase": "preparing", "task_attempt": 1}
    ui["state"]["active_chat_activities"] = [row]
    ui["rows"].append({"task_id": row["activity_id"], "chat_id": chat_id, "role": "assistant",
        "is_progress": True, "narration": True, "text": "Reading the requested sources.",
        "ts": "2026-10-11T00:00:00Z", "history_id": "progress:phase-task"})
    page.reload()
    page.wait_for_selector("#chat-input")
    badge = page.locator("#chat-status")
    card = page.locator('.chat-live-card[data-task-id="phase-task"]')
    expect(badge).to_have_text("Preparing request")
    card.wait_for(state="visible")
    chip = card.locator("[data-live-phase]")
    suffix = f"{ui['engine']}-{ui['theme']}-{ui['width']}-{chat_id}"

    def plain(phase, text):
        row.pop("model_activity", None)
        row["phase"] = phase
        page.evaluate("state => window.chat.hydrateStateSnapshot(state)", ui["state"])
        expect(badge).to_have_text(text)
        expect(chip).to_have_text(text)

    plain("working", "Working")
    plain("thinking", "Thinking")
    plain("working", "Working")
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"phase-neutral-{suffix}.png"), animations="disabled")

    def show(phase, text):
        row.update(phase="working", model_activity={"phase": phase, "operation_id": "same-operation",
                   "invocation_id": "same-send", "llm_call_id": "call", "task_attempt": 1})
        page.evaluate("state => window.chat.hydrateStateSnapshot(state)", ui["state"])
        expect(badge).to_have_text(text)
        expect(chip).to_have_text(text)
        assert card.locator("[data-wait-id]").count() == 0, "queue admission is not a quota/auth wait"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert card.evaluate("el => el.scrollWidth <= el.clientWidth + 1")

    show("queued", "Request accepted · waiting for admission")
    expect(chip).to_have_attribute("data-motion", "0")
    pausing = {"activity_id": "settling-other", "chat_id": chat_id, "kind": "managed_task", "phase": "budget_pausing"}
    ui["state"]["active_chat_activities"].append(pausing)
    page.evaluate("state => window.chat.hydrateStateSnapshot(state)", ui["state"])
    expect(badge).to_have_text("Pausing…")
    expect(chip).to_have_text("Request accepted · waiting for admission")
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"phase-pausing-{suffix}.png"), animations="disabled")
    ui["state"]["active_chat_activities"].remove(pausing)
    page.evaluate("state => window.chat.hydrateStateSnapshot(state)", ui["state"])
    expect(badge).to_have_text("Request accepted · waiting for admission")
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"phase-queued-{suffix}.png"), animations="disabled")
    # A cold reconnect recovers the current census, without any fresh typing frame.
    page.reload()
    expect(badge).to_have_text("Request accepted · waiting for admission")
    expect(chip).to_have_text("Request accepted · waiting for admission")
    row["model_activity"]["blockers"] = [{"kind": "global_limit"}]
    page.evaluate("state => window.chat.hydrateStateSnapshot(state)", ui["state"])
    expect(badge).to_have_text("Request accepted · waiting for admission · shared capacity limit")
    expect(chip).to_have_text("Request accepted · waiting for admission · shared capacity limit")
    page.evaluate("chatId => window.emit('log', {data: {type: 'owner_hurry', phase: 'applied',"
        "task_id: 'phase-task', chat_id: chatId, is_progress: false, ts: '2026-10-11T00:00:00Z'}})", chat_id)
    hurry = card.locator('[data-live-hurry]')
    expect(hurry).to_be_visible()
    expect(hurry).to_have_attribute('aria-label', 'Owner hurry applied')
    assert hurry.locator('svg').count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert badge.evaluate("el => el.getBoundingClientRect().width <= innerWidth")
    assert card.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"phase-capacity-{suffix}.png"), animations="disabled")
    show("dispatching", "Preparing request dispatch")
    show("awaiting_response", "Sending request / waiting for response")
    assert "Thinking" not in badge.inner_text()
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"phase-awaiting-{suffix}.png"), animations="disabled")
    show("rejoining", "Rejoining the same model operation")
    expect(chip).to_have_attribute("data-motion", "0")
    show("response_ready", "Response available")
    show("response_received", "Response received · processing")
    stale = deepcopy(ui["state"])
    plain("working", "Working")
    page.evaluate("chatId => window.emit('chat', {chat_id: chatId, task_id: 'phase-task', role: 'assistant',"
        "content: 'Finished the requested work.', system_type: 'task_summary', task_terminal_status: 'completed',"
        "ts: '2026-10-11T00:00:01Z'})", chat_id)
    page.evaluate("state => window.chat.hydrateStateSnapshot(state)", stale)
    expect(chip).to_have_text("Done")
    assert badge.inner_text() != "Response received · processing"
    expect(chip).to_have_attribute("data-motion", "0")
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"phase-terminal-{suffix}.png"), animations="disabled")
