// The pager's frozen continuation and the read receipt's newest admitted target
// may belong to different snapshots. Exercise their real createChatInstance seam.
import assert from 'node:assert/strict';
import test from 'node:test';
import { ElementStub } from './chat_dom_fixture.js';
import { row, room, until, quiet } from './project_read_state_fixture.js';

const note = n => ({ ...row(n), role: 'user', text: `owner note ${n}` });
const coverage = (upper, from) => ({ v: 1, view: 'room', upper: { chat: upper, progress: 0 },
    spans: { chat: { from, to: upper, chain: 'chat-file', gaps: [] },
        progress: { from: 0, to: 0, chain: 'empty', gaps: [] } } });
const named = id => ({ complete: false, truncated_by: ['quota'],
    latest_message: { history_id: `chat:${id}`, out_of_order: false } });
const snapshot = (name, upper, target) => {
    const rows = [note(upper - 80), note(upper - 70)];
    const pages = Array.from({ length: 6 }, (_, i) => ({
        messages: i === 5 ? [row(target)] : [note(upper - 200 - i * 100)],
        coverage: coverage(upper, i === 5 ? 0 : upper - 200 - i * 100),
        window: {},
    }));
    return { name, recent: { messages: rows, window: named(target), coverage: coverage(upper, upper - 100) }, pages };
};

function raceRoom(t) {
    const snapshots = { old: snapshot('old', 1000, 9), fresh: snapshot('fresh', 2000, 1100) };
    const server = { current: snapshots.old, recentGates: [], pageGates: new Map(), reads: [], pages: [] };
    const deferred = () => { let release; const promise = new Promise(resolve => { release = resolve; }); return { promise, release }; };
    const holdPage = key => { const gate = deferred(); server.pageGates.set(key, gate.promise); return gate.release; };
    const holdRecent = () => { const gate = deferred(); server.recentGates.push(gate.promise); return gate.release; };
    let arrivals = 0;
    const r = room(t, { projectId: 'snapshot-race', onReadingLatest: () => { arrivals += 1; }, fetchHistory: async url => {
        const cursor = new URL(url, 'http://local').searchParams.get('cursor');
        let data, gate;
        if (cursor) {
            const [name, number] = cursor.split(':'), index = Number(number);
            const frozen = snapshots[name];
            assert.ok(frozen, `cursor belongs to an immutable snapshot: ${cursor}`);
            server.pages.push(cursor);
            data = { ...frozen.pages[index - 1], page_cursor: cursor,
                next_cursor: index < frozen.pages.length ? `${name}:${index + 1}` : null,
                has_more: index < frozen.pages.length };
            gate = server.pageGates.get(cursor);
        } else {
            const fresh = server.current;
            server.reads.push(fresh.name);
            data = { ...fresh.recent, page_cursor: `${fresh.name}:0`, next_cursor: `${fresh.name}:1`, has_more: true };
            gate = server.recentGates.shift();
        }
        await gate;
        return { ok: true, json: async () => data };
    } });
    const rect = ElementStub.prototype.getBoundingClientRect;
    ElementStub.prototype.getBoundingClientRect = function () {
        const id = this.dataset?.historyId;
        if (Object.values(snapshots).some(frozen => frozen.recent.window.latest_message.history_id === id)) {
            return r.box(-r.messages.scrollTop, 20 - r.messages.scrollTop);
        }
        if (id) return r.box(-500, -480); // walked pages are off screen and can be evicted
        return rect.call(this);
    };
    t.after(() => { ElementStub.prototype.getBoundingClientRect = rect; });
    const open = () => {
        void r.instance.showLatest({ arrive: true });
        r.instance.cancelHistoryPaint();
        return r.instance.refreshHistory({ revision: 4 });
    };
    const newer = async () => {
        server.current = snapshots.fresh;
        const receipt = await r.instance.refreshHistory({ revision: 5 });
        assert.equal(receipt.read, false, 'the newly named target is offpage, so its revision cannot be read');
    };
    return { r, server, snapshots, holdPage, holdRecent, open, newer, arrivals: () => arrivals,
        trace: () => ({ reads: server.reads, pages: server.pages, shown: r.shown(),
            scrollTop: r.messages.scrollTop, arrivals }) };
}

