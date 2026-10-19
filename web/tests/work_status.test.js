// One work, one status in the browser (work_status.js) over the server's real projection: the cases in
// fixtures/work_projection_cases.json are what gateway/work_projection.works_snapshot builds for them
// (tests/test_work_projection_web_fixture.py keeps the two in step). The client says those facts in
// short words, keeps what an incomplete body omitted, and lays each work's controls on the shared doors.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { applyPayload, CODE_PREFIX, currentPayload, setMissTransport } from '../modules/i18n.js';
import {
    ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_RESUME, ACTION_STOP_EARLIER, ACTION_STOP_NOW,
    TASK_CONTROL_LABELS, requestStopAll,
} from '../modules/task_control_menu.js';
import { desiredLiveCardPhase, pausePhaseLabel, taskActivityPresentation } from '../modules/task_phase_chip.js';
import { markView } from '../modules/status_mark.js';
import {
    OUTCOME_TONES, activityWord, applyCardWorkViews, cardControls, cardMenuActions, cardWorkView, emptyWorkState,
    ownStopPress, reasonBeside, reasonText, reconcileWorkState, resumeTargets, roomStatusView, workControls,
    workMenuActions, workQuestion, workRowView,
} from '../modules/work_status.js';
import { SERVER_REASONS, room, serverMark, work as builtWork } from './work_fixture.js';

setMissTransport(() => Promise.resolve(null));
const CASES = JSON.parse(readFileSync(new URL('./fixtures/work_projection_cases.json', import.meta.url), 'utf8')).cases;
// One /api/state body for a case, as the server sends it (supervisor ready unless said otherwise).
const body = (name, extra = {}) => ({ supervisor_ready: true, ...structuredClone(CASES[name]), ...extra });
const work = (name, index = 0) => structuredClone(CASES[name].works[index]);
const stateOf = (name, chatId = 42, extra = {}) => reconcileWorkState(emptyWorkState(), body(name, extra), chatId);

test('the room says one short word for every activity, transition and exact call', () => {
    const words = {
        held_continue: 'Working', working_call: 'Sending', paused_by_owner: 'Paused', spending_limit: 'Paused',
        model_access: 'Waiting', sleeping: 'Waiting', queued_and_pausing: 'Pausing…', finalizing_failed: 'Finalizing…',
        unknown: 'Activity unconfirmed', ended_with_runs: 'Working',
    };
    for (const [name, word] of Object.entries(words)) assert.equal(roomStatusView(stateOf(name)).text, word, name);
    // A waiting room whose turn is a question says so in its word, in the amber ink beside the owner's dot; no note
    // repeats it.
    const asked = roomStatusView(stateOf('direct_question', 1));
    assert.deepEqual([asked.text, asked.tone, asked.owner, asked.amber], ['Waiting for your answer', 'wait', true, '']);
    for (const [call, word] of [['preparing', 'Preparing'], ['thinking', 'Thinking'], ['working', 'Working'], ['', 'Working']]) {
        assert.equal(activityWord({ activity: 'working', transition: 'none', call }), word, call);
    }
    assert.equal(activityWord({ activity: 'queued' }), 'Queued');
    assert.equal(activityWord({ activity: 'idle' }), 'Online');
    assert.equal(activityWord({ activity: 'something new' }), 'Activity unconfirmed');
});

