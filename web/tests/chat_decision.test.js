import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatDecision } from '../modules/chat_decision.js';
class Classes {
    constructor() { this.values = new Set(); }
    set(value) { this.values = new Set(String(value || '').split(/\s+/).filter(Boolean)); }
    add(...values) { values.forEach((value) => this.values.add(value)); }
    remove(...values) { values.forEach((value) => this.values.delete(value)); }
    contains(value) { return this.values.has(value); }
    toggle(value, force = !this.contains(value)) {
        if (force) this.add(value); else this.remove(value);
        return Boolean(force);
    }
}

class NodeStub {
    constructor(tag = 'div') {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.dataset = {};
        this.classList = new Classes();
        this.disabled = false;
        this.listeners = new Map();
        this.type = '';
        this._text = '';
    }
    set className(value) { this.classList.set(value); }
    get className() { return [...this.classList.values].join(' '); }
    set textContent(value) { this._text = String(value ?? ''); }
    get textContent() { return this._text; }
    append(...nodes) { nodes.forEach((node) => { node.parentNode = this; this.children.push(node); }); }
    remove() {
        const parent = this.parentNode;
        if (!parent) return;
        const i = parent.children.indexOf(this);
        if (i >= 0) parent.children.splice(i, 1);
        this.parentNode = null;
    }
    before(node) {
        const parent = this.parentNode;
        if (!parent) return;
        node.parentNode = parent;
        parent.children.splice(parent.children.indexOf(this), 0, node);
    }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    click() { const handler = this.listeners.get('click'); if (handler) handler(); }
    matchesClass(name) { return this.classList.contains(name); }
    collect(name, out = []) {
        if (this.matchesClass(name)) out.push(this);
        this.children.forEach((child) => child.collect(name, out));
        return out;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    querySelectorAll(selector) { return this.collect(selector.replace(/^\./, '')); }
}

function countPropertyWrites(target, key) {
    let value = target[key];
    let writes = 0;
    Object.defineProperty(target, key, {
        configurable: true,
        get: () => value,
        set: (next) => { writes += 1; value = next; },
    });
    return () => writes;
}

function fixture({ fetchImpl, renderMarkdown, onDomWrite, fetchDetail } = {}) {
    const prior = { document: globalThis.document, crypto: globalThis.crypto };
    globalThis.document = { createElement: (tag) => new NodeStub(tag) };
    if (!globalThis.crypto?.randomUUID) Object.defineProperty(globalThis, 'crypto', {
        configurable: true, value: { randomUUID: () => 'fixed-request-id' },
    });
    const toasts = [];
    const calls = [];
    const decision = createChatDecision({
        apiFetch: async (url, init) => {
            calls.push({ url, init });
            if (fetchImpl) return fetchImpl(url, init);
            const sent = JSON.parse(init.body);
            return { ok: true, status: 200, json: async () => ({ ok: true, state: 'answered',
                ...(Number.isInteger(sent.option_index) ? { answered_index: sent.option_index } : {}),
                ...(sent.comment ? { comment: sent.comment } : {}) }) };
        },
        frameNode: (_msg, node) => node,
        renderMarkdown,
        enhanceMarkdown: renderMarkdown ? () => {} : null,
        showToast: (text, tone) => toasts.push({ text, tone }),
        onDomWrite,
        fetchDetail,
    });
    return { decision, toasts, calls, restore: () => {
        globalThis.document = prior.document;
        Object.defineProperty(globalThis, 'crypto', { configurable: true, value: prior.crypto });
    } };
}

const WS_MSG = {
    type: 'quiz', role: 'assistant', quiz_id: 'qz-1', task_id: 't-1',
    question: 'Merge now?', stake: 'release timing',
    assumption: 'continuing with the merge', state: 'open',
    options: [{ label: 'Yes' }, { label: 'No', detail: 'wait for CI' }],
    ts: '2026-08-31T10:00:00Z',
};

test('Project pointer uses task/quiz identity and retains a reordered answer without a second quiz', () => {
    const fx = fixture();
    try {
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'qz-1', state: 'answered' });
        const row = { task_id: 't-1', quiz_id: 'qz-1', project_id: 'p1', project_chat_id: 23,
            project_name: 'Storage', quiz_state: 'open' };
        const pointer = fx.decision.buildQuestionPointer(row);
        assert.equal(pointer.querySelector('.project-question-status').textContent, 'You answered');
        assert.equal(pointer.querySelector('.system-message-action').textContent, 'View answer');
        assert.equal(pointer.querySelectorAll('.chat-quiz-option').length, 0);
        const labelWrites = countPropertyWrites(pointer.querySelector('.project-question-status'), 'textContent');
        assert.equal(fx.decision.buildQuestionPointer({ ...row, ts: 'later' }), null);
        assert.equal(labelWrites(), 0, 'unchanged pointer projection preserves selected text');
        fx.decision.applyQuizStateFrame({}, { task_id: 'another-task', quiz_id: 'qz-1', state: 'expired_terminal' });
        assert.equal(pointer.querySelector('.project-question-status').textContent, 'You answered');
        const next = fx.decision.buildQuestionPointer({ ...row, quiz_id: 'next' });
        assert.equal(next.querySelector('.project-question-status').textContent, 'Unanswered · an answer is still accepted');
        fx.decision.buildQuestionPointer({ ...row, quiz_id: 'next', wait_for_answer: true });
        assert.equal(next.querySelector('.project-question-status').textContent, 'Waiting for your answer');
        fx.decision.buildQuestionPointer({ ...row, quiz_id: 'next', wait_for_answer: true, owner_wait_state: 'resumed' });
        assert.equal(next.querySelector('.project-question-status').textContent, 'Unanswered · the task continued; an answer is still accepted');
        fx.decision.buildQuestionPointer({ ...row, quiz_id: 'next',
            owner_wait_state: 'resumed', owner_wait_resume_reason: 'timeout' });
        assert.equal(next.querySelector('.project-question-status').textContent, 'Unanswered · the task continued; an answer is still accepted');
        // An unavailable read never erases known evidence.
        fx.decision.buildQuestionPointer({ ...row, quiz_id: 'next', quiz_state: 'unknown', source_status: 'unavailable' });
        assert.equal(next.querySelector('.project-question-status').textContent, 'Unanswered · the task continued; an answer is still accepted');
    } finally { fx.restore(); }
});

