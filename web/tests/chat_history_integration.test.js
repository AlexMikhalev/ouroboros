import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom, ElementStub } from './chat_dom_fixture.js';

const TS = '2026-09-12T12:00:00.000Z';
const row = (id, text, extra = {}) => ({
    role: 'assistant', text, ts: TS, history_id: id,
    history_position: { source: 'chat:rotation-1', offset: Number(id.split(':').at(-1)) || 0 },
    ...extra,
});
const page = (messages, cursor = 'page:recent', next = null) => ({
    messages, page_cursor: cursor, next_cursor: next, has_more: next !== null,
    window: { complete: next === null, truncated_by: next ? ['quota'] : [] },
});

function fixture(t, initial = page([]), fetchPage = null) {
    let response = initial, revision = 0;
    const calls = [], handlers = new Map();
    const { prior, mount } = installDom(async (url, init) => {
        if (String(url).startsWith('/api/chat/history')) {
            const cursor = new URL(String(url), 'http://local').searchParams.get('cursor');
            calls.push(cursor);
            const data = cursor && fetchPage ? await fetchPage(cursor, init) : response;
            return { ok: true, json: async () => data };
        }
        return { ok: true, json: async () => ({ active_direct_turns: [] }) };
    });
    const priorSocket = globalThis.WebSocket;
    globalThis.WebSocket = { OPEN: 1 };
    const instance = createChatInstance({
        ws: { on(type, handler) { handlers.set(type, handler); return () => handlers.delete(type); },
            isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
        updateUnreadBadge() {}, stateSnapshots: { begin: () => ({ generation: 1, requestedAt: Date.now() }),
            isCurrent: () => true, apply() {} },
        chatId: 2, idPrefix: 'chat', mountEl: mount, asPanel: true,
    });
    t.after(() => { instance.destroy(); restoreDom(prior); globalThis.WebSocket = priorSocket; });
    const messages = globalThis.document.byId.get('chat-messages');
    return {
        instance, calls, messages,
        bubbles: () => messages.children.filter((node) => node.classList.contains('chat-bubble')
            && !node.classList.contains('typing-bubble') && node.dataset.ephemeral !== '1'),
        emit: (message) => handlers.get('chat')({ chat_id: 2, ...message }),
        respond: next => { response = next; },
        async refresh(next = response) {
            response = next;
            const result = await instance.refreshHistory({ revision: ++revision });
            assert.equal(result.painted, true, 'history reached the public paint acknowledgement');
        },
        async clickOlder() {
            const button = messages.querySelector('.chat-load-older').querySelector('.chat-load-older-btn');
            assert.equal(button.hidden, false);
            for (const handler of button.listeners.get('click')) await handler({ target: button });
        },
    };
}

test('live message adopts its physical history identity without replacing the visible bubble', async (t) => {
    const f = fixture(t);
    await f.refresh();
    f.emit({ role: 'assistant', content: 'A useful answer', ts: TS });
    const live = f.bubbles().find((node) => node.innerHTML.includes('A useful answer'));
    assert.ok(live);
    await f.refresh(page([row('chat:100', 'A useful answer')]));
    const adopted = f.bubbles().filter((node) => node.innerHTML.includes('A useful answer'));
    assert.deepEqual(adopted, [live]);
    assert.equal(live.dataset.historyId, 'chat:100');
    assert.equal(live.dataset.historySource, 'chat:rotation-1');
    assert.equal(live.dataset.historyOffset, '100');
    await f.refresh(page([row('chat:100', 'A useful answer')]));
    assert.deepEqual(f.bubbles().filter((node) => node.innerHTML.includes('A useful answer')), [live]);
});

test('repeated physical history identity updates the routing annotation on the same user bubble', async (t) => {
    const message = row('chat:200', 'Please investigate this', {
        role: 'user', client_message_id: 'owner-message', chat_annotation: { status: 'pending' },
    });
    const f = fixture(t, page([message]));
    await f.refresh();
    const bubble = f.bubbles().find((node) => node.dataset.historyId === 'chat:200');
    assert.ok(bubble);
    assert.equal(bubble.dataset.chatAnnotationStatus, 'pending');
    const note = bubble.querySelector('.msg-routing-annotation');
    assert.match(note.textContent, /Choosing/);
    await f.refresh(page([{ ...message, chat_annotation: {
        status: 'delivered', action: 'steer', target: 'task-existing', target_label: 'Investigation',
    } }]));
    assert.equal(f.bubbles().filter((node) => node.dataset.historyId === 'chat:200').length, 1);
    assert.equal(f.bubbles().find((node) => node.dataset.historyId === 'chat:200'), bubble);
    assert.equal(bubble.querySelector('.msg-routing-annotation'), note);
    assert.equal(bubble.dataset.chatAnnotationStatus, 'delivered');
    assert.notEqual(note.textContent, 'Choosing the right destination…');
    assert.match(note.textContent, /Investigation/);
});

test('two physical rows with identical timestamp and body remain two messages across refresh', async (t) => {
    const rows = [row('chat:300', 'Same words'), row('chat:400', 'Same words')];
    const f = fixture(t, page(rows));
    await f.refresh();
    const bubbles = f.bubbles().filter((node) => node.innerHTML.includes('Same words'));
    assert.equal(bubbles.length, 2);
    assert.deepEqual(bubbles.map((node) => node.dataset.historyId), ['chat:300', 'chat:400']);
    await f.refresh(page(rows));
    assert.deepEqual(f.bubbles().filter((node) => node.innerHTML.includes('Same words')), bubbles);
});

test('one older-navigation action crosses a sparse page and displays the next physical row', async (t) => {
    const f = fixture(t, page([row('chat:900', 'Recent message')], 'page:recent', 'before:sparse'), (cursor) => {
        if (cursor === 'before:sparse') return page([], 'page:sparse', 'before:older');
        assert.equal(cursor, 'before:older');
        return page([row('chat:100', 'An older message', { ts: '2026-09-11T12:00:00Z' })], 'page:older');
    });
    await f.refresh();
    await f.clickOlder();
    assert.deepEqual(f.calls.filter(Boolean), ['before:sparse', 'before:older']);
    assert.equal(f.bubbles().filter((node) => node.dataset.historyId === 'chat:100').length, 1);
    assert.equal(f.bubbles().filter((node) => node.dataset.historyId === 'chat:900').length, 1);
});

test('retained history nodes still dedupe after navigation exceeds the live key FIFO', async (t) => {
    const oldRect = ElementStub.prototype.getBoundingClientRect;
    ElementStub.prototype.getBoundingClientRect = function () {
        return this.dataset.historyId
            ? { top: 1000, bottom: 1020, left: 0, right: 100, width: 100, height: 20 }
            : oldRect.call(this);
    };
    t.after(() => { ElementStub.prototype.getBoundingClientRect = oldRect; });
    const rows = Array.from({ length: 2500 }, (_, i) => row(`chat:${i}`, `Retained answer ${i}`));
    const pack = index => page(rows.slice(Math.max(0, rows.length - (index + 1) * 150), rows.length - index * 150),
        `page:${index}`, (index + 1) * 150 < rows.length ? `older:${index + 1}` : null);
    const f = fixture(t, pack(0), cursor => pack(Number(cursor.split(':')[1])));
    await f.refresh();
    const recent = f.bubbles().find(node => node.dataset.historyId === 'chat:2499');
    for (let i = 0; i < 16; i += 1) await f.clickOlder();
    const mounted = f.bubbles().length;
    assert.ok(mounted < 1000, 'distant pages actually evicted rather than all remaining protected');
    await f.refresh(pack(0));
    assert.deepEqual(f.bubbles().filter(node => node.dataset.historyId === 'chat:2499'), [recent]);
    assert.equal(f.bubbles().length, mounted, 'refresh adds no duplicate retained recent page');
});

test('closing the real chat instance aborts its pending archive fetch', async (t) => {
    let started;
    const ready = new Promise(resolve => { started = resolve; });
    const f = fixture(t, page([row('chat:10', 'Recent')], 'recent', 'older'), (_cursor, init) => {
        assert.ok(init.signal, 'the pager signal reaches the shared fetch transport');
        started(init.signal);
        return new Promise((_resolve, reject) => init.signal.addEventListener('abort',
            () => reject(new DOMException('Aborted', 'AbortError')), { once: true }));
    });
    await f.refresh();
    const pending = f.clickOlder();
    const signal = await ready;
    f.instance.destroy();
    assert.equal(signal.aborted, true);
    await pending;
});

test('history query construction remains portable when URLSearchParams.size is unavailable', async () => {
    const source = await import('../modules/api_client.js');
    const original = Object.getOwnPropertyDescriptor(URLSearchParams.prototype, 'size');
    Object.defineProperty(URLSearchParams.prototype, 'size', { configurable: true, value: undefined });
    const priorFetch = globalThis.fetch;
    let requested;
    globalThis.fetch = async (url) => {
        requested = String(url);
        return { ok: true, json: async () => ({ messages: [], has_more: false, page_cursor: 'p' }) };
    };
    try { await source.apiClient.chatHistory({ chatId: 2, cursor: 'c' }); }
    finally {
        globalThis.fetch = priorFetch;
        if (original) Object.defineProperty(URLSearchParams.prototype, 'size', original);
        else delete URLSearchParams.prototype.size;
    }
    assert.match(requested, /chat_id=2/);
    assert.match(requested, /cursor=c/);
});


for (const hydrated of [false, true]) test(`an unavailable archive keeps recent messages and a fresh retry (hydrated=${hydrated})`, async (t) => {
    const partial = { messages: [row('chat:10', 'Readable recent answer', {
        history_id: undefined, history_position: undefined,
    })],
        has_more: true, page_cursor: null, next_cursor: null,
        reason_code: 'history_source_unavailable', error: 'Saved archive is unavailable',
        window: { complete: false, truncated_by: ['history_source_unavailable'] } };
    const f = fixture(t, page([row('chat:10', 'Readable recent answer')], 'initial', 'older'));
    if (hydrated) await f.refresh();
    await f.refresh(partial);
    const recent = f.bubbles().find(node => node.innerHTML.includes('Readable recent answer'));
    assert.ok(recent, 'available dialogue stays readable despite the source gap');
    assert.equal(f.bubbles().length, 1, 'a temporary missing boundary does not duplicate a retained row');
    const controls = f.messages.querySelector('.chat-load-older');
    const button = controls.querySelector('.chat-load-older-btn');
    assert.equal(button.textContent, 'Retry loading messages');
    assert.notEqual(controls.querySelector('.chat-load-older-note').textContent, 'Beginning of saved history');
    f.respond(page([row('chat:10', 'Readable recent answer')], 'real-page', 'real-older'));
    await f.clickOlder();
    assert.equal(button.textContent, 'Load older messages');
    assert.deepEqual(f.bubbles().filter(node => node.dataset.historyId === 'chat:10'), [recent]);
    assert.equal(f.calls.at(-1), null, 'retry captures a fresh real boundary rather than inventing a cursor');
});
