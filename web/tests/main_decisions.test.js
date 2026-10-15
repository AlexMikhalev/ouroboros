import assert from 'node:assert/strict';
import test, { mock } from 'node:test';
import { NodeStub, fixture, turn } from './chat_decision_fixture.js';
import { fixture as mediaFixture } from './helpers/media_dom.js';
import { ElementStub, installDom, restoreDom } from './chat_dom_fixture.js';
import { createChatInstance } from '../modules/chat.js';
import { createMainAttention } from '../modules/main_attention.js';
import { decorateProjectRow } from '../modules/project_answer.js';
import { mountResultFilesReader, syncQuestionMaterials } from '../modules/result_files.js';

const question = (id, patch = {}) => ({ task_id: 'task-1', quiz_id: id, project_id: 'project-1',
    project_chat_id: 12, project_name: 'Build', quiz_state: 'open', question: `Question ${id}?`,
    options: ['Yes', 'No'], ts: '2020-01-01T00:00:00Z', ...patch });

function currentFixture(extra = {}) {
    const current = new NodeStub();
    const history = new NodeStub();
    const fx = fixture({ isMain: true, currentQuestionHost: current,
        frameNode: (_msg, card) => { const bubble = new NodeStub(); bubble.append(card); return bubble; },
        insertMessageNode: node => history.append(node),
        removeMessageNode: node => node.remove(), ...extra });
    return { ...fx, current, history, cards: () => current.children.map(bubble => bubble.children[0]) };
}

test('Main attention forwards census, failed read and connection to its question owner', () => {
    const env = installDom();
    const calls = [], links = [];
    const attention = createMainAttention({ mount: env.mount, syncQuestions: (...args) => calls.push(args),
        connectQuestions: value => links.push(value) });
    try {
        const rows = [question('current')];
        attention.snapshot({ current_questions: rows, current_questions_complete: true }, 120);
        attention.snapshot(null, 130);
        attention.setConnected(false);
        assert.deepEqual(calls, [[rows, true, 120], [[], false, 130]]);
        assert.deepEqual(links, [false]);
        attention.setConnected(true);
        assert.equal(calls.length, 2, 'reconnection alone is not a new question census');
        assert.deepEqual(links, [false, true]);
    } finally { attention.destroy(); restoreDom(env.prior); }
});

test('owner can close a current question from Main with an explicit reason and stable retry identity', async () => {
    let attempts = 0;
    const fx = currentFixture({ fetchImpl: async (_url, init) => {
        const sent = JSON.parse(init.body); attempts++;
        if (attempts === 1) throw new Error('lost response');
        return { ok: true, status: 200, json: async () => ({ ok: true, state: 'closed',
            closed_reason: sent.reason, closed_actor: 'owner',
            question_delivery: { kind: 'closure', state: 'owed' } }) };
    } });
    try {
        fx.decision.syncCurrentQuestions([question('close-from-main', { quiz_state: 'expired_terminal' })], true);
        const card = fx.cards()[0], reason = card.querySelector('.chat-quiz-close-reason');
        const close = card.querySelector('.chat-quiz-close-submit');
        assert.equal(close.disabled, true);
        reason.value = 'The format decision is no longer needed.'; reason.listeners.get('input')();
        close.click(); await turn();
        assert.equal(card.dataset.state, 'expired_terminal', 'a lost response never invents closure');
        close.click(); await turn();
        const [first, retry] = fx.calls.map(call => JSON.parse(call.init.body));
        assert.deepEqual(retry, first);
        assert.equal(first.action, 'close'); assert.equal(first.reason, reason.value);
        assert.ok(!('option_index' in first) && !('comment' in first), 'closure never becomes an answer');
        assert.equal(card.dataset.state, 'closed');
        assert.equal(card.querySelector('.chat-quiz-closed-reason').textContent, reason.value);
        assert.equal(card.querySelector('.chat-quiz-comment'), null);
        assert.match(card.querySelector('.chat-quiz-status-text').textContent, /update pending/);
    } finally { fx.decision.destroy(); fx.restore(); }
});

