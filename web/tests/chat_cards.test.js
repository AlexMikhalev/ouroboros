// Living cards in the feed (chat_cards.js): one row live and on replay, never an empty box (the fallback text shows
// while the body loads and when it cannot be read), Keep on Widgets with Undo, Make it a skill and an app's submit
// into the owner's draft (never a send), card_state, and at most three live frames.
import assert from 'node:assert/strict';
import test from 'node:test';

import { createChatCards, createFrameBudget, readCardBody } from '../modules/chat_cards.js';
import { createChatMedia } from '../modules/chat_media.js';
import { stampNodeTimestamp } from '../modules/chat_activity.js';
import { installCardDom } from './helpers/card_dom.js';

const flush = async () => { for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve)); };
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) });
const page = (html) => ({ ok: true, status: 200, json: async () => { throw new Error('text'); }, text: async () => html });

function serve(routes) {
    const calls = [];
    const prior = globalThis.fetch;
    globalThis.fetch = async (url, init = {}) => {
        const key = `${String(init.method || 'GET').toUpperCase()} ${url}`;
        calls.push(key);
        const route = routes[key];
        return (typeof route === 'function' ? route(init) : route) || json(404, { error: 'card not found', reason_code: 'card_not_found' });
    };
    return { calls, restore: () => { globalThis.fetch = prior; } };
}

function feed({ composer = null } = {}) {
    const frameNode = (msg, node) => {
        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble assistant';
        bubble.append(node);
        return bubble;
    };
    return createChatCards({ frameNode, composer: () => composer });
}

const identity = (id, kind, extra = {}) => ({ type: 'card', role: 'assistant', card: { card_id: id, kind, title: 'Plans', revision: 1 },
    fallback_text: 'Basic 9 · Pro 19', ts: '2026-10-17T10:00:00Z', ...extra });
const componentsBody = (id, extra = {}) => ({ card_id: id, kind: 'components', title: 'Plans', fallback_text: 'Basic 9 · Pro 19', revision: 1,
    pinned: false, components: [{ type: 'action', label: 'Choose Pro', submit: { id: 'pro' } }], submissions_summary: [], ...extra });

test('at most three frames live: near ones mount while there is room, far ones unmount, Open makes room', () => {
    const log = [];
    const budget = createFrameBudget({ max: 3, mount: (r) => log.push(`+${r}`), unmount: (r) => log.push(`-${r}`) });
    for (const record of ['a', 'b', 'c', 'd']) budget.near(record, true);
    assert.deepEqual(budget.live(), ['a', 'b', 'c']);
    budget.near('a', false);
    assert.deepEqual(budget.live(), ['b', 'c', 'd'], 'the far one unmounted, the waiting near one took its place');
    budget.open('e');
    assert.equal(budget.live().length, 3);
    assert.ok(budget.live().includes('e'));
    budget.forget('e');
    assert.ok(!budget.live().includes('e'));
    assert.deepEqual(log.slice(0, 5), ['+a', '+b', '+c', '-a', '+d']);
});

test('a card body is read once per revision; a failed read is tried again', async () => {
    let reads = 0;
    const read = async () => { reads += 1; if (reads === 3) throw new Error('down'); return { revision: reads === 1 ? 1 : 2 }; };
    await readCardBody('card_cache1', 1, read);
    await readCardBody('card_cache1', 1, read);
    assert.equal(reads, 1);
    await readCardBody('card_cache1', 2, read);
    assert.equal(reads, 2, 'a newer revision is read again');
    await assert.rejects(readCardBody('card_cache1', 3, read));
    await readCardBody('card_cache1', 3, read);
    assert.equal(reads, 4, 'a failed read is not cached');
});

test('the fallback text shows while the body loads; a component card then draws with Keep on Widgets, then Undo', async () => {
    const dom = installCardDom();
    let pinned = false;
    const server = serve({
        'GET /api/cards/card_k1': () => json(200, componentsBody('card_k1', { pinned })),
        'POST /api/cards/card_k1/pin': () => { pinned = true; return json(200, { ok: true, card_id: 'card_k1', pinned: true }); },
        'DELETE /api/cards/card_k1/pin': () => { pinned = false; return json(200, { ok: true, card_id: 'card_k1', pinned: false }); },
    });
    try {
        const cards = feed();
        const bubble = cards.buildCard(identity('card_k1', 'components'));
        assert.ok(bubble.classList.contains('chat-card-bubble'));
        assert.equal(bubble.dataset.cardId, 'card_k1');
        assert.equal(bubble.querySelector('.chat-card-fallback').textContent, 'Basic 9 · Pro 19', 'never an empty box');
        assert.equal(bubble.querySelector('.chat-card-status').textContent, 'Loading the card…');
        assert.equal(bubble.querySelector('.chat-card-made').textContent, 'Ouroboros made this');
        await flush();
        assert.equal(bubble.querySelector('.chat-card-fallback'), null);
        assert.equal(bubble.querySelector('[data-widget-action]').textContent, 'Choose Pro');
        assert.equal(bubble.querySelector('.chat-card-status').hidden, true);
        bubble.querySelector('[data-card-pin]').click();
        await flush();
        assert.deepEqual(server.calls.filter((c) => c.includes('/pin')), ['POST /api/cards/card_k1/pin']);
        assert.match(bubble.querySelector('[data-card-actions]').textContent, /Kept on Widgets/);
        assert.equal(bubble.querySelector('[data-card-unpin]').textContent, 'Undo');
        bubble.querySelector('[data-card-unpin]').click();
        await flush();
        assert.equal(server.calls.at(-1), 'DELETE /api/cards/card_k1/pin');
        assert.equal(bubble.querySelector('[data-card-pin]').textContent, 'Keep on Widgets');
        // The kept state from another window arrives as extension_lifecycle for skill "_cards".
        cards.lifecycle({ skill: '_cards', action: 'card_pinned', data: { card_id: 'card_k1' } });
        assert.equal(bubble.querySelector('[data-card-unpin]').textContent, 'Remove');
        cards.destroy();
    } finally {
        server.restore();
        dom.restore();
    }
});

