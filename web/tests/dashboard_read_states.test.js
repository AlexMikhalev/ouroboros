import assert from 'node:assert/strict';
import test from 'node:test';
import { initActivity } from '../modules/activity.js';
import { initLogs } from '../modules/logs.js';
import { initCosts } from '../modules/costs.js';
import { initDashboard } from '../modules/dashboard.js';

// DOM bookkeeping only: the production initializers, HTTP client, status helper,
// stream subscription and log deduplication all run unchanged. Layout/AT behavior
// remains a browser check; this fixture does not pretend to measure either.
class NodeStub {
    constructor(tag = 'div') {
        this.tagName = tag;
        this.children = [];
        this.attrs = {};
        this.dataset = {};
        this.events = new Map();
        this.scrollHeight = this.scrollTop = this.clientHeight = 0;
        this.hidden = this.disabled = false;
        this._text = '';
        this.classList = {
            contains: (name) => this.className.split(/\s+/).includes(name),
            toggle: (name, force) => {
                const names = new Set(this.className.split(/\s+/).filter(Boolean));
                const enabled = force ?? !names.has(name);
                if (enabled) names.add(name); else names.delete(name);
                this.className = [...names].join(' ');
                return enabled;
            },
            add: (name) => this.classList.toggle(name, true),
        };
    }
    get id() { return this.attrs.id || ''; }
    set id(value) { this.attrs.id = value; }
    get className() { return this.attrs.class || ''; }
    set className(value) { this.attrs.class = value; }
    setAttribute(key, value) {
        this.attrs[key] = String(value);
        if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
        if (key === 'disabled' || key === 'hidden') this[key] = true;
    }
    getAttribute(key) { return this.attrs[key] ?? null; }
    hasAttribute(key) { return key in this.attrs; }
    removeAttribute(key) { delete this.attrs[key]; }
    get firstElementChild() { return this.children[0]; }
    get nextElementSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) + 1]; }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set textContent(text) { this._text = String(text); this.children = []; }
    set innerHTML(html) {
        this.textContent = '';
        const stack = [this];
        for (const token of String(html).match(/<[^>]+>|[^<]+/g) || []) {
            if (token.startsWith('</')) { stack.pop(); continue; }
            if (!token.startsWith('<')) { stack.at(-1)._text += token; continue; }
            const tag = token.match(/^<([\w-]+)/)?.[1];
            if (!tag) continue;
            const node = new NodeStub(tag);
            for (const attr of token.slice(tag.length + 1, -1).matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
                node.setAttribute(attr[1], attr[2] ?? '');
            }
            stack.at(-1).appendChild(node);
            if (!['input', 'br', 'hr', 'img', 'meta', 'link'].includes(tag)) stack.push(node);
        }
    }
    appendChild(node) { node.remove(); node.parentElement = this; this.children.push(node); return node; }
    append(...nodes) { nodes.forEach((node) => this.appendChild(node)); }
    remove() {
        if (this.parentElement) this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1);
        this.parentElement = null;
    }
    contains(node) { return this === node || this.children.some((child) => child.contains(node)); }
    matches(selector) {
        if (selector.includes(',')) return selector.split(',').some((part) => this.matches(part.trim()));
        if (selector.includes('][')) return (selector.match(/\[[^\]]+\]/g) || []).every((part) => this.matches(part));
        if (selector.startsWith('#')) return this.id === selector.slice(1);
        if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
        const attr = selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
        return attr ? this.hasAttribute(attr[1]) && (attr[2] === undefined || this.getAttribute(attr[1]) === attr[2]) : this.tagName === selector;
    }
    querySelectorAll(selector) {
        return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
    focus() { globalThis.document.activeElement = this; }
    addEventListener(type, fn) {
        if (!this.events.has(type)) this.events.set(type, []);
        this.events.get(type).push(fn);
    }
    async fire(type, event = {}) { for (const fn of this.events.get(type) || []) await fn({ currentTarget: this, ...event }); }
    dispatchEvent(event) { for (const fn of this.events.get(event.type) || []) fn(event); }
    removeEventListener(type, fn) { this.events.set(type, (this.events.get(type) || []).filter((listener) => listener !== fn)); }
}