test('owner close losing an answer race displays the canonical answer and cannot revive the close control', async () => {
    const fx = currentFixture({ fetchImpl: async () => ({ ok: false, status: 409,
        json: async () => ({ state: 'answered', answered_index: 1, comment: 'Keep the original.' }) }) });
    try {
        const row = question('race'); fx.decision.syncCurrentQuestions([row], true);
        const card = fx.cards()[0], reason = card.querySelector('.chat-quiz-close-reason');
        reason.value = 'No longer needed'; reason.listeners.get('input')();
        card.querySelector('.chat-quiz-close-submit').click(); await turn();
        assert.equal(card.dataset.state, 'answered');
        assert.equal(card.querySelectorAll('.chat-quiz-option')[1].classList.contains('chosen'), true);
        fx.decision.syncCurrentQuestions([row], true);
        assert.equal(card.dataset.state, 'answered');
        assert.equal(card.querySelector('.chat-quiz-close-submit'), null);
    } finally { fx.decision.destroy(); fx.restore(); }
});

test('canonical current questions recover the entire accepted horizon without loading or duplicating history', () => {
    const fx = currentFixture();
    try {
        const rows = Array.from({ length: 25 }, (_, id) => question(`q-${id}`,
            { quiz_state: id % 2 ? 'expired_terminal' : 'open' }));
        fx.decision.syncCurrentQuestions(rows, true);
        assert.equal(fx.cards().length, 25);
        assert.equal(fx.history.children.length, 0);
        assert.ok(fx.cards().every(card => card.querySelector('.chat-quiz-comment')));
        fx.decision.appendQuestionPointer(rows[0]);
        assert.equal(fx.cards().length, 25, 'a later old history delivery names the same form');
        const draft = fx.cards()[0].querySelector('.chat-quiz-comment');
        draft.value = 'Keep this unfinished answer';
        fx.decision.syncCurrentQuestions(rows, true);
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-comment'), draft);
        assert.equal(draft.value, 'Keep this unfinished answer');
        fx.decision.syncCurrentQuestions([], false);
        assert.equal(fx.cards().length, 25, 'an incomplete source is not an empty current set');
        assert.equal(fx.cards()[0].dataset.state, 'unknown');
        assert.equal(draft.disabled, true);
        assert.equal(draft.value, 'Keep this unfinished answer');
        fx.decision.syncCurrentQuestions(rows, true);
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-comment'), draft);
        assert.equal(draft.disabled, false);
        fx.decision.syncCurrentQuestions(rows.slice(1), true);
        assert.equal(fx.cards().length, 24, 'complete canonical withdrawal removes the old view');
    } finally { fx.decision.destroy(); fx.restore(); }
});

test('history alone mounts safe unknown until a canonical source confirms an accepted question', async () => {
    const fx = currentFixture({ fetchDetail: async () => null });
    try {
        fx.decision.appendQuestionPointer(question('old'));
        await turn();
        assert.equal(fx.cards()[0].dataset.state, 'unknown');
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-comment'), null);
        assert.ok(fx.cards()[0].querySelector('.chat-live-project-name'));
        fx.decision.syncCurrentQuestions([question('old', { quiz_state: 'expired_terminal' })], true);
        assert.equal(fx.cards()[0].dataset.state, 'expired_terminal');
        assert.ok(fx.cards()[0].querySelector('.chat-quiz-comment'));
    } finally { fx.decision.destroy(); fx.restore(); }
});

test('explicit closure shows its reason, accepts no answer, and stale snapshots cannot revive it', () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const fx = currentFixture();
    try {
        const row = question('close');
        fx.decision.syncCurrentQuestions([row], true);
        fx.decision.syncCurrentQuestions([], false);
        fx.decision.applyQuizStateFrame(fx.current, { task_id: row.task_id, quiz_id: row.quiz_id,
            state: 'closed', closed_reason: 'The owner cancelled this output.' });
        assert.equal(fx.cards()[0].dataset.state, 'closed');
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-closed-reason').textContent, 'The owner cancelled this output.');
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-comment'), null);
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-answer'), null);
        fx.decision.syncCurrentQuestions([row], true);
        assert.equal(fx.cards()[0].dataset.state, 'closed');
        mock.timers.tick(5000);
        assert.equal(fx.cards().length, 0);
        fx.decision.syncCurrentQuestions([row], true);
        assert.equal(fx.cards().length, 0);
    } finally { fx.decision.destroy(); fx.restore(); mock.timers.reset(); }
});

