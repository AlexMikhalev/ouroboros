// The composer's owner controls (docs/DESIGN.md "Composer owner controls"): two labelled
// capsules. The effort range's handle math, the open/pin state machine with and without a
// hovering pointer, keyboard, serialized saves with a refused save returning to the server's
// value, the one global value from /api/state, teardown; the context capsule's three modes, its
// save and a scrolling strip that keeps the chosen mode in view; one capsule open at a time, and
// the names that give way on a narrow line.
import test from 'node:test';
import assert from 'node:assert/strict';

import { ElementStub } from './chat_dom_fixture.js';
import {
    applyHandle, createComposerOwnerControls, createEffortRangeControl, nearestHandle, snapRange,
} from '../modules/composer_owner_controls.js';

/* A document whose listeners can be fired, and a window that may or may not hover. */
function stubDocument() {
    const listeners = new Map();
    const doc = {
        byId: new Map(), activeElement: null,
        createElement: (tag) => new ElementStub(tag, doc),
        addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]); },
        removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter((f) => f !== fn)); },
        fire(type, event) { for (const fn of listeners.get(type) || []) fn(event); },
        count: (type) => (listeners.get(type) || []).length,
    };
    return doc;
}
const hoverWindow = (matches) => ({ matchMedia: (query) => ({ matches: matches && query === '(hover: hover) and (pointer: fine)' }) });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function mount({ hover = false, save, win } = {}) {
    const doc = stubDocument();
    const row = doc.createElement('div');
    row.className = 'chat-toolbar-row';
    row.getBoundingClientRect = () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });  // not laid out: no fit
    const toasts = [];
    const refreshes = [];
    const layouts = [];
    const saves = [];
    const control = createEffortRangeControl({
        row, doc, win: win === undefined ? hoverWindow(hover) : win,
        saveEffortRange: save || (async (triple) => { saves.push(triple); return { ok: true, effort_range: triple }; }),
        showToast: (message, tone) => toasts.push([message, tone]),
        refreshState: (force) => refreshes.push(force),
        onLayout: () => layouts.push(control?.isOpen?.()),
    });
    const el = control.el;
    const segs = el.querySelectorAll('.chat-effort-seg');
    // The strip has layout: seven 40px segments, so a boundary is x / 40.
    segs.forEach((seg, i) => { seg.offsetLeft = i * 40; seg.offsetWidth = 40; });
    const head = el.querySelector('.chat-effort-head');
    const strip = el.querySelector('[data-effort-strip]');
    const handles = { min: el.querySelector('.chat-effort-min'), rec: el.querySelector('.chat-effort-rec'), max: el.querySelector('.chat-effort-max') };
    const fire = (node, type, event) => { for (const fn of node.listeners.get(type) || []) fn(event); };
    const last = (node, type) => (node.listeners.get(type) || []).at(-1);
    const pointer = (clientX, { handle = null, pointerId = 1, clientY = 10 } = {}) => ({
        button: 0, clientX, clientY, pointerId, pointerType: 'mouse', preventDefault() {},
        target: { closest: (selector) => (selector === '[data-handle]' ? handle : null) },
    });
    return {
        control, el, row, doc, head, strip, handles, segs, toasts, refreshes, layouts, saves,
        headClick: (detail = 1) => fire(head, 'click', { detail }),
        key: (which, key) => fire(handles[which], 'keydown', { key, currentTarget: handles[which], preventDefault() {} }),
        down: (x, options) => fire(strip, 'pointerdown', pointer(x, options)),
        move: (x, pointerId = 1) => last(strip, 'pointermove')?.(pointer(x, { pointerId })),
        up: (x, pointerId = 1) => last(strip, 'pointerup')?.(pointer(x, { pointerId })),
        cancel: (pointerId = 1) => last(strip, 'pointercancel')?.(pointer(0, { pointerId })),
        enter: (pointerType = 'mouse') => fire(el, 'pointerenter', { pointerType }),
        // The stub control's rectangle is x 0..100, y 0..20; the default leave point is far outside the zone.
        leave: (pointerType = 'mouse', { x = 500, y = 500 } = {}) => fire(el, 'pointerleave', { pointerType, clientX: x, clientY: y }),
        docMove: (x, y) => doc.fire('pointermove', { pointerType: 'mouse', clientX: x, clientY: y }),
        outside: () => doc.fire('pointerdown', { target: new ElementStub('div', doc) }),
        inside: () => doc.fire('pointerdown', { target: head }),
        escape: () => doc.fire('keydown', { key: 'Escape' }),
        settle: () => control.pendingSave(),
    };
}

test('handle math: every handle pushes the ones it meets; min and max never cross', () => {
    const base = { min: 1, rec: 2, max: 3 };
    assert.deepEqual(applyHandle(base, 'rec', 5), { min: 1, rec: 5, max: 5 }, 'rec carries max up');
    assert.deepEqual(applyHandle(base, 'rec', 0), { min: 0, rec: 0, max: 3 }, 'rec carries min down');
    assert.deepEqual(applyHandle(base, 'rec', 9), { min: 1, rec: 6, max: 6 }, 'never past the last level');
    assert.deepEqual(applyHandle(base, 'rec', 3), { min: 1, rec: 3, max: 3 }, 'inside the range nothing else moves');
    assert.deepEqual(applyHandle(base, 'min', 3), { min: 3, rec: 3, max: 3 }, 'min pushes rec up');
    assert.deepEqual(applyHandle(base, 'min', 6), { min: 3, rec: 3, max: 3 }, 'min never passes max');
    assert.deepEqual(applyHandle(base, 'max', 1), { min: 1, rec: 1, max: 1 }, 'max pushes rec down');
    assert.deepEqual(applyHandle(base, 'max', -4), { min: 1, rec: 1, max: 1 }, 'max never passes min');
    assert.deepEqual(applyHandle(base, 'min', 0), { min: 0, rec: 2, max: 3 });
    assert.deepEqual(applyHandle(base, 'max', 6), { min: 1, rec: 2, max: 6 });
    assert.deepEqual(snapRange({ min: 0.6, rec: 0.2, max: 2.4 }), { min: 1, rec: 1, max: 2 }, 'a snap keeps the order');
    // A tap inside the range moves the recommended level, outside it the bracket on that side.
    assert.equal(nearestHandle(base, 2), 'rec');
    assert.equal(nearestHandle(base, 1), 'rec');
    assert.equal(nearestHandle(base, 0), 'min');
    assert.equal(nearestHandle(base, 6), 'max');
    // Coinciding levels: the brackets stay reachable from either side of the pill.
    assert.equal(nearestHandle({ min: 3, rec: 3, max: 3 }, 3), 'rec');
    assert.equal(nearestHandle({ min: 3, rec: 3, max: 3 }, 2), 'min');
    assert.equal(nearestHandle({ min: 3, rec: 3, max: 3 }, 4), 'max');
});

