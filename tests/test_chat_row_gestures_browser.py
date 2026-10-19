"""A message's own gestures on a phone (owner 19.10, 4A; docs/DESIGN.md "Replies").

Swiped left past the reply point, a saved message replies as its Reply does: the row follows the finger, the reply mark
fills and takes the accent, the composer stages the quote, and the row settles back; short of the point nothing is
staged, and a thumb that drifts down once the swipe is sideways never scrolls the feed. Rightwards the finger stays the
drawer's. A long press sinks the row while the finger holds, then lifts it over the dimmed feed while its menu is
open (the header stays bright), and the row is back at rest once the menu closes. Saved rows come from the real
history endpoint (test_shell_interactions_browser.saved_history); synthetic CDP touch is Chromium's input pipeline, not
a claim about an iPhone.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from tests import test_subscription_setup_browser as setup_browser
from tests.test_design_system_gate_browser import seed_page
from tests.test_shell_interactions_browser import DRAWER, boot, chromium_ui, evidence, saved_history

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
__all__ = ['chromium_ui', 'subscription_ui']

ROW = """sel => { const row = document.querySelector(sel); const r = row.getBoundingClientRect();
    const mark = document.querySelector('.chat-row-reply-mark');
    return { x: r.x, y: r.y, w: r.width, h: r.height, swipe: row.dataset.rowSwipe || '', press: row.dataset.rowPress || '',
        transform: getComputedStyle(row).transform, shadow: getComputedStyle(row).boxShadow,
        mark: mark ? { armed: mark.dataset.armed || '', k: Number(mark.style.getPropertyValue('--reply-k')), shown: mark.isConnected } : null,
        staged: document.getElementById('chat-input-reply')?.textContent || '', scrollTop: document.getElementById('chat-messages').scrollTop,
        menu: [...document.querySelectorAll('.chat-row-menu [data-row-action]')].map((item) => item.dataset.rowAction) }; }"""
ASSISTANT = '#chat-messages .chat-bubble.assistant.has-reply'
USER = '#chat-messages .chat-bubble.user.has-reply'


class Finger:
    """One CDP touch, moved by hand."""

    def __init__(self, page):
        self.page = page
        self.cdp = page.context.new_cdp_session(page)

    def send(self, kind, x=0, y=0):
        points = [] if kind == 'touchEnd' else [{'x': x, 'y': y, 'id': 1}]
        self.cdp.send('Input.dispatchTouchEvent', {'type': kind, 'touchPoints': points})
        self.page.wait_for_timeout(16)

    def path(self, start, end, steps=12):
        for i in range(1, steps + 1):
            self.send('touchMove', start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)

    def close(self):
        self.cdp.detach()


def luminance(png, x, y):
    r, g, b = Image.open(io.BytesIO(png)).convert('RGB').getpixel((x, y))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def booted(chromium_ui, tmp_path, theme='light'):
    page = seed_page(chromium_ui, 390, 844)
    page.add_init_script(f"try {{ localStorage.setItem('ouroboros.theme', '{theme}'); }} catch (e) {{}}")
    client, respond = saved_history(tmp_path)
    page.route('**/api/chat/history**', respond)
    boot(page, chromium_ui)
    page.wait_for_selector(ASSISTANT, timeout=20_000)
    page.wait_for_timeout(400)
    return page, client


def test_a_message_swiped_left_past_the_point_replies_and_settles_back(chromium_ui, tmp_path):
    page, client = booted(chromium_ui, tmp_path)
    finger = Finger(page)
    facts = {}
    try:
        # Short of the reply point: the row comes back and nothing is staged.
        user = page.evaluate(ROW, USER)
        start = (user['x'] + user['w'] - 24, user['y'] + user['h'] / 2)
        finger.send('touchStart', *start)
        finger.path(start, (start[0] - 40, start[1] + 2), steps=6)
        facts['short_drag'] = page.evaluate(ROW, USER)
        finger.send('touchEnd')
        page.wait_for_timeout(450)
        facts['short_rest'] = page.evaluate(ROW, USER)
        # Past it, with the thumb drifting down once the swipe is sideways.
        row = page.evaluate(ROW, ASSISTANT)
        start = (row['x'] + row['w'] - 24, row['y'] + row['h'] / 2)
        facts['before'] = row
        finger.send('touchStart', *start)
        finger.path(start, (start[0] - 30, start[1] + 2), steps=3)
        facts['early'] = page.evaluate(ROW, ASSISTANT)
        finger.path((start[0] - 30, start[1] + 2), (start[0] - 110, start[1] + 60), steps=8)
        facts['armed'] = page.evaluate(ROW, ASSISTANT)
        page.screenshot(path=str(tmp_path / 'row-swipe-armed.png'))
        finger.send('touchEnd')
        page.wait_for_timeout(60)
        facts['released'] = page.evaluate(ROW, ASSISTANT)
        page.wait_for_timeout(450)
        facts['rest'] = page.evaluate(ROW, ASSISTANT)
    finally:
        finger.close()
        evidence(tmp_path, 'row-swipe-reply', facts)
        page.close()
        client.close()
    assert facts['short_drag']['swipe'] == 'drag' and facts['short_drag']['mark']['armed'] in ('', 'false'), facts
    assert facts['short_rest']['staged'] == '' and facts['short_rest']['transform'] == 'none', ('short of the point', facts)
    assert facts['short_rest']['swipe'] == '' and facts['short_rest']['mark'] is None, facts
    early, armed = facts['early'], facts['armed']
    assert early['swipe'] == 'drag' and early['transform'].startswith('matrix(1, 0, 0, 1, -'), ('the row follows', facts)
    assert 0 < early['mark']['k'] < 1, facts
    assert armed['mark']['armed'] == 'true' and armed['mark']['k'] == 1, ('past the point the mark takes the accent', facts)
    assert armed['scrollTop'] == facts['before']['scrollTop'], ('a sideways swipe never scrolls the feed', facts)
    assert 'The build is green' in facts['released']['staged'], ('released past it, the row replies', facts)
    assert facts['rest']['transform'] == 'none' and facts['rest']['swipe'] == '' and facts['rest']['mark'] is None, facts


def test_a_rightward_swipe_on_a_message_stays_the_drawers(chromium_ui, tmp_path):
    page, client = booted(chromium_ui, tmp_path)
    finger = Finger(page)
    facts = {}
    try:
        row = page.evaluate(ROW, ASSISTANT)
        start = (row['x'] + 30, row['y'] + row['h'] / 2)
        finger.send('touchStart', *start)
        finger.path(start, (start[0] + 240, start[1] - 10), steps=12)
        facts['during'] = page.evaluate(ROW, ASSISTANT)
        finger.send('touchEnd')
        page.wait_for_timeout(450)
        facts['drawer'] = page.evaluate(DRAWER)
        facts['row'] = page.evaluate(ROW, ASSISTANT)
    finally:
        finger.close()
        evidence(tmp_path, 'row-swipe-rightward', facts)
        page.close()
        client.close()
    assert facts['during']['swipe'] == '' and facts['during']['mark'] is None, facts
    assert facts['drawer']['open'] is True, ('rightwards the drawer takes the finger', facts)
    assert facts['row']['staged'] == '', facts


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_a_long_press_lifts_the_row_over_the_dimmed_feed_until_its_menu_closes(chromium_ui, tmp_path, theme):
    page, client = booted(chromium_ui, tmp_path, theme)
    finger = Finger(page)
    facts = {'theme': page.evaluate('document.documentElement.dataset.theme')}
    try:
        row = page.evaluate(ROW, ASSISTANT)
        # A pixel of the feed beside the row, and one of the header.
        beside = (8, round(row['y'] + row['h'] / 2))
        header = (195, 12)
        rest = page.screenshot()
        finger.send('touchStart', row['x'] + row['w'] / 2, row['y'] + min(row['h'] / 2, 30))
        page.wait_for_timeout(250)
        facts['hold'] = page.evaluate(ROW, ASSISTANT)
        page.wait_for_timeout(500)
        facts['lift'] = page.evaluate(ROW, ASSISTANT)
        lifted = page.screenshot()
        page.screenshot(path=str(tmp_path / 'row-lift.png'))
        facts['beside'] = [luminance(rest, *beside), luminance(lifted, *beside)]
        facts['header'] = [luminance(rest, *header), luminance(lifted, *header)]
        finger.send('touchEnd')
        page.wait_for_timeout(200)
        facts['after_up'] = page.evaluate(ROW, ASSISTANT)
        finger.send('touchStart', *header)
        finger.send('touchEnd')
        page.wait_for_timeout(80)
        facts['closing'] = page.evaluate(ROW, ASSISTANT)
        page.wait_for_timeout(450)
        facts['rest'] = page.evaluate(ROW, ASSISTANT)
    finally:
        finger.close()
        evidence(tmp_path, f'row-long-press-lift-{theme}', facts)
        page.close()
        client.close()
    hold, lift = facts['hold'], facts['lift']
    assert hold['press'] == 'hold' and hold['menu'] == [], ('a still finger sinks the row first', facts)
    assert lift['press'] == 'lift' and 'reply' in lift['menu'], facts
    assert lift['transform'].startswith('matrix(1.01'), ('the row lifts', facts)
    assert facts['theme'] == theme, facts
    assert facts['beside'][1] < facts['beside'][0] * 0.85, ('the feed around it dims', facts)
    assert abs(facts['header'][1] - facts['header'][0]) < 3, ('the header stays bright', facts)
    assert facts['after_up']['press'] == 'lift' and facts['after_up']['menu'], ('lifted while the menu is open', facts)
    assert facts['closing']['menu'] == [] and facts['closing']['press'] == 'release', facts
    assert facts['rest']['press'] == '' and facts['rest']['transform'] == 'none', facts