test('canonical owed answers and closures survive reload and time until their delivery is confirmed', () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const fx = currentFixture();
    try {
        const rows = [question('answered', { quiz_state: 'answered', answered_index: 0,
            question_delivery: { kind: 'answer', state: 'owed' } }),
        question('closed', { quiz_state: 'closed', closed_reason: 'The requested work was cancelled.',
            question_delivery: { kind: 'closure', state: 'owed' } })];
        fx.decision.syncCurrentQuestions(rows, true);
        assert.equal(fx.cards().length, 2, 'a fresh Main includes already settled but undischarged records');
        assert.match(fx.cards()[0].querySelector('.chat-quiz-status-text').textContent, /Answer recorded.*delivery pending/);
        assert.match(fx.cards()[1].querySelector('.chat-quiz-status-text').textContent, /Question closed.*update pending/);
        assert.ok(fx.cards().every(card => !card.querySelector('.chat-quiz-comment')));
        mock.timers.tick(60000);
        assert.equal(fx.cards().length, 2);
        fx.decision.syncCurrentQuestions([], false);
        mock.timers.tick(60000);
        assert.equal(fx.cards().length, 2, 'source failure cannot discharge the records');
        fx.decision.syncCurrentQuestions(rows.map(row => ({ ...row,
            question_delivery: { ...row.question_delivery, state: 'delivered' } })), true);
        mock.timers.tick(5000);
        assert.equal(fx.cards().length, 0);
        fx.decision.syncCurrentQuestions(rows, true);
        assert.equal(fx.cards().length, 0, 'stale owed delivery cannot undo a canonical receipt');
    } finally { fx.decision.destroy(); fx.restore(); mock.timers.reset(); }
});

test('owner removes an unknown late answer from Needs you once, without answering again or resending', async () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const unknown = { kind: 'answer', state: 'owed', reason_code: 'late_answer_delivery_unknown' };
    let attempts = 0;
    const fx = currentFixture({ fetchImpl: async (_url, init) => {
        attempts++;
        if (attempts === 1) throw new Error('lost response');
        return { ok: true, status: 200, json: async () => ({ ok: true, state: 'answered', answered_index: 1,
            duplicate: false, question_delivery: { ...unknown, dismissed: true } }) };
    } });
    try {
        const rows = [question('unknown', { quiz_state: 'answered', answered_index: 1, question_delivery: unknown }),
            question('pending', { quiz_state: 'answered', answered_index: 0, question_delivery: { kind: 'answer', state: 'owed' } })];
        fx.decision.syncCurrentQuestions(rows, true);
        const [card, pending] = fx.cards();
        assert.match(card.querySelector('.chat-quiz-status-text').textContent, /Answer recorded.*delivery unknown/);
        const remove = card.querySelector('.chat-quiz-dismiss');
        assert.equal(remove.textContent, 'Remove from Needs you');
        assert.equal(pending.querySelector('.chat-quiz-dismiss'), null, 'only an unknown delivery is removable');
        remove.click(); await turn();
        assert.ok(card.querySelector('.chat-quiz-dismiss'), 'a lost response removes nothing');
        card.querySelector('.chat-quiz-dismiss').click(); await turn();
        const [first, retry] = fx.calls.map(call => JSON.parse(call.init.body));
        assert.deepEqual(retry, first, 'the retry replays the same removal request');
        assert.deepEqual(Object.keys(first).sort(), ['action', 'decision_id', 'request_id']);
        assert.equal(first.action, 'dismiss'); assert.equal(first.decision_id, 'quiz:task-1:unknown');
        assert.equal(card.querySelector('.chat-quiz-dismiss'), null);
        assert.match(card.querySelector('.chat-quiz-status-text').textContent, /delivery unknown/, 'the record stays honest');
        mock.timers.tick(5000);
        assert.deepEqual(fx.cards().map(view => view.dataset.quizId), ['pending']);
        fx.decision.syncCurrentQuestions(rows, true);
        assert.deepEqual(fx.cards().map(view => view.dataset.quizId), ['pending'], 'a stale census cannot bring it back');
    } finally { fx.decision.destroy(); fx.restore(); mock.timers.reset(); }
});

test('a removal recorded by another client settles this Main copy and drops its control', () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const unknown = { kind: 'answer', state: 'owed', reason_code: 'late_answer_delivery_unknown' };
    const fx = currentFixture({ fetchImpl: async () => assert.fail('this client never posts') });
    try {
        const row = question('elsewhere', { quiz_state: 'answered', answered_index: 0, question_delivery: unknown });
        fx.decision.syncCurrentQuestions([row], true);
        assert.ok(fx.cards()[0].querySelector('.chat-quiz-dismiss'));
        fx.decision.applyQuizStateFrame(fx.current, { task_id: 'task-1', quiz_id: 'elsewhere', state: 'answered',
            answered_index: 0, question_delivery: { ...unknown, dismissed: true } });
        assert.equal(fx.cards()[0].querySelector('.chat-quiz-dismiss'), null);
        mock.timers.tick(5000);
        assert.equal(fx.cards().length, 0);
        assert.equal(fx.calls.length, 0);
    } finally { fx.decision.destroy(); fx.restore(); mock.timers.reset(); }
});