test('the mark is the server\'s shape; whose move it is stands beside it, said in words where the word does not', () => {
    const view = (name, chatId = 42) => roomStatusView(stateOf(name, chatId));
    const facts = (name, chatId) => ['mark', 'tone', 'owner', 'amber'].map((key) => view(name, chatId)[key]);
    assert.deepEqual(facts('held_continue'), ['moving', 'live', false, '']);
    assert.deepEqual(facts('paused_by_owner'), ['pause', 'held', false, '']);
    assert.deepEqual(facts('paused_by_mind'), ['pause', 'held', false, ''], "Ouroboros's own Pause is not the owner's move");
    // An action only the owner takes: the word keeps its neutral ink, and the control's name says what waits.
    assert.deepEqual(facts('spending_limit'), ['pause', 'held', true, 'action']);
    assert.deepEqual(facts('model_access'), ['still', 'held', true, 'action']);
    assert.deepEqual(facts('sleeping'), ['still', 'held', false, ''], 'a wait that ends by itself holds still dots');
    assert.deepEqual(facts('direct_question', 1), ['still', 'wait', true, ''], 'its own word says the question');
    assert.deepEqual(facts('unknown'), ['ring', 'unknown', false, '']);
    const pausing = view('queued_and_pausing');
    assert.deepEqual([pausing.text, pausing.tone, pausing.mark], ['Pausing…', 'held', 'moving'],
        'a Pause still settling: neutral ink, the server\'s moving mark');
    const queued = roomStatusView({ room: room('queued') });
    assert.deepEqual([queued.text, queued.mark, queued.tone], ['Queued', 'still', 'queued']);
    // A working room with a question elsewhere keeps its word and its shape; the owner's dot takes the mark's place.
    const mixed = roomStatusView({ room: room('working', { turn: [{ kind: 'question', task_id: 'x', quiz_id: 'q' }] }) });
    assert.deepEqual([mixed.text, mixed.tone, mixed.mark, mixed.owner, mixed.amber], ['Working', 'live', 'moving', true, 'question']);
    assert.deepEqual(markView(mixed), { shape: 'dot', hue: 'owner' });
    assert.deepEqual(markView(view('held_continue')), { shape: 'moving', hue: 'ink' });
    // Every room the server rolls up keeps the server's own mark: nothing on the client chooses another shape.
    for (const [name, { rooms }] of Object.entries(CASES)) {
        for (const [chat, served] of Object.entries(rooms || {})) assert.equal(view(name, Number(chat)).mark, served.mark, name);
    }
});

test('only the client facts overlay the room, each with its mark: socket closed, own send, start, own Stop press', () => {
    const working = stateOf('held_continue');
    assert.deepEqual(roomStatusView(working, { connected: false }),
        { kind: 'offline', text: 'Reconnecting…', tone: 'offline', mark: 'ring', owner: false, amber: '' });
    assert.equal(roomStatusView(stateOf('spending_limit'), { connected: false }).owner, false,
        'a closed socket proves nothing current, the owner\'s move included');
    const idle = emptyWorkState();
    assert.deepEqual(roomStatusView(idle),
        { kind: 'online', text: 'Online', tone: 'online', mark: 'none', owner: false, amber: '' });
    assert.deepEqual(roomStatusView(idle, { ready: false }),
        { kind: 'starting', text: 'Starting…', tone: 'starting', mark: 'none', owner: false, amber: '', title: '' });
    // The owner's own unconfirmed send outranks a quiet room, never work the room already does; it moves.
    const said = (state, overlay) => ['text', 'mark', 'owner'].map((key) => roomStatusView(state, overlay)[key]);
    assert.deepEqual(said(idle, { sending: true }), ['Sending', 'moving', false]);
    assert.deepEqual(said(stateOf('paused_by_owner'), { sending: true }), ['Sending', 'moving', false]);
    assert.deepEqual(said(stateOf('spending_limit'), { sending: true }), ['Sending', 'moving', true],
        'what waits for the owner still waits while a message goes out');
    assert.equal(roomStatusView(working, { sending: true }).text, 'Working');
    // The owner's own Stop press on every working work says Cancelling… or Finalizing… until the server catches up.
    const records = new Map([['c', { cancelPendingPolicy: 'immediate' }]]);
    assert.equal(ownStopPress(working, records), 'immediate');
    assert.deepEqual(said(working, { stopping: 'immediate' }), ['Cancelling…', 'moving', false]);
    assert.deepEqual(said(working, { stopping: 'finalize' }), ['Finalizing…', 'moving', false]);
    assert.equal(ownStopPress(working, new Map()), '');
    assert.equal(ownStopPress(stateOf('paused_by_owner'), new Map([['o', { cancelPendingPolicy: 'immediate' }]])), '',
        'a press on work that does not work changes no word');
});

