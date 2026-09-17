"""Explicit runtime access survives the benchmark's settings projection."""
import json

import pytest

from devtools.benchmarks.cybergym.run_cybergym import (
    _prepare_applied_settings,
    parse_args,
)


@pytest.mark.parametrize("mode", ["pro", "cyber_pro"])
def test_runtime_mode_reaches_applied_settings(tmp_path, mode):
    args = parse_args(["--runtime-mode", mode, "--budget-usd", "200",
                       "--per-task-cost-usd", "5", "--workers", "32"])
    template = tmp_path / "template.json"
    template.write_text('{"OUROBOROS_RUNTIME_MODE": "advanced"}', encoding="utf-8")
    output = tmp_path / "run"
    output.mkdir()
    path, metadata = _prepare_applied_settings(template, output, args)
    applied = json.loads(path.read_text(encoding="utf-8"))
    assert applied["OUROBOROS_RUNTIME_MODE"] == mode
    assert metadata["runtime_mode"] == mode
    assert metadata["effective_overrides"]["OUROBOROS_RUNTIME_MODE"] == mode
    assert applied["OUROBOROS_MAX_WORKERS"] == 32
    assert applied["TOTAL_BUDGET"] == 200


def test_runtime_mode_default_remains_pro():
    assert parse_args([]).runtime_mode == "pro"


@pytest.mark.parametrize("mode", ["advanced", "light", "typo"])
def test_runtime_mode_refuses_unsupported_benchmark_modes(mode):
    with pytest.raises(SystemExit):
        parse_args(["--runtime-mode", mode])