test('answer acknowledgement with pending delivery remains recorded and visible', async () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const fx = currentFixture({ fetchImpl: async () => ({ ok: true, json: async () => ({ ok: true,
        state: 'answered', answered_index: 0, answered_after_terminal: true, forwarded: false,
        question_delivery: { kind: 'answer', state: 'owed' } }) }) });
    try {
        fx.decision.syncCurrentQuestions([question('ack')], true);
        fx.cards()[0].querySelector('.chat-quiz-option').click();
        await turn();
        assert.equal(fx.cards()[0].dataset.state, 'answered');
        assert.match(fx.toasts.at(-1).text, /delivery.*pending/);
        mock.timers.tick(60000);
        assert.equal(fx.cards().length, 1);
        fx.decision.syncCurrentQuestions([], true);
        assert.equal(fx.cards().length, 0, 'complete canonical withdrawal discharges the visible copy');
    } finally { fx.decision.destroy(); fx.restore(); mock.timers.reset(); }
});

test('question materials use validated artifact identities and display their original labels', () => {
    const fx = currentFixture();
    try {
        fx.decision.syncCurrentQuestions([question('material', { materials: [
            { task_id: 'task-1', name: 'material-a.csv', label: 'Schedule.csv', sha256: 'abc', download_url: 'javascript:alert(1)' },
            { task_id: 'task-1', name: '../secrets', sha256: 'abc' },
            { name: 'unbound.csv', sha256: 'abc' },
        ] })], true);
        const links = fx.cards()[0].querySelector('.chat-quiz-materials').children;
        assert.equal(links.length, 1);
        assert.equal(links[0].textContent, 'Schedule.csv');
        assert.equal(links[0].href, '/api/tasks/task-1/artifacts/material-a.csv');
    } finally { fx.decision.destroy(); fx.restore(); }
});

test('question photos reuse the existing file preview and action dialog from their custody address', async () => {
    const fx = mediaFixture();
    try {
        const card = document.createElement('div');
        document.body.append(card);
        let rendered = '';
        syncQuestionMaterials(card, [{ task_id: 'task-1', name: 'material-safe.png', label: 'Design.png',
            size: 24, sha256: 'abc', download_url: '/api/files/download?path=arbitrary' }], row => {
            const bubble = fx.controller.buildDocumentBubble(row);
            rendered = bubble.innerHTML;
            return bubble;
        });
        const content = card.querySelector('.message');
        assert.ok(content.querySelector('.chat-file-thumb'));
        assert.match(rendered, /\/api\/tasks\/task-1\/artifacts\/material-safe.png/);
        assert.doesNotMatch(rendered, /arbitrary/);
        await card.querySelector('.chat-file-card').click();
        assert.ok(document.body.querySelector('.chat-file-dialog [data-file-action="download"]')
            || document.body.querySelector('[data-file-action="download"]'));
        fx.controller.release(card);
        assert.equal(document.body.querySelector('.chat-file-dialog').attributes.has('open'), false,
            'retiring the question closes its reader');
    } finally { fx.controller.destroy(); fx.restore(); }
});

const find = (node, predicate) => predicate(node) ? node
    : node.children.reduce((found, child) => found || find(child, predicate), null);

