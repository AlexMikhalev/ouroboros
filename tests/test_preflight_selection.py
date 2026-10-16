"""Selected execution, honest inventory and the full-only consumer boundaries."""
import json
from types import SimpleNamespace

import pytest

from ouroboros import commit_admission as admission, preflight_runner as pr, test_selection as selection
from ouroboros.preflight_qualification import full_proof_error, qualify_commit
from tests.test_preflight_runner import _make_repo, _commit_all

pytestmark = pytest.mark.serial  # finite nested pytest controllers; one at a time


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TEST_WORKERS", "2")
    monkeypatch.setenv("OURO_TEST_WORKER_BUDGET", "2")  # nested slots: the selected gate must really run two workers
    monkeypatch.delenv("OUROBOROS_PREFLIGHT_SERIAL", raising=False)
    monkeypatch.delenv("OUROBOROS_PREFLIGHT_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(selection, "FLOOR_TESTS", {"tests/test_floor.py": "floor witness"})
    repo = _make_repo(tmp_path, {
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "tests/test_floor.py": "def test_floor(): assert True\n",
        "leaf.py": "value = 1\n",
        "tests/test_leaf.py": "from leaf import value\ndef test_leaf(): assert value == 2\n",
        "tests/test_unrelated.py": "raise AssertionError('unrelated collection must be skipped')\n",
    })
    (repo / "leaf.py").write_text("value = 2\n", encoding="utf-8")
    return SimpleNamespace(repo_dir=repo, drive_root=tmp_path / "data", task_id="selected-proof",
                           task_metadata={}, current_task_type="task", emit_progress_fn=lambda *a: None)


def test_real_selected_run_records_inventory_and_cannot_qualify_managed_update(candidate, monkeypatch):
    from ouroboros import preflight_receipts
    from ouroboros.tools import git

    ctx = candidate
    assert pr.run_hermetic_pytest(ctx.repo_dir, ctx=ctx, selected=True, phase=pr.PRE_COMMIT_PHASE) is None
    proof = ctx._preflight_test_proof
    assert proof and proof.tier == "selected"
    assert not admission.preflight_test_proof_matches(ctx, ctx.repo_dir)
    assert admission.preflight_test_proof_matches(ctx, ctx.repo_dir, require_full=False)
    assert not admission.preflight_test_workload_unchanged(proof, ctx.repo_dir, timeout=1800, pytest_args=None)
    assert full_proof_error(ctx, ctx.repo_dir)
    monkeypatch.setattr(git, "_authorized_managed_update_resolver", lambda c: True)
    assert git._managed_candidate_needs_proof(ctx)
    row = ctx._preflight_test_disclosure
    assert row["tier"] == "selected" and row["candidate"] == proof.tree
    assert row["base"] == proof.head and row["policy"] == proof.selection.policy
    assert row["expected_files"] == ["tests/test_floor.py", "tests/test_leaf.py"]
    assert row["omitted_files"] == 1
    assert row["receipt"]["status"] == "declined"
    assert "selected" in row["receipt"]["reason"]
    assert not list(ctx.drive_root.glob("state/preflight_receipts/*.json"))
    inventory = row["inventory"]
    assert sorted(inventory["parallel"]["expected"]) == sorted(inventory["parallel"]["executed"])
    assert len(inventory["parallel"]["expected"]) == 2
    assert inventory["serial"]["expected"] == []
    exhibit = preflight_receipts.execution_exhibit(ctx)
    assert exhibit["tier"] == "selected" and exhibit["inventory"] == inventory
    assert "selected" in preflight_receipts.execution_section(exhibit)
    monkeypatch.setattr(pr, "_execute_pytest_pass", lambda *a: pytest.fail("unchanged selected proof should reuse"))
    assert pr.run_hermetic_pytest(ctx.repo_dir, ctx=ctx, selected=True, phase=pr.PRE_COMMIT_PHASE) is None
    assert ctx._preflight_test_disclosure["action"] == "reused"
    assert ctx._preflight_test_disclosure["inventory"] == inventory


def test_selected_widening_executes_the_named_consumer(candidate):
    error = pr.run_hermetic_pytest(candidate.repo_dir, ctx=candidate, selected=True, phase=pr.PRE_COMMIT_PHASE,
                                  extra_tests=["tests/test_unrelated.py"], test_reason="dynamic dispatch")
    assert error and "unrelated collection" in error
    assert candidate._preflight_test_proof is None


def test_apparatus_change_executes_full_and_reports_actual_failure(candidate):
    with (candidate.repo_dir / "pytest.ini").open("a", encoding="utf-8") as stream:
        stream.write("\n# verifier changed\n")
    error = pr.run_hermetic_pytest(candidate.repo_dir, ctx=candidate, selected=True, phase=pr.PRE_COMMIT_PHASE)
    assert error and "unrelated collection" in error
    assert candidate._preflight_test_proof is None


@pytest.mark.parametrize("mode", ["missing", "empty"])
def test_required_floor_cannot_silently_disappear(candidate, mode):
    (candidate.repo_dir / "tests/test_unrelated.py").write_text("def test_other(): pass\n", encoding="utf-8")
    path = candidate.repo_dir / "tests/test_floor.py"
    path.unlink() if mode == "missing" else path.write_text("# no witness\n", encoding="utf-8")
    error = pr.run_hermetic_pytest(candidate.repo_dir, ctx=candidate, selected=True, phase=pr.PRE_COMMIT_PHASE)
    assert error and "floor" in error
    assert candidate._preflight_test_proof is None


def test_selected_receipt_is_refused_before_installation_probe(candidate, monkeypatch, tmp_path):
    from ouroboros.preflight_receipts import FullGateReceipt
    plan = selection.select_tests(candidate.repo_dir)
    proof = admission.capture_preflight_test_subject(candidate.repo_dir, selection=plan)
    passes = pr._preflight_pass_specs(probe_module="nonce")
    monkeypatch.setattr("ouroboros.preflight_receipts._installation_identity", lambda *a: pytest.fail("selected proof"))
    receipt = FullGateReceipt.prepare(candidate, proof, passes, "nonce", "python", tmp_path, candidate.repo_dir)
    assert not receipt.covered() and receipt.path is None


def test_undisclosed_execution_keeps_the_proofs_actual_tier(tmp_path, monkeypatch):
    from ouroboros.preflight_receipts import execution_exhibit, execution_section
    from ouroboros.preflight_inventory import scope_record

    workload = ((("custom", ("tests/test_leaf.py",), False),), 1800, "env", (), (), "probe")
    proof = admission.PreflightTestProof("tree", "index", workload, "head")
    ctx = SimpleNamespace(repo_dir=tmp_path, _preflight_test_proof=proof, _preflight_tests_passed=True)
    monkeypatch.setattr(admission, "preflight_test_proof_matches", lambda *a, **kw: True)
    exhibit = execution_exhibit(ctx)
    assert exhibit["tier"] == "selected" and exhibit["evidence"] == "undisclosed"
    assert "The selected hermetic test gate" in execution_section(exhibit)
    assert scope_record(proof)["rule"] == "custom pytest selection"


def test_full_qualification_tests_the_named_commit_not_later_dirty_bytes(candidate):
    repo = candidate.repo_dir
    (repo / "tests/test_unrelated.py").write_text("def test_other(): assert True\n", encoding="utf-8")
    _commit_all(repo)
    sha = pr._run_git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    (repo / "leaf.py").write_text("value = 99\n", encoding="utf-8")
    record = qualify_commit(repo, sha, candidate.drive_root)
    assert record["tier"] == "full" and record["head"] == sha
    assert (repo / "leaf.py").read_text(encoding="utf-8") == "value = 99\n"


def test_evolution_publication_refuses_selected_but_accepts_full(candidate, monkeypatch):
    from ouroboros.tools import git
    from ouroboros.tools.git_evolution import _evolution_publication_stopped_result

    ctx = candidate
    ctx.current_task_type = "evolution"
    monkeypatch.setattr(git, "_evolution_commit_authority", lambda *a, **k: ({}, {"ok": True}))
    assert pr.run_hermetic_pytest(ctx.repo_dir, ctx=ctx, selected=True, phase=pr.PRE_COMMIT_PHASE) is None
    assert "EVOLUTION_PUBLICATION_STOPPED" in _evolution_publication_stopped_result(ctx, "candidate", ctx._preflight_test_proof.head, "")
    (ctx.repo_dir / "tests/test_unrelated.py").write_text("def test_other(): assert True\n", encoding="utf-8")
    assert pr.run_hermetic_pytest(ctx.repo_dir, ctx=ctx) is None
    assert _evolution_publication_stopped_result(ctx, "candidate", ctx._preflight_test_proof.head, "") == ""


def test_adoption_refuses_failed_qualification_before_writing_handoff(tmp_path, monkeypatch):
    from ouroboros import body_adoption
    from tests.test_body_adoption import Scene

    scene = Scene(tmp_path, monkeypatch)
    scene.commit()
    def failed(*args, **kwargs):
        raise ValueError("selected evidence is not full qualification")
    monkeypatch.setattr("ouroboros.preflight_qualification.qualify_commit", failed)
    with pytest.raises(body_adoption.AdoptionRefused, match="selected evidence") as caught:
        scene.authorize()
    assert caught.value.code == "ADOPTION_TESTS_REQUIRED"
    assert not body_adoption.read(scene.data)


def test_adoption_does_not_replace_a_restart_armed_during_qualification(tmp_path, monkeypatch):
    from ouroboros import body_adoption
    from tests.test_body_adoption import Scene

    scene = Scene(tmp_path, monkeypatch)
    scene.commit()
    def qualify(*args, **kwargs):
        directory = body_adoption.helper_dir(scene.data)
        directory.mkdir(parents=True, exist_ok=True)
        body_adoption.body_switch.record(str(directory), {"id": "other", "phase": "armed"}, "armed", "other restart")
        return {"tier": "full"}
    monkeypatch.setattr("ouroboros.preflight_qualification.qualify_commit", qualify)
    with pytest.raises(body_adoption.AdoptionRefused) as caught:
        scene.authorize()
    assert caught.value.code == "ADOPTION_STATE_CHANGED"
    assert body_adoption.read(scene.data)["id"] == "other"
    assert body_adoption.read(scene.data)["phase"] == "armed"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unfinished"])
def test_inventory_rejects_incomplete_or_duplicate_execution(tmp_path, mutation):
    from ouroboros.preflight_inventory import read_inventory
    rows = [{"event": "start"}, {"event": "collection", "ids": ["tests/test_x.py::test_x"]},
            {"event": "report", "nodeid": "tests/test_x.py::test_x", "when": "teardown", "outcome": "passed"},
            {"event": "finish", "exitcode": 0}]
    if mutation == "missing":
        rows.pop(2)
    elif mutation == "duplicate":
        rows.insert(2, rows[2])
    else:
        rows.pop()
    (tmp_path / "inventory-parallel.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert read_inventory(tmp_path, "parallel")[1]
