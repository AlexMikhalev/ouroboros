"""Actual sidebar + Project gateway/storage; controlled non-project census, no owner state.

The existing shell server supplies assets and non-project APIs. Project list/update
requests exercise the real gateway and registry in a private TestClient drive.
"""
import json
from urllib.parse import urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ouroboros.gateway.router import collect_routes
from ouroboros import projects_registry as registry
from tests.test_subscription_setup_browser import subscription_ui  # noqa: F401
from tests._shared import producer_evidence_dir

pytestmark = pytest.mark.ui_browser


@pytest.fixture(params=[("chromium", 1280, "dark"), ("webkit", 390, "light")])
def pins_ui(request, monkeypatch, tmp_path):
    engine, width, theme = request.param
    monkeypatch.setenv("OUROBOROS_UI_BROWSER_ENGINE", engine)
    ui = request.getfixturevalue("subscription_ui")
    root = tmp_path / "pin-data"
    for pid, name in (("older", "Old project with a long descriptive title"), ("newer", "Recent project")):
        registry.create_project(root, pid, name=name, origin="owner_ui")
    registry.update_project(root, "older", last_active_at="2026-01-01T00:00:00Z")
    app = Starlette(routes=collect_routes(data_dir=root))
    app.state.drive_root = root
    control = {"fail": False, "hold": False, "held": [], "writes": [], "unread": set(), "activities": []}
    pages = []
    with TestClient(app) as client:
        def respond(route):
            path = urlparse(route.request.url).path
            if path == "/api/state":
                rows = registry.projects_summary(root, include_pinned=True)
                for project in rows:
                    if project['id'] in control['unread']:
                        project['visible_revision'] = 2
                state = {"sha": "pin-fixture", "supervisor_ready": True, "active_chat_activities_complete": True,
                         "active_chat_activities": control['activities'], "projects": rows,
                         "project_chat_ids": [p["chat_id"] for p in rows]}
                if control["hold"]:
                    control["held"].append((route, json.dumps(state)))
                    return
                route.fulfill(content_type="application/json", body=json.dumps(state))
            elif path.startswith("/api/projects/") and route.request.method == "POST":
                body = route.request.post_data_json
                control["writes"].append(body)
                if control["fail"]:
                    route.fulfill(status=503, content_type="application/json", body='{"error":"Synthetic write outage"}')
                else:
                    response = client.post(path, json=body)
                    route.fulfill(status=response.status_code, content_type="application/json", body=response.text)
            elif path == "/api/projects":
                response = client.get(path)
                route.fulfill(status=response.status_code, content_type="application/json", body=response.text)
            else:
                route.fallback()

        def new_page():
            page = ui["new_page"](viewport={"width": width, "height": 900}, has_touch=width < 720)
            pages.append(page)
            page.add_init_script(f"localStorage.setItem('ouroboros.theme', {json.dumps(theme)})")
            page.route("**/api/**", respond)
            page.clock.install()
            page.goto(ui["url"])
            page.locator('.nav-project-pin').first.wait_for()
            if width < 720:
                page.locator('#page-chat [data-mobile-nav-toggle]').click()
            return page

        page = new_page()
        try:
            yield {**ui, "page": page, "make_page": new_page, "root": root, "control": control,
                   "width": width, "engine": engine, "theme": theme, "client": client}
        finally:
            # Stop route callbacks and polling before their TestClient drive closes.
            for page in pages:
                page.close()


def row(page, pid):
    return page.locator(f'.nav-project-item[data-nav-project-id="{pid}"]')


def refresh(page):
    page.clock.fast_forward(21_000)


