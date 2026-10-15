import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatDecision } from '../modules/chat_decision.js';
import { createMainAttention } from '../modules/main_attention.js';
import { fixture as mediaFixture, NodeStub } from './helpers/media_dom.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

const turn = () => new Promise(resolve => setTimeout(resolve, 0));
const find = (node, predicate) => predicate(node) ? node : node.children.reduce((found, child) => found || find(child, predicate), null);
const question = (id, state = 'open') => ({ task_id: 'task-1', quiz_id: id, project_id: 'project-1',
    project_chat_id: 12, project_name: 'Build', quiz_state: state, question: 'Choose?', options: ['Yes', 'No'],
    materials: [{ task_id: 'task-1', name: 'brief.txt', label: 'brief.txt', sha256: 'a'.repeat(64) }] });

test('replacing an unknown mirror releases its material listeners and reader while retaining fresh materials and drafts', async t => {
    const fx = mediaFixture();
    const changes = {
        focus() { document.activeElement = this; },
        replaceChildren(...nodes) { for (const node of [...this.children]) node.remove(); this.append(...nodes); },
        replaceWith(node) { this.before(node); this.remove(); },
    };
    const prior = Object.fromEntries(Object.keys(changes).map(key => [key, Object.getOwnPropertyDescriptor(NodeStub.prototype, key)]));
    prior.ownerDocument = Object.getOwnPropertyDescriptor(NodeStub.prototype, 'ownerDocument');
    Object.assign(NodeStub.prototype, changes);
    Object.defineProperty(NodeStub.prototype, 'ownerDocument', { configurable: true, get() { return document; } });
    const classes = Object.getPrototypeOf(document.body.classList);
    const remove = classes.remove;
    classes.remove = function(...names) { names.forEach(name => this.toggle(name, false)); };
    t.after(() => {
        for (const [key, descriptor] of Object.entries(prior)) {
            if (descriptor) Object.defineProperty(NodeStub.prototype, key, descriptor); else delete NodeStub.prototype[key];
        }
        if (remove) classes.remove = remove; else delete classes.remove;
        fx.controller.destroy(); fx.restore();
    });
    t.mock.method(globalThis, 'fetch', async () => new Response('Exact retained text'));
    const column = document.createElement('div'); document.body.append(column);
    const released = [], markdownDisposed = [];
    const decision = createChatDecision({ isMain: true, currentQuestionHost: column,
        apiFetch: async () => assert.fail('replacement never answers a question'),
        showToast() {}, mountMarkdown: (node, text) => { node.textContent = text; },
        enhanceMarkdown: node => () => markdownDisposed.push(node),
        renderMaterial: fx.controller.buildDocumentBubble,
        releaseMaterials: node => { released.push(node); fx.controller.release(node); },
        frameNode: (_msg, card) => { const bubble = document.createElement('div'); bubble.append(card); return bubble; },
    });
    t.after(() => decision.destroy());
    const rows = [question('recover', 'unknown'), question('draft')];
    decision.syncCurrentQuestions(rows, true);
    const old = column.children[0], sibling = column.children[1];
    const draft = sibling.querySelector('.chat-quiz-comment');
    draft.value = 'Keep my unfinished answer';
    const oldFile = old.querySelector('.chat-file-card');
    await oldFile.click(); await turn();
    const reader = document.body.querySelector('.document-reader');
    assert.ok(reader.attributes.has('open'));
    draft.focus();
    decision.syncCurrentQuestions([question('recover'), rows[1]], true);
    assert.deepEqual(released, [old]);
    assert.equal(markdownDisposed.length, 1);
    assert.equal(reader.attributes.has('open'), false, 'the released material cannot keep its reader open');
    assert.equal(oldFile.listeners.get('click').size, 0, 'the detached material has no live listener');
    assert.ok([...oldFile.listeners.values()].every(listeners => listeners.size === 0));
    assert.equal(document.activeElement, draft);
    assert.equal(draft.value, 'Keep my unfinished answer');
    assert.equal(column.children[1], sibling);
    const fresh = column.children[0];
    assert.notEqual(fresh, old);
    assert.ok(fresh.querySelector('.chat-quiz-comment'));
    await oldFile.click(); await turn();
    assert.equal(reader.attributes.has('open'), false);
    await fresh.querySelector('.chat-file-card').click(); await turn();
    const freshReader = document.body.querySelector('.document-reader');
    assert.ok(freshReader?.attributes.has('open'), 'the replacement owns usable fresh listeners');
    fx.controller.release(fresh);
    assert.equal(freshReader.attributes.has('open'), false);
    decision.syncCurrentQuestions([question('recover'), rows[1], question('focused', 'unknown')], true);
    const focusedOld = column.children[2];
    focusedOld.querySelector('.chat-file-card').focus();
    decision.syncCurrentQuestions([question('recover'), rows[1], question('focused')], true);
    assert.equal(document.activeElement, column.children[2].querySelector('.chat-quiz-project'));
    assert.deepEqual(released, [old, focusedOld]);
    assert.equal(draft.value, 'Keep my unfinished answer');
});

