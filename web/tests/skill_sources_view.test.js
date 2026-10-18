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

test('a row a build supplied says so; it keeps its trust value and ⋮ like any other', () => {
    const managed = renderSourceRow({ ...source('ouroboros'), managed: true });
    assert.match(managed, /data-tone="neutral">Set by your organization</);
    assert.match(managed, /data-source-action="trust"/);
    assert.match(managed, /data-source-menu="ouroboros"/);
    assert.ok(SOURCES.every((row) => !row.managed), 'an owner’s own list has no such row');
    assert.doesNotMatch(renderSourceRow(source('ouroboros')), /Set by your organization/);
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
    const ago = (ms) => new Date(Date.now() - ms).toISOString();
    assert.match(text(sourceHealthHtml(source('ouroboros'), { state: 'ready', last_read_at: ago(120_000) })), /^read 2 min ago$/);
    assert.match(sourceHealthHtml(source('ouroboros'), { state: 'reading' }), /data-work/);
    assert.equal(text(sourceHealthHtml(source('ouroboros'), { state: 'ready', last_read_at: ago(3_600_000 * 3), refreshing: true })),
        'reading… (list from 3 h ago)', 'a refresh in flight keeps the age of the list shown');
    // The host keeps the last list when a fetch fails: state ready, the failure in `error`.
    const failed = sourceHealthHtml(source('ouroboros-community'), health('ouroboros-community'));
    assert.match(failed, /data-tone="warn"/);
    assert.match(text(failed), /couldn’t be read: GitHub didn&#39;t answer \(timed out after 60 s\) — showing the list from .* ago Try again/);
    assert.match(failed, /data-source-action="refresh" data-source-id="ouroboros-community"/);
    const never = sourceHealthHtml(source('ouroboros'), { state: 'error', error: 'repository not found' });
    assert.equal(text(never), 'couldn’t be read: repository not found Try again', 'no list was ever read: no age is claimed');
    assert.equal(text(sourceHealthHtml(source('clawhub'), { state: 'search' })), 'searched when you search');
    assert.equal(text(sourceHealthHtml(source('ouroboros'), null)), 'not read yet');
    assert.equal(sourceHealthHtml(source('builtin'), { state: 'ready' }), '', 'built-in skills ship with the app: nothing is read');
});

test('a row without the catalog read uses its own state; a count is said only for a list that was read', () => {
    const own = renderSourceRow(source('my-folder'));
    assert.match(text(own), /~\/ouroboros-skills · 0 skills · read .* ago/, 'a read folder with no skills has 0, a fact');
    const unread = renderSourceRow({ ...source('ouroboros'), state: 'reading', last_read_at: '' });
    assert.doesNotMatch(text(unread), /skills ·/, 'before the first read the count is unknown, never 0');
    assert.match(unread, /data-work/);
    assert.doesNotMatch(text(renderSourceRow(source('builtin'), { items: items('builtin') })), /read/);
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