test('targeted detail preserves normalized option details, single-flight, and the existing answer form', async () => {
    let calls = 0;
    const block = { ...WS_MSG, options: ['Yes', 'No'], option_details: ['Immediate release', 'Wait for CI'], asked_at: WS_MSG.ts };
    const fx = fixture({ fetchDetail: async () => {
        calls += 1;
        return { task_id: 't-1', project_id: 'p1', owner_quiz: { 'qz-1': block } };
    } });
    try {
        const [question, same] = await Promise.all([
            fx.decision.readQuestion('t-1', 'qz-1', 'p1'), fx.decision.readQuestion('t-1', 'qz-1', 'p1'),
        ]);
        assert.equal(calls, 1);
        assert.deepEqual(question, same);
        const card = fx.decision.buildQuizCard(question);
        assert.deepEqual(card.querySelectorAll('.chat-quiz-option-detail').map((node) => node.textContent),
            ['Immediate release', 'Wait for CI']);
        const field = card.querySelector('.chat-quiz-comment');
        field.value = 'keep my draft';
        assert.equal(fx.decision.buildQuizCard({ ...question, ts: 'later-history' }), null);
        assert.equal(card.querySelector('.chat-quiz-comment'), field);
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setImmediate(resolve));
        assert.equal(JSON.parse(fx.calls[0].init.body).option_index, 1);
        assert.equal(JSON.parse(fx.calls[0].init.body).comment, 'keep my draft');
        assert.equal(card.dataset.state, 'answered');
        assert.equal(await fx.decision.readQuestion('t-1', 'qz-1', 'wrong-project'), null);
    } finally { fx.restore(); }
});

test('a finished task keeps its pointer answerable and legacy labels disclose missing details', async () => {
    const fx = fixture({ fetchDetail: async () => ({ task_id: 't-1', project_id: 'p1', owner_quiz: {
        'qz-1': { ...WS_MSG, state: 'expired_terminal', options: ['Yes', 'No'] },
    } }) });
    try {
        const pointer = fx.decision.buildQuestionPointer({ task_id: 't-1', quiz_id: 'qz-1', project_id: 'p1',
            project_chat_id: 23, project_name: 'Storage', quiz_state: 'expired_terminal', question: 'Merge now?' });
        assert.equal(pointer.querySelector('.project-question-status').textContent, 'Unanswered · the task finished; a late answer is accepted as your message');
        assert.equal(pointer.querySelector('.system-message-action').textContent, 'Answer question');
        const question = await fx.decision.readQuestion('t-1', 'qz-1', 'p1');
        const card = fx.decision.buildQuizCard(question);
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((button) => !button.disabled));
        assert.match(card.querySelector('.chat-quiz-details-unavailable').textContent, /not retained/);
    } finally { fx.restore(); }
});

test('a late targeted question read cannot steal a newer navigation or a hidden room', async () => {
    const pending = new Map();
    const fx = fixture({ fetchDetail: (taskId) => new Promise((resolve) => pending.set(taskId, resolve)) });
    const appended = [];
    const append = (msg) => { appended.push(msg.task_id); fx.decision.buildQuizCard(msg); };
    let visible = true;
    const detail = (taskId) => ({ task_id: taskId, project_id: 'p1', owner_quiz: {
        'qz-1': { ...WS_MSG, options: ['Yes', 'No'], option_details: ['', 'Wait for CI'] },
    } });
    try {
        const old = fx.decision.revealQuestion('old', 'qz-1', 'p1', 23, append, () => visible);
        const next = fx.decision.revealQuestion('new', 'qz-1', 'p1', 23, append, () => visible);
        await Promise.resolve();
        pending.get('new')(detail('new'));
        assert.equal(await next, true);
        pending.get('old')(detail('old'));
        assert.equal(await old, false);
        assert.deepEqual(appended, ['new']);
        const hidden = fx.decision.revealQuestion('hidden', 'qz-1', 'p1', 23, append, () => visible);
        await Promise.resolve();
        visible = false;
        pending.get('hidden')(detail('hidden'));
        assert.equal(await hidden, false);
        assert.deepEqual(appended, ['new']);
    } finally { fx.restore(); }
});

test('required question renders waiting identically from live and stored frames', () => {
    const fx = fixture();
    try {
        const live = { ...WS_MSG, quiz_id: 'required-live', wait_for_answer: true, assumption: '' };
        const card = fx.decision.buildQuizCard(live);
        assert.match(card.querySelector('.chat-quiz-assumption').textContent, /Waiting for your answer/);
        const stored = { task_id: 't-1', text: live.question, quiz: { ...live, quiz_id: 'required-replay' } };
        const replay = fx.decision.buildQuizCard(stored);
        assert.equal(replay.querySelector('.chat-quiz-assumption').textContent,
                     card.querySelector('.chat-quiz-assumption').textContent);
    } finally { fx.restore(); }
});

test('required waiting copy ends when the answer settles, including history replay', async () => {
    const fx = fixture();
    try {
        const required = { ...WS_MSG, wait_for_answer: true, assumption: '' };
        const card = fx.decision.buildQuizCard(required);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'answered');
        assert.equal(card.querySelector('.chat-quiz-assumption'), null);
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => btn.disabled));
        for (const state of ['answered', 'expired_terminal', 'superseded']) {
            const replay = fx.decision.buildQuizCard({
                task_id: 't-1', text: required.question,
                quiz: { ...required, state, wait_for_answer: false, wait_ended_at: "earlier", quiz_id: `closed-${state}` },
            });
            assert.equal(replay.querySelector('.chat-quiz-assumption'), null);
        }
    } finally { fx.restore(); }
});

