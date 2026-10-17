// Installed skill cards (Skills → Installed): one head shared with Browse, the run
// state as one chip, at most two attention trays (one sentence + its actions), and
// Details for the record. Rows come from the listing fixture shaped as SPEC §8.1.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    fillFindingsOnOpen, installedSkillModel, installedWhoHtml, isMadeHere, renderInstalledSkillCard, reviewSentence,
} from '../modules/skill_card_renderer.js';

const LISTING = JSON.parse(readFileSync(new URL('./fixtures/skills_v4/extensions_v4.json', import.meta.url), 'utf8'));
const row = (name) => structuredClone(LISTING.skills.find((skill) => skill.name === name));
const chipOf = (html) => html.match(/<span class="skills-card-status"><span class="ui-chip skills-chip" data-tone="([^"]+)"[^>]*>(?:<span class="chat-live-typing[^"]*"[^>]*>(?:<span><\/span>){3}<\/span>)?([^<]+)</);
const traysOf = (html) => html.split('data-skill-trays>')[1].split('<div data-skill-errors>')[0];

function skill(overrides = {}) {
    return {
        name: 'demo', type: 'extension', version: '1.0.0', description: 'Demo skill', enabled: false,
        source: 'external', payload_root: 'skills/external/demo', review_status: 'clean', review_stale: false,
        review_gate: { executable_review: true }, executable_review: true, permissions: [],
        grants: { all_granted: true }, owner_attestable: true, ...overrides,
    };
}

