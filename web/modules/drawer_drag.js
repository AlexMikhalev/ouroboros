/** The drawer follows the finger (owner Q1=A; docs/DESIGN.md "Shell").
 *
 * A horizontal drag that starts anywhere on the room, the screen's edge and its controls included, opens the
 * drawer; a drag on the open drawer (or its scrim) closes it. Only a field, a slider, media, selected text and
 * anything that scrolls sideways keep their own touch (owner 19.10: from the edge, and from where the thumb is).
 * Pointer Events only, transform-only motion driven by one number (`--drawer-p`, 0 closed … 1 open) that the scrim's
 * opacity follows; the owner of the drawer's state commits the result (so history, focus and inert stay the
 * layer stack's), and a gesture that does not commit leaves no trace. The drag is a shortcut: every gesture has a
 * visible control (☰ and ×). One constant switches it off in one line: DRAWER_DRAG. */

export const DRAWER_DRAG = true;

export const DRAG = Object.freeze({
    slop: 10,            // px of movement before the axis is decided
    axisRatio: 1,        // |dx| > ratio·|dy| locks the horizontal axis: a thumb's arc is a sideways drag
    minTravel: 24,       // px of travel before a fling may commit
    flingVelocity: 0.3,  // px/ms
    openPast: 0.5,       // a slow release opens past this progress
    closePast: 0.3,      // a slow release closes once this much of the drawer has gone
    sampleWindow: 100,   // ms of velocity samples
    clickGuard: 400,     // ms after a drag during which the click that ends it is not a tap
});

// What a drag may not start on: a field (the finger moves its caret or picks), a slider, media (the finger scrubs).
// A button, a link or a summary under the finger is fine: a drag is not a tap, and the click that ends it is
// swallowed (onClick).
export const FIELD_SELECTOR = 'input, textarea, select, video, audio, [role="slider"], '
    + '[contenteditable]:not([contenteditable="false"])';
// What scrolls sideways itself: code, tables, tab strips, anything that declares it.
export const SIDEWAYS_SELECTOR = 'pre, code, table, .md-table-wrap, .app-tab-strip, [data-scroll-x]';
// What lies over the room and owns the finger while it is open (shown, not merely mounted): a dialog, the reader,
// a menu, the Shade. A Project open on the phone is the room: the drag opens the drawer over it, as ☰ does.
export const BLOCKING_SELECTOR = 'dialog[open], .marketplace-modal-backdrop, .ui-popup, .task-control-menu, '
    + '.chat-photo-menu, .rw-shade:not([hidden]), details.chat-header-more[open]';

const clamp = (value) => Math.min(1, Math.max(0, value));