test('reasons read their code through the catalog; counts stay parameters; an unknown code reads its sentence', () => {
    const held = work('held_continue');
    assert.equal(reasonText(held.because[0]), 'Continue starts when 3 helpers of the earlier attempt finish');
    assert.equal(reasonText({ code: 'brand_new_reason', text: 'A sentence no client knows yet' }), 'A sentence no client knows yet');
    assert.equal(reasonText({ code: '', text: 'raw' }), 'raw');
    assert.equal(reasonText(null), '');
    // Short reason words stand beside a word; a code without one keeps its sentence.
    assert.equal(reasonBeside(work('paused_by_owner').because[0]), 'by you');
    assert.equal(reasonBeside(work('paused_by_mind').because[0]), 'by Ouroboros');
    assert.equal(reasonBeside(work('spending_limit').because[0]), 'spending limit');
    assert.equal(reasonBeside(work('sleeping').because[0]), 'sleeping');
    assert.equal(reasonBeside({ code: 'saved_on_stop', text: 'Saved when the app stopped; Resume continues it' }), 'app stopped');
    assert.equal(reasonBeside(work('model_access').because[0]), 'The model needs you: sign in or restore access');
    const prior = currentPayload();
    try {
        applyPayload({ language: 'ru', english: false, entries: {
            'Continue starts when {count} helpers of the earlier attempt finish':
                { text: 'Продолжение начнётся, когда закончат {count} помощника прошлой попытки' },
            [CODE_PREFIX + 'work.because.earlier_attempt_finishing']: { text: 'Продолжение начнётся, когда закончит помощник' },
            [CODE_PREFIX + 'work.because.model_access']: { text: 'Модели нужен вход' },
            [CODE_PREFIX + 'work.short.by_you']: { text: 'вами' },
        } });
        // The count is a parameter of the translated template, never a number baked into a translation.
        assert.equal(reasonText(held.because[0]), 'Продолжение начнётся, когда закончат 3 помощника прошлой попытки');
        assert.equal(reasonText({ ...held.because[0], count: 18, text: 'Continue starts when 18 helpers of the earlier attempt finish' }),
            'Продолжение начнётся, когда закончат 18 помощника прошлой попытки');
        assert.equal(reasonText({ ...held.because[0], count: 1 }), 'Продолжение начнётся, когда закончит помощник');
        assert.equal(reasonText(work('model_access').because[0]), 'Модели нужен вход');
        assert.equal(reasonBeside(work('paused_by_owner').because[0]), 'вами');
        assert.equal(reasonText({ code: 'brand_new_reason', text: 'Unknown sentence' }), 'Unknown sentence');
    } finally { applyPayload(prior); }
});

test('every reason the server can send reads in the server\'s own sentence; a counted one in its template', () => {
    assert.ok(Object.keys(SERVER_REASONS).length > 10);
    for (const [code, forms] of Object.entries(SERVER_REASONS)) {
        for (const [count, text] of Object.entries(forms)) {
            const reason = count ? { code, text, count: Number(count) } : { code, text };
            assert.equal(reasonText(reason), text, `${code} ${count}`);
        }
    }
    // A counted code this client does not know keeps the server's sentence: no translation bakes its number.
    assert.equal(reasonText({ code: 'brand_new_count', count: 2, text: '2 brand new things' }), '2 brand new things');
});

test('the reason beside a word is never the one the word already says, in any language', () => {
    const asked = work('direct_question');
    assert.deepEqual([workRowView(asked).word, workRowView(asked).secondary], ['Waiting for your answer', '']);
    const prior = currentPayload();
    try {
        applyPayload({ language: 'ru', english: false, entries: {
            [CODE_PREFIX + 'task.chip.waiting_for_answer']: { text: 'Ждёт вашего ответа' } } });
        // The word is translated and the server's sentence is not: still nothing beside the word.
        assert.deepEqual([workRowView(asked).word, workRowView(asked).secondary], ['Ждёт вашего ответа', '']);
        // Another reason beside the same word still shows.
        assert.equal(workRowView(work('model_access')).secondary, 'The model needs you: sign in or restore access');
    } finally { applyPayload(prior); }
});

test('the owner\'s own Stop press on a work\'s card says itself on the row at once; work that does not work keeps its word', () => {
    const live = work('working_call');
    assert.deepEqual(['word', 'secondary', 'tone', 'mark'].map((key) => workRowView(live, { stopping: 'finalize' })[key]),
        ['Finalizing…', '', 'live', 'moving']);
    assert.equal(workRowView(live, { stopping: 'immediate' }).word, 'Cancelling…');
    assert.equal(workRowView(live).word, 'Sending');
    assert.equal(workRowView(work('paused_by_owner'), { stopping: 'immediate' }).word, 'Paused');
});

test('a reusable slot gets no control by its id; a real target keeps its own', () => {
    const slot = { controls: [{ action: 'stop', target: 'active' }, { action: 'pause', target: 'active' },
        { action: 'stop', target: 't-1' }] };
    assert.deepEqual(workControls(slot), { stop: ['t-1'], stopEarlier: [], pause: '', resume: '', hurry: '', wrapUp: '' });
});