test('the control mounts after the pills, closed: ring, name and the recommended level only; three sliders out of the tab order', () => {
    const m = mount();
    assert.equal(m.el.dataset.open, 'false');
    assert.ok(m.el.classList.contains('chat-capsule'));
    assert.match(m.el.innerHTML, /class="chat-capsule-name">Effort</);
    // Closed, the capsule names only the recommended level: no range in words (the ring and the
    // button's name carry the range).
    assert.equal(m.el.querySelector('.chat-capsule-value').textContent, 'Medium');
    assert.equal(m.el.querySelector('.chat-capsule-value').dataset.i18nSkip, '', 'a catalog word the overlay leaves alone');
    assert.equal(m.el.getAttribute('data-effort-range'), '', 'the i18n DOM scope of the strip words');
    assert.equal(m.head.getAttribute('aria-expanded'), 'false');
    // The recommended level may go anywhere (it pushes the brackets); a bracket stops at the other one.
    const bounds = { min: ['0', '3'], rec: ['0', '6'], max: ['1', '6'] };
    for (const which of ['min', 'rec', 'max']) {
        assert.equal(m.handles[which].getAttribute('role'), 'slider');
        assert.equal(m.handles[which].tabIndex, -1);
        assert.deepEqual([m.handles[which].getAttribute('aria-valuemin'), m.handles[which].getAttribute('aria-valuemax')], bounds[which], which);
    }
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 3 }, 'the default before the first /api/state');
    assert.equal(m.el.dataset.known, 'false');
    assert.equal(m.handles.rec.getAttribute('aria-valuetext'), 'Medium');
    assert.match(m.head.title, /^Effort: Medium, range Low–High\. Applies to new work\.$/);
    assert.equal(m.head.getAttribute('aria-label'), m.head.title, 'the closed button says the state, not just "Effort"');
    assert.match(m.el.querySelector('[data-ring-band]').getAttribute('d'), /^M[\d.]+ [\d.]+ A70 70 0 0 1 /);
    assert.equal(m.segs.length, 7);
    // The strip is the seven levels alone: no title word of its own, no separator element, no reset button.
    for (const gone of ['.chat-effort-label', '.chat-effort-sep', '.chat-effort-reset']) assert.equal(m.el.querySelector(gone), null, gone);
    assert.deepEqual(m.segs.map((seg) => seg.dataset.in), ['false', 'true', 'true', 'true', 'false', 'false', 'false']);
    assert.deepEqual(m.segs.map((seg) => seg.dataset.rec), ['false', 'false', 'true', 'false', 'false', 'false', 'false']);
    assert.ok(m.row.children.includes(m.el));
});

test('/api/state is the one global value: it paints every composer, a stored minimal shows at Low', () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'minimal', recommended: 'high', max: 'ultra' } });
    assert.equal(m.el.dataset.known, 'true');
    assert.deepEqual(m.control.shown(), { min: 1, rec: 3, max: 6 });
    assert.deepEqual(m.control.stored(), { min: 'minimal', recommended: 'high', max: 'ultra' });
    assert.equal(m.handles.min.getAttribute('aria-valuetext'), 'Low');
    assert.equal(m.el.querySelector('.chat-capsule-value').textContent, 'High', 'the closed capsule follows the saved level');
    m.control.syncState({ context_mode: 'max' });
    assert.deepEqual(m.control.shown(), { min: 1, rec: 3, max: 6 }, 'a snapshot without the range changes nothing');
});

test('without a hovering pointer the press toggles; Enter/Space open and focus the recommended handle; Esc returns focus', () => {
    const m = mount({ hover: false });
    assert.equal(m.control.hoverCapable, false);
    assert.equal(m.el.listeners.get('pointerenter'), undefined, 'no hover listeners without the media');
    m.enter();
    assert.equal(m.control.isOpen(), false, 'a pointer arriving does not open');
    m.headClick(1);
    assert.equal(m.control.isOpen(), true);
    assert.equal(m.head.getAttribute('aria-expanded'), 'true');
    assert.equal(m.handles.rec.tabIndex, 0);
    assert.deepEqual(m.layouts, [true], 'the chat recomputes its composer reserve');
    assert.notEqual(m.doc.activeElement, m.handles.rec, 'a press does not steal focus');
    m.headClick(1);
    assert.equal(m.control.isOpen(), false);
    m.headClick(0);  // Enter / Space
    assert.equal(m.control.isOpen(), true);
    assert.equal(m.doc.activeElement, m.handles.rec);
    m.escape();
    assert.equal(m.control.isOpen(), false);
    assert.equal(m.doc.activeElement, m.head);
    assert.equal(m.handles.rec.tabIndex, -1);
    m.headClick(1);
    m.inside();
    assert.equal(m.control.isOpen(), true, 'a press inside keeps it open');
    m.outside();
    assert.equal(m.control.isOpen(), false, 'an outside press closes');
});

