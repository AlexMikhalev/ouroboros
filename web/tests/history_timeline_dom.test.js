import assert from 'node:assert/strict';
import test from 'node:test';
import { bindLiveCardTimeline, buildTimelineItemHtml, selectionInside } from '../modules/chat_activity.js';
import { createLiveCardTimelineRenderer } from '../modules/chat_render_batch.js';

// A real tree (including text nodes), with explicit removal effects on focus.
// The injected renderer builds JSON trees so this fixture needs no HTML parser.
class Node {
    constructor(doc, [name, attrs = {}, content = []]) {
        this.ownerDocument = doc;
        this.nodeName = name;
        this.nodeType = name === '#text' ? 3 : 1;
        this.attrs = { ...attrs };
        this.nodeValue = this.nodeType === 3 ? content : null;
        this.childNodes = this.nodeType === 3 ? [] : content.map((item) => new Node(doc, item));
        this.childNodes.forEach((child) => { child.parentNode = this; });
        this.parentNode = null;
        this.scrollTop = 0;
        this.clientHeight = 30;
    }
    get children() { return this.childNodes.filter((child) => child.nodeType === 1); }
    get firstElementChild() { return this.children[0]; }
    get lastElementChild() { return this.children.at(-1); }
    get lastChild() { return this.childNodes.at(-1); }
    get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({ name, value })); }
    get dataset() { return { liveLineKey: this.attrs['data-live-line-key'], expanded: this.attrs['data-expanded'] }; }
    get isConnected() { return this === this.ownerDocument.root || Boolean(this.parentNode?.isConnected); }
    get scrollHeight() { return this.children.length * 20; }
    hasAttribute(name) { return name in this.attrs; }
    getAttribute(name) { return this.attrs[name] ?? null; }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    contains(node) { return node === this || this.childNodes.some((child) => child.contains(node)); }
    focus() { this.ownerDocument.activeElement = this; }
    tuple() { return [this.nodeName, this.attrs, this.nodeType === 3 ? this.nodeValue : this.childNodes.map((child) => child.tuple())]; }
    get outerHTML() { return this.nodeType === 1 ? JSON.stringify(this.tuple()) : undefined; }
    set innerHTML(value) { this.childNodes = [new Node(this.ownerDocument, JSON.parse(value))]; this.childNodes[0].parentNode = this; }
    appendChild(node) { return this.insertBefore(node, null); }
    insertBefore(node, next) {
        node.remove();
        const index = this.childNodes.indexOf(next);
        this.childNodes.splice(index < 0 ? this.childNodes.length : index, 0, node);
        node.parentNode = this;
        return node;
    }
    replaceChild(next, current) { this.insertBefore(next, current); current.remove(); }
    remove() {
        if (!this.parentNode) return;
        if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = null;
        this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
        this.parentNode = null;
    }
    querySelector(selector) {
        const key = selector.match(/data-live-line-key="([^"]+)"/)?.[1];
        return this.children.find((child) => child.dataset.liveLineKey === key);
    }
}

const text = (value) => ['#text', {}, value];
function rendererFixture() {
    const doc = { activeElement: null, createElement: (name) => new Node(doc, [name]) };
    const timelineEl = new Node(doc, ['DIV']);
    doc.root = timelineEl;
    const record = { timelineEl, root: { dataset: { expanded: '1' } }, expandedLineKeys: new Set(), items: [] };
    const build = (item) => JSON.stringify(['DIV', { 'data-live-line-key': item.lineKey }, [
        ['DIV', { role: 'button', 'aria-expanded': String(record.expandedLineKeys.has(item.lineKey)) }, [
            ['SPAN', { class: 'title' }, [text(item.title || item.lineKey)]],
            ['SPAN', { class: 'time' }, [text(item.ts || '')]],
        ]],
        ['DIV', { class: 'body' }, [['P', {}, [text(item.body || 'Long narration')]]]],
    ]]);
    const renderer = createLiveCardTimelineRenderer({ withStableViewport: (fn) => fn(), buildTimelineItemHtml: build });
    return { doc, record, ...renderer };
}

test('older rows and timestamp patches preserve the mounted row, focused header and selected body', () => {
    const f = rendererFixture();
    f.record.items = [{ lineKey: 'newer', ts: '12:00' }];
    assert.equal(f.renderLiveCardTimeline(f.record), true);
    const row = f.record.timelineEl.firstElementChild;
    const [header, body] = row.children;
    const title = header.firstElementChild;
    const selectedText = body.firstElementChild.firstElementChild || body.firstElementChild.childNodes[0];
    header.focus();
    f.record.expandedLineKeys.add('newer');
    f.record.items.unshift({ lineKey: 'older' });
    f.record.items[1].ts = '12:01';
    assert.equal(f.renderLiveCardTimeline(f.record), true);
    assert.equal(f.record.timelineEl.children[1], row);
    assert.equal(row.firstElementChild, header);
    assert.equal(header.firstElementChild, title);
    assert.equal(row.children[1], body);
    assert.equal(body.contains(selectedText), true);
    assert.equal(f.doc.activeElement, header);
    assert.equal(header.getAttribute('aria-expanded'), 'true');
    assert.equal(f.renderLiveCardTimeline(f.record), false);
});