test('Main model access uses the existing revision-bound recovery decision and refreshes selection', async () => {
    const calls = [];
    const wait = { wait_id: 'login', revision: 1, task_attempt: 1, role: 'main', reason: 'auth',
        model: 'claudexor::route=model', state: 'waiting', auto_continue: true };
    const env = installDom(async (url, init) => {
        calls.push({ url, body: JSON.parse(init.body) });
        return { ok: true, json: async () => ({ ok: true, wait: { ...wait, revision: 2, state: 'resolved' } }) };
    });
    const attention = createMainAttention({ mount: env.mount });
    try {
        assert.equal(attention.questions.getAttribute('data-i18n-authored'), '1');
        const row = { task_id: 'access-task', project_id: 'build', project_name: 'Build', phase: 'working',
            task_attempt: 1, model_waits: { login: wait }, attention_reason: 'model_access' };
        attention.snapshot({ main_attention: [row], main_attention_complete: true });
        const retry = find(env.mount, node => Object.hasOwn(node.dataset, 'waitRetry'));
        assert.ok(retry, 'the model-wait controller mounted its actual recovery controls in Main');
        retry.click();
        await turn();
        assert.equal(calls.length, 1);
        assert.equal(calls[0].url, '/api/decisions');
        assert.equal(calls[0].body.decision_id, 'model_wait:access-task:login');
        assert.equal(calls[0].body.revision, 1);
        assert.equal(calls[0].body.action, 'retry');
        assert.equal(find(env.mount, node => node.classList.contains('model-wait-row')), null);
        attention.snapshot({ main_attention: [{ ...row, model_waits: {} }], main_attention_complete: true });
        assert.equal(find(env.mount, node => node.classList.contains('model-wait-row')), null);
    } finally { attention.destroy(); restoreDom(env.prior); }
});

test('current Main pause offers the existing real Resume action and withdraws it with current authority', async () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const calls = [];
    const env = installDom(async (url, init) => {
        calls.push({ url, init });
        return { ok: true, json: async () => ({ ok: true }) };
    });
    const attention = createMainAttention({ mount: env.mount });
    try {
        document.body = env.mount;
        const row = { task_id: 'paused', activity_id: 'paused', project_id: 'build', project_name: 'Build',
            phase: 'budget_paused', pause_cause: 'budget', can_resume: true, attention_reason: 'budget', model_waits: {} };
        attention.snapshot({ main_attention: [row], main_attention_complete: true, current_questions_complete: true });
        const resume = find(env.mount, node => node.dataset.resumeRun === '1');
        assert.ok(resume);
        await resume.listeners.get('click')[0]({ stopPropagation() {} });
        assert.equal(calls[0].url, '/api/tasks/paused/resume');
        assert.equal(calls[0].init.method, 'POST');
        attention.setConnected(false);
        assert.equal(resume.disabled, true);
        attention.snapshot({ main_attention: [], main_attention_complete: false });
        assert.ok(find(env.mount, node => node.dataset.taskId === 'paused'));
        attention.setConnected(true);
        assert.equal(resume.disabled, true, 'reconnection alone cannot restore stale control authority');
        attention.snapshot({ main_attention: [], main_attention_complete: true, current_questions_complete: true });
        assert.equal(find(env.mount, node => node.dataset.taskId === 'paused'), null);
    } finally { attention.destroy(); restoreDom(env.prior); mock.timers.reset(); }
});

test('a complete read that lands while disconnected restores nothing; only the census after reopening does', () => {
    const env = installDom(async () => assert.fail('withdrawn authority makes no request'));
    const calls = [], links = [];
    const attention = createMainAttention({ mount: env.mount, syncQuestions: (...args) => calls.push(args),
        connectQuestions: value => links.push(value) });
    try {
        document.body = env.mount;
        const wait = { wait_id: 'login', revision: 1, task_attempt: 1, role: 'main', reason: 'auth',
            model: 'claudexor::route=model', state: 'waiting', auto_continue: true };
        const rows = [question('current')];
        const census = { current_questions: rows, current_questions_complete: true, main_attention_complete: true,
            main_attention: [{ task_id: 'paused', activity_id: 'paused', project_id: 'build', project_name: 'Build',
                phase: 'budget_paused', pause_cause: 'budget', can_resume: true, cancelable: true,
                attention_reason: 'budget', task_attempt: 1, model_waits: { login: wait } }] };
        attention.snapshot(census, 100);
        const resume = find(env.mount, node => node.dataset.resumeRun === '1');
        const stop = find(env.mount, node => node.textContent === 'Stop…');
        const waits = find(env.mount, node => node.classList.contains('model-waits'));
        const note = find(env.mount, node => node.className === 'msg-provenance');
        const timeline = [];
        const observe = stage => timeline.push({ stage, resume: !resume.disabled, stop: !stop.disabled,
            waits: !waits.inert, current: note.hidden, questions: calls.length, connected: links.at(-1) ?? true });
        observe('census');
        attention.setConnected(false);
        observe('disconnected');
        attention.snapshot(census, 200);  // HTTP still answers while the socket is down
        observe('offline body');
        attention.setConnected(true);
        observe('reopened');
        attention.snapshot(census, 300);
        observe('fresh census');
        const restored = { resume: true, stop: true, waits: true, current: true };
        const withdrawn = { resume: false, stop: false, waits: false, current: false };
        assert.deepEqual(timeline, [{ stage: 'census', ...restored, questions: 1, connected: true },
            { stage: 'disconnected', ...withdrawn, questions: 1, connected: false },
            { stage: 'offline body', ...withdrawn, questions: 1, connected: false },
            { stage: 'reopened', ...withdrawn, questions: 1, connected: true },
            { stage: 'fresh census', ...restored, questions: 2, connected: true }]);
        assert.deepEqual(calls, [[rows, true, 100], [rows, true, 300]]);
    } finally { attention.destroy(); restoreDom(env.prior); }
});