test('Main renders the existing Try again control for a wait with no proven automatic route', async () => {
    const calls = [];
    const env = installDom(async (url, init) => {
        calls.push({ url, body: JSON.parse(init.body) });
        return { ok: true, status: 202, json: async () => ({ accepted: true }) };
    });
    const attention = createMainAttention({ mount: env.mount });
    try {
        attention.snapshot({ main_attention: [{ task_id: 'task-1', activity_id: 'task-1', project_id: 'project-1', phase: 'working',
            task_attempt: 1, attention_reason: 'model_access', model_waits: { w: {
                wait_id: 'w', state: 'waiting', reason: 'quota', role: 'main', source: 'codex',
                task_attempt: 1, revision: 7, auto_continue: true, requires_owner_action: true,
            } } }], main_attention_complete: true, current_questions_complete: true });
        const button = find(env.mount, node => Object.hasOwn(node.dataset, 'waitRetry'));
        assert.ok(button && !button.disabled);
        for (const handler of button.listeners.get('click')) await handler({ target: button });
        assert.equal(calls.length, 1);
        assert.equal(calls[0].url, '/api/decisions');
        assert.equal(calls[0].body.action, 'retry');
        assert.equal(calls[0].body.revision, 7);
    } finally { attention.destroy(); restoreDom(env.prior); }
});

test('a question whose original chat is unknown is a lasting gap, never a refresh failure', () => {
    const env = installDom(async () => assert.fail('the gap note makes no request'));
    const attention = createMainAttention({ mount: env.mount });
    const note = () => find(env.mount, node => node.className === 'msg-provenance');
    const routing = { task_id: 'task-1', quiz_id: 'q1', reason: 'publication_address_unknown' };
    try {
        attention.snapshot({ main_attention: [], main_attention_complete: true, current_questions_complete: false,
            current_questions_gaps: [routing] });
        assert.equal(note().hidden, false);
        assert.equal(note().textContent, "1 earlier question can't be shown: its original chat is unknown.");
        attention.snapshot({ main_attention: [], main_attention_complete: true, current_questions_complete: false,
            current_questions_gaps: [routing, { ...routing, quiz_id: 'q2' }, { reason: 'question_index_unavailable' }] });
        assert.match(note().textContent, /could not be refreshed/);
        attention.snapshot({ main_attention: [], main_attention_complete: true, current_questions_complete: false,
            current_questions_gaps: [routing] });
        attention.setConnected(false);  // a lost connection is a refresh failure, whatever the last census said
        assert.match(note().textContent, /could not be refreshed/);
        attention.setConnected(true);
        attention.snapshot({ main_attention: [], main_attention_complete: true, current_questions_complete: true,
            current_questions_gaps: [] });
        assert.equal(note().hidden, true);
    } finally { attention.destroy(); restoreDom(env.prior); }
});
