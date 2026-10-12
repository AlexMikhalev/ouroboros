"""#1183: exact cold Resume can move an active acceptance fence to another PID.

Admission, agent Pause, durable grant, assignment, loop consumption and pooled
fence event/ack are real; processes and paid transports are synthetic.
"""
import copy
import os
import queue as stdqueue
from types import SimpleNamespace

import pytest

from ouroboros import agent, budget_pause, loop
from supervisor import queue, workers
from tests import test_resume_handover as handover
from tests.test_acceptance_fence_transport import _Supervisor, _pooled_agent
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


def _transport(host, monkeypatch, ctx, pid):  # noqa: F811
    events = stdqueue.Queue()
    _Supervisor(events, host.root)
    actor = _pooled_agent(host.root, events, 'held', attempt=2)
    actor.tools._ctx = ctx

    def begin(**kwargs):
        # Only this synthetic process boundary changes PID; store/process locks keep theirs.
        with monkeypatch.context() as process:
            process.setattr(agent, 'os', SimpleNamespace(**{**vars(os), 'getpid': lambda: pid}))
            return actor._begin_acceptance_fence(**kwargs)

    ctx.begin_acceptance_fence = begin
    ctx.inspect_acceptance_fence = actor._inspect_acceptance_fence
    ctx.end_acceptance_fence = actor._end_acceptance_fence


def _paused_fence(host, tmp_path, monkeypatch, reason='owner', owner_change='', boot=False):  # noqa: F811
    original = handover._pause_through_agent
    prior = {}

    def pause(*args, **kwargs):
        workers.WORKERS[0].proc = SimpleNamespace(pid=1111, is_alive=lambda: True)
        ctx = SimpleNamespace(task_id='held', root_task_id='held', task_attempt=2, drive_root=host.root)
        _transport(host, monkeypatch, ctx, 1111)
        assert loop._begin_task_acceptance_fence(ctx, 'held')[0]
        row = queue.ACCEPTANCE_FENCES['held']
        if owner_change == 'sealed':
            row['status'] = 'sealed'
        elif owner_change == 'direct':
            row.pop('pooled_owner')
        elif owner_change == 'legacy':
            row['pooled_owner'] = {}
        elif owner_change == 'foreign_pid':
            row['pooled_owner']['pid'] = 9999
        elif owner_change == 'foreign_attempt':
            row['pooled_owner']['task_attempt'] = 3
        elif owner_change == 'foreign_task':
            row['task_id'] = 'other'
        row['owner_message_generation'] = 7
        prior.update(copy.deepcopy(row))
        result = original(*args, **kwargs)
        assert {key: value for key, value in queue.ACCEPTANCE_FENCES['held'].items()
                if key != 'paused_continuation'} == prior, 'Pause keeps its fence intact'
        if boot:
            assert handover._stop_boot(monkeypatch) == 1
        return result

    monkeypatch.setattr(handover, '_pause_through_agent', pause)
    rail = budget_pause.RAIL_OWNER_PAUSE if reason == 'owner' else budget_pause.RAIL_GLOBAL_EXHAUSTED
    saved = handover._recovered_then_paused(host, tmp_path, monkeypatch, 'main', rail=rail)
    return saved, prior


def _resume(host, monkeypatch, pid=2222, *, consume=True):  # noqa: F811
    task = handover._dispatch_one(host, monkeypatch)
    workers.WORKERS[0].proc = SimpleNamespace(pid=pid, is_alive=lambda: True)
    if consume:
        seen = handover._run_loop(host, monkeypatch, task, 'after_resume')
        ctx = seen['ctx']
        assert seen['kind'] == 'pause' and seen['round_idx'] == 29
        assert not getattr(ctx, '_task_acceptance_fence_token', None)
    else:
        ctx = SimpleNamespace(task_id='held', root_task_id='held', task_attempt=2, drive_root=host.root)
    _transport(host, monkeypatch, ctx, pid)
    return ctx