function setup(t) {
    const mount = new NodeStub();
    const document = {
        createElement: (tag) => new NodeStub(tag),
        getElementById: (id) => mount.querySelector(`#${id}`),
    };
    const window = new NodeStub();
    const routes = new Map();
    const calls = [];
    const wsHandlers = new Map();
    const ws = {
        on: (type, fn) => { wsHandlers.set(type, fn); },
        send() {},
        emit: (type, data = {}) => wsHandlers.get(type)?.(data),
    };
    const previous = Object.fromEntries(['document', 'window', 'fetch', 'requestAnimationFrame'].map((key) => [key, globalThis[key]]));
    Object.assign(globalThis, {
        document, window, requestAnimationFrame: (fn) => fn(),
        fetch: async (url) => {
            calls.push(url);
            if (!routes.has(url)) throw new Error(`Unexpected request ${url}`);
            const value = routes.get(url);
            if (typeof value === 'function') return value();
            if (value instanceof Error) throw value;
            return value;
        },
    });
    t.after(() => Object.assign(globalThis, previous));
    return { mount, window, ws, routes, calls };
}

const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
const queueUrl = '/api/tasks?queue_only=1';
const backgroundUrl = '/api/state';
const schedulesUrl = '/api/schedules';
const logUrl = (name) => `/api/logs/${name}?limit=150`;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const section = (mount, name) => mount.querySelector(`[data-activity-section="${name}"]`);
const message = (node) => node.querySelector('.ui-status').textContent;

function emptyActivity(routes) {
    routes.set(queueUrl, response({ queue: { running: [], pending: [] } }));
    routes.set(backgroundUrl, response({ bg_consciousness_enabled: false, active_chat_activities: [], active_chat_activities_complete: true }));
    routes.set(schedulesUrl, response({ tasks: [] }));
}

test('Activity failed reads stay unknown; independent successful empty state stays empty', async (t) => {
    const { mount, routes, ws } = setup(t);
    emptyActivity(routes);
    routes.set(queueUrl, response({ error: 'offline' }, 503));
    routes.set(backgroundUrl, new Error('offline'));
    const activity = initActivity({ mount, ws });
    await activity.refresh();
    assert.match(message(section(mount, 'queue')), /Could not load.*unknown/);
    assert.doesNotMatch(section(mount, 'queue').textContent, /Nothing running/);
    assert.match(message(section(mount, 'background')), /Could not load/);
    assert.equal(section(mount, 'background').querySelectorAll('button').length, 0, 'unknown must not invent Start');
    assert.match(section(mount, 'schedules').textContent, /No scheduled tasks/);
    assert.equal(message(section(mount, 'schedules')), '');
    emptyActivity(routes);
    await activity.refresh();
    assert.match(section(mount, 'queue').textContent, /Nothing running or queued/);
    assert.equal(message(section(mount, 'queue')), '');
    assert.equal(section(mount, 'background').querySelector('button').textContent, 'Start');
});

test('Activity keeps last-known data and actions on refresh failure, then reconciles recovery', async (t) => {
    const { mount, routes, ws } = setup(t);
    emptyActivity(routes);
    routes.set(schedulesUrl, response({ tasks: [{ id: 'scheduled', name: 'Daily report', enabled: true }] }));
    const activity = initActivity({ mount, ws });
    await activity.refresh();
    const scheduled = section(mount, 'schedules');
    routes.set(schedulesUrl, response({}, 503));
    await activity.refresh();
    assert.match(scheduled.textContent, /Daily report/);
    assert.match(message(scheduled), /Previously loaded.*unknown/);
    assert.ok(scheduled.querySelectorAll('button').every((button) => !button.disabled), 'failed refresh does not remove existing capability');
    assert.equal(section(mount, 'background').querySelector('button').disabled, false);
    routes.set(schedulesUrl, response({ tasks: [{ id: 'scheduled', name: 'Daily report', enabled: false }] }));
    await activity.refresh();
    assert.equal(message(scheduled), '');
    assert.equal(scheduled.querySelector('button').textContent, 'Enable');
    assert.equal(scheduled.querySelector('button').disabled, false);
});