test('quiz card renders full anatomy from a WS frame', () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        assert.ok(card);
        assert.equal(card.dataset.state, 'open');
        assert.equal(card.querySelector('.chat-quiz-question').textContent, 'Merge now?');
        assert.match(card.querySelector('.chat-quiz-stake').textContent, /At stake: release timing/);
        assert.match(card.querySelector('.chat-quiz-assumption').textContent, /Continuing meanwhile: continuing with the merge/);
        assert.equal(card.querySelector('.chat-quiz-status-text').textContent, 'Unanswered · an answer is still accepted');
        const buttons = card.querySelectorAll('.chat-quiz-option');
        assert.equal(buttons.length, 2);
        assert.equal(buttons[1].querySelector('.chat-quiz-option-detail').textContent, 'wait for CI');
        assert.ok(buttons.every((btn) => !btn.disabled));
    } finally { fx.restore(); }
});

test('a finished task leaves its card answerable; only a settled one is a record', async () => {
    const fx = fixture({ fetchImpl: async () => ({
        ok: true, status: 200,
        json: async () => ({ ok: true, state: 'answered', answered_index: 1,
            answered_after_terminal: true, forwarded: true }),
    }) });
    try {
        const replayMsg = {
            msg_type: 'quiz', role: 'assistant', task_id: 't-1',
            text: 'Merge now?', ts: 'x',
            quiz: {
                quiz_id: 'qz-2', state: 'expired_terminal',
                options: [{ label: 'Yes' }, { label: 'No' }],
                stake: '', assumption: 'merging meanwhile',
            },
        };
        const card = fx.decision.buildQuizCard(replayMsg);
        assert.ok(card);
        assert.equal(card.dataset.state, 'expired_terminal');
        assert.match(card.querySelector('.chat-quiz-status-text').textContent, /a late answer is accepted/);
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => !btn.disabled));
        const field = card.querySelector('.chat-quiz-comment');
        assert.ok(field);
        // The assumption line survives settlement: it is the record of the path taken.
        assert.match(card.querySelector('.chat-quiz-assumption').textContent, /merging meanwhile/);

        field.value = 'late but decided';
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(JSON.parse(fx.calls[0].init.body).option_index, 1);
        assert.equal(JSON.parse(fx.calls[0].init.body).comment, 'late but decided');
        // Answered IS settled: the draft field goes and the buttons close.
        assert.equal(card.dataset.state, 'answered');
        assert.equal(card.querySelector('.chat-quiz-comment'), null);
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => btn.disabled));

        const superseded = fx.decision.buildQuizCard({
            ...replayMsg, quiz: { ...replayMsg.quiz, quiz_id: 'qz-3', state: 'superseded' },
        });
        assert.ok(superseded.querySelectorAll('.chat-quiz-option').every((btn) => btn.disabled));
        assert.equal(superseded.querySelector('.chat-quiz-comment'), null);
    } finally { fx.restore(); }
});

test('a required card that outlived its task stops claiming the task is waiting', () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard({
            ...WS_MSG, quiz_id: 'qz-wait-expired', wait_for_answer: true,
            assumption: '', state: 'expired_terminal',
        });
        assert.equal(card.querySelector('.chat-quiz-wait'), null);
        assert.ok(card.querySelector('.chat-quiz-comment'));
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => !btn.disabled));
    } finally { fx.restore(); }
});

test('an accepted answer marks the chosen option; degenerate cards refuse to render', async () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 1);
        assert.equal(fx.calls[0].url, '/api/decisions');
        const body = JSON.parse(fx.calls[0].init.body);
        assert.equal(body.decision_id, 'quiz:t-1:qz-1');
        assert.equal(body.option_index, 1);
        assert.ok(body.request_id);
        assert.equal(card.dataset.state, 'answered');
        const buttons = card.querySelectorAll('.chat-quiz-option');
        assert.ok(buttons[1].classList.contains('chosen'));
        assert.ok(buttons.every((btn) => btn.disabled));

        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, options: [{ label: 'only' }] }), null);
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: '' }), null);
        // An anonymous quiz has no answer address: refuse to render buttons.
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, task_id: '' }), null);
    } finally { fx.restore(); }
});

test('one corrupt option refuses that card only, preserving index integrity', () => {
    const fx = fixture();
    try {
        // Filtering would shift option_index against the producer's original
        // list and submit a silently WRONG answer once the ingress exists.
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, options: [null, null] }), null);
        assert.equal(fx.decision.buildQuizCard({
            ...WS_MSG, options: [null, 'Plain', { label: 'Real' }, { detail: 'no label' }],
        }), null);
        // String options remain a legal producer shorthand.
        const card = fx.decision.buildQuizCard({ ...WS_MSG, options: ['Plain', { label: 'Real' }] });
        assert.ok(card);
        assert.equal(card.querySelectorAll('.chat-quiz-option').length, 2);
    } finally { fx.restore(); }
});

test('question and stake go through the injected markdown pipeline', () => {
    const fx = fixture({ renderMarkdown: (text) => `<md>${text}</md>` });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        assert.equal(card.querySelector('.chat-quiz-question').innerHTML, '<md>Merge now?</md>');
        assert.equal(card.querySelector('.chat-quiz-stake').innerHTML, '<md>At stake: release timing</md>');
    } finally { fx.restore(); }
});

