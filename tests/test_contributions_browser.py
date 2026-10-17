"""Skills in the page, over the static SPA in a real engine (no runtime): a saved reply's contributed action between
Reply and Copy and in the row menu, a page module that registers its provider beside one that throws (one notice, the
page keeps working), and the ways to start without page modules: `?plugins=off`, the boot marker of a start that did
not finish, and the chat header's Restart without plugins."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from tests import test_subscription_setup_browser as setup_browser

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

VOICE_URL = "/api/extensions/voice/ui/r1/ui/tts.js"
BROKEN_URL = "/api/extensions/broken/ui/r1/ui/panel.js"
MODULES = {
    VOICE_URL: """export function mount(ctx) {
        window.voiceContext = Object.keys(ctx).sort();
        ctx.providers.register('tts', {label: 'Device voice', voices: () => [], speak: async () => {}, stop() {}});
        return {dispose() { window.voiceDisposed = true; }};
    }""",
    BROKEN_URL: "export function mount() { throw new Error('the panel broke'); }",
}


def _item(key, **extra):
    skill, ident = key.split(":")
    return {"key": key, "skill": skill, "skill_label": skill.title(), "id": ident, "label": skill.title(), "title": "",
            "icon": "", "order": 0, "revision": "r1", "render": {"kind": "declarative", "url": ""}, **extra}


PROJECTION = {
    "contributions": [
        _item("lists:save", placement="message.action", label="Save to list", title="Keep this answer in a list",
              icon="list", action={"verb": "notice", "text": "Saved to Groceries"}),
        _item("voice:tts", provides="tts", runtime="client", destination="device", skill_label="Device voice",
              label="Device voice", render={"kind": "host", "url": VOICE_URL}),
        _item("broken:panel", placement="composer.plus", action={"verb": "notice", "text": "unused"},
              render={"kind": "host", "url": BROKEN_URL}),
    ],
    "offers": {"stt": []}, "voice": {}, "icons": [], "problems": {},
    "preferences": {"voice": {"stt": "", "tts": ""}, "contributions": {"hidden": [], "order": []}},
}
HISTORY = {"messages": [
    {"role": "user", "text": "What do we need from the shop?", "chat_id": 1, "source": "web",
     "ts": "2026-10-17T09:00:00Z", "history_id": "h-0", "row_address": "row:1@2026-10-17T09:00:00Z#aaaaaaaaaaaa"},
    {"role": "assistant", "text": "Milk, eggs and bread.", "chat_id": 1,
     "ts": "2026-10-17T09:00:20Z", "history_id": "h-1", "row_address": "row:1@2026-10-17T09:00:20Z#bbbbbbbbbbbb"},
], "progress": []}


def open_app(ui, path="/", *, width=1280, scheme="dark"):
    page = ui["page"]
    loaded = []
    origin = urlparse(ui["url"]).netloc
    page.route("**/*", lambda route: route.fallback()
               if urlparse(route.request.url).netloc == origin else route.abort())
    page.route("**/api/ui/contributions*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(PROJECTION)))
    page.route("**/api/chat/history*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(HISTORY)))

    def module(route):
        path = urlparse(route.request.url).path
        loaded.append(path)
        if path in MODULES:
            route.fulfill(content_type="text/javascript", body=MODULES[path])
        else:
            route.fulfill(status=404, body="")
    page.route("**/api/extensions/**", module)
    page.set_viewport_size({"width": width, "height": 800})
    page.emulate_media(color_scheme=scheme)
    page.goto(ui["url"] + path)
    page.wait_for_selector("#chat-input", state="attached")
    return page, loaded


def toast(page, text):
    return page.locator(".toast", has_text=text)


@pytest.mark.parametrize("width,scheme", [(360, "light"), (360, "dark"), (1280, "light"), (1280, "dark")])
def test_a_saved_reply_offers_the_skill_action_and_a_failing_page_module_costs_only_itself(subscription_ui, width, scheme):
    page, loaded = open_app(subscription_ui, width=width, scheme=scheme)
    answer = page.locator(".chat-bubble.assistant").first
    action = answer.locator(".chat-message-actions > .chat-message-action")
    action.wait_for()
    controls = answer.locator(".chat-message-actions > *").evaluate_all("nodes => nodes.map(node => node.className)")
    assert controls == ["chat-message-reply", "chat-message-action", "chat-message-copy"], controls
    owner = page.locator(".chat-bubble.user").first
    assert owner.locator(".chat-message-action").count() == 0, "a skill's action is for Ouroboros's replies"
    assert action.get_attribute("aria-label") == "Save to list"
    geometry = answer.evaluate("""bubble => {
        const time = bubble.querySelector('.msg-time').getBoundingClientRect();
        const row = bubble.querySelector('.chat-message-actions').getBoundingClientRect();
        const box = bubble.getBoundingClientRect();
        return {timeRight: time.right, rowLeft: row.left, rowRight: row.right, boxRight: box.right,
            sameLine: time.bottom > row.top && time.top < row.bottom};
    }""")
    assert not geometry["sameLine"] or geometry["timeRight"] <= geometry["rowLeft"] + 0.5, geometry
    assert geometry["rowRight"] <= geometry["boxRight"] + 0.5, geometry
    action.click()
    toast(page, "Saved to Groceries").wait_for()
    answer.locator(".message").click(button="right")
    menu = page.locator(".chat-row-menu")
    menu.wait_for()
    assert menu.locator("[role=menuitem]").all_inner_texts()[-1] == "Save to list", "after the row's own doors, in words"
    page.keyboard.press("Escape")
    # The module that registers its provider runs; the one that throws is off with one notice; the page still works.
    page.wait_for_function("Array.isArray(window.voiceContext)")
    assert page.evaluate("window.voiceContext") == [
        "api", "draft", "key", "notice", "origin", "providers", "signal", "skill", "theme"]
    toast(page, "Broken stopped working: the panel broke.").wait_for()
    providers = page.evaluate("""async () => (await import('/static/modules/host_modules.js'))
        .getHostModules().clientProviders('tts').map((entry) => entry.key)""")
    assert providers == ["voice:tts"]
    assert sorted(set(loaded)) == [BROKEN_URL, VOICE_URL]
    page.locator("#chat-input").fill("still typing")
    assert page.locator("#chat-input").input_value() == "still typing"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no horizontal page scroll"


def test_plugins_off_starts_without_page_code_and_its_notice_restarts_with_it(subscription_ui):
    page, loaded = open_app(subscription_ui, "/?plugins=off#chat")
    notice = toast(page, "Started without plugins.")
    notice.wait_for()
    page.locator(".chat-bubble.assistant .chat-message-action").wait_for()
    assert loaded == [], "no page module is even fetched"
    assert page.evaluate("window.voiceContext") is None
    with page.expect_navigation():
        notice.get_by_role("button", name="Restart with plugins").click()
    assert urlparse(page.url).query == "" and urlparse(page.url).fragment == "chat"
    page.wait_for_function("Array.isArray(window.voiceContext)")


def test_restart_without_plugins_in_the_more_menu_and_a_start_that_did_not_finish(subscription_ui):
    page, loaded = open_app(subscription_ui)
    page.wait_for_function("Array.isArray(window.voiceContext)")
    page.locator(".chat-header-more > summary").click()
    door = page.get_by_role("button", name="Restart without plugins")
    with page.expect_navigation():
        door.click()
    assert "plugins=off" in urlparse(page.url).query
    toast(page, "Started without plugins.").wait_for()
    # A marker left by a start that froze: the next load skips every module until the owner asks for them.
    page.evaluate("""() => localStorage.setItem('ouroboros.hostModules.boot',
        JSON.stringify({at: Date.now(), keys: ['voice:tts']}))""")
    loaded.clear()
    page.goto(subscription_ui["url"] + "/")
    skipped = toast(page, "Plugins were skipped because the last start did not finish.")
    skipped.wait_for()
    assert loaded == [] and page.evaluate("window.voiceContext") is None
    with page.expect_navigation():
        skipped.get_by_role("button", name="Start with plugins").click()
    page.wait_for_function("Array.isArray(window.voiceContext)")
    page.wait_for_function("localStorage.getItem('ouroboros.hostModules.boot') === null")
