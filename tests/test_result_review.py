"""``review_change(subject="result")``: one review machinery for a change and for a task's result.

Every seat of a result wave answers one question — does this task's result do what was asked — over
a subject frozen at the call (what was asked, the reply, what the task produced, the host-attested
verification). The wave is review_change's own: the same panel rules, $0 reuse of the same subject,
the ledger record, and its own paid-cycle count beside change reviews. Task review Off refuses it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import ouroboros.review_substrate as substrate
from ouroboros import review_ledger
from ouroboros.tools.review_change import ReviewChangeArgumentError, parse_request, run_review_change
from tests import _contributor_packet_shared as shared
from tests.test_review_change_end_to_end import _foreign_project

CRITICAL = '[{"item": "missing_section", "verdict": "FAIL", "severity": "critical", "reason": "The question asked for three findings; the reply gives two."}]'


def _seam(sends, answers=None):
    answers = answers or {}

    def answer(request, slot, actor, *, retry_state, pending_invocation_checkpoint):
        actor.raw_text = answers.get(slot.slot_id, "[]")  # contract A: [] is a clean answer
        return actor

    return shared.golden_physical_seam(sends, answer=answer)


def _reply(ctx, text):
    ctx._delivery_candidate = SimpleNamespace(full_text=text)


def test_every_seat_answers_the_one_result_question_and_the_same_subject_is_reused_free(tmp_path, monkeypatch):
    ctx, _project = _foreign_project(tmp_path, monkeypatch)
    _reply(ctx, "The report is ready: report.md lists the three findings.")
    sends: list[dict] = []
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends))

    first = run_review_change(ctx, subject="result", goal="Check the report answers the question")

    assert (first["state"], first["aggregate"], first["reused"]) == ("settled", "PASS", False), first
    assert first["per_question"] == {"result": "PASS"}, "a result wave asks no change or coupling question"
    assert {tuple(row["parts"]) for row in first["rows"]} == {("result",)}
    record = review_ledger.load_record(ctx.drive_root, first["record_id"])
    assert record["surface"] == "result" and record["subject"]["kind"] == "result"
    assert sorted(send["slot_id"] for send in sends) == ["s1", "t1", "t2"]

    again = run_review_change(ctx, subject="result", goal="Check the report answers the question")
    assert again["reused"] is True and again["record_id"] == first["record_id"] and len(sends) == 3

    _reply(ctx, "The report is ready: report.md lists four findings.")
    changed = run_review_change(ctx, subject="result", goal="Check the report answers the question")
    assert changed["reused"] is False and changed["record_id"] != first["record_id"] and len(sends) == 6


def test_a_critical_finding_fails_the_result(tmp_path, monkeypatch):
    ctx, _project = _foreign_project(tmp_path, monkeypatch)
    sends: list[dict] = []
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends, {"t1": CRITICAL}))

    result = run_review_change(ctx, subject="result", answer="Two findings are in report.md.")

    assert result["aggregate"] == "FAIL" and result["per_question"] == {"result": "FAIL"}
    [finding] = result["findings"]["critical_findings"]
    assert finding["seat_id"] == "t1" and finding["part"] == "result" and finding["item"] == "missing_section"


def test_task_review_off_refuses_a_result_review_before_any_seat(tmp_path, monkeypatch):
    ctx, project = _foreign_project(tmp_path, monkeypatch)
    _reply(ctx, "Done.")
    sends: list[dict] = []
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends))
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")

    with pytest.raises(ReviewChangeArgumentError, match="result review is off"):
        run_review_change(ctx, subject="result")
    assert sends == []

    (project / "app.py").write_text("VALUE = 2  # staged\n", encoding="utf-8")
    shared.git(project, "add", "app.py")
    change = run_review_change(ctx, subject="index", goal="Bump", scope="app.py")
    assert change["aggregate"] != "NOT_DISPATCHED" and sends, "change review stays available in Off"


def test_result_reviews_keep_their_own_paid_cycle_count(tmp_path, monkeypatch):
    """Two kinds of review, two counts: a change review that used the per-task ceiling never refuses an
    owed result review, and result reviews meet their own ceiling."""
    ctx, project = _foreign_project(tmp_path, monkeypatch)
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    sends: list[dict] = []
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends))
    (project / "app.py").write_text("VALUE = 2  # staged\n", encoding="utf-8")
    shared.git(project, "add", "app.py")
    assert run_review_change(ctx, subject="index", goal="Bump", scope="app.py")["aggregate"] != "NOT_DISPATCHED"

    first = run_review_change(ctx, subject="result", answer="Bumped VALUE to 2.")
    assert first["aggregate"] == "PASS" and first["dispatch_refusal"] is None

    second = run_review_change(ctx, subject="result", answer="Bumped VALUE to 2 and documented it.")
    assert second["aggregate"] == "NOT_DISPATCHED"
    assert second["dispatch_refusal"]["kind"] == "review_cycles_exhausted"


@pytest.mark.parametrize("args, problem", [
    ({"subject": "result", "base": "HEAD~1"}, "root, workspace_root, base and head do not apply"),
    ({"subject": "result", "workspace_root": "/tmp"}, "root, workspace_root, base and head do not apply"),
    ({"subject": "result", "coupling_only": ["x"]}, "coupling_only belongs to a change review"),
    ({"subject": "index", "answer": "Done."}, "answer belongs to subject=result"),
    ({"subject": "index", "surface": "result"}, "subject=result goes with surface=result"),
])
def test_a_result_call_that_cannot_mean_anything_is_refused_before_payment(args, problem):
    with pytest.raises(ReviewChangeArgumentError, match=problem.replace("(", r"\(").replace(")", r"\)")):
        parse_request(args)


def test_the_result_subject_implies_its_surface():
    request = parse_request({"subject": "result", "answer": "Done."})
    assert (request.surface, request.answer) == ("result", "Done.")
