import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

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
    const { prior, mount } = installDom(async (url) => {
        if (String(url).startsWith('/api/chat/history')) {
            const cursor = new URL(String(url), 'http://local').searchParams.get('cursor');
            calls.push(cursor);
            return { ok: true, json: async () => cursor && fetchPage ? fetchPage(cursor) : response };
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
