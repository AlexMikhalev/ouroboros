"""The server's own work projection of controlled census rows.

Tests that answer ``/api/state`` themselves (browser transports, the web fixture) send the
``works``/``rooms`` fields ``gateway/work_projection.works_snapshot`` builds from the same
census rows, so the page never reads a shape the server does not send. The projection's
stored reads (task results, owner fences, retained delegated runs, answer upkeep) are
answered from the given maps instead of a data directory.
"""
from __future__ import annotations

import json
import tempfile
from unittest import mock

from ouroboros.gateway import work_projection as wp


def work_fields(owners, raw=None, *, results=None, fences=None, held=None, complete=True):
    """``works``, ``rooms``, ``work_summary``, ``works_complete`` and ``works_gaps`` for these rows, as JSON."""
    results, fences, held = dict(results or {}), dict(fences or {}), dict(held or {})
    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(wp._Observations, "result", lambda self, task_id: results.get(task_id)), \
            mock.patch.object(wp._Observations, "fence", lambda self, root_id, drive_root=None: fences.get(root_id, {})), \
            mock.patch.object(wp, "_held_runs", lambda drive_root: held), \
            mock.patch.object(wp, "_inflight_upkeep", lambda drive_root, known: []):
        snapshot = wp.works_snapshot(root, [dict(row) for row in owners], dict(raw or {}), complete=complete)
    return json.loads(json.dumps(snapshot))
