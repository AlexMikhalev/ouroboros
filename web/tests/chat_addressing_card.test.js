// A turn's block is one predicate over facts the record already holds
// (docs/DESIGN.md "Conversation activity block"): every tool call is a compact
// row and therefore content, whatever the tool's name; an addressing action is
// represented on the owner's message by the typed routing receipt, and the
// block beside it shows the call honestly. No client tool-name list decides
// presence, no sticky flag survives the facts, and a recovered tool failure
// stays visible after a reload through the summary row's error term.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { summarizeChatLiveEvent, taskTerminalSummary } from '../modules/log_events.js';
import { ElementStub, installDom, restoreDom, walkCard } from './chat_dom_fixture.js';

// The flat fixture parses markup into bare children; give each parsed child
// the markup from its own tag onward so a rendered row's text is assertable.
const innerHTMLDescriptor = Object.getOwnPropertyDescriptor(ElementStub.prototype, 'innerHTML');
Object.defineProperty(ElementStub.prototype, 'innerHTML', {
    configurable: true,
    get() { return innerHTMLDescriptor.get.call(this); },
    set(value) {
        innerHTMLDescriptor.set.call(this, value);
        const html = String(value || '');
        let cursor = 0;
        for (const child of this.children) {
            const index = html.indexOf(`<${child.tagName.toLowerCase()}`, cursor);
            if (index < 0) continue;
            child._innerHTML = html.slice(index);
            cursor = index + 1;
        }
    },
});

const TS = '2026-09-12T12:00:00Z';
const TASK = 'ordinary-turn';

function fixture(history = []) {
    const { prior, mount } = installDom(async (url) => ({ ok: true, json: async () =>
        String(url).startsWith('/api/chat/history')
            ? { messages: history, window: { complete: true } }
            : { active_direct_turns: [] } }));
    const handlers = new Map();
    const ws = { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); },
        isConnected: () => true, send() {} };
    const instance = createChatInstance({ ws,
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
        updateUnreadBadge() {}, chatId: 1, idPrefix: 'chat', mountEl: mount,
        stateSnapshots: { begin: () => ({ generation: 1, requestedAt: Date.now() }),
            isCurrent: () => true, apply() {} },
    });
    const messages = document.byId.get('chat-messages');
    const nodes = (node) => [node, ...(node?.children || []).flatMap(nodes)];
    return { instance, messages,
        card: () => walkCard(messages, TASK),
        rows: () => nodes(walkCard(messages, TASK)).filter((n) => n.classList?.contains('chat-live-line')),
        meta: () => walkCard(messages, TASK)?.querySelector('[data-live-meta]')?.innerHTML || '',
        emit: (type, row) => handlers.get(type)({ chat_id: 1, ts: TS, ...row }),
        log: (row) => handlers.get('log')({ chat_id: 1, data: { task_id: TASK, ts: TS, ...row } }),
        close() { instance.destroy(); restoreDom(prior); },
    };
}

const ownerRow = { role: 'user', content: 'Please work on this', text: 'Please work on this',
    client_message_id: 'owner-message', ts: TS, chat_id: 1 };
const annotation = { annotation_type: 'routing_ack', client_message_id: 'owner-message',
    action: 'promote_chat_to_task', status: 'scheduled', target: 'managed-root',
    target_title: 'Requested work' };
const final = { task_id: TASK, role: 'assistant', content: 'The task is scheduled.',
    text: 'The task is scheduled.', task_terminal_status: 'completed', tool_calls: 1,
    outcome_axes: { execution: { status: 'ok' } }, reason_code: 'final_message',
    accounted_upper_bound_usd: 0.75, cost_final: true, cost_accounting_status: 'available' };

