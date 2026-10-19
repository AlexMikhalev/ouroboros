"""The row of capsules folds while the owner reads back through a room's history (owner 19.10, 2A; docs/DESIGN.md
"Composer owner controls"; web/modules/composer_fold.js).

On a phone, reading back folds the row (☰, Swarm, Context, Effort) away and ☰ waits beside the field, at its middle;
the way toward the latest and the field's focus bring it back; a desk never folds it. Uses the static-server/API
fixture. The feed is moved as the owner moves it, by a finger (CDP touch, Chromium's input pipeline) or a wheel: a
scroll the page makes itself is not the owner's, and the room pins it back to the latest.
"""
from __future__ import annotations

import pytest

from tests import test_subscription_setup_browser as setup_browser
from tests.test_design_system_gate_browser import seed_page
from tests.test_shell_interactions_browser import boot, chromium_ui, evidence, shell_ui

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
__all__ = ['chromium_ui', 'shell_ui', 'subscription_ui']

SEED = """() => { const f = document.querySelector('#chat-messages');
    for (let i = 0; i < 40; i += 1) { const r = document.createElement('div'); r.style.minHeight = '90px';
        r.textContent = 'row ' + i; f.append(r); }
    f.scrollTop = f.scrollHeight; }"""


def drag(page, dy, strokes=3):
    """A finger moves the feed by about `dy` per stroke (positive reads back), on the feed's right side."""
    cdp = page.context.new_cdp_session(page)
    try:
        for _ in range(strokes):
            y0 = 300 if dy > 0 else 600
            for i in range(19):
                cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart' if i == 0 else 'touchMove',
                                                      'touchPoints': [{'x': 380, 'y': y0 + dy * i / 18, 'id': 1}]})
                page.wait_for_timeout(12)
            cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
            page.wait_for_timeout(250)
    finally:
        cdp.detach()
    page.wait_for_timeout(450)
FACTS = """() => { const wrap = document.querySelector('#page-chat .chat-input-wrap');
    const row = wrap.querySelector('.chat-toolbar-row'), text = wrap.querySelector('.chat-text-row');
    const menu = row.querySelector(':scope > .composer-menu').getBoundingClientRect(), field = text.getBoundingClientRect();
    const swarm = row.querySelector('.chat-swarm');
    return {folded: 'folded' in wrap.dataset, rowHeight: Math.round(row.getBoundingClientRect().height),
            menuMiddle: menu.top + menu.height / 2, fieldMiddle: field.top + field.height / 2,
            menuRight: menu.right, fieldLeft: field.left, swarm: getComputedStyle(swarm).visibility,
            area: Math.round(document.querySelector('#chat-input-area').getBoundingClientRect().height)}; }"""


def test_reading_back_folds_the_row_and_the_way_down_brings_it_back(chromium_ui, tmp_path):
    page = seed_page(chromium_ui, 390, 844)
    facts = {}
    try:
        boot(page, chromium_ui)
        page.evaluate(SEED)
        page.wait_for_timeout(300)
        facts['latest'] = page.evaluate(FACTS)
        drag(page, 220)
        facts['reading'] = page.evaluate(FACTS)
        drag(page, -150, strokes=1)
        facts['back'] = page.evaluate(FACTS)
        drag(page, 220)
        facts['again'] = page.evaluate(FACTS)
        page.locator('#chat-input').focus()
        page.wait_for_timeout(450)
        facts['writing'] = page.evaluate(FACTS)
    finally:
        evidence(tmp_path, 'composer-fold-phone', facts)
        page.close()
    latest, reading, back, again, writing = (facts[k] for k in ('latest', 'reading', 'back', 'again', 'writing'))
    assert latest['folded'] is False and latest['swarm'] == 'visible', ('at the latest the row is there', latest)
    assert reading['folded'] is True and reading['swarm'] == 'hidden' and reading['rowHeight'] == 0, reading
    assert abs(reading['menuMiddle'] - reading['fieldMiddle']) <= 1.5, ('☰ waits at the field\'s middle', reading)
    assert reading['menuRight'] < reading['fieldLeft'], ('beside the field, not over it', reading)
    assert reading['area'] <= latest['area'] - 30, ('the composer gives its line back to the feed', reading, latest)
    assert back['folded'] is False and back['swarm'] == 'visible' and back['area'] == latest['area'], ('the way down', back)
    assert again['folded'] is True and writing['folded'] is False, ('the field\'s focus brings it back', writing)


def test_a_desk_never_folds_the_row(chromium_ui, tmp_path):
    page = seed_page(chromium_ui, 1280, 860)
    facts = {}
    try:
        boot(page, chromium_ui)
        page.evaluate(SEED)
        page.wait_for_timeout(300)
        page.mouse.move(700, 400)
        for _ in range(6):
            page.mouse.wheel(0, -200)
            page.wait_for_timeout(120)
        page.wait_for_timeout(500)
        facts['reading'] = page.evaluate(FACTS)
        facts['scrolled'] = page.evaluate("() => { const f = document.querySelector('#chat-messages'); return f.scrollHeight - f.clientHeight - f.scrollTop; }")
    finally:
        evidence(tmp_path, 'composer-fold-desk', facts)
        page.close()
    assert facts['scrolled'] > 300, ('the owner read back', facts)
    assert facts['reading']['folded'] is False and facts['reading']['swarm'] == 'visible', facts
