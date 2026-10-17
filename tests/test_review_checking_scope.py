"""Workspace release follows the copy-use policy for the exact run detail (external audit point 2, #1772).

The engine's terminal detail projects its compact cleanup outcome in
``summary.processScope``: an explicit ``null`` means no readable record; an older
engine omits the key. Success releases with an absent key or a settled scope.
Failed, cancelled or interrupted runs require literal ``settled: true`` instead;
null, invalid or unsettled failure scopes keep the copy. Complete capture still
precedes release, and no copy policy turns a failed execution into a reviewer verdict.
The owner's Windows choice (2026-10-13) allows a census-less SUCCESS to release
under a weaker permission: the receipt names its basis beside the engine's unchanged
facts, ``settled`` still false. The failed-run policy is covered independently too.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

from ouroboros import review_checking as rc

REPO = pathlib.Path(__file__).resolve().parents[1]
# The engine's compact projection of a reap (counts, not pids).
SETTLED = {"settled": True, "coverage": "marker+capture", "completeness": "complete",
           "census": "ok", "survivors": 0, "unreadable": 0}
UNSETTLED = {**SETTLED, "settled": False, "survivors": 1}
# Its answer where no environment census exists (Windows: the parent-chain tree kill only).
NO_CENSUS = {"settled": False, "coverage": "tree_only", "completeness": "not_provable",
             "census": "unsupported", "survivors": 0, "unreadable": 0}
ABSENT = object()  # the key itself is missing, which is not the same fact as ``null``
COMPATIBLE = rc.RELEASE_WITHOUT_CENSUS


def succeeded(scope):
    """A succeeded run detail whose ``processScope`` key is omitted only for ``ABSENT``."""
    summary = {"state": "succeeded"}
    if scope is not ABSENT:
        summary["processScope"] = scope
    return {"summary": summary}


def without(scope, key):
    return {name: value for name, value in scope.items() if name != key}


@pytest.mark.parametrize("detail,released", [
    (succeeded(ABSENT), True),                                       # older engine: no projection
    (succeeded(None), False),                                        # explicit null: unknown, never clean
    (succeeded(SETTLED), True),                                      # settled: every writer ended
    (succeeded(UNSETTLED), False),                                   # survivors may still write
    (succeeded({**SETTLED, "settled": False}), False),               # unsettled with nothing counted
    (succeeded(without(SETTLED, "settled")), False),                 # present, no settled flag
    (succeeded("settled"), False),                                   # malformed: never a release
    ({"summary": {"state": "failed", "processScope": SETTLED}}, True),   # published A2: literal settlement permits copy release
    ({"summary": {"state": "running", "processScope": SETTLED}}, False),
    (succeeded(NO_CENSUS), COMPATIBLE),                              # no census: the weaker permission
    (succeeded({**NO_CENSUS, "durationMs": 7}), COMPATIBLE),         # an additive engine key changes nothing
    ({"summary": {"state": "failed", "processScope": NO_CENSUS}}, False),
    ({"summary": {"state": "cancelled", "processScope": NO_CENSUS}}, False),
    ({"summary": {"state": "interrupted", "processScope": NO_CENSUS}}, False),
    (succeeded({**NO_CENSUS, "survivors": 1}), False),               # a known survivor
    (succeeded({**NO_CENSUS, "unreadable": 1}), False),              # an unreadable known member
    (succeeded({**NO_CENSUS, "census": "failed"}), False),           # mixed: a failed census is unknown
    (succeeded({**NO_CENSUS, "coverage": "marker+capture"}), False),  # mixed: census-backed coverage
    (succeeded({**NO_CENSUS, "completeness": "complete"}), False),   # mixed: no census cannot be complete
    (succeeded({**NO_CENSUS, "survivors": False}), False),           # a bool is not a count
    (succeeded({**NO_CENSUS, "unreadable": "0"}), False),
    (succeeded(without(NO_CENSUS, "unreadable")), False),
    (succeeded(without(NO_CENSUS, "settled")), False),
], ids=["absent", "null", "settled", "unsettled", "unsettled-nothing-counted", "no-settled-flag", "malformed",
        "failed", "running", "no-census", "no-census-extra-key", "no-census-failed", "no-census-cancelled",
        "no-census-interrupted", "no-census-survivor", "no-census-unreadable", "mixed-census-failed",
        "mixed-coverage", "mixed-complete", "bool-count", "string-count", "missing-count", "missing-settled"])
def test_release_follows_terminal_scope_policy(detail, released):
    answer = rc.checking_copy_release(detail)
    assert answer == released and type(answer) is type(released)


@pytest.mark.parametrize("scope,released", [
    (ABSENT, True), (None, False), (SETTLED, True), (UNSETTLED, False), (NO_CENSUS, COMPATIBLE),
    ({**NO_CENSUS, "survivors": 1}, False), ({**NO_CENSUS, "unreadable": 1}, False),
], ids=["absent", "null", "settled", "unsettled", "no-census", "no-census-survivor", "no-census-unreadable"])
def test_evidence_is_always_captured_and_only_the_copy_use_policy_releases_the_copy(tmp_path, scope, released):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "subject.txt").write_bytes(b"frozen subject\n")
    drive = tmp_path / "drive"
    source = rc.freeze_checking_source(root, data_root=drive)
    area = pathlib.Path(rc.prepare_seat_area(drive, "task", "op", source))
    (area / "written.txt").write_bytes(b"a reviewer wrote this\n")
    detail = succeeded(scope)
    assert ("processScope" in detail["summary"]) is (scope is not ABSENT)

    evidence = rc.terminal_seat_evidence(drive, "task", "op", detail)

    retained = pathlib.Path(evidence["evidence_dir"]) / "files" / "written.txt"
    assert evidence["status"] == "retained" and evidence["gaps"] == []
    assert retained.read_bytes() == b"a reviewer wrote this\n", "the verdict's evidence is captured either way"
    assert json.loads((pathlib.Path(evidence["evidence_dir"]) / "summary.json").read_text()) == evidence
    assert evidence["refresh_safe"] is bool(released)
    if released == COMPATIBLE:
        assert evidence["release_basis"] == COMPATIBLE
        assert evidence["process_scope"] == NO_CENSUS and evidence["process_scope"]["settled"] is False, \
            "the engine's facts ride verbatim: a permission, never a settled scope"
    else:
        assert "release_basis" not in evidence and "process_scope" not in evidence, "other receipts are unchanged"
    if released:
        assert not area.exists() and "retention_reason" not in evidence
    else:
        assert evidence["area"] == str(area) and evidence["retention_reason"] == "writer_quiescence_unverified"
        assert (area / "written.txt").read_bytes() == b"a reviewer wrote this\n", "unknown writers keep the copy"
    assert detail == succeeded(scope), "the run detail is read, never rewritten"


def test_an_incomplete_capture_under_the_census_less_permission_keeps_the_copy(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "subject.txt").write_bytes(b"frozen subject\n")
    drive = tmp_path / "drive"
    area = pathlib.Path(rc.prepare_seat_area(drive, "task", "op", rc.freeze_checking_source(root, data_root=drive)))
    (area / "written.txt").write_bytes(b"a reviewer wrote this\n")
    # Reviewer capture has no byte bound; an output it cannot retain is the gap.
    (area / rc.EVIDENCE_DIRNAME).write_bytes(b"a command redirected its log here\n")
    evidence = rc.terminal_seat_evidence(drive, "task", "op", succeeded(NO_CENSUS))
    assert evidence["status"] == "partial" and evidence["area"] == str(area)
    assert evidence["gaps"] == [{"path": rc.EVIDENCE_DIRNAME, "reason": "evidence_root_not_directory"}]
    assert evidence["refresh_safe"] is False and area.is_dir()
    assert "release_basis" not in evidence and "process_scope" not in evidence, "no permission was used"


def test_the_release_decision_never_starts_another_review():
    """``checking_copy_release`` is consulted only as the ``release=`` argument of the
    evidence capture; no caller branches a new review or a retry on it."""
    pattern_hits = []
    for path in (REPO / "ouroboros").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "checking_copy_release":
                parent_ok = False
                for outer in ast.walk(tree):
                    if isinstance(outer, ast.Call) and any(kw.arg == "release" and kw.value is node for kw in outer.keywords):
                        parent_ok = True
                pattern_hits.append((path.name, node.lineno, parent_ok))
    assert pattern_hits, "the decision is consumed somewhere"
    assert all(ok for _name, _line, ok in pattern_hits), pattern_hits
