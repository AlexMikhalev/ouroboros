// Component cards (docs/DESIGN.md "Living cards"; docs/CLIENT_CONTRACT.md §5 "Living cards"): the declarative tree
// Ouroboros put into a living card, drawn by widget_render.js in a CARD context — no skill route, the card's `data` as
// what path fields read, only inline `data:` media — in the chat feed (chat_cards.js) and, once kept, on the Widgets
// page (`mountPinnedCard`). Local components (tabs, details) never call the server. A press on an `action` or a `form`
// is the owner's act: it goes to `POST /api/decisions` as `card:<card_id>:<submit.id>` (`createCardPresses`) and
// comes back to Ouroboros as owner input.
import { apiFetch } from './api_client.js';
import { applyChartTheme, onThemeChange } from './theme_palette.js';
import { collectSafeFieldValues } from './ui_helpers.js';
import { indexComponentTree, patchWidgetChildren, renderComponent, syncWidgetCharts } from './widget_render.js';

/** Where a card's markup lives: no skill owns it, so there is no route and no extension URL from data. */
export const CARD_RENDER_CONTEXT = Object.freeze({ routePath: () => '', routePrefix: '' });

const COMMIT_TYPES = new Set(['action', 'form']);
const SENT = { status: 'success', message: 'Sent ✓' };
const OWED = { status: 'warning', message: 'Saved, not delivered yet. Press again to retry.' };