@pytest.mark.parametrize('reason', ['owner', 'budget'])
@pytest.mark.parametrize('pid', [1111, 2222], ids=['same-worker', 'different-worker'])
@pytest.mark.parametrize('boot', [False, True], ids=['park', 'park-boot'])
def test_exact_cold_resume_rebinds_and_seals(host, tmp_path, monkeypatch, reason, pid, boot):  # noqa: F811
    from ouroboros.loop_delivery import _no_tool_final_answer
    from ouroboros.task_results import load_task_result, write_task_result

    _, prior = _paused_fence(host, tmp_path, monkeypatch, reason, boot=boot)
    paid = [{'review_id': 'independent-paid-review', 'status': 'pending', 'cost_usd': .25}]
    write_task_result(host.root, 'held', 'scheduled', review_runs=paid)
    ctx = _resume(host, monkeypatch, pid)
    opened, token = loop._begin_task_acceptance_fence(ctx, 'held')
    assert opened, opened
    row = queue.ACCEPTANCE_FENCES['held']
    assert token != prior['token'] and row['pooled_owner'] == {'task_attempt': 2, 'pid': pid}
    assert row['owner_message_generation'] == prior['owner_message_generation']
    # A late old token cannot inspect/release/seal its successor's row.
    for action, outcome in [('inspect', ''), ('end', 'revision'), ('end', 'terminal')]:
        assert queue.transition_acceptance_fence(action=action, token=prior['token'], outcome=outcome)['row_absent']
    if pid != 1111:
        assert queue.transition_acceptance_fence(action='begin', token=prior['token'], task_id='held',
            root_task_id='held', pooled_owner=prior['pooled_owner'])['status'] == 'stale_attempt'
    from ouroboros.tools.registry import ToolRegistry
    tools = ToolRegistry(repo_dir=host.root, drive_root=host.root)
    tools._ctx = ctx
    ctx._task_acceptance_reviewed = True
    trace = {'reasoning_notes': [], 'tool_calls': []}
    limit = loop._RoundLimitContext(messages=[], llm=None, active_model='fixture', active_effort='high',
        max_retries=1, drive_logs=host.root / 'logs', task_id='held', round_idx=30, event_queue=None,
        accumulated_usage={}, task_type='task', active_use_local=False, max_rounds=None,
        drive_root=host.root, tools=tools, llm_trace=trace, tool_schemas=[])
    # Independent reviewers already have custody; this test buys no panel or model call.
    monkeypatch.setattr(loop, '_run_task_acceptance_review_once', lambda **_k: False)
    monkeypatch.setattr(loop, '_maybe_inject_finalization_nudges', lambda *_a, **_k: False)
    delivered = _no_tool_final_answer('Completed cold continuation.', limit, trace, tools,
        stdqueue.Queue(), set(), lambda *_a, **_k: None, explicit_candidate=True)
    assert delivered is not None and delivered[0] == 'Completed cold continuation.'
    assert queue.ACCEPTANCE_FENCES['held']['status'] == 'sealed'
    assert load_task_result(host.root, 'held')['review_runs'] == paid


@pytest.mark.parametrize('change', ['unconsumed', 'newer_pause', 'revoked', 'grant', 'generation',
                                   'foreign_root', 'unreadable', 'missing_binding', 'new_owner_pause', 'terminal'])
def test_cold_handoff_needs_current_consumed_authority(host, tmp_path, monkeypatch, change):  # noqa: F811
    _paused_fence(host, tmp_path, monkeypatch)
    ctx = _resume(host, monkeypatch, consume=change != 'unconsumed')
    current = budget_pause.budget_pause_row(host.root, 'held')
    if change == 'unreadable':
        from tests._store_shared import unreadable_result
        unreadable_result(monkeypatch, 'held')
    elif change == 'new_owner_pause':
        from supervisor.owner_pause_control import request_owner_pause
        assert request_owner_pause('held', request_id='new-pause')['ok']
    elif change == 'terminal':
        from ouroboros.task_results import write_task_result
        write_task_result(host.root, 'held', 'completed', result='Already delivered')
    elif change == 'missing_binding':
        queue.ACCEPTANCE_FENCES['held'].pop('paused_continuation', None)
    elif change != 'unconsumed':
        if change == 'newer_pause':
            current.update(pause_id='newer-pause', state=budget_pause.STATE_PAUSED)
        elif change == 'revoked':
            current['grant']['revoked_at'] = 'revoked'
        elif change == 'grant':
            current['grant']['grant_id'] = 'another-grant'
        elif change == 'generation':
            current['grant']['generation'] += 1
        elif change == 'foreign_root':
            current['root_task_id'] = 'another-root'
        budget_pause.set_budget_pause(host.root, 'held', current)
    before = copy.deepcopy(queue.ACCEPTANCE_FENCES)
    assert not loop._begin_task_acceptance_fence(ctx, 'held')[0]
    assert queue.ACCEPTANCE_FENCES == before