test('a second click while the first answer is in flight is ignored', async () => {
    let resolveFetch;
    const fx = fixture({ fetchImpl: () => new Promise((resolve) => { resolveFetch = resolve; }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const buttons = card.querySelectorAll('.chat-quiz-option');
        buttons[0].click();
        buttons[1].click();
        resolveFetch({ ok: true, status: 200, json: async () => ({ ok: true, state: 'answered', answered_index: 0 }) });
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 1);
        assert.equal(card.dataset.state, 'answered');
    } finally { fx.restore(); }
});

test('a non-409 failure keeps the card open with an honest toast', async () => {
    const fx = fixture({ fetchImpl: async () => ({ ok: false, status: 404 }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'open');
        assert.match(fx.toasts[0].text, /Could not record the answer \(404\)/);
        // The pending latch is released: a later click retries.
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 2);
    } finally { fx.restore(); }
});

test('a 409 settles the card by the BODY state — a lost race reads answered, not expired', async () => {
    // The refusal body carries the true lifecycle state: the loser of a
    // first-wins race must see the winning answer, never a false expiry.
    const fx = fixture({ fetchImpl: async () => ({
        ok: false, status: 409,
        json: async () => ({ ok: false, error: 'quiz_closed', state: 'answered', answered_index: 1 }),
    }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'answered');
        assert.ok(card.querySelectorAll('.chat-quiz-option')[1].classList.contains('chosen'));
        assert.match(fx.toasts[0].text, /Already answered/);
    } finally { fx.restore(); }
});

test('a bodyless 409 no longer invents an expiry the card would obey', async () => {
    // An expired card is still answerable, so a refusal without a state is not
    // evidence of anything: report the failed attempt and keep the card as it is.
    const fx = fixture({ fetchImpl: async () => ({ ok: false, status: 409 }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'open');
        assert.match(fx.toasts[0].text, /Could not record the answer \(409\)/);
        // The pending latch is released: the owner can try again.
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 2);
    } finally { fx.restore(); }
});

test('a retry reuses the SAME request_id (stable idempotency key)', async () => {
    const seen = [];
    let failFirst = true;
    const fx = fixture({ fetchImpl: async (url, init) => {
        seen.push(JSON.parse(init.body).request_id);
        if (failFirst) { failFirst = false; return { ok: false, status: 503, json: async () => ({}) }; }
        return { ok: true, status: 200, json: async () => ({ ok: true, state: 'answered', answered_index: 0 }) };
    } });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const btn = card.querySelectorAll('.chat-quiz-option')[0];
        btn.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'open'); // 503 leaves the card open for retry
        btn.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'answered');
        assert.equal(seen.length, 2);
        assert.equal(seen[0], seen[1]);
    } finally { fx.restore(); }
});

test('applyQuizStateFrame settles an existing card and ignores unknown ids', async () => {
    const fx = fixture({ fetchImpl: async () => ({ ok: true, status: 200 }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const quizCard = card;
        const root = {};
        // The live lifecycle frame (WS quiz_state) settles the card in place.
        const applied = fx.decision.applyQuizStateFrame(root, {
            quiz_id: 'qz-1', task_id: 't-1', state: 'answered', answered_index: 0,
        });
        assert.equal(applied, true);
        assert.equal(quizCard.dataset.state, 'answered');
        const buttons = quizCard.querySelectorAll('chat-quiz-option');
        assert.ok(buttons.length >= 2);
        assert.ok(buttons.every((btn) => btn.disabled));
        assert.ok(buttons[0].classList.contains('chosen'));
        const stateWrites = countPropertyWrites(quizCard.dataset, 'state');
        const disabledWrites = buttons.map((btn) => countPropertyWrites(btn, 'disabled'));
        let chosenWrites = 0;
        for (const btn of buttons) {
            const toggle = btn.classList.toggle.bind(btn.classList);
            btn.classList.toggle = (...args) => { chosenWrites += 1; return toggle(...args); };
        }
        assert.equal(fx.decision.applyQuizStateFrame(root, {
            quiz_id: 'qz-1', task_id: 't-1', state: 'answered', answered_index: 0,
        }), false, 'an identical acknowledgement performs no DOM mutation');
        assert.equal(stateWrites(), 0);
        assert.ok(disabledWrites.every((writes) => writes() === 0));
        assert.equal(chosenWrites, 0);

        // Unknown id: no card found, nothing thrown, honest false.
        assert.equal(fx.decision.applyQuizStateFrame(root, { quiz_id: 'other', state: 'answered' }), false);
    } finally {
        fx.restore();
    }
});

test('a live quiz_state frame carries the owner comment onto the card like replay does (#471)', () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const quizCard = card;
        const root = { querySelector: (sel) => (sel.includes('qz-1') ? quizCard : null) };
        // The owner rejected every option and answered in their own words: the
        // frame carries no answered_index and the recorded comment.
        assert.equal(fx.decision.applyQuizStateFrame(root, {
            quiz_id: 'qz-1', task_id: 't-1', state: 'answered', comment: 'neither — use duckdb',
        }), true);
        assert.equal(quizCard.dataset.state, 'answered');
        assert.equal(quizCard.dataset.ownerComment, 'neither — use duckdb');
        assert.equal(quizCard.querySelector('.chat-quiz-answer').textContent,
            "Owner's answer: neither — use duckdb");
        assert.ok(quizCard.querySelectorAll('.chat-quiz-option').every((btn) => !btn.classList.contains('chosen')));
        // A later lifecycle frame without a comment never wipes the recorded one.
        fx.decision.applyQuizStateFrame(root, { quiz_id: 'qz-1', task_id: 't-1', state: 'superseded' });
        assert.equal(quizCard.dataset.ownerComment, 'neither — use duckdb');
        assert.equal(quizCard.dataset.state, 'superseded');
    } finally {
        fx.restore();
    }
});

// ---- free answer (the owner's own option) ----

function commentParts(card) {
    return {
        field: card.querySelector('.chat-quiz-comment'),
        send: card.querySelector('.chat-quiz-send'),
    };
}

test('a typed remark rides WITH an option click as one answer', async () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const { field, send } = commentParts(card);
        assert.ok(field && send);
        field.value = 'yes, but only after CI';
        field.listeners.get('input')();
        assert.equal(send.disabled, false);
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        const body = JSON.parse(fx.calls[0].init.body);
        assert.equal(body.option_index, 0);
        assert.equal(body.comment, 'yes, but only after CI');
        // Settled: the draft field is gone and the words are the record.
        assert.equal(card.querySelector('.chat-quiz-comment-box'), null);
        assert.equal(card.querySelector('.chat-quiz-answer').textContent,
            "Owner's answer: yes, but only after CI");
    } finally { fx.restore(); }
});