test('Activity invalid success payload is unavailable and stale requests cannot replace newer state', async (t) => {
    const { mount, routes, ws } = setup(t);
    emptyActivity(routes);
    const deferred = [];
    routes.set(queueUrl, () => new Promise((resolve) => deferred.push(resolve)));
    const activity = initActivity({ mount, ws });
    const first = activity.refresh();
    const second = activity.refresh();
    deferred[1](response({ queue: { running: [{ id: 'current', task: { title: 'Current work' } }], pending: [] } }));
    await second;
    deferred[0](response({}, 503));
    await first;
    assert.equal(message(section(mount, 'queue')), '');
    assert.match(section(mount, 'queue').textContent, /Current work/);
    routes.set(queueUrl, response({ error: 'broken payload' }));
    await activity.refresh();
    assert.match(message(section(mount, 'queue')), /Could not refresh/);
    assert.match(section(mount, 'queue').textContent, /Current work/);
});

test('Activity unites live direct turns with queue identities and preserves queue controls', async (t) => {
    const { mount, routes, ws, calls } = setup(t);
    emptyActivity(routes);
    const now = Date.now;
    Date.now = () => 100000;
    t.after(() => { Date.now = now; });
    routes.set(backgroundUrl, response({ bg_consciousness_enabled: true,
        active_chat_activities_complete: false, active_chat_activities: [
            { activity_id: 'direct-1', kind: 'direct_chat', phase: 'thinking', started_at: 88 },
            { activity_id: 'queued-1', kind: 'direct_chat', phase: 'thinking', started_at: 80 },
            { activity_id: 'paused-1', kind: 'managed_task', phase: 'running', started_at: 80 },
        ] }));
    routes.set(queueUrl, response({ queue: {
        running: [{ id: 'queued-1', type: 'task', runtime_sec: 9, task: { title: 'Queue title' } }],
        pending: [{ task: { id: 'paused-1', title: 'Paused work', _budget_pause: true } }],
    } }));
    await initActivity({ mount, ws }).refresh();
    const queue = section(mount, 'queue');
    const rows = queue.querySelectorAll('.activity-row');
    assert.equal(rows.length, 3, 'partial positive census adds exactly the missing direct identity');
    assert.match(rows[0].textContent, /Queue title[\s\S]*running · task · 9s/);
    assert.match(rows[1].textContent, /Paused work[\s\S]*paused \(budget\)/);
    assert.equal(rows[1].querySelector('button').dataset.budgetPaused, '1');
    assert.match(rows[2].textContent, /Direct turn[\s\S]*thinking · 12s/);
    assert.equal(rows[2].querySelector('button').dataset.id, 'direct-1');
    assert.equal(rows[2].querySelector('button').dataset.act, 'task-control');
    assert.doesNotMatch(queue.textContent, /Nothing running/);
    assert.deepEqual(calls, [queueUrl, backgroundUrl, schedulesUrl], 'reuse the existing state read');
});

test('Activity complete empty census differs from unknown and failed state preserves queue facts', async (t) => {
    const { mount, routes, ws } = setup(t);
    emptyActivity(routes);
    const activity = initActivity({ mount, ws });
    const queue = section(mount, 'queue');
    await activity.refresh();
    assert.match(queue.textContent, /Nothing running or queued/);
    routes.set(backgroundUrl, response({ bg_consciousness_enabled: false,
        active_chat_activities: [], active_chat_activities_complete: false }));
    await activity.refresh();
    assert.match(queue.textContent, /Queue empty; live turns unknown/);
    routes.set(backgroundUrl, response({}, 503));
    await activity.refresh();
    assert.match(queue.textContent, /Queue empty; live turns unknown/);
    assert.equal(message(queue), '');
    routes.set(queueUrl, response({ queue: { running: [{ id: 'running-1', task: { title: 'Still working' } }], pending: [] } }));
    await activity.refresh();
    assert.match(queue.textContent, /Still working/);
    assert.equal(message(queue), '');
    routes.set(backgroundUrl, response({ bg_consciousness_enabled: false,
        active_chat_activities: [{ activity_id: 'direct-2', kind: 'direct_chat', phase: 'thinking' }],
        active_chat_activities_complete: true }));
    routes.set(queueUrl, response({}, 503));
    await activity.refresh();
    assert.match(message(queue), /Could not refresh.*unknown/);
    assert.match(queue.textContent, /Still working/);
    assert.doesNotMatch(queue.textContent, /Nothing running/);
});

