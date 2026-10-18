"""Phone shell interactions: layer inert, Back, Escape, focus, the drawer drag, inline links, a short phone.

Each case turns a CR1 reviewer probe around: it asserts the fixed behaviour and, where a guard is involved, that
the guard still fires. Uses the static-server/API fixture (test_subscription_setup_browser); never starts the
runtime or reads live data. Synthetic CDP touch is Chromium's input pipeline, not a claim about an iPhone WebView.
"""
from __future__ import annotations

import json

import pytest

from tests import test_subscription_setup_browser as setup_browser
from tests.test_design_system_gate_browser import PROJECTS, save_evidence, seed_page, settle, sidebar_metrics

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.fixture(params=['webkit', 'chromium'])
def shell_ui(request, monkeypatch):
    monkeypatch.setenv('OUROBOROS_UI_BROWSER_ENGINE', request.param)
    ui = request.getfixturevalue('subscription_ui')
    ui['engine'] = request.param
    return ui


@pytest.fixture
def chromium_ui(request, monkeypatch):
    # CDP Input.dispatchTouchEvent exists only in Chromium; WebKit has no equivalent, so these cases run there only.
    monkeypatch.setenv('OUROBOROS_UI_BROWSER_ENGINE', 'chromium')
    ui = request.getfixturevalue('subscription_ui')
    ui['engine'] = 'chromium'
    return ui


def evidence(tmp_path, name, facts):
    save_evidence(tmp_path, name, facts)
    print(name, json.dumps(facts, indent=1))


def boot(page, ui):
    page.goto(ui['url'] + '/')
    page.wait_for_selector('#chat-input', state='attached')
    page.wait_for_selector('#nav-projects-list [data-project-id]', state='attached')
    settle(page)


def ids(page):
    return page.evaluate('window.__ouroLayers.ids()')


