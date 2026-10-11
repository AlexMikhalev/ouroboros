"""Private checking copies of writable reviewer seats (review_checking).

Real git on a temp fixture: the frozen subject is read once, every seat gets an
independent copy, the author's repository (refs, stash, config, index, unstaged
work) is never touched by what a reviewer does in its copy, and the copy's work
survives as evidence before the copy is released.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros import review_checking as rc


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def author(tmp_path):
    repo = tmp_path / "author"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "a@b"), ("config", "user.name", "a")):
        _git(repo, *args)
    (repo / ".gitignore").write_text(".review-drive/\nbuild/\n")
    (repo / "f.txt").write_text("v0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "f.txt").write_text("A staged\n")
    _git(repo, "add", "f.txt")
    (repo / "f.txt").write_text("A staged\nB unstaged\n")
    (repo / ".review-drive" / "task").mkdir(parents=True)
    (repo / ".review-drive" / "task" / "view.txt").write_text("byte-exact evidence\n")
    return repo


def _author_state(repo):
    return {"stash": _git(repo, "stash", "list"), "refs": _git(repo, "show-ref"),
            "config": _git(repo, "config", "--list", "--local"), "index": _git(repo, "write-tree"),
            "worktree": (repo / "f.txt").read_text()}


def test_staged_subject_reaches_every_seat_and_reviewer_git_never_reaches_the_author(author, tmp_path):
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    before = _author_state(author)
    one = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    two = pathlib.Path(rc.prepare_seat_area(data, "task", "op-2", source))
    assert (one / "f.txt").read_text() == "A staged\n"  # staged A, never unstaged B
    assert _git(one, "write-tree") == source["tree"]
    assert _git(one, "rev-parse", "HEAD") == source["parent"]
    assert "f.txt" in _git(one, "diff", "--cached", "--name-only")
    assert (one / ".review-drive" / "task" / "view.txt").read_text() == "byte-exact evidence\n"
    # Ordinary reviewer git in its own copy: stash, branch, tag, config, commit.
    (one / "probe_test.py").write_text("assert True\n")
    _git(one, "add", "probe_test.py")
    subprocess.run(["git", "-c", "user.email=r@r", "-c", "user.name=r", "stash"], cwd=one, check=True,
                   capture_output=True)
    subprocess.run(["git", "stash", "pop"], cwd=one, check=True, capture_output=True)
    _git(one, "branch", "reviewer-branch")
    _git(one, "tag", "reviewer-tag")
    _git(one, "config", "core.reviewerKnob", "set")
    assert _author_state(author) == before
    assert not (two / "probe_test.py").exists()  # seats never share a copy
    with pytest.raises(subprocess.CalledProcessError):
        _git(one, "remote", "get-url", "origin")  # no default push target


def test_a_rejoin_reuses_the_copy_as_the_reviewer_left_it(author, tmp_path):
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    (area / "fixture.txt").write_text("reviewer work\n")
    _git(area, "add", "fixture.txt")  # an index-changed copy is still this seat's copy
    assert rc.prepare_seat_area(data, "task", "op-1", source) == str(area)
    assert (area / "fixture.txt").read_text() == "reviewer work\n"


def test_terminal_evidence_keeps_patch_named_files_and_ignored_listing_then_releases(author, tmp_path):
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    (area / "new_test.py").write_bytes(b"print(1)\n")
    (area / "build").mkdir()
    (area / "build" / "out.bin").write_bytes(b"\x00\x01")
    (area / rc.EVIDENCE_DIRNAME).mkdir()
    (area / rc.EVIDENCE_DIRNAME / "log.txt").write_text("pytest: 1 failed\n")
    evidence = rc.capture_seat_evidence(data, "task", "op-1", release=True)
    assert evidence["status"] == "retained" and not area.exists()
    assert "A\tnew_test.py" in evidence["changed"]
    assert all(".review-drive" not in row for row in evidence["changed"])  # host evidence is not reviewer work
    assert evidence["ignored_not_retained"] == ["build/"]
    assert [row["path"] for row in evidence["named_evidence"]] == [f"{rc.EVIDENCE_DIRNAME}/log.txt"]
    patch = pathlib.Path(evidence["evidence_dir"], "delta.patch").read_bytes()
    assert b"new_test.py" in patch and b"f.txt" not in patch  # the subject itself is not the reviewer's delta
    assert rc.capture_seat_evidence(data, "task", "op-1", release=True)["status"] == "retained"  # replayed, not recomputed


def test_a_failed_capture_keeps_the_recoverable_copy_and_names_the_gap(author, tmp_path):
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    os.rename(area / ".git", area / ".git-real")
    (area / ".git").symlink_to(area / ".git-real")  # a child-replaced .git is never trusted
    evidence = rc.capture_seat_evidence(data, "task", "op-1", release=True)
    assert evidence["status"] == "capture_failed" and area.exists()


def test_evidence_over_the_bound_keeps_the_copy(author, tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(rc, "EVIDENCE_RETAIN_BYTES", 4)
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    (area / rc.EVIDENCE_DIRNAME).mkdir()
    (area / rc.EVIDENCE_DIRNAME / "big.log").write_text("more than four bytes")
    evidence = rc.capture_seat_evidence(data, "task", "op-1", release=True)
    assert evidence["status"] == "partial" and evidence["gaps"][0]["reason"] == "evidence_budget_exceeded"
    assert area.exists() and evidence["area"] == str(area)


def test_a_worktree_subject_freezes_the_live_tree_once(author, tmp_path):
    data = tmp_path / "data"
    source = rc.freeze_checking_source(author, data_root=data)
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    (author / "f.txt").write_text("edited after the freeze\n")
    assert (area / "f.txt").read_text() == "A staged\nB unstaged\n"
    late = pathlib.Path(rc.prepare_seat_area(data, "task", "op-2", source))
    assert (late / "f.txt").read_text() == "A staged\nB unstaged\n"  # a later seat reads the same frozen input


def test_named_evidence_is_frozen_with_the_subject_not_read_live_by_each_seat(author, tmp_path):
    data, drive = tmp_path / "data", author / ".review-drive"
    source = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    first = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    (first / "probe.txt").write_text("seat experiment\n")
    (drive / "task" / "view.txt").write_text("rewritten after the freeze\n")
    (drive / "later").mkdir()
    (drive / "later" / "view.txt").write_text("a later attempt's view\n")
    late = pathlib.Path(rc.prepare_seat_area(data, "task", "op-2", source))
    thread = pathlib.Path(rc.thread_seat_area(data, "task", "plan_review:t1", source))
    for area in (first, late, thread):  # one wave, one evidence input
        assert (area / ".review-drive" / "task" / "view.txt").read_bytes() == b"byte-exact evidence\n"
        assert not (area / ".review-drive" / "later").exists()
    shutil.rmtree(drive)  # the attempt settled and released its view
    assert rc.prepare_seat_area(data, "task", "op-1", source) == str(first)  # a rejoin keeps the copy as left
    assert (first / "probe.txt").read_text() == "seat experiment\n"
    shutil.rmtree(late)  # a lost copy: the same operation's rejoin rematerializes the frozen input
    rc.prepare_seat_area(data, "task", "op-2", source)
    assert (late / ".review-drive" / "task" / "view.txt").read_bytes() == b"byte-exact evidence\n"
    shutil.rmtree(rc.seat_dir(data, "task", "op-2") / "area")
    for view in source["evidence_views"].values():
        shutil.rmtree(view)  # no longer retained: a named gap, never the live folder instead
    (drive / "task").mkdir(parents=True)
    (drive / "task" / "view.txt").write_text("newer live bytes\n")
    with pytest.raises(rc.CheckingAreaUnavailable, match="no longer retained"):
        rc.prepare_seat_area(data, "task", "op-2", source)
    with pytest.raises(rc.CheckingAreaUnavailable, match="no longer retained"):
        rc.thread_seat_area(data, "task", "plan_review:t2", source)
    assert not (rc.seat_dir(data, "task", "op-2") / "area").exists()  # refused before any clone


def test_an_ordinary_folder_is_one_retained_view_with_links_modes_and_empty_dirs(tmp_path):
    folder, data = tmp_path / "plain", tmp_path / "data"
    folder.mkdir()
    (folder / "run.sh").write_text("echo hi\n")
    (folder / "run.sh").chmod(0o755)
    (folder / "empty").mkdir()
    (folder / "link").symlink_to("run.sh")
    source = rc.freeze_checking_source(folder, data_root=data)
    assert source["kind"] == "directory"
    assert rc.freeze_checking_source(folder, data_root=data)["view"] == source["view"]  # unchanged folder, one view
    area = pathlib.Path(rc.prepare_seat_area(data, "task", "op-1", source))
    assert os.readlink(area / "link") == "run.sh" and (area / "empty").is_dir()
    assert os.access(area / "run.sh", os.X_OK)
    (folder / "run.sh").write_text("author edit\n")
    assert (area / "run.sh").read_text() == "echo hi\n"
    (area / "result.txt").write_text("checked")
    evidence = rc.capture_seat_evidence(data, "task", "op-1", release=True)
    assert evidence["changed"] == ["result.txt"] and [f["path"] for f in evidence["files"]] == ["result.txt"]


def test_a_payload_copy_must_carry_the_reviewed_hash(tmp_path):
    folder = tmp_path / "skill"
    folder.mkdir()
    (folder / "main.py").write_text("print('H')\n")
    digest = lambda root: (pathlib.Path(root) / "main.py").read_text()  # noqa: E731 - a stand-in loader hash
    bound = rc.bound_directory_source(folder, data_root=tmp_path / "data", expected_hash="print('H')\n",
                                      content_hash_of=digest)
    assert bound["kind"] == "directory" and bound["content_hash"] == "print('H')\n"
    moved = rc.bound_directory_source(folder, data_root=tmp_path / "data", expected_hash="H2", content_hash_of=digest)
    assert "unavailable" in moved


def test_only_writable_session_seats_freeze_a_source(author, tmp_path):
    seat = lambda route, access: SimpleNamespace(route=route, session_access=access)  # noqa: E731
    readonly = [seat("agent_session", "readonly"), seat("agent_session", ""), seat("api_chat", "full")]
    assert rc.wave_checking_source(str(author), readonly, data_root=tmp_path) == {}
    writable = [seat("agent_session", "full")]
    assert rc.wave_checking_source(str(author), writable, data_root=tmp_path)["kind"] == "git_tree"
    assert "unavailable" in rc.wave_checking_source(str(tmp_path / "missing"), [seat("agent_session", "full")],
                                                    data_root=tmp_path)


def test_a_thread_refresh_keeps_frozen_index_and_parent_without_synthetic_commits(author, tmp_path):
    data = tmp_path / "data"
    first = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.thread_seat_area(data, "task", "plan_review:t1", first, operation_id="turn-1"))
    assert _git(area, "write-tree") == first["tree"]
    assert _git(area, "rev-parse", "HEAD") == first["parent"]
    (author / "f.txt").write_text("A staged\nround two\n")
    _git(author, "add", "f.txt")
    second = rc.freeze_checking_source(author, data_root=data, subject=rc.SUBJECT_INDEX)
    with pytest.raises(rc.CheckingAreaUnavailable, match="quiescent"):
        rc.thread_seat_area(data, "task", "plan_review:t1", second, operation_id="turn-2")
    # This synchronous test owns all writers; capture acknowledges quiescence.
    retained = rc.capture_seat_evidence(data, "task", "turn-1", release=True)
    assert retained["refresh_safe"] and area.is_dir()
    with pytest.raises(rc.CheckingAreaUnavailable, match="same thread operation"):
        rc.thread_seat_area(data, "task", "plan_review:t1", second, operation_id="turn-1")
    assert rc.thread_seat_area(data, "task", "plan_review:t1", second, operation_id="turn-2") == str(area)
    assert _git(area, "write-tree") == second["tree"]
    assert _git(area, "rev-parse", "HEAD") == second["parent"]


def test_instructions_name_the_copy_the_pristine_subject_and_no_apply(author, tmp_path):
    source = rc.freeze_checking_source(author, data_root=tmp_path, subject=rc.SUBJECT_INDEX)
    text = rc.checking_instructions("/copy", source, str(author))
    assert "/copy" in text and source["tree"] in text and "nothing you change is applied" in text
    assert "not an OS sandbox" in text
    folder = {"kind": "directory", "root": "/payload", "view": "/views/x"}
    assert "reference repository stays /repo" in rc.checking_instructions("/copy", folder, "/repo")
    assert json.dumps(source)  # the frozen source is a plain, durable record


def test_a_rejoin_cannot_replace_the_original_checking_subject(author, tmp_path):
    drive = tmp_path / "drive"
    original = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    area = pathlib.Path(rc.prepare_seat_area(drive, "task-1", "original-op", original))
    (area / "retained-work.txt").write_bytes(b"unfinished check\n")
    (author / "f.txt").write_bytes(b"replacement subject\n")
    _git(author, "add", "f.txt")
    replacement = rc.freeze_checking_source(author, data_root=drive, subject=rc.SUBJECT_INDEX)
    with pytest.raises(rc.CheckingAreaUnavailable, match="cannot change its frozen source"):
        rc.prepare_seat_area(drive, "task-1", "original-op", replacement)
    assert (area / "f.txt").read_bytes() == b"A staged\n"
    assert (area / "retained-work.txt").read_bytes() == b"unfinished check\n"


def test_frozen_review_subject_survives_later_live_index_changes(author, tmp_path):
    from ouroboros.tools.review_subject import ReviewSubjectSpec, freeze_subject

    drive = tmp_path / "drive"
    frozen = freeze_subject(SimpleNamespace(), ReviewSubjectSpec(
        root_kind="active_workspace", root=str(author), kind="index", governance_root=str(author)))
    (author / "f.txt").write_bytes(b"later candidate\n")
    _git(author, "add", "f.txt")
    original_evidence = (author / ".review-drive/task/view.txt").read_bytes()
    source = rc.frozen_checking_source(frozen, data_root=drive)
    (author / ".review-drive/task/view.txt").write_bytes(b"later evidence\n")
    area = pathlib.Path(rc.prepare_seat_area(drive, "task", "original-subject", source))
    assert source["tree"] == frozen.tree_sha != _git(author, "write-tree")
    assert (area / "f.txt").read_bytes() == b"A staged\n"
    assert (area / ".review-drive/task/view.txt").read_bytes() == original_evidence
    assert (author / "f.txt").read_bytes() == b"later candidate\n"
