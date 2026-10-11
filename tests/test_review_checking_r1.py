"""R1 consumer regressions: bucket-aware inputs and bounded evidence capture."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

import pytest

from ouroboros import review_checking as rc
from ouroboros import subagent_worktrees as sw
from ouroboros.skill_loader import compute_content_hash
from tests.test_review_checking import _author_state, _git, author  # noqa: F401 — shared real-Git fixture
from tests.test_review_thread_checking import _run, fake_route, thread_gateway  # noqa: F401 — offline transport


@pytest.mark.parametrize("bucket", ["native", "external"])
def test_payload_snapshot_preserves_loader_bucket_semantics(tmp_path, bucket):
    folder = tmp_path / bucket / "sample"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: sample\ntype: instruction\n---\nFixture\n", encoding="utf8")
    (folder / ".seed-origin").write_text("seed marker", encoding="utf8")
    before = compute_content_hash(folder)
    bound = rc.bound_directory_source(folder, data_root=tmp_path / "data", expected_hash=before,
                                      content_hash_of=compute_content_hash)
    assert "unavailable" not in bound
    view = pathlib.Path(bound["view"])
    assert view.parent.name == bucket and compute_content_hash(view) == before
    (folder / ".seed-origin").write_text("changed marker", encoding="utf8")
    assert (compute_content_hash(folder) == before) == (bucket == "native")
    assert compute_content_hash(view) == before
    (folder / "SKILL.md").write_text("changed payload", encoding="utf8")
    assert "unavailable" in rc.bound_directory_source(
        folder, data_root=tmp_path / "data", expected_hash=before, content_hash_of=compute_content_hash)


def test_named_xml_outputs_are_kept_independently_of_patch_filters(tmp_path):
    area = tmp_path / "checking"
    declared = area / rc.EVIDENCE_DIRNAME
    declared.mkdir(parents=True)
    raw = b"<coverage>\x00byte-exact</coverage>"
    (declared / "coverage.xml").write_bytes(raw)
    budget, gaps = [len(raw)], []
    kept = rc.retain_named_evidence(area, tmp_path / "files", budget, gaps)
    assert not gaps and budget == [0]
    assert kept[0]["path"] == ".review-evidence/coverage.xml"
    assert (tmp_path / "files" / kept[0]["path"]).read_bytes() == raw
    gaps = []
    assert not rc.retain_named_evidence(area, tmp_path / "too-small", [1], gaps)
    assert gaps[0]["reason"] == "evidence_budget_exceeded"
    assert (declared / "coverage.xml").read_bytes() == raw


@pytest.mark.skipif(os.name == "nt", reason="symlink privileges vary on Windows")
def test_named_symlink_is_a_gap_not_silent_retained_evidence(tmp_path):
    area = tmp_path / "checking"
    area.mkdir()
    target = tmp_path / "other"
    target.mkdir()
    (target / "only-copy.xml").write_bytes(b"data")
    (area / rc.EVIDENCE_DIRNAME).symlink_to(target, target_is_directory=True)
    gaps = []
    assert rc.retain_named_evidence(area, tmp_path / "files", [100], gaps) == []
    assert gaps == [{"path": rc.EVIDENCE_DIRNAME, "reason": "evidence_symlink"}]
    assert not (tmp_path / "files").exists()


def test_large_binary_delta_streams_to_file_and_keeps_copy_when_over_budget(author, tmp_path, monkeypatch):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "large", source))
    (area / "binary.dat").write_bytes(os.urandom(256 * 1024))
    real_run, streamed = subprocess.run, []

    def observe(argv, *args, **kwargs):
        if "diff" in argv and "--binary" in argv:
            assert kwargs.get("stdout") is not None and kwargs["stdout"] != subprocess.PIPE
            streamed.append(True)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    monkeypatch.setattr(rc, "EVIDENCE_RETAIN_BYTES", 16 * 1024)
    result = rc.capture_seat_evidence(data, "task", "large", release=True)
    assert streamed and result["status"] == "partial"
    assert result["patch_bytes"] > rc.EVIDENCE_RETAIN_BYTES
    assert result["patch_retained"] is False and not pathlib.Path(result["evidence_dir"], "delta.patch").exists()
    assert result["gaps"][0]["path"] == "delta.patch" and area.is_dir()
    # The oversized stream was not a partial delivered patch; its original bytes
    # stay in the copy and a later capture with room retains the complete delta.
    monkeypatch.setattr(rc, "EVIDENCE_RETAIN_BYTES", 1024 * 1024)
    retained = rc.capture_seat_evidence(data, "task", "large", release=True)
    assert retained["status"] == "retained" and retained["patch_retained"]
    assert pathlib.Path(retained["evidence_dir"], "delta.patch").stat().st_size == retained["patch_bytes"]
    assert not area.exists()


@pytest.mark.skipif(os.name == "nt", reason="fsmonitor hook test uses a POSIX shell")
def test_capture_disables_reviewer_configured_host_hooks(author, tmp_path):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "hooks", source))
    marker, hook = tmp_path / "unexpected-host-execution", tmp_path / "fsmonitor"
    hook.write_text(f'#!/bin/sh\nprintf executed > "{marker}"\n', encoding="utf8")
    hook.chmod(0o755)
    _git(area, "config", "core.fsmonitor", str(hook))
    _git(area, "config", "core.hooksPath", str(tmp_path))
    (area / "test.txt").write_text("reviewer change", encoding="utf8")
    evidence = rc.capture_seat_evidence(data, "task", "hooks", release=True)
    assert evidence["status"] == "retained" and "A\ttest.txt" in evidence["changed"]
    assert not marker.exists()


def test_real_linked_worktree_subject_reaches_independent_checking_copy(author, tmp_path):  # noqa: F811
    linked = tmp_path / "linked"
    _git(author, "worktree", "add", "--detach", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    (linked / "f.txt").write_bytes(b"linked staged\n")
    _git(linked, "add", "f.txt")
    (linked / "f.txt").write_bytes(b"linked unstaged\n")
    source = rc.freeze_checking_source(linked, data_root=tmp_path / "data", subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(tmp_path / "data", "task", "linked", source))
    assert (area / ".git").is_dir()
    assert (area / "f.txt").read_bytes() == b"linked staged\n"
    assert _git(area, "write-tree") == source["tree"]
    assert (linked / "f.txt").read_bytes() == b"linked unstaged\n"


def test_release_receipt_retries_residual_cleanup_without_recapturing(author, tmp_path, monkeypatch):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "residual", source))
    (area / "result.txt").write_text("reviewer output\n", encoding="utf8")
    real_remove = sw._force_rmtree
    monkeypatch.setattr(sw, "_force_rmtree", lambda _path: None)

    receipt = rc.capture_seat_evidence(data, "task", "residual", release=True)
    durable = json.loads(pathlib.Path(receipt["evidence_dir"], "summary.json").read_text(encoding="utf8"))
    assert receipt == durable
    assert receipt["status"] == "retained" and not receipt["refresh_safe"]
    assert receipt["retention_reason"] == "cleanup_failed" and area.is_dir()
    evidence_dir = receipt["evidence_dir"]
    monkeypatch.setattr(rc, "_git_delta", lambda *_args, **_kwargs: pytest.fail("recaptured evidence"))
    monkeypatch.setattr(sw, "_force_rmtree", real_remove)
    # A later release retries the residual cleanup while keeping the immutable evidence directory.
    retained = rc.capture_seat_evidence(data, "task", "residual", release=True)
    assert retained["refresh_safe"] and retained["evidence_dir"] == evidence_dir and not area.exists()


def test_interrupted_materialization_refuses_unproven_cleanup(author, tmp_path, monkeypatch):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = rc.seat_dir(data, "task", "interrupted") / "area"
    area.mkdir(parents=True)
    (area / "unfinished.txt").write_text("keep me\n", encoding="utf8")
    monkeypatch.setattr(sw, "_force_rmtree", lambda _path: None)

    with pytest.raises(rc.CheckingAreaUnavailable, match="checking cleanup"):
        rc.prepare_seat_area(data, "task", "interrupted", source)
    assert (area / "unfinished.txt").read_text(encoding="utf8") == "keep me\n"


def test_sticky_refresh_keeps_previous_copy_when_cleanup_is_unproven(author, tmp_path, monkeypatch):  # noqa: F811
    data = tmp_path / "data"
    first = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.thread_seat_area(data, "task", "plan_review:t1", first, operation_id="turn-1"))
    rc.capture_seat_evidence(data, "task", "turn-1", release=True)
    (author / "f.txt").write_text("round two\n", encoding="utf8")
    _git(author, "add", "f.txt")
    second = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    real_remove = sw._force_rmtree

    def leave_previous(path):
        return None if pathlib.Path(path) == area else real_remove(path)

    monkeypatch.setattr(sw, "_force_rmtree", leave_previous)
    with pytest.raises(rc.CheckingAreaUnavailable, match="checking cleanup"):
        rc.thread_seat_area(data, "task", "plan_review:t1", second, operation_id="turn-2")
    assert area.is_dir() and (area / "f.txt").read_text(encoding="utf8") == "A staged\n"


def _readonly_git_object(area):
    probe = area / "readonly-probe.txt"
    probe.write_bytes(b"reviewer-created object, absent from the author's object store\n")
    oid = _git(area, "hash-object", "-w", probe.name)
    target = area / ".git" / "objects" / oid[:2] / oid[2:]
    target.chmod(0o444)
    return target


def _windows_readonly_unlink(monkeypatch, target):
    """Inject only the OS denial; real rmtree/onerror must chmod and retry."""
    events = []
    real_unlink, real_chmod = os.unlink, os.chmod

    def unlink(path, *, dir_fd=None):
        # POSIX rmtree uses a basename + dir_fd; Windows uses the full path.
        watched = pathlib.Path(os.fsdecode(path)).name == target.name
        if watched:
            if not os.stat(path, dir_fd=dir_fd).st_mode & stat.S_IWRITE:
                events.append("denied")
                raise PermissionError(13, "simulated Windows read-only Git object", os.fspath(path))
            events.append("retried")
        return real_unlink(path, dir_fd=dir_fd)

    def chmod(path, mode, *args, **kwargs):
        if pathlib.Path(path) == target:
            events.append("chmod")
            assert mode & stat.S_IWRITE
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "chmod", chmod)
    return events


@pytest.mark.parametrize("simulate_windows", [False, True])
def test_real_git_copy_with_readonly_objects_releases_after_verified_cleanup(
        author, tmp_path, monkeypatch, simulate_windows):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    before = _author_state(author)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "readonly-objects", source))
    target = _readonly_git_object(area)
    events = _windows_readonly_unlink(monkeypatch, target) if simulate_windows else []
    receipt = rc.terminal_seat_evidence(data, "task", "readonly-objects", {"summary": {"state": "succeeded"}})
    durable = json.loads(pathlib.Path(receipt["evidence_dir"], "summary.json").read_text(encoding="utf8"))
    assert receipt == durable and receipt["status"] == "retained" and receipt["refresh_safe"]
    assert not area.exists()
    assert _author_state(author) == before
    assert b"readonly-probe.txt" in pathlib.Path(receipt["evidence_dir"], "delta.patch").read_bytes()
    if simulate_windows:
        assert events == ["denied", "chmod", "retried"]


def test_sticky_consumer_refresh_recovers_readonly_objects_in_same_thread(
        author, tmp_path, monkeypatch, thread_gateway):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    before = _author_state(author)
    targets = []
    thread_gateway.before_turn = lambda area: targets.append(_readonly_git_object(area))
    first = _run(author, data, source)
    saved = pathlib.Path(first["checking_evidence"]["evidence_dir"])
    saved_bytes = {str(p.relative_to(saved)): p.read_bytes() for p in saved.rglob("*") if p.is_file()}
    area = rc.seat_dir(data, "task-1", "thread:plan_review:t1") / "area"
    events = _windows_readonly_unlink(monkeypatch, targets[0])
    thread_gateway.before_turn = None
    second = _run(author, data, source, operation="op-2", thread_id=first["thread_id"])
    assert events == ["denied", "chmod", "retried"]
    assert first["thread_id"] == second["thread_id"] == "thread-1"
    assert len(thread_gateway.thread_requests) == 1 and len(thread_gateway.turn_requests) == 2
    assert second["checking_evidence"]["refresh_safe"] and area.is_dir()
    assert _git(area, "write-tree") == source["tree"] and not (area / "readonly-probe.txt").exists()
    assert _author_state(author) == before
    assert {str(p.relative_to(saved)): p.read_bytes() for p in saved.rglob("*") if p.is_file()} == saved_bytes


def test_interrupted_materialization_retries_after_readonly_cleanup(author, tmp_path, monkeypatch):  # noqa: F811
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    before = _author_state(author)
    home = rc.seat_dir(data, "task", "interrupted-readonly")
    materialize = rc._materialize_git
    targets = []

    def interrupted(area, frozen):
        materialize(area, frozen)
        targets.append(_readonly_git_object(area))
        raise OSError("interrupted after materialization, before marker")

    monkeypatch.setattr(rc, "_materialize_git", interrupted)
    with pytest.raises(rc.CheckingAreaUnavailable, match="interrupted after materialization"):
        rc.prepare_seat_area(data, "task", "interrupted-readonly", source)
    assert not (home / "checking.json").exists()
    monkeypatch.setattr(rc, "_materialize_git", materialize)
    events = _windows_readonly_unlink(monkeypatch, targets[0])
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "interrupted-readonly", source))
    assert events == ["denied", "chmod", "retried"]
    assert area == home / "area" and (home / "checking.json").is_file()
    assert _git(area, "write-tree") == source["tree"] and not (area / "readonly-probe.txt").exists()
    assert _author_state(author) == before


@pytest.mark.parametrize("failure", ["pending", "released_outcome", "residual_outcome"])
def test_capture_retry_write_failure_returns_last_durable_receipt(tmp_path, monkeypatch, failure):
    data, subject = tmp_path / "data", tmp_path / "subject"
    subject.mkdir()
    (subject / "original.txt").write_text("original", encoding="utf8")
    source = rc.freeze_checking_source(subject, data_root=data)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "write-failure", source))
    (area / "result.txt").write_bytes(b"retained result\n")
    with monkeypatch.context() as patch:
        patch.setattr(sw, "_force_rmtree", lambda _path: None)
        prior = rc.capture_seat_evidence(data, "task", "write-failure", release=True)
    summary_path = pathlib.Path(prior["evidence_dir"], "summary.json")
    summary_bytes = summary_path.read_bytes()
    real_write, real_remove = rc.atomic_write_json, sw._force_rmtree
    attempted, pending_written, removals = [], [], []

    def write(path, receipt, **kwargs):
        assert path == summary_path
        attempted.append(receipt.copy())
        pending = receipt.get("retention_reason") == "cleanup_pending"
        if (failure == "pending") == pending:
            raise OSError("injected receipt write failure")
        real_write(path, receipt, **kwargs)
        pending_written.append(receipt.copy())

    def remove(path):
        assert pending_written and json.loads(summary_path.read_text(encoding="utf8")) == pending_written[-1]
        removals.append(path)
        if failure != "residual_outcome":
            real_remove(path)

    monkeypatch.setattr(rc, "atomic_write_json", write)
    monkeypatch.setattr(sw, "_force_rmtree", remove)
    monkeypatch.setattr(rc, "_directory_delta", lambda *_args: pytest.fail("recaptured residual"))
    receipt = rc.capture_seat_evidence(data, "task", "write-failure", release=True)
    assert receipt == json.loads(summary_path.read_text(encoding="utf8"))
    assert receipt["status"] == "retained" and not receipt["refresh_safe"]
    if failure == "pending":
        assert receipt == prior and summary_path.read_bytes() == summary_bytes and not removals
    else:
        assert len(attempted) == 2 and removals == [area]
        assert receipt == pending_written[-1] and receipt["retention_reason"] == "cleanup_pending"
    assert area.exists() == (failure != "released_outcome")
    assert pathlib.Path(receipt["evidence_dir"], "files", "result.txt").read_bytes() == b"retained result\n"