def wait_quietly(page, expression, timeout=3_000):
    """Wait for a condition; a defect that never meets it is recorded by the facts that follow, not a timeout."""
    try:
        page.wait_for_function(expression, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - the facts read next are the observation
        return False


def open_project(page, index=0):
    page.evaluate("p => window.dispatchEvent(new CustomEvent('ouro:open-project', {detail: {project: p}}))",
                  PROJECTS[index])
    page.wait_for_function("() => window.__ouroLayers.top() === 'project'")
    page.wait_for_timeout(300)


def open_drawer(page):
    page.locator('[data-mobile-nav-toggle]:visible').first.click()
    page.wait_for_function("() => window.__ouroLayers.top() === 'drawer'")
    page.wait_for_timeout(250)


def open_confirm(page):
    page.evaluate("""async () => {
        const m = await import('/static/modules/confirm_dialog.js');
        window.__confirmResult = 'pending';
        m.openConfirmDialog({title: 'Stop this work?', body: 'shell fixture', danger: true})
            .then((r) => { window.__confirmResult = JSON.stringify(r); });
    }""")
    page.wait_for_selector('.confirm-dialog-backdrop')


def open_reader(page):
    page.evaluate("""async () => {
        const m = await import('/static/modules/document_reader.js');
        window.__reader = m.createDocumentReader({});
        window.__reader.open({filename: 'notes.md', kind: 'markdown', meta: '1 KB',
                              reader: {url: '/api/files/read?path=notes.md'}});
    }""")
    page.wait_for_selector('dialog.document-reader[open]')
    page.wait_for_timeout(300)


def sync_project_rows(page):
    """The next /api/state snapshot changes one row (Project 5 has a new visible message). The 20 s poll would
    deliver it; the projects_changed frame (a backend-created project) asks for the same snapshot now."""
    rows = [dict(p) for p in PROJECTS]
    rows[5]['visible_revision'] = 3
    state = {'supervisor_ready': True, 'active_chat_activities': [], 'projects': rows,
             'project_chat_ids': [p['chat_id'] for p in rows]}
    page.route('**/api/state', lambda route: route.fulfill(json=state))
    page.evaluate("window.__ouroWs.emit('projects_changed', {})")
    page.wait_for_function("() => document.querySelector('#nav-projects-count')?.textContent === '1'")
    page.wait_for_timeout(300)


SURFACES = {
    'confirm': {'surface': '.confirm-dialog-backdrop',
                'control': '.confirm-dialog .marketplace-modal-actions [data-confirm-cancel]'},
    'reader': {'surface': 'dialog.document-reader', 'control': 'dialog.document-reader [data-reader-action="close"]'},
}
OVER_ROOM = """sel => {
    const s = document.querySelector(sel.surface), c = document.querySelector(sel.control);
    const room = {contentInert: Boolean(document.getElementById('content').closest('[inert]')),
                  panelInert: Boolean(document.getElementById('project-panel').closest('[inert]')),
                  layers: window.__ouroLayers.ids()};
    if (!s || !c) return {...room, open: false};
    const r = c.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return {...room, open: true, inert: Boolean(s.closest('[inert]')),
            hitIsControl: Boolean(hit) && (hit === c || c.contains(hit)),
            hit: hit ? hit.tagName + '.' + [...hit.classList].join('.') : null};
}"""


@pytest.mark.parametrize('surface', list(SURFACES))
def test_a_dialog_over_a_phone_project_stays_live_after_a_state_sync(shell_ui, tmp_path, surface):
    """Layer inert stops at the app shell: a body-level dialog survives the row change; the room stays inert."""
    page = seed_page(shell_ui, 390, 844)
    facts = {'engine': shell_ui['engine']}
    try:
        boot(page, shell_ui)
        open_project(page)
        open_confirm(page) if surface == 'confirm' else open_reader(page)
        facts['before_sync'] = page.evaluate(OVER_ROOM, SURFACES[surface])
        sync_project_rows(page)
        facts['after_sync'] = page.evaluate(OVER_ROOM, SURFACES[surface])
        try:
            page.locator(SURFACES[surface]['control']).click(timeout=2_500)
            facts['click'] = 'clicked'
        except Exception as exc:  # noqa: BLE001 - the observation is the point
            facts['click'] = f'failed: {type(exc).__name__}'
        page.wait_for_timeout(300)
        facts['after_click'] = page.evaluate(OVER_ROOM, SURFACES[surface])
        facts['result'] = page.evaluate('window.__confirmResult ?? null')
    finally:
        evidence(tmp_path, f'shell-dialog-after-sync-{surface}-{shell_ui["engine"]}', facts)
        page.close()
    for phase in ('before_sync', 'after_sync'):
        assert facts[phase]['open'] and not facts[phase]['inert'] and facts[phase]['hitIsControl'], facts
    # The guard still fires: the room outside the open Project layer stays inert, the panel stays live.
    assert facts['after_sync']['contentInert'] and not facts['after_sync']['panelInert'], facts
    assert facts['click'] == 'clicked' and not facts['after_click']['open'], facts
    assert facts['after_click']['layers'] == ['project'] and facts['after_click']['contentInert'], facts
    if surface == 'confirm':
        assert facts['result'] == 'false', facts


# A Back that leaves the app lands on about:blank: the facts say so instead of throwing.
BACK_FACTS = """() => ({layers: window.__ouroLayers?.ids() ?? 'left the app',
    drawer: Boolean(document.getElementById('primary-sidebar')?.classList.contains('open')),
    dialog: Boolean(document.querySelector('.new-project-dialog, .confirm-dialog')),
    result: window.__confirmResult ?? null})"""


def press_back(page, via):
    page.evaluate('Telegram.WebApp.BackButton.callback()' if via == 'telegram' else 'history.back()')


@pytest.mark.parametrize('via', ['history', 'telegram'])
@pytest.mark.parametrize('case', ['new-project-over-drawer', 'confirm-over-project'])
def test_back_closes_the_topmost_dialog_before_its_layer(shell_ui, tmp_path, case, via):
    layer = 'drawer' if case == 'new-project-over-drawer' else 'project'
    page = seed_page(shell_ui, 390, 844, telegram=via == 'telegram')
    facts = {'engine': shell_ui['engine']}
    try:
        boot(page, shell_ui)
        if layer == 'drawer':
            open_drawer(page)
            page.locator('#nav-projects-add').click()
            page.locator('[data-np-name]').fill('Retained draft')
        else:
            open_project(page)
            open_confirm(page)
        wait_quietly(page, 'window.__ouroLayers?.historyArmed()')
        facts['open'] = page.evaluate(BACK_FACTS)
        press_back(page, via)
        wait_quietly(page, "() => !document.querySelector('.new-project-dialog, .confirm-dialog') "
                           "|| !window.__ouroLayers?.size()")
        page.wait_for_timeout(200)
        facts['first_back'] = page.evaluate(BACK_FACTS)
        wait_quietly(page, 'window.__ouroLayers?.historyArmed()')
        press_back(page, via)
        wait_quietly(page, '() => !window.__ouroLayers?.size()')
        page.wait_for_timeout(200)
        facts['second_back'] = page.evaluate(BACK_FACTS)
    finally:
        evidence(tmp_path, f'shell-back-{case}-{via}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['open']['layers'] == [layer] and facts['open']['dialog'], facts
    assert facts['first_back']['layers'] == [layer] and not facts['first_back']['dialog'], facts
    assert facts['first_back']['drawer'] is (layer == 'drawer'), facts
    if layer == 'project':
        assert facts['first_back']['result'] == 'false', facts
    assert facts['second_back']['layers'] == [] and not facts['second_back']['drawer'], facts


@pytest.mark.parametrize('width,height', [(1280, 860), (390, 844)])
def test_escape_in_a_composer_capsule_closes_only_the_capsule(shell_ui, tmp_path, width, height):
    page = seed_page(shell_ui, width, height)
    facts = {'engine': shell_ui['engine'], 'viewport': [width, height]}
    try:
        boot(page, shell_ui)
        open_project(page)
        head = page.locator('#project-panel .chat-capsule-head').first
        head.click()
        page.wait_for_timeout(250)
        facts['capsule_before'] = head.get_attribute('aria-expanded')
        page.keyboard.press('Escape')
        page.wait_for_timeout(400)
        facts['capsule_after'] = head.get_attribute('aria-expanded') if head.count() else 'gone'
        facts['layers_after'] = ids(page)
        # Nothing inside takes the next Escape: it is the room's again (the guard is not a swallow-all).
        page.keyboard.press('Escape')
        page.wait_for_timeout(400)
        facts['layers_second'] = ids(page)
    finally:
        evidence(tmp_path, f'shell-escape-capsule-{shell_ui["engine"]}-{width}', facts)
        page.close()
    assert facts['capsule_before'] == 'true', facts
    assert facts['capsule_after'] == 'false' and facts['layers_after'] == ['project'], facts
    assert facts['layers_second'] == [], facts


WHO = "(() => { const a = document.activeElement; return a ? a.tagName + '#' + a.id + '.' + [...a.classList].slice(0, 3).join('.') : null; })()"


@pytest.mark.parametrize('how', ['enter', 'tap'])
def test_a_project_opened_from_the_phone_drawer_takes_focus(shell_ui, tmp_path, how):
    page = seed_page(shell_ui, 390, 844)
    facts = {'engine': shell_ui['engine'], 'how': how}
    def press(locator):  # a keyboard user or a finger
        if how == 'tap':
            return locator.tap()
        locator.focus()
        page.keyboard.press('Enter')

    try:
        boot(page, shell_ui)
        press(page.locator('[data-mobile-nav-toggle]:visible').first)
        page.wait_for_function("() => window.__ouroLayers.top() === 'drawer'")
        page.wait_for_timeout(250)
        press(page.locator('#nav-projects-list [data-project-id]').first)
        page.wait_for_function("() => window.__ouroLayers.top() === 'project'")
        page.wait_for_timeout(400)
        facts['focus'] = page.evaluate(WHO)
        facts['inside_panel'] = page.evaluate("document.getElementById('project-panel').contains(document.activeElement)")
        facts['layers'] = ids(page)
    finally:
        evidence(tmp_path, f'shell-focus-{how}-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['layers'] == ['project'] and facts['inside_panel'] is True, facts


def swipe(page, start, end, steps=12):
    cdp = page.context.new_cdp_session(page)
    try:
        for i in range(steps + 1):
            point = {'x': start[0] + (end[0] - start[0]) * i / steps,
                     'y': start[1] + (end[1] - start[1]) * i / steps, 'id': 1}
            cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart' if i == 0 else 'touchMove', 'touchPoints': [point]})
            page.wait_for_timeout(16)
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        page.wait_for_timeout(450)
    finally:
        cdp.detach()


DRAWER = """() => ({open: document.getElementById('primary-sidebar').classList.contains('open'),
                    layers: window.__ouroLayers.ids()})"""
UNDER = "([x, y]) => { const e = document.elementFromPoint(x, y); return e ? e.tagName + '#' + e.id + '.' + e.className : null; }"


@pytest.mark.parametrize('room', ['main', 'project'])
def test_the_drawer_follows_a_finger_on_a_phone_room(chromium_ui, tmp_path, room):
    page = seed_page(chromium_ui, 390, 844)
    under = [] if room == 'main' else ['project']  # the drawer opens over an open Project room
    facts = {'room': room}
    try:
        boot(page, chromium_ui)
        if room == 'project':
            open_project(page)
        facts['coarse'] = page.evaluate("matchMedia('(pointer: coarse)').matches")
        box = page.locator('#chat-messages' if room == 'main' else '#project-panel-body').bounding_box()
        y = box['y'] + box['height'] / 2
        facts['under'] = page.evaluate(UNDER, [60, y])
        for name, start, end in [('vertical_up', (200, y + 120), (200, y - 120)),
                                 ('vertical_down', (200, y - 120), (200, y + 120))]:
            swipe(page, start, end)
            facts[name] = page.evaluate(DRAWER)
            if facts[name]['open']:
                page.evaluate('window.__ouroLayers.pop()')  # keep the next case independent
                page.wait_for_timeout(300)
        swipe(page, (60, y), (330, y))
        facts['open_drag'] = page.evaluate(DRAWER)
        if facts['open_drag']['open']:
            swipe(page, (300, y), (20, y))  # starts on the open drawer
            facts['close_drag'] = page.evaluate(DRAWER)
            swipe(page, (60, y), (330, y))
            swipe(page, (370, y), (60, y))  # starts on the scrim's visible strip
            facts['scrim_close_drag'] = page.evaluate(DRAWER)
    finally:
        evidence(tmp_path, f'shell-drawer-drag-{room}', facts)
        page.close()
    assert facts['coarse'] is True, facts
    # The guard: a vertical finger scrolls the feed and never opens the drawer, in either direction.
    assert facts['vertical_up'] == facts['vertical_down'] == {'open': False, 'layers': under}, facts
    assert facts['open_drag'] == {'open': True, 'layers': under + ['drawer']}, facts
    assert facts['close_drag'] == {'open': False, 'layers': under}, facts
    assert facts['scrim_close_drag'] == {'open': False, 'layers': under}, facts


WORD = """bar => {
    const t = document.querySelector(bar + ' .rw-trigger:not([hidden])');
    const b = t?.querySelector('.status-badge');
    if (!t || !b) return null;
    const shown = getComputedStyle(b).display !== 'none';
    return {shown, cut: shown && b.scrollWidth > b.clientWidth + 1, word: t.dataset.word || '', title: t.title,
            mark: [...t.querySelectorAll('.rw-mark, .rw-dots')].some((m) => getComputedStyle(m).display !== 'none')};
}"""


@pytest.mark.parametrize('room,width', [('main', 390), ('project', 390), ('main', 1280)])
def test_the_status_word_is_whole_or_absent(shell_ui, tmp_path, room, width):
    page = seed_page(shell_ui, width, 844)
    facts = {'engine': shell_ui['engine'], 'room': room, 'width': width}
    try:
        boot(page, shell_ui)
        if room == 'project':
            open_project(page)  # the fixture's long Project name leaves the word no room on a phone
        page.wait_for_timeout(300)
        facts['word'] = page.evaluate(WORD, '#project-panel .project-panel-bar' if room == 'project' else '.chat-page-header')
    finally:
        evidence(tmp_path, f'shell-status-word-{room}-{width}-{shell_ui["engine"]}', facts)
        page.close()
    word = facts['word']
    assert word and word['mark'] and word['title'], facts
    assert word['cut'] is False, facts  # never a fragment such as "Wor…"
    if width >= 720:
        assert word['shown'] is True, facts  # a bar with room keeps the word


INLINE_LINK = """() => {
    const a = document.querySelector('.settings-section-copy a[href="https://clawhub.ai"]');
    const range = document.createRange();
    range.selectNodeContents(a.parentElement);
    const tops = [...range.getClientRects()].filter((q) => q.width && q.height).map((q) => q.top).sort((x, y) => x - y);
    const lines = tops.reduce((acc, t) => (acc.length && t - acc[acc.length - 1] < 3 ? acc : [...acc, t]), []);
    return {display: getComputedStyle(a).display, height: Math.round(a.getBoundingClientRect().height),
            lines: lines.length, gaps: lines.slice(1).map((t, i) => Math.round((t - lines[i]) * 10) / 10)};
}"""
TARGET = """sel => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const r = e.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const hit = [[cx - 21, cy], [cx + 21, cy], [cx, cy - 21], [cx, cy + 21]].every(([x, y]) => {
        const t = document.elementFromPoint(x, y);
        return Boolean(t) && (t === e || e.contains(t));
    });
    return {width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10, hit,
            display: getComputedStyle(e).display};
}"""


def open_settings_panel_of(page, selector):
    panel = page.evaluate("s => document.querySelector(s)?.closest('[data-settings-panel]')?.dataset.settingsPanel || ''",
                          selector)
    page.locator(f'[data-settings-tab="{panel}"]').first.click()
    page.locator(selector).first.scroll_into_view_if_needed()
    settle(page)
    return panel


def test_an_inline_link_in_settings_copy_reads_as_text_while_a_standalone_link_keeps_a_finger_target(shell_ui, tmp_path):
    page = seed_page(shell_ui, 390, 844)
    facts = {'engine': shell_ui['engine']}
    standalone = '.about-links a[href]'
    try:
        boot(page, shell_ui)
        facts['coarse'] = page.evaluate("matchMedia('(pointer: coarse)').matches")
        open_drawer(page)
        page.locator('[data-nav-page="settings"]').click()
        page.locator('#page-settings').wait_for(state='visible')
        facts['inline_panel'] = open_settings_panel_of(page, '.settings-section-copy a[href="https://clawhub.ai"]')
        facts['inline'] = page.evaluate(INLINE_LINK)
        facts['standalone_panel'] = open_settings_panel_of(page, standalone)
        facts['standalone'] = page.evaluate(TARGET, standalone)
    finally:
        evidence(tmp_path, f'shell-inline-link-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['coarse'] is True, facts
    inline = facts['inline']
    assert inline['display'] == 'inline', facts
    assert inline['lines'] >= 3 and max(inline['gaps']) - min(inline['gaps']) <= 2, facts
    target = facts['standalone']
    assert target and (target['hit'] or (target['width'] >= 43.9 and target['height'] >= 43.9)), facts


def test_a_short_phone_drawer_has_one_scroller_the_whole_column(shell_ui, tmp_path):
    page = seed_page(shell_ui, 390, 570)  # the 30-Project fixture of the gate
    facts = {'engine': shell_ui['engine']}
    try:
        boot(page, shell_ui)
        facts['coarse'] = page.evaluate("matchMedia('(pointer: coarse)').matches")
        facts['projects'] = page.locator('#nav-projects-list [data-project-id]').count()
        facts['scrollers'] = sidebar_metrics(page)  # opens the drawer
        facts['layers'] = ids(page)
    finally:
        evidence(tmp_path, f'shell-short-phone-sidebar-{shell_ui["engine"]}', facts)
        page.close()
    assert facts['coarse'] is True and facts['projects'] == len(PROJECTS) and facts['layers'] == ['drawer'], facts
    assert [row['node'] for row in facts['scrollers']] == ['primary-sidebar'], facts
