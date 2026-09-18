// Project questions in Main: one row per question, and its size follows the owner's attention
// (docs/DESIGN.md "Project question row"). A line in every settled or passed state; a card with
// the option buttons only while the task waits.
import assert from 'node:assert/strict';
import test from 'node:test';
import { questionRow } from '../modules/question_presentation.js';
import { fixture, turn } from './chat_decision_fixture.js';

const ROW = { task_id: 't-1', quiz_id: 'qz-1', project_id: 'p1', project_chat_id: 23,
    project_name: 'Storage', quiz_state: 'answered', question: 'Merge now?', options: ['Yes', 'No'] };
const WAITING = { ...ROW, quiz_state: 'open', wait_for_answer: true, owner_wait_state: 'waiting', recommended_index: 0 };
const text = (node, name) => node.querySelector(`.project-question-${name}`)?.textContent ?? null;
const mode = (node) => node.dataset.questionMode;
const options = (node) => node.querySelectorAll('.chat-quiz-option');

test('a realistic burst: three questions of one task are three lines, and only the waiting one is a card', async () => {
    const fx = fixture();
    try {
        const rows = [
            { ...ROW, quiz_id: 'q1', question: '1/3. Which account publishes?', answered_index: 0 },
            { ...ROW, quiz_id: 'q2', question: '2/3. Separate copy or the live install?', answered_index: 1, comment: 'Live first.' },
            { ...WAITING, quiz_id: 'q3', question: '3/3. Keep the **persistent** store?' },
        ].map((row) => fx.decision.buildQuestionPointer(row));
        assert.deepEqual(rows.map(mode), ['row', 'row', 'card']);
        assert.equal(text(rows[0], 'status-text'), 'You answered:');
        assert.equal(text(rows[0], 'answer'), 'Yes');
        assert.equal(text(rows[0], 'preview'), '1/3. Which account publishes?');
        assert.equal(text(rows[0], 'source'), 'Storage');
        assert.equal(text(rows[1], 'answer'), 'No — Live first.', 'the option and the owner\'s words both ride the line');
        assert.equal(rows[0].getAttribute('role'), 'button');
        assert.equal(options(rows[0]).length, 0, 'a settled line offers no answer buttons');
        // The waiting card: the whole question, the option labels, the recommendation, one way out.
        assert.equal(rows[2].getAttribute('role'), null);
        assert.equal(rows[2].querySelector('.chat-live-project-name').textContent, 'Storage');
        assert.equal(rows[2].querySelector('.chat-live-project-status').textContent, 'Waiting for your answer');
        assert.equal(rows[2].querySelector('.chat-quiz-question').textContent, '3/3. Keep the **persistent** store?');
        assert.deepEqual(options(rows[2]).map((button) => button.querySelector('.chat-quiz-option-label').textContent), ['Yes', 'No']);
        assert.ok(options(rows[2])[0].querySelector('.chat-quiz-option-recommended'), 'index zero is a recommendation too');
        assert.equal(options(rows[2])[1].querySelector('.chat-quiz-option-recommended'), null);
        assert.equal(rows[2].querySelector('.system-message-action').textContent, 'Details and own answer');
        assert.equal(rows[2].querySelector('.chat-quiz-comment'), null, 'own words stay in the Project form');
        // One touch answers it, and the card folds into a line like the others.
        options(rows[2])[1].click();
        await turn();
        const sent = JSON.parse(fx.calls[0].init.body);
        assert.deepEqual([fx.calls.length, fx.calls[0].url, sent.decision_id, sent.option_index, 'comment' in sent],
            [1, '/api/decisions', 'quiz:t-1:q3', 1, false]);
        assert.deepEqual([mode(rows[2]), text(rows[2], 'status-text'), text(rows[2], 'answer')], ['row', 'You answered:', 'No']);
        assert.equal(rows[2].dataset.state, 'answered');
    } finally { fx.restore(); }
});