test('with a hovering pointer: opens after ~160 ms; closes 1 s after the mouse leaves a 32 px zone, unless pinned', async () => {
    const m = mount({ hover: true });
    assert.equal(m.control.hoverCapable, true);
    m.enter('touch');
    await sleep(200);
    assert.equal(m.control.isOpen(), false, 'a touch pointer never hover-opens');
    m.enter();
    assert.equal(m.control.isOpen(), false, 'not yet');
    await sleep(200);
    assert.equal(m.control.isOpen(), true);
    assert.equal(m.control.isPinned(), false);
    m.leave('mouse', { x: 120, y: 10 });   // 20 px right of the control: inside the zone
    await sleep(1150);
    assert.equal(m.control.isOpen(), true, 'a mouse that drifts into the zone keeps it open');
    m.docMove(200, 10);                    // 100 px away: the zone is left, the delay starts
    await sleep(500);
    assert.equal(m.control.isOpen(), true, 'not before the delay');
    await sleep(650);
    assert.equal(m.control.isOpen(), false, 'closed a second after leaving the zone');
    assert.equal(m.doc.count('pointermove'), 0, 'zone tracking stops once closed');
    m.enter();
    await sleep(200);
    m.leave();
    await sleep(300);
    m.docMove(110, 30);                    // back into the zone before the delay ran out
    await sleep(900);
    assert.equal(m.control.isOpen(), true, 'coming back cancels the close');
    m.enter();
    m.headClick(1);
    assert.equal(m.control.isPinned(), true, 'a press pins the hover-opened strip');
    m.leave();
    await sleep(1150);
    assert.equal(m.control.isOpen(), true, 'pinned: leaving does not close');
    m.headClick(1);
    assert.equal(m.control.isOpen(), false, 'a second press unpins and closes');
    m.enter();
    m.leave();
    await sleep(200);
    assert.equal(m.control.isOpen(), false, 'leaving before the delay cancels the open');
    m.headClick(1);
    assert.equal(m.control.isOpen(), true, 'a press on the closed button pins it open');
    assert.equal(m.control.isPinned(), true);
    m.escape();
    assert.equal(m.control.isOpen(), false);
});

test('a drag that ends inside the hover zone does not close the strip a second later', async () => {
    const m = mount({ hover: true });
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.enter();
    await sleep(200);
    m.down(100);
    m.up(110);                             // released 10 px past the control's right edge
    await m.settle();
    m.leave('mouse', { x: 110, y: 10 });
    await sleep(1150);
    assert.equal(m.control.isOpen(), true, 'the pointer is still within the zone');
    m.docMove(300, 10);
    await sleep(1150);
    assert.equal(m.control.isOpen(), false, 'leaving the zone closes it');
});

test('keyboard: arrows, Home, End, PageUp/PageDown move a handle, save the full triple on each step, and push neighbours', async () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.key('rec', 'ArrowRight');
    assert.deepEqual(m.control.shown(), { min: 1, rec: 3, max: 3 });
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'high', max: 'high' });
    m.key('rec', 'ArrowUp');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 1, rec: 4, max: 4 }, 'rec pushes max along');
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'xhigh', max: 'xhigh' });
    m.key('rec', 'ArrowDown');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 1, rec: 3, max: 4 }, 'a pushed bracket stays where it was pushed');
    const saved = m.saves.length;
    m.key('max', 'End');
    m.key('max', 'End');
    await m.settle();
    assert.equal(m.saves.length, saved + 1, 'an unchanged range saves nothing');
    m.key('max', 'End');
    m.key('min', 'Home');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'none', recommended: 'high', max: 'ultra' });
    m.key('min', 'PageUp');
    m.key('min', 'PageUp');
    m.key('min', 'PageUp');
    m.key('min', 'ArrowRight');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 4, rec: 4, max: 6 }, 'min pushes rec along');
    assert.deepEqual(m.saves.at(-1), { min: 'xhigh', recommended: 'xhigh', max: 'ultra' });
    m.key('max', 'PageDown');
    m.key('max', 'ArrowDown');
    m.key('max', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 4, rec: 4, max: 4 }, 'max comes down to min and never passes it');
    assert.equal(m.handles.max.getAttribute('aria-valuenow'), '4');
    assert.equal(m.handles.max.getAttribute('aria-valuetext'), 'X-High');
    assert.deepEqual(m.saves.at(-1), { min: 'xhigh', recommended: 'xhigh', max: 'xhigh' });
    assert.equal(m.refreshes.at(-1), true, 'every settled save re-reads /api/state for every composer');
});

test('a stored tier outside the owner tiers survives until the owner moves that handle', async () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'minimal', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.key('max', 'ArrowRight');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'minimal', recommended: 'medium', max: 'xhigh' }, 'min keeps its stored minimal');
    m.key('min', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'none', recommended: 'medium', max: 'xhigh' }, 'moved: rewritten to an owner tier');
    m.control.syncState({ effort_range: { min: 'minimal', recommended: 'medium', max: 'high' } });
    m.key('rec', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 1, rec: 1, max: 3 }, 'rec meets the shown minimum');
    assert.deepEqual(m.saves.at(-1), { min: 'minimal', recommended: 'low', max: 'high' }, 'the untouched minimum keeps minimal');
    m.key('rec', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 0, rec: 0, max: 3 }, 'one more step pushes the minimum');
    assert.deepEqual(m.saves.at(-1), { min: 'none', recommended: 'none', max: 'high' }, 'pushed: rewritten to an owner tier');
});