test('a held Continue with three helpers of its failed earlier attempt: one working row with the reason, Stop and Stop earlier attempt, no Resume', () => {
    const state = stateOf('held_continue');
    assert.equal(state.works.length, 1, 'one work, one row: the Continue and its earlier helpers are inside it');
    const row = workRowView(state.works[0]);
    assert.deepEqual(row, { word: 'Working', secondary: 'Continue starts when 3 helpers of the earlier attempt finish',
        tone: 'live', mark: 'moving', owner: false, outcome: '', focus: '' });
    const grouped = workControls(state.works[0]);
    assert.deepEqual(grouped, { stop: ['c', 'p'], stopEarlier: ['p'], pause: '', resume: '', hurry: '', wrapUp: '' });
    assert.deepEqual(workMenuActions(grouped), [ACTION_STOP_EARLIER, ACTION_STOP_NOW]);
    assert.equal(TASK_CONTROL_LABELS[ACTION_STOP_EARLIER], 'Stop earlier attempt');
    // The held Continue's own card says the work's word and reason, and offers no Resume.
    const card = cardControls(state, 'c');
    assert.deepEqual([card.resume, card.menu, Boolean(card.work)], [false, true, true]);
    assert.deepEqual(cardWorkView(state.works[0]), { phase: 'working', text: 'Working',
        secondary: 'Continue starts when 3 helpers of the earlier attempt finish', classes: 'working',
        tone: 'live', waiting: false });
    // A count says itself the same from one helper to eighteen; the structure does not change.
    const many = { ...state.works[0], because: [{ code: 'earlier_attempt_finishing', count: 18,
        text: 'Continue starts when 18 helpers of the earlier attempt finish' }] };
    assert.deepEqual(Object.keys(workRowView(many)), Object.keys(row));
    assert.equal(workRowView(many).tone, row.tone);
});

test('rows: their word, the reason beside it, tone, mark and the owner\'s move from the work alone', () => {
    const row = (name, index = 0) => workRowView(work(name, index));
    const quiet = { owner: false, outcome: '', focus: '' };
    assert.deepEqual(row('paused_by_owner'), { word: 'Paused', secondary: 'by you', tone: 'held', mark: 'pause', ...quiet });
    assert.deepEqual(row('spending_limit'), { word: 'Paused', secondary: 'spending limit', tone: 'held', mark: 'pause',
        ...quiet, owner: true });
    assert.deepEqual(row('model_access'), { word: 'Waiting', secondary: 'The model needs you: sign in or restore access',
        tone: 'held', mark: 'still', ...quiet, owner: true });
    // The word already says the owner's question: the reason does not repeat it; the owner's dot stands beside it.
    assert.deepEqual(row('direct_question'), { word: 'Waiting for your answer', secondary: '', tone: 'wait', mark: 'still',
        ...quiet, owner: true });
    assert.deepEqual(markView(row('direct_question')), { shape: 'dot', hue: 'owner' });
    // A Pause still settling: the owner's own Pause keeps the neutral ink while the work still moves.
    assert.deepEqual(['word', 'secondary', 'tone', 'mark'].map((key) => row('queued_and_pausing', 0)[key]),
        ['Pausing…', 'by you', 'held', 'moving']);
    assert.deepEqual(['word', 'tone', 'mark'].map((key) => row('queued_and_pausing', 1)[key]), ['Queued', 'queued', 'still']);
    assert.deepEqual(['tone', 'mark'].map((key) => row('working_call')[key]), ['live', 'moving']);
    assert.deepEqual(['word', 'tone', 'mark'].map((key) => row('sleeping')[key]), ['Waiting', 'held', 'still']);
    // An outcome stays beside Finalizing…, its dot in the outcome's tone; unknown stays unknown, a ring.
    assert.deepEqual(row('finalizing_failed'), { word: 'Failed', secondary: 'Finalizing…', tone: 'failed', mark: 'moving',
        ...quiet, outcome: 'error' });
    assert.deepEqual(markView(row('finalizing_failed')), { shape: 'dot', hue: 'failed' });
    assert.deepEqual(['word', 'tone', 'mark'].map((key) => row('unknown')[key]), ['Activity unconfirmed', 'unknown', 'ring']);
    assert.deepEqual([row('ended_with_runs').word, row('ended_with_runs').secondary], ['Working', '2 external runs still working']);
    assert.deepEqual(workQuestion(work('direct_question')), { taskId: 'd', quizId: 'q1' });
    assert.equal(workQuestion(work('model_access')), null, 'an access wait is not an answer wait');
});

