// The wizard's Starter skills row (board B13): ticks = the maintainer's recommended
// marks, built-ins lead, one scrollable categorized list, the two hub trust choices,
// and one payload the atomic Finish carries — nothing when the list could not be read.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createStarterSkillsRow, normalizeStarterGroups } from '../modules/onboarding_starter_skills.js';

const STARTER = JSON.parse(readFileSync(new URL('./fixtures/skills_v4/onboarding_starter_skills.json', import.meta.url), 'utf8'));

function fakeHost() {
    const handlers = {};
    const summary = { innerHTML: '' };
    return {
        isConnected: true, innerHTML: '', handlers,
        addEventListener(type, fn) { handlers[type] = fn; },
        removeEventListener(type) { delete handlers[type]; },
        querySelector(selector) { return selector === '.skills-starter-summary' ? summary : null; },
    };
}
const target = (attrs) => ({ closest: (selector) => {
    const key = Object.keys(attrs).find((name) => selector.includes(name));
    return key ? attrs[key] : null;
} });
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

test('built-ins lead as their own group; the host’s category order stays', () => {
    const lifted = normalizeStarterGroups([
        { category: 'messaging', label: 'Messaging', items: [{ id: 'builtin/telegram', name: 'telegram', builtin: true }, { id: 'h/x', name: 'x' }] },
        { category: 'computer', label: 'Computer control', items: [{ id: 'builtin/ucu', name: 'unix_computer_use', builtin: true }] },
    ]);
    assert.deepEqual(lifted.map((group) => [group.label, group.items.map((item) => item.name)]), [
        ['Built in — comes with the app', ['telegram', 'unix_computer_use']],
        ['Messaging', ['x']],
    ]);
    // The host's answer groups built-ins inside their categories; the row lifts them into one group first.
    assert.deepEqual(normalizeStarterGroups(STARTER.groups).map((group) => group.label), [
        'Built in — comes with the app', 'Messaging', 'Documents', 'Search & research', 'About Ouroboros', 'Writing & review']);
    assert.deepEqual(normalizeStarterGroups(null), []);
});

test('collapsed: «N · M ticked · Change ›» with the ticked names; the payload carries ticks and both trust choices', async () => {
    const row = createStarterSkillsRow({ read: async () => STARTER });
    await row.loading;
    const host = fakeHost();
    row.mount(host);
    assert.match(text(host.innerHTML), /^Starter skills 10 · 6 ticked · Change › Ticked: telegram, pdf_tools, word_tools, duckduckgo, memory-atlas, anti-slop\. 4 more you can tick\.$/);
    assert.deepEqual(row.payload(), {
        starter_skills: ['builtin/telegram', 'ouroboros/pdf_tools', 'ouroboros/word_tools', 'ouroboros/duckduckgo', 'ouroboros/memory-atlas', 'ouroboros/anti-slop'],
        skill_source_trust: { ouroboros: 'trusted', 'ouroboros-community': 'review' },
    });
    assert.doesNotMatch(host.innerHTML, /skills-starter-panel/, 'the list opens only on Change');
});

test('expanded: one scroll of categorized cards, Tick all / Untick all, and the hub trust pills', async () => {
    const row = createStarterSkillsRow({ read: async () => STARTER });
    await row.loading;
    const host = fakeHost();
    row.mount(host);
    host.handlers.click({ target: target({ '[data-starter-toggle]': {} }) });
    const html = host.innerHTML;
    assert.match(html, /data-starter-toggle aria-expanded="true">Hide ▾</);
    assert.match(html, /class="skills-starter-scroll" tabindex="0" aria-label="Starter skills"/);
    assert.match(html, /<h4>Built in — comes with the app<span>2<\/span><\/h4>/);
    assert.match(html, /<h4>Search &amp; research<span>3<\/span><\/h4>/);
    assert.match(html, /class="wizard-choice skills-starter-card active">[\s\S]*?data-starter-item="ouroboros\/duckduckgo" checked/);
    assert.match(html, /data-starter-item="builtin\/unix_computer_use"><strong>unix_computer_use<\/strong><span class="ui-chip skills-chip" data-tone="neutral">Built in<\/span>/);
    assert.match(html, /Needs: KEENABLE_API_KEY/);
    assert.match(html, /They install without review — you trust Ouroboros\. Skills that need a key wait for it\./);
    assert.match(html, /data-starter-trust="ouroboros-community" data-trust="review">Review each</);

    host.handlers.click({ target: target({ '[data-starter-all]': { dataset: { starterAll: '0' } } }) });
    assert.deepEqual(row.payload().starter_skills, []);
    host.handlers.click({ target: target({ '[data-starter-all]': { dataset: { starterAll: '1' } } }) });
    assert.equal(row.payload().starter_skills.length, 10);
    host.handlers.click({ target: target({ '[data-starter-trust]': { dataset: { starterTrust: 'ouroboros', trust: 'review' } } }) });
    assert.equal(row.payload().skill_source_trust.ouroboros, 'review');
    assert.match(host.innerHTML, /Ouroboros reviews each one before it runs\./);
});

test('a box changed by the owner updates the ticks and the summary in place', async () => {
    const row = createStarterSkillsRow({ read: async () => STARTER });
    await row.loading;
    const host = fakeHost();
    row.mount(host);
    const card = { classList: { toggle() {} } };
    const box = { checked: true, dataset: { starterItem: 'ouroboros/keenable' }, closest: (sel) => (sel === '.skills-starter-card' ? card : null) };
    host.handlers.change({ target: { closest: (sel) => (sel === '[data-starter-item]' ? box : null) } });
    assert.ok(row.payload().starter_skills.includes('ouroboros/keenable'));
    assert.match(host.querySelector('.skills-starter-summary').innerHTML, /10 · 7 ticked/);
});

test('an unreadable list leaves the host defaults and says so; the Finish payload adds nothing', async () => {
    const row = createStarterSkillsRow({ read: async () => { throw new Error('offline'); } });
    await row.loading;
    const host = fakeHost();
    row.mount(host);
    assert.match(text(host.innerHTML), /The default set The list couldn’t be read \(offline\)\. Ouroboros installs the recommended skills after it starts/);
    assert.deepEqual(row.payload(), {});
    const empty = createStarterSkillsRow({ read: async () => ({}) });
    await empty.loading;
    assert.deepEqual(empty.payload(), {});
});