test('a card that cannot be read keeps its words: Retry after a failure, a plain note when it is gone', async () => {
    const dom = installCardDom();
    let failing = true;
    const server = serve({
        'GET /api/cards/card_f1': () => (failing ? json(503, { error: 'store migrating' }) : json(200, componentsBody('card_f1'))),
    });
    try {
        const cards = feed();
        const broken = cards.buildCard(identity('card_f1', 'components'));
        await flush();
        assert.equal(broken.querySelector('.chat-card-fallback').textContent, 'Basic 9 · Pro 19');
        assert.match(broken.querySelector('.chat-card-status').textContent, /^The card could not be loaded: store migrating/);
        assert.equal(broken.querySelector('.chat-card-status').dataset.tone, 'danger');
        failing = false;
        broken.querySelector('[data-card-retry]').click();
        await flush();
        assert.equal(broken.querySelector('[data-widget-action]').textContent, 'Choose Pro');

        const gone = cards.buildCard(identity('card_g1', 'components'));
        await flush();
        assert.equal(gone.querySelector('.chat-card-fallback').textContent, 'Basic 9 · Pro 19');
        assert.equal(gone.querySelector('.chat-card-status').textContent, 'This card is no longer available.');
        assert.equal(gone.querySelector('[data-card-retry]'), null);
        cards.destroy();
    } finally {
        server.restore();
        dom.restore();
    }
});

test('an app shows its face with Open; open, its submit and Make it a skill fill the draft on their own line', async () => {
    const dom = installCardDom();
    const server = serve({
        'GET /api/cards/card_a1': json(200, { card_id: 'card_a1', kind: 'app', title: 'Loan', fallback_text: 'A loan calculator', revision: 1,
            app: { sha256: 'f00d', size: 12, url: '/api/cards/card_a1/app' }, submissions_summary: [] }),
        'GET /api/cards/card_a1/app': page('<p>calc</p>'),
    });
    try {
        const inserted = [];
        const composer = { input: { value: 'Hello', selectionStart: 5 }, insertAtCursor: (text) => inserted.push(text) };
        const cards = feed({ composer });
        const bubble = cards.buildCard(identity('card_a1', 'app', { card: { card_id: 'card_a1', kind: 'app', title: 'Loan', revision: 1 } }));
        await flush();
        assert.equal(bubble.querySelector('.chat-card-face').querySelector('.chat-card-fallback').textContent, 'Basic 9 · Pro 19');
        assert.equal(bubble.querySelector('iframe'), null, 'no frame before Open (no viewport observer here)');
        bubble.querySelector('[data-card-open]').click();
        await flush();
        const frame = bubble.querySelector('iframe');
        assert.equal(frame.getAttribute('sandbox'), 'allow-scripts');
        assert.ok(server.calls.includes('GET /api/cards/card_a1/app'));
        frame.contentWindow = {};
        const nonce = frame.srcdoc.match(/const nonce = "([^"]+)"/)[1];
        window.emit('message', { source: frame.contentWindow, data: { type: 'ouro-card-submit', value: { monthly: 1520 }, summary: 'Monthly', nonce } });
        assert.deepEqual(inserted, ['\nFrom «Loan»: Monthly {"monthly":1520}']);
        bubble.querySelector('[data-card-make-skill]').click();
        assert.equal(inserted[1], '\nTurn the card «Loan» into a skill so I can keep it on Widgets.');
        composer.input = { value: '', selectionStart: 0 };
        bubble.querySelector('[data-card-make-skill]').click();
        assert.equal(inserted[2], 'Turn the card «Loan» into a skill so I can keep it on Widgets.', 'an empty draft takes the line as is');
        cards.release(bubble);
        assert.equal(bubble.querySelector('iframe'), null, 'released: the frame is gone');
        assert.equal(window.listeners('message'), 0);
    } finally {
        server.restore();
        dom.restore();
    }
});

