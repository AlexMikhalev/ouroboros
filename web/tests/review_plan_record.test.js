import assert from 'node:assert/strict';
import test from 'node:test';

import {
    mergeReviewGroup,
    planReviewGroupFromTaskDetail,
    renderReviewsSection,
} from '../modules/review_presentation.js';

// The planning organ's record as the host stores it (plan_record, schema 3).
const answered = (seat, model, reply, extra = {}) => ({
    seat_id: seat, model, route: 'api_chat', owner_marked: true, dispatched: true, status: 'answered',
    reply: { prose: '', objections: [], withdrawn: [], ...reply }, ...extra,
});

const RECORD = {
    schema_version: 3,
    revisions: [
        { n: 1, ts: '2026-10-15T01:00:00+00:00', goal: 'Ship the deck', plan: 'outline first', body: false, affected_paths: [],
          done_when: [{ id: 'c1', claim: 'five slides', support: 'count them' }] },
        { n: 2, ts: '2026-10-15T02:00:00+00:00', goal: 'Ship the deck', plan: 'outline, then a story', body: false, affected_paths: [],
          done_when: [{ id: 'c1', claim: 'a five-slide story', support: 'read it' }] },
    ],
    asks: [
        { ask_id: 'a1', ts: '2026-10-15T01:05:00+00:00', revision: 1, message: 'First look.', host_added: [], participants: [
            answered('triad_a', 'm/a', { prose: 'The bar is low.', objections: [{ id: 'o1', text: 'five slides is not the bar' }] }),
            answered('triad_b', 'm/b', { prose: 'Fine.' }),
            { seat_id: 'triad_c', model: 'm/c', route: 'api_chat', owner_marked: true, dispatched: false, status: 'not_dispatched',
              not_dispatched_reason: 'health_skip[window_exhausted]' },
        ] },
        { ask_id: 'a2', ts: '2026-10-15T02:05:00+00:00', revision: 2, message: 'Raised the bar; o1?', host_added: [], participants: [
            answered('triad_a', 'm/a', { prose: 'Better.', withdrawn: [{ id: 'o1', why: 'revision 2 raised the bar' }] }),
            { seat_id: 'critic_x', model: 'm/x', route: 'api_chat', owner_marked: false, dispatched: true, status: 'answered',
              reply: { prose: 'No.', objections: [{ id: 'k1', text: 'wrong shape' }], withdrawn: [] } },
        ] },
    ],
};

test('the plan record renders its revisions, participants, contributions, open objections and done_when', () => {
    const group = planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: RECORD }, 'root');
    assert.equal(group.id, 'plan:root');
    assert.equal(group.label, 'Plan');
    assert.equal(group.subject, 'revision 2');
    assert.equal(group.state, 'terminal');  // nobody is awaited
    // A1's o1 was withdrawn by its raiser in a2; the added critic's k1 stands but never holds.
    assert.equal(group.verdict, '1 open objection');
    assert.equal(group.tone, 'warn');
    assert.deepEqual(group.attempts.map((a) => a.id), ['revision:2', 'ask:a1', 'ask:a2']);
    const revision = group.attempts[0];
    assert.match(revision.detailText, /done_when:\n  c1: a five-slide story — shown by: read it/);
    assert.match(revision.detailText, /earlier revision 1 .*: c1: five slides/);
    const first = group.attempts[1];
    assert.match(first.detailText, /triad_a \(m\/a\): answered — 1 objection: The bar is low\./);
    assert.match(first.detailText, /triad_c \(m\/c\): not sent — health_skip\[window_exhausted\]/);
    assert.equal(first.verdict, 'no objection');  // o1 no longer stands
    const second = group.attempts[2];
    assert.match(second.detailText, /critic_x \(m\/x, added — never holds\): answered — 1 objection: No\./);
    assert.match(second.detailText, /objection critic_x\/k1 \(never holds\): wrong shape/);
    assert.match(second.detailText, /triad_a \(m\/a\): answered — 0 objections, 1 withdrawn: Better\./);
    const html = renderReviewsSection([group], { sectionExpanded: true, expandedGroups: new Set([group.id]),
        expandedAttempts: new Set(group.attempts.map((a) => `${group.id}:${a.id}`)) });
    assert.match(html, /a five-slide story/);
    assert.match(html, /wrong shape/);
});

