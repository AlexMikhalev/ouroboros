#!/usr/bin/env python3
"""Offline Cowork evidence audit, never a scorer or proof of no contamination.

Read only the adapter ledger and its copied logs, never task answers or evaluator
contents. Findings identify log coordinates for manual inspection without copying
commands, results, or gold values. Cost is the llm_usage compatibility projection,
not provider billing authority; missing prices/logs remain explicitly unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import shlex
from typing import Any, Iterator


# Diagnostic source/artifact references, not a semantic cheating classifier.
_REFERENCE = re.compile(
    r"(?:groundtruth_workspace|(?:^|[/\\\s])evaluation[/\\]|eval_res\.json|"
    r"(?:github\.com|raw\.githubusercontent\.com)/0717376/cowork_bench|toolathlon)",
    re.IGNORECASE,
)
_SQL_CLIENT = re.compile(r"\b(?:psql|pgcli|psycopg2?|asyncpg)\b", re.IGNORECASE)


def _records(path: Path, gaps: list[dict[str, Any]]) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line, text in enumerate(handle, 1):
                if not text.strip():
                    continue
                try:
                    row = json.loads(text)
                    if not isinstance(row, dict):
                        raise ValueError("not an object")
                except ValueError:
                    gaps.append({"source": path.name, "line": line, "reason": "invalid_record"})
                    continue
                yield line, row
    except (OSError, UnicodeError):
        gaps.append({"source": path.name, "reason": "unreadable_or_missing"})


def _object(path: Path, gaps: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError, UnicodeError):
        pass
    gaps.append({"source": path.name, "reason": "unreadable_or_invalid"})
    return {}


def argument_strings(value: Any) -> Iterator[str]:
    """Unwrap dict/list arguments, JSON-encoded args, and shell argv without execution."""
    if isinstance(value, dict):
        for child in value.values():
            yield from argument_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from argument_strings(child)
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, list)):
            yield from argument_strings(decoded)
            return
        yield value
        try:
            yield from shlex.split(value)
        except ValueError:
            pass  # The original malformed shell text still gets inspected.


def call_findings(row: dict[str, Any]) -> list[str]:
    """Return review cues from requested arguments only; echoed tool output is ignored."""
    texts = list(argument_strings(row.get("args", row.get("arguments", {}))))
    findings = []
    if any(_REFERENCE.search(text) for text in texts):
        findings.append("answer_source_or_evaluator_reference")
    tool = str(row.get("tool") or "")
    # SQL through the benchmark's database MCP is expected. A native shell or the
    # benchmark terminal/python tool can instead bypass those tool interfaces.
    shell_or_code = tool in {"run_command", "run_script", "start_service", "python_execute"}
    shell_or_code |= tool.startswith("mcp_terminal__")
    if shell_or_code and any(_SQL_CLIENT.search(text) for text in texts):
        findings.append("possible_direct_postgres_access")
    return findings


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _omission_count(value: Any) -> int:
    if isinstance(value, (list, dict)):
        return len(value)
    return int(bool(value))


def audit_task(task_dump: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    """Report copied runtime evidence separately from the existing official verdict."""
    gaps: list[dict[str, Any]] = []
    summary = _object(task_dump / "ouroboros_summary.json", gaps)
    events = task_dump / "ouroboros" / "events.jsonl"
    tools = task_dump / "ouroboros" / "tools.jsonl"
    activity = {"usage_records": 0, "nonempty_usage_records": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                "tool_calls": 0, "mcp_calls": 0, "mcp_error_calls": 0}
    known_cost = 0.0
    unknown_cost = estimated_cost = 0
    billing_providers: set[str] = set()
    response_providers: set[str] = set()
    models: set[str] = set()
    findings: list[dict[str, Any]] = []
    omission_refs: list[dict[str, Any]] = []
    for source in (events, tools):
        for line, record in _records(source, gaps):
            if record.get("capability_omissions"):
                omission_refs.append({"source": source.name, "line": line,
                                      "count": _omission_count(record["capability_omissions"])})
            if record.get("response_provider"):
                response_providers.add(str(record["response_provider"]))
            if source == events and record.get("type") == "llm_usage":
                activity["usage_records"] += 1
                usage = record.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                token_values = {}
                for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
                    value = _number(record.get(key, usage.get(key)))
                    if value is None:
                        gaps.append({"source": source.name, "line": line, "reason": f"unknown_{key}"})
                    token_values[key] = int(value or 0)
                    activity[key] += token_values[key]
                activity["nonempty_usage_records"] += int(
                    token_values["prompt_tokens"] + token_values["completion_tokens"] > 0
                )
                cost = _number(record.get("cost", usage.get("cost")))
                if cost is None or record.get("cost_known") is False:
                    unknown_cost += 1
                else:
                    known_cost += cost
                    estimated_cost += int(bool(record.get("cost_estimated")))
                if record.get("provider"):
                    billing_providers.add(str(record["provider"]))
                if record.get("model"):
                    models.add(str(record["model"]))
                if usage.get("response_provider"):
                    response_providers.add(str(usage["response_provider"]))
            elif source == tools and record.get("type") == "tool_call":
                activity["tool_calls"] += 1
                if str(record.get("tool") or "").startswith("mcp_"):
                    activity["mcp_calls"] += 1
                    activity["mcp_error_calls"] += int(record.get("is_error") is True)
                for reason in call_findings(record):
                    findings.append({"source": source.name, "line": line, "reason": reason})
    if summary.get("capability_omissions"):
        omission_refs.append({"source": "ouroboros_summary.json",
                              "count": _omission_count(summary["capability_omissions"])})
    status = str(ledger.get("status") or "unknown")
    classification = {
        "infra_failed": "infrastructure", "agent_failed": "genuine_failure",
        "failed": "genuine_failure", "passed": "passed", "not_attempted": "not_attempted",
    }.get(status, "unknown")
    # Preserve the typed ledger, including genuine timeouts with model activity.
    # Lack of telemetry is an audit gap, never a reason to rewrite its verdict.
    log_incomplete = any(g["source"] == "events.jsonl" for g in gaps)
    cost_complete = bool(activity["usage_records"]) and not unknown_cost and not log_incomplete
    official_status = str(ledger.get("official_eval_status") or "not_run")
    return {
        "instance_id": str(ledger.get("instance_id") or task_dump.name.removeprefix("SingleUserTurn-")),
        "ledger_status": status,
        "classification": classification,
        "reason_code": str(ledger.get("reason_code") or summary.get("reason_code") or ""),
        "official_eval_status": official_status,
        "official_pass": status == "passed" if official_status == "completed" and status in {"passed", "failed"} else None,
        "activity": activity,
        "model_activity_observed": bool(activity["nonempty_usage_records"]),
        "mcp_activity_observed": bool(activity["mcp_calls"]),
        "cost": {"known_usd": round(known_cost, 9),
                 "total_usd": round(known_cost, 9) if cost_complete else None,
                 "unknown_usage_records": unknown_cost, "estimated_usage_records": estimated_cost,
                 "complete": cost_complete, "source": "llm_usage_projection"},
        "models": sorted(models), "billing_providers": sorted(billing_providers),
        "observed_response_providers": sorted(response_providers),
        "response_provider_coverage": "observed_subset" if response_providers else "unavailable",
        "capability_omissions": {"reported_count": sum(r["count"] for r in omission_refs),
                                 "references": omission_refs, "absence_proves_none": False},
        "manual_review": findings, "gaps": gaps,
    }


def audit_run(run_root: Path | str) -> dict[str, Any]:
    """Audit each ledger row, retaining not-attempted tasks in the denominator."""
    root = Path(run_root).expanduser().resolve()
    gaps: list[dict[str, Any]] = []
    rows = []
    for _, ledger in _records(root / "result_index.jsonl", gaps):
        raw_path = (ledger.get("output_paths") or {}).get("task_dump")
        if not raw_path:
            gaps.append({"source": "result_index.jsonl", "reason": "missing_task_dump"})
            raw_path = root / "missing" / str(ledger.get("instance_id") or "unknown")
        task_dump = Path(raw_path)
        if not task_dump.is_absolute():
            task_dump = root / task_dump
        rows.append(audit_task(task_dump, ledger))
    complete = bool(rows) and not gaps and all(row["cost"]["complete"] for row in rows)
    known = round(sum(row["cost"]["known_usd"] for row in rows), 9)
    return {
        "schema": "ouroboros.cowork.audit.v1",
        "scoring_authority": "official evaluator; this audit never changes scores",
        "limitations": ["Diagnostic argument references require manual review, not automatic disqualification.",
                        "No flags do not prove no contamination; copied logs may omit full arguments or responses.",
                        "llm_usage cost is a compatibility projection, not authoritative provider billing.",
                        "Billing provider names do not identify OpenRouter upstream endpoints."],
        "task_count": len(rows), "classifications": dict(Counter(row["classification"] for row in rows)),
        "manual_review_tasks": [row["instance_id"] for row in rows if row["manual_review"]],
        "cost": {"known_usd": known, "total_usd": known if complete else None, "complete": complete},
        "gaps": gaps, "tasks": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", help="Optional JSON output; stdout otherwise. Existing files are not replaced.")
    args = parser.parse_args(argv)
    report = audit_run(args.run_dir)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
