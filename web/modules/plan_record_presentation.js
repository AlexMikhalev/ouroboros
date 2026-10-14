// The planning organ's record (plan_record, schema 3) as one Reviews group: revisions,
// asks, participants, contributions, open objections and done_when. Nobody votes and no
// verdict exists, so the group reports facts — the current revision, how many objections
// stand open (and whether an owner-marked reviewer's may hold) and whether answers are still
// on their way. The retired organ's v1/v2 records keep their read-only branches in
// review_presentation.js.

const text = (value) => String(value ?? '').trim();

export const planRecordEngaged = (record) => record && typeof record === 'object'
    && ((Array.isArray(record.revisions) && record.revisions.length) || (Array.isArray(record.asks) && record.asks.length));

function planObjections(record) {
    // The fold the host applies (plan_record.fold_objections): an objection stands until the
    // SAME seat lists it under withdrawn; a reply that mentions neither flags it; a foreign
    // withdrawal changes nothing.
    const open = new Map();
    for (const ask of (Array.isArray(record.asks) ? record.asks : [])) {
        for (const participant of (Array.isArray(ask.participants) ? ask.participants : [])) {
            if (!participant || participant.status !== 'answered' || !participant.reply) continue;
            const seat = text(participant.seat_id);
            const mentioned = new Set();
            for (const item of (Array.isArray(participant.reply.objections) ? participant.reply.objections : [])) {
                const key = `${seat}:${text(item.id)}`;
                mentioned.add(key);
                open.set(key, { seat, id: text(item.id), text: text(item.text), askId: text(ask.ask_id), revision: ask.revision,
                    holds: participant.owner_marked === true, notAdjudicated: false });
            }
            for (const item of (Array.isArray(participant.reply.withdrawn) ? participant.reply.withdrawn : [])) {
                const key = `${seat}:${text(item.id)}`;
                mentioned.add(key);
                if (open.has(key)) open.delete(key);
            }
            for (const [key, objection] of open) {
                if (objection.seat === seat && !mentioned.has(key)) objection.notAdjudicated = true;
            }
        }
    }
    return [...open.values()];
}

function planDoneWhenLines(revision) {
    const rows = Array.isArray(revision?.done_when) ? revision.done_when : [];
    if (!rows.length) return ['done_when: (none stated yet)'];
    return ['done_when:', ...rows.map((row) => `  ${text(row.id)}: ${text(row.claim)}${text(row.support) ? ` — shown by: ${text(row.support)}` : ''}`)];
}