test('a known outcome beside Finalizing… is a dot in its tone: Done with warnings succeeds, Stopped is neutral', () => {
    const said = (outcome) => {
        const view = workRowView({ ...work('finalizing_failed'), outcome });
        return [view.word, view.secondary, view.tone, view.outcome, markView(view)];
    };
    assert.deepEqual(said('completed'), ['Done', 'Finalizing…', 'done', 'done', { shape: 'dot', hue: 'ok' }]);
    assert.deepEqual(said('rejected_duplicate'), ['Done with warnings', 'Finalizing…', 'done', 'warn', { shape: 'dot', hue: 'ok' }]);
    assert.deepEqual(said('cancelled'), ['Stopped', 'Finalizing…', 'stopped', 'cancelled', { shape: 'dot', hue: 'neutral' }]);
    assert.deepEqual(said('failed'), ['Failed', 'Finalizing…', 'failed', 'error', { shape: 'dot', hue: 'failed' }]);
    assert.deepEqual(OUTCOME_TONES, { done: 'done', warn: 'done', cancelled: 'stopped', error: 'failed', timeout: 'failed',
        lifecycle_error: 'failed' });
});

test('whatever waits for the owner takes a row\'s mark place beside any word; the word keeps its own ink', () => {
    const question = [{ kind: 'question', task_id: 'w', quiz_id: 'q' }];
    const asking = workRowView({ ...work('working_call'), turn: question });
    assert.deepEqual([asking.word, asking.tone, asking.mark, asking.owner], ['Sending', 'live', 'moving', true]);
    assert.deepEqual(markView(asking), { shape: 'dot', hue: 'owner' });
    // The owner's own Stop press says itself and keeps the owner's dot; an outcome yields to it too.
    const stopping = workRowView({ ...work('working_call'), turn: question }, { stopping: 'finalize' });
    assert.deepEqual([stopping.word, stopping.mark, markView(stopping)], ['Finalizing…', 'moving', { shape: 'dot', hue: 'owner' }]);
    assert.deepEqual(markView(workRowView({ ...work('finalizing_failed'), turn: [{ kind: 'action' }] })),
        { shape: 'dot', hue: 'owner' });
    // Nothing for the owner: the work's shape in the word's ink.
    assert.deepEqual(markView(workRowView(work('working_call'))), { shape: 'moving', hue: 'ink' });
    assert.deepEqual(markView(workRowView(work('paused_by_owner'))), { shape: 'pause', hue: 'ink' });
});

test('the test builders name the marks the server names', () => {
    for (const { works, rooms } of Object.values(CASES)) {
        for (const item of works) assert.equal(serverMark(item.activity, item.confirmed), item.mark, item.work_id);
        for (const served of Object.values(rooms || {})) {
            assert.equal(serverMark(served.activity, served.confirmed), served.mark, served.activity);
        }
    }
    for (const activity of ['working', 'queued', 'waiting', 'paused', 'unknown']) {
        assert.equal(room(activity).mark, serverMark(activity, activity !== 'unknown'), activity);
    }
    assert.equal(builtWork({ activity_id: 'x' }, { activity: 'paused' }).mark, 'pause');
    assert.equal(builtWork({ activity_id: 'x' }, { activity: 'queued', confirmed: false }).mark, 'ring');
    assert.equal(builtWork({ activity_id: 'x' }, { confirmed: false }).mark, 'moving', 'work observed running moves');
});

test('the focus line is the work\'s own update_focus text under its state, and none without one', () => {
    const focused = { ...work('working_call'), focus: { text: '  Release 7.7 · code merged · waiting for 7 signed files ', at: 1 } };
    assert.equal(workRowView(focused).focus, 'Release 7.7 · code merged · waiting for 7 signed files');
    assert.equal(workRowView(work('working_call')).focus, '');
    assert.equal(workRowView({ ...work('working_call'), focus: { text: '   ', at: 1 } }).focus, '');
});

