// The Main and Project composer (composer.js), mounted by every chat room: the same chat-* ids (namespaced in a
// Project panel), the room's draft kept across the room's teardown and cleared by Send, the draft API that later
// controls use, and the input recall seeded once from the room's own history. Enter and Swarm are in
// chat_composer_enter.test.js; the real engines are in the browser lane.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));
const SUFFIXES = ['input-area', 'attachment-preview', 'composer-pills', 'swarm', 'context-mode', 'attach', 'file-input',
    'input', 'send'];

function fetchStub(history = []) {
    return async (url) => ({ ok: true, json: async () => (String(url).startsWith('/api/chat/history')
        ? { messages: history, window: { complete: true } }
        : { active_chat_activities: [], active_chat_activities_complete: true, supervisor_ready: true }) });
}

function mount(env, { chatId = 1, projectId = '', idPrefix = 'chat', frames = [] } = {}) {
    let generation = 0;
    return createChatInstance({
        ws: { on: () => () => {}, isConnected: () => true,
            send: (frame) => { frames.push(frame); return { status: 'sent', clientMessageId: `cm-${frames.length}` }; } },
        state: { activePage: 'chat', projectChatIds: new Set(chatId === 1 ? [] : [chatId]), unreadCount: 0 },
        updateUnreadBadge() {}, chatId, projectId, idPrefix, mountEl: env.mount, asPanel: chatId !== 1,
        stateSnapshots: { begin: () => ({ generation: ++generation, requestedAt: Date.now() }),
            gate() { return Promise.resolve(this.begin()); }, isCurrent: () => true, apply() {}, fail() {}, latest: () => null },
    });
}

const PROJECT = { chatId: 7, projectId: 'p7', idPrefix: 'pchat-p7' };
const node = (instance, id) => instance.page.querySelector(`[id="${id}"]`);

test('every room mounts the composer with the chat-* ids, namespaced in a Project panel with the mirror classes', () => {
    for (const options of [{}, PROJECT]) {
        const prefix = options.idPrefix || 'chat';
        const env = installDom(fetchStub());
        const instance = mount(env, options);
        try {
            for (const suffix of SUFFIXES) {
                const el = node(instance, `${prefix}-${suffix}`);
                assert.ok(el, `${prefix}-${suffix}`);
                if (prefix !== 'chat') {
                    assert.equal(el.classList.contains(`chat-${suffix}`), true, `${suffix}: the mirror class keeps the styling`);
                    assert.equal(node(instance, `chat-${suffix}`), null, `${suffix}: no Main id inside a Project panel`);
                }
            }
            assert.equal(instance.composer.input, node(instance, `${prefix}-input`), 'the composer owns the room\'s field');
        } finally { instance.destroy(); restoreDom(env.prior); }
    }
});

test('a room keeps its own draft: setDraft and insertAtCursor keep it, a reopened room restores it, Send clears it', async () => {
    const env = installDom(fetchStub());
    const frames = [];
    let instance = mount(env, { ...PROJECT, frames });
    try {
        const { composer } = instance;
        composer.setDraft('hello world');
        assert.equal(composer.getDraft(), 'hello world');
        assert.equal(sessionStorage.getItem('ouro_chat_draft:7'), 'hello world', 'kept under the room\'s own key');
        assert.equal(sessionStorage.getItem('ouro_chat_draft'), null, 'never under Main\'s');
        // A selection is replaced by the inserted words; the caret follows them in the focused field.
        const field = composer.input;
        let caret = null;
        field.setSelectionRange = (start, end) => { caret = [start, end]; };
        field.selectionStart = 6;
        field.selectionEnd = 11;
        composer.insertAtCursor('there');
        assert.equal(composer.getDraft(), 'hello there');
        assert.deepEqual(caret, [11, 11]);
        assert.equal(document.activeElement, field, 'the field has focus for the next word');
        assert.equal(sessionStorage.getItem('ouro_chat_draft:7'), 'hello there');
        // A collapsed caret inserts in place, nothing replaced.
        field.selectionStart = 5;
        field.selectionEnd = 5;
        composer.insertAtCursor(',');
        assert.equal(composer.getDraft(), 'hello, there');
        assert.deepEqual(caret, [6, 6]);

        instance.destroy();
        instance = mount(env, { ...PROJECT, frames });
        assert.equal(instance.composer.getDraft(), 'hello, there', 'a reopened room restores its draft');
        node(instance, 'pchat-p7-send').click();
        await tick();
        assert.equal(frames.length, 1);
        assert.deepEqual([frames[0].content, frames[0].chat_id, frames[0].project_id], ['hello, there', 7, 'p7']);
        assert.equal(instance.composer.getDraft(), '');
        assert.equal(sessionStorage.getItem('ouro_chat_draft:7'), null, 'Send clears the kept draft');
        instance.composer.setDraft('');
        assert.equal(sessionStorage.getItem('ouro_chat_draft:7'), null, 'an empty draft is not kept');
    } finally { instance.destroy(); restoreDom(env.prior); }
});

test('the input recall is seeded once from the room\'s own sent words, newest nearest', async () => {
    const row = (id, role, text) => ({ role, text, ts: `2026-10-17T10:0${id}:00.000Z`, history_id: `chat:${id}`,
        history_position: { source: 'chat', offset: id } });
    const env = installDom(fetchStub([row(1, 'user', 'first ask'), row(2, 'assistant', 'an answer'), row(3, 'user', 'second ask')]));
    const instance = mount(env, PROJECT);
    try {
        for (let i = 0; i < 10; i += 1) await tick();
        const field = instance.composer.input;
        field.setSelectionRange = (start, end) => { field.selectionStart = start; field.selectionEnd = end; };
        const press = (key) => { for (const listener of field.listeners.get('keydown')) listener({ key, target: field, preventDefault() {} }); };
        field.selectionStart = 0;
        field.selectionEnd = 0;
        press('ArrowUp');
        assert.equal(field.value, 'second ask');
        field.setSelectionRange(0, 0);
        press('ArrowUp');
        assert.equal(field.value, 'first ask', 'an assistant row is never recalled');
        press('ArrowDown');
        assert.equal(field.value, 'second ask');
        press('ArrowDown');
        assert.equal(field.value, '', 'past the newest: the words being written');
        assert.deepEqual(JSON.parse(sessionStorage.getItem('ouro_chat_input_history')), ['first ask', 'second ask']);
    } finally { instance.destroy(); restoreDom(env.prior); }
});