test('pointer: a press inside the range moves the recommended level, outside it the nearest bracket; the pill pushes brackets and they stay pushed', async () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(1);
    m.down(60);                       // inside: segment 1 -> rec
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 3 }, 'a preview is not a commit');
    assert.equal(m.handles.rec.dataset.dragging, 'true');
    assert.equal(m.el.dataset.dragging, 'false', 'a press that has not moved is still a tap: its marks animate');
    assert.equal(m.doc.activeElement, m.handles.rec);
    m.move(180);                      // segment 4: the pill carries max along
    assert.equal(m.el.dataset.dragging, 'true', 'once the pointer moves, the marks follow it without animating');
    assert.equal(m.handles.max.getAttribute('aria-valuenow'), '4', 'the preview shows the pushed bracket');
    assert.equal(m.handles.rec.style.left, '160px', 'the pill sits on a whole level while dragging');
    m.move(100);                      // back to segment 2
    assert.equal(m.handles.max.getAttribute('aria-valuenow'), '4', 'no spring: a pushed bracket stays pushed');
    m.up(100);
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 4 });
    assert.equal(m.handles.rec.dataset.dragging, 'false');
    assert.equal(m.el.dataset.dragging, 'false', 'a tap or a key animates again');
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'medium', max: 'xhigh' });
    m.down(250);                      // outside above: segment 6 -> max
    m.up(250);
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'medium', max: 'ultra' });
    m.down(10);                       // outside below: segment 0 -> min
    m.up(10);
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'none', recommended: 'medium', max: 'ultra' });
    // Grab the max bracket (boundary x=280 is level 7-1 = 6) and drag it by two segments: the
    // offset between the grab point and the bracket is kept, so the bracket lands at 4.
    m.down(285, { handle: m.handles.max });
    assert.equal(m.handles.max.dataset.dragging, 'true');
    m.move(205);
    m.up(205);
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 0, rec: 2, max: 4 });
    assert.deepEqual(m.saves.at(-1), { min: 'none', recommended: 'medium', max: 'xhigh' });
    // pointercancel cancels the preview, a pushed bracket included, without a save.
    const before = m.saves.length;
    m.down(100);
    m.move(270);
    assert.equal(m.handles.max.getAttribute('aria-valuenow'), '6');
    m.cancel();
    assert.equal(m.handles.rec.dataset.dragging, 'false');
    await m.settle();
    assert.equal(m.saves.length, before);
    assert.deepEqual(m.control.shown(), { min: 0, rec: 2, max: 4 });
    assert.equal(m.handles.max.getAttribute('aria-valuenow'), '4', 'the bracket is back where it was saved');
    // A second pointer's events are ignored while the first drags; a press on the recommended
    // level's own segment released there changes nothing and saves nothing.
    m.down(100);
    m.move(200, 2);
    m.up(200, 2);
    assert.equal(m.handles.rec.dataset.dragging, 'true');
    m.up(100);
    await m.settle();
    assert.equal(m.saves.length, before, 'released where it was pressed: no change, no save');
});

test('a strip that scrolls keeps the handle that moved in view, clear of the fade, and says where it continues', () => {
    const m = mount();                // seven 40 px segments: the strip's content is 280 px
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.el.dataset.fit = 'overflow';    // what fit() sets when even the tightest padding does not fit
    Object.assign(m.strip, { clientWidth: 200, scrollWidth: 280, scrollLeft: 0 });
    m.handles.min.getBoundingClientRect = () => ({ width: 8 });
    m.key('max', 'End');              // the max bracket goes to Ultra: 272..280, past the 200 px view
    assert.equal(m.strip.scrollLeft, 280 + 12 - 200, 'the whole bracket plus the fade margin is in view');
    assert.equal(m.strip.dataset.scroll, 'end', 'scrolled to the end: only the start fades');
    m.key('max', 'Home');             // max comes down to min (Low): 72..80
    assert.equal(m.strip.scrollLeft, 72 - 12);
    assert.equal(m.strip.dataset.scroll, 'middle', 'levels hide on both sides: both fade');
    m.key('min', 'Home');             // min goes to None: 0..8
    assert.equal(m.strip.scrollLeft, 0);
    assert.equal(m.strip.dataset.scroll, 'start');
});

test('hover opens only where the button stays under the mouse; alone or scrolling, the press opens it', async () => {
    const doc = stubDocument();
    const row = doc.createElement('div');
    row.className = 'chat-toolbar-row';
    const pills = doc.createElement('div');
    pills.className = 'chat-composer-pills';
    const swarm = doc.createElement('button');
    swarm.getBoundingClientRect = () => ({ width: 220 });
    pills.appendChild(swarm);
    row.appendChild(pills);
    const styles = new Map([[row, { paddingLeft: '8px', paddingRight: '8px', columnGap: '8px' }]]);
    const win = { ...hoverWindow(true), getComputedStyle: (node) => styles.get(node) || {} };
    const control = createEffortRangeControl({ row, doc, win, saveEffortRange: async (triple) => ({ effort_range: triple }), showToast: () => {} });
    const el = control.el;
    el.querySelector('.chat-effort-head').getBoundingClientRect = () => ({ width: 30 });
    el.querySelectorAll('.chat-effort-seg').forEach((seg) => {
        Object.defineProperty(seg, 'firstElementChild', { value: { getBoundingClientRect: () => ({ width: 32 }) } });
    });
    const enter = () => el.listeners.get('pointerenter').forEach((fn) => fn({ pointerType: 'mouse' }));
    const leave = () => el.listeners.get('pointerleave').forEach((fn) => fn({ pointerType: 'mouse', clientX: 900, clientY: 900 }));
    row.getBoundingClientRect = () => ({ width: 440 });  // a Project pane: the strip would stand alone
    enter();
    await sleep(220);
    assert.equal(control.isOpen(), false, 'no hover-open where the pills would step aside under the mouse');
    leave();
    row.getBoundingClientRect = () => ({ width: 900 });  // room beside the pills: the button stays put
    enter();
    await sleep(220);
    assert.equal(control.isOpen(), true, 'hover opens beside the pills');
    control.destroy();
});

