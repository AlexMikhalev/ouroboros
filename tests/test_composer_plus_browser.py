"""The composer's "+" and its empty mount points in real engines (docs/DESIGN.md "Composer owner controls").

On a real server in Chromium and WebKit, light and dark, at 1280 and 360 px: "+" stands where the paperclip
stood, a 36 px target that keeps the text row's height; a pointer opens its menu above it without taking the
field's focus, and Attach file opens the file chooser and stages the chosen file; by keyboard, Enter opens the
menu onto Attach file, Esc closes it and returns focus to "+", Tab leaves it. The voice and chips slots take no
room while empty; chips put into the capsule row wrap inside it (no horizontal page scroll at 320 or 360 px) and
step aside with the other controls while an owner capsule stands alone.
"""
from __future__ import annotations

import json

import pytest

from tests.test_effort_range_browser import _evidence_dir, _launch, _open_chat
from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

PLUS = "#page-chat #chat-plus"
MENU = ".chat-plus-menu"
ATTACH = f"{MENU} [data-plus-item='attach']"
FACTS = """() => {
    const box = (node) => { if (!node) return null; const r = node.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
    const plus = document.querySelector('#page-chat #chat-plus');
    const menu = document.querySelector('.chat-plus-menu');
    const page = document.querySelector('#page-chat');
    const active = document.activeElement;
    return {
        plus: box(plus), row: box(plus.closest('.chat-text-row')), input: box(page.querySelector('#chat-input')),
        send: box(page.querySelector('.chat-send-group')), menu: box(menu),
        items: menu ? [...menu.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent.trim()) : [],
        expanded: plus.getAttribute('aria-expanded'), name: plus.getAttribute('aria-label'),
        focus: active === plus ? 'plus' : active?.id === 'chat-input' ? 'input'
            : active?.dataset?.plusItem ? 'item:' + active.dataset.plusItem : (active?.tagName || '').toLowerCase(),
        slots: ['[data-composer-voice]', '[data-composer-chips]'].map((sel) => {
            const node = page.querySelector(sel);
            return { display: getComputedStyle(node).display, width: node.getBoundingClientRect().width };
        }),
        overflow: document.documentElement.scrollWidth - window.innerWidth,
        view: window.innerWidth,
    };
}"""
TEXT_FILE = {"name": "plus-attach.txt", "mimeType": "text/plain", "buffer": b"staged through the plus menu\n"}


def _facts(page) -> dict:
    return page.evaluate(FACTS)


def _assert_plus_in_place(facts: dict, where: str) -> None:
    plus, row, field = facts["plus"], facts["row"], facts["input"]
    assert abs(plus["width"] - 36) <= 0.5 and abs(plus["height"] - 36) <= 0.5, (where, plus)
    # The finger-sized target keeps the paperclip's place: the row is no taller than its field needs.
    assert row["height"] <= field["height"] + 18.5, (where, row, field)
    assert abs((plus["top"] + plus["bottom"]) / 2 - (row["top"] + row["bottom"]) / 2) <= 1, (where, plus, row)
    assert plus["right"] <= field["left"] + 0.5, (where, plus, field)
    assert facts["name"] == "More actions" and facts["expanded"] == "false", (where, facts)
    assert all(slot == {"display": "none", "width": 0} for slot in facts["slots"]), (where, facts["slots"])
    assert facts["overflow"] <= 0, (where, facts["overflow"])


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_plus_menu_attaches_by_pointer_and_keyboard_in_both_themes(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    evidence = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 1280, "height": 900})
        try:
            page.close()
            for theme in ("light", "dark"):
                for width in (1280, 360):
                    where = f"{engine} {theme} {width}px"
                    page = browser.new_page(viewport={"width": width, "height": 800})
                    try:
                        _open_chat(page, url, width=width, height=800, theme=theme)
                        closed = _facts(page)
                        _assert_plus_in_place(closed, where)

                        # A pointer: the field keeps its focus; the menu opens above "+", inside the window.
                        page.click("#chat-input")
                        page.click(PLUS)
                        page.wait_for_selector(MENU, state="visible", timeout=5_000)
                        opened = _facts(page)
                        assert opened["focus"] == "input", (where, opened["focus"])
                        assert opened["expanded"] == "true" and opened["items"] == ["Attach file"], (where, opened)
                        menu = opened["menu"]
                        assert menu["bottom"] <= opened["plus"]["top"] + 0.5, (where, menu, opened["plus"])
                        assert menu["left"] >= 0 and menu["right"] <= opened["view"] + 0.5, (where, menu)
                        assert opened["overflow"] <= 0, (where, opened["overflow"])
                        page.screenshot(path=str(_evidence_dir(tmp_path) / f"composer-plus-{engine}-{theme}-{width}.png"),
                                        animations="disabled")
                        with page.expect_file_chooser(timeout=5_000) as chooser:
                            page.click(ATTACH)
                        chooser.value.set_files([TEXT_FILE])
                        page.wait_for_function(
                            "() => document.querySelectorAll('#chat-attachment-preview .attach-badge').length === 1")
                        attached = _facts(page)
                        assert attached["menu"] is None and attached["expanded"] == "false", (where, attached)
                        page.click("#chat-attachment-preview .attach-remove")
                        page.wait_for_function(
                            "() => !document.querySelector('#chat-attachment-preview .attach-badge')")

                        # The keyboard: Enter opens onto Attach file, Esc closes back to "+", Space opens, Tab leaves.
                        page.focus(PLUS)
                        page.keyboard.press("Enter")
                        page.wait_for_selector(MENU, state="visible", timeout=5_000)
                        assert _facts(page)["focus"] == "item:attach", where
                        page.keyboard.press("ArrowDown")
                        assert _facts(page)["focus"] == "item:attach", f"{where}: arrows stay among the items"
                        page.keyboard.press("Escape")
                        page.wait_for_selector(MENU, state="detached", timeout=5_000)
                        escaped = _facts(page)
                        assert escaped["focus"] == "plus" and escaped["expanded"] == "false", (where, escaped)
                        page.keyboard.press(" ")
                        page.wait_for_selector(MENU, state="visible", timeout=5_000)
                        page.keyboard.press("Tab")
                        page.wait_for_selector(MENU, state="detached", timeout=5_000)
                        evidence[where] = {"closed": closed, "opened": opened}
                    finally:
                        page.close()
        finally:
            (_evidence_dir(tmp_path) / f"composer-plus-{engine}.json").write_text(json.dumps(evidence, indent=1),
                                                                                 encoding="utf-8")
            browser.close()


