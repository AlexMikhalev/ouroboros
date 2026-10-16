// One work, one card (work_portal.js) through the real Main chat consumer: a card whose work moved into a Project
// room stays the same node and becomes its portal — from /api/state task_bindings, from a census row naming the
// Project, and from a project_started / project_handoff row (no second entry) — reads its present from the
// all-chat census (CONTRACTS K8: last_line, last_line_at, tool_calls, detached), keeps [Reply] … [Stop…], and
// settles from the completion row's typed outcome. A census without the new fields leaves today's card.
import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { createWorkPortals } from '../modules/work_portal.js';
import { ElementStub, installDom, restoreDom, walkCard } from './chat_dom_fixture.js';

const originalQuery = ElementStub.prototype.querySelector;
after(() => { ElementStub.prototype.querySelector = originalQuery; });
ElementStub.prototype.querySelector = function (selector) {
    const direct = originalQuery.call(this, selector);
    if (direct) return direct;
    for (const child of this.children) { const found = child.querySelector(selector); if (found) return found; }
    return null;
};

const settle = async () => { for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };

function fixture({ history = [], details = {} } = {}) {
    const reads = [];
    const env = installDom(async (url) => {
        const path = String(url);
        if (path.startsWith('/api/tasks/')) reads.push(path.split('/').pop().split('?')[0]);
        return { ok: true, json: async () => (path.startsWith('/api/chat/history') ? { messages: history, window: { complete: true } }
            : path.startsWith('/api/tasks/') ? details[path.split('/').pop().split('?')[0]] || { status: 'running' }
                : { active_direct_turns: [] }) };
    });
    const handlers = new Map();
    let generation = 0;
    const instance = createChatInstance({
        ws: { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); }, isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set([7]), unreadCount: 0 },
        updateUnreadBadge() {}, chatId: 1, idPrefix: 'chat', mountEl: env.mount,
        stateSnapshots: { begin: () => ({ generation: ++generation, requestedAt: Date.now() }), gate() { return Promise.resolve(this.begin()); },
            isCurrent: () => true, apply() {} },
    });
    const messages = document.byId.get('chat-messages');
    const card = (id) => walkCard(messages, id);
    return {
        instance, messages, card, reads,
        part: (id, attr) => card(id)?.querySelector(`[data-${attr}]`),
        narrate: (taskId, content, seconds = 0) => handlers.get('chat')({ chat_id: 1, role: 'assistant', is_progress: true,
            narration: true, task_id: taskId, content, ts: `2026-10-10T12:00:${String(seconds).padStart(2, '0')}Z` }),
        frame: (row) => handlers.get('chat')({ chat_id: 1, ...row }),
        state: (data) => instance.hydrateStateSnapshot({ supervisor_ready: true, active_chat_activities_complete: true,
            active_chat_activities: [], ...data }),
        close() { instance.destroy(); restoreDom(env.prior); },
    };
}

const room = (extra = {}) => ({ activity_id: 'w-1', chat_id: 7, kind: 'managed_task', phase: 'working',
    started_at: Date.parse('2026-10-10T12:00:00Z') / 1000, ...extra });
const controls = (f, id) => (f.card(id)?.querySelector('.chat-live-actions')?.children || []).map((node) => node.textContent);

test('a bound card becomes its work\'s portal in place: footer reference, the room\'s present, Reply and Stop…', async () => {
    const f = fixture();
    try {
        f.narrate('w-1', 'Reading the logs before the audit.');
        const node = f.card('w-1');
        assert.ok(node, 'the work card is in Main');
        f.state({ task_bindings: { 'w-1': { project_id: 'p-1', chat_id: 7 } }, projects: [{ id: 'p-1', name: 'Audit' }],
            active_chat_activities: [room({ last_line: 'Checking the third project.', last_line_at: '2026-10-10T12:05:00Z',
                tool_calls: 41 })] });
        await settle();
        assert.equal(f.card('w-1'), node, 'the SAME node, never a second card or an entry');
        assert.equal(node.dataset.portal, '1');
        assert.equal(node.dataset.projectId, 'p-1');
        const footer = node.children.at(-1);
        assert.ok(footer.classList.contains('chat-live-bound-pointer'), 'the Project reference closes the card');
        assert.equal(footer.getAttribute('aria-label'), 'Open project Audit');
        assert.equal(f.part('w-1', 'live-activity').textContent, 'Checking the third project.', 'the room\'s latest line');
        assert.match(f.part('w-1', 'live-meta').innerHTML, /41 tool calls/);
        assert.match(f.part('w-1', 'live-meta').innerHTML, /updated /);
        assert.deepEqual(controls(f, 'w-1'), ['Reply', 'Stop…'], 'one action row: Reply first, Stop… while the room runs it');
        assert.equal(node.querySelector('[data-turn-into-project]'), null, 'a moved work no longer converts');
        assert.equal(messages(f).filter((child) => child.classList.contains('project-handoff')).length, 0);
    } finally { f.close(); }
});

const messages = (f) => f.messages.children;

