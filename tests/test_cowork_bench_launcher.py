"""Campaign continuity, resume and custody checks without a provider or Docker."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from devtools.benchmarks.common.launcher_audit import audit_launcher, launcher_paths
from devtools.benchmarks.common.model_slots import runtime_actor_snapshot
from devtools.benchmarks.cowork_bench import campaign as budgets
from devtools.benchmarks.cowork_bench import run_cowork_bench as launcher


@pytest.fixture(autouse=True)
def forbid_live_services(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("offline launcher test tried to contact a provider or Docker")
    monkeypatch.setattr(budgets.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(launcher, "_docker", forbidden)


def write_json(path: pathlib.Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_campaign_restarts_keep_one_baseline_and_prior_spend(tmp_path):
    path = tmp_path / "campaign.json"
    first = budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=500, prior_spend=30)
    first.start(tmp_path / "smoke")
    first.observe(570)
    first.finish(tmp_path / "smoke", outcome="done")
    second = budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=580)
    assert second.spent == 110
    assert second.remaining == 890
    second.start(tmp_path / "recovery")
    second.observe(640)
    second.finish(tmp_path / "recovery", outcome="done")
    third = budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=650)
    assert third.spent == 180
    assert third.remaining == 820
    assert [row["run_root"] for row in third.record["runs"]] == [str(tmp_path / "smoke"), str(tmp_path / "recovery")]
    assert third.record["usage_baseline"] == 500


@pytest.mark.parametrize("changed", [{"fingerprint": "different"}, {"ceiling": 2000}, {"prior_spend": 10}])
def test_existing_campaign_cannot_silently_reset_owner_budget(tmp_path, changed):
    path = tmp_path / "campaign.json"
    budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=100)
    original = path.read_bytes()
    kwargs = {"fingerprint": "key-a", "ceiling": 1000, "usage": 110, **changed}
    with pytest.raises(ValueError):
        budgets.CampaignBudget(path, **kwargs)
    assert path.read_bytes() == original


def test_unsettled_run_and_counter_regression_refuse_a_new_paid_run(tmp_path):
    path = tmp_path / "campaign.json"
    budget = budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=100)
    budget.start(tmp_path / "active")
    with pytest.raises(ValueError, match="unsettled custody"):
        budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=120)
    budget.finish(tmp_path / "active", outcome="stopped")
    with pytest.raises(ValueError, match="decreased"):
        budgets.CampaignBudget(path, fingerprint="key-a", ceiling=1000, usage=99)
    assert json.loads(path.read_text(encoding="utf-8"))["last_usage"] == 100


def test_campaign_lock_excludes_another_launcher_and_releases_on_exception(tmp_path):
    path = tmp_path / "campaign.json"
    with pytest.raises(LookupError):
        with budgets.campaign_lock(path):
            with pytest.raises(RuntimeError, match="another launcher"):
                with budgets.campaign_lock(path):
                    pytest.fail("second launcher acquired the active campaign")
            raise LookupError("caller failed")
    with budgets.campaign_lock(path):
        assert path.with_suffix(".json.lock").exists()
    assert not path.with_suffix(".json.lock").exists()


@pytest.fixture
def selection(tmp_path):
    bench = tmp_path / "bench"
    for task in ("passed", "wrong-answer", "infra", "new"):
        write_json(bench / "tasks" / "finalpool" / task / "task_config.json", {})
    previous = tmp_path / "previous"
    config = {"model": "test-model", "settings": {"effort": "high"}}
    write_json(previous / "run_manifest.json", {
        "harness": {"applied_config": config, "bench": {"head": "bench-sha"}},
        "source": {"head": "seed-sha"},
    })
    rows = [{"instance_id": name, "status": status} for name, status in (
        ("passed", "passed"), ("wrong-answer", "failed"), ("infra", "infra_failed"))]
    (previous / "result_index.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    args = argparse.Namespace(task_file="", task=[], resume_from=[str(previous)], bench_commit="bench-sha")
    return bench, previous, args, config


def test_resume_retries_infrastructure_but_never_genuine_failures(selection):
    bench, _previous, args, config = selection
    assert launcher.select_tasks(bench, args, config, "seed-sha") == ["infra", "new"]
    args.task = ["passed", "wrong-answer"]
    assert launcher.select_tasks(bench, args, config, "seed-sha") == []


@pytest.mark.parametrize("change", ["config", "seed", "benchmark"])
def test_resume_refuses_incompatible_protocol_or_source(selection, change):
    bench, _previous, args, config = selection
    seed = "seed-sha"
    if change == "config":
        config = {**config, "model": "another-model"}
    elif change == "seed":
        seed = "new-seed"
    else:
        args.bench_commit = "new-benchmark"
    with pytest.raises(ValueError, match="configuration/seed differs"):
        launcher.select_tasks(bench, args, config, seed)


@pytest.mark.parametrize("summary,evaluation,runner,expected", [
    ({"bench_status": "success"}, {"pass": True}, {"status": "success"}, "passed"),
    ({"bench_status": "success"}, {"pass": False}, {"status": "success"}, "failed"),
    ({"bench_status": "failed", "reason_code": "timeout"}, {}, {"status": "failed"}, "agent_failed"),
    ({"bench_status": "failed", "infra_failed": True, "reason_code": "llm_api_error"}, {}, {"status": "failed"}, "infra_failed"),
    ({"bench_status": "success"}, {}, {"status": "success"}, "infra_failed"),
    ({}, {}, {"status": "pg_fail"}, "infra_failed"),
    ({}, {}, {}, "not_attempted"),
])
def test_ledger_separates_real_failures_from_recoverable_infrastructure(tmp_path, summary, evaluation, runner, expected):
    if summary:
        write_json(tmp_path / "ouroboros_summary.json", summary)
    if evaluation:
        write_json(tmp_path / "eval_res.json", evaluation)
    row = launcher.ledger_row("task", tmp_path, runner)
    assert row["status"] == expected
    assert row["instance_id"] == "task"


@pytest.fixture
def dry_launcher(tmp_path, monkeypatch):
    bench, out, repo = tmp_path / "source-bench", tmp_path / "run", tmp_path / "seed"
    write_json(bench / "tasks" / "finalpool" / "one" / "task_config.json", {})
    (bench / "configs").mkdir()
    repo.mkdir()
    def admit(path, **kwargs):
        path.parent.mkdir(parents=True)
        return {"source": {"head": "seed-sha"}, "harness": kwargs["harness"], "extra": kwargs["extra"]}
    def clone(command, **_kwargs):
        assert command[:4] == ["git", "clone", "--quiet", "--no-hardlinks"]
        shutil.copytree(command[-2], command[-1])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(launcher, "admit_benchmark_run", admit)
    monkeypatch.setattr(launcher, "bench_provenance", lambda *_a: {"head": launcher.PINNED_BENCH_COMMIT})
    monkeypatch.setattr(launcher, "image_exists", lambda *_a: True)
    monkeypatch.setattr(launcher, "image_labels", lambda *_a: {
        "org.ouroboros.cowork.seed_sha": "seed-sha",
        "org.ouroboros.cowork.bench_sha": launcher.PINNED_BENCH_COMMIT,
    })
    monkeypatch.setattr(launcher, "_git", lambda *_a: "")
    monkeypatch.setattr(launcher.subprocess, "run", clone)
    argv = ["--bench-root", str(bench), "--repo-dir", str(repo), "--run-root", str(out),
            "--docker-host", "unix:///fake.sock", "--min-free-gib", "0", "--dry-run"]
    return out, argv


def test_empty_resume_never_calls_official_runner_with_zero_task_arguments(dry_launcher, monkeypatch):
    out, argv = dry_launcher
    monkeypatch.setattr(launcher, "select_tasks", lambda *_a: [])
    monkeypatch.setattr(launcher, "spawn_supervised", lambda *_a, **_k: pytest.fail("empty remainder launched all tasks"))
    assert launcher.main(argv) == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["extra"]["outcome"] == "nothing_remaining"
    assert manifest["requested_count"] == 0
    assert (out / "result_index.jsonl").read_text(encoding="utf-8") == ""


def test_manifest_metadata_matches_config_received_by_container(dry_launcher):
    out, argv = dry_launcher
    assert launcher.main([*argv, "--effort", "xhigh", "--model", "provider/test-model"]) == 0
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    config = json.loads((out / "bench" / "configs" / launcher.CONFIG_NAME).read_text(encoding="utf-8"))
    assert manifest["harness"]["applied_config"] == config
    observed = runtime_actor_snapshot(config["settings"], expected_model=config["model"])
    assert not observed["mismatches"]
    assert manifest["model_slots"] == observed["model_slots"]
    assert manifest["available_subagents"] == observed["available_subagents"]
    assert manifest["harness"]["fixed_model_actor"] == observed
    assert config["settings"]["OUROBOROS_EFFORT_TASK"] == "xhigh"
    assert not config["settings"].get("OUROBOROS_OR_PROVIDER")


@pytest.fixture
def supervised(tmp_path, monkeypatch):
    bench = tmp_path / "bench"
    bench.mkdir()
    args = launcher.parse_args([])
    args.resource_root = tmp_path
    args.docker_host = "unix:///owned.sock"
    args.selected_tasks = ["unstarted"]
    args.min_free_gib = args.min_root_free_gib = 0
    env = {"COWORK_STOP_FILE": str(tmp_path / "resource_stop"), "COWORK_RUN_LABEL": "owned-run"}
    budget = budgets.CampaignBudget(tmp_path / "campaign.json", fingerprint="key-a", ceiling=1000, usage=100)
    events = []
    proc = SimpleNamespace(pid=424242, returncode=None)
    proc.poll = lambda: proc.returncode
    proc.wait = lambda **_kwargs: proc.returncode
    handlers = {}
    def register(sig, handler):
        old = handlers.get(sig, "old-handler")
        handlers[sig] = handler
        return old
    def stop(owned):
        assert owned is proc
        events.append("stop-group")
        owned.returncode = -15
    def cleanup(host, label):
        assert (host, label) == (args.docker_host, env["COWORK_RUN_LABEL"])
        events.append("cleanup-owned")
    monkeypatch.setattr(launcher.signal, "signal", register)
    def launch(_command, **kwargs):
        assert kwargs["drive_root"] == bench.parent
        assert kwargs["purpose"] == "cowork-official-runner"
        assert kwargs["scope"] == "session"
        assert kwargs.get("new_process_group", True) is True
        return proc
    monkeypatch.setattr(launcher, "spawn_supervised", launch)
    monkeypatch.setattr(launcher, "stop_process_group", stop)
    monkeypatch.setattr(launcher, "remove_run_containers", cleanup)
    monkeypatch.setattr(launcher, "key_usage", lambda _key: 100)
    monkeypatch.setattr(launcher.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1024**4))
    return args, bench, env, budget, events, handlers, proc


@pytest.mark.parametrize("trigger,expected", [
    ("meter-loss", "budget_meter_unavailable"), ("disk", "disk_reserve"),
    ("campaign", "campaign_budget_reserve"), ("run", "run_budget"),
])
def test_supervisor_stops_owned_work_on_real_boundaries(supervised, monkeypatch, trigger, expected):
    args, bench, env, budget, events, handlers, _proc = supervised
    if trigger == "meter-loss":
        def offline(_key):
            raise OSError("meter unavailable")
        monkeypatch.setattr(launcher, "key_usage", offline)
    elif trigger == "disk":
        args.min_free_gib = 200
        monkeypatch.setattr(launcher.shutil, "disk_usage", lambda _path: SimpleNamespace(free=199 * 1024**3))
    else:
        monkeypatch.setattr(launcher, "key_usage", lambda _key: 1000 if trigger == "campaign" else 250)
    result = launcher.supervise_run(args, ["fake-runner"], bench, env, "not-a-real-key", budget)
    assert result["stop_reason"] == expected
    assert events == ["stop-group", "cleanup-owned"]
    assert pathlib.Path(env["COWORK_STOP_FILE"]).read_text(encoding="utf-8").strip() == expected
    assert "active_run" not in budget.record
    assert budget.record["runs"][-1]["outcome"] == expected
    assert all(value == "old-handler" for value in handlers.values())
    if trigger == "meter-loss":
        assert result["meter_error"] == "OSError"


def test_supervisor_handles_sigterm_then_cleans_only_owned_resources(supervised, monkeypatch):
    args, bench, env, budget, events, handlers, proc = supervised
    def launch(*_args, **kwargs):
        assert kwargs["drive_root"] == bench.parent
        assert kwargs["scope"] == "session"
        assert kwargs["purpose"] == "cowork-official-runner"
        assert kwargs.get("new_process_group", True) is True
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return proc
    monkeypatch.setattr(launcher, "spawn_supervised", launch)
    result = launcher.supervise_run(args, ["fake-runner"], bench, env, "not-a-real-key", budget)
    assert result["stop_reason"] == f"signal_{signal.SIGTERM}"
    assert events == ["stop-group", "cleanup-owned"]
    assert all(value == "old-handler" for value in handlers.values())


def test_cleanup_failure_keeps_campaign_custody_unsettled(supervised, monkeypatch):
    args, bench, env, budget, _events, _handlers, _proc = supervised
    pathlib.Path(env["COWORK_STOP_FILE"]).write_text("operator_stop\n", encoding="utf-8")
    def fail_cleanup(*_args):
        raise RuntimeError("Docker unreachable")
    monkeypatch.setattr(launcher, "remove_run_containers", fail_cleanup)
    with pytest.raises(RuntimeError, match="Docker unreachable"):
        launcher.supervise_run(args, ["fake-runner"], bench, env, "not-a-real-key", budget)
    assert budget.record["active_run"] == str(bench.parent)
    with pytest.raises(ValueError, match="unsettled custody"):
        budgets.CampaignBudget(budget.path, fingerprint="key-a", ceiling=1000, usage=100)


def test_cleanup_selection_cannot_include_a_peer_run(monkeypatch):
    calls = []
    def docker(host, *argv, **_kwargs):
        assert host == "unix:///owned.sock"
        calls.append(argv)
        if argv[:2] == ("ps", "-aq"):
            assert argv[-1] == f"label={launcher.LABEL_KEY}=owned-run"
            return subprocess.CompletedProcess(argv, 0, "own-a\nown-b\n")
        if argv[:3] == ("network", "ls", "-q"):
            assert argv[-1] == f"label={launcher.LABEL_KEY}=owned-run"
            return subprocess.CompletedProcess(argv, 0, "own-net\n")
        return subprocess.CompletedProcess(argv, 0, "")
    monkeypatch.setattr(launcher, "_docker", docker)
    launcher.remove_run_containers("unix:///owned.sock", "owned-run")
    assert ("rm", "-fv", "own-a", "own-b") in calls
    assert ("network", "rm", "own-net") in calls


@pytest.mark.serial
@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="real POSIX process-group ownership check")
def test_stop_process_group_terminates_its_child_without_touching_peer(tmp_path):
    child_file = tmp_path / "child.pid"
    shell = shutil.which("sh")
    owned = subprocess.Popen([shell, "-c", 'trap \'kill "$child" 2>/dev/null; wait "$child"; exit 0\' TERM; '
                              'sleep 60 & child=$!; printf "%s" "$child" > "$1"; wait "$child"',
                              "cowork-test", str(child_file)], start_new_session=True)
    peer = subprocess.Popen([shell, "-c", "sleep 60"], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_file.exists()
        child = int(child_file.read_text(encoding="utf-8"))
        launcher.stop_process_group(owned)
        assert owned.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert peer.poll() is None
    finally:
        for process in (owned, peer):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_cowork_launcher_is_registered_and_obeys_shared_manifest_contract():
    path = pathlib.Path(launcher.__file__).resolve()
    assert path in launcher_paths()
    assert audit_launcher(path) == []


def test_existing_run_is_preserved_and_refusal_gets_its_own_manifest(dry_launcher, monkeypatch):
    out, argv = dry_launcher
    write_json(out / "run_manifest.json", {"previous": "finished run", "score": 0.75})
    write_json(out / "dumps" / "task" / "eval_res.json", {"pass": True})
    original_manifest = (out / "run_manifest.json").read_bytes()
    original_result = (out / "dumps" / "task" / "eval_res.json").read_bytes()
    monkeypatch.setattr(launcher, "timestamp_run_id", lambda _prefix: "fresh-refusal")
    monkeypatch.setattr(launcher, "image_exists", lambda *_a: pytest.fail("reused run reached Docker preflight"))
    assert launcher.main(argv) == 2
    assert (out / "run_manifest.json").read_bytes() == original_manifest
    assert (out / "dumps" / "task" / "eval_res.json").read_bytes() == original_result
    assert not (out / "bench").exists()
    refused = json.loads((out.parent / "fresh-refusal" / "run_manifest.json").read_text(encoding="utf-8"))
    assert refused["extra"]["outcome"] == "refused"
    assert refused["extra"]["exit_code"] == 2
    assert refused["extra"]["refusal"]["requested_root"] == str(out)
