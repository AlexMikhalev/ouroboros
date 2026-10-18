"""Readers that announce a retained source test its presence through the pack-aware check, not the
loose file alone: once the packer has moved a terminal task's checkpoint handles into its pack, a
Continue successor still gets its predecessor's exact readers (no ``predecessor_source_unavailable``
gap) and a wake still names a settled late review's exact source. A handle that is truly gone stays
a disclosed gap."""
from __future__ import annotations

import json

from ouroboros import evidence_packer as packer, evidence_packs as packs
from ouroboros.artifacts import store_actor_source_bytes
from tests.test_evidence_packs import _handle_path, _result, root as root  # noqa: F401  (the shared fixture)


def _readers(root, owner, refs):
    from ouroboros.main_context_authority import _predecessor_review_readers

    return _predecessor_review_readers(root, owner, refs)


def test_a_successor_reads_its_predecessors_packed_sources_and_a_lost_one_stays_a_gap(root):
    ref = store_actor_source_bytes(root, "pred", category="context_checkpoints", source_id="plan-author-x",
                                   data=json.dumps({"decision": "keep the protocol"}).encode(), extension="json")
    _result(root, "pred", "failed")
    reads, gaps = _readers(root, "pred", [ref])
    assert [r["source_ref"]["sha256"] for r in reads] == [ref["sha256"]] and gaps == []
    assert packer.run_pass(root)["packed"] == 1
    assert not _handle_path(root, "pred", ref).exists() and packs.handle_info(root, "pred", ref["path"])["loose"] is False
    reads, gaps = _readers(root, "pred", [ref])  # the handle is a verified pack member: still read, same address
    assert [r["source_ref"]["sha256"] for r in reads] == [ref["sha256"]] and gaps == []
    assert reads[0]["read"]["arguments"]["root"] == "artifact_store"
    lost = store_actor_source_bytes(root, "pred", category="context_checkpoints", source_id="plan-author-y",
                                    data=b'{"decision": "lost"}', extension="json")
    _handle_path(root, "pred", lost).unlink()  # gone before any pack held it
    reads, gaps = _readers(root, "pred", [ref, lost])
    assert len(reads) == 1 and [g["reason"] for g in gaps] == ["predecessor_source_unavailable"]
    assert gaps[0]["source_ref"]["sha256"] == lost["sha256"]


def test_a_wake_names_the_exact_source_of_a_late_review_whose_handle_is_packed(tmp_path):
    from tests.test_consciousness_wake import T0, _iso, _late_panel, _lines, _wake, _write

    packs._READERS.clear()
    projection, _ref = _late_panel(tmp_path, "rv", pending=True)
    _write(tmp_path, "rv", status="completed", ts=_iso(T0 - 900), updated_at=_iso(T0 - 100), review_projection=projection)
    first = _wake(tmp_path, None, T0)
    projection, ref = _late_panel(tmp_path, "rv", settled_at=T0 + 60)
    _write(tmp_path, "rv", review_projection=projection)
    assert packer.run_pass(tmp_path)["packed"] == 1  # the settled task's handles move into its pack
    assert not _handle_path(tmp_path, "rv", ref).exists()
    line = _lines(_wake(tmp_path, first.boundary, T0 + 600))[0]
    assert "exact source {" in line and f"sha256 {ref['sha256']}" in line and "unavailable" not in line
    gone = _late_panel(tmp_path, "gone", settled_at=T0 + 60)
    _write(tmp_path, "gone", status="completed", ts=_iso(T0 - 900), updated_at=_iso(T0 - 100), review_projection=gone[0])
    _handle_path(tmp_path, "gone", gone[1]).unlink()  # no pack ever held it
    lines = _lines(_wake(tmp_path, None, T0 + 600))
    assert any("exact source unavailable" in text and "gone" in text for text in lines), lines