test('a Main question regains its form only after the reopen: no offline pointer or read crossing the close restores it', async t => {
    const original = ElementStub.prototype.querySelector;
    ElementStub.prototype.querySelector = function(selector) {
        return original.call(this, selector) || this.children.map(child => child.querySelector(selector)).find(Boolean) || null;
    };
    ElementStub.prototype.replaceWith = function(node) { this.parentNode.replaceChild(node, this); };
    t.after(() => { ElementStub.prototype.querySelector = original; delete ElementStub.prototype.replaceWith; });
    const asked = { task_id: 'question-task', quiz_id: 'cached', quiz_state: 'open', project_id: 'build', project_name: 'Build',
        project_chat_id: 12, question: 'Keep this output?', options: ['Yes', 'No'], ts: '2026-10-13T10:00:00Z' };
    const newer = { ...asked, quiz_id: 'newer', question: 'A newer live question?', ts: '2026-10-13T10:05:00Z' };
    const detail = { task_id: 'question-task', project_id: 'build',
        question_rooms: Object.fromEntries([asked, newer].map(row => [row.quiz_id, { state: 'known', chat_id: 12, project_id: 'build' }])),
        owner_quiz: Object.fromEntries(
        [asked, newer].map(row => [row.quiz_id, { ...row, state: 'open', asked_at: row.ts }])) };
    const reads = [];
    let started = 0;
    const env = installDom(url => String(url) === '/api/tasks/question-task'
        ? new Promise(resolve => { started += 1; reads.push(() => resolve({ ok: true, json: async () => detail })); })
        : Promise.resolve({ ok: true, json: async () => (String(url).startsWith('/api/chat/history')
            ? { messages: [], window: { complete: true } } : {}) }));
    const handlers = new Map();
    let generation = 0;
    const instance = createChatInstance({
        ws: { on(type, fn) { handlers.set(type, [...(handlers.get(type) || []), fn]); return () => {}; },
            isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 }, updateUnreadBadge() {},
        // The sequencer is app.js's; here each body is handed to the real Main consumer directly.
        stateSnapshots: { begin: () => ({ generation: ++generation, requestedAt: Date.now() }),
            gate() { return Promise.resolve(this.begin()); }, isCurrent: () => true, apply() {}, fail() {}, latest: () => null },
        chatId: 1, idPrefix: 'chat', mountEl: env.mount });
    const emit = (type, msg = {}) => (handlers.get(type) || []).forEach(fn => fn(msg));
    // Reads start a turn after their request; each lands only once everything requested so far was asked.
    const land = async (count = Infinity) => {
        await turn();
        while (reads.length && count-- > 0) reads.shift()();
        for (let i = 0; i < 8; i += 1) await turn();
    };
    // A real waiting Project question: the census names it and its activity carries the pointer (gateway/state.py).
    const census = (patch = {}) => instance.hydrateStateSnapshot({ current_questions: [asked], current_questions_complete: true,
        main_attention: [], main_attention_complete: true, active_chat_activities_complete: true,
        active_chat_activities: [{ activity_id: 'question-task', chat_id: 12, project_id: 'build',
            required_question: { ...asked, owner_wait_state: 'waiting' } }], ...patch }, Infinity);
    const card = id => find(env.mount, node => node.classList?.contains('chat-quiz-card') && node.dataset.quizId === id);
    const comment = () => card('cached').querySelector('.chat-quiz-comment');
    const timeline = [];
    const view = stage => timeline.push({ stage, state: card('cached').dataset.state, comment: !comment().disabled, reads: started });
    try {
        census();
        await land();
        comment().value = 'Draft kept across the drop';
        view('census');
        emit('close');
        view('closed');
        census();  // HTTP still answers while the socket is down, and its pointer's read too
        await land();
        view('offline body');
        emit('open', { previouslyConnected: true });
        view('reopened, census held');
        census();
        view('fresh census');
        // An incomplete census leaves the copy to its own canonical read, still in flight at the next close.
        census({ current_questions: [], current_questions_complete: false });
        await turn();  // its request leaves before the close
        view('read in flight');
        emit('close');
        emit('open', { previouslyConnected: true });
        // Connected again, the copy's next pointer reads anew while the older read is still out.
        census({ current_questions: [], current_questions_complete: false });
        await land(1);
        view('read begun before the close lands after the reopen');
        census({ current_questions: [], current_questions_complete: false });
        await turn();
        view('a further pointer while the newer read is out');
        await land();
        view('the read begun after the reopen gives the form back');
        // A pointer arriving on the reopened socket validates by its own read.
        emit('chat', { ...newer, type: 'chat', chat_id: 1, role: 'system', system_type: 'project_question_pointer' });
        await land();
        const live = card('newer').dataset.state;
        census();
        view('fresh census again');
        const on = { state: 'open', comment: true }, off = { state: 'unknown', comment: false };
        assert.deepEqual(timeline, [{ stage: 'census', ...on, reads: 0 }, { stage: 'closed', ...off, reads: 0 },
            { stage: 'offline body', ...off, reads: 0 }, { stage: 'reopened, census held', ...off, reads: 0 },
            { stage: 'fresh census', ...on, reads: 0 }, { stage: 'read in flight', ...off, reads: 1 },
            { stage: 'read begun before the close lands after the reopen', ...off, reads: 2 },
            { stage: 'a further pointer while the newer read is out', ...off, reads: 2 },
            { stage: 'the read begun after the reopen gives the form back', ...on, reads: 2 },
            { stage: 'fresh census again', ...on, reads: 3 }]);
        assert.equal(live, 'open', 'a read begun after the reopen validates its own copy');
        assert.equal(comment().value, 'Draft kept across the drop');
    } finally { instance.destroy(); restoreDom(env.prior); }
});

