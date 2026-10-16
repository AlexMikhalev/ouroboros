"""The real app's shared state reader invalidates Main, retaining newer live questions."""
import json
import os
import re
import socket
from pathlib import Path

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data
from tests.ui_chat_viewport_smoke import (
    _CAPTURE_TEST_SOCKET, _OBSERVE_STATE_READS, _emit_ws_frame, _wait_socket_open_quiescent,
    _wait_state_reads_quiescent,
)

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_shared_app_read_failure_reaches_main_without_overwriting_newer_question(direct_server_with_data, engine):
    from playwright.sync_api import expect, sync_playwright

    server = direct_server_with_data
    evidence = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or server["data_dir"].parent) / engine
    evidence.mkdir(parents=True, exist_ok=True)
    question = {"task_id": "question-task", "quiz_id": "cached", "quiz_state": "open",
                "project_id": "build", "project_name": "Build", "project_chat_id": 12,
                "question": "Keep this output?", "options": ["Yes", "No"], "ts": "2026-10-13T10:00:00Z"}
    newer = {**question, "quiz_id": "newer", "question": "A newer live question?"}
    census = {"current_questions": [question], "current_questions_complete": True,
              "main_attention_complete": True, "main_attention": [{"task_id": "paused-task",
              "task_name": "Paused build", "phase": "budget_paused", "can_resume": True,
              "attention_reason": "budget", "pause_cause": "budget"}]}
    held, stages, errors = [], [], []
    holding = False
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(f"({_CAPTURE_TEST_SOCKET})(); ({_OBSERVE_STATE_READS})();")
        page.add_init_script("""(() => {
            const fetch = window.fetch;
            window.__stateCallers = [];
            window.fetch = (url, init) => {
                if (String(url).endsWith('/api/state')) window.__stateCallers.push(new Error().stack);
                return fetch(url, init);
            };
        })();""")

        def state_read(route):
            if holding:
                held.append(route)
            else:
                response = route.fetch()
                route.fulfill(response=response, json={**response.json(), **census})

        page.route("**/api/state", state_read)
        # A live Main pointer validates against its canonical task before enabling
        # answers; serve that existing read too, rather than treating an unknown
        # synthetic task as a confirmed question.
        page.route("**/api/tasks/question-task", lambda route: route.fulfill(json={
            "task_id": "question-task", "project_id": "build", "question_rooms": {
                row["quiz_id"]: {"state": "known", "chat_id": 12, "project_id": "build"} for row in [question, newer]}, "owner_quiz": {
                row["quiz_id"]: {**row, "state": "open", "asked_at": row["ts"]}
                for row in [question, newer]
            },
        }))
        try:
            page.goto(server["url"], wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(state="visible")
            _wait_socket_open_quiescent(page)
            attention = page.locator(".main-attention")
            attention.locator(".main-attention-summary").click()
            resume = attention.get_by_role("button", name="Resume", exact=True)
            cached = attention.locator('.chat-quiz-card[data-quiz-id="cached"]')
            live = attention.locator('.chat-quiz-card[data-quiz-id="newer"]')
            unavailable = attention.get_by_text("Current questions and actions could not be refreshed.", exact=False)
            expect(resume).to_be_enabled()
            expect(cached).to_have_attribute("data-state", "open")
            stages.append("cached census enables Resume and question")
            page.screenshot(path=str(evidence / "before-failure.png"))
            budget = page.locator('#chat-budget-text')
            known_budget = budget.inner_text()
            holding = True
            # Exercise the real socket-open listeners while their new read is held.
            # Generation retirement alone is not a failed HTTP read.
            page.evaluate("window.__testSockets.find(s => s.readyState === WebSocket.OPEN).dispatchEvent(new Event('open'))")
            for _ in range(150):
                if held:
                    break
                page.wait_for_timeout(20)
            assert held, "socket open requested a fresh census"
            expect(unavailable).not_to_be_visible()
            expect(budget).to_have_text(known_budget)
            expect(cached).to_have_attribute('data-state', 'open')
            stages.append('healthy socket open retires generations without fabricating failure')
            holding = False
            for route in held[:]:
                response = route.fetch()
                route.fulfill(response=response, json={**response.json(), **census})
                held.remove(route)
            _wait_state_reads_quiescent(page)

            holding = True
            _emit_ws_frame(page, {"type": "projects_changed"})
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(20)
            assert len(held) == 1
            caller = page.evaluate("window.__stateCallers.at(-1)")
            assert "refreshProjectsNav" in caller and "refreshHeaderControlState" not in caller, caller
            # This frame arrived after the failed request began. Its positive fact wins.
            _emit_ws_frame(page, {**newer, "type": "chat", "chat_id": 1, "role": "system",
                                  "system_type": "project_question_pointer"})
            expect(live).to_have_attribute("data-state", "open")
            held.pop().fulfill(status=503, json={"error": "fixture shared state failure"})
            expect(unavailable).to_be_visible()
            expect(budget).to_have_text('Unavailable')
            expect(resume).to_be_disabled()
            expect(cached).to_have_attribute("data-state", "unknown")
            expect(live).to_have_attribute("data-state", "open")
            stages.append("shared failure disables cached controls, preserves newer live question")
            page.screenshot(path=str(evidence / "shared-failure.png"))

            holding = False
            census["current_questions"] = [question, newer]
            _emit_ws_frame(page, {"type": "projects_changed"})
            expect(resume).to_be_enabled()
            expect(cached).to_have_attribute("data-state", "open")
            expect(live).to_have_attribute("data-state", "open")
            expect(unavailable).not_to_be_visible()
            stages.append("new complete census restores availability")
            _wait_state_reads_quiescent(page)
            # The existing Main poll is a second producer of failed reads. Admit
            # other shared reads normally until that actual consumer is observed.
            holding = True
            chat_read = None
            for _ in range(150):
                if held:
                    route = held.pop(0)
                    caller = page.evaluate('window.__stateCallers.at(-1)')
                    if 'refreshHeaderControlState' in caller:
                        chat_read = route
                        break
                    response = route.fetch()
                    route.fulfill(response=response, json={**response.json(), **census})
                page.wait_for_timeout(100)
            assert chat_read is not None, 'real chat poll was not observed'
            chat_read.fulfill(status=503, json={'error': 'fixture chat state failure'})
            expect(budget).to_have_text('Unavailable')
            expect(unavailable).to_be_visible()
            expect(resume).to_be_disabled()
            stages.append('chat-originated failure remains unavailable rather than loading')
            assert not errors, errors
        finally:
            page.screenshot(path=str(evidence / "last-view.png"))
            (evidence / "receipt.json").write_text(json.dumps({
                "engine": engine, "stages": stages, "page_errors": errors,
                "state_callers": page.evaluate("window.__stateCallers"),
                "candidate_identity": server["candidate_identity"],
                "boundary": "Real SPA; state HTTP response and late WS frame supplied by this fixture",
            }, indent=2), encoding="utf-8")
            for route in held:
                route.abort()
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_project_panel_read_failure_reaches_main_without_overwriting_newer_question(direct_server_with_data, engine):
    """A Project panel's own failed read disables Main's cached actions at once, not at Main's next poll."""
    from playwright.sync_api import expect, sync_playwright

    from ouroboros import projects_registry as registry

    server = direct_server_with_data
    evidence = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or server["data_dir"].parent) / f"panel-{engine}"
    evidence.mkdir(parents=True, exist_ok=True)
    project = registry.create_project(server["data_dir"], "build", name="Build")
    question = {"task_id": "question-task", "quiz_id": "cached", "quiz_state": "open", "project_id": "build",
                "project_name": "Build", "project_chat_id": int(project["chat_id"]),
                "question": "Keep this output?", "options": ["Yes", "No"], "ts": "2026-10-13T10:00:00Z"}
    newer = {**question, "quiz_id": "newer", "question": "A newer live question?"}
    census = {"current_questions": [question], "current_questions_complete": True,
              "main_attention_complete": True, "main_attention": [{"task_id": "paused-task",
              "task_name": "Paused build", "phase": "budget_paused", "can_resume": True,
              "attention_reason": "budget", "pause_cause": "budget"}]}
    held, stages, errors = [], [], []
    holding = False

    def admit(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), **census})

    def wait_held(count):
        for _ in range(150):
            if len(held) >= count:
                return
            page.wait_for_timeout(20)
        raise AssertionError(f"expected {count} held state read(s), saw {len(held)}")

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(f"({_CAPTURE_TEST_SOCKET})(); ({_OBSERVE_STATE_READS})();")
        page.add_init_script("""(() => {
            const fetch = window.fetch;
            window.__stateCallers = [];
            window.fetch = (url, init) => {
                if (String(url).endsWith('/api/state')) window.__stateCallers.push(new Error().stack);
                return fetch(url, init);
            };
        })();""")
        page.route("**/api/state", lambda route: held.append(route) if holding else admit(route))
        page.route("**/api/tasks/question-task", lambda route: route.fulfill(json={
            "task_id": "question-task", "project_id": "build", "question_rooms": {
                row["quiz_id"]: {"state": "known", "chat_id": 12, "project_id": "build"} for row in [question, newer]}, "owner_quiz": {
                row["quiz_id"]: {**row, "state": "open", "asked_at": row["ts"]} for row in [question, newer]},
        }))
        try:
            page.goto(server["url"], wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(state="visible")
            _wait_socket_open_quiescent(page)
            attention = page.locator(".main-attention")
            attention.locator(".main-attention-summary").click()
            resume = attention.get_by_role("button", name="Resume", exact=True)
            cached = attention.locator('.chat-quiz-card[data-quiz-id="cached"]')
            live = attention.locator('.chat-quiz-card[data-quiz-id="newer"]')
            unavailable = attention.get_by_text("Current questions and actions could not be refreshed.", exact=False)
            budget = page.locator("#chat-budget-text")
            expect(resume).to_be_enabled()
            expect(cached).to_have_attribute("data-state", "open")
            stages.append("cached census enables Resume and question")
            _wait_state_reads_quiescent(page)

            # Hold a shared read so Main's periodic poll joins it instead of reading;
            # the panel's forced mount read then owns the one coalesced follow-up.
            holding = True
            _emit_ws_frame(page, {"type": "projects_changed"})
            wait_held(1)
            assert "refreshProjectsNav" in page.evaluate("window.__stateCallers.at(-1)")
            page.locator('.nav-project-row[data-project-id="build"]').click()
            page.locator("#panel-pchat-build").wait_for(state="attached")
            admit(held.pop(0))
            wait_held(1)
            caller = page.evaluate("window.__stateCallers.at(-1)")
            assert "refreshHeaderControlState" in caller and "refreshProjectsNav" not in caller, caller
            expect(resume).to_be_enabled()
            # This frame arrived after the failed request began. Its positive fact wins.
            _emit_ws_frame(page, {**newer, "type": "chat", "chat_id": 1, "role": "system",
                                  "system_type": "project_question_pointer"})
            expect(live).to_have_attribute("data-state", "open")
            held.pop(0).fulfill(status=503, json={"error": "fixture Project panel state failure"})
            expect(unavailable).to_be_visible()
            expect(budget).to_have_text("Unavailable")
            expect(resume).to_be_disabled()
            expect(cached).to_have_attribute("data-state", "unknown")
            expect(live).to_have_attribute("data-state", "open")
            stages.append("Project-panel failure disables Main's cached actions at once, keeps newer live question")
            page.screenshot(path=str(evidence / "panel-failure.png"))

            holding = False
            census["current_questions"] = [question, newer]
            _emit_ws_frame(page, {"type": "projects_changed"})
            expect(resume).to_be_enabled()
            expect(cached).to_have_attribute("data-state", "open")
            expect(unavailable).not_to_be_visible()
            stages.append("new complete census restores availability")
            assert not errors, errors
        finally:
            page.screenshot(path=str(evidence / "last-view.png"))
            (evidence / "receipt.json").write_text(json.dumps({
                "engine": engine, "stages": stages, "page_errors": errors,
                "state_callers": page.evaluate("window.__stateCallers"),
                "candidate_identity": server["candidate_identity"],
                "boundary": "Real SPA and Project panel; state HTTP response and late WS frame supplied by this fixture",
            }, indent=2), encoding="utf-8")
            for route in held:
                route.abort()
            browser.close()


# While `__refuseSockets` is set, a new socket dials a closed loopback port: a genuine refused
# connection the app's own reconnect logic handles. Every /api/state read is tagged with its caller
# so the fixture holds only the shared reader's census, never ws.js's served-page decision.
_REAL_SOCKET_DROP = """(refusedUrl) => {
    const NativeWebSocket = window.WebSocket;
    window.__refuseSockets = false;
    window.__socketRefusals = 0;
    window.WebSocket = class FixtureWebSocket extends NativeWebSocket {
        constructor(url, ...rest) {
            const refused = window.__refuseSockets;
            super(refused ? refusedUrl : url, ...rest);
            if (refused) window.__socketRefusals += 1;
        }
    };
    const fetch = window.fetch;
    window.__stateReads = [];
    window.fetch = (input, init) => {
        const raw = typeof input === 'string' ? input : input?.url || '';
        if (new URL(raw, location.href).pathname !== '/api/state') return fetch(input, init);
        const stack = new Error().stack || '';
        const read = { id: window.__stateReads.length + 1, served: stack.includes('/modules/ws.js'), stack };
        window.__stateReads.push(read);
        const headers = new Headers(init?.headers || {});
        headers.set('x-fixture-state-read', `${read.id}:${read.served ? 'served' : 'shared'}`);
        return fetch(input, { ...(init || {}), headers });
    };
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_genuine_socket_close_withdraws_main_authority_until_a_fresh_census(direct_server_with_data, engine):
    """A real close disables Resume/Stop and marks cached questions unknown, keeping drafts and settled
    rows; a complete read that HTTP still answers offline (its waiting activity's question pointer
    included), reopening alone and a question read begun before the drop restore nothing; the
    post-open census does, without overwriting a newer live question that arrived after that census
    was requested and validated by its own read."""
    from playwright.sync_api import expect, sync_playwright

    from supervisor.message_bus import log_chat

    server = direct_server_with_data
    with socket.socket() as probe:  # a loopback port nothing listens on once closed
        probe.bind(("127.0.0.1", 0))
        refused_url = f"ws://127.0.0.1:{probe.getsockname()[1]}/ws"
    evidence = Path(os.environ.get("OUROBOROS_BROWSER_EVIDENCE_OUT") or server["data_dir"].parent) / f"close-{engine}"
    evidence.mkdir(parents=True, exist_ok=True)
    settled_text = "Settled result: the quarterly report is ready."
    log_chat("out", 1, 1, settled_text, task_id="settled-task", drive_root=server["data_dir"], require_write=True)
    question = {"task_id": "question-task", "quiz_id": "cached", "quiz_state": "open",
                "project_id": "build", "project_name": "Build", "project_chat_id": 12,
                "question": "Keep this output?", "options": ["Yes", "No"], "ts": "2026-10-13T10:00:00Z"}
    newer = {**question, "quiz_id": "newer", "question": "A newer live question?"}
    inflight = {**question, "task_id": "inflight-task", "quiz_id": "inflight", "question": "Asked just before the drop?"}
    # The waiting task's activity carries its question pointer too (gateway/state.py `required_question`).
    census = {"current_questions": [question], "current_questions_complete": True,
              "main_attention_complete": True, "main_attention": [{"task_id": "paused-task",
              "task_name": "Paused build", "phase": "budget_paused", "can_resume": True, "cancelable": True,
              "attention_reason": "budget", "pause_cause": "budget"}],
              "active_chat_activities_complete": True, "active_chat_activities": [{
                  "activity_id": "question-task", "chat_id": 12, "project_id": "build",
                  "required_question": {**question, "owner_wait_state": "waiting"}}]}
    held, stages, errors, released, offline_reads, views, details = [], [], [], [], [], {}, []
    holding = False

    def admit(route, body=None):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), **(body or census)})

    def state_read(route):
        tag = route.request.headers.get("x-fixture-state-read", "")
        if holding and not tag.endswith(":served"):
            held.append((int(tag.split(":")[0] or 0), route))
        else:
            admit(route)

    def release(rows, body):
        for read_id, route in rows:
            admit(route, body)
            held.remove((read_id, route))
            released.append(read_id)

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("window.__windowErrors = []; addEventListener('error', e => window.__windowErrors.push(String(e.message)));")
        page.add_init_script(f"({_OBSERVE_STATE_READS})(); ({_REAL_SOCKET_DROP})({json.dumps(refused_url)});")
        page.route("**/api/state", state_read)
        page.route("**/api/tasks/question-task", lambda route: route.fulfill(json={
            "task_id": "question-task", "project_id": "build", "question_rooms": {
                row["quiz_id"]: {"state": "known", "chat_id": 12, "project_id": "build"} for row in [question, newer]}, "owner_quiz": {
                row["quiz_id"]: {**row, "state": "open", "asked_at": row["ts"]} for row in [question, newer]},
        }))
        page.route("**/api/tasks/inflight-task", lambda route: details.append(route))  # answered after the reopen
        page.add_init_script("""(() => {
            const fetch = window.fetch;
            window.__inflightLanded = 0;  // the witness that the held read's late answer reached the app
            window.fetch = (input, init) => {
                const read = fetch(input, init);
                if (String(input?.url || input).includes('/api/tasks/inflight-task'))
                    read.then(() => { window.__inflightLanded += 1; }, () => {});
                return read;
            };
        })();""")
        try:
            page.goto(server["url"], wait_until="domcontentloaded")
            page.locator("#chat-input").wait_for(state="visible")
            page.wait_for_function("() => window.__ouroWs?.isConnected()", timeout=30_000)
            _wait_state_reads_quiescent(page)
            served_sha = str(page.request.get(server["url"] + "/api/state").json().get("sha") or "")
            if served_sha:  # a drop before the served SHA is known would reload the page (ws.js decide)
                page.wait_for_function("sha => window.__ouroWs?._lastSha === sha", arg=served_sha, timeout=30_000)
            attention = page.locator(".main-attention")
            attention.locator(".main-attention-summary").click()
            resume = attention.get_by_role("button", name="Resume", exact=True)
            stop = attention.get_by_role("button", name="Stop…", exact=True)
            cached = attention.locator('.chat-quiz-card[data-quiz-id="cached"]')
            live = attention.locator('.chat-quiz-card[data-quiz-id="newer"]')
            pending = attention.locator('.chat-quiz-card[data-quiz-id="inflight"]')
            unavailable = attention.get_by_text("Current questions and actions could not be refreshed.", exact=False)
            budget, status = page.locator("#chat-budget-text"), page.locator("#chat-status")
            settled = page.locator("#chat-messages").get_by_text(settled_text, exact=True)
            comment, composer = cached.locator(".chat-quiz-comment"), page.locator("#chat-input")
            expect(resume).to_be_enabled()
            expect(stop).to_be_enabled()
            expect(cached).to_have_attribute("data-state", "open")
            expect(settled).to_have_count(1)
            known_budget = budget.inner_text()
            comment.fill("Draft kept across the drop")
            composer.fill("Composer text kept across the drop")
            stages.append("cached census enables Resume, Stop and question; drafts typed")
            # Both Main forms resize the same composer container at narrow widths.
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                for _ in range(2):
                    attention.locator(".main-attention-summary").click()
                    page.wait_for_timeout(180)
                    attention.locator(".main-attention-summary").click()
                    page.wait_for_timeout(180)
                    expect(comment).to_have_value("Draft kept across the drop")
                    expect(resume).to_be_enabled()
                assert not page.evaluate("window.__windowErrors"), page.evaluate("window.__windowErrors")
                page.screenshot(path=str(evidence / f"needs-you-{width}.png"))
            stages.append("narrow question plus action disclosure has no window/ResizeObserver errors")
            page.set_viewport_size({"width": 1100, "height": 900})

            def deliver(frame):
                page.evaluate("""frame => window.__ouroWs.ws.dispatchEvent(
                    new MessageEvent('message', {data: JSON.stringify(frame)}))""",
                              {**frame, "type": "chat", "chat_id": 1, "role": "system",
                               "system_type": "project_question_pointer"})

            # A question published just before the drop: Main validates its copy by the copy's own
            # detail read, which is still in flight when the socket closes.
            deliver(inflight)
            expect(pending).to_have_attribute("data-state", "unknown")
            for _ in range(250):
                if details:
                    break
                page.wait_for_timeout(20)
            assert len(details) == 1, "the copy's own read is in flight at the drop"
            stages.append("a newly delivered question's own detail read is in flight")

            def authority():
                return {"resume": resume.is_enabled(), "stop": stop.is_enabled(),
                        "question": cached.get_attribute("data-state"), "comment": comment.is_enabled(),
                        "draft": comment.input_value(), "refresh_failure": unavailable.is_visible(),
                        "in_flight": pending.get_attribute("data-state"),
                        "in_flight_answer": pending.locator(".chat-quiz-option").first.is_enabled()}
            withdrawn = {"resume": False, "stop": False, "question": "unknown", "comment": False,
                         "draft": "Draft kept across the drop", "refresh_failure": True,
                         "in_flight": "unknown", "in_flight_answer": False}

            # Reads the app requests up to its own reopen belong to retired generations. Every refused
            # redial closes again and retires older reads too: only reads requested after the LAST close
            # are current offline reads.
            page.evaluate("""() => {
                window.__closes = 0;
                window.__ouroWs.on('open', () => {
                    window.__readsAtOpen ??= window.__stateReads.length;
                    window.__closesAtOpen ??= window.__closes;
                });
                window.__ouroWs.on('close', () => {
                    window.__closes += 1;
                    window.__readsAtClose = window.__stateReads.length;
                });
            }""")
            holding = True
            page.evaluate("window.__refuseSockets = true; window.__ouroWs.ws.close()")
            expect(status).to_have_text("Reconnecting…")
            expect(page.locator("#reconnect-overlay")).to_have_class(re.compile(r"\bvisible\b"))
            expect(resume).to_be_disabled()
            expect(stop).to_be_disabled()
            expect(cached).to_have_attribute("data-state", "unknown")
            expect(comment).to_be_disabled()
            expect(comment).to_have_value("Draft kept across the drop")
            expect(composer).to_have_value("Composer text kept across the drop")
            expect(unavailable).to_be_visible()
            expect(budget).to_have_text("Unavailable")
            expect(settled).to_have_count(1)
            page.wait_for_function("() => window.__socketRefusals >= 1", timeout=10_000)
            stages.append("genuine close: controls off, question unknown, drafts and settled row kept; reconnect refused")
            page.screenshot(path=str(evidence / "closed.png"))
            # The real backoff (x1.5 per refusal) outgrows the 3 s poll and caps at 10 s. Jump to that
            # cap so the next refusal leaves a gap in which an offline read lands after the last close
            # and before the reopen: the window in which an offline body could re-arm stale controls.
            closes = page.evaluate(
                "() => { window.__ouroWs.reconnectDelay = window.__ouroWs.maxDelay; return window.__closes; }")
            page.wait_for_function("n => window.__closes > n", arg=closes, timeout=10_000)
            last_close, closes = page.evaluate("[window.__readsAtClose, window.__closes]")
            # Reads held from before that close belong to retired generations: completing them applies
            # nothing, and frees the single-flight gate for the next offline poll.
            release([(read_id, route) for read_id, route in held if read_id <= last_close], census)

            def offline_read(after):
                for _ in range(400):
                    current = [(read_id, route) for read_id, route in held if read_id > after]
                    if current:
                        return current[0]
                    page.wait_for_timeout(20)
                raise AssertionError("the app polled the shared state while offline")

            # HTTP still answers while the socket is down: one complete offline read lands. The
            # budget reading it refreshes proves the body applied; it may restore no authority.
            applied = offline_read(last_close)
            release([applied], census)
            expect(budget).to_have_text(known_budget)
            views["offline_body_applied"] = authority()
            stages.append(f"offline read {applied[0]} applied (budget {known_budget}); status "
                          f"{status.inner_text()!r}")
            page.screenshot(path=str(evidence / "offline-body-applied.png"))
            # Hold the app's next offline poll in flight. With no read in flight, Main's open listener
            # gates a read before app.js retires older generations in the same open event; that read
            # can never apply, and only its coalesced follow-up is the post-open census.
            offline_reads = [applied[0], offline_read(applied[0])[0]]

            page.evaluate("window.__refuseSockets = false")
            page.wait_for_function("() => window.__ouroWs?.isConnected()", timeout=30_000)
            before_open = page.evaluate("window.__readsAtOpen")
            assert page.evaluate("window.__closesAtOpen") == closes, \
                "a close fell between the offline body and the reopen"
            expect(status).not_to_have_text("Reconnecting…")
            expect(page.locator("#reconnect-overlay")).not_to_have_class(re.compile(r"\bvisible\b"))
            page.wait_for_timeout(1_500)
            views["reopened_census_held"] = authority()
            page.screenshot(path=str(evidence / "reopened-census-held.png"))
            # The read begun before the drop answers only now, after the reopen, its body open.
            landed = page.evaluate("() => window.__inflightLanded")
            details.pop().fulfill(json={"task_id": "inflight-task", "project_id": "build",
                "question_rooms": {"inflight": {"state": "known", "chat_id": 12, "project_id": "build"}}, "owner_quiz": {
                "inflight": {**inflight, "state": "open", "asked_at": inflight["ts"]}}})
            page.wait_for_function("n => window.__inflightLanded > n", arg=landed, timeout=10_000)
            page.wait_for_timeout(500)
            views["stale_read_landed"] = authority()
            page.screenshot(path=str(evidence / "stale-read-landed.png"))
            assert views == {"offline_body_applied": withdrawn, "reopened_census_held": withdrawn,
                             "stale_read_landed": withdrawn}, views
            assert not details, "nothing read the withdrawn copy again before the census"
            expect(resume).to_be_disabled()
            expect(stop).to_be_disabled()
            expect(cached).to_have_attribute("data-state", "unknown")
            expect(unavailable).to_be_visible()
            expect(budget).to_have_text(known_budget)  # a reading, not an action: the offline body set it
            stages.append("offline body with its activity's question pointer, reopened socket with census "
                          "held and a read begun before the drop: authority stays withdrawn")

            # A read requested before the reopen belongs to a retired generation; the post-open
            # census queued behind it starts only when it settles.
            stale = [(read_id, route) for read_id, route in held if read_id <= before_open]
            release(stale, census)
            expect(resume).to_be_disabled()
            for _ in range(250):
                post_open = [(read_id, route) for read_id, route in held if read_id > before_open]
                if post_open:
                    break
                page.wait_for_timeout(20)
            assert post_open, "the reopened socket requested a fresh census"
            # This frame arrives on the reopened socket after that census was requested; its own
            # read, begun after the reopen, validates it.
            deliver(newer)
            expect(live).to_have_attribute("data-state", "open")
            census["current_questions"] = [question, inflight]
            release(post_open[:1], census)
            expect(resume).to_be_enabled()
            expect(stop).to_be_enabled()
            expect(cached).to_have_attribute("data-state", "open")
            expect(pending).to_have_attribute("data-state", "open")
            expect(pending.locator(".chat-quiz-option").first).to_be_enabled()
            expect(live).to_have_attribute("data-state", "open")
            expect(unavailable).not_to_be_visible()
            expect(budget).to_have_text(known_budget)
            expect(comment).to_be_enabled()
            expect(comment).to_have_value("Draft kept across the drop")
            expect(composer).to_have_value("Composer text kept across the drop")
            expect(settled).to_have_count(1)
            stages.append("fresh census restores controls; newer live question and drafts kept")
            page.screenshot(path=str(evidence / "fresh-census.png"))
            census["current_questions"] = [question, newer, inflight]
            holding = False
            release(list(held), census)
            assert not errors, errors
            assert not page.evaluate("window.__windowErrors"), page.evaluate("window.__windowErrors")
        finally:
            page.screenshot(path=str(evidence / "last-view.png"))
            (evidence / "receipt.json").write_text(json.dumps({
                "engine": engine, "stages": stages, "page_errors": errors, "released_reads": released,
                "window_errors": page.evaluate("window.__windowErrors"),
                "state_reads": page.evaluate("window.__stateReads.map(({id, served}) => ({id, served}))"),
                "socket_refusals": page.evaluate("window.__socketRefusals"),
                "reads_at_open": page.evaluate("window.__readsAtOpen ?? null"),
                "reads_at_last_close": page.evaluate("window.__readsAtClose ?? null"),
                "closes": page.evaluate("window.__closes ?? null"),
                "closes_at_open": page.evaluate("window.__closesAtOpen ?? null"),
                "offline_reads": offline_reads, "authority_views": views,
                "in_flight_read_landings": page.evaluate("window.__inflightLanded ?? null"),
                "candidate_identity": server["candidate_identity"],
                "boundary": "Real SPA, server and sockets; the drop closes the page's socket and refuses redials "
                            "on a closed loopback port; state census and the late WS frame are supplied by this fixture",
            }, indent=2), encoding="utf-8")
            for _read_id, route in held:
                route.abort()
            for route in details:
                route.abort()
            browser.close()
