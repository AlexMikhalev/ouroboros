/**
 * Project lifecycle rows in Main (docs/DESIGN.md "Project completion mirror").
 *
 * A Project root that ended with Ouroboros's own final answer reaches Main as an
 * ordinary Ouroboros message, folded when it is long. Every other lifecycle row (a
 * start, an ending with no answer, a row written before the answer rode the row)
 * keeps its System text. Either way the row ends with the same Project reference:
 * the voice of a row never chooses how the UI points at its Project.
 */
import { createSystemMessageActions } from './ui_helpers.js';
import { tr } from './i18n.js';
import { projectReference } from './project_reference.js';
import { terminalTimeNote } from './utils.js';
import { mountResultFilesReader } from './result_files.js';

export const PROJECT_ROW_TYPES = new Set(['project_started', 'project_handoff', 'project_completion_summary']);

// Host-stamped Project lifecycle rows (§3 Main rows), live and replayed alike:
// a mirrored final answer is an ordinary assistant message, the rest System text.
export function appendProjectRow(addMessage, msg, text, opts) {
    const a = msg.completion_answer;
    return addMessage(a || text, a ? 'assistant' : 'system', !!(a || msg.markdown), msg.ts || null, false, {
        ...opts, systemType: msg.system_type, narration: msg.narration, projectId: msg.project_id || '',
        projectName: msg.project_name || '', taskName: msg.task_name || '', handoffId: msg.handoff_id || '', terminalTime: msg.terminal_time || null,
        taskId: msg.task_id || opts?.taskId || '', completionLimitation: msg.completion_reason || '', deliveryId: msg.delivery_id || '',
    });
}

// The fade says "there is more": it appears only when the fold really hides text. The clamp
// itself is unconditional CSS, so toggling the class never moves layout. A row mounted while
// Chat is not the visible page has no layout box (clientHeight 0): it is left alone and
// measured when Chat is shown; a resize re-measures, because the width decides the height.
// An expanded answer keeps its Show less until the owner folds it again.
function markFold(bubble) {
    const message = bubble.querySelector('.message');
    if (!message || !message.clientHeight || bubble.classList.contains('is-expanded')) return;
    const folded = message.scrollHeight > message.clientHeight + 1;
    bubble.classList.toggle('is-folded', folded);
    syncFold(bubble, message, folded);
}

// Collapsed text offers no keyboard stops (pointer clicks still reach visible links): Show full
// is the way in, so focus never lands on clipped text. Later Markdown regions are re-marked here
// on every measure, and a scrolling table re-marks itself once the attribute leaves.
const FOCUSABLE = 'a[href], button, input, select, textarea, summary, [tabindex]';
function syncFold(bubble, message, collapsed) {
    const toggle = bubble.querySelector(':scope > .system-message-actions > [data-answer-fold]');
    if (toggle) toggle.hidden = !collapsed && !bubble.classList.contains('is-expanded');
    for (const node of message.querySelectorAll(collapsed ? FOCUSABLE : '[data-fold-stop]')) {
        node.toggleAttribute('data-fold-stop', collapsed);
        if (collapsed) node.setAttribute('tabindex', '-1');
        else node.removeAttribute('tabindex');
    }
}

function setFold(bubble, expand, write) {
    const message = bubble.querySelector('.message');
    const toggle = bubble.querySelector(':scope > .system-message-actions > [data-answer-fold]');
    if (!message || !toggle) return;
    const feed = bubble.closest('#chat-messages, .chat-messages');
    const before = toggle.getBoundingClientRect().top;
    write(() => {
        bubble.classList.toggle('is-expanded', expand);
        bubble.classList.toggle('is-folded', !expand);
        message.scrollTop = 0;
        syncFold(bubble, message, !expand);
        toggle.setAttribute('aria-expanded', String(expand));
        toggle.textContent = expand ? tr('result.show_less', 'Show less') : tr('result.show_full', 'Show full');
        return true;
    }, expand ? undefined : { excludeAnchorNode: bubble });
    // Folding again keeps the control under the pointer once the viewport has settled.
    if (!expand && feed) feed.scrollTop += toggle.getBoundingClientRect().top - before;
}