test('the send button answers WITHOUT an option_index', async () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const { field, send } = commentParts(card);
        field.value = '  neither — do C  ';
        field.listeners.get('input')();
        send.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 1);
        const body = JSON.parse(fx.calls[0].init.body);
        assert.equal('option_index' in body, false, 'no option was taken — the key must be absent');
        // VERBATIM: the owner's exact characters, edges included.
        assert.equal(body.comment, '  neither — do C  ');
        assert.equal(card.dataset.state, 'answered');
        // No option is highlighted: the owner chose none of them.
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => !btn.classList.contains('chosen')));
    } finally { fx.restore(); }
});

test('an empty field leaves the send button disabled and sends nothing', async () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const { field, send } = commentParts(card);
        assert.equal(send.disabled, true);
        field.value = '   ';
        field.listeners.get('input')();
        assert.equal(send.disabled, true);
        send.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 0);
        assert.equal(card.dataset.state, 'open');
    } finally { fx.restore(); }
});

test('an over-long answer is refused client-side, not truncated', async () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const { field, send } = commentParts(card);
        field.value = 'x'.repeat(2001);
        field.listeners.get('input')();
        assert.equal(send.disabled, true, 'the cap is the ingress limit, mirrored');
        send.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 0);
        assert.match(fx.toasts[0].text, /under 2000 characters/);
        assert.equal(card.dataset.state, 'open');
    } finally { fx.restore(); }
});

test('a settled replayed card shows the owner answer and offers no field', () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard({
            msg_type: 'quiz', role: 'assistant', task_id: 't-1', text: 'Merge now?', ts: 'x',
            quiz: {
                quiz_id: 'qz-3', state: 'answered', stake: '', assumption: 'merging meanwhile',
                options: [{ label: 'Yes' }, { label: 'No' }],
                comment: 'neither — revert first',
            },
        });
        assert.equal(card.querySelector('.chat-quiz-comment-box'), null);
        assert.equal(card.querySelector('.chat-quiz-answer').textContent,
            "Owner's answer: neither — revert first");
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => btn.disabled));
        assert.ok(card.querySelectorAll('.chat-quiz-option').every((btn) => !btn.classList.contains('chosen')));
        const replay = { ...WS_MSG, quiz_id: 'qz-3', state: 'answered' };
        fx.decision.buildQuizCard(replay);
        assert.match(card.querySelector('.chat-quiz-answer').textContent, /neither — revert first/);
        fx.decision.buildQuizCard({ ...replay, answered_index: 0, comment: '' });
        assert.equal(card.querySelector('.chat-quiz-answer'), null);
        assert.ok(card.querySelectorAll('.chat-quiz-option')[0].classList.contains('chosen'));
    } finally { fx.restore(); }
});

test('a live state frame for an open card never wipes the typed draft', () => {
    const fx = fixture();
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const { field } = commentParts(card);
        field.value = 'half-written thought';
        field.listeners.get('input')();
        const root = { querySelector: () => card };
        assert.equal(fx.decision.applyQuizStateFrame(root, {
            quiz_id: 'qz-1', task_id: 't-1', state: 'open',
        }), false, 'an open→open frame is a no-op');
        assert.equal(card.querySelector('.chat-quiz-comment').value, 'half-written thought');
        assert.ok(card.querySelector('.chat-quiz-comment-box'));
    } finally { fx.restore(); }
});

// ---- routing picker (#198) ----

function routingBubble(cmid = 'cm-1') {
    const bubble = new NodeStub('div');
    bubble.dataset.clientMessageId = cmid;
    return bubble;
}

const ROUTING_ANNOTATION = {
    status: 'needs_manual_target', routing_token: 'tok-1',
    options: [
        { action: 'steer_task', task_id: 't1', title: 'Fix CI' },
        { action: 'new_task_in_project', project_id: 'p1', project_name: 'Web' },
    ],
};

test('an actionable refusal renders the picker card; other statuses fall back to text', () => {
    const fx = fixture();
    try {
        const bubble = routingBubble();
        assert.equal(fx.decision.renderRoutingDecision(bubble, ROUTING_ANNOTATION), true);
        assert.equal(fx.decision.renderRoutingDecision(bubble, ROUTING_ANNOTATION), false);
        const card = bubble.querySelector('.chat-routing-card');
        assert.ok(card);
        assert.equal(card.dataset.state, 'open');
        const buttons = card.querySelectorAll('.chat-quiz-option');
        assert.equal(buttons.length, 2);
        assert.equal(buttons[0].querySelector('.chat-quiz-option-label').textContent, 'Fix CI');
        assert.equal(buttons[1].querySelector('.chat-quiz-option-label').textContent, 'New task in Web');
        // A later settled annotation (the dispatch ack) REPLACES the card
        // with the plain text line — the card never lingers past its attempt.
        const settled = {
            status: 'delivered', action: 'steer_task', target: 't1', target_label: 'Fix CI',
        };
        assert.equal(fx.decision.renderRoutingDecision(bubble, settled), true);
        const note = bubble.querySelector('.msg-routing-annotation');
        const textWrites = countPropertyWrites(note, 'textContent');
        const noteStatusWrites = countPropertyWrites(note.dataset, 'annotationStatus');
        const bubbleStatusWrites = countPropertyWrites(bubble.dataset, 'chatAnnotationStatus');
        assert.equal(fx.decision.renderRoutingDecision(bubble, settled), false);
        assert.deepEqual(
            [textWrites(), noteStatusWrites(), bubbleStatusWrites()], [0, 0, 0],
        );
        assert.equal(bubble.querySelector('.chat-routing-card'), null);
        assert.match(bubble.querySelector('.msg-routing-annotation').textContent, /Steered task/);
        assert.equal(fx.decision.renderRoutingDecision(bubble, null), true);
        assert.equal(bubble.querySelector('.msg-routing-annotation'), null);
        assert.equal(bubble.dataset.chatAnnotationStatus, undefined);
        assert.equal(fx.decision.renderRoutingDecision(bubble, null), false);
    } finally { fx.restore(); }
});

