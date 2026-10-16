"""One work, one card in Main, over static gateway data in real engines (no runtime): a Project row binds the work's
own card as its portal (never a second entry), Turn into project keeps the card, the row menu opens by pointer,
keyboard and touch, and a reply sends only the quoted row's address."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import pytest

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
WEB = Path(__file__).resolve().parents[1] / "web"
BOOT = """<script type="module">
import {createChatInstance} from '/static/modules/chat.js';
const handlers=new Map();let connected=true;let coined=0;window.sentFrames=[];
const ws={ws:{readyState:1},on(type,fn){const rows=handlers.get(type)||[];rows.push(fn);handlers.set(type,rows);return()=>{};},
    isConnected:()=>connected,send(frame){const id=frame.client_message_id||`cm-${++coined}`;
        window.sentFrames.push({...frame,client_message_id:id});return {status:'sent',clientMessageId:id};}};
window.emit=(type,row)=>(handlers.get(type)||[]).forEach(fn=>fn(row));
window.connection=value=>{connected=value;window.emit(value?'open':'close');};
window.chat=createChatInstance({ws,state:{activePage:'chat',projectChatIds:new Set([7,8]),unreadCount:0},
    updateUnreadBadge(){},openSettingsTab(){},openDashboardTab(){},chatId:1,
    mountEl:document.getElementById('content'),stateSnapshots:{begin:()=>({generation:1,requestedAt:Date.now()}),
    gate(){return Promise.resolve(this.begin());},isCurrent:()=>true,
    apply(request,data){window.chat?.hydrateStateSnapshot(data);}}});
