// The row of capsules folds while the owner reads back through a room's history (composer_fold.js, owner 19.10 2A):
// a run of reading back folds it and ☰ lands at the field's middle; the way down, the latest, the field's focus, an open
// capsule, another layout and the end of the room bring it back; older history added above and a hidden typing row
// move nothing. Real frames are in the browser lane (tests/test_composer_fold_browser.py).
import assert from 'node:assert/strict';
import test from 'node:test';
import { FOLD, createComposerFold } from '../modules/composer_fold.js';

function rig({ stack = true } = {}) {
    const timers = new Map();
    let timerId = 0;
    const listeners = new Map();
    const on = (target) => (type, fn) => listeners.set(`${target}:${type}`, fn);
    const off = (target) => (type, fn) => { if (listeners.get(`${target}:${type}`) === fn) listeners.delete(`${target}:${type}`); };
    // The newest row lies 90px under the visible bottom: the owner has read back a little already.
    const newest = { bottom: 900, getClientRects: () => [1], getBoundingClientRect() { return { bottom: this.bottom }; } };
    const typing = { getClientRects: () => [], getBoundingClientRect: () => ({ bottom: 0 }), previousElementSibling: newest };
    const feed = {
        lastElementChild: typing, scrollHeight: 6000, clientHeight: 810, scrollTop: 5190,
        getBoundingClientRect: () => ({ bottom: 810 }), addEventListener: on('feed'), removeEventListener: off('feed'),
    };
    const field = { getBoundingClientRect: () => ({ top: 725, height: 52 }) };
    const menu = { getBoundingClientRect: () => ({ top: 685, height: 32 }) };
    const props = new Map();
    const row = {
        dataset: {}, open: false,
        getBoundingClientRect: () => ({ top: 685, height: 32 }),
        querySelector(selector) {
            if (selector.includes('composer-menu')) return menu;
            if (selector.includes('data-open')) return this.open ? {} : null;
            return null;
        },
    };
    const wrap = {
        dataset: {}, offsetHeight: 120, inside: new Set([field]),
        style: { setProperty: (k, v) => props.set(k, v), removeProperty: (k) => props.delete(k) },
        contains(el) { return this.inside.has(el); },
        querySelector: (selector) => (selector === '.chat-text-row' ? field : null),
        addEventListener: on('wrap'), removeEventListener: off('wrap'),
    };
    const keys = new Set();
    const doc = { activeElement: null, body: { classList: { contains: (c) => keys.has(c) } } };
    const win = {
        requestAnimationFrame: (fn) => { fn(); return 0; },
        cancelAnimationFrame: () => {},
        setTimeout: (fn, ms) => { timerId += 1; timers.set(timerId, fn); return timerId; },
        clearTimeout: (id) => { timers.delete(id); },
    };
    const env = { stack, layout: null };
    const fold = createComposerFold({ feed, wrap, row, win, doc, isStack: () => env.stack,
        onLayout: (fn) => { env.layout = fn; return () => { env.layout = null; }; } });
    // The owner's finger moves the feed: reading back brings the newest row further down and the scroll off its end.
    const scroll = (down, by = 30) => {
        newest.bottom += down ? by : -by;
        feed.scrollTop -= down ? by : -by;
        listeners.get('feed:scroll')?.();
    };
    const runTimers = () => { for (const [id, fn] of [...timers]) { timers.delete(id); fn(); } };
    return { fold, feed, wrap, row, field, newest, props, doc, keys, env, listeners, scroll, runTimers };
}

