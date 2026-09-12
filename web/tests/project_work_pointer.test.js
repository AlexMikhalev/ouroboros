import test from 'node:test';
import assert from 'node:assert/strict';
import {projectWorkTarget} from '../modules/project_work_pointer.js';

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