for (const tool of ['promote_chat_to_task', 'route_to_project', 'steer_task']) {
    test(`${tool} is a compact row beside the owner annotation, live and after the terminal aggregate`, () => {
        const f = fixture();
        try {
            f.emit('chat', ownerRow);
            f.log({ type: 'task_started' });
            assert.equal(Boolean(f.card()), false, 'a plain start is not content');
            f.log({ type: 'tool_call_started', tool, tool_call_id: 'call-1' });
            assert.ok(f.card(), 'the first tool call is the block');
            assert.equal(f.rows().length, 1);
            assert.match(f.rows()[0].innerHTML, new RegExp(tool));
            f.emit('message_annotation', { ...annotation, action: tool });
            f.log({ type: 'tool_call_finished', tool, tool_call_id: 'call-1', is_error: false, duration_sec: 0.4 });
            assert.equal(f.rows().length, 1, 'start and finish evolve one row');
            assert.match(summarizeChatLiveEvent({ type: 'tool_call_finished', task_id: TASK, tool, tool_call_id: 'call-1', duration_sec: 0.4 }).headline, /✓ 0\.4s/);
            const owner = f.messages.children.find((node) => node.dataset.clientMessageId === 'owner-message');
            assert.ok(owner?.querySelector('.msg-routing-annotation'), 'the receipt is on the original message');
            f.emit('chat', final);
            f.log({ ...final, type: 'task_done', status: 'completed' });
            assert.equal(f.card().dataset.finished, '1');
            assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, 'done');
            assert.match(f.meta(), /1 tool call/);
            assert.equal(f.messages.children.filter((n) => n.classList.contains('assistant')
                && /The task is scheduled/.test(n.innerHTML)).length, 1, 'authored answer remains visible');
        } finally { f.close(); }
    });
}

test('read_file followed by promote keeps one block with both rows, the full count and cost', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'read_file', args: { path: 'docs/plan.md' } });
        const card = f.card();
        assert.ok(card);
        assert.match(f.rows()[0].innerHTML, /read_file · docs\/plan\.md/);
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        assert.equal(f.rows().length, 2);
        f.emit('chat', { ...final, tool_calls: 2 });
        assert.equal(f.card(), card);
        assert.equal(card.dataset.finished, '1');
        assert.match(f.meta(), /2 tool calls/);
        assert.match(f.meta(), /\$0\.75/);
    } finally { f.close(); }
});

test('failed steer turns its row into the error row and the terminal keeps the failure', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'steer_task', tool_call_id: 'steer-1' });
        assert.ok(f.card());
        f.log({ type: 'tool_call_finished', tool: 'steer_task', tool_call_id: 'steer-1', is_error: true, error: 'Target unavailable' });
        assert.equal(f.rows().length, 1, 'the failure lands on the call\'s own row');
        const failure = summarizeChatLiveEvent({ type: 'tool_call_finished', task_id: TASK, tool: 'steer_task', tool_call_id: 'steer-1', is_error: true, error: 'Target unavailable' });
        assert.equal(failure.dedupeKey, summarizeChatLiveEvent({ type: 'tool_call_started', task_id: TASK, tool: 'steer_task', tool_call_id: 'steer-1' }).dedupeKey);
        assert.deepEqual([failure.phase, failure.visible], ['error', true]);
        f.log({ type: 'task_done', status: 'failed', reason_code: 'tool_failure',
            outcome_axes: { execution: { status: 'failed' } } });
        assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, 'error');
    } finally { f.close(); }
});

for (const type of ['task_metrics_event', 'task_eval']) {
    test(`a late ${type} aggregate mints the summary row for a turn whose calls were never seen`, () => {
        const f = fixture();
        try {
            f.emit('chat', { ...final, tool_calls: undefined });
            assert.equal(Boolean(f.card()), false, 'a final that reports no calls is a plain answer');
            f.log({ ...final, type, tool_calls: 1, tool_call_counts: { promote_chat_to_task: 1 } });
            assert.ok(f.card(), 'the aggregate is the only evidence of the call');
            // The summary row beside the completion note the final already left.
            assert.equal(f.rows().length, 2);
            assert.ok(f.rows().some((n) => /1 tool call/.test(n.innerHTML)));
            f.log({ ...final, type, tool_calls: 2, outcome_axes: undefined, tool_call_counts: { promote_chat_to_task: 1, read_file: 1 } });
            assert.equal(f.rows().length, 2, 'a larger aggregate updates the same row');
            assert.match(f.meta(), /2 tool calls/);
        } finally { f.close(); }
    });
}