test('reading back folds the row, ☰ lands at the field\'s middle, and the way down brings it back', () => {
    const r = rig();
    r.scroll(true);  // the first read is the baseline
    r.scroll(true);
    assert.equal('folded' in r.wrap.dataset, false, 'one step of 30px is not a run');
    r.scroll(true);
    assert.equal(r.fold.folded(), true, 'a run of 60px folds it');
    assert.equal('folded' in r.wrap.dataset, true);
    assert.equal(r.props.get('--fold-row-h'), '32px', 'the line collapses from its own height');
    assert.equal(r.props.get('--fold-menu-dy'), `${52 / 2 - 16}px`, 'down by half the field less its own centre on the line');
    r.scroll(false);
    assert.equal(r.fold.folded(), true, 'one step back is not a run');
    r.scroll(false);
    assert.equal(r.fold.folded(), false, 'a run toward the latest brings it back');
    assert.equal('fold' in r.wrap.dataset, true, 'and holds its height until the motion is over');
    r.runTimers();
    assert.equal('fold' in r.wrap.dataset, false);
    assert.equal(r.props.size, 0, 'then lets go of it');
    assert.ok(FOLD.run <= 60 && FOLD.away < 90, 'the fixture reads past both bounds');
});

test('at the latest the row stays; a wobble, older history above and the hidden typing row move nothing', () => {
    const r = rig();
    r.feed.scrollTop = r.feed.scrollHeight - r.feed.clientHeight - 2;  // at the end
    for (let i = 0; i < 4; i += 1) { r.newest.bottom += 30; r.listeners.get('feed:scroll')(); }
    assert.equal(r.fold.folded(), false, 'at the latest the row is always there');
    r.feed.scrollTop = 4000;
    r.scroll(true, 0);
    for (let i = 0; i < 6; i += 1) r.scroll(i % 2 === 0, 20);
    assert.equal(r.fold.folded(), false, 'reading back and forth by a little never folds it');
    // Older history prepended above: the scroll position jumps, the newest row stays where it was.
    for (let i = 0; i < 3; i += 1) { r.feed.scrollTop += 900; r.feed.scrollHeight += 900; r.listeners.get('feed:scroll')(); }
    assert.equal(r.fold.folded(), false, 'history added above moves nothing');
    r.scroll(true, 50);
    assert.equal(r.fold.folded(), true, 'the reader is read through the hidden typing row, from the newest row with a box');
});

test('while the owner writes or works a capsule the row stays, and focusing the field brings it back', () => {
    const r = rig();
    r.keys.add('keyboard-open');
    for (let i = 0; i < 4; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), false, 'writing (the keyboard up): the row stays');
    r.keys.delete('keyboard-open');
    r.doc.activeElement = r.field;
    for (let i = 0; i < 2; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), true, 'a field that merely holds the focus, with no keyboard, is not writing');
    r.listeners.get('wrap:focusin')();
    assert.equal(r.fold.folded(), false);
    r.doc.activeElement = null;
    r.row.dataset.ownerSolo = 'effort';
    for (let i = 0; i < 4; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), false, 'a strip open alone: the row stays');
    delete r.row.dataset.ownerSolo;
    for (let i = 0; i < 4; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), true);
    r.listeners.get('wrap:focusin')();
    assert.equal(r.fold.folded(), false, 'the field\'s focus brings it back');
    for (let i = 0; i < 4; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), true);
    r.listeners.get('wrap:pointerdown')();
    assert.equal(r.fold.folded(), false, 'and so does a press on the composer, the focused field included');
});

test('only on a phone; another layout brings the row back; the end of the room clears it', () => {
    const docked = rig({ stack: false });
    for (let i = 0; i < 4; i += 1) docked.scroll(true);
    assert.equal(docked.fold.folded(), false, 'a desk never folds it');
    const r = rig();
    for (let i = 0; i < 4; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), true);
    r.env.layout();
    assert.equal(r.fold.folded(), false, 'a layout change brings it back');
    for (let i = 0; i < 4; i += 1) r.scroll(true);
    assert.equal(r.fold.folded(), true);
    r.fold.destroy();
    assert.equal('folded' in r.wrap.dataset || 'fold' in r.wrap.dataset, false);
    assert.equal(r.props.size, 0);
    assert.equal(['feed:scroll', 'wrap:focusin', 'wrap:pointerdown'].some((k) => r.listeners.has(k)), false, 'no listener stays');
    assert.equal(r.env.layout, null, 'nor the layout watch');
});