@pytest.mark.parametrize('owner', ['sealed', 'direct', 'legacy', 'foreign_pid', 'foreign_attempt', 'foreign_task'])
def test_cold_pause_never_transfers_an_unbound_owner(host, tmp_path, monkeypatch, owner):  # noqa: F811
    _paused_fence(host, tmp_path, monkeypatch, owner_change=owner)
    ctx = _resume(host, monkeypatch)
    before = copy.deepcopy(queue.ACCEPTANCE_FENCES)
    assert not loop._begin_task_acceptance_fence(ctx, 'held')[0]
    assert queue.ACCEPTANCE_FENCES == before


@pytest.mark.parametrize('consume', [False, True])
def test_a_new_pid_cannot_reuse_the_old_token(host, tmp_path, monkeypatch, consume):  # noqa: F811
    _, prior = _paused_fence(host, tmp_path, monkeypatch)
    _resume(host, monkeypatch, consume=consume)
    before = copy.deepcopy(queue.ACCEPTANCE_FENCES)
    response = queue.transition_acceptance_fence(action='begin', token=prior['token'], task_id='held',
        root_task_id='held', pooled_owner={'task_attempt': 2, 'pid': 2222})
    assert not response['ok'] and queue.ACCEPTANCE_FENCES == before


@pytest.mark.parametrize('reason', ['owner', 'budget'])
def test_unconsumed_worker_death_preserves_the_same_pause_binding(host, tmp_path, monkeypatch, reason):  # noqa: F811
    from supervisor import worker_health
    from tests.test_worker_crash_retry import _make_worker

    _, prior = _paused_fence(host, tmp_path, monkeypatch, reason)
    _resume(host, monkeypatch, consume=False)
    worker = _make_worker(busy_task_id='held', exitcode=1)
    worker.proc.pid = 2222
    workers.WORKERS[0] = worker
    jobs = stdqueue.Queue()
    monkeypatch.setattr(workers, '_LAST_SPAWN_TIME', 0)
    monkeypatch.setattr(workers, 'CRASH_TS', [])
    monkeypatch.setattr(queue, '_reap_queue', jobs)
    monkeypatch.setattr(queue, '_ensure_reaper_started', lambda: None)
    monkeypatch.setattr(workers, 'respawn_worker', lambda *_a, **_k: None)
    monkeypatch.setattr('ouroboros.headless.prepare_terminal_task_files',
                        lambda *_a, **_k: {'terminal_source_present': False})
    workers.ensure_workers_healthy()
    worker_health.recover_confirmed_dead_worker(jobs.get_nowait())
    assert queue.ACCEPTANCE_FENCES['held']['token'] == prior['token']
    assert len(host.pending) == 1 and host.pending[0]['_attempt'] == 2
    binding = copy.deepcopy(queue.ACCEPTANCE_FENCES['held'].get('paused_continuation'))
    assert queue.resume_budget_paused_task('held')['ok']
    if reason == 'owner':
        from tests.test_project_hold_recovery import worker as free_worker
        sent = free_worker(host, monkeypatch)
        workers.assign_tasks()
        current = budget_pause.budget_pause_row(host.root, 'held')
        print('OWNER_REGRANT', {'sent': len(sent), 'state': current['state'], 'grant': current['grant']})
        # Existing owner-Pause regrant is re-fenced at assignment (#1730); no new admission here.
        assert not sent and current['state'] == budget_pause.STATE_PAUSED
        assert current['grant'].get('revoked_at') and len(host.pending) == 1
        assert current['grant']['revoke_reason'] == 'root_resume_generation_stale'
        assert queue.ACCEPTANCE_FENCES['held'].get('paused_continuation') == binding
        return
    ctx = _resume(host, monkeypatch, pid=3333)
    assert loop._begin_task_acceptance_fence(ctx, 'held')[0]
    assert queue.ACCEPTANCE_FENCES['held']['pooled_owner']['pid'] == 3333
