"""Workspace release follows confirmed process termination (external audit point 2, #1772).

The engine publishes a run's terminal only after its scope is settled and projects a
compact outcome into ``GET /v2/runs/:id`` as ``summary.processScope``; an older engine
and historical runs carry none (``null``: unknown). A checking copy is released only when
the run succeeded AND the scope is absent (today's behaviour) or settled; present and
unsettled keeps the copy, keeps and delivers the verdict, and starts no new review.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from ouroboros import review_checking as rc

REPO = pathlib.Path(__file__).resolve().parents[1]
SETTLED = {"settled": True, "coverage": "marker+capture", "completeness": "complete"}
UNSETTLED = {"settled": False, "coverage": "marker+capture", "completeness": "partial",
             "completeness_reason": "survivors", "survivors": [{"pid": 4242, "start_token": "x"}]}


@pytest.mark.parametrize("detail,released", [
    ({"summary": {"state": "succeeded"}}, True),                             # older engine: no projection
    ({"summary": {"state": "succeeded", "processScope": None}}, True),       # historical run: null is unknown
    ({"summary": {"state": "succeeded", "processScope": SETTLED}}, True),    # settled: every writer ended
    ({"summary": {"state": "succeeded", "processScope": UNSETTLED}}, False),  # survivors may still write
    ({"summary": {"state": "succeeded", "processScope": {"coverage": "tree_only"}}}, False),  # present, no settled flag
    ({"summary": {"state": "succeeded", "processScope": "settled"}}, False),  # malformed: never a release
    ({"summary": {"state": "failed", "processScope": SETTLED}}, False),      # a settled failure keeps its copy
    ({"summary": {"state": "running", "processScope": SETTLED}}, False),
])
def test_release_needs_success_and_an_absent_or_settled_scope(detail, released):
    assert rc.checking_writers_settled(detail) is released


@pytest.mark.parametrize("scope", [None, SETTLED, UNSETTLED])
def test_an_unsettled_scope_keeps_the_copy_while_the_evidence_is_still_captured(tmp_path, scope):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "subject.txt").write_bytes(b"frozen subject\n")
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(root, data_root=drive)
    area = pathlib.Path(rc.prepare_seat_area(drive, "task", "op", source))
    (area / "written.txt").write_bytes(b"a reviewer wrote this\n")
    detail = {"summary": {"state": "succeeded", **({} if scope is None else {"processScope": scope})}}

    evidence = rc.capture_seat_evidence(drive, "task", "op", release=rc.checking_writers_settled(detail))

    retained = pathlib.Path(evidence["evidence_dir"]) / "files" / "written.txt"
    assert retained.read_bytes() == b"a reviewer wrote this\n", "the verdict's evidence is captured either way"
    if scope is UNSETTLED:
        assert evidence["status"] == "retained" and area.is_dir(), "survivors may still write: the copy stays"
    else:
        assert not area.is_dir() or evidence["status"] != "retained", "absent or settled: today's release"


def test_the_release_decision_never_starts_another_review():
    """``checking_writers_settled`` is consulted only as the ``release=`` argument of the
    evidence capture; no caller branches a new review or a retry on it."""
    pattern_hits = []
    for path in (REPO / "ouroboros").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "checking_writers_settled":
                parent_ok = False
                for outer in ast.walk(tree):
                    if isinstance(outer, ast.Call) and any(kw.arg == "release" and kw.value is node for kw in outer.keywords):
                        parent_ok = True
                pattern_hits.append((path.name, node.lineno, parent_ok))
    assert pattern_hits, "the decision is consumed somewhere"
    assert all(ok for _name, _line, ok in pattern_hits), pattern_hits
