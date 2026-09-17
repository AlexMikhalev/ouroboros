// Read-only question presentation. Task liveness and answerability are separate.
// Python's Project-pointer fallback is pinned by question_presentation_parity.json.
export function questionPresentation(row = {}) {
    const state = row.quiz_state || row.state || 'unknown';
    const resumed = row.owner_wait_state === 'resumed' || Boolean(row.wait_ended_at);
    const waiting = !resumed && (row.owner_wait_state === 'waiting'
        || (!row.owner_wait_state && row.wait_for_answer === true));
    const status = state === 'answered' ? 'You answered'
        : state === 'superseded' ? 'Replaced by a newer question'
            : state === 'expired_terminal' ? 'Task finished — you can still answer'
                : state === 'open' ? (resumed ? 'Task continued — you can still answer'
                    : waiting ? 'Waiting for your answer' : 'Question open — you can answer')
                    : 'Question status unavailable';
    const action = state === 'answered' ? 'View answer'
        : state === 'superseded' ? 'View previous question'
            : ['open', 'expired_terminal'].includes(state) ? 'Answer question' : 'View question';
    return { status, action };
}

export function questionPreview(row = {}) {
    const option = Number.isInteger(row.answered_index) ? row.options?.[row.answered_index] : null;
    const selected = typeof option === 'string' ? option : option?.label;
    const answer = row.state === 'answered' || row.quiz_state === 'answered'
        ? [selected, row.comment].filter(Boolean).join(' — ') : '';
    const excerpt = (text) => {
        const value = String(text || '').replace(/\s+/g, ' ').trim();
        return value.length > 280 ? `${value.slice(0, 280)}… (preview; open for full text)` : value;
    };
    return { question: excerpt(row.question), answer: excerpt(answer) };
}
