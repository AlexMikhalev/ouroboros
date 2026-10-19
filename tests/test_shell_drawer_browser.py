"""The phone drawer and a room's menu (owner 19.10; docs/DESIGN.md "Shell").

The owner could pull the drawer only from the middle of the room, never from the left edge, and it slipped off the
finger. A sideways finger now opens it from the screen's very edge, from the header and from the composer's
controls, and it keeps following while the row under the finger is redrawn or a browser bar changes the window's
height. In a room (Main, an open Project) ☰ is the composer row's first control, left of Swarm (1A); the drawer's
× stays in its head (2A); with the keyboard up the room's menu stays above it. Uses the static-server/API fixture
(test_subscription_setup_browser); synthetic CDP touch is Chromium's input pipeline, not a claim about an iPhone.
"""
from __future__ import annotations

import pytest

from tests import test_subscription_setup_browser as setup_browser
from tests.test_design_system_gate_browser import open_nav, seed_page
from tests.test_shell_interactions_browser import DRAWER, UNDER, boot, chromium_ui, evidence, open_project, shell_ui
from tests.test_ui_smoke_playwright import _install_controlled_visual_viewport

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
__all__ = ['chromium_ui', 'shell_ui', 'subscription_ui']


def swipe_with(page, start, end, steps=12, during=None):
    """A touch swipe from `start` to `end` with `during` run a third of the way, while the finger is still down."""
    cdp = page.context.new_cdp_session(page)
    try:
        for i in range(steps + 1):
            point = {'x': start[0] + (end[0] - start[0]) * i / steps,
                     'y': start[1] + (end[1] - start[1]) * i / steps, 'id': 1}
            cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart' if i == 0 else 'touchMove', 'touchPoints': [point]})
            if during and i == steps // 3:
                during()
            page.wait_for_timeout(16)
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        page.wait_for_timeout(450)
    finally:
        cdp.detach()


# A row of the room's feed the finger lands on (the stub's feed may be empty), remembered at pointerdown.
SEED_ROW = """feed => {
    const root = document.querySelector(feed);
    const row = document.createElement('div');
    row.className = 'test-drag-row';
    row.style.cssText = 'min-height:120px;padding:12px';
    row.textContent = 'A row the finger starts on';
    root.append(row);
    root.scrollTop = root.scrollHeight;
    const r = row.getBoundingClientRect();
    window.__downRow = null;
    document.addEventListener('pointerdown', (event) => { window.__downRow = event.target.closest('.test-drag-row'); },
        {capture: true, once: true});
    return {x: r.x + 30, y: r.y + r.height / 2};
}"""
REDRAW = "() => { const row = window.__downRow; if (!row) return false; row.replaceWith(row.cloneNode(true)); return !row.isConnected; }"