test('every state reads as one line, and the line names what the owner needs first', () => {
    const fx = fixture();
    try {
        const line = (row) => {
            const node = fx.decision.buildQuestionPointer({ ...ROW, quiz_id: `q-${Math.random()}`, ...row });
            return [mode(node), text(node, 'status-text'), text(node, 'answer'), node.dataset.state];
        };
        assert.deepEqual(line({ quiz_state: 'open', assumption: 'WebP meanwhile', recommended_index: 1 }),
            ['row', 'Unanswered · continuing with:', 'WebP meanwhile', 'open'], 'a passed optional question names the path the task took');
        assert.deepEqual(line({ quiz_state: 'open' }), ['row', 'Unanswered · an answer is still accepted', null, 'open']);
        assert.deepEqual(line({ quiz_state: 'open', wait_for_answer: true, owner_wait_state: 'resumed' }),
            ['row', 'Unanswered · the task continued; an answer is still accepted', null, 'open']);
        assert.deepEqual(line({ quiz_state: 'open', wait_for_answer: true, owner_wait_state: 'resumed', assumption: 'ship it' }),
            ['row', 'Unanswered · continuing with:', 'ship it', 'open'], 'a wait that ended under an assumption says so');
        assert.deepEqual(line({ quiz_state: 'expired_terminal', assumption: 'ship it' }),
            ['row', 'Unanswered · the task finished; a late answer is accepted as your message', null, 'expired_terminal']);
        assert.deepEqual(line({ quiz_state: 'superseded' }), ['row', 'Replaced by a newer question', null, 'superseded']);
        assert.deepEqual(line({ quiz_state: 'unknown', source_status: 'unavailable' }), ['row', 'Status unavailable', null, 'unknown']);
        assert.deepEqual(line({ quiz_state: 'answered', comment: 'Neither — use the archive.' }),
            ['row', 'You answered:', 'Neither — use the archive.', 'answered'], 'a comment-only answer is the whole answer');
        const long = 'x'.repeat(400);
        assert.match(questionRow({ ...ROW, quiz_state: 'answered', comment: long }).detail, /^x{280}… \(preview; open for full text\)$/);
        assert.equal(questionRow({ ...ROW, question: '' }).question, '');
    } finally { fx.restore(); }
});

test('the line opens the exact question; the card opens it from its head and its details action', () => {
    const fx = fixture();
    try {
        const settled = fx.decision.buildQuestionPointer({ ...ROW, answered_index: 0 });
        settled.click();
        const waiting = fx.decision.buildQuestionPointer({ ...WAITING, quiz_id: 'qz-2' });
        waiting.click();
        assert.equal(fx.opened.length, 1, 'the body of a waiting card is not a control');
        waiting.querySelector('.chat-live-project-card-btn').click();
        waiting.querySelector('.system-message-action').click();
        assert.deepEqual(fx.opened.map((detail) => [detail.project.id, detail.project.chat_id, detail.task_id, detail.quiz_id]),
            [['p1', 23, 't-1', 'qz-1'], ['p1', 23, 't-1', 'qz-2'], ['p1', 23, 't-1', 'qz-2']]);
    } finally { fx.restore(); }
});

test('an unchanged row writes nothing, and a narrower re-delivery never blanks a painted line or card', () => {
    let writes = 0;
    const fx = fixture({ onDomWrite: (mutate) => { writes += 1; return mutate(); } });
    try {
        const pointer = fx.decision.buildQuestionPointer({ ...WAITING, assumption: 'Yes meanwhile' });
        const painted = writes;
        assert.equal(fx.decision.buildQuestionPointer({ ...WAITING, assumption: 'Yes meanwhile', ts: 'later' }), null);
        assert.equal(writes, painted, 'the same projection preserves the owner\'s selection and focus');
        // The 3-second activity census re-delivers the waiting question without its display fields.
        const { question: _q, options: _o, recommended_index: _r, project_name: _p, ...narrow } = WAITING;
        fx.decision.buildQuestionPointer({ ...narrow, question: '', options: [], assumption: '', recommended_index: null });
        assert.equal(writes, painted);
        assert.equal(options(pointer).length, 2);
        assert.ok(options(pointer)[0].querySelector('.chat-quiz-option-recommended'));
        assert.equal(pointer.querySelector('.chat-live-project-name').textContent, 'Storage');
        // A card painted from the census alone still badges option zero.
        const cold = fx.decision.buildQuestionPointer({ ...WAITING, quiz_id: 'cold' });
        assert.ok(options(cold)[0].querySelector('.chat-quiz-option-recommended'));
        fx.decision.releaseViews({ contains: (node) => node === pointer });
        const restored = fx.decision.buildQuestionPointer({ ...ROW, answered_index: 1 });
        assert.equal(text(restored, 'answer'), 'No');
    } finally { fx.restore(); }
});

test('lifecycle only moves forward: a closed wait and a settled answer survive stale snapshots', () => {
    const fx = fixture();
    try {
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'early', state: 'answered', answered_index: 0 });
        const early = fx.decision.buildQuestionPointer({ ...ROW, quiz_id: 'early', quiz_state: 'open' });
        assert.deepEqual([mode(early), text(early, 'answer')], ['row', 'Yes'], 'an answer observed before the row wins over its open snapshot');
        const pointer = fx.decision.buildQuestionPointer(WAITING);
        assert.equal(mode(pointer), 'card');
        // The production timeout frame carries only wait_for_answer:false.
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'qz-1', state: 'open', wait_for_answer: false });
        assert.deepEqual([mode(pointer), text(pointer, 'status-text')], ['row', 'Unanswered · the task continued; an answer is still accepted']);
        fx.decision.buildQuestionPointer({ ...WAITING, ts: 'older' });
        assert.equal(mode(pointer), 'row', 'an older history row cannot reopen a wait a live frame closed');
        fx.decision.buildQuestionPointer({ ...ROW, quiz_state: 'unknown', source_status: 'unavailable' });
        assert.equal(text(pointer, 'status-text'), 'Unanswered · the task continued; an answer is still accepted', 'an unavailable read keeps the known evidence');
        fx.decision.applyQuizStateFrame({}, { task_id: 'another-task', quiz_id: 'qz-1', state: 'expired_terminal' });
        assert.equal(pointer.dataset.state, 'open', 'identity is the task AND the quiz');
        fx.decision.applyQuizStateFrame({}, { task_id: 't-1', quiz_id: 'qz-1', state: 'answered', answered_index: 1, comment: 'No.' });
        assert.deepEqual([text(pointer, 'status-text'), text(pointer, 'answer')], ['You answered:', 'No — No.']);
        fx.decision.buildQuestionPointer(WAITING);
        assert.equal(mode(pointer), 'row', 'a settled question never reopens');
    } finally { fx.restore(); }
});