test('a refusal with a token but no options renders the host cause as the plain line, never a card', () => {
    // The incident shape (client_message_id + routing_token, NO options): the
    // host sends `cause`, the owner reads a sentence, and nothing is clickable.
    const cause = 'Not started: the working folder can\'t be used';
    const fx = fixture();
    try {
        const bubble = routingBubble('cm-refused');
        const refused = {
            action: 'promote_chat_to_task', status: 'needs_manual_target', routing_token: 'tok-r',
            target: 'never-started', target_label: 'Аудит', cause,
        };
        assert.equal(fx.decision.renderRoutingDecision(bubble, refused), true);
        assert.equal(bubble.querySelector('.chat-routing-card'), null);
        const note = bubble.querySelector('.msg-routing-annotation');
        assert.equal(note.textContent, cause);
        assert.equal(note.dataset.annotationStatus, 'needs_manual_target');
        assert.equal(bubble.dataset.chatAnnotationStatus, 'needs_manual_target');
        assert.equal(fx.decision.renderRoutingDecision(bubble, refused), false);
    } finally { fx.restore(); }
});

test('a routing 409 that reopens the card names the host cause before the raw reason', async () => {
    const cause = 'Not started: the working folder can\'t be used';
    const fx = fixture({
        fetchImpl: async () => ({
            ok: false, status: 409,
            json: async () => ({ state: 'open', reason: 'workspace_unusable', cause }),
        }),
    });
    try {
        const bubble = routingBubble('cm-5');
        fx.decision.renderRoutingDecision(bubble, ROUTING_ANNOTATION);
        const card = bubble.querySelector('.chat-routing-card');
        card.querySelectorAll('.chat-quiz-option')[0].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'open');
        assert.equal(fx.toasts.length, 1);
        assert.equal(fx.toasts[0].text, `Not routed: ${cause} — pick again.`);
        assert.equal(fx.toasts[0].text.includes('workspace_unusable'), false);
    } finally { fx.restore(); }
});

test('a routing click posts the routing decision id with a STABLE request id', async () => {
    const fx = fixture();
    try {
        const bubble = routingBubble('cm-2');
        fx.decision.renderRoutingDecision(bubble, ROUTING_ANNOTATION);
        const card = bubble.querySelector('.chat-routing-card');
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(fx.calls.length, 1);
        const body = JSON.parse(fx.calls[0].init.body);
        assert.equal(body.decision_id, 'routing:cm-2:tok-1');
        assert.equal(body.option_index, 1);
        assert.ok(body.request_id);
        assert.equal(card.dataset.state, 'answered');
        assert.ok(card.querySelectorAll('.chat-quiz-option')[1].classList.contains('chosen'));
    } finally { fx.restore(); }
});

test('a routing 409 settles the card from the body state, never a false expiry', async () => {
    const fx = fixture({
        fetchImpl: async () => ({
            ok: false, status: 409,
            json: async () => ({ state: 'answered', answered_index: 0 }),
        }),
    });
    try {
        const bubble = routingBubble('cm-3');
        fx.decision.renderRoutingDecision(bubble, ROUTING_ANNOTATION);
        const card = bubble.querySelector('.chat-routing-card');
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'answered');
        assert.ok(card.querySelectorAll('.chat-quiz-option')[0].classList.contains('chosen'));
        assert.equal(fx.toasts.length, 1);
    } finally { fx.restore(); }
});

test('more than eight options hide behind a show-all control', () => {
    let writes = 0;
    const fx = fixture({ onDomWrite: (mutate) => { writes += 1; return mutate(); } });
    try {
        const bubble = routingBubble('cm-4');
        const wide = {
            ...ROUTING_ANNOTATION,
            options: Array.from({ length: 11 }, (_, i) => (
                { action: 'steer_task', task_id: `t${i}`, title: `Task ${i}` })),
        };
        fx.decision.renderRoutingDecision(bubble, wide);
        const card = bubble.querySelector('.chat-routing-card');
        const buttons = card.querySelectorAll('.chat-quiz-option');
        assert.equal(buttons.length, 11);
        assert.equal(buttons.filter((btn) => btn.hidden).length, 3);
        const more = card.querySelector('.chat-quiz-more');
        assert.match(more.textContent, /11/);
        const beforeClick = writes;
        more.click();
        assert.equal(writes, beforeClick + 1);
        assert.equal(buttons.filter((btn) => btn.hidden).length, 0);
        assert.equal(card.querySelector('.chat-quiz-more'), null);
    } finally { fx.restore(); }
});

test('quiz success and duplicate retry use recorded confirmation, never the attempted answer', async () => {
    for (const body of [null, 'malformed', {}, { ok: true, state: 'answered' },
        { ok: true, state: 'answered', answered_index: 99 },
        { ok: true, state: 'answered', answered_index: 0 },
        { ok: true, state: 'answered', duplicate: true, comment: 'my own plan' }]) {
        const fx = fixture({ fetchImpl: async () => ({ ok: true, status: 200,
            json: async () => { if (body === 'malformed') throw new Error('bad JSON'); return body; } }) });
        try {
            const card = fx.decision.buildQuizCard(WS_MSG);
            card.querySelector('.chat-quiz-comment').value = 'unconfirmed retry draft';
            card.querySelectorAll('.chat-quiz-option')[1].click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            const valid = body?.answered_index === 0 || body?.duplicate === true;
            assert.deepEqual([card.dataset.state, card.dataset.ownerComment, fx.toasts.length],
                [valid ? 'answered' : 'open', body?.comment, valid ? 0 : 1]);
            assert.deepEqual(card.querySelectorAll('.chat-quiz-option').map(b => b.classList.contains('chosen')),
                [body?.answered_index === 0, false]);
        } finally { fx.restore(); }
    }
});

test('a 409 loser adopts the winning comment over its local draft', async () => {
    const fx = fixture({ fetchImpl: async () => ({
        ok: false, status: 409,
        json: async () => ({ ok: false, error: 'quiz_closed', state: 'answered', answered_index: 0, comment: 'winning note' }),
    }) });
    try {
        const card = fx.decision.buildQuizCard(WS_MSG);
        const area = card.querySelector('.chat-quiz-comment');
        area.value = 'losing draft';
        card.querySelectorAll('.chat-quiz-option')[1].click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.equal(card.dataset.state, 'answered');
        assert.equal(card.dataset.ownerComment, 'winning note');
    } finally { fx.restore(); }
});