test('one head: the name opens the detail sheet, marks stay beside it, the who line names the source', () => {
    const html = renderInstalledSkillCard(row('weather'));
    assert.match(html, /<button type="button" class="skills-card-name" data-skill="weather" data-skill-action="open_detail">weather<span class="skills-card-name-more" aria-hidden="true">›<\/span><\/button>/);
    assert.match(html, /data-official-mark[^>]*>[\s\S]*?Official<\/button>/, 'Official comes from the install record');
    assert.match(installedWhoHtml(row('weather')), /^Ouroboros · v0\.3\.1 · installed .* by Ouroboros$/);
    assert.match(installedWhoHtml(row('telegram')), /^Built in · v1\.3\.0 · came with Ouroboros /);
    assert.match(installedWhoHtml(row('zoom_meeting')), /^Made here · v0\.1\.0 · made here /);
    assert.doesNotMatch(renderInstalledSkillCard(row('telegram')), /data-official-mark/, 'a built-in skill is not Official');
    assert.doesNotMatch(html, / title="/, 'no explanation lives in a hover title');
});

test('the run state is one status chip: On, Off, Needs access, Reviewing…, Review found problems, Not reviewed yet', () => {
    assert.deepEqual(chipOf(renderInstalledSkillCard(row('telegram'))).slice(1), ['ok', 'On']);
    assert.deepEqual(chipOf(renderInstalledSkillCard(skill())).slice(1), ['neutral', 'Off']);
    assert.deepEqual(chipOf(renderInstalledSkillCard(row('zoom_meeting'))).slice(1), ['warn', 'Needs access']);
    assert.deepEqual(chipOf(renderInstalledSkillCard(row('whoop-health'))).slice(1), ['warn', 'Reviewing…']);
    assert.deepEqual(chipOf(renderInstalledSkillCard(row('minecraft-widget'))).slice(1), ['danger', 'Review found problems']);
    assert.deepEqual(chipOf(renderInstalledSkillCard(row('claudexor_quotas'))).slice(1), ['warn', 'Not reviewed yet']);
    assert.match(renderInstalledSkillCard(row('whoop-health')), /class="chat-live-typing skills-dots"/, 'live work is the three-dot loop');
    assert.doesNotMatch(renderInstalledSkillCard(row('telegram')), /skills-dots/, 'nothing moves when nothing works');
});

test('each attention tray is one sentence with its own actions, visible without hover', () => {
    const access = traysOf(renderInstalledSkillCard(row('zoom_meeting')));
    assert.match(access, /Wants your permission to talk with other people for you\./);
    assert.match(access, /data-skill-action="grant" data-keys="presence"[^>]*>Grant access</);

    const problems = traysOf(renderInstalledSkillCard(row('minecraft-widget')));
    assert.match(problems, /data-tone="danger"/);
    assert.match(problems, /Review found 1 problem and 1 note\. First: “widget\.js::set permits a new removal when modified already has 4096 keys\.”/);
    assert.match(problems, /Installed, but it doesn’t run and gets no keys until you decide\./);
    assert.match(problems, />Read findings</);
    assert.match(problems, /data-skill-action="repair"[^>]*>Ask Ouroboros to fix</);
    assert.match(problems, /skills-skip-btn[^>]*data-skill-action="attest_review"[^>]*><span class="skills-skip-glyph" aria-hidden="true"><svg[^>]*stroke="currentColor"/);

    const reviewing = traysOf(renderInstalledSkillCard(row('whoop-health')));
    assert.match(reviewing, /Ouroboros reviews it before it runs or gets your keys\./);
    assert.match(reviewing, />Skip review</);

    const otherEdit = traysOf(renderInstalledSkillCard(row('claudexor_quotas')));
    assert.match(otherEdit, /Its files changed outside Ouroboros, so it stopped until it is reviewed\. Review it, or skip the review if you made the change yourself\./);
    assert.match(otherEdit, /data-skill-action="rereview"[^>]*>Review</);
});

test('updates: one click for an unmodified skill, «Update…» asks what to keep for a modified one', () => {
    const plain = traysOf(renderInstalledSkillCard(row('weather')));
    assert.match(plain, /v0\.3\.2 is available\./);
    assert.match(plain, /data-skill-action="update"[^>]*>Update</);
    const modified = renderInstalledSkillCard(row('anime_studio'));
    assert.match(traysOf(modified), /Ouroboros Community has v3\.2\.0\. You changed this skill here, so updating asks what to keep\./);
    assert.match(traysOf(modified), />Update…</);
    assert.match(modified, /Modified · edited by you/);
    assert.equal(installedWhoHtml(row('anime_studio')).split(' · ').slice(-1)[0], 'edited by you here: 9 files (now v3.3.0)');
    assert.match(installedWhoHtml(row('anime_studio')), /^Ouroboros Community · installed v2\.12\.0 /);
    const sameLabel = row('weather');
    sameLabel.install.update_available.version = '0.3.1';
    assert.match(traysOf(renderInstalledSkillCard(sameLabel)), /changed the files of v0\.3\.1 \(same version label\)/);
});

test('withdrawn and left-source skills say so; a withdrawn one offers Uninstall', () => {
    const withdrawn = traysOf(renderInstalledSkillCard(row('old_tool')));
    assert.match(withdrawn, /Ouroboros Community withdrew v0\.2\.0 on 16 Oct: Sends data to an unnamed server\./);
    assert.match(withdrawn, /data-skill-action="uninstall"[^>]*>Uninstall</);
    assert.equal(installedSkillModel(row('old_tool')).needs, true);
    assert.match(traysOf(renderInstalledSkillCard(row('gone_tool'))), /No longer in Ouroboros Community\. It keeps working; it won’t get updates\./);
    assert.equal(installedSkillModel(row('gone_tool')).needs, false);
});

test('conflicts lock an off switch with a visible reason but never trap an enabled skill', () => {
    const conflict = { code: 'skill_conflict', skills: ['beta'], omitted: 0 };
    const off = renderInstalledSkillCard(skill({ conflict }));
    assert.match(off, /class="skills-switch is-locked"/);
    assert.match(off, /input type="checkbox" class="skills-toggle"[^>]*disabled/);
    assert.match(traysOf(off), /Uses the same tool names as beta\. Turn one of them off to use the other\./);
    assert.match(off, /aria-describedby="skill-trays-demo"/, 'the locked switch points at the visible explanation');
    const on = renderInstalledSkillCard(skill({ conflict, enabled: true }));
    assert.doesNotMatch(on, /input type="checkbox" class="skills-toggle"[^>]*disabled/);
});

test('a pending lifecycle job shows its words and locks the switch on the intended side', () => {
    const html = renderInstalledSkillCard(skill({ enabled: true, lifecycle_pending: true, lifecycle_kind: 'disable', lifecycle_status: 'running' }));
    assert.deepEqual(chipOf(html).slice(1), ['warn', 'Turning off…']);
    assert.match(html, /input type="checkbox" class="skills-toggle"[^>]*data-skill="demo"\s+disabled/);
    const failed = renderInstalledSkillCard(skill({ lifecycle_status: 'failed', lifecycle_kind: 'enable', lifecycle_error: 'reconcile failed' }));
    assert.deepEqual(chipOf(failed).slice(1), ['danger', 'Enable failed']);
    assert.match(failed, /reconcile failed/);
});

test('#335: a repairable preflight failure offers Ask Ouroboros to fix and never Skip review', () => {
    const preflight = skill({
        type: 'extension', review_status: 'pending', review_gate: { executable_review: false, preflight_failed: true },
        executable_review: false,
        review_findings: [{ item: 'skill_preflight', verdict: 'FAIL', severity: 'critical',
            reason: JSON.stringify({ manifest: [{ item: 'manifest_entry_exists', ok: false, detail: 'missing entry: plugin.py' }], ok: false }) }],
    });
    const html = renderInstalledSkillCard(preflight);
    assert.match(traysOf(html), /First: “Preflight failed — manifest_entry_exists: missing entry: plugin\.py”/);
    assert.match(traysOf(html), /data-skill-action="repair"[^>]*>Ask Ouroboros to fix</);
    assert.doesNotMatch(html, /attest_review/, 'Skip review would be refused by the file check');
    assert.doesNotMatch(html, /skills-menu-item skills-review"/, 'Review again would fail the same way');
    // A non-repairable native payload keeps its Review path.
    const native = renderInstalledSkillCard({ ...preflight, source: 'native', payload_root: '' });
    assert.doesNotMatch(native, /data-skill-action="repair"/);
    assert.match(native, /skills-menu-item skills-review"/);
});

test('a stale recorded preflight failure keeps Review first and offers the fix from the menu (D11)', () => {
    const html = renderInstalledSkillCard(skill({
        review_status: 'pending', review_stale: true, executable_review: false,
        review_gate: { executable_review: false, preflight_failed: false, preflight_failed_stale: true },
    }));
    assert.match(traysOf(html), /The last file check failed on earlier files\. Review the current files, or ask Ouroboros to fix it\./);
    assert.match(traysOf(html), /data-skill-action="rereview"[^>]*>Review</);
    assert.match(html, /skills-menu-item skills-repair-stale" data-skill="demo" data-skill-action="repair">Ask Ouroboros to fix</);
});

test('the menu carries details, review again, make runnable, publish and the remove action for the origin', () => {
    const made = renderInstalledSkillCard(skill({ type: 'instruction', source: 'self_authored', is_self_authored: true }));
    assert.match(made, /skills-make-runnable" data-skill="demo" data-skill-action="repair">Make runnable</);
    assert.match(made, /skills-delete-local"[^>]*data-skill-action="delete"[^>]*>Delete</);
    assert.doesNotMatch(made, /skills-uninstall/);
    const sourced = renderInstalledSkillCard(row('weather'));
    assert.match(sourced, /skills-uninstall"[^>]*>Uninstall</);
    assert.doesNotMatch(sourced, /skills-delete-local/);
    assert.doesNotMatch(renderInstalledSkillCard(row('telegram')), /skills-uninstall|skills-delete-local/, 'built-in skills are turned off, not removed');
});

test('Publish follows the host verdict: task_start_allowed decides, a missing token is said in Details', () => {
    const allowed = renderInstalledSkillCard(skill({ submit_hub: { visible: true, task_start_allowed: true, disabled: true, reason: '' } }));
    assert.match(allowed, /skills-submit-hub"[^>]*data-submit-disabled="false"[^>]*>Publish…</);
    const legacy = renderInstalledSkillCard(skill(), new Set(), new Set(), {}, { githubTokenConfigured: false });
    assert.match(legacy, /data-submit-disabled="true"/);
    assert.match(legacy, /Publishing needs a GitHub token: Settings → Secrets → GITHUB_TOKEN\./);
    assert.doesNotMatch(renderInstalledSkillCard(skill({ source: 'native' })), /skills-submit-hub/);
});

test('Details keep the record: review sentence and verdict, what it can use, the install record and live state', () => {
    const weather = renderInstalledSkillCard(row('weather'), new Set(), new Set(), LISTING.live);
    assert.match(weather, /Reviewed on this computer · passed\./);
    assert.match(weather, /Internet · A widget and a settings section · Its own pages inside Ouroboros/);
    assert.match(weather, /installed without review — you trusted Ouroboros · by Ouroboros · in task t-weather/);
    assert.match(weather, />Loaded</);
    const trusted = skill({ review_status: 'pending', review_gate: { executable_review: true },
        install: { source: { id: 'ouroboros', name: 'Ouroboros' }, basis: 'trusted', installed_at: '2026-10-12T10:00:00Z', release: { version: '1.0.0' } } });
    assert.equal(reviewSentence(trusted), 'Installed without review on 12 Oct because you trust Ouroboros.');
    assert.equal(reviewSentence(skill({ review_profile: 'owner_attested' })), 'You skipped the review; it runs unreviewed.');
    const worker = renderInstalledSkillCard(skill({ enabled: true, live_loaded: true, dispatch_live: true, process: 'worker' }));
    assert.match(worker, />Loaded · worker</);
});

test('Presence runtime keeps the working folder and its reset, inside Details', () => {
    const html = renderInstalledSkillCard(skill({
        presence_runtime: {
            defaults: { model_slot: 'main', inline_max_rounds: 10 }, overrides: { model_slot: 'light', inline_max_rounds: 7 },
            workspace_root: '/work/reports & notes', state_fingerprint: 'a'.repeat(64),
        },
    }));
    assert.match(html, /name="workspace_root"[^>]+value="\/work\/reports &amp; notes"/);
    assert.match(html, /Memory stays shared/);
    assert.match(html, /data-presence-runtime-reset/);
    assert.doesNotMatch(renderInstalledSkillCard(skill()), /skills-presence-runtime/);
});

test('findings are built on first open from the rows in memory, once', () => {
    const rows = [row('minecraft-widget')];
    const details = (open, built = false, name = 'minecraft-widget') => ({
        open, dataset: { skillFindings: name }, childElementCount: built ? 2 : 1, inserted: [],
        insertAdjacentHTML(where, markup) { this.inserted.push([where, markup]); },
    });
    const closed = details(false);
    assert.equal(fillFindingsOnOpen(closed, rows), false);
    const opened = details(true);
    assert.equal(fillFindingsOnOpen(opened, rows), true);
    assert.match(opened.inserted[0][1], /widget\.js::set permits/);
    assert.equal(fillFindingsOnOpen(details(true, true), rows), false, 'a built list survives re-opening');
    const gone = details(true, false, 'vanished');
    fillFindingsOnOpen(gone, rows);
    assert.match(gone.inserted[0][1], /not in the current list/);
    assert.equal(fillFindingsOnOpen({ open: true, dataset: {} }, rows), false, 'other disclosures are not ours');
});

test('made here means created on this computer without an install record', () => {
    assert.equal(isMadeHere(row('zoom_meeting')), true);
    assert.equal(isMadeHere(row('weather')), false);
    assert.equal(isMadeHere(row('telegram')), false);
});
