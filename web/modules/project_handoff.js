/** Compact Project work entries; event identity and observed work stay separate. */
import { projectReference } from './project_reference.js';
import { activeModelWaits, mergeModelWaits } from './model_wait.js';
import { isTerminalTaskDetail } from './log_events.js';
import { censusTaskPhase, paintTaskPhase } from './task_phase_chip.js';
import { formatMsgTime, selectionInside } from './chat_activity.js';

// A press that travelled farther than this is a drag, even when it selected nothing.
const PRESS_SLOP = 6;
const CONTROL = 'a[href], button, input, select, textarea, summary, label, [contenteditable="true"], [role="button"], [tabindex]';
const PATH = ['pointermove', 'pointerup', 'pointercancel'];

// An entry's free area: the entry itself, or a part of it that is neither a control nor text.
function isFreeArea(target, entry) {
    const control = target?.closest?.(CONTROL);
    if (control && control !== entry && entry.contains(control)) return false;
    return target === entry || ![...(target?.childNodes || [])].some(node => node.nodeType === 3 && node.nodeValue.trim());
}

/**
 * Whether a pointer click lands on an entry's free area, the one place that stands in for
 * its Project reference (DESIGN "Project work entries"). Text (title, chips, time) stays
 * reading — selectable, and a double click selects a word — and a control keeps its own
 * press. A drag, a selection in the entry that this press made or changed, a repeated or
 * modified click never navigates; a repeat cannot undo the first click, so only the first
 * one counts. `press` is the pointerdown, armed only on free area: where, the farthest its
 * pointer has since travelled from there, and the selection it found.
 */
