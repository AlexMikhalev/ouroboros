"""Review checkouts nothing could retire (owner 3A).

C8: an isolated checkout (``state/review_checkouts/<token>/repo``) that its wave kept for open custody,
or that a crash left, is retired on the 600 s custody pass once no review can still read it: a marked
one (``checkout.json``) when its task is terminal and not live, no active review custody names its
round, no live review operation runs for the task and no retained seat checks from it; an unmarked
older one under the same conditions only past the GC retention wait and only when a recorded round
names its task.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from types import SimpleNamespace

import pytest

from ouroboros import review_checking_prune as rp
from ouroboros.startup_migrations import prepare_startup_state
from ouroboros.task_results import write_task_result
from ouroboros.tools.review_subject import CHECKOUT_MARKER, ReviewSubjectSpec, checkout_token, isolated_checkout

TASK = "t-reviewer"
RETRY_KEY = "review:round-1"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(path, "config", key, value)
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    (path / "a.txt").write_text("two\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "proposal")
    return path


def _alive_controller() -> dict:
    from ouroboros.platform_layer import process_start_time

    return {"pid": os.getppid(), "birth": process_start_time(os.getppid()), "session": "another-process"}


@pytest.fixture
def subject(tmp_path):
    """A prepared data root, a foreign repository with a proposal, and a reviewer task context."""
    data = tmp_path / "data"
    prepare_startup_state(data)
    repo = _repo(tmp_path / "subject")
    system = _repo(tmp_path / "system")
    ctx = SimpleNamespace(repo_dir=str(system), drive_root=str(data), task_id=TASK, task_metadata=None)
    spec = ReviewSubjectSpec(root_kind="active_workspace", root=str(repo), kind="base..head", base="HEAD~1",
                             head="HEAD", governance_root=str(system), surface="change")
    return SimpleNamespace(data=data.resolve(), repo=repo, ctx=ctx, spec=spec,
                           name=checkout_token(RETRY_KEY, task_id=TASK))


def _retain(subject, *, task_status="completed"):
    """The wave of TASK's round exits with custody open: its checkout stays, marked."""
    with isolated_checkout(subject.ctx, subject.spec, retain=lambda: {"seats": ["late seat"]},
                           token=lambda _identity: subject.name) as frozen:
        checkout = pathlib.Path(frozen.checkout)
    write_task_result(subject.data, TASK, task_status, result="done")
    assert checkout.is_dir() and len(_git(subject.repo, "worktree", "list").splitlines()) == 2
    return checkout.parent


def _attempt(subject, status):
    """One review-state attempt row of TASK's round (``reviewing`` keeps custody active)."""
    path = subject.data / "state" / "advisory_review.json"
    path.write_text(json.dumps({"attempts": [{"task_id": TASK, "review_retry_key": RETRY_KEY, "status": status,
                                              "tool_name": "review_change", "repo_key": "subject",
                                              "ts": "2026-10-17T00:00:00+00:00"}]}), encoding="utf-8")


def _prune(subject, live=()):
    return rp.prune_review_checkouts(subject.data, set(live))


def test_the_marker_names_the_task_and_a_reused_checkout_keeps_it(subject):
    entry = _retain(subject)
    marker = json.loads((entry / CHECKOUT_MARKER).read_text(encoding="utf-8"))
    assert marker["task_id"] == TASK and marker["schema_version"] == 1 and marker["created_at"]
    with isolated_checkout(subject.ctx, subject.spec, retain=lambda: {"seats": ["late seat"]},
                           token=lambda _identity: subject.name) as again:  # a rejoin reads it in place
        assert pathlib.Path(again.checkout).parent == entry
    assert json.loads((entry / CHECKOUT_MARKER).read_text(encoding="utf-8")) == marker


def test_a_marked_checkout_no_review_can_read_is_removed_and_its_worktree_pruned(subject):
    entry = _retain(subject)
    report = _prune(subject)
    assert not entry.exists() and report["checkouts_removed"] == 1, report
    assert len(_git(subject.repo, "worktree", "list").splitlines()) == 1, "the subject's worktree entry is pruned"


@pytest.mark.parametrize("hold", ["task_live", "task_running", "custody_active", "seat_checking", "operation_live",
                                  "no_owner"])
def test_a_checkout_a_review_may_still_read_stays(subject, hold):
    entry = _retain(subject, task_status="running" if hold == "task_running" else "completed")
    live = {TASK} if hold == "task_live" else set()
    if hold == "custody_active":
        _attempt(subject, "reviewing")
    elif hold == "seat_checking":
        seat = subject.data / "state" / "review_checkouts" / "seats" / "seat-1"
        (seat / "area").mkdir(parents=True)
        (seat / "checking.json").write_text(json.dumps({"task_id": TASK, "operation_id": "op-1", "source": {
            "kind": "git_tree", "root": str(entry / "repo")}}), encoding="utf-8")
    elif hold == "operation_live":
        write_task_result(subject.data, TASK, "completed", review_operations={"op-live": {
            "state": "dispatched", "surface": "multi_model_review", "retry_key": RETRY_KEY,
            "controller": _alive_controller()}})
    elif hold == "no_owner":
        marker = entry / CHECKOUT_MARKER
        marker.write_text(json.dumps({**json.loads(marker.read_text(encoding="utf-8")), "task_id": ""}),
                          encoding="utf-8")
    report = _prune(subject, live)
    assert entry.is_dir() and report["checkouts_removed"] == 0 and not report["errors"], report


def test_an_unmarked_checkout_goes_only_past_the_wait_when_a_recorded_round_names_its_task(subject):
    entry = _retain(subject)
    (entry / CHECKOUT_MARKER).unlink()  # made by a body before the marker
    assert _prune(subject)["checkouts_removed"] == 0 and entry.is_dir(), "no recorded round: owner unknown"
    _attempt(subject, "succeeded")
    assert _prune(subject)["checkouts_removed"] == 0 and entry.is_dir(), "younger than the retention wait"
    later = rp.prune_review_checkouts(subject.data, set(), now=time.time() + 30 * 86400)  # the wait has passed
    assert later["checkouts_removed"] == 1 and not entry.exists(), later


def test_an_unreadable_review_state_removes_no_checkout(subject):
    entry = _retain(subject)
    (subject.data / "state" / "advisory_review.json").write_text("{torn", encoding="utf-8")
    report = _prune(subject)
    assert entry.is_dir() and report["checkouts_removed"] == 0 and report["errors"], report
