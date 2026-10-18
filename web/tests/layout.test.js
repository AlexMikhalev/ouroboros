import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// The shell's one layout switch: theme.js writes html[data-layout] before CSS from one number pair and keeps
// it current; layout.js is the door every module reads it through (docs/DESIGN.md "Shell", P1).

const themeSource = fs.readFileSync(new URL('../theme.js', import.meta.url), 'utf8');

function bootTheme({ docked = true, media = true } = {}) {
    const handlers = new Map();
    const events = [];
    const root = { dataset: {} };
    const noop = () => {};
    const colorQuery = { matches: false, addEventListener: noop, removeEventListener: noop };
    const layoutQuery = {
        matches: docked,
        addEventListener: (key, fn) => handlers.set(`layout:${key}`, fn),
        removeEventListener: (key) => handlers.delete(`layout:${key}`),
    };
    const window = {
        addEventListener: noop, removeEventListener: noop,
        dispatchEvent: (event) => events.push(event),
        ...(media ? { matchMedia: (q) => (q.includes('min-width') ? layoutQuery : colorQuery) } : {}),
    };
    const document = {
        documentElement: root, createElement: () => ({ dataset: {}, classList: { toggle: noop }, setAttribute: noop }),
        querySelectorAll: () => [], addEventListener: noop, removeEventListener: noop,
    };
    vm.runInNewContext(themeSource, {
        document, window, localStorage: { getItem: () => null, setItem: noop },
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    });
    return { root, window, layoutQuery, handlers, events };
}

test('theme.js paints the layout before CSS and publishes the numbers once', () => {
    const wide = bootTheme({ docked: true });
    assert.equal(wide.root.dataset.layout, 'docked');
    assert.equal(wide.window.ouroLayout.dockedMinWidth, 720);
    assert.equal(wide.window.ouroLayout.projectBesideMainMinWidth, 360);
    assert.equal(wide.window.ouroLayout.layout, 'docked');
    assert.ok(Object.isFrozen(wide.window.ouroLayout), 'the facts are read-only');
    assert.equal(bootTheme({ docked: false }).root.dataset.layout, 'stack');
    // No media queries at all (an old shell): a window, so docked.
    assert.equal(bootTheme({ media: false }).root.dataset.layout, 'docked');
});

test('a resize or rotation across the switch repaints and announces once', () => {
    const s = bootTheme({ docked: true });
    s.layoutQuery.matches = false;
    s.handlers.get('layout:change')();
    assert.equal(s.root.dataset.layout, 'stack');
    assert.equal(s.window.ouroLayout.layout, 'stack');
    assert.deepEqual(s.events.map((e) => [e.type, e.detail.layout]), [['ouro:layout-changed', 'stack']]);
    // The same answer again is not a change.
    s.handlers.get('layout:change')();
    assert.equal(s.events.length, 1);
});

test('layout.js reads the painted attribute, falls back to the facts, and hears changes', async () => {
    const mod = await import('../modules/layout.js');
    const w = { ouroLayout: { dockedMinWidth: 720, projectBesideMainMinWidth: 360, layout: 'stack' }, listeners: new Map(),
        addEventListener(type, fn) { this.listeners.set(type, fn); }, removeEventListener(type) { this.listeners.delete(type); } };
    assert.equal(mod.currentLayout({ root: { dataset: { layout: 'docked' } }, w }), 'docked');
    assert.equal(mod.currentLayout({ root: { dataset: {} }, w }), 'stack', 'no attribute yet: the facts answer');
    assert.equal(mod.currentLayout({ root: { dataset: {} }, w: {} }), 'docked', 'no facts either: a window');
    assert.equal(mod.isStackLayout({ root: { dataset: { layout: 'stack' } }, w }), true);
    assert.throws(() => mod.layoutFacts({}), /theme\.js/);
    const seen = [];
    const dispose = mod.onLayoutChange((layout) => seen.push(layout), { w });
    w.listeners.get('ouro:layout-changed')({ detail: { layout: 'docked' } });
    assert.deepEqual(seen, ['docked']);
    dispose();
    assert.equal(w.listeners.size, 0, 'the subscription has a disposer');
});

test('the Project panel fills the content area only when Main would keep less than the minimum', async () => {
    const { projectPanelFillsContent } = await import('../modules/layout.js');
    const w = { ouroLayout: { dockedMinWidth: 720, projectBesideMainMinWidth: 360, layout: 'docked' } };
    // 1280 wide, 280 sidebar, 440 panel: Main keeps 560.
    assert.equal(projectPanelFillsContent({ viewportWidth: 1280, sidebarWidth: 280, panelWidth: 440, w }), false);
    // 980 wide, 280 sidebar, 352 panel: Main would keep 348.
    assert.equal(projectPanelFillsContent({ viewportWidth: 980, sidebarWidth: 280, panelWidth: 352, w }), true);
    // Exactly the minimum beside it stays beside it.
    assert.equal(projectPanelFillsContent({ viewportWidth: 1000, sidebarWidth: 280, panelWidth: 360, w }), false);
    assert.equal(projectPanelFillsContent({ viewportWidth: 1000, sidebarWidth: 280, panelWidth: 361, minMain: 360 }), true);
});
