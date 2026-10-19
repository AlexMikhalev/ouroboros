// The row menu of one chat feed (docs/DESIGN.md "Replies"): what a bubble or a root work card offers, reached
// without hover — a right-click, Shift+F10 or the ContextMenu key on the focused row, a long press (no movement)
// on touch (row_menu_gesture.js). Its items are the row's own doors, never second ones: Reply, Copy, Open project
// (when the row has one) and, on a work card, Resume / Pause / Stop… exactly when the card itself offers them (Pause
// is the item of the card's Stop… menu, offered by the same rule). A work card's Reply lives only here (owner 17.10,
// Q9 B): the card's action row holds commands over the work. Esc and an outside press close it; keyboard focus
// enters the menu and returns to the row.
import { tr } from './i18n.js';
import { bindMenu } from './ui_interactions.js';
import { ACTION_PAUSE, pauseTaskAction } from './task_control_menu.js';
import { copyTextWithToast } from './ui_helpers.js';
import { fullText } from './text_fit.js';
import { bindRowMenuGesture } from './row_menu_gesture.js';

const ROW = '.chat-bubble, .chat-live-card:not(.subagent)';

const shown = (node) => Boolean(node?.isConnected && !node.hidden && !node.disabled);

/**
 * `feed` is the chat's transcript, `replies` its reply controller (chat_reply.js), `cardOf(taskId)` its root card
 * record, `actionsOf(record)` the items that card's own Stop… menu offers now (its work's controls),
 * `afterAction()` the re-read once an action settled. Returns `{ open, destroy }`.
 */
export function createChatRowMenu({ feed, replies, cardOf, actionsOf, afterAction = () => {} }) {
    const doc = feed.ownerDocument;
    let current = null;  // { menu, binding, row }

    const rowOf = (node) => {
        const row = node?.closest?.(ROW);
        return row && feed.contains(row) ? row : null;
    };

    function bubbleItems(row) {
        const items = [];
        const ref = replies.bubbleRef(row);
        if (ref) items.push(['reply', tr('chat.reply.reply', 'Reply'), () => replies.start(ref)]);
        const copy = row.querySelector(':scope > .chat-message-copy, :scope > .chat-message-actions > .chat-message-copy');
        if (copy) items.push(['copy', tr('code.copy', 'Copy'), () => copy.click(), true]);
        return items;
    }

    function cardItems(row, record) {
        const items = [];
        // A work card draws no Reply of its own (its action row holds only commands): the menu is its Reply.
        if (replies.cardRef(record)) items.push(['reply', tr('chat.reply.reply', 'Reply'), () => replies.start(replies.cardRef(record))]);
        const words = [record.titleEl?.textContent, fullText(record.activityEl)].map((text) => String(text || '').trim());
        if (words.some(Boolean)) items.push(['copy', tr('code.copy', 'Copy'),
            () => copyTextWithToast(words.filter(Boolean).join('\n')), true]);
        if (shown(record.resumeRunBtn)) items.push(['resume', tr('task.control.resume', 'Resume'), () => record.resumeRunBtn.click(), true]);
        if (shown(record.cancelRunBtn)) {
            // The card's own Stop… offers Pause on the same facts; its menu re-checks before any effect.
            if (actionsOf(record).includes(ACTION_PAUSE)) {
                items.push(['pause', tr('task.control.pause', 'Pause'),
                    () => pauseTaskAction(record.groupId).finally(afterAction), true]);
            }
            items.push(['stop', record.cancelRunBtn.textContent, () => record.cancelRunBtn.click()]);
        }
        return items;
    }

    function itemsFor(row) {
        const record = row.matches('.chat-live-card') ? cardOf(row.dataset.taskId || '') : null;
        const items = record ? cardItems(row, record) : bubbleItems(row);
        // The row's own Project reference (a portal's footer, the reference under an answer or a receipt).
        const reference = [...row.querySelectorAll('[data-intent="open-project"]')]
            .find((node) => shown(node) && rowOf(node) === row && !node.closest('.chat-live-card.subagent'));
        if (reference) items.splice(Math.min(items.length, 2), 0,
            ['project', tr('chat.row.open_project', 'Open project'), () => reference.click()]);
        return items;
    }

    function close(options) {
        current?.binding.close(options);
    }

    /** Open the menu of `row`, at a pointer `point` or under the focused `anchor`. False when the row offers nothing. */
    function open(row, { point, anchor = row } = {}) {
        close();
        const items = itemsFor(row);
        if (!items.length) return false;
        // Focus returns to the row: a bubble or card that is not itself focusable takes focus like a quote's target.
        if (anchor === row && row.tabIndex < 0 && !row.hasAttribute('tabindex')) row.tabIndex = -1;
        const menu = doc.createElement('div');
        menu.className = 'ui-popup chat-row-menu';
        menu.setAttribute('role', 'menu');
        menu.setAttribute('aria-label', row.matches('.chat-live-card') ? tr('chat.row.work_actions', 'Work actions')
            : tr('chat.row.message_actions', 'Message actions'));
        for (const [kind, label, run, keepFocus] of items) {
            const item = doc.createElement('button');
            item.type = 'button';
            item.setAttribute('role', 'menuitem');
            item.dataset.rowAction = kind;
            item.textContent = label;
            item.addEventListener('click', (event) => {
                event.stopPropagation();
                // An item that moves focus on its own (Reply to the composer, Stop… into its menu) is not given it back.
                close({ restoreFocus: Boolean(keepFocus), reason: 'action' });
                run();
            });
            menu.append(item);
        }
        doc.body.append(menu);
        const binding = bindMenu(menu, { anchor, point, onClose: () => {
            menu.remove();
            if (current?.menu === menu) current = null;
        } });
        current = { menu, binding, row };
        return true;
    }

    const unbindGesture = bindRowMenuGesture({ root: feed, rowOf, open, isOpen: (row) => current?.row === row });

    return {
        open,
        destroy() {
            unbindGesture();
            close();
        },
    };
}