def test_pin_reorder_reload_collapse_keyboard_and_shared_state(pins_ui, tmp_path):
    ui = pins_ui
    page, expect = ui["page"], pytest.importorskip("playwright.sync_api").expect
    pin = row(page, "older").locator('.nav-project-pin')
    expect(pin).to_have_attribute("aria-pressed", "false")
    page.evaluate("window.savedPin = document.querySelector('[data-nav-project-id=older] .nav-project-pin')")
    pin.focus()
    pin.press("Enter")
    expect(pin).to_have_attribute("aria-pressed", "true")
    assert page.evaluate("savedPin === document.activeElement && savedPin.isConnected")
    assert page.locator('.nav-project-item').first.get_attribute("data-nav-project-id") == "older"
    assert page.locator('.nav-project-row.active').count() == 0  # not an Open Project gesture
    assert registry.get_project(ui["root"], "older")["pinned"] is True
    evidence = producer_evidence_dir(tmp_path, "project-pin", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    page.locator('#primary-sidebar').screenshot(path=str(evidence / f"expanded-{ui['engine']}-{ui['width']}.png"), animations="disabled")
    page.locator('#nav-projects-toggle').click()
    expect(row(page, "older")).to_be_visible()
    expect(row(page, "newer")).to_be_hidden()
    page.screenshot(path=str(evidence / f"pinned-{ui['engine']}-{ui['width']}.png"), animations="disabled")
    # Each visible control owns a non-overlapping coarse-pointer target.
    geometry = pin.bounding_box()
    if ui["width"] < 720:
        assert geometry["width"] >= 44 and geometry["height"] >= 44
    assert page.locator('#primary-sidebar').evaluate("e => e.scrollWidth <= e.clientWidth")
    page.reload()
    if ui["width"] < 720:
        page.locator('#page-chat [data-mobile-nav-toggle]').click()
    expect(row(page, "older").locator('.nav-project-pin')).to_have_attribute("aria-pressed", "true")
    second = ui["make_page"]()
    row(second, "older").locator('.nav-project-pin').click()
    expect(row(second, "older").locator('.nav-project-pin')).to_have_attribute("aria-pressed", "false")
    refresh(page)
    expect(row(page, "older").locator('.nav-project-pin')).to_have_attribute("aria-pressed", "false")
    assert page.locator('.nav-project-item').first.get_attribute("data-nav-project-id") == "newer"
    # Existing Rename remains a partial update and does not pin the room again.
    response = ui["client"].post('/api/projects/older/update', json={"name": "Renamed"})
    assert response.status_code == 200 and response.json()["project"]["pinned"] is False
    refresh(page)
    expect(row(page, "older").locator('.nav-row-label')).to_have_text("Renamed")


def test_failed_save_and_delayed_state_do_not_forge_or_undo_confirmation(pins_ui):
    ui, expect = pins_ui, pytest.importorskip("playwright.sync_api").expect
    page, control = ui["page"], ui["control"]
    pin = row(page, "older").locator('.nav-project-pin')
    control["fail"] = True
    pin.click()
    expect(page.locator('#toast-stack')).to_contain_text("Synthetic write outage")
    expect(pin).to_have_attribute("aria-pressed", "false")
    assert registry.get_project(ui["root"], "older")["pinned"] is False
    assert len(control["writes"]) == 1
    control["fail"] = False
    control["hold"] = True
    with page.expect_request(lambda request: urlparse(request.url).path == '/api/state'):
        refresh(page)
    assert control["held"]
    pin.click()
    expect(pin).to_have_attribute("aria-pressed", "true")
    control["hold"] = False
    for route, body in control["held"]:
        route.fulfill(content_type="application/json", body=body)
    expect(pin).to_have_attribute("aria-disabled", "false")
    expect(pin).to_have_attribute("aria-pressed", "true")
    page.wait_for_function("() => document.querySelector('[data-nav-project-id=older] .nav-project-pin').getAttribute('aria-pressed') === 'true'")
    assert registry.get_project(ui["root"], "older")["pinned"] is True


def test_pins_keep_unread_activity_menu_and_deleting_semantics(pins_ui):
    ui, expect = pins_ui, pytest.importorskip("playwright.sync_api").expect
    page, control = ui["page"], ui["control"]
    control["unread"].add("newer")
    control["activities"] = [{"activity_id": "work", "project_id": "newer",
                              "chat_id": registry.get_project(ui["root"], "newer")["chat_id"],
                              "kind": "managed_task", "phase": "working"}]
    refresh(page)
    expect(row(page, "newer").locator('.nav-unread-dot')).to_be_visible()
    expect(row(page, "newer")).to_have_attribute("data-live", "")
    row(page, "older").locator('.nav-project-pin').click()
    expect(row(page, "older").locator('.nav-project-pin')).to_have_attribute("aria-pressed", "true")
    assert page.locator('.nav-project-item').first.get_attribute("data-nav-project-id") == "older"
    expect(row(page, "newer").locator('.nav-unread-dot')).to_be_visible()
    page.locator('#nav-projects-toggle').click()
    expect(row(page, "newer")).to_be_visible()  # live, though not pinned
    kebab = row(page, "newer").locator('.nav-project-kebab')
    kebab.click()
    page.evaluate("window.savedMenuAnchor = document.querySelector('[data-nav-project-id=newer] .nav-project-kebab')")
    original_anchor = kebab.bounding_box()
    ui["client"].post('/api/projects/older/update', json={"name": "Changed elsewhere", "pinned": False})
    refresh(page)
    expect(row(page, "older").locator('.nav-row-label')).to_have_text("Changed elsewhere")
    assert page.evaluate("savedMenuAnchor.isConnected && savedMenuAnchor === document.querySelector('[data-nav-project-id=newer] .nav-project-kebab')")
    expect(page.locator('.project-row-menu')).to_be_visible()
    anchor, popup = kebab.bounding_box(), page.locator('.project-row-menu').bounding_box()
    assert anchor['y'] != original_anchor['y']  # a real reorder, not a same-position repaint
    assert abs(popup['y'] - (anchor['y'] + anchor['height'] + 4)) <= 1
    page.keyboard.press("Escape")
    control['unread'].clear()
    refresh(page)
    expect(row(page, 'newer').locator('.nav-unread-dot')).to_have_count(0)
    registry.begin_project_deletion(ui["root"], "older")
    refresh(page)
    expect(row(page, "older").locator('.nav-project-row')).to_be_disabled()
    expect(row(page, "older").locator('.nav-project-deleting-status')).to_have_text("Deleting…")
    assert row(page, "older").locator('.nav-project-pin').count() == 0


def test_menu_repositions_after_a_hidden_pin_is_revealed(pins_ui, tmp_path):
    ui, expect = pins_ui, pytest.importorskip("playwright.sync_api").expect
    page, control = ui["page"], ui["control"]
    control["activities"] = [{"activity_id": "work", "project_id": "newer",
                              "chat_id": registry.get_project(ui["root"], "newer")["chat_id"],
                              "kind": "managed_task", "phase": "working"}]
    refresh(page)
    expect(row(page, "newer")).to_have_attribute("data-live", "")
    page.locator('#nav-projects-toggle').click()
    expect(row(page, "older")).to_be_hidden()
    kebab = row(page, "newer").locator('.nav-project-kebab')
    kebab.click()
    original_anchor = kebab.bounding_box()
    response = ui["client"].post('/api/projects/older/update', json={"pinned": True})
    assert response.status_code == 200
    refresh(page)
    expect(row(page, "older")).to_be_visible()
    expect(page.locator('.project-row-menu')).to_be_visible()
    anchor, popup = kebab.bounding_box(), page.locator('.project-row-menu').bounding_box()
    assert anchor['y'] != original_anchor['y']
    assert abs(popup['y'] - (anchor['y'] + anchor['height'] + 4)) <= 1
    evidence = producer_evidence_dir(tmp_path, "pin-menu", env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    page.screenshot(path=str(evidence / f"collapsed-menu-{ui['engine']}.png"), animations="disabled")