test('a mouse press on a level does not keep a hover-opened strip from closing; keyboard focus does', async () => {
    const m = mount({ hover: true });
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.enter();
    await sleep(200);
    m.doc.fire('pointerdown', { target: m.head });  // the document sees every press first
    m.down(100);
    m.up(100);
    await m.settle();
    assert.equal(m.doc.activeElement, m.handles.rec, 'the press focused the handle');
    m.leave('mouse', { x: 600, y: 10 });
    await sleep(1150);
    assert.equal(m.control.isOpen(), false, 'a mouse press does not hold the strip open');
    m.enter();
    await sleep(200);
    m.doc.fire('keydown', { key: 'ArrowRight' });   // the last input was the keyboard
    m.key('rec', 'ArrowRight');
    await m.settle();
    m.handles.rec.focus();
    m.leave('mouse', { x: 600, y: 10 });
    await sleep(1150);
    assert.equal(m.control.isOpen(), true, 'keyboard focus inside keeps it open');
    m.escape();
});

test('fit: beside the other controls when the strip fits there, alone when it does not, its name giving way, scrolling last', () => {
    const doc = stubDocument();
    const row = doc.createElement('div');
    row.className = 'chat-toolbar-row';
    const pills = doc.createElement('div');
    pills.className = 'chat-composer-pills';
    const swarm = doc.createElement('button');
    const context = doc.createElement('div');
    context.className = 'chat-context-mode';
    const sized = (node, width) => { node.getBoundingClientRect = () => ({ width }); };
    sized(swarm, 120);
    sized(context, 100);              // the other capsule, closed
    pills.appendChild(swarm);
    row.appendChild(pills);
    row.appendChild(context);
    const styles = new Map([
        [row, { paddingLeft: '8px', paddingRight: '8px', columnGap: '8px' }],
        [pills, { columnGap: '6px' }],
    ]);
    const resize = [];
    const win = {
        ...hoverWindow(false), getComputedStyle: (node) => styles.get(node) || {},
        addEventListener: (type, fn) => { if (type === 'resize') resize.push(fn); }, removeEventListener: () => {},
    };
    const control = createEffortRangeControl({
        row, doc, win, saveEffortRange: async (triple) => ({ effort_range: triple }), showToast: () => {},
    });
    const el = control.el;
    assert.equal(row.children.indexOf(el), 2, 'after the pills and the context capsule');
    const q = (selector) => el.querySelector(selector);
    styles.set(el, { borderLeftWidth: '1px', borderRightWidth: '1px' });
    styles.set(q('[data-effort-strip]'), { marginRight: '9.33px' });  // the end gutter
    styles.set(q('.chat-capsule-name'), { marginLeft: '-2px', marginRight: '8px' });
    styles.set(q('.chat-capsule-body'), { width: '1px', marginRight: '4px' });  // its ::before, the divider
    sized(q('.chat-capsule-glyph'), 30);
    sized(q('.chat-capsule-name'), 36);
    // Fractional words: whole-pixel offsets would lose almost a pixel here.
    [31.41, 24.19, 48.08, 28.19, 42.11, 24.94, 29.8].forEach((width, i) => {
        Object.defineProperty(el.querySelectorAll('.chat-effort-seg')[i], 'firstElementChild', { value: { getBoundingClientRect: () => ({ width }) } });
    });
    const head = q('.chat-effort-head');
    const open = (width) => { sized(row, width); head.listeners.get('click').forEach((fn) => fn({ detail: 1 })); };
    const close = () => head.listeners.get('click').forEach((fn) => fn({ detail: 1 }));
    const facts = () => [el.dataset.fit, el.style.getPropertyValue('--capsule-seg-pad'), el.dataset.tight, el.dataset.label, row.dataset.ownerSolo];
    // Words 228.72 and 14 paddings; the gutter and the borders 11.33; the head 30 bare, 77 with the
    // name and the divider; beside it Swarm 120 and the context capsule 100, a row gap each (236).
    open(700);
    assert.deepEqual(facts(), ['inline', '8px', 'false', 'true', undefined], 'the owner row: beside the others at full padding');
    close();
    assert.deepEqual([el.dataset.fit, row.dataset.ownerSolo], [undefined, undefined], 'closing restores the others');
    assert.deepEqual([el.style.getPropertyValue('--capsule-seg-pad'), el.dataset.tight], ['8px', 'false'],
        'the fitted padding stays while the strip collapses (the next open fits again)');
    open(640);
    assert.deepEqual(facts(), ['inline', '4px', 'true', 'true', undefined], 'beside them, the padding tightens to var(--space-1)');
    close();
    open(560);
    assert.deepEqual(facts(), ['solo', '8px', 'false', 'true', 'effort'], 'no room beside them: they step aside');
    sized(swarm, 0);                  // hidden controls measure 0: the width seen before hiding stays
    sized(context, 0);
    resize.forEach((fn) => fn());
    assert.deepEqual(facts(), ['solo', '8px', 'false', 'true', 'effort'], 'a resize while alone keeps them aside');
    close();
    sized(swarm, 120);
    sized(context, 100);
    open(390);
    assert.deepEqual(facts(), ['solo', '3px', 'true', 'true', 'effort'], 'alone, the name stays down to 3 px');
    close();
    open(350);
    assert.deepEqual(facts(), ['solo', '4px', 'true', 'false', 'effort'], 'a phone: the name and the divider give way first');
    // Open, the line narrows until the strip scrolls: it shows what opening shows (the recommended
    // level, Medium at 80..120 in a 100 px view); once it scrolls, a relayout keeps the owner's scroll.
    const strip = q('[data-effort-strip]');
    el.querySelectorAll('.chat-effort-seg').forEach((seg, i) => { seg.offsetLeft = i * 40; seg.offsetWidth = 40; });
    Object.assign(strip, { clientWidth: 100, scrollLeft: 0 });
    sized(row, 320);
    resize.forEach((fn) => fn());
    assert.deepEqual([el.dataset.fit, strip.scrollLeft, strip.dataset.scroll], ['overflow', 120 + 12 - 100, 'middle']);
    strip.scrollLeft = 0;             // the owner swipes back to None
    resize.forEach((fn) => fn());
    assert.deepEqual([el.dataset.fit, strip.scrollLeft, strip.dataset.scroll], ['overflow', 0, 'start'], 'the owner\'s scroll stays');
    close();
    open(350);
    control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'ultra' } });
    q('.chat-effort-max').focus();
    strip.scrollLeft = 0;
    sized(row, 320);
    resize.forEach((fn) => fn());
    assert.equal(strip.scrollLeft, 280 + 12 - 100, 'new overflow reveals the focused bracket, not the recommended pill');
    close();
    doc.activeElement = null;
    open(320);
    assert.deepEqual(facts(), ['overflow', '3px', 'true', 'false', 'effort'], 'even that does not fit: the strip scrolls');
    close();
    assert.equal(row.dataset.ownerSolo, undefined);
    control.destroy();
});

