/** The composer's row of capsules folds while the owner reads back through a room's history (owner 19.10, 2A;
 * docs/DESIGN.md "Composer owner controls").
 *
 * On a phone (the stack layout) the row (☰, Swarm, Context, Effort) takes its own line above the field. While the
 * owner scrolls back through the history it folds away: the composer keeps only the field, and ☰, still the row's
 * first control, waits beside it. Scrolling toward the latest, reaching it, a press on the composer, focusing the
 * field or opening a capsule brings the row back, and so does any other layout. Like Safari's bars, a direction decides: FOLD.run px one way.
 *
 * The direction is read from where the newest row stands below the feed's visible bottom, not from the scroll
 * position: older history prepended above keeps the reader's place (and so the newest row's), and the room the
 * composer reserves after the newest row moves nothing, so neither folds the row nor brings it back by itself.
 * style.css draws the fold from two facts on the composer: the row's measured height (`--fold-row-h`, so the line
 * collapses from what it was) and how far ☰ goes down to the field's middle (`--fold-menu-dy`); `data-fold`
 * holds from the fold until the row is back, `data-folded` while it is folded. */

import { isStackLayout, onLayoutChange } from './layout.js';

export const FOLD = Object.freeze({
    run: 40,      // px read back (or forward) in one direction before the row folds (or comes back)
    away: 80,     // px the newest row lies below the visible bottom before the row may fold
    latest: 24,   // px from the scroll's end: the row is always there
    settle: 340,  // ms of the fold's own motion (style.css), after which an unfolded row lets go of its height
});

export function createComposerFold({ feed, wrap, row, win = globalThis.window, doc = globalThis.document,
    isStack = () => isStackLayout(), onLayout = (sync) => onLayoutChange(() => sync(), { w: win }) } = {}) {
    if (!feed || !wrap || !row) return { destroy() {}, folded: () => false };
    let destroyed = false;
    let folded = false;
    let last = null;
    let run = 0;
    let releaseTimer = 0;
    let frame = 0;
    const text = wrap.querySelector?.('.chat-text-row');

    // How far the newest row lies below the feed's visible bottom (negative: above it). The newest row with a box:
    // the typing indicator sits last and is hidden most of the time.
    function away() {
        let newest = feed.lastElementChild;
        while (newest && !newest.getClientRects?.().length) newest = newest.previousElementSibling;
        if (!newest?.getBoundingClientRect) return null;
        return newest.getBoundingClientRect().bottom - feed.getBoundingClientRect().bottom;
    }
    const atLatest = () => feed.scrollHeight - feed.clientHeight - feed.scrollTop < FOLD.latest;
    // While the owner writes (the keyboard is up: app.js's keyboard-open), or works a capsule, the row stays. A field
    // that merely holds the focus is not writing: a room focuses it when it opens, with no keyboard on a phone.
    const busy = () => Boolean(doc?.body?.classList?.contains?.('keyboard-open'))
        || Boolean(row.dataset?.ownerSolo) || Boolean(row.querySelector?.('[data-open="true"]'));

    function release() {
        delete wrap.dataset.fold;
        wrap.style.removeProperty('--fold-row-h');
        wrap.style.removeProperty('--fold-menu-dy');
    }

    function fold() {
        if (folded) return;
        folded = true;
        if (releaseTimer) { win?.clearTimeout?.(releaseTimer); releaseTimer = 0; }
        // The line collapses from the height it has: measured and held before the fold starts.
        if (!('fold' in wrap.dataset)) {
            const line = row.getBoundingClientRect();
            wrap.style.setProperty('--fold-row-h', `${Math.ceil(line.height)}px`);
            // Folded, the line starts where the field does, and ☰ keeps its place on the line: it goes down by half
            // the field's height less its own centre's depth on the line, to the field's middle.
            const menu = row.querySelector?.(':scope > .composer-menu')?.getBoundingClientRect?.();
            const field = text?.getBoundingClientRect?.();
            if (menu && field) {
                wrap.style.setProperty('--fold-menu-dy', `${Math.round(field.height / 2 - (menu.top + menu.height / 2 - line.top))}px`);
            }
            wrap.dataset.fold = '';
            void wrap.offsetHeight;  // the held height is the transition's start
        }
        wrap.dataset.folded = '';
    }

    function unfold() {
        if (!folded) return;
        folded = false;
        delete wrap.dataset.folded;
        // Once back, the row lets go of its held height (a strip that opens, a longer name, a larger text).
        releaseTimer = win?.setTimeout?.(() => { releaseTimer = 0; if (!folded) release(); }, FOLD.settle) || 0;
    }

    function read() {
        frame = 0;
        if (destroyed) return;
        const now = away();
        const prev = last;
        last = now;
        if (!isStack?.() || busy()) { run = 0; unfold(); return; }
        if (atLatest()) { run = 0; unfold(); return; }
        if (now === null || prev === null) return;
        const step = now - prev;  // > 0: the newest row went further down, the owner reads back
        if (!step) return;
        run = Math.sign(step) === Math.sign(run) ? run + step : step;
        if (run >= FOLD.run && now > FOLD.away) fold();
        else if (run <= -FOLD.run) unfold();
    }
    // One read per frame, however many scroll events the frame brings.
    const onScroll = () => {
        if (frame || destroyed) return;
        frame = typeof win?.requestAnimationFrame === 'function' ? win.requestAnimationFrame(read) : (read(), 0);
    };
    // A press on the composer (the field the owner is about to write in, which may hold the focus already) or its
    // focus brings the row back.
    const onFocus = () => { run = 0; unfold(); };

    feed.addEventListener('scroll', onScroll, { passive: true });
    wrap.addEventListener('focusin', onFocus);
    wrap.addEventListener('pointerdown', onFocus);
    const unwatchLayout = onLayout(() => { run = 0; last = away(); unfold(); });

    return {
        folded: () => folded,
        destroy() {
            if (destroyed) return;
            destroyed = true;
            feed.removeEventListener('scroll', onScroll);
            wrap.removeEventListener('focusin', onFocus);
            wrap.removeEventListener('pointerdown', onFocus);
            if (frame && typeof win?.cancelAnimationFrame === 'function') win.cancelAnimationFrame(frame);
            if (releaseTimer) win?.clearTimeout?.(releaseTimer);
            unwatchLayout?.();
            delete wrap.dataset.folded;
            release();
        },
    };
}
