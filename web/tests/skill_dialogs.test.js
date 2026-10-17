import assert from 'node:assert/strict';
import test from 'node:test';
import { createMySkillsDestination, sourceProbeWarning, updateWithoutReview } from '../modules/skill_dialogs.js';
import { skillSourcesApi } from '../modules/api_client.js';

test('rejected update confirms its exact version and sends only that release after literal confirmation', async () => {
    const skill = { name: 'demo', install: { update_rejected: { version: '2.0', digest: 'd'.repeat(64) },
        update_available: { version: '3.0', digest: 'e'.repeat(64) } } };
    for (const decision of [false, null, undefined, { confirmed: true }, true]) {
        const calls = [];
        const result = await updateWithoutReview(skill, {
            dialog: async (model) => {
                assert.match(model.title, /demo to 2\.0 without review/);
                assert.match(model.body, /Version 2\.0 didn’t pass/);
                assert.equal(model.confirmLabel, 'Update without review');
                return decision;
            },
            update: async (...args) => { calls.push(args); return { ok: true }; },
        });
        assert.deepEqual(calls, decision === true ? [['demo', 'replace', 'd'.repeat(64), '', { skipReview: true }]] : []);
        assert.deepEqual(result, decision === true ? { ok: true } : null);
    }
});

test('an unencrypted probe discloses the warning; a regular probe has none', () => {
    assert.equal(sourceProbeWarning({ warning: 'not_encrypted' }), 'This address is not encrypted: anyone on the network path could change what you install.');
    assert.equal(sourceProbeWarning({ kind: 'git' }), '');
});

test('My skills selects the source from both create and connect answers', async () => {
    for (const created of [false, true]) {
        const choice = await createMySkillsDestination(async () => ({ ok: true, created,
            repository: 'owner/ouroboros-skills', source: { id: 'owner-skills', name: 'My skills' } }));
        assert.equal(choice.value, 'source:owner-skills');
        assert.match(choice.label, /private repository/);
        assert.match(choice.detail, /owner\/ouroboros-skills/);
    }
});

test('My skills failures give the next step for typed refusals and network failures', async () => {
    const messages = { no_token: /Add GITHUB_TOKEN in Settings/, cannot_create: /allow repository access/, failed: /check your GitHub connection/ };
    for (const [code, expected] of Object.entries(messages)) {
        await assert.rejects(createMySkillsDestination(async () => ({ ok: false, code })), expected);
        await assert.rejects(createMySkillsDestination(async () => { throw { body: { code } }; }), expected);
    }
    await assert.rejects(createMySkillsDestination(async () => { throw new Error('offline'); }), /check your GitHub connection/);
});

test('owner skip and My skills API calls use the shared HTTP contract', async () => {
    const before = globalThis.fetch;
    const calls = [];
    globalThis.fetch = async (url, init) => { calls.push([url, JSON.parse(init.body)]); return { ok: true, json: async () => ({ ok: true }) }; };
    try {
        await skillSourcesApi.mySkills();
        await skillSourcesApi.updateSkill('demo', 'replace', 'a'.repeat(64), '', { skipReview: true });
        await skillSourcesApi.updateSkill('demo', 'replace', 'b'.repeat(64));
        assert.deepEqual(calls, [
            ['/api/skill-sources/my-skills', {}],
            ['/api/skills/demo/update', { mode: 'replace', digest: 'a'.repeat(64), skip_review: true }],
            ['/api/skills/demo/update', { mode: 'replace', digest: 'b'.repeat(64) }],
        ]);
    } finally { globalThis.fetch = before; }
});