test('a history rebuild keeps a live block whose evidence the window does not carry', async () => {
    const f = fixture([{ ...ownerRow, chat_annotation: annotation }]);
    try {
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        await f.instance.refreshHistory({ revision: 1 });
        assert.ok(f.card(), 'the rebuilt transcript keeps the live rows');
        f.log({ type: 'task_metrics_event', tool_calls: 2 });
        assert.match(f.meta(), /2 tool calls/);
    } finally { f.close(); }
});

test('an aggregate that recorded a tool error keeps the error in view when the finish frame was missed', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'steer_task' });
        f.log({ type: 'task_metrics_event', tool_calls: 1, tool_errors: 1 });
        assert.ok(f.card());
        assert.match(f.meta(), /1 error/);
    } finally { f.close(); }
});

test('ordinary authored progress and runtime failures stay visible', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
        f.emit('chat', { task_id: TASK, role: 'assistant', is_progress: true, content: 'Inspecting the source.' });
        assert.ok(f.card(), 'real narration keeps its existing card');
        f.emit('chat', { role: 'system', system_type: 'terminal_incident', content: 'Provider outcome unknown.' });
        assert.ok(f.messages.children.some((node) => /Provider outcome unknown/.test(node.innerHTML)));
    } finally { f.close(); }
});

test('a window without the summary row keeps the annotation and the answer and mints no block', async () => {
    const history = [{ ...ownerRow, chat_annotation: annotation },
        { ...final, ts: TS, chat_id: 1, ephemeral_decision: true }];
    const f = fixture(history);
    try {
        await f.instance.refreshHistory({ revision: 1 });
        assert.equal(Boolean(f.card()), false);
        assert.ok(f.messages.children.some((node) => /The task is scheduled/.test(node.innerHTML)));
        const owner = f.messages.children.find((node) => node.dataset.clientMessageId === 'owner-message');
        assert.ok(owner?.querySelector('.msg-routing-annotation'));
        await f.instance.refreshHistory({ revision: 2 });
        assert.equal(Boolean(f.card()), false);
        assert.equal(f.messages.children.filter((node) => node.dataset.clientMessageId === 'owner-message').length, 1);
    } finally { f.close(); }
});

test('an old ephemeral marker cannot manufacture terminal status', () => {
    assert.equal(taskTerminalSummary({ type: 'task_done', task_id: TASK, ephemeral_decision: true }).terminal, false);
    assert.equal(taskTerminalSummary({ type: 'task_done', task_id: TASK, status: 'completed' }).terminal, true);
});

for (const [label, counts, total, errors, row] of [
    ['promotion only', { promote_chat_to_task: 1 }, 1, 0, /1 tool call/],
    ['several addressing calls', { promote_chat_to_task: 2, steer_task: 1 }, 3, 0, /3 tool calls/],
    ['read and promote', { read_file: 1, promote_chat_to_task: 1 }, 2, 0, /read_file · promote_chat_to_task/],
    ['failed steering', { steer_task: 1 }, 1, 1, /1 tool call · 1 error/],
    ['unknown errors', { promote_chat_to_task: 1 }, 1, null, /1 tool call/],
    ['legacy summary', undefined, 1, undefined, /1 tool call/],
]) {
    test(`cold history shows the summary row and a late aggregate keeps it: ${label}`, async () => {
        const summary = { ...final, role: 'system', system_type: 'task_summary',
            text: 'Recorded task summary.', rounds: 2, tool_calls: total,
            ...(counts === undefined ? {} : { tool_call_counts: counts }),
            ...(errors === undefined ? {} : { tool_errors: errors }) };
        const f = fixture([{ ...ownerRow, chat_annotation: annotation }, final, summary]);
        try {
            await f.instance.refreshHistory({ revision: 1 });
            assert.ok(f.card());
            assert.equal(f.card().dataset.finished, '1');
            assert.match(f.rows().map((n) => n.innerHTML).join('\n'), row);
            f.log({ ...summary, type: 'task_metrics_event' });
            assert.ok(f.card(), 'late totals preserve the historical evidence');
            await f.instance.refreshHistory({ revision: 2 });
            assert.ok(f.card());
            const owner = f.messages.children.find((node) => node.dataset.clientMessageId === 'owner-message');
            assert.ok(owner?.querySelector('.msg-routing-annotation'));
            assert.ok(f.messages.children.some((node) => /The task is scheduled/.test(node.innerHTML)));
            assert.match(f.meta(), /\$0\.75/);
            assert.equal(summary.tool_calls, total, 'presentation never rewrites the accounting aggregate');
        } finally { f.close(); }
    });
}