test('controls group by action with exact targets; Resume is a button, a pending Stop leaves its escalation', () => {
    const running = workControls(work('working_call'));
    assert.deepEqual(workMenuActions(running), [ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_STOP_NOW]);
    assert.deepEqual(workMenuActions(running, { cancelPending: true }), [ACTION_STOP_NOW]);
    const paused = workControls(work('paused_by_owner'));
    assert.equal(paused.resume, 'o');
    assert.deepEqual(workMenuActions(paused), [ACTION_STOP_NOW], 'Resume stands as its own button');
    assert.deepEqual(workMenuActions(paused, { resume: true }), [ACTION_RESUME, ACTION_STOP_NOW]);
    assert.deepEqual(workMenuActions(workControls(work('direct_question'))), [ACTION_PAUSE, ACTION_STOP_NOW],
        'a direct turn waiting for its answer can be paused and stopped, as its doors take it');
    assert.deepEqual(workControls({ controls: [{ action: 'teleport', target: 'x' }, { action: 'stop', target: '' }] }),
        { stop: [], stopEarlier: [], pause: '', resume: '', hurry: '', wrapUp: '' }, 'unknown or targetless actions are not offered');
    assert.deepEqual([...resumeTargets(body('paused_by_owner'))], ['o']);
    assert.deepEqual([...resumeTargets(body('held_continue'))], [], 'a held Continue is never resumed by the owner');
});

test('a card menu reads its work, else its own task controls without Resume', () => {
    const state = stateOf('paused_by_owner');
    assert.deepEqual(cardMenuActions(state, { groupId: 'o' }), [ACTION_STOP_NOW]);
    assert.equal(cardControls(state, 'o').resume, true);
    assert.deepEqual(cardMenuActions(state, { groupId: 'loose' }, { phase: 'budget_paused' }), [ACTION_STOP_NOW]);
    assert.deepEqual(cardMenuActions(state, { groupId: 'loose' }, { phase: 'working' }),
        [ACTION_FINALIZE, ACTION_HURRY, ACTION_PAUSE, ACTION_STOP_NOW]);
    assert.equal(cardControls(state, 'loose').resume, false);
});

test('a parent that finished while its helper works keeps the Stop its work offers; an ended work offering nothing gives none', () => {
    const helper = stateOf('helper_after_parent');
    assert.equal(work('helper_after_parent').episode.live, false, 'the server says the parent\'s episode ended');
    const card = cardControls(helper, 'p');
    assert.deepEqual([card.menu, card.resume, card.work?.work_id], [true, false, 'p']);
    assert.deepEqual(cardMenuActions(helper, { groupId: 'p' }), [ACTION_STOP_NOW]);
    // An ended work that offers nothing (the server may also offer its open runs' Stop) gives none:
    // the card keeps its own controls.
    const ended = stateOf('ended_with_runs', 42, { works: [{ ...work('ended_with_runs'), controls: [] }] });
    const runs = cardControls(ended, 'r');
    assert.deepEqual([runs.menu, runs.resume, runs.work], [false, false, null]);
});

test('the grouped Stop reaches every target in order and says one combined result', async () => {
    const asked = [], toasts = [];
    const ok = await requestStopAll(['c', 'p', 'c', ''], ACTION_STOP_NOW, {
        stopOne: async (target, action) => { asked.push([target, action]); },
        toast: (...args) => toasts.push(args) });
    assert.equal(ok, true);
    assert.deepEqual(asked, [['c', ACTION_STOP_NOW], ['p', ACTION_STOP_NOW]]);
    assert.deepEqual(toasts, []);
    const failing = await requestStopAll(['c', 'p'], ACTION_STOP_NOW, {
        stopOne: async (target) => { if (target === 'p') throw Object.assign(new Error('busy'), { status: 409 }); },
        toast: (...args) => toasts.push(args) });
    assert.equal(failing, false);
    assert.deepEqual(toasts, [['Stop failed for 1 of 2 parts of this work: busy', 'error']]);
    toasts.length = 0;
    // A target that already ended is settled, not a failure.
    assert.equal(await requestStopAll(['gone'], ACTION_STOP_NOW, {
        stopOne: async () => { throw Object.assign(new Error('missing'), { status: 404 }); },
        toast: (...args) => toasts.push(args) }), true);
    assert.deepEqual(toasts, []);
});