test('an interrupted owner Resume of a member offers the same Resume; the owner\'s own Pause never does', async () => {
    const calls = [];
    const env = installDom(async (url, init) => {
        calls.push({ url, init });
        return { ok: true, json: async () => ({ ok: true }) };
    });
    const attention = createMainAttention({ mount: env.mount });
    try {
        document.body = env.mount;
        // The census rows of a member whose consumed owner Resume a stop interrupted (#1716):
        // a held boot restore says restart, an acknowledged return has no cause of its own.
        const member = (id, cause, reason) => ({ task_id: id, activity_id: id, project_id: 'build',
            project_name: 'Build', root_task_id: 'root', phase: 'budget_paused', pause_cause: cause,
            can_resume: true, cancelable: false, attention_reason: reason, model_waits: {} });
        attention.snapshot({ main_attention: [member('held', 'restart', 'restart'),
            member('returned', 'unknown', 'resume'), member('own', 'owner', 'resume')],
            main_attention_complete: true, current_questions_complete: true });
        const card = id => find(env.mount, node => node.dataset.taskId === id);
        const resume = id => find(card(id), node => node.dataset.resumeRun === '1');
        assert.equal(find(card('held'), node => node.classList.contains('main-attention-reason')).textContent,
            'This task needs Resume after the restart.');
        assert.equal(find(card('returned'), node => node.classList.contains('main-attention-reason')).textContent,
            'This task needs an explicit Resume.');
        assert.equal(resume('own'), null, 'the owner\'s own Pause is never asked for again');
        assert.equal(find(card('held'), node => node.textContent === 'Stop…'), null, 'Stop addresses roots only');
        for (const id of ['held', 'returned']) {
            await resume(id).listeners.get('click')[0]({ stopPropagation() {} });
            assert.equal(calls.at(-1).url, `/api/tasks/${id}/resume`);
            assert.equal(calls.at(-1).init.method, 'POST');
        }
        assert.equal(calls.length, 2);
    } finally { attention.destroy(); restoreDom(env.prior); }
});