test('the recommended option carries a badge on the live card and on the targeted-detail replay', async () => {
    const fx = fixture({ fetchDetail: async () => ({ task_id: 't-1', project_id: 'p1', owner_quiz: {
        'qz-2': { ...WS_MSG, quiz_id: 'qz-2', options: ['Yes', 'No'], option_details: ['', 'wait for CI'],
            recommended_index: 1, asked_at: WS_MSG.ts },
    } }) });
    const badges = (card) => card.querySelectorAll('.chat-quiz-option')
        .map((button) => button.querySelectorAll('.chat-quiz-option-recommended').length);
    try {
        const live = fx.decision.buildQuizCard({ ...WS_MSG, options: [{ label: 'Yes', recommended: true }, { label: 'No' }] });
        assert.deepEqual(badges(live), [1, 0]);
        assert.equal(live.querySelector('.chat-quiz-option-recommended').textContent, 'recommended');
        // A history row that lost the option details carries no badge; the targeted detail
        // (durable recommended_index) adds it in place, exactly like the option details.
        const stale = fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-2', options: ['Yes', 'No'] });
        assert.deepEqual(badges(stale), [0, 0]);
        const question = await fx.decision.readQuestion('t-1', 'qz-2', 'p1');
        assert.equal(fx.decision.buildQuizCard(question), null);
        assert.deepEqual(badges(stale), [0, 1]);
        assert.equal(fx.decision.buildQuizCard(question), null);
        assert.deepEqual(badges(stale), [0, 1], 'the badge is added once');
        // A fresh card built straight from the projection carries the same badge; a plain card none.
        assert.deepEqual(badges(fx.decision.buildQuizCard({ ...question, quiz_id: 'qz-5' })), [0, 1]);
        assert.deepEqual(badges(fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-3' })), [0, 0]);
    } finally { fx.restore(); }
});

test('a bounded wait that closed stops saying waiting while the card stays answerable', () => {
    const fx = fixture({ fetchImpl: async () => ({ ok: true, status: 200 }) });
    try {
        const card = fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-w', wait_for_answer: true });
        const quizCard = card;
        assert.match(quizCard.querySelector('.chat-quiz-wait').textContent, /Waiting for your answer/);
        const root = { querySelector: (sel) => (sel.includes('qz-w') ? quizCard : null) };
        const applied = fx.decision.applyQuizStateFrame(root, { quiz_id: 'qz-w', task_id: 't-1', state: 'open', wait_for_answer: false });
        assert.equal(applied, true);
        assert.equal(quizCard.querySelector('.chat-quiz-wait'), null);
        assert.match(quizCard.querySelector('.chat-quiz-assumption').textContent, /wait ended/);
        assert.equal(quizCard.dataset.state, 'open');
        // History replay renders the ended wait from the projection (wait_ended_at, no wait_for_answer).
        const replayed = fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-r', wait_ended_at: '2026-08-31T10:05:00Z' });
        const rcard = replayed;
        assert.match(rcard.querySelector('.chat-quiz-assumption').textContent, /wait ended/);
        assert.equal(rcard.querySelector('.chat-quiz-wait'), null);
    } finally { fx.restore(); }
});

test('reconciling a replayed row into an existing card projects the closed bound', () => {
    const fx = fixture({ fetchImpl: async () => ({ ok: true, status: 200 }) });
    try {
        const card = fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-m', wait_for_answer: true });
        const quizCard = card;
        assert.match(quizCard.querySelector('.chat-quiz-wait').textContent, /Waiting for your answer/);
        // The owner was disconnected during the timeout: no frame arrived, history replays the row
        // with the closed bound and reconciles it into the SAME card.
        assert.equal(fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-m', wait_ended_at: '2026-08-31T10:05:00Z' }), null);
        assert.equal(quizCard.querySelector('.chat-quiz-wait'), null);
        assert.match(quizCard.querySelector('.chat-quiz-assumption').textContent, /wait ended/);
    } finally { fx.restore(); }
});

const POINTER = { task_id: 't-1', quiz_id: 'qz-1', project_id: 'p1', project_chat_id: 23,
    project_name: 'Storage', quiz_state: 'answered', question: 'Merge now?', options: ['Yes', 'No'] };
const turn = () => new Promise((resolve) => setImmediate(resolve));

test('the pointer paints from its row alone: question, option zero and comment, no detail read', async () => {
    let reads = 0;
    const fx = fixture({ fetchDetail: async () => { reads += 1; return null; } });
    try {
        const pointer = fx.decision.buildQuestionPointer({ ...POINTER, answered_index: 0, comment: 'After the release.' });
        await turn();
        assert.equal(reads, 0, 'a complete row needs no detail read');
        assert.equal(pointer.querySelector('.project-question-status').textContent, 'You answered');
        assert.equal(pointer.querySelector('.project-question-preview').textContent, 'Merge now?');
        assert.equal(pointer.querySelector('.project-question-answer').textContent, 'Your answer: Yes — After the release.');
        assert.equal(pointer.querySelector('.project-question-source').textContent, 'In Storage');
        assert.equal(pointer.querySelector('.system-message-action').parentNode.className, 'system-message-actions');
        assert.equal(pointer.querySelectorAll('.system-message-action').length, 1);
        // A comment-only answer replayed later updates the same pointer in place.
        assert.equal(fx.decision.buildQuestionPointer({ ...POINTER, comment: 'Neither — use the archive.' }), null);
        assert.equal(pointer.querySelector('.project-question-answer').textContent, 'Your answer: Yes — Neither — use the archive.');
        // A narrower re-delivery (the 3-second activity census) never blanks the painted question or option label.
        const { question: _q, options: _o, ...narrow } = POINTER;
        assert.equal(fx.decision.buildQuestionPointer({ ...narrow, question: '', options: [], answered_index: 0 }), null);
        assert.equal(pointer.querySelector('.project-question-preview').textContent, 'Merge now?');
        assert.equal(pointer.querySelector('.project-question-answer').textContent, 'Your answer: Yes — Neither — use the archive.');
        fx.decision.releaseViews({ contains: (node) => node === pointer });
        const restored = fx.decision.buildQuestionPointer({ ...POINTER, answered_index: 1 });
        assert.equal(restored.querySelector('.project-question-answer').textContent, 'Your answer: No — Neither — use the archive.');
    } finally { fx.restore(); }
});

test('a live frame that closed the wait outranks an older history row and an unavailable row', () => {
    const fx = fixture();
    try {
        const status = (node) => node.querySelector('.project-question-status').textContent;
        const pointer = fx.decision.buildQuestionPointer({ ...POINTER, quiz_state: 'open', wait_for_answer: true, owner_wait_state: 'waiting' });
        assert.equal(status(pointer), 'Waiting for your answer');
        // The production timeout frame carries only wait_for_answer:false.
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'qz-1', state: 'open', wait_for_answer: false });
        assert.equal(status(pointer), 'Unanswered · the task continued; an answer is still accepted');
        fx.decision.buildQuestionPointer({ ...POINTER, quiz_state: 'open', wait_for_answer: true, owner_wait_state: 'waiting', ts: 'older' });
        assert.equal(status(pointer), 'Unanswered · the task continued; an answer is still accepted', 'an older history row cannot reopen a wait a live frame closed');
        fx.decision.buildQuestionPointer({ ...POINTER, quiz_state: 'unknown', source_status: 'unavailable' });
        assert.equal(status(pointer), 'Unanswered · the task continued; an answer is still accepted', 'an unavailable read keeps the known evidence');
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'qz-1', state: 'answered', answered_index: 1, comment: 'No.' });
        assert.equal(status(pointer), 'You answered');
        assert.equal(pointer.querySelector('.project-question-answer').textContent, 'Your answer: No — No.');
        fx.decision.buildQuestionPointer({ ...POINTER, quiz_state: 'open', wait_for_answer: true });
        assert.equal(status(pointer), 'You answered', 'a settled question never reopens');
    } finally { fx.restore(); }
});

