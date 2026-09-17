import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { questionPresentation, questionPreview } from '../modules/question_presentation.js';
const cases = JSON.parse(readFileSync(new URL('./fixtures/question_presentation_parity.json', import.meta.url)));
for (const row of cases) test(`question status parity: ${JSON.stringify(row)}`, () => {
    assert.deepEqual(questionPresentation(row), { status: row.status, action: row.action });
});
test('preview bounds are visible and option/comment text is never interpreted as markup', () => {
    const preview = questionPreview({ question: 'a'.repeat(400), state: 'answered',
        options: ['<b>First</b>', 'Second'], answered_index: 0, comment: 'Exact comment' });
    assert.match(preview.question, /… \(preview; open for full text\)$/);
    assert.equal(preview.answer, '<b>First</b> — Exact comment');
    assert.equal(questionPreview({ state: 'open', comment: 'draft' }).answer, '');
});
