import test from 'node:test';
import assert from 'node:assert/strict';
import { createLayerStack } from '../modules/layers.js';

// The layer stack (P2): one dismiss for every layer, one history entry while any is open, inert outside a modal
// layer, focus into the layer and back to the opener. A fake DOM small enough to read: nodes with parents,
// children, `inert`, `contains` and `focus`; a window with listeners; a history that records its calls.

function makeDoc() {
    const doc = { activeElement: null };
    function node(tag, parent) {
        const n = {
            tagName: tag.toUpperCase(), children: [], parentElement: null, inert: false, isConnected: true, attrs: {},
            tabIndex: null, ownerDocument: doc,
            hasAttribute(name) { return name in this.attrs; },
            setAttribute(name, value) { this.attrs[name] = value; },
            contains(other) { for (let x = other; x; x = x.parentElement) if (x === this) return true; return false; },
            closest(selector) {
                if (selector !== '[inert]') return null;
                for (let x = this; x; x = x.parentElement) if (x.inert) return x;
                return null;
            },
            focus() { doc.activeElement = this; },
            querySelector() { return null; },
        };
        if (parent) { n.parentElement = parent; parent.children.push(n); }
        return n;
    }
    doc.body = node('body');
    doc.body.querySelector = () => null;
    doc.querySelector = (selector) => (selector === 'dialog[open]' && doc.dialogOpen ? {} : null);
    doc.activeElement = doc.body;
    return { doc, node };
}

