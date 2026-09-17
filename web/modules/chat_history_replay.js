/** Historical rows enrich the existing timeline without acquiring live authority. */
export function compareHistoryPosition(left, right) {
    if (!left || !right) return 0;
    const stream = String(left.source || '').localeCompare(String(right.source || ''));
    return stream || Number(left.offset) - Number(right.offset);
}

export function historyRowIds(row) {
    return [row.history_id, ...(row.review_group?.attempts || []).map(attempt => attempt.history_id)].filter(Boolean);
}

export function stampHistoryNode(node, id, position) {
    if (!node || !id) return;
    node.dataset.historyId = id;
    if (position) {
        node.dataset.historySource = position.source;
        node.dataset.historyOffset = String(position.offset);
    }
}

export function mergeHistoricalTimelineItem(record, summary, row, ts) {
    if (summary.visible === false || !(summary.headline || summary.body)) return false;
    const identity = String(row?.history_id || '');
    if (!identity) return false;
    // A terminal projection is still one logical completion; narration consists
    // of independently addressed source records, even when text and time match.
    const key = summary.terminal ? summary.dedupeKey : `history:${identity}`;
    let item = record.items.find((entry) => entry.dedupeKey === key);
    if (!item && !summary.terminal) {
        item = record.items.find((entry) => !entry.historyId
            && entry.dedupeKey === summary.dedupeKey && entry.sourceTs === row.ts);
    }
    if (item && summary.terminal) {
        const incomingTime = Date.parse(row.ts), existingTime = Date.parse(item.sourceTs || '');
        if (incomingTime < existingTime || incomingTime === existingTime
                && compareHistoryPosition(row.history_position, item.historyPosition) < 0) return false;
        const update = { headline: summary.headline || item.headline,
            fullHeadline: summary.fullHeadline || summary.headline || item.fullHeadline,
            body: summary.body || '', fullBody: summary.fullBody || summary.body || '',
            phase: summary.phase || item.phase, sourceTs: row.ts || item.sourceTs,
            ts, sourceHistoryId: identity, historyPosition: row.history_position };
        if (Object.entries(update).every(([key, value]) => key === 'historyPosition'
            ? JSON.stringify(item[key]) === JSON.stringify(value) : item[key] === value)) return false;
        Object.assign(item, update);
        return true;
    }
    if (item?.historyId === identity) return false;
    if (item) {
        item.historyId = identity;
        item.historyPosition = row.history_position;
        item.dedupeKey = key;
        item.count = 1;
    } else {
        record.items.push({
            phase: summary.phase || 'working', headline: summary.headline || 'Update',
            fullHeadline: summary.fullHeadline || summary.headline || 'Update',
            body: summary.body || '', fullBody: summary.fullBody || summary.body || '',
            fullRef: summary.fullRef || '', truncated: summary.truncated || false,
            ts: ts || '', sourceTs: row.ts || '', count: 1, dedupeKey: key,
            ...(summary.terminal ? { sourceHistoryId: identity } : { historyId: identity }),
            historyPosition: row.history_position,
            lineKey: summary.terminal ? `terminal-${String(key).replace(/[^A-Za-z0-9_-]/g, '-')}`
                : `history-${identity.replace(/[^A-Za-z0-9_-]/g, '-')}`,
        });
    }
    record.items.sort((a, b) => {
        const first = Date.parse(a.sourceTs || ''), second = Date.parse(b.sourceTs || '');
        return (Number.isFinite(first) && Number.isFinite(second) ? first - second : 0)
            || compareHistoryPosition(a.historyPosition, b.historyPosition);
    });
    return true;
}

/** A page can leave the rendered window only outside reading, focus and selection. */
export function historyNodeIsProtected(node, viewport, selection = globalThis.getSelection?.()) {
    if (!node?.isConnected) return false;
    const active = node.ownerDocument?.activeElement;
    if (active && node.contains(active)) return true;
    if (selection && !selection.isCollapsed) {
        for (let index = 0; index < selection.rangeCount; index += 1) {
            try { if (selection.getRangeAt(index).intersectsNode(node)) return true; } catch {}
        }
    }
    const bounds = viewport.getBoundingClientRect(), rect = node.getBoundingClientRect();
    return Boolean(node.getClientRects().length && rect.bottom > bounds.top && rect.top < bounds.bottom);
}
