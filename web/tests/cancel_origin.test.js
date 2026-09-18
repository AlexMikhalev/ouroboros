import assert from 'node:assert/strict';
import test from 'node:test';

import { cardMetaKeys } from '../modules/chat_activity.js';
import { summarizeChatLiveEvent, taskReasonDetail, taskTerminalSummary } from '../modules/log_events.js';

test('cancelled root and replay name recorded transport without inventing an owner', () => {
    for (const [origin, expected] of [
        [{ source: 'http_single' }, 'Stopped from the app (Stop now) · initiator: not recorded'],
        [{ source: 'http_cascade', scope: 'cascade' }, 'Stopped from the app (Stop now) · this task and its sub-tasks · initiator: not recorded'],
        [{ source: 'http_graceful', requested_by: 'owner' }, 'Stopped from the app (Wrap up) · initiator: owner'],
        [{ source: 'agent_tool', requested_by: 'parent' }, 'agent_tool · initiator: parent'],
        [{ source: 'owner_restart', requested_by: 'owner' }, 'owner_restart · initiator: owner'],
        [{ source: '__proto__' }, '__proto__ · initiator: not recorded'],
    ]) {
        const record = { status: 'cancelled', cancel_origin: origin };
        assert.equal(taskTerminalSummary(record).body, expected);
        assert.equal(taskReasonDetail({ ...record, status: 'running', task_terminal_status: 'cancelled' }), expected);
    }
    assert.equal(taskReasonDetail({ status: 'cancelled' }), '');
    assert.equal(taskReasonDetail({ status: 'completed', cancel_origin: { source: 'http_single' } }), '');
});

test('a recorded foreign task actor survives the empty parent-decision trigger', () => {
    const origin = { source: 'agent_tool', request_origin: { kind: 'agent_task', task_id: 'foreign-root' } };
    assert.equal(taskReasonDetail({ status: 'cancelled', cancel_origin: origin }), 'agent_tool · initiator: foreign-root');
    assert.equal(taskReasonDetail({ status: 'cancelled', cancel_origin: {
        source: 'http_single', request_origin: { kind: 'http_client', source: 'http_single' },
    } }), 'Stopped from the app (Stop now) · initiator: not recorded');
});

test('the child wire carry and child summarizer keep cancellation visible and partial work inspectable', () => {
    const origin = { source: 'cascade_descendant', requested_by: 'root' };
    const carried = cardMetaKeys({ cancel_origin: origin });
    assert.deepEqual(carried.cancel_origin, origin);
    for (const frame of [
        { subagent_event: 'cancelled' },
        { subagent_event: 'completed', outcome_axes: { lifecycle: { status: 'cancelled' } } },
    ]) {
        const view = summarizeChatLiveEvent({
            type: 'send_message', is_progress: true, delegation_role: 'subagent',
            parent_task_id: 'root', subagent_task_id: 'child', result: 'Saved partial work',
            ...carried, ...frame,
        });
        assert.equal(view.phase, 'cancelled');
        assert.equal(view.body, 'cascade_descendant · initiator: root');
        assert.equal(view.activityPreview, view.body);
        assert.match(view.fullBody, /Saved partial work/);
        assert.match(view.fullBody, /cascade_descendant · initiator: root/);
    }
});

test('retained origin does not replace a non-cancelled child frame', () => {
    for (const subagent_event of ['running', 'failed']) {
        const view = summarizeChatLiveEvent({
            type: 'send_message', is_progress: true, delegation_role: 'subagent',
            parent_task_id: 'root', subagent_task_id: 'child', subagent_event,
            result: 'Saved partial work', error: subagent_event === 'failed' ? 'Worker failed' : '',
            status: 'cancelled', cancel_origin: { source: 'http_single' },
        });
        assert.doesNotMatch(view.body, /initiator:/);
        assert.equal(view.phase, subagent_event === 'failed' ? 'error' : 'working');
    }
});