test('an older census without the new facts leaves today\'s card: its own line, a start time, no invented count', async () => {
    const f = fixture();
    try {
        f.narrate('w-1', 'Reading the logs before the audit.');
        f.narrate('w-1', 'Comparing the two runs.', 1);
        const before = { title: f.part('w-1', 'live-title').textContent, line: f.part('w-1', 'live-activity').textContent };
        f.state({ task_bindings: { 'w-1': { project_id: 'p-1' } }, active_chat_activities: [room()] });
        await settle();
        const node = f.card('w-1');
        assert.equal(node.dataset.portal, '1');
        assert.deepEqual({ title: f.part('w-1', 'live-title').textContent, line: f.part('w-1', 'live-activity').textContent },
            before, 'its own title and last line stay as they were');
        const meta = f.part('w-1', 'live-meta').innerHTML;
        assert.doesNotMatch(meta, /tool call/, 'a count the host did not send is never shown');
        assert.match(meta, /(updated|started) /, 'an unfinished card always carries a time');
        assert.equal(node.children.at(-1).getAttribute('aria-label'), 'Open project Project', 'an unnamed Project reads Project');
    } finally { f.close(); }
});

test('a census row naming the work\'s Project binds it too; detached work says in background', async () => {
    const f = fixture();
    try {
        f.narrate('w-1', 'Started the audit.');
        f.state({ active_chat_activities: [room({ project_id: 'p-2', project_name: 'Second room', detached: true,
            last_line: 'Queued behind the build.' })] });
        await settle();
        const node = f.card('w-1');
        assert.equal(node.dataset.portal, '1');
        assert.equal(node.children.at(-1).getAttribute('aria-label'), 'Open project Second room');
        assert.equal(f.part('w-1', 'live-phase-secondary')?.textContent, 'in background');
    } finally { f.close(); }
});

test('a project_started row binds its work\'s card at the request\'s place instead of drawing an entry', async () => {
    const history = [
        { role: 'user', text: 'make the audit a project', ts: '2026-10-10T11:59:00Z', chat_id: 1, source: 'web',
            history_id: 'h-1', row_address: 'row:1@h#1' },
        { role: 'system', system_type: 'project_started', chat_id: 1, task_id: 'w-3', project_id: 'p-3',
            project_name: 'Audit room', task_name: 'Audit the projects', text: 'Audit room › Audit the projects · Started',
            ts: '2026-10-10T12:00:00Z', history_id: 'h-2' },
    ];
    const f = fixture({ history });
    try {
        await f.instance.refreshHistory({ revision: 1 });
        await settle();
        const node = f.card('w-3');
        assert.ok(node, 'the work has its card');
        assert.equal(node.dataset.portal, '1');
        assert.equal(f.part('w-3', 'live-title').textContent, 'Audit the projects', 'named by the row\'s work name');
        assert.equal(messages(f).filter((child) => child.dataset?.systemType === 'project_started').length, 0,
            'the row is data: no second entry');
    } finally { f.close(); }
});

test('the author\'s final settles the portal from its typed outcome; without one, the durable detail decides', async () => {
    const f = fixture({ details: { 'w-5': { task_id: 'w-5', status: 'failed', outcome_axes: { lifecycle: { status: 'failed' } } } } });
    try {
        for (const id of ['w-4', 'w-5']) f.narrate(id, 'Working on it.');
        f.state({ task_bindings: { 'w-4': { project_id: 'p-1' }, 'w-5': { project_id: 'p-1' } },
            active_chat_activities: [room({ activity_id: 'w-4' }), room({ activity_id: 'w-5' })] });
        await settle();
        f.frame({ role: 'system', system_type: 'project_completion_summary', task_id: 'w-4', project_id: 'p-1',
            project_name: 'Audit', content: 'All seven projects run again.', completion_answer: 'All seven projects run again.',
            outcome_axes: { lifecycle: { status: 'completed' }, execution: { status: 'ok' } }, status: 'completed',
            ts: '2026-10-10T13:00:00Z' });
        await settle();
        assert.equal(f.part('w-4', 'live-phase').textContent, 'Done', 'the typed outcome speaks');
        f.frame({ role: 'system', system_type: 'project_completion_summary', task_id: 'w-5', project_id: 'p-1',
            project_name: 'Audit', content: 'Stopped early.', status: 'completed', ts: '2026-10-10T13:01:00Z' });
        await settle();
        assert.ok(f.reads.includes('w-5'), 'the bare lifecycle word never decides: the durable detail is read');
    } finally { f.close(); }
});

test('Main only: a Project room\'s portals bind nothing and read nothing', () => {
    const records = new Map([['w-1', { groupId: 'w-1', root: { dataset: {}, isConnected: true } }]]);
    const calls = [];
    const portals = createWorkPortals({ isMain: false, feed: null, records, onDomWrite: (write) => (calls.push('write'), write()),
        card: new Proxy({}, { get: (_target, name) => () => calls.push(name) }) });
    assert.equal(portals.bind(records.get('w-1'), { id: 'p-1' }), false);
    portals.sync({ task_bindings: { 'w-1': { project_id: 'p-1' } }, active_chat_activities: [room()] });
    assert.equal(portals.bindRow({ task_id: 'w-1', project_id: 'p-1' }), false);
    assert.deepEqual(calls, [], 'the room shows the full card');
    assert.equal(records.get('w-1').root.dataset.portal, undefined);
});
