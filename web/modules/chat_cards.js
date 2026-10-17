// Living cards in the chat feed (docs/DESIGN.md "Living cards"; docs/CLIENT_CONTRACT.md §5 "Living cards"). A card
// row is built one way live (the `card` frame) and on replay (a `msg_type: "card"` history row): both carry only the
// card's identity `{card_id, kind, title, revision}` and its plain `fallback_text`. The body is read from
// `GET /api/cards/{id}` (cached by revision); while it loads, or when it cannot be read, the card shows the fallback
// text — never an empty box. A `card_state` frame re-reads a card whose revision grew and takes new presses in place.
//   components → card_components.js (presses go to the card's `card:` decisions);
//   widget     → the live skill's tab from `GET /api/widgets`, inline: declarative through the Widgets page's own
//                declarative host, module/iframe in a frame under the message's size policy; never registered on
//                the Widgets page, so leaving that page disposes nothing here: the feed disposes it with its row;
//   app        → card_frames.js, an opaque frame without network whose submit fills the owner's draft.
// Frames mount lazily near the viewport, at most three live at once; a far one becomes a static face with Open.
import { apiClient, fetchJson } from './api_client.js';
import { createCardPresses, mountCardComponents, setCardPinned } from './card_components.js';
import { createInlineFit, mountCardApp } from './card_frames.js';
import { escapeHtmlText as escapeHtml } from './utils.js';
import { frameHeight, mountModuleWidget, mountRouteIframeWidget } from './widget_module.js';
import { mountDeclarativeWidget } from './widgets.js';

export const MAX_LIVE_CARD_FRAMES = 3;
const NEAR_MARGIN = '240px 0px';
const MAKE_SKILL = (title) => `Turn the card «${title}» into a skill so I can keep it on Widgets.`;

// Shared by every room on the page: a card's current body by id, an app's bytes by hash, the live widget list, and
// each card's press ledger (a row drawn again keeps the retry ids of presses still owed).
const bodies = new Map();
const appTexts = new Map();
const presses = new Map();
let widgetTabs = null;

/** The current body of a card, read again only when `revision` is newer than the one held. */
export function readCardBody(cardId, revision = 0, read = (url) => fetchJson(url, { cache: 'no-store' })) {
    const held = bodies.get(cardId);
    if (held && (held.failed ? false : held.revision >= revision)) return held.promise;
    const promise = read(`/api/cards/${encodeURIComponent(cardId)}`).then((body) => {
        bodies.set(cardId, { revision: Number(body?.revision) || revision, promise });
        return body;
    }, (error) => {
        bodies.set(cardId, { revision, promise, failed: true });
        throw error;
    });
    bodies.set(cardId, { revision, promise });
    return promise;
}

function readAppText(url, sha256) {
    if (!appTexts.has(sha256)) {
        appTexts.set(sha256, fetch(url, { cache: 'no-store' }).then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.text();
        }).catch((error) => { appTexts.delete(sha256); throw error; }));
    }
    return appTexts.get(sha256);
}

function liveWidgetTabs() {
    widgetTabs ||= apiClient.widgets().then((data) => (Array.isArray(data?.ui_tabs) ? data.ui_tabs : []))
        .catch((error) => { widgetTabs = null; throw error; });
    return widgetTabs;
}

const pressLedger = (cardId) => {
    if (!presses.has(cardId)) presses.set(cardId, createCardPresses({ cardId }));
    return presses.get(cardId);
};

/**
 * At most `max` live frames: `near(record, isNear)` follows the viewport (a frame that drifts far unmounts, a near
 * one mounts while there is room), `open(record)` mounts one at the owner's request (the oldest other live frame
 * makes room), `forget(record)` drops a released row.
 */
export function createFrameBudget({ max = MAX_LIVE_CARD_FRAMES, mount, unmount }) {
    const live = new Set();
    const nearby = new Set();
    const start = (record) => {
        if (live.has(record)) return;
        live.add(record);
        mount(record);
    };
    const stop = (record) => {
        if (!live.delete(record)) return;
        unmount(record);
    };
    const fill = () => {
        for (const record of nearby) if (live.size < max) start(record);
    };
    return {
        live: () => [...live],
        near(record, isNear) {
            if (isNear) nearby.add(record);
            else { nearby.delete(record); stop(record); }
            fill();
        },
        open(record) {
            if (!live.has(record) && live.size >= max) stop([...live].find((other) => !nearby.has(other)) || [...live][0]);
            start(record);
        },
        forget(record) { nearby.delete(record); stop(record); fill(); },
    };
}