export function bindDrawerDrag({
    sidebar, scrim, isOpen, setOpen, isStack, isCoarse,
    win = globalThis.window, doc = globalThis.document, enabled = DRAWER_DRAG,
    // The eligibility of a start, injectable for tests; the defaults read the real DOM.
    isField = (el) => Boolean(el?.closest?.(FIELD_SELECTOR)),
    isSideways = (el) => Boolean(el?.closest?.(SIDEWAYS_SELECTOR)),
    isBlocked = () => [...(doc?.querySelectorAll?.(BLOCKING_SELECTOR) || [])].some((el) => el.getClientRects?.().length),
    hasSelection = () => Boolean(String(win?.getSelection?.()?.toString?.() || '').length),
} = {}) {
    if (!enabled || !sidebar || !scrim) return () => {};
    let g = null;
    let suppressUntil = 0;
    const now = () => (typeof win?.performance?.now === 'function' ? win.performance.now() : Date.now());

    // The chain under the finger: a `touch-action: none` element (the effort strip) or a scroller that can move
    // sideways keeps its gesture.
    function chainKeepsGesture(target) {
        for (let el = target; el && el !== doc.body; el = el.parentElement) {
            const cs = win.getComputedStyle?.(el);
            if (!cs) continue;
            const touch = String(cs.touchAction || '');
            if (touch === 'none' || touch.includes('pan-x')) return true;
            if (/(auto|scroll)/.test(String(cs.overflowX || '')) && el.scrollWidth > el.clientWidth + 1) return true;
        }
        return false;
    }

    function eligibleOpenStart(event) {
        const target = event.target;
        if (sidebar.contains?.(target) || scrim.contains?.(target)) return false;
        if (isField(target) || isSideways(target) || hasSelection()) return false;
        if (isBlocked()) return false;
        if (chainKeepsGesture(target)) return false;
        return true;
    }

    function eligibleCloseStart(event) {
        const target = event.target;
        if (!(sidebar.contains?.(target) || scrim.contains?.(target))) return false;
        return !target?.closest?.('input, textarea, select, [role="slider"]');
    }

    const setProgress = (p) => {
        sidebar.style.setProperty('--drawer-p', String(p));
        scrim.style.setProperty('--drawer-p', String(p));
        g.p = p;
    };
    const paint = (on) => {
        for (const el of [sidebar, scrim]) {
            el.classList.toggle('dragging', on);
            if (!on) el.style.removeProperty('--drawer-p');
        }
    };

    function onDown(event) {
        if (g && event.pointerId !== g.id) { restore(); return; }  // a second finger: back to where we started
        if (!event.isPrimary || event.pointerType !== 'touch') return;
        if (!isStack() || !isCoarse()) return;
        const open = Boolean(isOpen());
        if (open ? !eligibleCloseStart(event) : !eligibleOpenStart(event)) return;
        g = { id: event.pointerId, startOpen: open, x0: event.clientX, y0: event.clientY, mode: 'wait', samples: [],
            lockX: 0, width: 0, p: open ? 1 : 0, viewWidth: win?.innerWidth || 0 };
    }

    function onMove(event) {
        if (!g || event.pointerId !== g.id) return;
        const dx = event.clientX - g.x0;
        const dy = event.clientY - g.y0;
        if (g.mode === 'wait') {
            if (Math.abs(dy) >= DRAG.slop && Math.abs(dy) >= Math.abs(dx)) { g = null; return; }  // the page scrolls
            if (Math.abs(dx) < DRAG.slop || Math.abs(dx) <= DRAG.axisRatio * Math.abs(dy)) return;
            if ((dx > 0) === g.startOpen) { g = null; return; }  // the wrong way: nothing to move
            g.mode = 'drag';
            g.lockX = event.clientX;
            g.width = sidebar.getBoundingClientRect?.().width || 320;
            // The drawer holds the finger itself: the row under it may be re-rendered (a live card, a new message)
            // and would take the capture with it.
            try { sidebar.setPointerCapture?.(g.id); } catch { /* a synthetic pointer */ }
            if (!g.startOpen) scrim.hidden = false;
            paint(true);
            setProgress(g.startOpen ? 1 : 0);
            return;
        }
        const t = event.timeStamp ?? now();
        g.samples.push([t, event.clientX]);
        g.samples = g.samples.filter((sample) => t - sample[0] <= DRAG.sampleWindow);
        setProgress(clamp((g.startOpen ? 1 : 0) + (event.clientX - g.lockX) / g.width));
    }

    function onUp(event) {
        if (!g || event.pointerId !== g.id) return;
        const gg = g;
        g = null;
        if (gg.mode !== 'drag') return;  // a tap, not a drag
        try { sidebar.releasePointerCapture?.(gg.id); } catch { /* released already */ }
        const first = gg.samples[0];
        const last = gg.samples[gg.samples.length - 1];
        const velocity = first && last && last[0] > first[0] ? (last[1] - first[1]) / (last[0] - first[0]) : 0;
        const travel = Math.abs(event.clientX - gg.x0);
        let open;
        if (Math.abs(velocity) >= DRAG.flingVelocity && travel >= DRAG.minTravel) open = velocity > 0;
        else open = gg.startOpen ? gg.p > 1 - DRAG.closePast : gg.p >= DRAG.openPast;
        paint(false);
        if (open !== gg.startOpen) setOpen(open);
        else if (!open) scrim.hidden = true;  // sprang back closed
        suppressUntil = now() + DRAG.clickGuard;
    }

    /** Multitouch, pointercancel, the drawer losing the finger, a new width: back to the start, no history change. */
    function restore() {
        if (!g) return;
        const gg = g;
        g = null;
        if (gg.mode !== 'drag') return;
        try { sidebar.releasePointerCapture?.(gg.id); } catch { /* gone */ }
        paint(false);
        if (!gg.startOpen) scrim.hidden = true;
    }

    // Only the drawer's own capture counts: the element first under the finger gives up its implicit capture when
    // the drawer takes it, or when a re-render removes it.
    function onLost(event) {
        if (g && event.pointerId === g.id && event.target === sidebar) restore();
    }
    // A height-only change is a browser bar or the keyboard moving (Safari, Telegram); a new width is a new layout.
    function onResize() {
        if (g && (win?.innerWidth || 0) !== g.viewWidth) restore();
    }

    // The click that ends a drag is not a tap on whatever lies under the finger.
    function onClick(event) {
        if (now() >= suppressUntil) return;
        suppressUntil = 0;
        event.stopPropagation();
        event.preventDefault();
    }

    doc.addEventListener('pointerdown', onDown, { passive: true });
    doc.addEventListener('pointermove', onMove, { passive: true });
    doc.addEventListener('pointerup', onUp);
    doc.addEventListener('pointercancel', restore);
    doc.addEventListener('lostpointercapture', onLost);
    doc.addEventListener('click', onClick, true);
    win.addEventListener('resize', onResize);
    return () => {
        restore();
        doc.removeEventListener('pointerdown', onDown);
        doc.removeEventListener('pointermove', onMove);
        doc.removeEventListener('pointerup', onUp);
        doc.removeEventListener('pointercancel', restore);
        doc.removeEventListener('lostpointercapture', onLost);
        doc.removeEventListener('click', onClick, true);
        win.removeEventListener('resize', onResize);
    };
}