function mountFoldToggle(bubble, actions, write) {
    const toggle = bubble.ownerDocument.createElement('button');
    toggle.type = 'button';
    toggle.className = 'btn btn-xs btn-default';
    toggle.dataset.answerFold = '';
    toggle.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.textContent = tr('result.show_full', 'Show full');
    toggle.addEventListener('click', () => setFold(bubble, !bubble.classList.contains('is-expanded'), write));
    // Backstop for a stop added after the last measure: keyboard focus opens the answer.
    bubble.querySelector('.message')?.addEventListener('focusin', (event) => {
        if (bubble.classList.contains('is-folded') && event.target?.matches?.(':focus-visible')) setFold(bubble, true, write);
    });
    actions.append(toggle);
}

// One pair of window listeners for the app's lifetime, however many rows exist: a row holds
// only listeners on its own nodes, so a released row needs no disposer here.
let watching = false;
let queued = false;
function afterLayout(work) {
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(work);
}
function refreshFoldsSoon() {
    if (queued) return;
    queued = true;
    afterLayout(() => {
        queued = false;
        document.querySelectorAll('.chat-bubble.project-answer').forEach(markFold);
    });
}
function watchFolds() {
    if (watching || typeof window === 'undefined') return;
    watching = true;
    window.addEventListener('ouro:page-shown', refreshFoldsSoon);
    window.addEventListener('resize', refreshFoldsSoon);
}

export function decorateProjectRow(bubble, { role = 'system', projectId = '', projectName = '', terminalTime, addedAt,
    systemType = '', completion = systemType === 'project_completion_summary', taskId = '',
    completionLimitation = '', limitation = completionLimitation, fetchDetail, renderFile, onDomWrite = (write) => write(), fold = role === 'assistant' } = {}) {
    if (!bubble || !projectId) return null;
    if (fold) {
        bubble.classList.add('project-answer');
        watchFolds();
        afterLayout(() => markFold(bubble));
    }
    const actions = createSystemMessageActions(projectReference({ id: projectId, name: projectName }));
    bubble.querySelector('.message')?.after(actions);
    if (completion) {
        if (limitation && role === 'assistant') {
            const warning = bubble.ownerDocument.createElement('div');
            warning.className = 'msg-provenance project-answer-limitation';
            warning.textContent = limitation;
            warning.title = limitation;
            bubble.insertBefore(warning, actions);
        }
        const note = bubble.ownerDocument.createElement('div');
        note.className = 'msg-provenance';
        note.textContent = terminalTimeNote(terminalTime, addedAt);
        bubble.insertBefore(note, actions);
        if (taskId && fetchDetail) mountResultFilesReader(bubble, actions, taskId, fetchDetail, renderFile, onDomWrite);
    }
    // An answer kept unfolded (one that speaks for another room's work) has no fold to toggle.
    if (fold) mountFoldToggle(bubble, actions, onDomWrite);
    return actions;
}

/** Kept beside the authored body; adoption never replaces the selected text. */
export function syncSavedProjectContext(bubble, projected, originId = '') {
    if (bubble && originId) bubble.dataset.originId = originId;
    if (!bubble || (projected && bubble.dataset.historyId)) return false;
    const prior = bubble.querySelector('.saved-project-context');
    if (!projected) {
        if (!prior) return false;
        prior.remove(); delete bubble.dataset.originProjected;
        return true;
    }
    if (prior) return false;
    const note = bubble.ownerDocument.createElement('div');
    note.className = 'msg-provenance saved-project-context';
    note.textContent = tr('project.saved_context_note', 'Saved project context — original request retained here; surrounding history may still need loading.');
    bubble.insertBefore(note, bubble.querySelector('.message')?.nextSibling || null);
    bubble.dataset.originProjected = '1';
    return true;
}