test('a detail read begun before a live answer enriches the source, never the state', async () => {
    let resolve;
    const fx = fixture({ fetchDetail: () => new Promise((done) => { resolve = done; }) });
    try {
        const read = fx.decision.readQuestion('t-1', 'qz-1', 'p1');
        await turn();
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'qz-1', state: 'answered', answered_index: 0, comment: 'Actual answer' });
        resolve({ task_id: 't-1', project_id: 'p1', owner_quiz: { 'qz-1': { ...WS_MSG, wait_for_answer: true } },
            owner_wait: { quiz_id: 'qz-1', state: 'waiting' } });
        const question = await read;
        assert.equal(question.state, 'answered');
        assert.equal(question.comment, 'Actual answer');
        assert.equal(question.question, 'Merge now?');
        const card = fx.decision.buildQuizCard(question);
        assert.equal(card.querySelector('.chat-quiz-status-text').textContent, 'You answered');
        assert.ok(card.querySelectorAll('.chat-quiz-option')[0].classList.contains('chosen'));
        assert.equal(card.querySelector('.chat-quiz-wait'), null);
        fx.decision.destroy();
        assert.equal(await fx.decision.readQuestion('t-1', 'qz-1', 'p1'), null);
    } finally { fx.restore(); }
});

test('the Project card reads the wait facts history attaches: a wait resumed by owner input stops waiting', async () => {
    const fx = fixture({ fetchDetail: async () => ({ task_id: 't-1', project_id: 'p1',
        owner_quiz: { 'qz-d': { ...WS_MSG, quiz_id: 'qz-d', wait_for_answer: true, options: ['Yes', 'No'] } },
        owner_wait: { quiz_id: 'another', state: 'waiting' } }) });
    try {
        const replayed = fx.decision.buildQuizCard({ task_id: 't-1', text: 'Merge now?',
            quiz: { ...WS_MSG, quiz_id: 'qz-o', wait_for_answer: true, owner_wait_state: 'resumed', assumption: '' } });
        assert.equal(replayed.querySelector('.chat-quiz-wait'), null);
        assert.match(replayed.querySelector('.chat-quiz-assumption').textContent, /wait ended without an answer/);
        assert.equal(replayed.querySelector('.chat-quiz-status-text').textContent, 'Unanswered · the task continued; an answer is still accepted');
        assert.ok(replayed.querySelectorAll('.chat-quiz-option').every((btn) => !btn.disabled));
        // A detail read whose wait record moved on to another quiz proves this wait ended too.
        const detail = fx.decision.buildQuizCard(await fx.decision.readQuestion('t-1', 'qz-d', 'p1'));
        assert.equal(detail.querySelector('.chat-quiz-status-text').textContent, 'Unanswered · the task continued; an answer is still accepted');
        // A bounded wait that ended under an assumption names the path the task took.
        const assumed = fx.decision.buildQuizCard({ ...WS_MSG, quiz_id: 'qz-a', wait_for_answer: true, wait_ended_at: 'x' });
        assert.match(assumed.querySelector('.chat-quiz-assumption').textContent, /under its assumption \(continuing with the merge\)/);
    } finally { fx.restore(); }
});

test('a late answer says where it went', async () => {
    for (const forwarded of [true, false]) {
        const fx = fixture({ fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({
            ok: true, state: 'answered', answered_index: 0, answered_after_terminal: true, forwarded }) }) });
        try {
            const card = fx.decision.buildQuizCard({ ...WS_MSG, state: 'expired_terminal' });
            card.querySelectorAll('.chat-quiz-option')[0].click();
            await turn();
            assert.equal(card.dataset.state, 'answered');
            assert.deepEqual(fx.toasts.map((toast) => toast.tone), ['info']);
            assert.match(fx.toasts[0].text, forwarded ? /delivered to its chat as your message/ : /nothing is waiting on it/);
        } finally { fx.restore(); }
    }
});
