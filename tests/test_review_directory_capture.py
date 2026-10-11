"""Non-Git review consumers retain typed changes before releasing their copies."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import review_checking as rc
from ouroboros.review_execution import ReviewRouteKind
from ouroboros.review_substrate import run_review_request
from ouroboros.skill_loader import compute_content_hash
from ouroboros.skill_review_passes import skill_checking_source
from tests._review_session_route_shared import (  # noqa: F401 - transport fixtures
    FakeGateway,
    FakeLLM,
    _agent_request,
    _agent_slot,
    _owned_gateway_uses_each_test_transport,
    fake_route,
)
from tests.test_review_session_rights import writable_engine  # noqa: F401


def _subject(tmp_path, surface, *, links=False):
    root = tmp_path / "external" / "demo" if surface == "skill_review" else tmp_path / "folder"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: demo\ndescription: Fixture.\n---\nBody.\n")
    (root / "old-target").write_bytes(b"old\n")
    (root / "new-target").write_bytes(b"new\n")
    if links:
        try:
            (root / "retarget").symlink_to("old-target")
            (root / "removed-link").symlink_to("missing-target")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"this host cannot create symlinks: {exc}")
    for name in ("mode-dir", "removed-dir", "to-link"):
        (root / name).mkdir()
        (root / name).chmod(0o755)  # mkdir(mode=...) is filtered by the umask
    (root / "to-link" / "old-child").write_bytes(b"pristine child\n")
    return root


def _require_posix_modes(tmp_path):
    probe = tmp_path / "mode-probe"
    probe.mkdir()
    (probe / "file").write_bytes(b"")
    for path, mode in ((probe, 0o750), (probe / "file", 0o700)):
        path.chmod(mode)
        if stat.S_IMODE(path.stat().st_mode) != mode:
            pytest.skip("this filesystem does not preserve POSIX permission bits")


def _source(root, drive, surface, *, expected_hash=None):
    if surface != "skill_review":
        return rc.freeze_checking_source(root, data_root=drive)
    skill = SimpleNamespace(skill_dir=root, manifest=SimpleNamespace(entry="", scripts=[]))
    return skill_checking_source(
        drive, skill, {"routes": [ReviewRouteKind.AGENT_SESSION], "session_accesses": ["full"]},
        expected_hash if expected_hash is not None else compute_content_hash(root))


def _run(root, drive, surface, source, monkeypatch, edit):
    original_start = FakeGateway.start_run

    def start(self, request, **kwargs):
        edit(Path(request["execution"]["workspaceRoot"]))
        return original_start(self, request, **kwargs)

    monkeypatch.setattr(FakeGateway, "start_run", start)
    result = run_review_request(
        _agent_request(surface=surface, call_type=surface, task_id="directory-task",
                       session_root=str(root), checking_source=source),
        slots=[_agent_slot(slot_id="session", session_access="full")],
        drive_root=drive, llm=FakeLLM())
    return result.actors[0]


def _released(actor, area):
    assert actor["status"] == "ok", actor
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == "retained" and receipt["gaps"] == []
    evidence = Path(receipt["evidence_dir"])
    assert json.loads((evidence / "summary.json").read_text()) == receipt
    assert "entries" in receipt, "released directory evidence loses typed symlink and mode changes"
    assert receipt["refresh_safe"] and not area.exists()
    assert len(FakeGateway.instances[-1].start_requests) == 1
    return receipt["entries"], evidence


@pytest.mark.parametrize("surface", ["skill_review", "task_acceptance"])
def test_public_directory_consumer_retains_content_deletions_and_evidence_before_release(
    tmp_path, writable_engine, monkeypatch, surface,  # noqa: F811
):
    root, drive = _subject(tmp_path, surface), tmp_path / "drive"
    observed = {}

    def edit(area):
        observed["area"] = area
        (area / "new-target").write_bytes(b"rewritten\n")
        (area / "old-target").unlink()
        (area / "new-dir").mkdir()
        (area / "removed-dir").rmdir()
        (area / "to-link" / "old-child").unlink()
        (area / ".review-evidence-extra.txt").write_bytes(b"ordinary changed file\n")
        declared = area / rc.EVIDENCE_DIRNAME
        declared.mkdir()
        (declared / "proof.log").write_bytes(b"all checks complete\n")

    actor = _run(root, drive, surface, _source(root, drive, surface), monkeypatch, edit)
    entries, evidence = _released(actor, observed["area"])
    assert entries["new-target"]["after"]["kind"] == "file"
    assert entries["old-target"]["before"]["kind"] == "file" and entries["old-target"]["after"] is None
    assert entries["new-dir"]["before"] is None and entries["new-dir"]["after"]["kind"] == "directory"
    assert entries["removed-dir"]["before"]["kind"] == "directory" and entries["removed-dir"]["after"] is None
    assert entries["to-link/old-child"]["after"] is None
    assert (evidence / "files" / "new-target").read_bytes() == b"rewritten\n"
    assert (evidence / "files" / ".review-evidence-extra.txt").read_bytes() == b"ordinary changed file\n"
    assert (evidence / "files" / rc.EVIDENCE_DIRNAME / "proof.log").read_bytes() == b"all checks complete\n"
    assert (root / "new-target").read_bytes() == b"new\n" and (root / "old-target").read_bytes() == b"old\n"
    assert (root / "removed-dir").is_dir() and not (root / "new-dir").exists()
    assert (root / "to-link" / "old-child").read_bytes() == b"pristine child\n"


@pytest.mark.parametrize("surface", ["skill_review", "task_acceptance"])
def test_public_directory_consumer_retains_links_before_release(
    tmp_path, writable_engine, monkeypatch, surface,  # noqa: F811
):
    root, drive = _subject(tmp_path, surface, links=True), tmp_path / "drive"
    source = _source(root, drive, surface)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"must not follow directory link\n")
    observed = {}

    def edit(area):
        observed["area"] = area
        (area / "retarget").unlink()
        (area / "retarget").symlink_to("new-target")
        (area / "new-link").symlink_to("missing-new-target")
        (area / "removed-link").unlink()
        (area / "to-link" / "old-child").unlink()
        (area / "to-link").rmdir()
        (area / "to-link").symlink_to(outside, target_is_directory=True)
        # The host's own link text: Windows may report an absolute target with a \\?\ prefix.
        observed["outside-link"] = os.readlink(area / "to-link")

    actor = _run(root, drive, surface, source, monkeypatch, edit)
    entries, _evidence = _released(actor, observed["area"])
    assert entries["retarget"]["before"]["target"] == "old-target"
    assert entries["retarget"]["after"]["kind"] == "symlink"
    assert entries["retarget"]["after"]["target"] == "new-target"
    assert entries["new-link"]["before"] is None
    assert entries["new-link"]["after"]["target"] == "missing-new-target"
    assert entries["removed-link"]["before"]["target"] == "missing-target"
    assert entries["removed-link"]["after"] is None
    assert entries["to-link"]["before"]["kind"] == "directory"
    assert entries["to-link"]["after"]["target"] == observed["outside-link"]
    assert entries["to-link/old-child"]["after"] is None
    assert not any("private" in path for path in entries)
    assert os.readlink(root / "retarget") == "old-target"
    assert (root / "removed-link").is_symlink()
    assert (root / "to-link" / "old-child").read_bytes() == b"pristine child\n"
    assert (outside / "private").read_bytes() == b"must not follow directory link\n"


@pytest.mark.parametrize("surface", ["skill_review", "task_acceptance"])
def test_public_directory_consumer_retains_posix_modes_before_release(
    tmp_path, writable_engine, monkeypatch, surface,  # noqa: F811
):
    _require_posix_modes(tmp_path)
    root, drive = _subject(tmp_path, surface), tmp_path / "drive"
    observed = {}

    def edit(area):
        observed["area"] = area
        (area / "mode-dir").chmod(0o700)
        (area / "new-dir").mkdir()
        (area / "new-dir").chmod(0o750)
        (area / "removed-dir").rmdir()
        (area / "new-target").chmod(0o700)

    actor = _run(root, drive, surface, _source(root, drive, surface), monkeypatch, edit)
    entries, evidence = _released(actor, observed["area"])
    assert entries["mode-dir"]["before"] == {"kind": "directory", "mode": 0o755}
    assert entries["mode-dir"]["after"] == {"kind": "directory", "mode": 0o700}
    assert entries["new-dir"] == {"before": None, "after": {"kind": "directory", "mode": 0o750}}
    assert entries["removed-dir"] == {"before": {"kind": "directory", "mode": 0o755}, "after": None}
    assert entries["new-target"]["after"]["mode"] == 0o700
    assert (evidence / "files" / "new-target").read_bytes() == b"new\n"
    assert stat.S_IMODE((evidence / "files" / "new-target").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "mode-dir").stat().st_mode) == 0o755


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO output requires POSIX")
@pytest.mark.parametrize("surface", ["skill_review", "task_acceptance"])
def test_public_directory_consumer_keeps_copy_for_unretainable_output(
    tmp_path, writable_engine, monkeypatch, surface,  # noqa: F811
):
    root, drive = _subject(tmp_path, surface, links=True), tmp_path / "drive"
    actor = _run(root, drive, surface, _source(root, drive, surface), monkeypatch,
                 lambda area: os.mkfifo(area / "pipe"))
    assert actor["status"] == "ok", actor
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == "partial", "special output must not disappear behind a complete receipt"
    assert receipt["gaps"] == [{"path": "pipe", "reason": "unsupported_entry_type"}]
    assert receipt["entries"]["pipe"]["after"]["kind"] == "special"
    assert not receipt["refresh_safe"]
    assert stat.S_ISFIFO((Path(receipt["area"]) / "pipe").lstat().st_mode)


def test_skill_consumer_rejects_foreign_payload_hash_before_start(tmp_path, writable_engine):  # noqa: F811
    root, drive = _subject(tmp_path, "skill_review"), tmp_path / "drive"
    source = _source(root, drive, "skill_review", expected_hash="unrelated-payload-hash")
    result = run_review_request(
        _agent_request(surface="skill_review", call_type="skill_review", task_id="directory-task",
                       session_root=str(root), checking_source=source),
        slots=[_agent_slot(slot_id="session", session_access="full")],
        drive_root=drive, llm=FakeLLM())
    assert result.actors[0]["status"] != "ok"
    assert not any(gateway.start_requests for gateway in FakeGateway.instances)
    assert not list(drive.glob("state/review_checkouts/seats/*/area"))


def test_directory_metadata_budget_gap_keeps_source_and_paid_result(
    tmp_path, writable_engine, monkeypatch,  # noqa: F811
):
    root, drive = _subject(tmp_path, "task_acceptance", links=True), tmp_path / "drive"
    monkeypatch.setattr(rc, "EVIDENCE_RETAIN_BYTES", 128)

    def edit(area):
        (area / "long-link").symlink_to("x" * 512)
        (area / rc.EVIDENCE_DIRNAME).mkdir()
        (area / rc.EVIDENCE_DIRNAME / "proof.log").write_bytes(b"proof\n")

    actor = _run(root, drive, "task_acceptance", _source(root, drive, "task_acceptance"),
                 monkeypatch, edit)
    assert actor["status"] == "ok", actor
    receipt = actor["usage"]["checking_evidence"]
    assert receipt["status"] == "partial", "link metadata must also fit the retention budget"
    assert receipt["gaps"][0]["reason"] == "evidence_budget_exceeded"
    assert receipt["gaps"][0]["path"] == "directory-entries"
    assert receipt["gaps"][0]["size"] > 128
    assert receipt["entries"] == {} and not receipt["refresh_safe"]
    assert os.readlink(Path(receipt["area"]) / "long-link") == "x" * 512
    assert (Path(receipt["evidence_dir"]) / "files" / rc.EVIDENCE_DIRNAME / "proof.log").read_bytes() == b"proof\n"