const button = (label, data) => `<button type="button" class="btn btn-default btn-sm chat-card-action" ${data}>${escapeHtml(label)}</button>`;

/**
 * One room's card rows. `frameNode(msg, node)` wraps a node in an Ouroboros bubble (sender, time, row address);
 * `decorate(bubble, msg, text)` gives it Copy (the fallback text) and Reply; `composer()` is the room's composer, whose
 * draft an app's submit and "Make it a skill" fill; `onDomWrite(write)` keeps the reading position.
 */
export function createChatCards({ frameNode, decorate = () => {}, composer = () => null, onDomWrite = (write) => write() }) {
    const records = new Map();
    const byBubble = new WeakMap();
    const messageHandlers = new Set();
    let destroyed = false;
    const budget = createFrameBudget({
        mount: (record) => { record.framed = startFrame(record); },
        unmount: (record) => { record.framed?.(); record.framed = null; showFace(record); },
    });
    const observer = typeof IntersectionObserver === 'function'
        ? new IntersectionObserver((entries) => entries.forEach((entry) => {
            const record = byBubble.get(entry.target);
            if (record?.framedKind && records.get(record.id) === record) budget.near(record, entry.isIntersecting);
        }), { rootMargin: NEAR_MARGIN })
        : null;

    const write = (fn) => (destroyed ? undefined : onDomWrite(() => { fn(); return true; }));
    const insertLine = (line) => {
        const room = composer();
        if (!room?.input) return false;
        const at = room.input.selectionStart ?? room.input.value.length;
        const before = room.input.value.slice(0, at);
        room.insertAtCursor(`${before && !before.endsWith('\n') ? '\n' : ''}${line}`);
        return true;
    };
    const setStatus = (record, text, tone = 'muted') => {
        record.status.hidden = !text;
        record.status.textContent = text;
        record.status.dataset.tone = tone;
    };
    const showFallback = (record, note = '', tone = 'muted', retry = false) => write(() => {
        record.body.innerHTML = `<p class="chat-card-fallback">${escapeHtml(record.fallback)}</p>`;
        setStatus(record, note, tone);
        if (retry) record.status.insertAdjacentHTML('beforeend', ` ${button('Retry', 'data-card-retry')}`);
    });
    const disposeBody = (record) => {
        observer?.unobserve(record.bubble);
        record.mounted?.dispose?.();
        record.mounted = null;
        record.fit?.dispose();
        record.fit = null;
        if (record.framedKind) budget.forget(record);
        record.framedKind = '';
    };

    // The foot: "Ouroboros made this", then what the owner can do with it — keep a component card on Widgets (and
    // take it off again), or ask Ouroboros to make an app into a skill. A widget card is a skill's: nothing to keep.
    function paintActions(record, justPinned = false) {
        const body = record.data;
        let html = '';
        if (body?.kind === 'components') {
            html = body.pinned
                ? `<span class="ui-status" data-tone="ok">Kept on Widgets</span> ${button(justPinned ? 'Undo' : 'Remove', 'data-card-unpin')}`
                : button('Keep on Widgets', 'data-card-pin');
        } else if (body?.kind === 'app') html = button('Make it a skill', 'data-card-make-skill');
        record.actions.innerHTML = html;
    }

    async function setPinned(record, pinned) {
        record.actions.querySelectorAll('button').forEach((node) => { node.disabled = true; });
        const result = await setCardPinned(record.id, pinned);
        if (destroyed || !records.has(record.id)) return;
        if (result.ok && record.data) record.data = { ...record.data, pinned };
        write(() => {
            paintActions(record, pinned && result.ok);
            if (!result.ok) {
                record.actions.insertAdjacentHTML('afterbegin', `<span class="ui-status" data-tone="danger">${escapeHtml(
                    `${pinned ? 'Not kept' : 'Not removed'}: ${result.error}`)}</span> `);
            }
        });
    }

    function showFace(record) {
        write(() => {
            record.body.innerHTML = `<div class="chat-card-face"><p class="chat-card-fallback">${escapeHtml(record.fallback)}</p>${button('Open', 'data-card-open')}</div>`;
            record.toggle.hidden = true;
        });
    }

    // One live frame (an app, or a framed widget): mounts into the body and returns its disposer.
    function startFrame(record) {
        const host = document.createElement('div');
        host.className = 'chat-card-frame-host';
        write(() => { record.body.replaceChildren(host); setStatus(record, ''); });
        // A frame's height changes the feed like any other write: through the room's reading position.
        const inline = createInlineFit(host, record.toggle);
        record.fit = { fit: (height) => write(() => inline.fit(height)), dispose: () => inline.dispose() };
        const controller = new AbortController();
        let dispose = null;
        const done = (fn) => { if (controller.signal.aborted) fn?.(); else dispose = fn; };
        const fail = (text) => { if (!controller.signal.aborted) showFallback(record, text, 'danger', true); };
        const body = record.data;
        if (record.framedKind === 'app') {
            readAppText(body.app.url, body.app.sha256).then((html) => done(mountCardApp(host, {
                cardId: record.id, title: body.title, sha256: body.app.sha256, html, fit: record.fit,
                onSubmit: (line) => insertLine(line),
            })), (error) => fail(`The app could not be read: ${error.message || error}`));
        } else {
            const { tab } = record;
            const inline = { fit: (height) => record.fit?.fit(height), fault: (text) => setStatus(record, text, 'danger') };
            Promise.resolve().then(() => (tab.render.kind === 'module'
                ? mountModuleWidget(host, tab, tab.render, controller.signal, messageHandlers, inline)
                : mountRouteIframeWidget(host, tab, tab.render))).then((fn) => {
                if (tab.render.kind !== 'module') {
                    host.querySelector('iframe')?.style.removeProperty('--widget-frame-height');
                    record.fit?.fit(frameHeight(tab.render));
                }
                done(fn);
            }, (error) => fail(`The widget could not start: ${error.message || error}`));
        }
        return () => {
            controller.abort();
            const settle = dispose?.();
            if (settle?.then) settle.catch(() => {});
            record.fit?.dispose();
            record.fit = null;
        };
    }

    async function mountWidget(record, ref) {
        let tab = null;
        try {
            tab = (await liveWidgetTabs()).find((item) => item.skill === ref?.skill && item.tab_id === ref?.tab_id && !item.pinned_card) || null;
        } catch (error) {
            showFallback(record, `The widget list could not be read: ${error.message || error}`, 'danger', true);
            return;
        }
        if (destroyed || !records.has(record.id)) return;
        if (!tab) { showFallback(record, 'This skill is off.'); return; }
        record.tab = tab;
        record.widgetSignature = JSON.stringify([tab.revision, tab.render]);
        if (tab.render?.kind === 'declarative') {
            const host = document.createElement('div');
            host.className = 'chat-card-widget';
            write(() => { record.body.replaceChildren(host); setStatus(record, ''); });
            const dispose = await mountDeclarativeWidget(host, { ...tab, key: `card:${record.id}` }, tab.render, { input: ref.input });
            if (destroyed || !records.has(record.id)) dispose?.();
            else record.mounted = { dispose };
            return;
        }
        if (!['module', 'iframe'].includes(tab.render?.kind)) { showFallback(record, 'This widget cannot be shown in a message.'); return; }
        record.framedKind = 'widget';
        showFace(record);
        if (observer) observer.observe(record.bubble);
    }

    function drawBody(record) {
        const body = record.data;
        disposeBody(record);
        paintActions(record);
        if (body.kind === 'components') {
            write(() => {
                const host = document.createElement('div');
                host.className = 'chat-card-components';
                record.body.replaceChildren(host);
                setStatus(record, '');
                record.mounted = mountCardComponents(host, body, { presses: pressLedger(record.id) });
            });
        } else if (body.kind === 'widget') {
            mountWidget(record, body.widget);
        } else if (body.kind === 'app' && body.app?.url) {
            record.framedKind = 'app';
            showFace(record);
            if (observer) observer.observe(record.bubble);
        } else {
            showFallback(record, 'This card cannot be shown here.');
        }
    }

    function load(record, revision) {
        readCardBody(record.id, revision).then((body) => {
            if (destroyed || records.get(record.id) !== record) return;
            const sameBody = record.data && record.data.revision === body.revision;
            record.data = body;
            if (sameBody && body.kind === 'components') record.mounted?.update(body);
            else drawBody(record);
        }, (error) => {
            if (destroyed || records.get(record.id) !== record) return;
            if (error?.status === 404) showFallback(record, 'This card is no longer available.');
            else showFallback(record, `The card could not be loaded: ${error?.message || error}`, 'danger', true);
        });
    }

    function onClick(record, event) {
        const target = event.target.closest?.('button');
        if (!target || target.disabled) return;
        if (target.hasAttribute('data-card-retry')) {
            bodies.delete(record.id);
            widgetTabs = null;
            disposeBody(record);
            showFallback(record, 'Loading the card…');
            load(record, record.revision);
        } else if (target.hasAttribute('data-card-open')) budget.open(record);
        else if (target.hasAttribute('data-card-pin')) setPinned(record, true);
        else if (target.hasAttribute('data-card-unpin')) setPinned(record, false);
        else if (target.hasAttribute('data-card-make-skill')) insertLine(MAKE_SKILL(record.title));
    }

    /** The bubble for a card row (live frame or history row), or null without a card id. */
    function buildCard(msg) {
        const card = msg?.card || {};
        const id = String(card.card_id || '');
        if (destroyed || !id) return null;
        const title = String(card.title || '');
        const fallback = String(msg.fallback_text ?? msg.text ?? '');
        const node = document.createElement('section');
        node.className = 'chat-card';
        node.dataset.cardId = id;
        node.dataset.cardKind = String(card.kind || '');
        node.setAttribute('aria-label', title || 'Card');
        node.innerHTML = `<div class="chat-card-title" role="heading" aria-level="3">${escapeHtml(title)}</div>
            <div class="chat-card-body" data-card-body><p class="chat-card-fallback">${escapeHtml(fallback)}</p></div>
            <div class="chat-card-status ui-status" data-tone="muted" role="status" aria-live="polite">Loading the card…</div>
            <button type="button" class="btn btn-ghost btn-sm chat-card-expand" aria-expanded="false" hidden>Expand</button>
            <div class="chat-card-foot"><span class="chat-card-made">Ouroboros made this</span><span class="chat-card-actions" data-card-actions></span></div>`;
        const bubble = frameNode(msg, node);
        bubble.classList.add('chat-card-bubble');
        bubble.dataset.cardId = id;
        const prior = records.get(id);
        if (prior) release(prior.bubble);
        const record = {
            id, title, fallback, bubble, revision: Number(card.revision) || 0,
            body: node.querySelector('[data-card-body]'), status: node.querySelector('.chat-card-status'),
            toggle: node.querySelector('.chat-card-expand'), actions: node.querySelector('[data-card-actions]'),
            data: null, mounted: null, framed: null, framedKind: '', fit: null, tab: null,
        };
        node.addEventListener('click', (event) => onClick(record, event));
        records.set(id, record);
        byBubble.set(bubble, record);
        decorate(bubble, msg, fallback);
        load(record, record.revision);
        return bubble;
    }

    /** A `card_state` frame: a newer revision is read again; recorded presses update the drawn card in place. */
    function applyCardState(msg) {
        const record = records.get(String(msg?.card?.card_id || ''));
        if (!record) return false;
        const revision = Number(msg.card.revision) || 0;
        record.revision = Math.max(record.revision, revision);
        if (!record.data || revision > record.data.revision) {
            load(record, revision);
            return true;
        }
        record.data = { ...record.data, submissions_summary: msg.submissions_summary || [] };
        bodies.set(record.id, { revision: record.data.revision, promise: Promise.resolve(record.data) });
        record.mounted?.update?.(record.data);
        return true;
    }

    /** `extension_lifecycle`: a kept card's pin, or a skill's widget that changed or left. */
    function lifecycle(msg) {
        if (msg?.skill === '_cards') {
            const record = records.get(String(msg.data?.card_id || ''));
            const pinned = msg.action === 'card_pinned';
            // The echo of this room's own Keep / Undo changes nothing; another window's does.
            if (record?.data?.kind === 'components' && record.data.pinned !== pinned) {
                record.data = { ...record.data, pinned };
                bodies.delete(record.id);
                write(() => paintActions(record, false));
            }
            return;
        }
        widgetTabs = null;
        const shown = [...records.values()].filter((record) => record.data?.kind === 'widget');
        if (!shown.length) return;
        liveWidgetTabs().then((tabs) => shown.forEach((record) => {
            const ref = record.data?.widget;
            const tab = tabs.find((item) => item.skill === ref?.skill && item.tab_id === ref?.tab_id && !item.pinned_card);
            if (records.get(record.id) !== record || (tab && JSON.stringify([tab.revision, tab.render]) === record.widgetSignature)) return;
            drawBody(record);
        }), () => {});
    }

    /** The rows inside a released subtree give back everything they hold. */
    function release(root) {
        for (const record of [...records.values()]) {
            if (root !== record.bubble && !root?.contains?.(record.bubble)) continue;
            disposeBody(record);
            records.delete(record.id);
        }
    }

    return {
        buildCard,
        applyCardState,
        lifecycle,
        /** Every socket message, for module widgets that listen to their skill's events. */
        relay(msg) { messageHandlers.forEach((handler) => handler(msg)); },
        release,
        destroy() {
            if (destroyed) return;
            for (const record of records.values()) disposeBody(record);
            records.clear();
            observer?.disconnect();
            messageHandlers.clear();
            destroyed = true;
        },
    };
}
