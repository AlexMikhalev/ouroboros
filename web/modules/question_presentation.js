// Read-only presentation of one owner question, shared by the Main row and the
// quiz card header. Task liveness and answerability are separate facts: a status
// leads with one word that answers "is there an unanswered question for me?" and
// keeps the lifecycle context after it. The Python pointer fallback
// (ouroboros/project_dialogue.py::QUESTION_STATUS) emits the same words; the shared
// fixture web/tests/fixtures/question_presentation_parity.json pins both sides on the
// rows Python actually emits.
const STATUS = {
    waiting: 'Waiting for your answer',
    open: 'Unanswered · an answer is still accepted',
    resumed: 'Unanswered · the task continued; an answer is still accepted',
    expired_terminal: 'Unanswered · the task finished; a late answer is accepted as your message',
    answered: 'You answered',
    superseded: 'Replaced by a newer question',
    unknown: 'Status unavailable',
};
// The closed lifecycle set, and the states that still take an answer (only a settled one —
// answered/superseded — turns a card into a pure record). One JS home for both lists.
export const QUIZ_LIFECYCLE = ['open', 'answered', 'expired_terminal', 'superseded'];
export const ANSWERABLE_QUIZ_STATES = ['open', 'expired_terminal'];
// A passed optional question leads with the path the task took instead of a bare status.
const CONTINUING = 'Unanswered · continuing with:';
const PREVIEW_CHARS = 280;
const PREVIEW_MARK = '… (preview; open for full text)';

// Waiting needs positive evidence: the task's live wait record, or the original
// required flag before any record exists. A resumed record or a closed bound ends it.
export function waitFacts(row = {}) {
    const resumed = row.owner_wait_state === 'resumed' || Boolean(row.wait_ended_at);
    const waiting = !resumed && (row.owner_wait_state === 'waiting'
        || (!row.owner_wait_state && row.wait_for_answer === true));
    return { waiting, resumed };
}

export function questionPresentation(row = {}) {
    const state = row.quiz_state || row.state || 'unknown';
    const { waiting, resumed } = waitFacts(row);
    const key = !QUIZ_LIFECYCLE.includes(state) ? 'unknown' : state !== 'open' ? state
        : waiting ? 'waiting' : resumed ? 'resumed' : 'open';
    return { status: STATUS[key] };
}

// A cut that saves fewer characters than its own marker is pure damage: such text stays whole.
export function excerpt(text) {
    const value = String(text || '').replace(/\s+/g, ' ').trim();
    return value.length > PREVIEW_CHARS + PREVIEW_MARK.length ? `${value.slice(0, PREVIEW_CHARS)}${PREVIEW_MARK}` : value;
}

export function questionPreview(row = {}) {
    const option = Number.isInteger(row.answered_index) ? row.options?.[row.answered_index] : null;
    const selected = typeof option === 'string' ? option : option?.label;
    // The option and the comment are bounded separately: a long label never hides the comment.
    const answer = (row.quiz_state || row.state) === 'answered'
        ? [selected, row.comment].filter(Boolean).map(excerpt).join(' — ') : '';
    return { question: excerpt(row.question), answer };
}

// The Main row of one Project question: status words, then the owner's answer or the
// assumption the task continues under, then the question as context. `waiting` is the one
// state that grows the row into a card with the option buttons.
export function questionRow(row = {}) {
    const state = row.quiz_state || row.state || 'unknown';
    const { answer, question } = questionPreview(row);
    const assumption = state === 'open' ? excerpt(row.assumption) : '';
    return {
        waiting: state === 'open' && waitFacts(row).waiting,
        lead: answer ? `${STATUS.answered}:` : assumption ? CONTINUING : questionPresentation(row).status,
        detail: answer || assumption,
        question,
    };
}
