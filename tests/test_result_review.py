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


# --- The delivery step: binding and the Required floor ------------------------------------------

def _floor_ctx(tmp_path, monkeypatch, *, mode="required", enforcement="advisory", root=True):
    ctx, _project = _foreign_project(tmp_path, monkeypatch)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", mode)
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    if not root:
        ctx.parent_task_id = "parent-task"
        ctx.task_metadata = {"parent_task_id": "parent-task", "root_task_id": "parent-task"}
    return ctx


def _produced_answer_protocol(monkeypatch):
    """A benchmark-style result: the adapter's answer protocol makes the answer line produced work."""
    monkeypatch.setattr("ouroboros.contracts.task_contract.answer_protocol_active", lambda ctx: True)


def test_required_reviews_a_root_result_with_produced_work_once_and_shows_its_findings_first(tmp_path, monkeypatch):
    from ouroboros.tools.review_result import result_review_at_delivery

    ctx = _floor_ctx(tmp_path, monkeypatch)
    _produced_answer_protocol(monkeypatch)
    sends: list[dict] = []
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends, {"t1": CRITICAL}))
    shown, trace = [], {"tool_calls": []}
    answer = "FINAL ANSWER: 2"

    assert result_review_at_delivery(ctx, answer, trace, shown.append) is True, "Advisory: findings first"
    assert len(sends) == 3 and "[Result review]" in shown[0] and "FAIL" in shown[0] and "You decide" in shown[0]
    [run] = trace["review_runs"]
    assert (run["surface"], run["aggregate_signal"], run["authority"]) == ("result", "FAIL", "host_root")

    assert result_review_at_delivery(ctx, answer, trace, shown.append) is False, "the mind decided: deliver"
    assert len(sends) == 3 and len(shown) == 1, "the same result is never reviewed or shown twice"


def test_blocking_holds_the_same_failed_result_until_it_changes_and_passes(tmp_path, monkeypatch):
    from ouroboros.tools.review_result import result_review_at_delivery

    ctx = _floor_ctx(tmp_path, monkeypatch, enforcement="blocking")
    _produced_answer_protocol(monkeypatch)
    sends: list[dict] = []
    answers = {"t1": CRITICAL}
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends, answers))
    shown, trace = [], {"tool_calls": []}

    assert result_review_at_delivery(ctx, "FINAL ANSWER: 2", trace, shown.append) is True
    assert result_review_at_delivery(ctx, "FINAL ANSWER: 2", trace, shown.append) is True, "Blocking holds again"
    assert "not delivered until a review passes it" in shown[-1] and len(sends) == 3
    answers.clear()  # the revised result satisfies the reviewers
    assert result_review_at_delivery(ctx, "FINAL ANSWER: 3", trace, shown.append) is False
    assert len(sends) == 6
    current = [run for run in trace["review_runs"] if not run.get("superseded_by_revision")]
    assert [run["aggregate_signal"] for run in current] == ["PASS"]


def test_nothing_is_forced_for_a_child_auto_off_or_a_plain_text_answer(tmp_path, monkeypatch):
    from ouroboros.tools.review_result import result_review_at_delivery

    sends: list[dict] = []
    for kwargs, protocol in (({"root": False}, True), ({"mode": "auto"}, True), ({"mode": "off"}, True), ({}, False)):
        ctx = _floor_ctx(tmp_path / str(len(sends)) / repr(sorted(kwargs.items())).replace("/", ""), monkeypatch, **kwargs)
        if protocol:
            _produced_answer_protocol(monkeypatch)
        else:
            monkeypatch.setattr("ouroboros.contracts.task_contract.answer_protocol_active", lambda ctx: False)
        monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends))
        trace = {"tool_calls": []}
        assert result_review_at_delivery(ctx, "FINAL ANSWER: 2", trace, lambda text: None) is False, kwargs
        assert not trace.get("review_runs")
    assert sends == []


def test_a_mind_reviewed_result_is_bound_at_delivery_in_auto_without_a_second_wave(tmp_path, monkeypatch):
    from ouroboros.outcomes import _objective_axis, _review_axis
    from ouroboros.tools.review_result import result_review_at_delivery

    ctx = _floor_ctx(tmp_path, monkeypatch, mode="auto")
    sends: list[dict] = []
    monkeypatch.setattr(substrate.ReviewCoordinator, "_run_slot", _seam(sends))
    answer = "The summary is in summary.md."
    assert run_review_change(ctx, subject="result", answer=answer)["aggregate"] == "PASS"
    trace = {"tool_calls": []}

    assert result_review_at_delivery(ctx, answer, trace, lambda text: None) is False
    assert len(sends) == 3, "binding never buys a second wave"
    review = _review_axis(trace)
    assert review["status"] == "pass" and review["source"] == "result_review"
    assert _objective_axis(review) == {"status": "pass", "source": "result_review", "review_status": "pass"}