test('Main Files reads the existing inventory lazily and exposes every available root file', async () => {
    const env = installDom();
    let calls = 0;
    try {
        const bubble = document.createElement('div');
        const actions = document.createElement('div');
        env.mount.append(bubble);
        bubble.append(actions);
        const button = mountResultFilesReader(bubble, actions, 'task-1', async () => {
            calls++;
            return { task_id: 'task-1', artifacts: Array.from({ length: 12 }, (_, id) =>
                ({ name: `file-${id}.txt`, status: 'ready', size: 1 })) };
        });
        assert.equal(calls, 0);
        button.click();
        await turn();
        const body = bubble.querySelector('.project-answer-files');
        assert.equal(calls, 1);
        assert.equal(body.querySelector('.msg-provenance').textContent, 'Available files');
        assert.equal(body.children.filter(node => node.classList.contains('project-answer-file')).length, 12);
        assert.ok(body.children.slice(1).every(node => node.children[0].href.startsWith('/api/tasks/task-1/artifacts/')));
        button.click(); button.click();
        assert.equal(calls, 1);
    } finally { restoreDom(env.prior); }
});

test('a missing or foreign Main inventory is disclosed and retryable, never substituted', async () => {
    const env = installDom();
    let calls = 0;
    try {
        const bubble = document.createElement('div');
        const actions = document.createElement('div');
        env.mount.append(bubble);
        bubble.append(actions);
        const button = mountResultFilesReader(bubble, actions, 'task-1', async () => {
            calls++;
            return calls === 1 ? { task_id: 'foreign', artifacts: [{ name: 'wrong.txt' }] }
                : { task_id: 'task-1', artifacts: [] };
        });
        button.click(); await turn();
        assert.match(bubble.querySelector('.project-answer-files').textContent, /could not be loaded/);
        button.click(); button.click(); await turn();
        assert.equal(calls, 2);
        assert.match(bubble.querySelector('.project-answer-files').querySelector('.msg-provenance').textContent, /No available files/);
    } finally { restoreDom(env.prior); }
});

test('Main Files changes its height only through the stable-viewport writer its final row forwards', async t => {
    const originalAfter = ElementStub.prototype.after;
    ElementStub.prototype.after = function(node) { return this.insertAdjacentElement('afterend', node); };
    t.after(() => { if (originalAfter) ElementStub.prototype.after = originalAfter; else delete ElementStub.prototype.after; });
    const env = installDom();
    const reads = [], writes = [];
    try {
        const bubble = document.createElement('div');
        const message = document.createElement('div');
        message.className = 'message';
        bubble.append(message);
        env.mount.append(bubble);
        const body = () => bubble.querySelector('.project-answer-files');
        const onDomWrite = (mutate, options) => {
            const result = mutate();
            writes.push({ hidden: body().hidden, text: body().textContent, rows: body().children.length, result, options });
            return result;
        };
        const actions = decorateProjectRow(bubble, { role: 'assistant', projectId: 'project-1', projectName: 'Build',
            systemType: 'project_completion_summary', taskId: 'task-1', onDomWrite,
            fetchDetail: id => new Promise(resolve => reads.push({ id, resolve })) });
        const button = actions.children.find(node => node.textContent === 'Files');
        const step = () => writes.splice(0).map(({ hidden, text, rows, result, options }) =>
            ({ hidden, text: text.startsWith('Available files could not') ? 'error' : text, rows, result, options }));
        // The expansion and its loading line are one write; the reply is another, wherever it lands.
        button.click();
        assert.deepEqual(step(), [{ hidden: false, text: 'Loading available files…', rows: 0, result: true, options: undefined }]);
        reads[0].resolve({ task_id: 'foreign', artifacts: [] }); await turn();
        assert.deepEqual(step(), [{ hidden: false, text: 'error', rows: 0, result: true, options: undefined }]);
        button.click(); button.click();
        assert.deepEqual(step().map(({ hidden, text }) => [hidden, text]), [[true, 'error'], [false, 'Loading available files…']]);
        reads[1].resolve({ task_id: 'task-1', artifacts: [{ name: 'report.md', status: 'ready' },
            { name: 'brief.txt', relpath: 'research/brief.txt', status: 'ready' }] }); await turn();
        assert.deepEqual(step(), [{ hidden: false, text: '', rows: 3, result: true, options: undefined }]);
        button.click(); button.click();
        assert.deepEqual(step().map(({ hidden, rows }) => [hidden, rows]), [[true, 3], [false, 3]]);
        assert.deepEqual(reads.map(({ id }) => id), ['task-1', 'task-1'], 'a loaded inventory is never read again');
    } finally { restoreDom(env.prior); }
});
