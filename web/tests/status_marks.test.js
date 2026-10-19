// Status marks (docs/DESIGN.md "Status marks"): one mapping from facts to a mark, one painter.
import test from 'node:test';
import assert from 'node:assert/strict';

import { MARK_HUES, MARK_SHAPES, markView, paintStatusMark, statusMarkHtml } from '../modules/status_mark.js';
import { summarizeRoom } from '../modules/project_activity.js';
import { roomStatusView } from '../modules/work_status.js';
import { room } from './work_fixture.js';

test('the owner move outranks everything and stands alone as the amber dot', () => {
    for (const mark of ['moving', 'still', 'pause', 'ring', 'none']) {
        assert.deepEqual(markView({ mark, owner: true, unread: true, outcome: 'done' }), { shape: 'dot', hue: 'owner' });
    }
});

test('new messages tint the work shape, and are a crimson dot where nothing works', () => {
    assert.deepEqual(markView({ mark: 'moving', unread: true }), { shape: 'moving', hue: 'unread' });
    assert.deepEqual(markView({ mark: 'pause', unread: true }), { shape: 'pause', hue: 'unread' });
    assert.deepEqual(markView({ mark: 'still', unread: true }), { shape: 'still', hue: 'unread' });
    assert.deepEqual(markView({ mark: 'ring', unread: true }), { shape: 'ring', hue: 'unread' });
    assert.deepEqual(markView({ mark: 'none', unread: true }), { shape: 'dot', hue: 'unread' });
});

test('without anything for the owner the shape takes the surface ink, and rest draws nothing', () => {
    for (const mark of ['moving', 'still', 'pause', 'ring']) assert.deepEqual(markView({ mark }), { shape: mark, hue: 'ink' });
    assert.deepEqual(markView({ mark: 'none' }), { shape: 'none', hue: 'ink' });
    assert.deepEqual(markView({}), { shape: 'none', hue: 'ink' });
    // An unknown or legacy token is never drawn as something it is not.
    assert.deepEqual(markView({ mark: 'dot' }), { shape: 'none', hue: 'ink' });
    assert.deepEqual(markView({ mark: 'sparkle' }), { shape: 'none', hue: 'ink' });
});

test('an outcome beside its word is a dot in its tone: Done with warnings succeeds and Stopped is neutral', () => {
    assert.deepEqual(markView({ outcome: 'done' }), { shape: 'dot', hue: 'ok' });
    assert.deepEqual(markView({ outcome: 'warn' }), { shape: 'dot', hue: 'ok' });
    assert.deepEqual(markView({ outcome: 'cancelled' }), { shape: 'dot', hue: 'neutral' });
    for (const outcome of ['error', 'timeout', 'lifecycle_error']) assert.deepEqual(markView({ outcome }), { shape: 'dot', hue: 'failed' });
});

test('every view the mapping returns stays inside the grammar', () => {
    for (const mark of [...MARK_SHAPES, 'x']) for (const owner of [false, true]) for (const unread of [false, true]) {
        for (const outcome of ['', 'done', 'cancelled', 'error', 'x']) {
            const view = markView({ mark, owner, unread, outcome });
            assert.ok(MARK_SHAPES.includes(view.shape) && MARK_HUES.includes(view.hue), JSON.stringify({ mark, view }));
        }
    }
});

test('the painter writes only a change and hides the mark at rest', () => {
    const node = { dataset: { shape: 'none', hue: 'ink' }, hidden: true };
    assert.equal(paintStatusMark(node, { shape: 'moving', hue: 'unread' }), true);
    assert.deepEqual([node.dataset.shape, node.dataset.hue, node.hidden], ['moving', 'unread', false]);
    assert.equal(paintStatusMark(node, { shape: 'moving', hue: 'unread' }), false);
    assert.equal(paintStatusMark(node, { shape: 'none' }), true);
    assert.equal(node.hidden, true);
    assert.equal(paintStatusMark(null, { shape: 'dot' }), false);
});

test('the markup form is the same decorative anatomy', () => {
    const html = statusMarkHtml('nav-mark');
    assert.match(html, /^<span class="status-mark nav-mark" aria-hidden="true" data-shape="none" data-hue="ink" hidden>/);
    assert.equal((html.match(/<i><\/i>/g) || []).length, 3);
    // A template that knows its mark paints it at once; a view outside the grammar draws nothing.
    assert.match(statusMarkHtml('', { shape: 'moving', hue: 'ink' }),
        /^<span class="status-mark" aria-hidden="true" data-shape="moving" data-hue="ink"><i>/);
    assert.match(statusMarkHtml('', { shape: 'sparkle', hue: 'gold' }), /data-shape="none" data-hue="ink" hidden>/);
});

test('the owner move a server room lists reads the same on the bar and the sidebar, an unconfirmed room included', () => {
    const turn = [{ kind: 'question', quiz_id: 'q-1', task_id: 't-1' }];
    for (const activity of ['working', 'waiting', 'paused', 'unknown']) {
        const listed = room(activity, { turn });
        const bar = roomStatusView({ room: listed, works: [] });
        const row = summarizeRoom(listed);
        assert.deepEqual(markView({ mark: bar.mark, owner: bar.owner }), { shape: 'dot', hue: 'owner' }, activity);
        assert.deepEqual(markView({ mark: row.mark, owner: row.owner }), { shape: 'dot', hue: 'owner' }, activity);
        const quiet = room(activity);
        assert.equal(roomStatusView({ room: quiet, works: [] }).owner, false, activity);
        assert.equal(summarizeRoom(quiet).owner, false, activity);
    }
});