test('a zero-tool summary mints nothing on a cold client', async () => {
    const summary = { ...final, role: 'system', system_type: 'task_summary', text: 'Recorded.',
        rounds: 1, tool_calls: 0, tool_errors: 0, tool_call_counts: {} };
    const f = fixture([{ ...ownerRow, chat_annotation: annotation }, { ...final, tool_calls: 0 }, summary]);
    try {
        await f.instance.refreshHistory({ revision: 1 });
        assert.equal(Boolean(f.card()), false);
        f.log({ ...summary, type: 'task_metrics_event' });
        assert.equal(Boolean(f.card()), false, 'a zero aggregate is not evidence of work');
    } finally { f.close(); }
});

test('recorded narration stays visible beside complete addressing-only counts', async () => {
    const f = fixture([
        { task_id: TASK, role: 'assistant', is_progress: true, text: 'Inspecting the source.', ts: TS },
        { ...final, role: 'system', system_type: 'task_summary', text: 'Recorded summary.',
            rounds: 2, tool_errors: 0, tool_call_counts: { promote_chat_to_task: 1 } },
    ]);
    try {
        await f.instance.refreshHistory({ revision: 1 });
        assert.ok(f.card());
    } finally { f.close(); }
});

for (const [status, phase] of [['completed', 'done'], ['failed', 'error']]) {
    test(`late metrics update counts and cost on a finished ${status} block without moving its phase`, () => {
        const f = fixture();
        try {
            f.log({ type: 'tool_call_started', tool: 'promote_chat_to_task' });
            f.emit('chat', { ...final, task_terminal_status: status,
                outcome_axes: { execution: { status: status === 'failed' ? 'failed' : 'ok' } } });
            assert.equal(f.card().dataset.finished, '1');
            f.log({ type: 'task_metrics_event',
                outcome_axes: { lifecycle: { status: 'completed' }, execution: { status: 'ok' } },
                tool_calls: 2, tool_errors: 0,
                tool_call_counts: { promote_chat_to_task: 1, read_file: 1 },
                accounted_upper_bound_usd: 0.75, cost_final: true, cost_accounting_status: 'available' });
            assert.equal(f.card().dataset.finished, '1');
            assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, phase, 'the final owns the phase');
            assert.match(f.meta(), /2 tool calls/);
            assert.match(f.meta(), /\$0\.75/);
        } finally { f.close(); }
    });
}

test('a late tool start after the final adds no row and cannot reopen the block', () => {
    const f = fixture();
    try {
        f.log({ type: 'tool_call_started', tool: 'read_file' });
        const failed = { ...final, task_terminal_status: 'failed', outcome_axes: { execution: { status: 'failed' } } };
        f.emit('chat', failed);
        f.log({ ...failed, type: 'task_done', status: 'failed' });
        for (const event of [{ type: 'tool_call_started', tool: 'read_file' },
            { type: 'task_metrics_event', tool_calls: 2, tool_errors: 0, tool_call_counts: { read_file: 2 } }]) {
            f.log(event);
            assert.equal(f.rows().filter((n) => !n.classList.contains('done') && !n.classList.contains('error')).length, 1);
            assert.equal(f.card().querySelector('[data-live-phase]').dataset.phase, 'error');
            assert.equal(f.card().dataset.finished, '1');
            assert.match(f.meta(), /\$0\.75/);
        }
    } finally { f.close(); }
});
