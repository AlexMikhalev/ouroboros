import test from 'node:test';
import assert from 'node:assert/strict';
import {
    POINTER_NAME_CHARS, bindProjectWorkPointer, projectWorkLabel, projectWorkTarget,
} from '../modules/project_work_pointer.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

test('Project pointer prefers represented unfinished roots without duplicating child cards',()=>{
    const done={root:{isConnected:true},finished:true};
    const active={root:{isConnected:true},finished:false};
    const child={root:{isConnected:true},isSubagent:true};
    const removed={root:{isConnected:false}};
    assert.equal(projectWorkTarget([done,active,child,removed]),active);
    active.finished=true;
    assert.equal(projectWorkTarget([done,active,child]),active);
    assert.equal(projectWorkTarget([child,removed]),null);
});

test('an older prepended card at the same timestamp does not become the latest task', () => {
    const card = offset => ({ root: { isConnected: true, dataset: { ts: '1000' } },
        historyPosition: { source: 'progress', offset }, finished: true });
    const newer = card(500), older = card(100);
    assert.equal(projectWorkTarget([newer, older]), newer);
});

test('the pointer label names the card and caps a status-line title to one line of text', () => {
    const card = (title, extra = {}) => ({ root: { isConnected: true }, finished: true,
        titleEl: { textContent: title }, ...extra });
    assert.equal(projectWorkLabel(null), '');
    assert.equal(projectWorkLabel(card('Task activity', { finished: false })), 'Working · Task activity');
    assert.equal(projectWorkLabel(card('plan_task: DEGRADED (0/3 parseable reviewers)',
        { suggestedName: 'Swarm dynamics survey' })), 'Latest task · Swarm dynamics survey');
    assert.equal(projectWorkLabel(card('  multi\n line\t title  ')), 'Latest task · multi line title');
    assert.equal(projectWorkLabel(card('')), 'Latest task · Task');
    const verdict = 'plan_task: DEGRADED (0/3 parseable reviewers; counts are untrusted) — 0 blocking / '
        + '0 note / 0 need_evidence; cycles paid 1/3; slot reasons: '
        + 'Pending dispatch; the physical review operation is in flight (window 2700s); '.repeat(4);
    assert.ok(verdict.length > 300);
    const label = projectWorkLabel(card(verdict));
    assert.ok(label.startsWith('Latest task · plan_task: DEGRADED'));
    assert.ok(label.endsWith('…'));
    assert.ok(label.length <= 'Latest task · '.length + POINTER_NAME_CHARS);
});

test('the bound pointer hides itself and the coverage note without a represented card', () => {
    const { prior, mount } = installDom();
    try {
        const records = new Map();
        let historyWindow = null;
        let navigated = null;
        const host = mount.ownerDocument.createElement('div');
        mount.appendChild(host);
        const pointer = bindProjectWorkPointer(host, {
            records, getWindow: () => historyWindow, onNavigate: (root) => { navigated = root; },
        });
        const button = host.querySelector('.project-work-pointer');
        const note = host.querySelector('.project-work-coverage');
        const label = button.querySelector('.project-work-pointer-label');
        assert.equal(button.hidden, true);
        assert.equal(button.disabled, true);
        assert.equal(note.hidden, true);

        const root = mount.ownerDocument.createElement('div');
        mount.appendChild(root);
        root.dataset.ts = '1000';
        records.set('t1', { root, finished: true, titleEl: { textContent: 'x'.repeat(400) } });
        pointer.update();
        assert.equal(button.hidden, false);
        assert.equal(button.disabled, false);
        assert.ok(label.textContent.startsWith('Latest task · xxx'));
        assert.ok(label.textContent.length <= 'Latest task · '.length + POINTER_NAME_CHARS);
        assert.equal(note.hidden, false);
        assert.equal(note.textContent, 'Loaded messages only');

        historyWindow = { complete: true };
        pointer.update();
        assert.equal(note.hidden, true);
        for (const fn of button.listeners.get('click') || []) fn();
        assert.equal(navigated, root);

        pointer.destroy();
        assert.equal(host.children.length, 0);
    } finally {
        restoreDom(prior);
    }
});