CHIPS = """(count) => {
    const slot = document.querySelector('#page-chat [data-composer-chips]');
    for (let i = 0; i < count; i += 1) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chat-swarm';
        chip.textContent = `Contributed chip ${i + 1}`;
        slot.append(chip);
    }
    slot.hidden = false;
    const voice = document.querySelector('#page-chat [data-composer-voice]');
    const mic = document.createElement('button');
    mic.type = 'button';
    mic.className = 'chat-swarm';
    mic.textContent = 'Mic';
    voice.append(mic);
    voice.hidden = false;
}"""
SPREAD = """() => {
    const page = document.querySelector('#page-chat');
    const row = page.querySelector('.chat-toolbar-row');
    const view = window.innerWidth;
    const out = (node) => { const r = node.getBoundingClientRect(); return r.right > view + 0.5 || r.left < -0.5; };
    const chips = [...page.querySelectorAll('[data-composer-chips] button')];
    return {
        overflow: document.documentElement.scrollWidth - view,
        outside: [...chips, page.querySelector('[data-composer-voice] button'), page.querySelector('.chat-send-inline'),
            page.querySelector('#chat-plus')].filter(out).map((node) => node.textContent.trim() || node.id),
        chipsShown: getComputedStyle(page.querySelector('[data-composer-chips]')).display !== 'none',
        rowRight: row.getBoundingClientRect().right,
    };
}"""


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_filled_slots_wrap_inside_the_composer_and_step_aside_with_the_capsules(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 360, "height": 800})
        try:
            for width in (320, 360):
                where = f"{engine} {width}px"
                page.set_viewport_size({"width": width, "height": 800})
                _open_chat(page, url, width=width, height=800)
                _assert_plus_in_place(_facts(page), where)
                page.evaluate(CHIPS, 6)
                page.wait_for_timeout(200)
                spread = page.evaluate(SPREAD)
                assert spread["overflow"] <= 0 and not spread["outside"], (where, spread)
                assert spread["chipsShown"] and spread["rowRight"] <= width + 0.5, (where, spread)
                page.screenshot(path=str(_evidence_dir(tmp_path) / f"composer-slots-{engine}-{width}.png"),
                                animations="disabled")
                # An owner capsule open alone: the chips step aside with the pills.
                page.evaluate("() => { document.querySelector('#page-chat .chat-toolbar-row').dataset.ownerSolo = 'effort'; }")
                assert page.evaluate(SPREAD)["chipsShown"] is False, where
        finally:
            browser.close()
