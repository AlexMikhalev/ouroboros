"""Live-side campaign execution lock coverage."""

from __future__ import annotations

import os
import threading

import pytest

from devtools.benchmarks.cybergym import cybergym_result_index
from devtools.benchmarks.cybergym.cybergym_adapter import (
    ClaimRefused,
    campaign_execution_lock,
    run_campaign,
)


def test_campaign_lock_releases_without_creating_run_root(tmp_path):
    root = tmp_path / "uncreated"
    with campaign_execution_lock(root, blocking=False) as held:
        assert held is True
        with campaign_execution_lock(root, blocking=False) as second:
            assert second is False
    with campaign_execution_lock(root, blocking=False) as held:
        assert held is True
    assert not root.exists()


@pytest.mark.parametrize("blocking", [True, False])
def test_campaign_lock_closes_handle_on_error(monkeypatch, tmp_path, blocking):
    descriptors = []

    def fail(fd):
        descriptors.append(fd)
        raise OSError("lock unavailable")

    name = "file_lock_exclusive" if blocking else "file_lock_exclusive_nb"
    monkeypatch.setattr(cybergym_result_index, name, fail)
    with pytest.raises(OSError, match="lock unavailable"):
        cybergym_result_index.acquire_campaign_execution_lock(
            tmp_path / "uncreated", blocking=blocking,
        )
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not (tmp_path / "uncreated").exists()


def test_run_campaign_holds_campaign_lock_through_dispatch(tmp_path):
    root = tmp_path / "live-lock"
    started = threading.Event()
    release = threading.Event()
    result: list[list[dict]] = []

    def callback(_task, _task_dir):
        started.set()
        assert release.wait(timeout=5)
        return {
            "status": "infra_failed",
            "infra_reason": "test_terminal",
            "cost_usd": 0.1,
            "cost_estimated": False,
            "cost_final": True,
        }

    worker = threading.Thread(
        target=lambda: result.append(run_campaign(
            ["arvo:1"],
            run_root=root,
            executor=callback,
            estimated_cost_usd=1,
            budget_cap_usd=2,
        )),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=5)
    try:
        with campaign_execution_lock(root, blocking=False) as lock_held:
            assert lock_held is False
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert result[0][0]["status"] == "infra_failed"


def test_second_live_campaign_is_refused_before_stale_history_admission(tmp_path):
    root = tmp_path / "double-live"
    started = threading.Event()
    release = threading.Event()

    def callback(_task, _task_dir):
        started.set()
        assert release.wait(timeout=5)
        return {
            "status": "infra_failed",
            "infra_reason": "test_terminal",
            "cost_usd": 0.1,
            "cost_estimated": False,
            "cost_final": True,
        }

    worker = threading.Thread(
        target=lambda: run_campaign(
            ["arvo:1"], run_root=root, executor=callback,
            estimated_cost_usd=1, budget_cap_usd=2,
        ),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=5)
    try:
        with pytest.raises(ClaimRefused, match="execution lock"):
            run_campaign(
                ["arvo:1"], run_root=root, executor=callback,
                estimated_cost_usd=1, budget_cap_usd=2,
            )
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_launcher_lock_loser_writes_no_admission_manifest(tmp_path):
    from devtools.benchmarks.cybergym.run_cybergym import main

    root = tmp_path / "locked-run"
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    with campaign_execution_lock(root, blocking=False) as lock_held:
        assert lock_held is True
        assert main([
            "--dry-run",
            "--out-dir", str(root),
            "--settings-path", str(settings),
            "--task-id", "arvo:1",
        ]) == 2

    assert not root.exists()