@pytest.mark.parametrize('room', ['main', 'project'])
def test_the_drawer_opens_from_the_edge_from_the_chrome_and_keeps_the_finger(chromium_ui, tmp_path, room):
    """The edge, the header and a composer capsule open the drawer (the page itself only pans up and down); it keeps
    following when a browser bar changes the height and when the row first under the finger is redrawn; a field
    keeps its finger."""
    page = seed_page(chromium_ui, 390, 844)
    under = [] if room == 'main' else ['project']
    facts = {'room': room}
    scope = '#page-chat' if room == 'main' else '#project-panel'
    feed_selector = '#chat-messages' if room == 'main' else '#project-panel .chat-messages'

    def reset():
        if page.evaluate(DRAWER)['open']:
            page.evaluate('window.__ouroLayers.pop()')
            page.wait_for_timeout(350)
    try:
        boot(page, chromium_ui)
        if room == 'project':
            open_project(page)
        header = page.locator(f'{scope} .app-page-header').first.bounding_box()
        feed = page.locator(feed_selector).first.bounding_box()
        field = page.locator(f'{scope} textarea').first.bounding_box()
        capsule = page.locator(f'{scope} .chat-toolbar-row .chat-swarm').first.bounding_box()
        mid = feed['y'] + feed['height'] / 2
        starts = {
            'edge': (3, mid),
            'header': (header['x'] + header['width'] / 2, header['y'] + header['height'] / 2),
            'capsule': (capsule['x'] + capsule['width'] / 2, capsule['y'] + capsule['height'] / 2),
        }
        for name, (x, y) in starts.items():
            facts[f'{name}_under'] = page.evaluate(UNDER, [x, y])
            swipe_with(page, (x, y), (x + 240, y - 16))
            facts[name] = page.evaluate(DRAWER)
            reset()
        swipe_with(page, (field['x'] + 30, field['y'] + field['height'] / 2), (field['x'] + 270, field['y'] + field['height'] / 2))
        facts['field'] = page.evaluate(DRAWER)
        reset()
        # A browser bar (or the keyboard) changes only the height while the finger is down.
        swipe_with(page, (40, mid), (300, mid), during=lambda: page.set_viewport_size({'width': 390, 'height': 790}))
        facts['bar_moved'] = page.evaluate(DRAWER)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_timeout(300)
        reset()
        # The row the finger started on is redrawn mid-drag (a live card, a new message).
        start = page.evaluate(SEED_ROW, feed_selector)
        facts['redrawn'] = None
        swipe_with(page, (start['x'], start['y']), (start['x'] + 260, start['y']),
                   during=lambda: facts.__setitem__('redrawn', page.evaluate(REDRAW)))
        facts['row_redrawn'] = page.evaluate(DRAWER)
    finally:
        evidence(tmp_path, f'shell-drawer-edge-{room}', facts)
        page.close()
    opened = {'open': True, 'layers': under + ['drawer']}
    for name in ('edge', 'header', 'capsule', 'bar_moved', 'row_redrawn'):
        assert facts[name] == opened, (name, facts)
    assert facts['redrawn'] is True, ('the row under the finger really left the page mid-drag', facts)
    assert facts['field'] == {'open': False, 'layers': under}, ('a field keeps its finger', facts)


MENU = """scope => {
    const room = document.querySelector(scope);
    const shown = (el) => Boolean(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
    const menu = [...room.querySelectorAll('[data-mobile-nav-toggle]')].find(shown);
    const row = room.querySelector('.chat-toolbar-row');
    const swarm = row?.querySelector('.chat-swarm');
    const box = (el) => el ? (({x, y, width, height}) => ({x, y, w: width, h: height}))(el.getBoundingClientRect()) : null;
    const b = menu?.getBoundingClientRect();
    const at = (dx, dy) => document.elementFromPoint(b.x + b.width / 2 + dx, b.y + b.height / 2 + dy)
        ?.closest('[data-mobile-nav-toggle]') === menu;
    const area = menu ? getComputedStyle(menu, '::after') : null;
    return {inRow: Boolean(menu && menu.parentElement === row && row.firstElementChild === menu),
            inHeader: [...room.querySelectorAll('.app-page-header [data-mobile-nav-toggle]')].some(shown),
            menu: box(menu), swarm: box(swarm),
            // The finger's target is the touch minimum, centred on the button: 21px from its centre is still the
            // button, 30px is not (not the composer around it either).
            inside: b ? [[0, -21], [0, 21], [-21, 0]].map(([dx, dy]) => at(dx, dy)) : [],
            outside: b ? [[0, -30], [0, 30], [-30, 0]].map(([dx, dy]) => at(dx, dy)) : [],
            area: area && {w: area.width, h: area.height}};
}"""


