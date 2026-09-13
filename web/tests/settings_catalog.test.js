import assert from 'node:assert/strict';
import test from 'node:test';
import { catalogReadNote, catalogReadState, mergeModelCatalog, refreshModelCatalog } from '../modules/settings_catalog.js';

const first = {
    items: [{ value: 'claudexor::opaque=owner-model', source_id: 'opaque', credential_profile_id: 'personal', observed_at: 'earlier' }],
    model_sources: [{ id: 'opaque', label: 'Model service', credentialHarness: 'codex' }],
    observed_at: 'earlier', errors: [],
};

test('unread, failed, and stale discovery preserve last-known entries with their provenance', () => {
    const before = mergeModelCatalog({}, first);
    for (const gap of [
        { read_state: 'not_read', items: [], model_sources: [] },
        { errors: [{ provider_id: 'claudexor', error: 'connection refused' }], items: [], model_sources: [] },
        { freshness: 'stale', items: [], model_sources: [], coverage: { complete: false } },
        { read_state: 'transport', errors: [{ error: 'network offline' }] },
    ]) {
        const after = mergeModelCatalog(before, gap);
        assert.notEqual(catalogReadState(after), 'ok');
        assert.deepEqual(after.items, first.items);
        assert.deepEqual(after.model_sources, first.model_sources);
        assert.equal(after.observed_at, 'earlier');
        assert.match(catalogReadNote(after), /selection.*kept/);
        assert.doesNotMatch(catalogReadNote(after), /connect.*account|\blog[ -]?in\b/i);
        if (gap.coverage) assert.deepEqual(after.coverage, gap.coverage);
    }
});

test('partial discovery enriches healthy choices and a later successful empty read clears only discovery', () => {
    const partial = mergeModelCatalog(first, {
        items: [{ value: 'openai::new-model', label: 'New model' }], model_sources: [],
        errors: [{ provider_id: 'claudexor', error: 'catalog timeout' }],
    });
    assert.deepEqual(partial.items.map((item) => item.value), ['claudexor::opaque=owner-model', 'openai::new-model']);
    assert.deepEqual(partial.model_sources, first.model_sources);
    assert.match(catalogReadNote(partial), /catalog timeout/);
    const empty = mergeModelCatalog(partial, { items: [], model_sources: [], errors: [] });
    assert.equal(empty.read_state, 'ok');
    assert.equal(empty.sources_read_state, 'ok');
    assert.deepEqual(empty.items, []);
    assert.deepEqual(empty.model_sources, []);
    assert.equal(catalogReadNote(empty), '');
    assert.equal(empty.observed_at, undefined, 'an earlier observation is not the new read timestamp');
});

test('reading models does not prove an omitted model-source list was read', () => {
    const legacy = mergeModelCatalog(first, { items: [], errors: [] });
    assert.equal(legacy.read_state, 'ok');
    assert.equal(legacy.sources_read_state, 'not_read');
    assert.deepEqual(legacy.model_sources, first.model_sources);
});

test('the real Refresh event keeps read failures distinct and carries complete catalog evidence', async (t) => {
    const previousDocument = globalThis.document;
    const previousFetch = globalThis.fetch;
    t.after(() => { globalThis.document = previousDocument; globalThis.fetch = previousFetch; });
    const status = { textContent: '', dataset: {} };
    const document = new EventTarget();
    document.getElementById = (id) => id === 'settings-model-catalog-status' ? status : null;
    globalThis.document = document;
    const events = [];
    document.addEventListener('settings-model-catalog:updated', (event) => events.push(event.detail));
    let response = first;
    globalThis.fetch = async () => {
        if (response instanceof Error) throw response;
        return { ok: true, json: async () => response };
    };
    await refreshModelCatalog();
    assert.deepEqual(events.at(-1).model_sources, first.model_sources);
    assert.equal(events.at(-1).observed_at, 'earlier');
    assert.equal(events.at(-1).read_state, 'ok');
    response = new Error('offline');
    await refreshModelCatalog();
    assert.equal(events.at(-1).read_state, 'transport');
    assert.equal(events.at(-1).items, undefined, 'a failed request must not broadcast an empty catalog');
    assert.equal(events.at(-1).model_sources, undefined);
    assert.match(status.textContent, /offline/);
    response = { items: [], model_sources: [], errors: [], coverage: { complete: true } };
    await refreshModelCatalog();
    assert.equal(events.at(-1).read_state, 'ok');
    assert.deepEqual(events.at(-1).items, []);
    assert.deepEqual(events.at(-1).coverage, { complete: true });
    assert.doesNotMatch(status.textContent, /offline/);
    response = {};
    await refreshModelCatalog();
    assert.equal(events.at(-1).read_state, 'transport', 'a malformed 2xx does not certify an empty read');
    assert.match(status.textContent, /no model list/);
});

test('an older Refresh completion cannot replace newer success or its read facts', async (t) => {
    const previousDocument = globalThis.document;
    const previousFetch = globalThis.fetch;
    t.after(() => { globalThis.document = previousDocument; globalThis.fetch = previousFetch; });
    const document = new EventTarget();
    document.getElementById = () => null;
    globalThis.document = document;
    const events = [];
    document.addEventListener('settings-model-catalog:updated', (event) => events.push(event.detail));
    let finishFirst;
    globalThis.fetch = () => new Promise((resolve) => { finishFirst = resolve; });
    const old = refreshModelCatalog();
    globalThis.fetch = async () => ({ ok: true, json: async () => first });
    await refreshModelCatalog();
    finishFirst({ ok: true, json: async () => ({ items: [], model_sources: [] }) });
    assert.equal((await old).stale, true);
    assert.equal(events.length, 1);
    assert.deepEqual(events[0].items, first.items);
});
