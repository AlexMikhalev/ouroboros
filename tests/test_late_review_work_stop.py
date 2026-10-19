"""A review still running after the answer keeps its work's Stop, exactly where the Stop door reaches.

The answered root has left the queue; only its review operation and that operation's delegated run still live.
The work projection offers Stop where the door reaches (``queue_transitions.task_subtree_is_live``: a live queue
or direct row of the tree, or the root's live review operation), so this work keeps it. A delegated run outside a
review keeps no Stop (tests/test_work_status.py, the started delegated runs of an ended root). The late review
that drove this through the deleted task acceptance is a review_change wave now; the rule is the projection's.
"""
import os
from unittest import mock

import pytest

from ouroboros.gateway import work_projection as wp
from ouroboros.platform_layer import process_start_time
from ouroboros.review_operation import OPERATION_DISPATCHED

ROOT = "answered-root"


def _works(tmp_path, state):
    stored = {ROOT: {"task_id": ROOT, "status": "completed", "chat_id": 42, "root_task_id": ROOT,
                     "review_operations": {"wave-owner": {
                         "state": state, "surface": "multi_model_review", "retry_key": "wave",
                         # another live process's controller (this process's own would need a live registration)
                         "controller": {"pid": os.getppid(), "birth": process_start_time(os.getppid()),
                                        "session": "another-process"}}}}}
    with mock.patch.object(wp._Observations, "result", lambda self, task_id: stored.get(task_id)), \
            mock.patch.object(wp, "_open_runs", lambda drive_root: {ROOT: ["review-run"]}), \
            mock.patch.object(wp, "_inflight_upkeep", lambda drive_root, known: []):
        return wp.works_snapshot(tmp_path, [], {})["works"]


@pytest.mark.parametrize("state, stoppable", [(OPERATION_DISPATCHED, True), ("closed", False)])
def test_a_live_review_after_the_answer_keeps_its_works_stop(tmp_path, state, stoppable):
    """Both directions: the root's open review pointer under a live controller keeps Stop on the work its
    delegated run still holds; the same work with the pointer closed offers no Stop (the door would answer 404)."""
    [work] = _works(tmp_path, state)
    assert work["episode"]["task_id"] == ROOT, work
    stops = [entry["target"] for entry in work["controls"] if entry["action"] == "stop"]
    assert stops == ([ROOT] if stoppable else []), work
