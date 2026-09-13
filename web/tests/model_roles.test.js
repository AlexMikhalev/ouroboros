import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { composeModelSource, createModelRolesEditor, modelContextNote,
    modelSourceGroups, parseModelSource } from '../modules/model_roles.js';

const { contract } = JSON.parse(readFileSync(new URL('./fixtures/onboarding_bootstrap.json', import.meta.url)));

test('source/model spelling is reversible and never embeds the account', () => {
    for (const value of ['google/a', 'openai::b', 'claudexor::codex=gpt-test', 'custom::x=y']) {
        const { source, model } = parseModelSource(value);
        assert.equal(composeModelSource(source, model), value);
    }
    assert.equal(composeModelSource('subscription:codex', ''), '');
    assert.equal(composeModelSource('openrouter', 'openai::owner-model'), 'openai::owner-model');
});

test('Models shows only model-capable sources, keeping a saved missing choice', () => {
    const groups = modelSourceGroups({ sources: [{ id: 'codex', label: 'Codex' }],
        providers: contract.providerProfiles });
    assert.deepEqual(groups[0].options.map((row) => row.value), ['subscription:codex']);
    assert.ok(groups[1].options.some((row) => row.value === 'openai'));
    const saved = modelSourceGroups({ current: 'subscription:future' });
    assert.equal(saved[0].options[0].value, 'subscription:future');
    assert.match(saved[0].options[0].label, /not checked/);
});

test('Models offers connection only after both source discovery and Accounts were read', () => {
    for (const facts of [{}, { catalogKnown: true }, { accountsKnown: true }]) {
        const groups = modelSourceGroups(facts);
        assert.doesNotMatch(groups[0].options[0].label, /connect one/i);
    }
    const empty = modelSourceGroups({ catalogKnown: true, accountsKnown: true });
    assert.match(empty[0].options[0].label, /No model sources listed/);
    assert.match(empty[0].options[0].label, /connect one/i);
    const saved = modelSourceGroups({ current: 'subscription:owner-source' });
    assert.equal(saved[0].options[0].value, 'subscription:owner-source');
});

test('role pins and context survive a no-edit save, including identical model names', () => {
    const editor = createModelRolesEditor({ hostId: 'test', doc: () => null });
    const settings = {
        OUROBOROS_MODEL: 'claudexor::codex=gpt-test',
        OUROBOROS_MODEL_LIGHT: 'claudexor::codex=gpt-test',
        OUROBOROS_MODEL_FALLBACKS: 'claudexor::codex=gpt-second, openai::model',
        OUROBOROS_MODEL_ACCOUNTS: { main: 'personal', light: 'work', vision: 'work', fallback: ['reserve', ''], websearch: 'saved' },
        OUROBOROS_MODEL_CONTEXT_WINDOWS: { main: 1000000, fallback: [872000, 0], deep_review: 250000 },
    };
    editor.load(settings, contract);
    const after = editor.collect();
    for (const [key, value] of Object.entries(settings)) assert.deepEqual(after[key], value, key);
    editor.adoptCatalog({ model_sources: [{ id: 'codex', label: 'Codex' }], items: [] });
    assert.deepEqual(editor.collect(), after, 'new discovery never rewrites the assignment');
    assert.equal(editor.validate(), '');
    editor.destroy();
});

test('an unloaded editor authors nothing and an API-only save does not create role maps', () => {
    const editor = createModelRolesEditor({ hostId: 'test', doc: () => null });
    assert.deepEqual(editor.collect(), {});
    editor.load({ OUROBOROS_MODEL: 'openai::model' }, contract);
    const setting = editor.collect();
    assert.equal(setting.OUROBOROS_MODEL, 'openai::model');
    assert.ok(!('OUROBOROS_MODEL_ACCOUNTS' in setting));
    assert.ok(!('OUROBOROS_MODEL_CONTEXT_WINDOWS' in setting));
    editor.destroy();
});

test('context Auto uses the exact advertised maximum, while manual values stay assertions', () => {
    assert.match(modelContextNote({ context_window: 272000, max_context_window: 872000 }), /872,000.*advertised/);
    assert.match(modelContextNote({ max_context_window: 872000 }, 1000000), /1,000,000.*set by you/);
    assert.match(modelContextNote(null), /not known/);
});

test('the same role sheets are loaded by both actual UI hosts', () => {
    for (const file of ['index.html', 'onboarding_template.html']) {
        const html = readFileSync(new URL(`../${file}`, import.meta.url), 'utf8');
        for (const sheet of ['model_roles.css', 'reviewer_slots.css']) {
            assert.ok(html.includes(`href="/static/${sheet}"`), `${file} loads ${sheet}`);
        }
    }
});