test('an incomplete body keeps what it omitted, unconfirmed and without controls; only a complete one clears', () => {
    const seen = stateOf('held_continue');
    const partial = reconcileWorkState(seen, { supervisor_ready: true, works: [], rooms: {}, works_complete: false }, 42);
    assert.equal(partial.complete, false);
    assert.deepEqual(partial.works.map((item) => [item.work_id, item.activity, item.confirmed, item.controls.length, item.mark]),
        [['p', 'unknown', false, 0, 'ring']]);
    assert.deepEqual(['word', 'tone', 'mark'].map((key) => workRowView(partial.works[0])[key]),
        ['Activity unconfirmed', 'unknown', 'ring'], 'what an incomplete body omitted is a ring, never moving work');
    assert.deepEqual([partial.room.activity, partial.room.mark, roomStatusView(partial).text], ['unknown', 'ring', 'Activity unconfirmed']);
    // Not ready, or read over a closed socket: no absence is proved either.
    assert.equal(reconcileWorkState(seen, { ...body('held_continue'), works: [], rooms: {}, supervisor_ready: false }, 42).works.length, 1);
    assert.equal(reconcileWorkState(seen, { ...body('held_continue'), works: [], rooms: {} }, 42, false).works.length, 1);
    const cleared = reconcileWorkState(partial, { supervisor_ready: true, works: [], rooms: {}, works_complete: true }, 42);
    assert.deepEqual([cleared.works.length, cleared.room.activity, roomStatusView(cleared).text], [0, 'idle', 'Online']);
    // A failed read or a body without the projection changes nothing.
    assert.equal(reconcileWorkState(seen, null, 42), seen);
    assert.equal(reconcileWorkState(seen, { active_chat_activities: [] }, 42), seen);
    // Nothing seen before and nothing listed: the room is quiet, never invented work.
    assert.equal(roomStatusView(reconcileWorkState(emptyWorkState(), { works: [], works_complete: false }, 42)).text, 'Online');
});

test('a room reads only its own works; every room feeds the card index and the member ids', () => {
    const merged = { supervisor_ready: true, works_complete: true,
        works: [...CASES.held_continue.works, ...CASES.direct_question.works],
        rooms: { ...CASES.held_continue.rooms, ...CASES.direct_question.rooms },
        active_chat_activities: [{ activity_id: 'c', task_attempt: 2 }] };
    const main = reconcileWorkState(emptyWorkState(), merged, 1);
    assert.deepEqual(main.works.map((item) => item.work_id), ['d']);
    assert.equal(main.room.activity, 'waiting');
    // A Main portal's work runs in its Project: its card still finds that work.
    assert.equal(main.episodes.get('c').work_id, 'p');
    assert.deepEqual([...main.members].sort(), ['c', 'd', 'h0', 'h1', 'h2']);
    assert.equal(main.attempts.get('c'), 2);
    assert.equal(reconcileWorkState(emptyWorkState(), merged, 42).works[0].work_id, 'p');
});

test('a work card says its work\'s word and short reason; plain running, finalizing and unconfirmed work keep their own', () => {
    const view = (name, index = 0) => cardWorkView(work(name, index));
    assert.deepEqual([view('paused_by_owner').text, view('paused_by_owner').classes], ['Paused · by you', 'paused']);
    assert.equal(view('paused_by_mind').text, 'Paused · by Ouroboros');
    assert.deepEqual([view('spending_limit').text, view('spending_limit').classes], ['Paused · spending limit', 'warn']);
    assert.equal(view('sleeping').text, 'Waiting · sleeping');
    assert.deepEqual([view('model_access').text, view('model_access').secondary],
        ['Waiting', 'The model needs you: sign in or restore access']);
    assert.deepEqual([view('queued_and_pausing', 0).text, view('queued_and_pausing', 0).tone], ['Pausing… · by you', 'neutral']);
    assert.equal(view('working_call'), null, 'its own model-call words already say Sending');
    assert.equal(view('finalizing_failed'), null, 'its outcome beside Finalizing… is its own');
    assert.equal(view('unknown'), null);
    assert.equal(view('ended_with_runs'), null, 'an ended episode keeps its own outcome');
    // The chip presenter takes the view, but a disconnected card still says Activity unconfirmed.
    const workView = view('paused_by_owner');
    assert.equal(taskActivityPresentation({ phase: 'budget_paused', workView }).text, 'Paused · by you');
    assert.equal(taskActivityPresentation({ phase: 'budget_paused', workView }).className, 'chat-live-phase paused');
    assert.equal(taskActivityPresentation({ phase: 'unknown', workView }).text, 'Activity unconfirmed');
    const records = new Map([['o', { groupId: 'o' }], ['child', { groupId: 'o', isSubagent: true }]]);
    assert.equal(applyCardWorkViews(records, stateOf('paused_by_owner')), true);
    assert.equal(records.get('o').workView.text, 'Paused · by you');
    assert.deepEqual(records.get('o').workLook, { mark: 'pause', owner: false }, 'the work\'s own shape, nobody\'s move');
    assert.equal(records.get('child').workView, undefined, 'a helper card keeps its own presenter');
    assert.equal(applyCardWorkViews(records, stateOf('paused_by_owner')), false, 'an equal view is no change');
});

