"""C10: a plain working copy past retention goes only with proof its task is over (the supervisor's
liveness probe, a terminal row, no lineage process); a checkout that survives its removal keeps its
row and pin; the registry is written only when something changed; ``only`` leaves other rows alone."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from ouroboros import body_candidate
from ouroboros import subagent_worktrees as sw
from ouroboros.task_results import write_task_result


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


def _init_repo(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo, data, root = tmp_path / "repo", tmp_path / "data", tmp_path / "wtroot"
    _init_repo(repo)
    data.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data))
    return repo, data, root


def _copy(world, task_id):
    repo, data, root = world
    return pathlib.Path(sw.provision_worktree(repo_dir=repo, task_id=task_id, worktree_root=root, data_dir=data).path)


def _age(data, *, created_at=1.0):
    path = data / "state" / "subagent_worktrees.json"
    rows = json.loads(path.read_text())
    for row in rows["worktrees"]:
        row["created_at"] = created_at
    path.write_text(json.dumps(rows))


def _prune(world, **kwargs):
    repo, data, root = world
    return sw.prune_orphans(worktree_root=root, data_dir=data, **kwargs)


def _registry_mtime(data):
    return (data / "state" / "subagent_worktrees.json").stat().st_mtime_ns


@pytest.mark.parametrize("retention_days", [7, 0])
def test_an_old_copy_stays_while_its_task_runs_is_pending_unknown_or_unreadable(world, monkeypatch, retention_days):
    """L3, the keeping half: age is a courtesy, never a proof."""
    repo, data, root = world
    cases = {
        "running": ("running", True), "pending": ("", True), "unknown": ("completed", None),
        "unreadable": (None, False), "process": ("completed", False), "done": ("completed", False),
    }
    copies = {name: _copy(world, name) for name in cases}
    for name, (status, _probe) in cases.items():
        if status:
            write_task_result(data, name, status)
    _age(data)
    answers = {name: probe for name, (_status, probe) in cases.items()}
    monkeypatch.setattr(body_candidate, "live_lineage_processes",
                        lambda _root, task_id: [4242] if task_id == "process" else [])

    assert _prune(world, retention_days=retention_days) == {"removed": 0, "kept": 6}, "without a probe nothing goes"
    assert all(path.is_dir() for path in copies.values())
    report = _prune(world, retention_days=retention_days, live=lambda task_id: answers[task_id])
    assert report == {"removed": 1, "kept": 5}, "only the proven-over copy goes"
    assert not copies["done"].exists() and all(path.is_dir() for name, path in copies.items() if name != "done")


@pytest.mark.parametrize("retention_days", [7, 0])
def test_an_old_copy_goes_once_its_task_is_terminal_and_proven_not_live(world, retention_days):
    """L3, the removing half, and the registry written once for a changed row set."""
    repo, data, root = world
    done, fresh = _copy(world, "done"), _copy(world, "fresh")
    write_task_result(data, "done", "completed")
    write_task_result(data, "fresh", "completed")
    _age(data)
    if retention_days:
        rows = json.loads((data / "state" / "subagent_worktrees.json").read_text())
        for row in rows["worktrees"]:
            if row["task_id"] == "fresh":
                row["created_at"] = 9e12  # inside the wait
        (data / "state" / "subagent_worktrees.json").write_text(json.dumps(rows))

    report = _prune(world, retention_days=retention_days, live=lambda _task: False)
    expected = 1 if retention_days else 2
    assert report["removed"] == expected
    assert not done.exists() and fresh.exists() != bool(retention_days) or fresh.exists() == bool(retention_days)
    assert _git(repo, "branch", "--list", "subagent/done*").stdout.strip() == ""
    assert "done" not in {row["task_id"] for row in sw.list_worktrees(data_dir=data)}


def test_a_missing_checkout_goes_whatever_the_probe_says(world):
    repo, data, root = world
    gone = _copy(world, "gone")
    shutil.rmtree(gone)
    assert _prune(world, retention_days=9999, live=lambda _task: True) == {"removed": 1, "kept": 0}


def test_only_restricts_the_pass_and_an_unchanged_row_set_leaves_the_registry_untouched(world):
    repo, data, root = world
    first, second = _copy(world, "first"), _copy(world, "second")
    for name in ("first", "second"):
        write_task_result(data, name, "completed")
    _age(data)
    before = _registry_mtime(data)
    assert _prune(world, retention_days=0, live=lambda _task: False, only=["nobody"]) == {"removed": 0, "kept": 2}
    assert _registry_mtime(data) == before, "nothing changed: the registry is not rewritten"
    assert _prune(world, retention_days=0, live=lambda _task: False, only=["second"]) == {"removed": 1, "kept": 1}
    assert first.is_dir() and not second.exists()
    assert _registry_mtime(data) != before


def test_a_checkout_surviving_its_removal_keeps_its_row_and_is_retried(world, monkeypatch):
    """L5: a forced removal that leaves the directory marks the row instead of forgetting it."""
    repo, data, root = world
    stubborn = _copy(world, "stubborn")
    write_task_result(data, "stubborn", "completed")
    _age(data)
    monkeypatch.setattr(sw, "_force_rmtree", lambda _path: None)  # the removal fails to delete anything
    monkeypatch.setattr(sw, "_git_quiet", lambda *_args: None)

    report = _prune(world, retention_days=0, live=lambda _task: False)
    assert report == {"removed": 0, "kept": 1}
    [row] = sw.list_worktrees(data_dir=data)
    assert row["task_id"] == "stubborn" and row["removal_failed"]
    assert stubborn.is_dir()
    assert _git(repo, "branch", "--list", "subagent/stubborn*").stdout.strip(), "the branch is kept with the row"

    monkeypatch.undo()
    assert _prune(world, retention_days=0, live=lambda _task: False) == {"removed": 1, "kept": 0}
    assert not stubborn.exists() and sw.list_worktrees(data_dir=data) == []


def test_remove_worktree_keeps_the_row_when_the_directory_survives(world, monkeypatch):
    repo, data, root = world
    path = _copy(world, "held")
    monkeypatch.setattr(sw, "_force_rmtree", lambda _path: None)
    monkeypatch.setattr(sw, "_git_quiet", lambda *_args: None)
    assert sw.remove_worktree(task_id="held", worktree_root=root, data_dir=data) is False
    [row] = sw.list_worktrees(data_dir=data)
    assert row["removal_failed"] and path.is_dir()
    monkeypatch.undo()
    assert sw.remove_worktree(task_id="held", worktree_root=root, data_dir=data) is True
    assert sw.list_worktrees(data_dir=data) == [] and not path.exists()


def test_a_snapshot_surviving_its_removal_keeps_its_row_and_pin_and_only_selects_snapshots(world, monkeypatch):
    repo, data, root = world
    one = sw.provision_execution_snapshot(target_root=repo, task_id="t1", snapshot_id="snapOne", worktree_root=root, data_dir=data)
    two = sw.provision_execution_snapshot(target_root=repo, task_id="t2", snapshot_id="snapTwo", worktree_root=root, data_dir=data)
    monkeypatch.setattr(sw, "_force_rmtree", lambda _path: None)
    report = sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data, only={"snapOne"})
    assert report == {"removed": [], "kept": ["snapOne"]}
    assert sw.find_execution_snapshot("snapOne", data_dir=data)["removal_failed"]
    assert pathlib.Path(one.path).is_dir() and pathlib.Path(two.path).is_dir()
    assert _git(repo, "rev-parse", "--verify", one.baseline_ref).stdout.strip(), "the pin stays with the row"
    monkeypatch.undo()
    assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data, only={"snapOne"}) == {"removed": ["snapOne"], "kept": []}
    assert not pathlib.Path(one.path).exists() and pathlib.Path(two.path).is_dir()
    assert sw.find_execution_snapshot("snapTwo", data_dir=data) is not None


@pytest.mark.parametrize("kind", ["plain", "body", "snapshot"])
def test_pruning_deletes_outside_the_ops_lock_and_preserves_concurrent_registration(world, monkeypatch, kind):
    repo, data, root = world
    if kind == "snapshot":
        handle = sw.provision_execution_snapshot(
            target_root=repo, task_id="done", snapshot_id="closed", worktree_root=root, data_dir=data)
        path = pathlib.Path(handle.path)
    else:
        path = _copy(world, "done")
        if kind == "body":
            rows = sw._load_registry(data)
            rows[0]["kind"] = "body_candidate"
            sw._save_registry(rows, data)
    write_task_result(data, "done", "completed")
    _age(data)
    real_remove, real_save, real_git = sw._force_rmtree, sw._save_registry, sw._git_quiet
    saves, deletions = [], []

    def remove(checkout):
        assert not sw._inproc_lock.locked(), "tree deletion must not hold the shared ops lock"
        deletions.append(checkout)
        _copy(world, "arrived")  # a real provision interleaves with this deletion
        real_remove(checkout)

    def save(rows, data_dir=None):
        saves.append(sw._inproc_lock.locked())
        real_save(rows, data_dir)

    def git(repo_dir, *args):
        if args[:2] == ("worktree", "remove") and path.exists():
            assert not sw._inproc_lock.locked(), "Git tree deletion must also be outside the shared lock"
        real_git(repo_dir, *args)

    monkeypatch.setattr(sw, "_force_rmtree", remove)
    monkeypatch.setattr(sw, "_save_registry", save)
    monkeypatch.setattr(sw, "_git_quiet", git)
    if kind == "snapshot":
        assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data)["removed"] == ["closed"]
    else:
        assert _prune(world, retention_days=0, live=lambda _: False)["removed"] == 1
    assert deletions == [path] and not path.exists()
    assert saves and all(saves), "registry mutations still require the shared lock"
    [row] = sw.list_worktrees(data_dir=data)
    assert row["task_id"] == "arrived" and pathlib.Path(row["path"]).is_dir()