test('an opening rebases its frozen cursor when a later recent read names a different offpage target', async t => {
    const f = raceRoom(t), releaseOld = f.holdPage('old:5'), releaseFresh = f.holdPage('fresh:2');
    await f.open();
    await until(() => f.server.pages.includes('old:5'));
    assert.ok(!f.r.shown().includes('chat:800'), 'the walk has passed pages the pager keeps');
    await f.newer();
    releaseOld();
    await quiet();
    t.diagnostic(JSON.stringify(f.trace()));
    assert.ok(f.server.pages.includes('fresh:2'), 'the opening must leave the old frozen snapshot and reach the admitted target');
    assert.deepEqual(f.server.pages, ['old:1', 'old:2', 'old:3', 'old:4', 'old:5', 'fresh:1', 'fresh:2'],
        'no stale old-chain continuation after the in-flight page settles');
    // More traffic can grow the room without replacing the named target.
    f.server.current = f.snapshots.poll = snapshot('poll', 3000, 1100);
    await f.r.instance.refreshHistory({ revision: 6 });
    releaseFresh();
    await until(() => f.r.shown().includes('chat:1100') && f.r.messages.scrollTop === 0);
    assert.deepEqual(f.server.pages, ['old:1', 'old:2', 'old:3', 'old:4', 'old:5', ...[1, 2, 3, 4, 5, 6].map(i => `fresh:${i}`)]);
    assert.deepEqual(f.server.reads, ['old', 'fresh', 'fresh', 'poll'], 'one rebase; a same-target poll does not restart the walk');
    assert.equal(f.arrivals(), 1, 'the owned opening lands once');
    assert.equal((await f.r.instance.refreshHistory({ revision: 6 })).read, true, 'only the visible target can be read');
    const pages = [...f.server.pages], reads = f.server.reads.length;
    f.r.readLatest();
    const top = f.r.messages.scrollTop;
    f.server.current = f.snapshots.later = snapshot('later', 4000, 3999);
    assert.equal((await f.r.instance.refreshHistory({ revision: 7 })).read, false);
    await quiet();
    assert.deepEqual(f.server.pages, pages, 'a later arrival inherits no opening and starts no page or return loop');
    assert.equal(f.server.reads.length, reads + 1);
    assert.equal(f.r.messages.scrollTop, top, 'a later arrival cannot jump the reader');
    t.diagnostic(JSON.stringify(f.trace()));
});

test('a rebase response superseded in flight cannot lend its cursor to the newest admitted target', async t => {
    const f = raceRoom(t), releaseOld = f.holdPage('old:5');
    await f.open();
    await until(() => f.server.pages.includes('old:5'));
    await f.newer();
    const releaseRebase = f.holdRecent();
    releaseOld();
    await quiet();
    assert.deepEqual(f.server.reads, ['old', 'fresh', 'fresh'], 'the rebase is in flight on fresh snapshot');
    f.server.current = f.snapshots.newest = snapshot('newest', 3000, 2100);
    assert.equal((await f.r.instance.refreshHistory({ revision: 6 })).read, false, 'an even newer target is admitted offpage');
    releaseRebase();
    await until(() => f.r.shown().includes('chat:2100') && f.r.messages.scrollTop === 0);
    assert.deepEqual(f.server.reads, ['old', 'fresh', 'fresh', 'newest', 'newest'], 'the still-owned opening rebases for each changed target');
    assert.deepEqual(f.server.pages, ['old:1', 'old:2', 'old:3', 'old:4', 'old:5', ...[1, 2, 3, 4, 5, 6].map(i => `newest:${i}`)],
        'neither superseded chain can continue after the latest target is admitted');
    assert.equal(f.arrivals(), 1);
    assert.equal((await f.r.instance.refreshHistory({ revision: 6 })).read, true);
    t.diagnostic(JSON.stringify(f.trace()));
});

for (const cancel of ['gesture', 'question', 'hidden']) {
    test(`a ${cancel} during snapshot rebasing ends the same opening before any new-chain page`, async t => {
        const f = raceRoom(t), releaseOld = f.holdPage('old:5');
        await f.open();
        await until(() => f.server.pages.includes('old:5'));
        await f.newer();
        const releaseRebase = f.holdRecent();
        releaseOld();
        await quiet();
        assert.equal(f.server.reads.length, 3, 'the next recent read is the pending snapshot rebase');
        if (cancel === 'gesture') {
            for (const handler of f.r.messages.listeners.get('wheel') || []) {
                handler({ type: 'wheel', deltaY: 1, target: f.r.messages, timeStamp: 0 });
            }
        } else if (cancel === 'question') {
            globalThis.document.body = globalThis.document.createElement('body');
            void f.r.instance.revealQuestion('explicit-task', 'explicit-question');
        } else f.r.instance.page.hidden = true;
        const top = f.r.messages.scrollTop;
        releaseRebase();
        await quiet();
        assert.deepEqual(f.server.pages, ['old:1', 'old:2', 'old:3', 'old:4', 'old:5'], 'neither frozen chain continues after cancellation');
        assert.equal(f.arrivals(), 0);
        assert.equal(f.r.messages.scrollTop, top);
        f.r.instance.page.hidden = false;
        assert.equal((await f.r.instance.refreshHistory({ revision: 6 })).read, false);
        await quiet();
        assert.deepEqual(f.server.pages, ['old:1', 'old:2', 'old:3', 'old:4', 'old:5'], 'showing or refreshing later cannot revive the spent opening');
        assert.equal(f.r.messages.scrollTop, top);
        t.diagnostic(JSON.stringify(f.trace()));
    });
}
