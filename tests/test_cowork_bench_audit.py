"""Offline evidence tests: the audit cannot turn missing traces into a clean claim."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from devtools.benchmarks.cowork_bench.audit_cowork_bench import audit_run, call_findings, main


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def run_fixture(tmp_path: Path, *, status="passed", events=None, tools=None) -> Path:
    task_dump = tmp_path / "bench" / "dumps" / "model" / "SingleUserTurn-задача"
    (task_dump / "ouroboros").mkdir(parents=True)
    (task_dump / "ouroboros_summary.json").write_text(
        json.dumps({"task": "задача", "bench_status": "success"}), encoding="utf-8"
    )
    write_jsonl(tmp_path / "result_index.jsonl", [{
        "instance_id": "задача", "status": status,
        "official_eval_status": "completed" if status in {"passed", "failed"} else "not_run",
        "output_paths": {"task_dump": str(task_dump.relative_to(tmp_path))},
    }])
    if events is not None:
        write_jsonl(task_dump / "ouroboros" / "events.jsonl", events)
    if tools is not None:
        write_jsonl(task_dump / "ouroboros" / "tools.jsonl", tools)
    return task_dump


def usage(**overrides) -> dict:
    return {"type": "llm_usage", "prompt_tokens": 100, "completion_tokens": 10,
            "cached_tokens": 80, "cost": 0.002, "cost_known": True,
            "model": "moonshotai/kimi-k3", "provider": "openrouter", **overrides}


def test_official_verdict_survives_flags_without_copying_gold(tmp_path):
    gold = "PRIVATE_GOLD_ЖЁЛТЫЙ"
    dump = run_fixture(tmp_path, events=[usage()], tools=[{
        "type": "tool_call", "tool": "run_command", "args": {
            "command": json.dumps({"argv": ["bash", "-lc", "cat /workspace/tasks/x/evaluation/answer.txt"],
                                   "note": gold}),
        }, "result_preview": gold,
    }, {"type": "tool_call", "tool": "mcp_pptx__create", "args": {}, "is_error": False}])
    # The auditor must not inspect the evaluator payload, even though it exists.
    (dump / "eval_res.json").write_text(json.dumps({"pass": True, "gold": gold}), encoding="utf-8")
    before = (dump / "eval_res.json").read_bytes()
    report = audit_run(tmp_path)
    row = report["tasks"][0]
    assert row["official_pass"] is True
    assert row["classification"] == "passed"
    assert report["manual_review_tasks"] == ["задача"]
    assert row["manual_review"] == [{"source": "tools.jsonl", "line": 1,
                                     "reason": "answer_source_or_evaluator_reference"}]
    assert gold not in json.dumps(report, ensure_ascii=False)
    assert (dump / "eval_res.json").read_bytes() == before
    assert row["activity"]["mcp_calls"] == 1
    assert row["cost"]["total_usd"] == 0.002
    assert row["billing_providers"] == ["openrouter"]
    assert row["observed_response_providers"] == []
    assert row["response_provider_coverage"] == "unavailable"


def test_unknown_price_and_observed_endpoint_remain_separate(tmp_path):
    run_fixture(tmp_path, events=[usage(), usage(cost=None, cost_known=False,
                usage={"response_provider": "Endpoint A"}), usage(cost=0, cost_estimated=True)], tools=[])
    report = audit_run(tmp_path)
    row = report["tasks"][0]
    assert report["cost"] == {"known_usd": 0.002, "total_usd": None, "complete": False}
    assert row["cost"]["unknown_usage_records"] == 1
    assert row["cost"]["estimated_usage_records"] == 1
    assert row["observed_response_providers"] == ["Endpoint A"]
    assert row["response_provider_coverage"] == "observed_subset"
    assert row["model_activity_observed"] is True
    assert row["mcp_activity_observed"] is False


def test_corrupt_or_missing_logs_cannot_be_a_clean_zero(tmp_path):
    dump = run_fixture(tmp_path, status="infra_failed", events=[usage()], tools=None)
    with (dump / "ouroboros" / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type":\n')
    row = audit_run(tmp_path)["tasks"][0]
    assert row["classification"] == "infrastructure"
    assert row["official_pass"] is None
    assert row["cost"]["known_usd"] == 0.002
    assert row["cost"]["total_usd"] is None
    assert {g["source"] for g in row["gaps"]} == {"tools.jsonl", "events.jsonl"}
    assert row["capability_omissions"]["absence_proves_none"] is False


@pytest.mark.parametrize("tool,args,expected", [
    ("run_command", {"argv": ["/usr/bin/psql", "-c", "select 1"]}, ["possible_direct_postgres_access"]),
    ("run_script", {"code": "import psycopg2; psycopg2.connect()"}, ["possible_direct_postgres_access"]),
    ("mcp_terminal__python_execute", {"code": "import asyncpg"}, ["possible_direct_postgres_access"]),
    ("mcp_clickhouse__query", {"sql": "SELECT 'psql'"}, []),
    ("run_command", {"command": "curl https://raw.githubusercontent.com/0717376/cowork_bench/main/a"},
     ["answer_source_or_evaluator_reference"]),
    ("run_command", json.dumps({"command": "cat '/workspace/groundtruth_workspace/test.txt'"}),
     ["answer_source_or_evaluator_reference"]),
    ("run_command", {"command": "echo ordinary"}, []),
])
def test_requested_argv_json_and_sql_are_diagnostic_only(tool, args, expected):
    assert call_findings({"tool": tool, "args": args,
                          "result_preview": "groundtruth_workspace/answer.txt psql"}) == expected


def test_timeouts_and_not_attempted_denominator_are_retained(tmp_path):
    run_fixture(tmp_path, status="agent_failed", events=[usage()], tools=[])
    with (tmp_path / "result_index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"instance_id": "not-started", "status": "not_attempted",
                                 "reason_code": "missing_result"}) + "\n")
    report = audit_run(tmp_path)
    assert report["task_count"] == 2
    assert report["classifications"] == {"genuine_failure": 1, "not_attempted": 1}
    assert report["tasks"][0]["model_activity_observed"] is True
    assert report["tasks"][1]["cost"]["total_usd"] is None


def test_omissions_are_referenced_without_republishing_their_contents(tmp_path):
    secret = "do-not-copy-gold-or-arbitrary-text"
    run_fixture(tmp_path, events=[usage(), {"type": "capability_report", "capability_omissions": [secret]}], tools=[])
    row = audit_run(tmp_path)["tasks"][0]
    assert row["capability_omissions"]["reported_count"] == 1
    assert row["capability_omissions"]["references"] == [{"source": "events.jsonl", "line": 2, "count": 1}]
    assert secret not in json.dumps(row)


def test_cli_is_offline_and_never_overwrites_existing_report(tmp_path):
    run_fixture(tmp_path, events=[], tools=[])
    target = tmp_path / "audit.json"
    assert main(["--run-dir", str(tmp_path), "--output", str(target)]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["tasks"][0]["cost"]["total_usd"] is None
    with pytest.raises(FileExistsError):
        main(["--run-dir", str(tmp_path), "--output", str(target)])
