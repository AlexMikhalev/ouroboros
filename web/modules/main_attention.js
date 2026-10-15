// Current Main decisions are projections of canonical questions and task controls.
// This host survives bounded transcript eviction; it owns no polling or saved state.
import { createModelWaitController } from './model_wait.js';
import { projectReference, nameProjectReference } from './project_reference.js';
import { createTaskResumeButton, openTaskControlMenu, requestStop, hurryTaskAction,
    resumeTaskAction, ACTION_RESUME, ACTION_HURRY, TASK_CONTROL_TRIGGER_LABEL } from './task_control_menu.js';
import { syncParkedPhase } from './task_phase_chip.js';
import { showToast } from './toast.js';
import { fmt, tr, tx } from './i18n.js';

export function createMainAttention({ mount, openSettings, refresh = () => {}, mutate = fn => fn(),
    syncQuestions = () => {}, connectQuestions = () => {} }) {
    const host = document.createElement('details');
    host.className = 'main-attention';
    host.hidden = true;
    const summary = document.createElement('summary');
    summary.className = 'main-attention-summary';
    const body = document.createElement('div');
    body.className = 'main-attention-body';
    const questions = document.createElement('div');
    questions.className = 'main-attention-questions';
    questions.setAttribute('data-i18n-authored', '1');
    const actions = document.createElement('div');
    actions.className = 'main-attention-actions';
    // The same source-translated renderers live inside transcript exclusions in
    // Project. Keep author prose, filenames and account names out of the overlay here too.
    actions.setAttribute('data-i18n-authored', '1');
    const availability = document.createElement('div');
    availability.className = 'msg-provenance';
    availability.hidden = true;
    body.append(availability, questions, actions);
    host.append(summary, body);
    mount.insertBefore(host, mount.firstElementChild || null);
    const records = new Map();
    let connected = true;
    let unavailable = false;
    let unrouted = 0;  // questions whose original chat no import could prove: a lasting gap, not a refresh failure
    const waits = createModelWaitController({ getRecord: id => records.get(id), openSettings,
        onDomWrite: mutate, includeWait: (id, row) => Boolean(records.get(id)?.row.model_waits?.[row.wait_id]) });

    function update() {
        const count = questions.children.length + records.size;
        summary.textContent = `${tr('main.needs_you', 'Needs you')}${count ? ` · ${count}` : ''}`;
        host.hidden = !count && !unavailable && !unrouted;
        availability.hidden = !unavailable && !unrouted;
        availability.textContent = unavailable
            ? tr('main.attention_unavailable', 'Current questions and actions could not be refreshed. Previously loaded items are retained.')
            : fmt(unrouted === 1 ? "{n} earlier question can't be shown: its original chat is unknown."
                : "{n} earlier questions can't be shown: their original chat is unknown.", { n: unrouted });
    }

    function recordFor(row) {
        const id = String(row.task_id || row.activity_id || '');
        let record = records.get(id);
        if (record) return record;
        const root = document.createElement('section');
        root.className = 'main-attention-task';
        root.dataset.taskId = id;
        const heading = document.createElement('div');
        heading.className = 'model-wait-heading';
        const phaseEl = document.createElement('span');
        const title = document.createElement('strong');
        const reason = document.createElement('div');
        reason.className = 'main-attention-reason';
        const controls = document.createElement('div');
        controls.className = 'model-wait-actions';
        const timelineEl = document.createElement('div');
        timelineEl.hidden = true;
        heading.append(title, phaseEl);
        root.append(heading, reason, controls, timelineEl);
        const reference = row.project_id ? projectReference({ id: row.project_id, name: row.project_name }, { taskId: id }) : null;
        if (reference) controls.append(reference);
        record = { groupId: id, root, phaseEl, title, reason, controls, timelineEl, reference,
            phaseSecondaryEl: null, row, available: true, finished: false };
        records.set(id, record);
        actions.append(root);
        return record;
    }

    const canResume = record => connected && record.available && records.get(record.groupId) === record
        && record.row.can_resume === true
        && record.row.pause_cause !== 'owner';

    function paint(record, row) {
        record.row = row;
        record.title.textContent = row.task_name || row.title || row.project_name || 'Task';
        record.reason.textContent = tx(({ budget: 'The budget limit paused this task. Change the limit if needed, then Resume.',
            restart: 'This task needs Resume after the restart.', resume: 'This task needs an explicit Resume.',
            model_access: 'This task needs your help to restore model access.' })[row.attention_reason]
            || row.reason || row.project_admission_hold?.label || '');
        if (record.reference) nameProjectReference(record.reference, { id: row.project_id, name: row.project_name });
        syncParkedPhase(record, connected ? row.phase : 'unknown', row);
        if (canResume(record) && !record.resume) {
            record.resume = createTaskResumeButton(record.groupId, { canResume: () => canResume(record), onSettled: refresh });
            record.controls.append(record.resume);
        }
        if (record.resume) record.resume.disabled = !canResume(record);
        if (row.cancelable === true && !record.stop) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-xs btn-default';
            button.textContent = tx(TASK_CONTROL_TRIGGER_LABEL);
            button.addEventListener('click', () => {
                if (!connected || !record.available || record.row.cancelable !== true) return;
                openTaskControlMenu(button, { budgetPaused: record.row.phase === 'budget_paused',
                    resumeVisible: canResume(record), onAction: async action => {
                        if (!connected || !record.available || record.row.cancelable !== true) return;
                        try {
                            if (action === ACTION_RESUME) await resumeTaskAction(record.groupId);
                            else if (action === ACTION_HURRY) await hurryTaskAction(record.groupId);
                            else await requestStop(record.groupId, action);
                            await refresh();
                        } catch (error) { showToast(`Task control not confirmed: ${error.message || error}`, 'error'); }
                    } });
            });
            record.stop = button;
            record.controls.append(button);
        }
        if (record.stop) record.stop.disabled = !connected || !record.available || row.cancelable !== true;
        waits.observe(record.groupId, row);
        waits.refresh(record.groupId);
        const waitHost = record.root.querySelector('.model-waits');
        if (waitHost) waitHost.inert = !connected || !record.available;
    }

    return { questions,
        update,
        // One state snapshot feeds both halves: the decision owner's canonical questions first, then
        // task controls. `null` is a failed read: nothing current is known, so both become incomplete.
        snapshot(data, requestedAt = Infinity) {
            // HTTP can answer while the socket is down, but frames sent after that body are lost until
            // the reopen, so it restores nothing: only a census the reopen sequences (app.js retires
            // reads begun before it) brings back questions and controls; the disconnect kept the rest.
            if (!connected) return;
            syncQuestions(data ? data.current_questions : [], data?.current_questions_complete === true, requestedAt);
            const rows = Array.isArray(data?.main_attention) ? data.main_attention : [];
            const complete = data?.main_attention_complete === true;
            const gaps = Array.isArray(data?.current_questions_gaps) ? data.current_questions_gaps : [];
            unrouted = gaps.filter(gap => gap?.reason === 'publication_address_unknown').length;
            unavailable = (data?.current_questions_complete !== true && (!gaps.length || gaps.length > unrouted))
                || data?.main_attention_complete !== true;
            mutate(() => {
                const current = new Set();
                for (const row of rows) {
                    const id = String(row.task_id || row.activity_id || '');
                    if (!id) continue;
                    current.add(id);
                    const record = recordFor(row);
                    record.available = true;
                    paint(record, row);
                }
                for (const [id, record] of records) if (!current.has(id)) {
                    if (complete) { waits.forget(id); record.root.remove(); records.delete(id); }
                    else { record.available = false; paint(record, record.row); }
                }
                update();
                return true;
            });
        },
        setConnected(value) {
            connected = value;
            if (!value) {
                unavailable = true;
                for (const record of records.values()) record.available = false;
            }
            mutate(() => { for (const record of records.values()) paint(record, record.row); update(); return true; });
            connectQuestions(value);  // the question owner withdraws its forms and voids its reads in flight
        },
        destroy() { waits.destroy(); records.clear(); host.remove(); },
    };
}