test('saves are serialized, last wins; Send can wait for them; a refused save returns to the server value and says why', async () => {
    const pending = [];
    const m = mount({
        save: (triple) => new Promise((resolve, reject) => pending.push({ triple, resolve, reject })),
    });
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    assert.equal(m.control.hasPendingSave(), false);
    m.key('rec', 'ArrowRight');
    assert.equal(m.control.hasPendingSave(), true);
    assert.equal(m.el.dataset.saving, 'true');
    m.key('rec', 'ArrowLeft');
    m.key('max', 'ArrowRight');
    assert.equal(pending.length, 1, 'one request in flight; later gestures wait');
    assert.deepEqual(pending[0].triple, { min: 'low', recommended: 'high', max: 'high' });
    m.control.syncState({ effort_range: { min: 'none', recommended: 'none', max: 'none' } });
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 4 }, 'a stale poll never overwrites a save in flight');
    const waiter = m.control.pendingSave();
    pending[0].resolve({ ok: true, effort_range: pending[0].triple });
    await sleep(0);
    assert.equal(pending.length, 2, 'the latest queued triple follows, the superseded one never leaves');
    assert.deepEqual(pending[1].triple, { min: 'low', recommended: 'medium', max: 'xhigh' });
    pending[1].resolve({ ok: true, effort_range: pending[1].triple });
    assert.equal(await waiter, true);
    assert.equal(m.control.hasPendingSave(), false);
    assert.equal(m.el.dataset.saving, 'false');
    assert.deepEqual(m.control.stored(), { min: 'low', recommended: 'medium', max: 'xhigh' });
    assert.deepEqual(m.refreshes, [true]);
    // A refusal: the server's sentence as a toast, the control back at the server's value.
    m.key('min', 'ArrowLeft');
    assert.deepEqual(m.control.shown(), { min: 0, rec: 2, max: 4 });
    const refused = m.control.pendingSave();
    pending[2].reject(Object.assign(new Error('Effort range must be ordered: minimum at most the recommended level.'), { status: 400 }));
    assert.equal(await refused, false);
    assert.deepEqual(m.toasts, [['Effort range must be ordered: minimum at most the recommended level.', 'error']]);
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 4 }, 'the unsaved preview is gone');
    assert.deepEqual(m.control.stored(), { min: 'low', recommended: 'medium', max: 'xhigh' });
    assert.equal(await m.control.pendingSave(), false, 'the last outcome stays readable');
});

test('an edit made while saves are on their way builds on the latest choice, never on an older answer', async () => {
    const pending = [];
    const m = mount({ save: (triple) => new Promise((resolve, reject) => pending.push({ triple, resolve, reject })) });
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.key('min', 'Home');        // in flight: none · medium · high
    m.key('rec', 'ArrowRight');  // queued:    none · high · high
    pending[0].resolve({ ok: true, effort_range: pending[0].triple });
    await sleep(0);
    assert.deepEqual(m.control.shown(), { min: 0, rec: 3, max: 3 }, 'the older answer does not pull the draft back');
    m.key('max', 'End');         // while the second save is in flight
    pending[1].resolve({ ok: true, effort_range: pending[1].triple });
    await sleep(0);
    pending[2].resolve({ ok: true, effort_range: pending[2].triple });
    assert.equal(await m.control.pendingSave(), true);
    assert.deepEqual(pending.map((p) => p.triple), [
        { min: 'none', recommended: 'medium', max: 'high' },
        { min: 'none', recommended: 'high', max: 'high' },
        { min: 'none', recommended: 'high', max: 'ultra' },
    ]);
    assert.deepEqual(m.control.stored(), { min: 'none', recommended: 'high', max: 'ultra' });
    assert.deepEqual(m.control.shown(), { min: 0, rec: 3, max: 6 });
    // A refusal drops edits queued on top of it and returns to the server's value.
    m.key('rec', 'ArrowLeft');
    m.key('rec', 'ArrowLeft');
    pending[3].reject(new Error('Could not save.'));
    assert.equal(await m.control.pendingSave(), false);
    assert.equal(pending.length, 4, 'the edit queued on the refused draft never leaves');
    assert.deepEqual(m.control.shown(), { min: 0, rec: 3, max: 6 });
});

test('destroy removes the element, its document listeners and its timers', async () => {
    const m = mount({ hover: true });
    assert.equal(m.doc.count('pointerdown'), 1);
    assert.equal(m.doc.count('keydown'), 1);
    m.enter();
    m.control.destroy();
    assert.equal(m.doc.count('pointerdown'), 0);
    assert.equal(m.doc.count('keydown'), 0);
    assert.ok(!m.row.children.includes(m.el));
    await sleep(220);
    assert.equal(m.control.isOpen(), false, 'the hover timer died with the control');
});

