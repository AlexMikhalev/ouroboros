import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeHistoricalTimelineItem, compareHistoryPosition } from '../modules/chat_history_replay.js';
import { createChatHistoryPager } from '../modules/chat_history.js';

test('equal-time identical narration retains physical identities in source order', () => {
    const record = { items: [], finished: true };
    const summary = { visible: true, headline: 'Identical narration', phase: 'working', dedupeKey: 'same' };
    const row = offset => ({ history_id: `progress:${offset}`,
        history_position: { source: 'progress', offset }, ts: '2026-09-12T12:00:00Z' });
    for (const offset of [20, 10, 30, 10]) mergeHistoricalTimelineItem(record, summary, row(offset), '12:00');
    assert.deepEqual(record.items.map(item => item.historyId), ['progress:10', 'progress:20', 'progress:30']);
    assert.equal(record.finished, true);
    assert.equal(compareHistoryPosition(row(10).history_position, row(20).history_position), -10);
});

test('a richer terminal updates its existing line while older terminal content cannot replace it', () => {
    const record = { items: [] };
    const summary = { visible: true, terminal: true, phase: 'done', headline: 'Done', dedupeKey: 'task_done|task' };
    const row = (offset, second) => ({ history_id: `chat:${offset}`,
        history_position: { source: 'chat', offset }, ts: `2026-09-12T12:00:0${second}Z` });
    mergeHistoricalTimelineItem(record, summary, row(10, 1), '12:00:01');
    const item = record.items[0];
    mergeHistoricalTimelineItem(record, { ...summary, body: 'Retained result details' }, row(20, 2), '12:00:02');
    assert.equal(record.items[0], item);
    assert.equal(item.body, 'Retained result details');
    mergeHistoricalTimelineItem(record, { ...summary, body: 'Older incomplete details' }, row(5, 0), '12:00:00');
    assert.equal(item.body, 'Retained result details');
});

test('narration and current terminal projection of one source have distinct DOM keys', () => {
    const record = { items: [] };
    const row = { history_id: 'progress:100', ts: '2026-09-12T12:00:00Z' };
    mergeHistoricalTimelineItem(record, { visible: true, headline: 'Narration' }, row, '12:00');
    mergeHistoricalTimelineItem(record, { visible: true, terminal: true, headline: 'Done', dedupeKey: 'task_done|t' }, row, '12:00');
    assert.equal(new Set(record.items.map(item => item.lineKey)).size, 2);
    assert.equal(record.items.filter(item => item.historyId).length, 1);
});

test('reopening a deep window fetches its exact page and retains newer navigation without cached bodies', async () => {
    const response = index => ({ messages: [{ history_id: `chat:${index}` }], has_more: index < 4,
        next_cursor: index < 4 ? `older-${index + 1}` : null, page_cursor: `page-${index}` });
    const create = (calls, applied) => createChatHistoryPager({ maxPages: 1,
        fetchPage: async cursor => { calls.push(cursor); return response(Number(cursor.split('-')[1])); },
        applyPage: (messages, page) => applied.push(page.index), releasePage() {},
    });
    const first = create([], []);
    first.acceptRecent(response(0));
    await first.older(); await first.older();
    const saved = first.exportResume();
    assert.equal(JSON.stringify(saved).includes('messages'), false);
    first.destroy();
    const calls = [], applied = [], reopened = create(calls, applied);
    await reopened.restore(saved);
    assert.deepEqual(calls, ['page-2']);
    assert.equal(reopened.getState().canNewer, true);
    await reopened.newer();
    assert.deepEqual(calls, ['page-2', 'page-1']);
    assert.deepEqual(applied, [2, 1]);
    reopened.destroy();
});
