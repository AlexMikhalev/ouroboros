// Skills → Sources rows and the dialogs' shared trust cards: the trust value is a
// setting, health is live evidence, and the first-start rows show their suggestion.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { renderSourceRow, sourceHealthHtml, unconfirmedHubRows } from '../modules/skill_sources_view.js';
import { choiceCardsHtml, trustChoices } from '../modules/skill_dialogs.js';

const fixture = (name) => JSON.parse(readFileSync(new URL(`./fixtures/skills_v4/${name}`, import.meta.url), 'utf8'));
const SOURCES = fixture('skill_sources.json').sources;
const CATALOG = fixture('skill_catalog.json');
const source = (id) => SOURCES.find((row) => row.id === id);
const health = (id) => CATALOG.sources.find((row) => row.id === id);
const items = (id) => CATALOG.items.filter((item) => item.source_id === id);
const text = (html) => html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();

test('the official hub row carries the Official glyph; the folder row says Folder; counts come from the catalog', () => {
    const official = renderSourceRow(source('ouroboros'), { items: items('ouroboros'), health: health('ouroboros') });
    assert.match(official, /<strong>Ouroboros<\/strong><button type="button" class="skills-mark"/);
    assert.match(text(official), /github\.com\/razzant\/OuroborosHub · 4 skills · read .* ago/);
    assert.match(text(official), /The Ouroboros project’s own skills, marked Official by its maintainer\./);
    const community = renderSourceRow(source('ouroboros-community'), { items: items('ouroboros-community'), health: health('ouroboros-community') });
    assert.doesNotMatch(community, /data-official-mark/);
    assert.match(text(community), /4 skills by 4 authors/);
    const folder = renderSourceRow(source('my-folder'), { items: items('my-folder'), health: health('my-folder') });
    assert.match(folder, /data-tone="neutral">Folder</);
    assert.match(text(folder), /~\/ouroboros-skills · 1 skill/);
});

test('trust is a setting on the right, marked suggested until the owner confirms the hub rows', () => {
    const suggested = renderSourceRow(source('ouroboros'), {});
    assert.match(suggested, /class="skills-trust-value is-suggested" data-source-action="trust" data-source-id="ouroboros"[^>]*>Installs without review \(suggested\) ›</);
    const confirmed = renderSourceRow(source('clawhub'), {});
    assert.match(confirmed, /class="skills-trust-value" data-source-action="trust"[^>]*>Reviews each skill ›</);
    assert.deepEqual(unconfirmedHubRows(SOURCES).map((row) => row.id), ['ouroboros', 'ouroboros-community']);
    assert.deepEqual(unconfirmedHubRows(SOURCES.map((row) => ({ ...row, confirmed: true }))), []);
});

test('health is a status sentence: read, reading, couldn’t be read with Try again, registry on demand', () => {
    assert.match(text(sourceHealthHtml(source('ouroboros'), { state: 'ready', last_read_at: new Date(Date.now() - 120_000).toISOString() })), /^read 2 min ago$/);
    assert.match(sourceHealthHtml(source('ouroboros'), { state: 'reading' }), /data-work/);
    const failed = sourceHealthHtml(source('ouroboros-community'), health('ouroboros-community'));
    assert.match(failed, /data-tone="warn"/);
    assert.match(text(failed), /couldn’t be read: GitHub didn&#39;t answer — showing the list from .* ago Try again/);
    assert.match(failed, /data-source-action="refresh" data-source-id="ouroboros-community"/);
    assert.equal(text(sourceHealthHtml(source('clawhub'), { state: 'ready', last_read_at: null })), 'searched when you search');
    assert.equal(text(sourceHealthHtml(source('ouroboros'), null)), 'not read yet');
});

test('the trust cards say what each choice costs: review sends files to review models, trust hands out keys', () => {
    const [review, trusted] = trustChoices();
    assert.equal(review.label, 'Review each skill before it runs');
    assert.match(review.detail, /A review sends the skill’s files to your review models\./);
    assert.equal(trusted.label, 'Trust this source — install without review');
    assert.match(trusted.detail, /While automatic keys are on, they get the keys they ask for\./);
    const html = choiceCardsHtml(trustChoices(), 'review', { label: 'Trust' });
    assert.match(html, /role="radiogroup" aria-label="Trust"/);
    assert.match(html, /class="chat-quiz-option chosen" role="radio" aria-checked="true" tabindex="0" data-choice="review"/);
    assert.match(html, /class="chat-quiz-option" role="radio" aria-checked="false" tabindex="-1" data-choice="trusted"/);
});