test('an answer from Main settles through the observation: a lost live frame plus a stale open snapshot cannot reopen it', async () => {
    const fx = fixture();
    try {
        const pointer = fx.decision.buildQuestionPointer(WAITING);
        const pressed = options(pointer)[0];
        pressed.focus();
        pressed.click();
        options(pointer)[1].click();
        await turn();
        assert.equal(fx.calls.length, 1, 'a second press while the first is in flight sends nothing');
        assert.deepEqual([mode(pointer), text(pointer, 'answer')], ['row', 'Yes']);
        assert.equal(globalThis.document.activeElement, pointer, 'focus follows from the removed button to the line');
        // No quiz_state frame arrived; history and the census still replay the open, waiting row.
        fx.decision.buildQuestionPointer({ ...WAITING, ts: 'stale' });
        assert.deepEqual([mode(pointer), text(pointer, 'status-text')], ['row', 'You answered:']);
    } finally { fx.restore(); }
});

test('a lost race settles into the winner\'s record, and a failed attempt leaves the card answerable', async () => {
    const replies = [
        { ok: false, status: 500, json: async () => ({}) },
        { ok: false, status: 409, json: async () => ({ state: 'answered', answered_index: 1, comment: 'From the Project form.' }) },
    ];
    const fx = fixture({ fetchImpl: async () => replies.shift() });
    try {
        const pointer = fx.decision.buildQuestionPointer(WAITING);
        options(pointer)[0].click();
        await turn();
        assert.equal(mode(pointer), 'card');
        assert.match(fx.toasts[0].text, /Could not record the answer \(500\)/);
        options(pointer)[0].click();
        await turn();
        const ids = fx.calls.map((call) => JSON.parse(call.init.body).request_id);
        assert.equal(ids[0], ids[1], 'a retry replays the same request');
        assert.deepEqual([mode(pointer), text(pointer, 'answer')], ['row', 'No — From the Project form.']);
        assert.equal(fx.toasts[1].text, 'Already answered.');
        assert.equal(pointer.querySelector('.chat-quiz-answer'), null, 'the Project card\'s record line never lands in a Main row');
    } finally { fx.restore(); }
});

test('a folding card releases what its rendered question owned, and an empty question still reads', async () => {
    const released = [];
    const fx = fixture({ renderMarkdown: (text) => `<p>${text}</p>`,
        enhanceMarkdown: (node) => () => released.push(node) });
    try {
        const pointer = fx.decision.buildQuestionPointer(WAITING);
        const rendered = pointer.querySelector('.chat-quiz-question');
        assert.equal(rendered.innerHTML, '<p>Merge now?</p>');
        options(pointer)[0].click();
        await turn();
        assert.deepEqual([mode(pointer), released], ['row', [rendered]], 'charts and timers of the removed question are released');
        const blank = fx.decision.buildQuestionPointer({ ...WAITING, quiz_id: 'blank', question: '' });
        assert.equal(blank.querySelector('.chat-quiz-question').innerHTML, '<p>Open the original question for its text.</p>');
    } finally { fx.restore(); }
});

test('only the line is a control: a waiting card lets clicks through and keeps focus across a repaint', () => {
    const fx = fixture();
    try {
        let stopped = 0;
        const event = { stopPropagation: () => { stopped += 1; } };
        const waiting = fx.decision.buildQuestionPointer(WAITING);
        waiting.click(event);
        assert.deepEqual([stopped, fx.opened.length], [0, 0], 'document-level handlers still see a click on the card');
        // A renamed Project repaints the card; the option the owner was on keeps the focus.
        options(waiting)[1].focus();
        fx.decision.buildQuestionPointer({ ...WAITING, project_name: 'Storage v2' });
        assert.equal(waiting.querySelector('.chat-live-project-name').textContent, 'Storage v2');
        assert.equal(globalThis.document.activeElement, options(waiting)[1]);
        const settled = fx.decision.buildQuestionPointer({ ...ROW, quiz_id: 'line', answered_index: 0 });
        settled.click(event);
        assert.deepEqual([stopped, fx.opened.length], [1, 1]);
    } finally { fx.restore(); }
});