document.getElementById('reconnect-overlay').hidden=true;
document.getElementById('reconnect-overlay').style.display='none';
window.addEventListener('ouro:open-project',event=>{window.openedProject=event.detail;});
</script>"""


@pytest.fixture(params=[
    ("chromium", "light", 1360), ("chromium", "dark", 390),
    ("webkit", "light", 390), ("webkit", "dark", 1360),
], ids=["chromium-light-desktop", "chromium-dark-mobile", "webkit-light-mobile", "webkit-dark-desktop"])
def project_chat(request):
    """A real chat instance over static gateway data (Main, or a Project panel when the test is parametrized with
    `chat_id`); `rows` is the history the page reads and `state` the /api/state answer, both mutable."""
    engine, theme, width = request.param
    if os.environ.get("OUROBOROS_RUN_UI_SMOKE") != "1":
        pytest.skip("Set OUROBOROS_RUN_UI_SMOKE=1 to run the browser flow")
    playwright = pytest.importorskip("playwright.sync_api")
    rows = [
        {"role": "user", "text": "Audit the review runner and keep it as a project", "chat_id": 1, "source": "web",
         "ts": "2026-10-08T13:57:00Z", "history_id": "h-0", "row_address": "row:1@2026-10-08T13:57:00Z#aaaaaaaaaaaa"},
        {"role": "assistant", "text": "I will audit the review runner in its own room.", "chat_id": 1,
         "ts": "2026-10-08T13:57:20Z", "history_id": "h-1", "row_address": "row:1@2026-10-08T13:57:20Z#bbbbbbbbbbbb"},
        {"role": "system", "system_type": "project_started", "chat_id": 1,
         "task_id": "creation", "project_id": "alpha", "project_name": "Project Alpha",
         "task_name": "Review runner › integration", "text": "Project Alpha › Review runner › integration · Started",
         "ts": "2026-10-08T13:57:33Z", "history_id": "h-2"},
        {"role": "system", "system_type": "project_handoff", "chat_id": 1,
         "task_id": "transfer", "project_id": "beta", "project_name": "Project Beta",
         "handoff_id": "transfer-beta", "task_name": "A second independent work item",
         "text": "Project Beta › A second independent work item", "ts": "2026-10-08T13:58:00Z", "history_id": "h-3"},
    ]
    state = {"supervisor_ready": True, "active_chat_activities_complete": True,
             "task_bindings": {"creation": {"project_id": "alpha", "chat_id": 7}, "transfer": {"project_id": "beta", "chat_id": 8}},
             "projects": [{"id": "alpha", "name": "Project Alpha", "chat_id": 7}, {"id": "beta", "name": "Project Beta", "chat_id": 8}],
             "active_chat_activities": [
                 {"activity_id": "creation", "project_id": "alpha", "project_name": "Project Alpha", "chat_id": 7,
                  "kind": "managed_task", "phase": "working", "started_at": 1791467853,
                  "last_line": "Comparing the reviewer verdicts across the last three runs.",
                  "last_line_at": "2026-10-08T14:05:00Z", "tool_calls": 41, "cancelable": True},
                 {"activity_id": "transfer", "project_id": "beta", "chat_id": 8, "kind": "managed_task",
                  "phase": "budget_paused", "pause_cause": "owner", "started_at": 1791467880},
             ]}
    conversions, details, requests = {}, {}, []
    history = {"messages": rows, "progress": [], "has_more": False, "page_cursor": "work-cards",
               "next_cursor": None, "window": {"complete": True}, "coverage": {
                   "v": 1, "view": "work", "upper": {"chat": len(rows), "progress": 0}, "spans": {
                       "chat": {"from": 0, "to": len(rows), "chain": "work", "gaps": []}, "progress": None}}}
    chat_id = request.node.callspec.params.get("chat_id", 1)
    boot = BOOT.replace("chatId:1,", f"chatId:{chat_id},asPanel:{str(chat_id != 1).lower()},")
    html = (WEB / "index.html").read_text(encoding="utf-8").replace(
        '<script type="module" src="/static/app.js"></script>', boot)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return
            self.path = self.path.removeprefix("/static")
            super().do_GET()

    def respond(route):
        path = urlparse(route.request.url).path
        data = history if path == "/api/chat/history" else state if path == "/api/state" else {}
        if path == "/api/projects/from-task":
            payload = route.request.post_data_json
            requests.append(payload)
            data = conversions[payload["task_id"]]
        if path.startswith("/api/tasks/"):
            task_id = path.split("/")[-1]
            data = details.get(task_id, {"task_id": task_id, "status": "running"})
        route.fulfill(content_type="application/json", body=json.dumps(data))

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": width, "height": 900}, has_touch=width < 980, timezone_id="UTC")
                page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)});")
                page.add_init_script("""Object.defineProperty(navigator, 'clipboard', {
                    value: {writeText: async text => {window.copiedText = text;}}});""")
                page.route("**/api/**", respond)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                if chat_id == 1:
                    page.wait_for_function("() => window.chat && document.querySelector('#chat-messages .chat-bubble')")
                    page.evaluate("s=>chat.hydrateStateSnapshot(s)", state)
                yield {"page": page, "expect": playwright.expect, "rows": rows, "state": state, "conversions": conversions,
                       "details": details, "requests": requests, "engine": engine, "theme": theme, "width": width}
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _evidence_dir(tmp_path):
    path = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _evidence(tmp_path, chat, name):
    return str(_evidence_dir(tmp_path) / f"{name}-{chat['engine']}-{chat['theme']}-{chat['width']}.png")


def _card(page, task_id):
    return page.locator(f'#chat-messages > .chat-live-card[data-task-id="{task_id}"]')


def test_project_rows_bind_the_works_own_cards_never_an_entry(project_chat, tmp_path):
    page, expect = project_chat["page"], project_chat["expect"]
    creation, transfer = _card(page, "creation"), _card(page, "transfer")
    for card, name in ((creation, "Project Alpha"), (transfer, "Project Beta")):
        expect(card).to_have_attribute("data-portal", "1")
        footer = card.locator(':scope > .chat-live-bound-pointer')
        expect(footer).to_have_attribute("aria-label", f"Open project {name}")
        assert card.evaluate("c => c.lastElementChild.classList.contains('chat-live-bound-pointer')"), "the footer closes the card"
    assert page.locator('.project-handoff, [data-system-type="project_started"], [data-system-type="project_handoff"]').count() == 0
    # The room's present: chip, the latest line, tool calls and time; [Reply] [Stop…] while it runs.
    expect(creation.locator('.chat-live-phase').first).to_have_text("Working")
    expect(creation.locator('[data-live-activity]')).to_contain_text("Comparing the reviewer verdicts")
    expect(creation.locator('[data-live-meta]')).to_contain_text("41 tool calls")
    expect(creation.locator('.chat-live-actions > button')).to_have_text(["Reply", "Stop…"])
    # A paused work: Resume is the one primary, beside Reply and Stop….
    expect(transfer.locator('.chat-live-actions > button')).to_have_text(["Reply", "Resume", "Stop…"])
    assert transfer.locator('.chat-live-actions > .btn-primary').all_text_contents() == ["Resume"]
    # The summary is reading content in a portal: no disclosure, no stop.
    assert creation.locator(':scope > [data-live-summary-button]').evaluate("e=>['role','tabindex','aria-expanded'].map(n=>e.getAttribute(n))") == [None, None, None]
    geometry = creation.evaluate("""card => { const c = card.getBoundingClientRect();
        const box = (sel) => { const r = card.querySelector(sel).getBoundingClientRect(); return {l: r.left - c.left, r: c.right - r.right, h: r.height}; };
        return {overflow: card.scrollWidth > card.clientWidth + 1, footer: box(':scope > .chat-live-bound-pointer'),
            buttons: [...card.querySelectorAll('.chat-live-actions > button')].map((b) => b.getBoundingClientRect().height)}; }""")
    assert not geometry["overflow"]
    assert geometry["footer"]["l"] <= 1 and geometry["footer"]["r"] <= 1, "the footer strip spans the card"
    assert geometry["footer"]["h"] >= 36, "a full pointer target"
    assert min(geometry["buttons"]) >= 24
    creation.locator(':scope > .chat-live-bound-pointer').click()
    assert page.evaluate("[openedProject.project.id, openedProject.task_id]") == ["alpha", "creation"]
    creation.screenshot(path=_evidence(tmp_path, project_chat, "portal"), animations="disabled")


def test_turn_into_project_keeps_the_card_as_its_portal(project_chat):
    page, expect = project_chat["page"], project_chat["expect"]
    page.evaluate("""() => {
        emit('chat', {chat_id: 1, task_id: 'local', role: 'assistant', is_progress: true, narration: true,
            content: 'Inspecting the requested work', ts: '2026-10-08T14:10:00Z'});
        emit('task_named', {task_id: 'local', suggested_name: 'Inspect the runner'});
    }""")
    card = _card(page, "local")
    state = project_chat["state"]
    state["active_chat_activities"].append({"activity_id": "local", "chat_id": 1, "kind": "direct_chat", "phase": "working",
                                            "cancelable": True})
    page.evaluate("s=>chat.hydrateStateSnapshot(s)", state)
    expect(card.locator('[data-turn-into-project]')).to_have_count(1)
    node = card.element_handle()
    project_chat["conversions"]["local"] = {"project": {"id": "gamma", "name": "Project Gamma", "chat_id": 9},
                                         "binding": {"project_id": "gamma"}, "handoff_receipt": "durable"}
    card.locator('[data-turn-into-project]').click()
    expect(card).to_have_attribute("data-portal", "1")
    assert page.evaluate("n => n.isConnected && n === document.querySelector('[data-task-id=\"local\"]')", node), "the SAME node"
    expect(card.locator('[data-turn-into-project]')).to_have_count(0)
    expect(card.locator(':scope > .chat-live-bound-pointer')).to_have_attribute("aria-label", "Open project Project Gamma")
    assert project_chat["requests"][-1]["task_id"] == "local"


def test_the_row_menu_opens_by_pointer_keyboard_and_touch_and_gives_focus_back(project_chat, tmp_path):
    page, expect = project_chat["page"], project_chat["expect"]
    card = _card(page, "creation")
    menu = page.locator('.chat-row-menu')
    card.locator('[data-live-title]').click(button="right")
    expect(menu.locator('[role="menuitem"]')).to_have_text(["Reply", "Copy", "Open project", "Pause", "Stop…"])
    expect(menu.locator('[role="menuitem"]').first).to_be_focused()
    page.screenshot(path=_evidence(tmp_path, project_chat, "row-menu-card"), animations="disabled")
    page.keyboard.press("ArrowDown")
    expect(menu.locator('[role="menuitem"]').nth(1)).to_be_focused()
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    assert page.evaluate("() => document.activeElement === document.querySelector('[data-task-id=\"creation\"]')")
    # Keyboard: Shift+F10 on the answer's focused Copy.
    answer = page.locator('#chat-messages > .chat-bubble.assistant').first
    answer.locator('.chat-message-copy').focus()
    page.keyboard.press("Shift+F10")
    expect(menu.locator('[role="menuitem"]')).to_have_text(["Reply", "Copy"])
    page.keyboard.press("Escape")
    expect(answer.locator('.chat-message-copy')).to_be_focused()
    # Touch: a held finger (no movement) opens it; the lifting finger presses nothing.
    box = answer.locator('.message').bounding_box()
    point = {"x": box["x"] + 20, "y": box["y"] + 8}
    page.evaluate("""p => { const t = document.elementFromPoint(p.x, p.y);
        t.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerType: 'touch', isPrimary: true, pointerId: 7,
            clientX: p.x, clientY: p.y})); }""", point)
    page.wait_for_timeout(600)
    expect(menu.locator('[role="menuitem"]')).to_have_count(2)
    # Where the finger lifts the menu now lies (it opened at the finger): the item under it is not pressed.
    page.evaluate("""p => { const t = document.elementFromPoint(p.x + 12, p.y + 12);
        document.querySelector('[data-task-id]')?.closest('#chat-messages')
            .dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerType: 'touch', pointerId: 7}));
        t.click(); }""", point)
    expect(menu).to_have_count(1)
    assert page.evaluate("() => document.querySelectorAll('.chat-quote--composer').length") == 0, "the lift pressed no item"
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)


def test_a_reply_sends_only_the_quoted_rows_address(project_chat, tmp_path):
    page, expect = project_chat["page"], project_chat["expect"]
    answer = page.locator('#chat-messages > .chat-bubble.assistant').first
    answer.locator('.chat-message-reply').click()
    strip = page.locator('.chat-quote--composer')
    expect(strip).to_have_count(1)
    expect(strip.locator('.chat-quote-author')).to_have_text("Ouroboros")
    expect(page.locator('#chat-input')).to_be_focused()
    page.keyboard.press("Escape")
    expect(strip).to_have_count(0)
    answer.locator('.chat-message-reply').click()
    page.screenshot(path=_evidence(tmp_path, project_chat, "reply-strip"), animations="disabled")
    page.fill('#chat-input', "Which runs disagree?")
    page.locator('#chat-send').click()
    frame = page.evaluate("() => sentFrames.at(-1)")
    assert frame["reply_to"] == "row:1@2026-10-08T13:57:20Z#bbbbbbbbbbbb"
    assert frame["content"] == "Which runs disagree?"
    expect(strip).to_have_count(0)
    echo = page.locator(f'.chat-bubble.user[data-client-message-id="{frame["client_message_id"]}"]')
    expect(echo.locator('.chat-quote .chat-quote-author')).to_have_text("Ouroboros")
    # The work card's own Reply quotes the work by its task address.
    _card(page, "creation").locator('[data-card-reply]').click()
    page.fill('#chat-input', "Stop after the third run")
    page.locator('#chat-send').click()
    assert page.evaluate("() => sentFrames.at(-1).reply_to") == "task:creation"


SUCCESSOR = "creation-cnext"


def _interrupted(**extra):
    return {"task_id": "creation", "status": "failed", "reason_code": "model_provider_failed",
            "terminal_time": {"occurred_at": "2026-10-08T14:09:00Z", "attempt": {"started_at": "2026-10-08T13:57:33Z"}},
            **extra}


def _continued():
    binding = {"project_id": "alpha", "chat_id": 7}
    return _interrupted(
        continued_by={"state": "admitted", "successor_task_id": SUCCESSOR, "action_nonce": "n-1", "binding": binding},
        continuation_offer={"eligible": False, "refusal": "already_continued", "state": "admitted",
                            "successor_task_id": SUCCESSOR})


def _room_continue(chat):
    """The owner pressed Continue in Project Alpha's room: the predecessor's record names its admitted successor and
    the all-chat census lists that successor in the room (Main receives none of its frames)."""
    chat["details"]["creation"] = _continued()
    state = chat["state"]
    state["active_chat_activities"] = [row for row in state["active_chat_activities"] if row["activity_id"] != "creation"]
    state["active_chat_activities"].append({
        "activity_id": SUCCESSOR, "project_id": "alpha", "project_name": "Project Alpha", "chat_id": 7,
        "kind": "managed_task", "phase": "working", "started_at": 1791468000, "last_line": "Continuing the audit",
        "last_line_at": "2026-10-08T14:12:00Z", "tool_calls": 3, "cancelable": True})
    return state


def _visible_cards(page):
    return page.evaluate("""() => [...document.querySelectorAll('#chat-messages > .chat-live-card')]
        .filter((card) => card.getClientRects().length).map((card) => card.dataset.taskId)""")


@pytest.mark.parametrize("journey", ["continued-later", "same-snapshot", "reload"])
def test_main_portal_follows_a_continue_made_in_the_project_room(project_chat, journey, tmp_path):
    """Owner QB = A and 16.10 Q1 = A: a Main request moved into Project Alpha, interrupted, then continued in the room.
    Main's ONE card for that work shows the admitted successor as its current episode (chip, line, meta, its Stop…),
    keeps the work's place, first name and Project footer, and folds the ended episode into one meta line: live
    (the census arrives while Main is open) and after a Main reload."""
    page, expect = project_chat["page"], project_chat["expect"]
    creation = _card(page, "creation")
    expect(creation.locator('.chat-live-phase').first).to_have_text("Working")
    assert _visible_cards(page) == ["creation", "transfer"]
    if journey == "continued-later":
        # The work fails first: Main reads its record and offers Continue on the same card.
        project_chat["details"]["creation"] = _interrupted(
            continuation_offer={"eligible": True, "cause": "model_provider_failed"})
        state = project_chat["state"]
        state["active_chat_activities"] = [row for row in state["active_chat_activities"] if row["activity_id"] != "creation"]
        page.evaluate("s => chat.hydrateStateSnapshot(s)", state)
        expect(creation.locator('.chat-live-phase').first).to_have_text("Failed", timeout=15000)
        expect(creation.locator('[data-continue-task]')).to_have_text("Continue")
    state = _room_continue(project_chat)
    if journey == "reload":
        page.reload()
        page.wait_for_function("() => window.chat && document.querySelector('#chat-messages .chat-bubble')")
    page.evaluate("s => chat.hydrateStateSnapshot(s)", state)
    card = _card(page, SUCCESSOR)
    expect(card).to_be_visible(timeout=15000)
    expect(_card(page, "creation")).to_be_hidden()
    assert _visible_cards(page) == [SUCCESSOR, "transfer"], "one card for the work, at its first place"
    expect(card).to_have_attribute("data-portal", "1")
    expect(card.locator('[data-live-title]')).to_have_text("Review runner › integration")
    expect(card.locator('.chat-live-phase').first).to_have_text("Working")
    expect(card.locator('[data-live-activity]')).to_have_text("Continuing the audit")
    expect(card.locator('[data-live-meta]')).to_contain_text("3 tool calls")
    expect(card.locator('.chat-live-actions > button')).to_have_text(["Reply", "Stop…"])
    footer = card.locator(':scope > .chat-live-bound-pointer')
    expect(footer).to_have_attribute("aria-label", "Open project Project Alpha")
    assert card.evaluate("c => c.lastElementChild.classList.contains('chat-live-bound-pointer')"), "the footer closes the card"
    episodes = card.locator('.chat-live-episode')
    expect(episodes).to_have_count(2)
    expect(episodes.nth(0)).to_contain_text("Episode 1 · 13:57–14:09 · Failed")
    expect(episodes.nth(1)).to_have_text("Episode 2 · since 14:00")
    expect(episodes.nth(1)).to_have_attribute("data-current", "1")
    assert page.locator('#chat-messages [data-continue-task]:visible').count() == 0, "no Continued stands as its action"
    assert page.locator('.project-handoff, [data-system-type="project_started"], [data-system-type="project_handoff"]').count() == 0
    card.screenshot(path=_evidence(tmp_path, project_chat, f"continue-{journey}"), animations="disabled")