test('Logs reports partial history without losing live rows or deduplication, then clears the gap on reconnect', async (t) => {
    const { mount, routes, ws, calls } = setup(t);
    const live = { type: 'test_live_event', ts: '2026-09-09T12:00:00Z', message: 'live' };
    const earlier = { type: 'test_earlier_event', ts: '2026-09-09T11:59:00Z', message: 'earlier' };
    for (const name of ['events', 'tools', 'progress', 'supervisor']) routes.set(logUrl(name), response({ entries: [] }));
    let finishEvents;
    routes.set(logUrl('events'), () => new Promise((resolve) => { finishEvents = resolve; }));
    routes.set(logUrl('tools'), response({}, 503));
    initLogs({ mount, ws, state: { activePage: 'dashboard', dashboardActiveSubtab: 'logs' } });
    ws.emit('log', { data: live });
    const entries = mount.querySelector('#log-entries');
    assert.equal(entries.children.length, 1, 'live delivery does not wait for backfill');
    finishEvents(response({ entries: [earlier, live] }));
    await settle();
    const status = mount.querySelector('.logs-history-status');
    assert.equal(entries.children.length, 2, 'backfill/live twin appears once');
    assert.match(status.textContent, /incomplete: tools could not be loaded/);
    assert.equal(status.dataset.tone, 'danger');
    assert.equal(status.hidden, false);
    await mount.querySelector('#btn-clear-logs').fire('click');
    assert.equal(entries.children.length, 0);
    assert.equal(status.hidden, false, 'Clear cannot hide missing-history evidence');
    routes.set(logUrl('events'), response({ entries: [earlier, live] }));
    routes.set(logUrl('tools'), response({ entries: [] }));
    ws.emit('open');
    await settle();
    assert.equal(status.hidden, true);
    assert.equal(status.textContent, '');
    assert.equal(entries.children.length, 0, 'existing exact dedupe survives Clear and reconnect');
    assert.equal(calls.length, 8, 'only existing init/reconnect backfill runs');
});

test('Logs catches unavailable and malformed sources while retaining later live events', async (t) => {
    const { mount, routes, ws } = setup(t);
    routes.set(logUrl('events'), response(null));
    routes.set(logUrl('tools'), new Error('offline'));
    routes.set(logUrl('progress'), response({}, 503));
    routes.set(logUrl('supervisor'), { ok: true, json: async () => { throw new Error('not JSON'); } });
    initLogs({ mount, ws, state: {} });
    await settle();
    assert.match(mount.querySelector('.logs-history-status').textContent, /events, tools, progress, supervisor/);
    ws.emit('log', { data: { type: 'test_after_failure', ts: '2026-09-09T13:00:00Z' } });
    assert.equal(mount.querySelector('#log-entries').children.length, 1);
});

test('an older failed backfill cannot overwrite a newer complete reconnect result', async (t) => {
    const { mount, routes, ws } = setup(t);
    const pending = [];
    routes.set(logUrl('events'), () => new Promise((resolve) => pending.push(resolve)));
    for (const name of ['tools', 'progress', 'supervisor']) routes.set(logUrl(name), response({ entries: [] }));
    initLogs({ mount, ws, state: {} });
    ws.emit('open');
    pending[1](response({ entries: [{ type: 'new_backfill', ts: '2026-09-09T14:00:00Z' }] }));
    await settle();
    const status = mount.querySelector('.logs-history-status');
    assert.equal(status.hidden, true);
    pending[0](response({}, 503));
    await settle();
    assert.equal(status.hidden, true);
    assert.equal(status.textContent, '');
    assert.equal(mount.querySelector('#log-entries').children.length, 1);
});

test('Costs numeric inputs have visible associated names and the task cap description', (t) => {
    const { mount } = setup(t);
    initCosts({ mount, state: {} });
    for (const [id, label] of [['s-budget', 'Total Budget ($)'], ['s-per-task-cost', 'Per-task Cost Cap ($)']]) {
        const field = mount.querySelector(`#${id}`);
        assert.ok(field);
        assert.equal(mount.querySelector(`[for="${id}"]`).textContent, label);
        assert.equal(field.classList.contains('ui-control'), true);
    }
    const input = mount.querySelector('#s-per-task-cost');
    const description = mount.querySelector(`#${input.getAttribute('aria-describedby')}`);
    assert.match(description.textContent, /whole root task tree/);
});

