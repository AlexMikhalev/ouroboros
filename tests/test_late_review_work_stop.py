"""A live acceptance review after the answer keeps its work's Stop, and the real Stop door takes it.

The answered root has left the queue; only its review operation and that operation's delegated run still live.
The work projection offers Stop exactly where the door reaches (queue_transitions.task_subtree_is_live), so
this work keeps it, and the Stop prevents every paid send after the one already in flight. A delegated run
outside a review keeps no Stop (tests/test_work_status.py, the started delegated runs of an ended root).
"""
import queue

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import review_operation
from ouroboros.gateway.tasks import api_task_cancel
from tests._budget_pause_exact_helpers import _install_queue
from tests._review_session_route_shared import (  # noqa: F401 - fixtures
    _owned_gateway_uses_each_test_transport, _terminal_detail, fake_route,
)
from tests._work_projection_shared import state_fields
from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool
from tests.test_acceptance_history import _caller, _request, _source
from tests.test_acceptance_late_consumers import delivered, late  # noqa: F401 - fixture
from tests.test_review_operation_collection import fresh_sends  # noqa: F401 - fixture
from tests.test_review_operation_lifetime import until


def test_a_live_late_review_keeps_its_works_stop_and_the_door_takes_it(late, fake_route, tmp_path, monkeypatch):  # noqa: F811
    f = delivered(tmp_path, monkeypatch)
    q, _state, _workers = _install_queue(f.root, monkeypatch)
    set_review_pool(monkeypatch, pool_roster(pool_seat('late-review', 'fake-review=fake-small', kind='agent_session',
                                                       access='readonly')))
    fake_route.nonterminal = True
    real_start = fake_route.start_run

    def start(self, request, **kw):
        return {**real_start(self, request, **kw), 'runDir': str(tmp_path / 'review-run')}

    def cancel(self, run_id, *, reason=''):
        self.cancels.append((run_id, reason))
        fake_route.detail = _terminal_detail('', state='cancelled')
        fake_route.nonterminal = False
        return {'accepted': True}

    monkeypatch.setattr(fake_route, 'start_run', start)
    monkeypatch.setattr(fake_route, 'cancel_run', cancel)
    monkeypatch.setattr('ouroboros.claudexor_daemon.read_owned_gateway', lambda: fake_route())
    ctx = _caller(f)
    ctx.event_queue = queue.Queue()
    try:
        launched = _request(f, ctx, _source(ctx, text='Review this delivered historical answer.'))
        assert launched['status'] in {'pending', 'announced', 'published', 'settled'}, launched
        from ouroboros.delegate_custody_reconcile import open_runs
        until(lambda: open_runs(f.root))
        assert not q.PENDING and not q.RUNNING
        assert review_operation.task_has_live_review_operation(f.root, f.tid)
        work = next(w for w in state_fields(f.root, {}, direct_turns=[])['works'] if w['episode']['task_id'] == f.tid)
        assert [c['target'] for c in work['controls'] if c['action'] == 'stop'] == [f.tid], work
        app = Starlette(routes=[Route('/api/tasks/{task_id}/cancel', api_task_cancel, methods=['POST'])])
        app.state.drive_root = f.root
        with TestClient(app) as client:
            response = client.post(f'/api/tasks/{f.tid}/cancel', json={'cascade': True})
        assert response.status_code in {200, 202}, response.json()
        assert review_operation.operation_stop_state(f.root, f.tid) == 'cancelled'
        # The review answer already sent arrives; the Stop keeps every following paid send from starting.
        fake_route.nonterminal = False
        until(lambda: not review_operation._LIVE)
        assert q.drive_cancel_intent_scope(f.tid) == q.CANCEL_CANCELLED
        assert not late.calls and not list(open_runs(f.root))
    finally:
        fake_route.nonterminal = False
        until(lambda: not review_operation._LIVE)