const newRequestId = () => globalThis.crypto?.randomUUID?.() || `card-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const sameValues = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** One press to the decision ingress: `{status, body}`, status 0 when no answer came back. */
export async function postCardDecision(body) {
    try {
        const response = await apiFetch('/api/decisions', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        return { status: response.status, body: await response.json().catch(() => ({})) };
    } catch (error) {
        return { status: 0, body: { error: error?.message || String(error) } };
    }
}

// What a press's answer means for its control: the feedback it shows, whether the same request id is retried by the
// next press (no answer, a server failure, delivery still owed) and whether the control locks (gone, or a `once`
// control that already counted).
function pressOutcome(status, body = {}) {
    if (status === 200 && body.ok !== false) return { feedback: SENT, sent: true };
    if (status === 202) return { feedback: OWED, retry: true };
    if (status === 409 && body.reason_code === 'card_action_once') {
        return { feedback: { status: 'success', message: 'Already sent ✓' }, lock: true };
    }
    if (status === 404) {
        const message = body.reason_code === 'card_not_found' ? 'This card is gone.' : 'This control is no longer on the card.';
        return { feedback: { status: 'error', message }, lock: true };
    }
    if (status >= 400 && status < 500) {
        return { feedback: { status: 'error', message: `Not sent: ${body.error || `HTTP ${status}`}` } };
    }
    const reason = body.error ? `: ${body.error}` : (status ? ` (HTTP ${status})` : '');
    return { feedback: { status: 'error', message: `Not sent${reason}. Press again to retry.` }, retry: true };
}

/**
 * The presses of one card, by `submit.id`. Each press is its own submission with a fresh `request_id`; a press that
 * got no answer, failed on the server or is still owed is retried by the next press with the SAME id (when its values
 * are the same), so the host records it once. A `once` control locks after its first press counted — here, or in the
 * card's recorded presses (`adopt`). `press(actionId, {values, once})` resolves to the control's feedback, or null
 * while that control's earlier press is still in flight.
 */
export function createCardPresses({ cardId, post = postCardDecision, newId = newRequestId }) {
    const retry = new Map();
    const locked = new Map();
    const inFlight = new Set();
    return {
        locked: (actionId) => locked.has(actionId),
        lockedFeedback: (actionId) => locked.get(actionId) || null,
        /** The card's `submissions_summary`: a `once` control whose press was recorded stays locked. */
        adopt(summary, onceIds) {
            for (const entry of Array.isArray(summary) ? summary : []) {
                if (onceIds.has(entry?.action_id) && !locked.has(entry.action_id)) {
                    locked.set(entry.action_id, entry.delivery === 'owed' ? { status: 'warning', message: 'Saved, not delivered yet.' } : SENT);
                }
            }
        },
        async press(actionId, { values = null, once = false } = {}) {
            if (locked.has(actionId)) return locked.get(actionId);
            if (inFlight.has(actionId)) return null;
            const prior = retry.get(actionId);
            const requestId = prior && sameValues(prior.values, values) ? prior.requestId : newId();
            retry.set(actionId, { requestId, values });
            inFlight.add(actionId);
            const answer = await post({ request_id: requestId, decision_id: `card:${cardId}:${actionId}`, ...(values ? { values } : {}) });
            inFlight.delete(actionId);
            const outcome = pressOutcome(answer?.status ?? 0, answer?.body || {});
            if (!outcome.retry) retry.delete(actionId);
            if (outcome.lock || (once && outcome.sent)) locked.set(actionId, outcome.feedback);
            return outcome.feedback;
        },
    };
}

// The tree as drawn now: a commit control says "Sending…" while its press is in flight (the renderer's busy label)
// and is disabled once locked. A copy: the card's own declaration is never edited.
function drawnTree(components, presses) {
    const visit = (component) => {
        if (!component || typeof component !== 'object') return component;
        const next = { ...component };
        if (COMMIT_TYPES.has(next.type)) {
            next.busy_label = next.busy_label || 'Sending…';
            if (presses.locked(next.submit?.id)) next.disabled = true;
        }
        if (Array.isArray(next.components)) next.components = next.components.map(visit);
        if (Array.isArray(next.tabs)) next.tabs = next.tabs.map((tab) => ({ ...tab, components: Array.isArray(tab?.components) ? tab.components.map(visit) : [] }));
        return next;
    };
    return (Array.isArray(components) ? components : []).map(visit);
}

/**
 * Mount one component card: `card` carries the `CardResponse` fields `{card_id, components, data?,
 * submissions_summary?}`. `presses` is the card's ledger when the caller keeps it across mounts (a feed row released
 * and drawn again keeps its retry ids); `write(fn)` is the place's own DOM write (the feed keeps its reading position
 * through it). Returns `{update(card), dispose()}`; `update` draws a new revision in place.
 */
export function mountCardComponents(mount, card, { presses = createCardPresses({ cardId: card.card_id }), write = (fn) => fn() } = {}) {
    const componentState = {};
    const formValues = {};
    const pendingActions = new Set();
    const charts = new Map();
    const chartShapes = new Map();
    const interactions = new AbortController();
    let current = card;
    let index = indexComponentTree(card.components);
    let disposed = false;
    const unsubscribeTheme = onThemeChange(() => charts.forEach((chart) => applyChartTheme(chart)));

    const adoptSubmissions = () => {
        const commits = index.entries.filter(({ component }) => COMMIT_TYPES.has(component.type) && component.submit?.id);
        presses.adopt(current.submissions_summary, new Set(commits.filter(({ component }) => component.submit.once === true)
            .map(({ component }) => component.submit.id)));
        const latest = new Map((Array.isArray(current.submissions_summary) ? current.submissions_summary : [])
            .map((entry) => [entry?.action_id, entry]));
        for (const { component, key } of commits) {
            const recorded = latest.get(component.submit.id);
            const lockedFeedback = presses.lockedFeedback(component.submit.id);
            if (lockedFeedback) componentState[`feedback:${key}`] = lockedFeedback;
            else if (recorded && !componentState[`feedback:${key}`]) componentState[`feedback:${key}`] = recorded.delivery === 'owed' ? OWED : SENT;
        }
    };
    const render = () => {
        if (disposed) return;
        const view = { state: { result: current.data || {} }, status: {}, componentState, formValues, pendingActions, visibleKeys: new Set() };
        const desired = document.createElement('template');
        desired.innerHTML = drawnTree(current.components, presses)
            .map((component, idx) => renderComponent(CARD_RENDER_CONTEXT, component, view, `components.${idx}`)).join('');
        patchWidgetChildren(mount, desired.content);
        syncWidgetCharts(mount, charts, chartShapes);
    };
    const send = async (key, values) => {
        const spec = index.byKey.get(key)?.component;
        const actionId = spec?.submit?.id;
        if (disposed || !actionId || pendingActions.has(key) || presses.locked(actionId)) return;
        pendingActions.add(key);
        delete componentState[`feedback:${key}`];
        write(render);
        const feedback = await presses.press(actionId, { values, once: spec.submit.once === true });
        if (disposed) return;
        pendingActions.delete(key);
        if (feedback) componentState[`feedback:${key}`] = feedback;
        write(render);
    };
    const listen = (type, handler) => mount.addEventListener(type, handler, { signal: interactions.signal });
    listen('submit', (event) => {
        const form = event.target.closest('[data-widget-form]');
        if (!form) return;
        event.preventDefault();
        const key = form.dataset.widgetForm || '';
        const spec = index.byKey.get(key)?.component;
        if (spec) send(key, collectSafeFieldValues(form, spec.fields || []));
    });
    listen('click', (event) => {
        const button = event.target.closest('button');
        if (!button || button.disabled) return;
        if (button.dataset.widgetAction) send(button.dataset.widgetAction, null);
        else if (button.dataset.widgetTabKey) {
            componentState[button.dataset.widgetTabKey] = Number(button.dataset.widgetTabIdx || 0);
            write(render);
        }
    });
    mount.replaceChildren();
    adoptSubmissions();
    render();
    return {
        update(next) {
            if (disposed) return;
            current = next;
            index = indexComponentTree(next.components);
            adoptSubmissions();
            render();
        },
        dispose() {
            if (disposed) return;
            disposed = true;
            interactions.abort();
            unsubscribeTheme();
            charts.forEach((chart) => chart.destroy());
            charts.clear();
        },
    };
}

/** Keep a component card on the Widgets page (`POST`) or take it off (`DELETE`): `{ok, error?}`. */
export async function setCardPinned(cardId, pinned) {
    try {
        const response = await apiFetch(`/api/cards/${encodeURIComponent(cardId)}/pin`, { method: pinned ? 'POST' : 'DELETE' });
        const body = await response.json().catch(() => ({}));
        return response.ok ? { ok: true, ...body } : { ok: false, error: body.error || `HTTP ${response.status}`, reason: body.reason_code || '' };
    } catch (error) {
        return { ok: false, error: error?.message || String(error) };
    }
}

/** A kept card on the Widgets page (a `pinned_card` tab of skill `_cards`): the same host, presses to `card:`. */
export function mountPinnedCard(mount, tab) {
    const render = tab.render || {};
    const mounted = mountCardComponents(mount, { card_id: tab.tab_id, components: render.components, data: render.data });
    return () => mounted.dispose();
}