test('patching a timestamp keeps DOM additions made by markdown enhancement', () => {
    const f = rendererFixture();
    const item = { lineKey: 'one', ts: '12:00' };
    f.record.items = [item];
    f.appendTimelineItem(item, f.record);
    const row = f.record.timelineEl.firstElementChild;
    const body = row.children[1];
    const copy = new Node(f.doc, ['BUTTON', { 'aria-label': 'Copy' }, [text('Copy')]]);
    body.appendChild(copy);
    copy.focus();
    item.ts = '12:01';
    assert.equal(f.patchLastTimelineItem(item, f.record), true);
    assert.equal(body.lastElementChild, copy);
    assert.equal(f.doc.activeElement, copy);
    assert.equal(f.patchTimelineItemAt(item, f.record), false);
});

test('keyed reorder preserves header identity and restores focus; removed keys leave the timeline', () => {
    const f = rendererFixture();
    const a = { lineKey: 'a' }, b = { lineKey: 'b' };
    f.record.items = [a, b];
    f.renderLiveCardTimeline(f.record);
    const second = f.record.timelineEl.children[1];
    const header = second.firstElementChild;
    header.focus();
    f.record.items = [b, a];
    f.renderLiveCardTimeline(f.record);
    assert.equal(f.record.timelineEl.firstElementChild, second);
    assert.equal(f.doc.activeElement, header);
    f.record.items = [b];
    f.renderLiveCardTimeline(f.record);
    assert.equal(f.record.timelineEl.children.length, 1);
    assert.equal(f.doc.activeElement, header);
});

test('collapsed subagent defers DOM writes and reconciles when opened', () => {
    const f = rendererFixture();
    f.record.isSubagent = true;
    f.record.root.dataset.expanded = '0';
    f.record.items = [{ lineKey: 'one' }];
    assert.equal(f.renderLiveCardTimeline(f.record), false);
    assert.equal(f.record._timelineDirty, true);
    f.record.root.dataset.expanded = '1';
    assert.equal(f.renderLiveCardTimeline(f.record), true);
    assert.equal(f.record._timelineDirty, false);
});

test('timeline markup uses a selectable accessible header and exact history attribution', (t) => {
    const prior = globalThis.document;
    globalThis.document = { createElement: () => ({ textContent: '', get innerHTML() { return this.textContent; } }) };
    t.after(() => { globalThis.document = prior; });
    const html = buildTimelineItemHtml({ lineKey: 'one', historyId: 'source"1', phase: 'working', headline: '## Title\nNarration', fullHeadline: 'Full title', body: '## Body\nDetails' }, { expandedLineKeys: new Set(), groupId: 'task' });
    assert.match(html, /<div\s+role="button" tabindex="0"/);
    assert.doesNotMatch(html, /<button/);
    assert.match(html, /data-history-id="source&quot;1"/);
    assert.match(html, /aria-controls="chat-live-line-body-task-one"/);
    assert.match(html, /Title<\/strong><br>/);
    assert.match(html, /Body<\/strong><br>/);
});

test('selection crossing a header is protected even with both endpoints outside', () => {
    const el = { contains: () => false };
    assert.equal(selectionInside(el, { isCollapsed: false, rangeCount: 1, getRangeAt: () => ({ intersectsNode: (node) => node === el }) }), true);
});

test('delegated header activation respects selection, nested controls and one keyboard activation', () => {
    const handlers = {};
    let selection = null, calls = 0, stopped = 0, prevented = 0;
    const line = { dataset: { liveLineKey: 'one' }, contains: (node) => node === inside };
    const inside = {};
    const header = { matches: () => true };
    const target = {
        closest: (selector) => selector === '.chat-live-line.expandable' ? line
            : selector === '[data-live-line-toggle]' ? header : header,
    };
    const owner = { contains: (node) => node === header, ownerDocument: { getSelection: () => selection }, addEventListener: (name, fn) => { handlers[name] = fn; } };
    bindLiveCardTimeline(owner, (key) => { assert.equal(key, 'one'); calls += 1; });
    const event = { target, stopPropagation: () => { stopped += 1; }, preventDefault: () => { prevented += 1; } };
    selection = { isCollapsed: false, anchorNode: inside };
    handlers.click(event);
    assert.equal(calls, 0);
    selection = null;
    handlers.click(event);
    handlers.keydown({ ...event, key: 'Enter' });
    handlers.keydown({ ...event, key: ' ' });
    handlers.keydown({ ...event, key: ' ', repeat: true });
    assert.deepEqual([calls, stopped, prevented], [3, 4, 3]);
    const link = { closest: (selector) => selector === '.chat-live-line.expandable' ? line : selector === '[data-live-line-toggle]' ? header : { matches: () => false } };
    handlers.click({ ...event, target: link });
    handlers.keydown({ ...event, target: link, key: 'Enter' });
    assert.equal(calls, 3);
});