export function planRecordGroup(owner, record, detail) {
    const revisions = (Array.isArray(record.revisions) ? record.revisions : []).filter((r) => r && typeof r === 'object');
    const asks = (Array.isArray(record.asks) ? record.asks : []).filter((a) => a && typeof a === 'object');
    const latest = revisions.at(-1) || null;
    const objections = planObjections(record);
    const holding = objections.filter((o) => o.holds);
    const live = text(detail?.status).toLowerCase() === 'running';
    const attempts = [];
    if (latest) {
        attempts.push({
            id: `revision:${latest.n}`, surface: 'plan', state: 'terminal', tone: 'neutral',
            verdict: `revision ${latest.n}${latest.body ? ' · body plan' : ''}`,
            timestamp: text(latest.ts), ordinal: 0, label: `plan revision ${latest.n} of ${revisions.length}`,
            summary: text(latest.goal), superseded: false, compact: false, replayed: false, revised: revisions.length > 1,
            initiatorTaskId: '', executions: [], execution: null, detailRef: null,
            detailText: [
                `goal: ${text(latest.goal) || '(none)'}`,
                ...planDoneWhenLines(latest),
                `affected_paths: ${JSON.stringify(Array.isArray(latest.affected_paths) ? latest.affected_paths : [])}`,
                latest.body ? `body plan: ${text(latest.body_note) || 'changes Ouroboros\'s own body'}` : '',
                ...revisions.slice(0, -1).map((r) => `earlier revision ${r.n} (${text(r.ts) || '?'}): ${
                    (Array.isArray(r.done_when) ? r.done_when : []).map((c) => `${text(c.id)}: ${text(c.claim)}`).join('; ') || 'no done_when'}`),
                'Cost unavailable',
            ].filter(Boolean).join('\n'),
        });
    }
    asks.forEach((ask, index) => {
        const participants = (Array.isArray(ask.participants) ? ask.participants : []).filter((p) => p && typeof p === 'object');
        const pending = participants.filter((p) => p.status === 'pending');
        const answered = participants.filter((p) => p.status === 'answered');
        const raised = objections.filter((o) => o.askId === text(ask.ask_id));
        const running = pending.length > 0 && live;
        const lines = [
            `ask ${text(ask.ask_id)} on revision ${ask.revision}${text(ask.message) ? ` — «${text(ask.message)}»` : ''}`,
            ...(Array.isArray(ask.host_added) && ask.host_added.length
                ? [`first ask on a body plan: the host added ${ask.host_added.join(', ')}`] : []),
            ...participants.map((p) => {
                const who = `${text(p.seat_id)} (${text(p.model) || '?'}${p.owner_marked ? '' : ', added — never holds'})`;
                if (p.status === 'answered') {
                    const reply = p.reply || {};
                    const n = (Array.isArray(reply.objections) ? reply.objections : []).length;
                    const w = (Array.isArray(reply.withdrawn) ? reply.withdrawn : []).length;
                    const prose = text(reply.prose);
                    return `${who}: answered — ${n} objection${n === 1 ? '' : 's'}${w ? `, ${w} withdrawn` : ''}${prose ? `: ${prose}` : ''}`;
                }
                if (p.status === 'not_dispatched') return `${who}: not sent — ${text(p.not_dispatched_reason) || 'not sendable'}`;
                if (p.status === 'pending') return `${who}: awaiting`;
                return `${who}: ${text(p.status)}${text(p.error || p.parse_error) ? ` — ${text(p.error || p.parse_error)}` : ''}`;
            }),
            ...raised.map((o) => `objection ${o.seat}/${o.id}${o.holds ? '' : ' (never holds)'}${o.notAdjudicated ? ' (not adjudicated)' : ''}: ${o.text}`),
            'Cost unavailable',
        ];
        attempts.push({
            id: `ask:${text(ask.ask_id)}`, surface: 'plan', state: running ? 'running' : 'terminal',
            progress: pending.length ? `${answered.length} of ${participants.length} answered` : '',
            tone: running ? 'working' : (raised.length ? 'warn' : 'done'),
            verdict: raised.length ? `${raised.length} objection${raised.length === 1 ? '' : 's'}` : (pending.length ? 'awaiting' : 'no objection'),
            timestamp: text(ask.ts), ordinal: index + 1, label: `ask ${text(ask.ask_id)} · revision ${ask.revision}`,
            summary: text(ask.message), superseded: false, compact: false, replayed: false, revised: false,
            initiatorTaskId: '', executions: [], execution: null, detailRef: null, detailText: lines.join('\n'),
        });
    });
    const awaiting = asks.some((ask) => (Array.isArray(ask.participants) ? ask.participants : []).some((p) => p?.status === 'pending'));
    const state = awaiting && live ? 'running' : 'terminal';
    const verdict = holding.length ? `${holding.length} objection${holding.length === 1 ? '' : 's'} may hold`
        : objections.length ? `${objections.length} open objection${objections.length === 1 ? '' : 's'}`
            : awaiting ? 'awaiting answers' : (asks.length ? 'no open objection' : 'not asked yet');
    return {
        id: `plan:${owner}`, surface: 'plan', label: 'Plan', subject: latest ? `revision ${latest.n}` : '',
        presentationOwnerTaskId: owner, subjectTaskId: owner, initiatorTaskId: owner,
        state, progress: '', tone: holding.length ? 'warn' : (objections.length ? 'warn' : (state === 'running' ? 'working' : 'done')),
        verdict, summary: text(latest?.goal), authorDecisionText: '',
        activeCount: state === 'running' ? 1 : 0, attemptCount: attempts.length, countIsAuthoritative: true, attempts,
    };
}