test('an owner-marked objection that stands may hold, and awaited answers keep the group live only while the task runs', () => {
    const record = { ...RECORD, asks: [RECORD.asks[0], {
        ask_id: 'a2', ts: 't', revision: 2, message: '', host_added: [], participants: [
            { seat_id: 'triad_a', model: 'm/a', route: 'api_chat', owner_marked: true, dispatched: true, status: 'pending' },
        ] }] };
    const live = planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: record }, 'root');
    assert.equal(live.verdict, '1 objection may hold');  // o1 stands: a1 raised it and nobody withdrew it
    assert.equal(live.state, 'running');
    assert.equal(live.activeCount, 1);
    assert.equal(live.attempts.at(-1).state, 'running');
    assert.equal(live.attempts.at(-1).progress, '0 of 1 answered');
    const done = planReviewGroupFromTaskDetail({ task_id: 'root', status: 'completed', plan_record: record }, 'root');
    assert.equal(done.state, 'terminal');
    assert.equal(done.attempts.at(-1).state, 'terminal');
});

test('a withdrawal by another seat and an omission are rendered as the host records them', () => {
    const record = { ...RECORD, asks: [RECORD.asks[0], {
        ask_id: 'a2', ts: 't', revision: 2, message: '', host_added: [], participants: [
            answered('triad_b', 'm/b', { withdrawn: [{ id: 'o1', why: 'I disagree with A' }] }),
            answered('triad_a', 'm/a', { prose: 'nothing about o1' }),
        ] }] };
    const group = planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: record }, 'root');
    assert.equal(group.verdict, '1 objection may hold');
    assert.match(group.attempts[1].detailText, /objection triad_a\/o1 \(not adjudicated\): five slides is not the bar/);
});

test('a body plan names the host-added reviewers and a record without asks says so', () => {
    const body = { schema_version: 3, revisions: [{ n: 1, ts: 't', goal: 'g', plan: 'p', body: true, body_note: 'constitutional: ouroboros/loop.py',
        affected_paths: ['ouroboros/loop.py'], done_when: [] }], asks: [] };
    const group = planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: body }, 'root');
    assert.equal(group.verdict, 'not asked yet');
    assert.match(group.attempts[0].detailText, /body plan: constitutional: ouroboros\/loop\.py/);
    assert.match(group.attempts[0].detailText, /done_when: \(none stated yet\)/);
    const asked = { ...body, asks: [{ ask_id: 'a1', ts: 't', revision: 1, message: '', host_added: ['triad_b', 'triad_c'], participants: [
        answered('triad_a', 'm/a', {}), answered('triad_b', 'm/b', {}, { host_added: true }), answered('triad_c', 'm/c', {}, { host_added: true }),
    ] }] };
    const withAsk = planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: asked }, 'root');
    assert.match(withAsk.attempts[1].detailText, /first ask on a body plan: the host added triad_b, triad_c/);
    assert.equal(withAsk.verdict, 'no open objection');
});

test('an empty plan record falls back to the retired organ\'s record and a detail with neither renders nothing', () => {
    const fingerprint = 'a'.repeat(64);
    const legacy = planReviewGroupFromTaskDetail({
        task_id: 'root', status: 'completed', plan_record: { schema_version: 3, revisions: [], asks: [] },
        plan_review_state: { schema_version: 2, current_attempt: { fingerprint, status: 'open', reason: '' },
            waves: [{ request_fingerprint: fingerprint, cycle_index: 1, aggregate: 'GREEN', closed: true }], waves_omitted: 0 },
    }, 'root');
    assert.equal(legacy.label, 'Plan review');
    assert.equal(legacy.attempts[0].label, 'wave 1');
    assert.equal(planReviewGroupFromTaskDetail({ task_id: 'root', status: 'completed' }, 'root'), null);
});

test('a refreshed record replaces the live ask in the store without inventing a verdict', () => {
    const store = new Map();
    const pending = { ...RECORD, asks: [{ ask_id: 'a1', ts: 't', revision: 2, message: '', host_added: [], participants: [
        { seat_id: 'triad_a', model: 'm/a', route: 'api_chat', owner_marked: true, dispatched: true, status: 'pending' },
    ] }] };
    mergeReviewGroup(store, planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: pending }, 'root'));
    assert.equal(store.get('plan:root').attempts.at(-1).state, 'running');
    const settled = { ...pending, asks: [{ ...pending.asks[0], participants: [answered('triad_a', 'm/a', { prose: 'ok' })] }] };
    mergeReviewGroup(store, planReviewGroupFromTaskDetail({ task_id: 'root', status: 'running', plan_record: settled }, 'root'));
    const merged = store.get('plan:root');
    assert.equal(merged.attempts.at(-1).state, 'terminal');
    assert.equal(merged.attempts.at(-1).verdict, 'no objection');
    assert.equal(merged.verdict, 'no open objection');
});