// The work's look reaches its card apart from the word: a running parent whose helper waits for the owner keeps
// its own live word with the owner's amber dot (the server takes the turn from every member), and the look survives
// the card's outcome, its record adapter and its painter (docs/DESIGN.md "Status marks").
test('a work card keeps its work\'s look beside its own words: the owner\'s dot reaches a running parent', () => {
    const parent = structuredClone(CASES.working_call.works[0]);
    assert.equal(cardWorkView(parent), null, 'plain running work keeps the card\'s own words');
    parent.turn = [{ kind: 'question', task_id: 'helper', quiz_id: 'q1' }];
    const body = { supervisor_ready: true, works_complete: true, works: [parent], rooms: {} };
    const state = reconcileWorkState(emptyWorkState(), body, 42);
    const record = { groupId: parent.episode.task_id };
    assert.equal(applyCardWorkViews(new Map([['w', record]]), state), true);
    assert.deepEqual([record.workView, record.workLook], [null, { mark: 'moving', owner: true }]);
    const view = desiredLiveCardPhase(record);
    assert.deepEqual([view.text, view.tone, view.owner, view.mark], ['Working', 'live', true, 'moving']);
    // The outcome owns the chip while its work finalizes; the owner's move stays its dot.
    const late = desiredLiveCardPhase({ ...record, finalizingHold: true, observedOutcome: 'done' });
    assert.deepEqual([late.text, late.tone, late.owner, late.secondary], ['Done', 'ok', true, 'Finalizing…']);
    // The work's own word that is the owner's move takes the amber body too.
    const limit = structuredClone(CASES.spending_limit.works[0]);
    const waits = { ...record, workView: cardWorkView(limit), workLook: { mark: limit.mark, owner: limit.turn.length > 0 } };
    assert.deepEqual([desiredLiveCardPhase(waits).text, desiredLiveCardPhase(waits).tone, desiredLiveCardPhase(waits).owner],
        ['Paused · spending limit', 'owner', true]);
    // A turn the body lists is a fact even when another member could not be confirmed: the dot stays.
    const partial = { ...parent, confirmed: false };
    const read = reconcileWorkState(emptyWorkState(), { ...body, works_complete: false, works: [partial] }, 42);
    applyCardWorkViews(new Map([['w', record]]), read);
    assert.deepEqual([record.workView, record.workLook], [null, { mark: 'moving', owner: true }]);
    assert.deepEqual([desiredLiveCardPhase(record).text, desiredLiveCardPhase(record).owner], ['Working', true]);
    // A work an incomplete body omitted proves nothing: no look, so the card's own words and shape stand.
    const kept = reconcileWorkState(state, { supervisor_ready: true, works_complete: false, works: [], rooms: {} }, 42);
    applyCardWorkViews(new Map([['w', record]]), kept);
    assert.deepEqual([record.workView, record.workLook], [null, null]);
    assert.equal(desiredLiveCardPhase(record).owner, false);
});

test('the census pause words are the short reason words, and only the projection may say who paused', () => {
    assert.equal(pausePhaseLabel('budget_paused', 'budget'), 'Paused · spending limit');
    assert.equal(pausePhaseLabel('budget_paused', 'restart'), 'Paused · app stopped');
    assert.equal(pausePhaseLabel('budget_paused', 'authority'), 'Paused · technical problem');
    assert.equal(pausePhaseLabel('budget_paused', 'sleep'), 'Waiting · sleeping');
    // The census `owner` cause also covers Ouroboros's own Pause: it never claims "by you".
    assert.equal(pausePhaseLabel('budget_paused', 'owner'), 'Paused');
    assert.equal(pausePhaseLabel('budget_pausing', 'owner'), 'Pausing…');
});
