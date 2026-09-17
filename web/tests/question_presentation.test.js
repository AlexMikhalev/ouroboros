import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { excerpt, questionPresentation, questionPreview, waitFacts } from '../modules/question_presentation.js';

// Each fixture row is the pointer row the Python producer emits for that case
// (tests/test_project_question_pointer.py pins the emission); the browser must read the
// same status and action out of it.
const cases = JSON.parse(readFileSync(new URL('./fixtures/question_presentation_parity.json', import.meta.url)));
for (const row of cases) test(`question status parity: ${row.case}`, () => {
    assert.deepEqual(questionPresentation(row.row), { status: row.status, action: row.action });
});

test('waiting needs positive evidence and a closed bound ends it', () => {
    assert.deepEqual(waitFacts({ wait_for_answer: true }), { waiting: true, resumed: false });
    assert.deepEqual(waitFacts({ wait_for_answer: true, owner_wait_state: 'resumed' }), { waiting: false, resumed: true });
    assert.deepEqual(waitFacts({ wait_for_answer: true, wait_ended_at: 'x' }), { waiting: false, resumed: true });
    assert.deepEqual(waitFacts({ owner_wait_state: 'waiting' }), { waiting: true, resumed: false });
    assert.deepEqual(waitFacts({}), { waiting: false, resumed: false });
});

test('previews are visibly bounded, never cut for less than the marker, and never interpret markup', () => {
    const long = 'a'.repeat(400);
    const preview = questionPreview({ question: long, state: 'answered',
        options: ['<b>First</b>', 'Second'], answered_index: 0, comment: 'Exact comment' });
    assert.match(preview.question, /… \(preview; open for full text\)$/);
    assert.equal(preview.answer, '<b>First</b> — Exact comment');
    // A 285-character text would grow if cut: it stays whole.
    assert.equal(excerpt('b'.repeat(285)), 'b'.repeat(285));
    // The option and the comment are bounded separately: a long label never hides the comment.
    const both = questionPreview({ state: 'answered', options: [long], answered_index: 0, comment: 'Still here' });
    assert.match(both.answer, /open for full text\) — Still here$/);
    assert.equal(questionPreview({ state: 'open', comment: 'draft' }).answer, '');
    assert.equal(questionPreview({ quiz_state: 'answered', comment: 'Only words' }).answer, 'Only words');
});