function makeWin() {
    const listeners = new Map();
    const history = { calls: [], pushState(state, title) { this.calls.push(['push', state, title]); }, back() { this.calls.push(['back']); } };
    const win = {
        history, listeners,
        addEventListener(type, fn) { listeners.set(type, fn); },
        removeEventListener(type) { listeners.delete(type); },
        fire(type, event = {}) { listeners.get(type)?.(event); },
    };
    return win;
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function world() {
    const { doc, node } = makeDoc();
    const app = node('div', doc.body);
    const sidebar = node('nav', app);
    const content = node('main', app);
    const panel = node('aside', app);
    const toast = node('div', doc.body);
    const win = makeWin();
    const layers = createLayerStack({ win, doc, history: win.history });
    return { doc, node, app, sidebar, content, panel, toast, win, layers };
}

test('push, top, size, pop through the owner, remove is idempotent', () => {
    const w = world();
    const closed = [];
    assert.equal(w.layers.size(), 0);
    w.layers.push({ id: 'drawer', el: w.sidebar, onClose: (reason) => { closed.push(['drawer', reason]); w.layers.remove('drawer'); } });
    w.layers.push({ id: 'project', el: w.panel, modal: false, onClose: (reason) => closed.push(['project', reason]) });
    assert.deepEqual(w.layers.ids(), ['drawer', 'project']);
    assert.equal(w.layers.top(), 'project');
    assert.equal(w.layers.pop('escape'), true);
    // The owner that forgot to call remove is removed by the stack itself; the owner that did is not removed twice.
    assert.deepEqual(closed, [['project', 'escape']]);
    assert.equal(w.layers.size(), 1);
    assert.equal(w.layers.pop('scrim'), true);
    assert.deepEqual(w.layers.ids(), []);
    assert.equal(w.layers.pop(), false, 'nothing to pop');
    assert.equal(w.layers.remove('drawer'), false);
    // Pushing a known id keeps its place and takes new facts.
    w.layers.push({ id: 'a', el: w.sidebar, onClose() {} });
    w.layers.push({ id: 'b', el: w.panel, onClose() {} });
    w.layers.push({ id: 'a', el: w.sidebar, modal: false, onClose() {} });
    assert.deepEqual(w.layers.ids(), ['a', 'b']);
    w.layers.destroy();
});

test('Escape closes the top layer unless something inside took the key or a native dialog is open', () => {
    const w = world();
    const closed = [];
    w.layers.push({ id: 'drawer', el: w.sidebar, onClose: () => closed.push('drawer') });
    const prevented = { key: 'Escape', defaultPrevented: true, preventDefault() {} };
    w.win.fire('keydown', prevented);
    assert.deepEqual(closed, [], 'a menu that prevented default owns its Escape');
    w.doc.dialogOpen = true;
    w.win.fire('keydown', { key: 'Escape', defaultPrevented: false, preventDefault() {} });
    assert.deepEqual(closed, [], 'a native <dialog> owns Escape until it adapts');
    w.doc.dialogOpen = false;
    const event = { key: 'Escape', defaultPrevented: false, prevented: false, preventDefault() { this.prevented = true; } };
    w.win.fire('keydown', event);
    assert.deepEqual(closed, ['drawer']);
    assert.equal(event.prevented, true);
    assert.equal(w.layers.size(), 0);
    w.win.fire('keydown', { key: 'Escape', defaultPrevented: false, preventDefault() { throw new Error('nothing open'); } });
    w.layers.destroy();
});

test('one same-URL history entry while any layer is open; popstate pops one; a programmatic close consumes it', async () => {
    const w = world();
    const closed = [];
    const drawer = { id: 'drawer', el: w.sidebar, onClose: (reason) => { closed.push(['drawer', reason]); w.layers.remove('drawer'); } };
    const project = { id: 'project', el: w.panel, onClose: (reason) => { closed.push(['project', reason]); w.layers.remove('project'); } };
    w.layers.push(drawer);
    w.layers.push(project);
    await tick();
    assert.deepEqual(w.win.history.calls, [['push', { ouroLayers: 1 }, '']], 'two layers, one entry');
    assert.equal(w.layers.historyArmed(), true);
    // The system Back closes the top layer, and the entry is re-armed while the other stays open.
    w.win.fire('popstate');
    assert.deepEqual(closed, [['project', 'history']]);
    await tick();
    assert.deepEqual(w.win.history.calls.slice(1), [['push', { ouroLayers: 1 }, '']]);
    // Closing the last layer from a button consumes the entry; the popstate that causes is not a dismiss.
    w.layers.pop('close');
    assert.deepEqual(closed.at(-1), ['drawer', 'close']);
    await tick();
    assert.deepEqual(w.win.history.calls.at(-1), ['back']);
    assert.equal(w.layers.historyArmed(), false);
    w.win.fire('popstate');
    assert.equal(closed.length, 2, 'the swallowed popstate closed nothing');
    // "Close the drawer, open the Project" within one task leaves the one entry in place: no back, no extra push.
    const before = w.win.history.calls.length;
    w.layers.push(drawer);
    await tick();
    w.layers.remove('drawer');
    w.layers.push(project);
    await tick();
    assert.deepEqual(w.win.history.calls.slice(before), [['push', { ouroLayers: 1 }, '']]);
    assert.equal(w.layers.historyArmed(), true);
    // A document whose history refuses: layers still open and close.
    w.win.history.pushState = () => { throw new Error('sandboxed'); };
    w.layers.remove('project');
    await tick();
    w.layers.push(drawer);
    await tick();
    assert.equal(w.layers.size(), 1);
    w.layers.destroy();
});

test('a modal layer makes everything outside it inert; a non-modal one leaves the sidebar live', () => {
    const w = world();
    w.toast.inert = true;  // the page had already made this inert: the stack leaves it alone
    w.layers.push({ id: 'drawer', el: w.sidebar, modal: true, onClose() {} });
    assert.equal(w.sidebar.inert, false);
    assert.equal(w.content.inert, true);
    assert.equal(w.panel.inert, true);
    assert.equal(w.app.inert, false, 'the ancestor of the layer stays live');
    w.layers.remove('drawer');
    assert.equal(w.content.inert, false);
    assert.equal(w.panel.inert, false);
    assert.equal(w.toast.inert, true, 'what the page inerted stays inert');
    w.layers.push({ id: 'project', el: w.panel, modal: false, onClose() {} });
    assert.equal(w.sidebar.inert, false);
    assert.equal(w.content.inert, false);
    // The phone: the drawer (modal) opens over the Project layer; the panel below goes inert, the drawer stays live.
    w.layers.push({ id: 'drawer', el: w.sidebar, modal: true, onClose() {} });
    assert.equal(w.panel.inert, true);
    assert.equal(w.sidebar.inert, false);
    // A layer above a modal one stays live.
    w.layers.remove('drawer');
    w.layers.configure('project', { modal: true });
    w.layers.push({ id: 'drawer', el: w.sidebar, modal: false, onClose() {} });
    assert.equal(w.sidebar.inert, false);
    assert.equal(w.content.inert, true);
    w.layers.destroy();
    assert.equal(w.content.inert, false, 'destroy releases what it inerted');
});

test('focus goes into the layer on open and back to the opener on close', () => {
    const w = world();
    const opener = w.node('button', w.content);
    opener.focus();
    w.layers.push({ id: 'drawer', el: w.sidebar, onClose() {} });
    assert.equal(w.sidebar.tabIndex, -1, 'a plain container becomes focusable');
    assert.equal(w.doc.activeElement, w.sidebar);
    w.layers.remove('drawer');
    assert.equal(w.doc.activeElement, opener);
    // A custom focus target, and an opener the page itself made inert meanwhile is not focused.
    const field = w.node('input', w.panel);
    w.layers.push({ id: 'project', el: w.panel, modal: false, focus: () => field.focus(), onClose() {} });
    assert.equal(w.doc.activeElement, field);
    w.content.inert = true;
    w.layers.remove('project');
    assert.equal(w.doc.activeElement, field, 'no focus into an inert opener');
    w.layers.destroy();
});

test('onChange reports the stack after every change and returns a disposer', () => {
    const w = world();
    const seen = [];
    const off = w.layers.onChange((snapshot) => seen.push([snapshot.size, snapshot.top]));
    w.layers.push({ id: 'a', el: w.sidebar, onClose() {} });
    w.layers.push({ id: 'b', el: w.panel, onClose() {} });
    w.layers.pop();
    w.layers.remove('a');
    assert.deepEqual(seen, [[1, 'a'], [2, 'b'], [1, 'a'], [0, null]]);
    off();
    w.layers.push({ id: 'a', el: w.sidebar, onClose() {} });
    assert.equal(seen.length, 4);
    w.layers.destroy();
    assert.equal(w.win.listeners.size, 0, 'destroy releases the window listeners');
});
