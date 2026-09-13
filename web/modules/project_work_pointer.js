/** A navigation pointer to existing Project cards, never another work dashboard. */
import { compareHistoryPosition } from './chat_history_replay.js';

// The pointer names the card; it never restates the card's latest status line
// (a plan_task verdict runs to ~400 characters, and the card itself clamps its
// title to two lines). One line of text is the whole budget: the CSS ellipsizes
// the rest, and this cap keeps the accessible name short on every surface. The
// complete text stays on the card, one click away — no mouse-only tooltip.
export const POINTER_NAME_CHARS = 120;

export function projectWorkTarget(records = []) {
    const roots = records.filter(record => !record.isSubagent && record.root?.isConnected)
        .sort((a, b) => Number(a.root.dataset?.ts || 0) - Number(b.root.dataset?.ts || 0)
            || compareHistoryPosition(a.historyPosition, b.historyPosition));
    return roots.filter(record => !record.finished).at(-1) || roots.at(-1) || null;
}

/** `Working · <name>` / `Latest task · <name>`: the coined name first, else the
 *  card's own title, whitespace-normalized and capped to one line's worth. */
export function projectWorkLabel(target) {
    if (!target) return '';
    const name = String(target.suggestedName || target.titleEl?.textContent || '')
        .replace(/\s+/g, ' ').trim() || 'Task';
    const short = name.length > POINTER_NAME_CHARS
        ? `${name.slice(0, POINTER_NAME_CHARS - 1).trimEnd()}…`
        : name;
    return `${target.finished ? 'Latest task' : 'Working'} · ${short}`;
}

export function bindProjectWorkPointer(host, { records, getWindow, onNavigate }) {
    const doc = host.ownerDocument;
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-ghost project-work-pointer';
    const label = doc.createElement('span');
    label.className = 'project-work-pointer-label';
    button.append(label);
    const note = doc.createElement('span');
    note.className = 'project-work-coverage';
    let target = null;
    let disposed = false;
    const navigate = () => {
        update();
        if (target?.root?.isConnected) onNavigate(target.root);
    };
    function update() {
        if (disposed) return;
        target = projectWorkTarget([...records.values()]);
        const text = projectWorkLabel(target);
        if (label.textContent !== text) label.textContent = text;
        // Without a represented card the pointer leads nowhere and takes no
        // space; an absent card is not a claim that the Project has no work.
        button.hidden = !target;
        button.disabled = !target;
        // A represented card is not a claim that all Project work/history is loaded.
        const coverage = target && getWindow()?.complete !== true ? 'Loaded messages only' : '';
        if (note.textContent !== coverage) note.textContent = coverage;
        note.hidden = !coverage;
    }
    button.addEventListener('click', navigate);
    host.prepend(button, note);
    update();
    return {
        update,
        destroy() {
            disposed = true;
            button.removeEventListener('click', navigate);
            button.remove();
            note.remove();
            target = null;
        },
    };
}