@pytest.mark.parametrize(('height', 'theme'), [(844, 'light'), (640, 'light'), (844, 'dark')])
@pytest.mark.parametrize('room', ['main', 'project'])
def test_a_rooms_menu_is_the_composers_first_control(shell_ui, tmp_path, room, height, theme):
    """In a room ☰ is the composer row's first control, left of Swarm, under the thumb: as tall as the capsules,
    the touch minimum centred on it as its finger target; the room's header has none. It opens the drawer, whose ×
    stays in its head. A page without a composer keeps ☰ in its header. The light theme is the owner's: its
    capsules have no glass, and a glass backdrop would itself hold the finger target on the button."""
    page = seed_page(shell_ui, 390, height)
    page.add_init_script(f"try {{ localStorage.setItem('ouroboros.theme', '{theme}'); }} catch (e) {{}}")
    facts = {'engine': shell_ui['engine'], 'room': room, 'height': height}
    scope = '#page-chat' if room == 'main' else '#project-panel'
    try:
        boot(page, shell_ui)
        facts['theme'] = page.evaluate('document.documentElement.dataset.theme')
        if room == 'project':
            open_project(page)
        page.wait_for_timeout(800)  # the composer's controls settle (composer_owner_controls.js) before the measure
        facts.update(page.evaluate(MENU, scope))
        page.locator(f'{scope} .chat-toolbar-row [data-mobile-nav-toggle]').tap()
        page.wait_for_function("() => window.__ouroLayers.top() === 'drawer'")
        page.wait_for_timeout(300)
        facts['close'] = page.locator('#nav-drawer-close').bounding_box()
        facts['menu_after_open'] = page.locator(f'{scope} .chat-toolbar-row [data-mobile-nav-toggle]').get_attribute('aria-expanded')
        page.evaluate('window.__ouroLayers.pop()')
        page.wait_for_timeout(300)
        if room == 'main':
            open_nav(page, 'settings')
            facts['settings_menu_in_header'] = page.evaluate(
                "() => [...document.querySelectorAll('#page-settings .app-page-header [data-mobile-nav-toggle]')].some(e => e.getClientRects().length)")
    finally:
        evidence(tmp_path, f'shell-room-menu-{room}-{height}-{theme}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['theme'] == theme, facts
    assert facts['inRow'] is True and facts['inHeader'] is False, facts
    assert facts['menu']['x'] + facts['menu']['w'] <= facts['swarm']['x'], ('left of Swarm', facts)
    assert facts['menu']['h'] == facts['swarm']['h'] == 32, ('as tall as the capsules', facts)
    assert facts['inside'] == [True, True, True] and facts['outside'] == [False, False, False], (
        'the touch minimum, centred on the button, is its finger target', facts)
    assert facts['area'] == {'w': '44px', 'h': '44px'}, facts
    assert facts['menu_after_open'] == 'true', facts
    assert facts['close']['y'] < 100, ('the drawer\'s × stays in its head', facts)
    if room == 'main':
        assert facts['settings_menu_in_header'] is True, ('a page without a composer keeps ☰ in its header', facts)


KEYBOARD = """scope => {
    const menu = document.querySelector(scope + ' .chat-toolbar-row [data-mobile-nav-toggle]').getBoundingClientRect();
    const field = document.querySelector(scope + ' textarea').getBoundingClientRect();
    return {open: document.body.classList.contains('keyboard-open'), menuBottom: menu.bottom, fieldBottom: field.bottom,
            vvh: getComputedStyle(document.documentElement).getPropertyValue('--vvh').trim()};
}"""


@pytest.mark.parametrize('room', ['main', 'project'])
def test_a_rooms_menu_stays_above_the_keyboard(shell_ui, tmp_path, room):
    """With the keyboard up (the visible viewport 500px of 844), the room's composer, and the ☰ in it, sits inside
    the visible part, a Project room's as well as Main's."""
    page = seed_page(shell_ui, 390, 844)
    _install_controlled_visual_viewport(page, 844)
    facts = {'engine': shell_ui['engine'], 'room': room}
    scope = '#page-chat' if room == 'main' else '#project-panel'
    try:
        boot(page, shell_ui)
        if room == 'project':
            open_project(page)
        page.locator(f'{scope} textarea').first.focus()
        page.evaluate('() => window.__setTestVisualViewportHeight(500)')
        page.wait_for_function("() => document.body.classList.contains('keyboard-open')", timeout=5_000)
        page.wait_for_timeout(300)
        facts.update(page.evaluate(KEYBOARD, scope))
    finally:
        evidence(tmp_path, f'shell-room-menu-keyboard-{room}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['open'] is True and facts['vvh'] == '500px', facts
    assert facts['menuBottom'] <= 500 and facts['fieldBottom'] <= 500, ('above the keyboard', facts)