export function isFreePress(event, entry, press, selection) {
    const target = event?.target;
    if (!entry || !target || !press || press.entry !== entry || event.defaultPrevented || event.button !== 0
        || event.detail !== 1 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return false;
    if (!isFreeArea(target, entry)) return false;
    // The farthest point decides, not where the pointer came back to; an unmeasured path is no press.
    if (!(Math.max(press.travel, Math.hypot(event.clientX - press.x, event.clientY - press.y)) <= PRESS_SLOP)) return false;
    // A click inside an older selection keeps it; only a selection this press made is the reader's.
    return !selectionInside(entry, selection) || String(selection) === press.selected;
}

// The gateway's typed receipt word (ouroboros/project_handoff.py RECEIPT_STATES).
// The binding is committed under every word; only these two prove that the Main
// history row is owed or already delivered.
export const DURABLE_RECEIPTS = new Set(['durable', 'already_delivered']);

/** Owner wording for a non-durable receipt; '' when nothing needs saying. */
export function receiptNotice(status) {
    if (DURABLE_RECEIPTS.has(status)) return '';
    return {
        unregistered: 'Project binding saved; the Main history receipt was sent but is not protected against a restart.',
        unavailable: 'Project binding saved; the Main history receipt could not be sent.',
        origin_unproven: 'Project binding saved; this conversation has no recorded Main origin, so no history receipt was written.',
    }[status] || 'Project binding saved; the Main history receipt is unconfirmed.';
}

export function handoffPhase(activity, detail, connected = true) {
    const waits = mergeModelWaits({}, activity?.model_waits);
    return censusTaskPhase(activity, detail, connected,
        activeModelWaits(waits, false, activity?.task_attempt || 0).length > 0);
}

// Transfer anchors share their recorded handoff identity: a converted live card
// (`card`, at the request's own position; several roots of ONE owner message may
// each convert, and every one of them stays a visible card — a card is never a
// shadow) and the durable receipt row (`receipt`, at most one visible per
// identity). A receipt is folded under the first visible card of its identity, or
// under the earlier receipt, and every folded node stays in `shadows` so an
// evicted anchor hands over instead of dropping the transfer from the feed.
// A creation entry retains its own identity and subject. Matching transfers
// fold it visually; they never turn creation into a transfer or lend it status.
export function createProjectHandoffs({ feed, fetchDetail, mutate, attachCopy }) {
    const rows = new Map();  // Converted task, transfer receipt, or creation entry.
    let connected = true, destroyed = false, complete = false;
    let activities = new Map();
    const inFeed = node => feed.contains(node);
    // One listener pair on the feed serves every entry, so a remount or an evicted
    // node leaves nothing behind; the press goes to the reference the entry holds NOW.
    // Only a pointer pressed on free area arms it. Until that pointer lifts, the document
    // follows it wherever it goes and keeps one number, the farthest it got; its cancel,
    // a second pointer or destroy disarms the press.
    let press = null, tracing = false;
    const entryOf = target => {
        const entry = target?.closest?.('.project-handoff');
        return entry && inFeed(entry) ? entry : null;
    };
    const onPath = event => {
        if (!press || event.pointerId !== press.id) return;
        if (event.type === 'pointercancel') press = null;
        else press.travel = Math.max(press.travel, Math.hypot(event.clientX - press.x, event.clientY - press.y));
        if (event.type !== 'pointermove') trace(false);
    };
    function trace(on) {
        if (tracing === on) return;
        tracing = on;
        for (const type of PATH) feed.ownerDocument[on ? 'addEventListener' : 'removeEventListener'](type, onPath, true);
    }
    const onPointerDown = event => {
        const entry = tracing && event.pointerId !== press.id ? null : entryOf(event.target);
        press = entry && event.button === 0 && isFreeArea(event.target, entry) ? { entry, id: event.pointerId,
            x: event.clientX, y: event.clientY, travel: 0, selected: String(globalThis.getSelection?.() || '') } : null;
        trace(Boolean(press));
    };
    const onClick = event => {
        const entry = entryOf(event.target), start = press;
        press = null;
        trace(false);
        if (!destroyed && isFreePress(event, entry, start, globalThis.getSelection?.())) {
            entry.querySelector('[data-intent="open-project"]')?.click();
        }
    };
    feed.addEventListener('pointerdown', onPointerDown);
    feed.addEventListener('click', onClick);
    const current = row => !destroyed && inFeed(row.node) && rows.get(row.key) === row;
    const visibleAnchor = id => [...rows.values()].find(row => row.id === id && inFeed(row.node));
    const matching = (taskId, projectId) => [...rows.values()].find(row =>
        row.projectId === projectId && row.subjects.has(taskId) && inFeed(row.node));
    function stampTime(row, ts) {
        const time = formatMsgTime(ts);
        if (!time) return;
        row.ts = ts;
        row.time.textContent = time.short;
        row.time.title = time.full;
        row.time.hidden = false;
    }
    function paint(row) {
        const activity = activities.get(row.taskId);
        // Keep already observed result facts when connectivity or a partial
        // census withdraws activity. A retry clears this same-subject cache.
        if (activity?.status || activity?.outcome_axes) row.detail = { ...activity };
        const phase = handoffPhase(activity, row.detail, connected);
        paintTaskPhase(row.status, phase, row.secondary);
    }
    function reconcileStarted(node) {
        const represented = [...rows.values()].some(row => row.kind !== 'started'
            && row.projectId === node.dataset.projectId && row.subjects.has(node.dataset.taskId) && inFeed(row.node));
        node.hidden = represented;
    }
    function reconcileAnnotation(note) {
        const [projectId, , taskId] = (note.dataset.destinationKey || '').split('|');
        const represented = Boolean(matching(taskId, projectId)
            && ['scheduled', 'delivered'].includes(note.dataset.annotationStatus));
        note.hidden = represented;
        const actions = note.parentElement?.querySelector('.msg-routing-actions');
        if (actions) actions.hidden = represented;
    }
    // A durable receipt row folded under a card proves the transfer is in Main
    // history: the card's "not saved" mark, if any, was about exactly that gap.
    function fold(shadow, under) {
        shadow.node.hidden = true;
        under.shadows.push(shadow, ...shadow.shadows.splice(0));
        for (const taskId of shadow.subjects) under.subjects.add(taskId);
        // The receipt owns the transfer time. Conversion supplies no invented
        // date while its durable row is still in flight; ordering stays intact.
        if (shadow.kind === 'receipt') for (const row of rows.values()) {
            if (row.id === shadow.id && row.kind === 'card') stampTime(row, shadow.ts);
        }
        if (shadow.kind === 'receipt' && under.node.dataset.receipt) {
            delete under.node.dataset.receipt;
            under.node.classList.remove('project-handoff--unsaved');
        }
    }
    // An evicted anchor hands its shadows to a surviving anchor of the same
    // identity, else the first still-mounted shadow takes over — keeping the
    // CURRENT liveness subject (a followed retry), never the shadow's older one.
    function promote(row) {
        rows.delete(row.key);
        const survivor = visibleAnchor(row.id);
        if (survivor) for (const taskId of row.subjects) survivor.subjects.add(taskId);
        for (const next of row.shadows) {
            if (survivor) { fold(next, survivor); continue; }
            if (!inFeed(next.node)) continue;
            next.node.hidden = false;
            // Folding groups receipts, not execution evidence. A different root
            // sharing the origin must recover its OWN status when it reappears.
            const follows = row.followed.has(next.taskId);
            rows.set(next.key, { ...next, subjects: new Set([...row.subjects, ...next.subjects]),
                shadows: row.shadows.filter(other => other !== next),
                ...(follows ? { detail: row.detail, taskId: row.taskId, followed: new Set(row.followed) } : {}),
                epoch: row.epoch + 1, pending: false, checked: false });
            return true;
        }
        return true;
    }
    function sweep() {
        let changed = false;
        for (const row of [...rows.values()]) if (!inFeed(row.node)) changed = promote(row) || changed;
        return changed;
    }
    /** With a node: only that node's projection. Without: every dependent node. */
    function reconcile(node) {
        if (destroyed) return;
        if (node) {
            if (node.dataset?.systemType === 'project_started') reconcileStarted(node);
            const note = node.querySelector?.('.msg-routing-annotation');
            if (note) reconcileAnnotation(note);
            const mountedStart = node.dataset?.systemType === 'project_started'
                && rows.get(`started:${node.dataset.projectId}`)?.node === node;
            if (node.dataset?.systemType !== 'project_handoff' && !mountedStart) return;
        }
        sweep();
        for (const started of feed.querySelectorAll('[data-system-type="project_started"]')) reconcileStarted(started);
        for (const note of feed.querySelectorAll('.msg-routing-annotation')) reconcileAnnotation(note);
    }
    function follow(row, successor) {
        if (!successor || row.followed.has(successor)) return false;
        row.followed.add(successor);
        row.subjects.add(successor);
        row.taskId = successor;
        row.epoch++;
        row.checked = false;
        row.detail = null;
        return true;
    }
    function resolve(row) {
        if (!current(row) || !complete || !connected || activities.has(row.taskId)
            || row.node.hidden || row.pending || row.checked || isTerminalTaskDetail(row.detail)) return;
        const taskId = row.taskId, epoch = row.epoch;
        row.pending = true;
        row.checked = true;
        Promise.resolve().then(() => fetchDetail(taskId)).then(detail => {
            if (!current(row) || epoch !== row.epoch || taskId !== row.taskId) return;
            // The retained task result, never project activity, names a retry.
            const effectiveRetry = detail?.task_id !== taskId
                && (detail?.original_task_id === taskId || detail?.retry_lineage?.some(item => item.task_id === taskId));
            const successor = String((effectiveRetry ? detail.task_id : '') || detail?.superseded_by || detail?.retry_task_id || '');
            if (successor && successor !== taskId) {
                if (!follow(row, successor)) return;
            } else {
                row.detail = detail;
                // A successful read during withdrawal/retry publication is not
                // a settled absence. The existing census refresh revisits that
                // in-flight fact; failures and inactive history remain latched.
                if (['running', 'scheduled'].includes(detail?.status)) row.checked = false;
            }
            mutate(() => paint(row));
        }).catch(() => {
            // A failed read remains unknown until a real re-entry/reconnect,
            // not another costly detail request on each census tick.
        }).finally(() => {
            row.pending = false;
            if (current(row) && row.taskId !== taskId) resolve(row);
        });
    }
    function mount(node, { taskId, projectId, projectName, title, handoffId, kind = 'receipt', receipt = '', ts = '' }) {
        if (!taskId || !projectId || destroyed) return node;
        const id = kind === 'started' ? `started:${projectId}` : handoffId || `legacy:${JSON.stringify([taskId, projectId])}`;
        sweep();
        const anchor = visibleAnchor(id);
        if (anchor && anchor.node === node) { anchor.subjects.add(taskId); stampTime(anchor, ts); return node; }
        node.dataset.projectId = projectId;
        node.dataset.taskId = taskId;
        if (kind !== 'started') node.dataset.handoffId = id;
        node.dataset.systemType = kind === 'started' ? 'project_started' : 'project_handoff';
        node.classList.add('project-handoff');
        const line = document.createElement('div');
        line.className = 'project-handoff-heading';
        const status = document.createElement('span');
        status.setAttribute('role', 'status');
        const secondary = document.createElement('span');
        secondary.className = 'chat-live-phase-secondary';
        secondary.hidden = true;
        const name = document.createElement('span');
        name.className = 'project-handoff-title';
        name.textContent = title || projectName || 'Project';
        const phases = document.createElement('div');
        phases.className = 'project-handoff-phase';
        phases.append(status, secondary);
        line.append(name, phases);
        const reference = projectReference({ id: projectId, name: projectName }, { layout: 'inline', taskId });
        // The destination row: the reference fills it beside the recorded time and Copy.
        const footer = document.createElement('div');
        footer.className = 'project-handoff-footer';
        const time = document.createElement('div');
        time.className = 'msg-time';
        time.hidden = true;
        footer.append(reference, time);
        // A converted card whose receipt is not durable is an honest live chip,
        // never a claim that Main history holds this transfer (it will not
        // survive a reload as an anchor; the binding and the pointer do).
        if (kind === 'card' && receipt && !DURABLE_RECEIPTS.has(receipt)) {
            node.dataset.receipt = receipt;
            node.classList.add('project-handoff--unsaved');
        }
        // One composition for conversion, creation and replay, independent of
        // the old task-card or message shell. Copy keeps the shared chat control.
        node.replaceChildren(line, footer);
        const copy = attachCopy?.(node, `${name.textContent}\n${reference.querySelector('.chat-live-project-name').textContent}`);
        if (copy) footer.append(copy);
        const row = { key: kind === 'card' ? `card:${taskId}` : id, id, node, status, secondary, kind, taskId, projectId,
            time, ts: '', subjects: new Set([taskId]), followed: new Set([taskId]), shadows: [], detail: null, pending: false, checked: false, epoch: 0 };
        stampTime(row, ts || (kind === 'card' ? anchor?.ts : ''));
        if (kind === 'receipt' && anchor) {
            // Duplicate delivery is not evidence that a differently named
            // execution supersedes its subject: one receipt, the rest shadowed.
            fold(row, anchor);
            return anchor.node;
        }
        rows.set(row.key, row);
        if (kind === 'card' && anchor?.kind === 'receipt') {
            rows.delete(anchor.key);
            fold(anchor, row);
            if (row.taskId === anchor.taskId) row.detail = anchor.detail;
        }
        paint(row);
        // A card is already in the feed when it converts: the rows it now
        // represents (Started, routing) fold at once. addMessage appends a
        // receipt after mounting; its own reconcile follows the insert.
        if (kind === 'card' && inFeed(node)) reconcile();
        return node;
    }
    function snapshot(data) {
        if (destroyed) return;
        complete = data?.active_chat_activities_complete === true && data.supervisor_ready === true;
        const incoming = new Map((data?.active_chat_activities || []).map(a => [String(a.activity_id || ''), a]));
        // Only the host's explicit technical retry linkage advances a subject.
        // A shared Project, owner origin or an explicit Continue is not lineage.
        const successors = new Map();
        for (const [id, activity] of incoming) {
            const predecessor = String(activity.timeout_retry_from || activity.original_task_id || '');
            if (predecessor && predecessor !== id) successors.set(predecessor, id);
        }
        for (const row of rows.values()) {
            while (successors.has(row.taskId) && follow(row, successors.get(row.taskId))) { /* explicit chain */ }
            if (incoming.has(row.taskId) && !activities.has(row.taskId)) {
                row.epoch++;
                row.checked = false;
            }
        }
        activities = incoming;
        // An evicted anchor is the only reason the feed-wide projections move on a
        // census tick; a steady feed repaints its rows and nothing else.
        if (rows.size) mutate(() => { if (sweep()) reconcile(); for (const row of rows.values()) paint(row); });
        for (const row of rows.values()) resolve(row);
    }
    return { mount, reconcile, snapshot,
        setConnected(value) {
            if (connected !== value) {
                connected = value;
                // Reconnection alone is not a fresh activity observation.
                activities = new Map();
                for (const row of rows.values()) { row.epoch++; row.checked = false; }
            }
            if (rows.size) mutate(() => { for (const row of rows.values()) paint(row); });
        },
        destroy() {
            destroyed = true;
            press = null;
            trace(false);
            feed.removeEventListener('pointerdown', onPointerDown);
            feed.removeEventListener('click', onClick);
            rows.clear();
            activities.clear();
        },
    };
}