/* Both capsules in one composer row, as chat.js builds it: the pills, then the context anchor. */
function mountPair({ hover = false, reply } = {}) {
    const doc = stubDocument();
    const row = doc.createElement('div');
    row.className = 'chat-toolbar-row';
    row.getBoundingClientRect = () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });  // not laid out: no fit
    const pills = doc.createElement('div');
    pills.className = 'chat-composer-pills';
    row.appendChild(pills);
    const contextEl = doc.createElement('div');
    contextEl.className = 'chat-context-mode';
    contextEl.dataset.contextMode = 'max';
    row.appendChild(contextEl);
    const posts = [];
    const toasts = [];
    const refreshes = [];
    const answer = { value: reply || { ok: true } };
    const controls = createComposerOwnerControls({
        row, doc, win: hoverWindow(hover), byId: (suffix) => (suffix === 'context-mode' ? contextEl : null),
        apiFetch: async (url, init) => { posts.push([url, JSON.parse(init.body)]); return answer.value; },
        saveEffortRange: async (triple) => ({ effort_range: triple }),
        showToast: (message, tone) => toasts.push([message, tone]),
        refreshState: (force) => refreshes.push(force),
    });
    const fire = (node, type, event) => Promise.all((node.listeners.get(type) || []).map((fn) => fn(event)));
    const effortEl = controls.effort.el;
    const seg = (mode) => contextEl.querySelectorAll('.chat-seg').find((node) => node.dataset.mode === mode);
    return {
        controls, row, doc, contextEl, effortEl, posts, toasts, refreshes, answer, seg,
        contextHead: contextEl.querySelector('.chat-context-head'), effortHead: effortEl.querySelector('.chat-effort-head'),
        press: (head, detail = 1) => fire(head, 'click', { detail }),
        choose: (mode) => fire(contextEl, 'click', { target: { closest: (selector) => (selector === '.chat-seg' ? seg(mode) : null) } }),
        key: (mode, key) => fire(contextEl.querySelector('.chat-context-strip'), 'keydown', {
            key, preventDefault() {}, target: { closest: (selector) => (selector === '.chat-seg' ? seg(mode) : null) },
        }),
        docDown: (target) => doc.fire('pointerdown', { target }),
    };
}

test('the context capsule: ring, Context and the mode; it opens into exactly three modes, one tab stop among them', async () => {
    const m = mountPair();
    assert.ok(m.contextEl.classList.contains('chat-capsule'));
    assert.equal(m.contextEl.getAttribute('aria-label'), 'Context size mode');
    assert.match(m.contextEl.innerHTML, /class="chat-capsule-name">Context</);
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Max');
    assert.match(m.contextHead.title, /^Context mode \(owner setting\)\. Nano is the compact owner window/);
    const segs = m.contextEl.querySelectorAll('.chat-seg');
    // Three modes, a position among them, never a fill level: three arcs and three radios.
    assert.deepEqual(segs.map((node) => node.dataset.mode), ['nano', 'low', 'max']);
    assert.equal(m.contextEl.querySelectorAll('.chat-context-arc').length, 3);
    assert.ok(m.contextEl.querySelectorAll('[data-mode]').every((node) => node.classList.contains('chat-seg')),
        'inside the control [data-mode] still means a mode button: the arcs carry data-arc');
    assert.deepEqual(segs.map((node) => node.getAttribute('aria-checked')), ['false', 'false', 'true']);
    assert.deepEqual(segs.map((node) => node.tabIndex), [-1, -1, -1], 'closed: out of the tab order');
    await m.press(m.contextHead, 0);  // Enter / Space
    assert.equal(m.contextEl.dataset.open, 'true');
    assert.equal(m.contextHead.getAttribute('aria-expanded'), 'true');
    assert.deepEqual(segs.map((node) => node.tabIndex), [-1, -1, 0], 'one tab stop: the chosen mode');
    assert.equal(m.doc.activeElement, m.seg('max'), 'the keyboard lands on the chosen mode');
    m.doc.fire('keydown', { key: 'Escape' });
    assert.equal(m.contextEl.dataset.open, 'false');
    assert.equal(m.doc.activeElement, m.contextHead, 'Esc returns focus to the head');
    m.controls.syncState({ context_mode: 'nano' });
    assert.equal(m.contextEl.dataset.contextMode, 'nano', 'every composer shows the one global mode');
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Nano');
    m.controls.syncState({ context_mode: 'huge' });
    assert.equal(m.contextEl.dataset.contextMode, 'max', 'an unknown mode reads as Max');
    m.controls.destroy();
});