test('Dashboard binds stored, keyboard and programmatic selection to the same named panels', async (t) => {
    const { mount, window } = setup(t);
    const content = new NodeStub(); content.id = 'content'; mount.appendChild(content);
    const changes = [];
    window.addEventListener('ouro:dashboard-subtab-shown', (event) => changes.push(event.detail.tab));
    const state = { dashboardActiveSubtab: 'costs' };
    const dashboard = initDashboard({ state });
    const tabs = dashboard.page.querySelectorAll('[role="tab"]');
    const strip = dashboard.page.querySelector('[role="tablist"]');
    const assertSelected = (name) => {
        assert.equal(state.dashboardActiveSubtab, name);
        assert.deepEqual(tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true').map((tab) => tab.dataset.dashboardTab), [name]);
        assert.deepEqual(tabs.filter((tab) => tab.tabIndex === 0).map((tab) => tab.dataset.dashboardTab), [name]);
        for (const tab of tabs) {
            const panel = dashboard.page.querySelector(`#${tab.getAttribute('aria-controls')}`);
            assert.equal(panel.getAttribute('aria-labelledby'), tab.id);
            assert.equal(panel.hidden, tab.dataset.dashboardTab !== name);
            assert.equal(panel.classList.contains('active'), tab.dataset.dashboardTab === name);
        }
    };
    assertSelected('costs');
    assert.deepEqual(changes, ['costs']);
    const costs = dashboard.page.querySelector('#dashboard-tab-costs');
    await strip.fire('keydown', { target: costs, key: 'ArrowRight', preventDefault() {} });
    assertSelected('updates');
    assert.equal(document.activeElement.id, 'dashboard-tab-updates');
    assert.deepEqual(changes, ['costs', 'updates'], 'one key gesture causes one domain load');
    dashboard.activateTab('activity');
    assertSelected('activity');
    const activity = dashboard.page.querySelector('#dashboard-tab-activity');
    await strip.fire('keydown', { target: activity, key: 'Home', preventDefault() {} });
    assertSelected('logs');
    const count = changes.length;
    dashboard.activateTab('not-a-tab');
    assertSelected('logs');
    assert.equal(changes.length, count);
    dashboard.destroy();
    await strip.fire('click', { target: costs });
    assertSelected('logs');
});

test('Logs files a wake-up group under Consciousness with its label, from the origin fact on its frames', async (t) => {
    const { mount, routes, ws } = setup(t);
    for (const name of ['events', 'tools', 'progress', 'supervisor']) routes.set(logUrl(name), response({ entries: [] }));
    initLogs({ mount, ws, state: { activePage: 'dashboard', dashboardActiveSubtab: 'logs' } });
    ws.emit('log', { data: { type: 'tool_call_finished', tool: 'read_file', is_error: false, task_id: 'wake-1',
        initiator: 'consciousness', ts: '2026-09-16T12:00:00Z' } });
    ws.emit('log', { data: { type: 'tool_call_finished', tool: 'read_file', is_error: false, task_id: 'turn-2',
        ts: '2026-09-16T12:00:01Z' } });
    // A later frame of the same wake without the stamp (a supervisor-rebuilt
    // row) keeps the group's label: the fact is sticky once seen.
    ws.emit('log', { data: { type: 'task_heartbeat', task_id: 'wake-1', phase: 'running', ts: '2026-09-16T12:00:02Z' } });
    await settle();
    const entries = mount.querySelector('#log-entries');
    const card = (id) => entries.children.find((node) => node.dataset.taskGroup === id);
    assert.equal(card('wake-1').dataset.category, 'consciousness');
    assert.equal(card('wake-1').querySelector('[data-task-kind]').textContent, 'Consciousness');
    assert.equal(card('turn-2').dataset.category, 'tasks');
    assert.equal(card('turn-2').querySelector('[data-task-kind]').textContent, 'task turn-2');
});