test('card_state: recorded presses update the drawn card in place; a newer revision is read again', async () => {
    const dom = installCardDom();
    let revision = 1;
    const server = serve({ 'GET /api/cards/card_s1': () => json(200, componentsBody('card_s1', {
        revision, components: [{ type: 'action', label: revision === 1 ? 'Choose Pro' : 'Choose Max', submit: { id: 'pro', once: true } }],
    })) });
    try {
        const cards = feed();
        const bubble = cards.buildCard(identity('card_s1', 'components'));
        await flush();
        const reads = () => server.calls.filter((c) => c === 'GET /api/cards/card_s1').length;
        assert.equal(reads(), 1);
        cards.applyCardState({ type: 'card_state', card: { card_id: 'card_s1', kind: 'components', title: 'Plans', revision: 1 },
            submissions_summary: [{ action_id: 'pro', label: 'Choose Pro', at: 't', delivery: 'delivered' }], submissions_dropped: 0 });
        assert.equal(reads(), 1, 'a press needs no read');
        assert.equal(bubble.querySelector('[data-widget-action]').disabled, true, 'its once control counted');
        revision = 2;
        assert.equal(cards.applyCardState({ type: 'card_state', card: { card_id: 'card_s1', revision: 2 }, submissions_summary: [] }), true);
        await flush();
        assert.equal(reads(), 2);
        assert.equal(bubble.querySelector('[data-widget-action]').textContent, 'Choose Max');
        assert.equal(cards.applyCardState({ card: { card_id: 'card_unknown', revision: 9 } }), false, 'a card this room does not show');
        cards.destroy();
    } finally {
        server.restore();
        dom.restore();
    }
});

test('a widget card whose skill is off keeps its words and says so', async () => {
    const dom = installCardDom();
    const server = serve({
        'GET /api/cards/card_w1': json(200, { card_id: 'card_w1', kind: 'widget', title: 'Weather', fallback_text: 'Sunny', revision: 1,
            widget: { skill: 'weather', tab_id: 'now' }, submissions_summary: [] }),
        'GET /api/widgets': json(200, { ui_tabs: [{ key: 'other:tab', skill: 'other', tab_id: 'tab', render: { kind: 'declarative', components: [] } }] }),
    });
    try {
        const cards = feed();
        const bubble = cards.buildCard(identity('card_w1', 'widget'));
        await flush();
        assert.equal(bubble.querySelector('.chat-card-fallback').textContent, 'Basic 9 · Pro 19');
        assert.equal(bubble.querySelector('.chat-card-status').textContent, 'This skill is off.');
        assert.equal(bubble.querySelector('[data-card-actions]').textContent, '', 'a skill\'s widget has nothing to keep');
        cards.destroy();
    } finally {
        server.restore();
        dom.restore();
    }
});

test('the live card frame and its history row are one row; replay draws it before final handling', async () => {
    const dom = installCardDom();
    const server = serve({ 'GET /api/cards/card_r1': json(200, componentsBody('card_r1')) });
    try {
        const root = document.createElement('div');
        const media = createChatMedia({
            chatSessionId: 's', durableChatMediaUrl: () => '', formatMsgTime: () => null, senderLabel: () => 'Owner',
            insertMessageNode: (node) => root.append(node), stampNodeTimestamp,
        });
        const handlers = {};
        const seen = new Set();
        const unread = [];
        const delivery = media.wireDeliveries({
            onWs: (type, fn) => { handlers[type] = fn; }, isMyThread: () => true, hideTypingIndicatorOnly() {}, syncChatStatus() {},
            incrementUnreadIfNeeded: (msg) => unread.push(msg.type), seenMessageKeys: seen, rememberMessageKey: (key) => seen.add(key),
            chatMediaMessageKey: () => '', documentMessageKey: () => '', messagesRoot: () => root,
        });
        assert.equal(typeof handlers.card, 'function');
        assert.equal(typeof handlers.card_state, 'function');
        handlers.card(identity('card_r1', 'components', { row_address: 'chat:1:abc' }));
        assert.equal(root.children.length, 1);
        assert.deepEqual(unread, ['card'], 'a new card is a message the owner has not read');
        assert.equal(root.children[0].dataset.rowAddress, 'chat:1:abc');
        const row = { ...identity('card_r1', 'components'), type: undefined, msg_type: 'card', system_type: 'card', text: 'Basic 9 · Pro 19',
            history_id: 'chat:7', history_position: { source: 'chat', offset: 700 } };
        assert.equal(delivery.appendDelivery(row), true, 'a card row is a delivery');
        assert.equal(root.children.length, 1, 'the same card: one row');
        assert.equal(root.children[0].dataset.historyId, 'chat:7');
        assert.equal(delivery.appendDelivery({ msg_type: undefined, text: 'hi' }), false, 'an ordinary row is not');
        await flush();
        media.release(root.children[0]);
        handlers.card_state({ card: { card_id: 'card_r1', revision: 1 }, submissions_summary: [] });
        media.destroy();
    } finally {
        server.restore();
        dom.restore();
    }
});