test('a context choice posts the owner endpoint at once, waits for the answer, shows a refusal and always re-reads /api/state', async () => {
    const m = mountPair();
    await m.press(m.contextHead);
    await m.choose('low');
    assert.deepEqual(m.posts, [['/api/owner/context-mode', { mode: 'low' }]]);
    assert.equal(m.contextEl.dataset.contextMode, 'low');
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Low');
    assert.equal(m.seg('low').getAttribute('aria-checked'), 'true');
    assert.equal(m.contextEl.dataset.disabled, 'false');
    assert.deepEqual(m.refreshes, [true]);
    await m.choose('low');
    assert.equal(m.posts.length, 1, 'the current mode is not re-posted');
    m.answer.value = { ok: false, json: async () => ({ error: 'Context mode can only be lowered while Ouroboros is idle.' }) };
    await m.choose('max');
    assert.equal(m.contextEl.dataset.contextMode, 'low', 'a refusal leaves the saved mode');
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Low');
    assert.deepEqual(m.toasts, [['Context mode can only be lowered while Ouroboros is idle.', 'error']]);
    assert.deepEqual(m.refreshes, [true, true]);
    // While a save is on its way the strip waits: a second choice and a stale poll change nothing.
    let release;
    m.answer.value = new Promise((resolve) => { release = resolve; });
    const pending = m.choose('nano');
    assert.equal(m.contextEl.dataset.disabled, 'true');
    await m.choose('max');
    m.controls.syncState({ context_mode: 'max' });
    assert.equal(m.contextEl.dataset.contextMode, 'low', 'the save answers for itself, not a stale poll');
    release({ ok: true });
    await pending;
    assert.deepEqual(m.posts.map(([, body]) => body.mode), ['low', 'max', 'nano']);
    assert.equal(m.contextEl.dataset.contextMode, 'nano');
    // Arrows move to a neighbour and choose it; Home and End go to the ends.
    m.answer.value = { ok: true };
    await m.key('nano', 'ArrowRight');
    assert.equal(m.doc.activeElement, m.seg('low'));
    assert.equal(m.contextEl.dataset.contextMode, 'low');
    await m.key('low', 'End');
    assert.equal(m.contextEl.dataset.contextMode, 'max');
    await m.key('max', 'ArrowDown');
    assert.equal(m.posts.length, 5, 'past the end nothing moves and nothing is posted');
    assert.equal(m.controls.hasPendingSave(), false, 'Send waits for the effort save only');
    assert.equal(await m.controls.pendingSave(), true);
    m.controls.destroy();
});

test('a context strip that scrolls shows the chosen mode whole on opening, and the mode an arrow, Home or End reaches', async () => {
    const m = mountPair();
    const strip = m.contextEl.querySelector('.chat-context-strip');
    // Long translated words on a phone: three 90 px modes in a 160 px view.
    m.contextEl.querySelectorAll('.chat-seg').forEach((seg, i) => { seg.offsetLeft = i * 90; seg.offsetWidth = 90; });
    Object.assign(strip, { clientWidth: 160, scrollLeft: 0 });
    m.contextEl.dataset.fit = 'overflow';  // what fit() sets when even the tightest padding does not fit
    await m.press(m.contextHead, 0);
    assert.equal(strip.scrollLeft, 270 + 12 - 160, 'opening shows Max, the last mode, whole and clear of the fade');
    assert.equal(strip.dataset.scroll, 'end');
    await m.key('max', 'Home');
    assert.deepEqual([m.contextEl.dataset.contextMode, strip.scrollLeft, strip.dataset.scroll], ['nano', 0, 'start']);
    await m.key('nano', 'ArrowRight');
    assert.deepEqual([m.contextEl.dataset.contextMode, strip.scrollLeft, strip.dataset.scroll], ['low', 180 + 12 - 160, 'middle']);
    await m.key('low', 'End');
    assert.deepEqual([m.contextEl.dataset.contextMode, strip.scrollLeft, strip.dataset.scroll], ['max', 270 + 12 - 160, 'end']);
    m.controls.destroy();
});

test('one capsule is open at a time: opening one closes the other at once, a narrow solo strip included', async () => {
    const m = mountPair();
    await m.press(m.contextHead);
    assert.equal(m.contextEl.dataset.open, 'true');
    // The press on the other capsule is its own to answer: the document handler leaves it alone,
    // and opening Effort closes Context at once.
    m.docDown(m.effortHead);
    assert.equal(m.contextEl.dataset.open, 'true');
    await m.press(m.effortHead);
    assert.deepEqual([m.contextEl.dataset.open, m.effortEl.dataset.open], ['false', 'true']);
    await m.press(m.contextHead);
    assert.deepEqual([m.contextEl.dataset.open, m.effortEl.dataset.open], ['true', 'false']);
    // A narrow line: Effort stands alone (the pills and Context step aside); once it closes and
    // Context opens, Context is the one alone, never hidden by Effort's flag.
    m.row.dataset.ownerSolo = 'effort';
    m.effortEl.dataset.fit = 'solo';
    await m.press(m.effortHead);
    assert.equal(m.effortEl.dataset.open, 'true');
    m.row.dataset.ownerSolo = 'effort';
    m.effortEl.dataset.fit = 'solo';
    m.docDown(new ElementStub('div', m.doc));
    assert.equal(m.effortEl.dataset.open, 'false');
    assert.equal(m.row.dataset.ownerSolo, undefined, 'closing gives the line back to every control');
    await m.press(m.contextHead);
    assert.equal(m.contextEl.dataset.open, 'true');
    assert.notEqual(m.row.dataset.ownerSolo, 'effort', 'Effort\'s flag never hides the capsule that opened');
    m.controls.destroy();
});

test('hover never opens a capsule while the other one is open', async () => {
    const m = mountPair({ hover: true });
    await m.press(m.effortHead);  // pinned open
    for (const fn of m.contextEl.listeners.get('pointerenter')) fn({ pointerType: 'mouse' });
    await sleep(220);
    assert.deepEqual([m.contextEl.dataset.open, m.effortEl.dataset.open], ['false', 'true']);
    await m.press(m.effortHead);  // unpins and closes
    for (const fn of m.contextEl.listeners.get('pointerenter')) fn({ pointerType: 'mouse' });
    await sleep(220);
    assert.equal(m.contextEl.dataset.open, 'true', 'with the other closed, hover opens it');
    m.controls.destroy();
});

test('closed, the capsules keep their names while the line holds them; a narrower line keeps rings and values', () => {
    const m = mountPair();
    const line = (top) => { m.effortEl.getBoundingClientRect = () => ({ top, bottom: top + 32, width: 130 }); };
    line(40);                         // the names push Effort onto a second line
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, 'true');
    line(0);
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, undefined, 'room again: the names come back');
    line(40);
    m.contextHead.listeners.get('click').forEach((fn) => fn({ detail: 1 }));
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, undefined, 'never decided while a strip is open');
    m.controls.destroy();
});
