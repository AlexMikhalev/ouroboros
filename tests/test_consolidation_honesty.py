"""Dialogue consolidation reports what it actually did.

Three honesty facts of this one stage: a run that advanced the cursor without failing
retires a stale error; every non-None return carries the block count it wrote; and a
nomination batch that was accepted but not fully published leaves a durable receipt
that era compression cannot erase, surfaced as a Health line.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros import consolidator as c
from ouroboros import context_health
from ouroboros.tools.registry import ToolContext
from tests import test_consolidator_context_fit as fit_helpers
from tests.test_consolidator_context_fit import _LLM, _Refusal, _paths, _write_chat

fit = fit_helpers.fit


def _health_env(tmp_path):
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path,
                           repo_path=lambda p: tmp_path / p, drive_path=lambda p: tmp_path / p)


# --- stale error is retired only by a run that recorded none of its own -----------


def test_a_chronicle_only_pass_still_reports_zero_written_blocks(tmp_path, fit, monkeypatch):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=5, text_size=0)  # below the block size: nothing to consolidate
    monkeypatch.setattr(c, "_compact_chronicle", lambda *a, **k: {"prompt_tokens": 1, "completion_tokens": 1,
                                                                   "total_tokens": 2, "cost": 0.0})
    usage = c.consolidate(chat, blocks, meta, _LLM(), compact_chronicle=True, pressure_fits=lambda: False)
    assert usage["_blocks_written"] == 0


def test_a_chronicle_pass_after_a_real_run_keeps_the_written_count(tmp_path, fit, monkeypatch):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, text_size=0)
    monkeypatch.setattr(c, "_compact_chronicle", lambda *a, **k: {"prompt_tokens": 1, "completion_tokens": 1,
                                                                   "total_tokens": 2, "cost": 0.0})
    usage = c.consolidate(chat, blocks, meta, _LLM(), compact_chronicle=True, pressure_fits=lambda: False)
    assert usage["_blocks_written"] == 1


def test_light_is_told_to_author_summaries_and_keep_explicit_requests_explicit():
    # Light never sees the knowledge_write schema, so the maintenance prompt is its only carrier.
    assert "YAML summary" in c.KNOWLEDGE_MAINTENANCE_PROMPT
    assert "resident in the index" in c.KNOWLEDGE_MAINTENANCE_PROMPT
    assert "explicit standing request stays explicit" in c.KNOWLEDGE_MAINTENANCE_PROMPT


def test_a_nominated_note_with_a_summary_becomes_resident_in_the_index(tmp_path):
    from ouroboros.knowledge import inventory_knowledge, render_knowledge_index, resolve_knowledge_address
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, budget_drive_root=str(tmp_path), task_id="t")
    entry = {"topic": "people/alex", "scope": "global", "expected_revision": None,
             "content": "---\ntype: understanding\nsummary: Alex asks for brevity; an interpretation to test.\n---\nEvidence."}
    outcomes = c._write_knowledge_entries(tmp_path / "memory" / "knowledge", [entry], context=ctx)
    assert outcomes and outcomes[0]["ok"]
    rendered = render_knowledge_index(inventory_knowledge(resolve_knowledge_address(tmp_path, "overview", "global")))
    assert "Alex asks for brevity" in rendered


def test_clean_run_clears_a_stale_error_from_an_earlier_run(tmp_path, fit):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, text_size=0)
    meta.parent.mkdir(parents=True, exist_ok=True)
    c.atomic_write_json(meta, {"last_consolidated_offset": 0,
                               "chat_log_signature": c._chat_log_signature(chat),
                               "last_consolidation_error": {"kind": "context_overflow", "cursor_offset": 0}})
    c.consolidate(chat, blocks, meta, _LLM())
    saved = json.loads(meta.read_text())
    assert saved["last_consolidated_offset"] == 100
    assert "last_consolidation_error" not in saved


def test_a_run_that_never_advances_keeps_the_stale_error(tmp_path, fit):
    """No cursor movement, no proof: the earlier failure is still the latest word."""
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=100, text_size=0)
    meta.parent.mkdir(parents=True, exist_ok=True)
    stale = {"kind": "provider_failed", "cursor_offset": 0}
    c.atomic_write_json(meta, {"last_consolidated_offset": 0,
                               "chat_log_signature": c._chat_log_signature(chat),
                               "last_consolidation_error": stale})

    def refuse(_llm, _prompt):
        raise _Refusal("auth failed", code="invalid_api_key")

    c.consolidate(chat, blocks, meta, _LLM(effect=refuse))
    assert json.loads(meta.read_text())["last_consolidation_error"]["kind"]


# --- every non-None return carries its own block count ---------------------------


def test_block_count_is_reported_on_a_successful_run(tmp_path, fit):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=200, text_size=0)
    usage = c.consolidate(chat, blocks, meta, _LLM())
    assert usage["_blocks_written"] == 2


def test_block_count_is_zero_when_nothing_was_written(tmp_path, fit):
    """An empty summary writes no block; the receipt says 0 rather than going absent."""
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=100, text_size=0)

    def empty(_llm, _prompt):
        return {"content": "   "}, {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1, "cost": 0.0}

    usage = c.consolidate(chat, blocks, meta, _LLM(effect=empty))
    assert usage["_blocks_written"] == 0
    assert not blocks.exists()


def test_block_count_is_zero_when_nomination_retention_is_refused(tmp_path, fit, monkeypatch):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=100, text_size=0)

    class Nominating:
        def chat(self, **kwargs):
            return {"content": "Episode.\nKNOWLEDGE_ENTRIES_JSON: " + json.dumps(
                [{"topic": "people/alex", "content": "A durable understanding."}])}, {"cost": 0.01}

    monkeypatch.setattr(c, "append_jsonl", lambda *a, **k: False)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="refused")
    usage = c.consolidate(chat, blocks, meta, Nominating(), knowledge_context=ctx)
    assert usage["_blocks_written"] == 0
    assert not blocks.exists()  # the cursor and the blocks are both preserved for retry


@pytest.mark.parametrize("usage, blocks_written, error_kind", [
    ({"cost": 0.25, "prompt_tokens": 10, "_blocks_written": 2,
      "_consolidation_errors": [{"kind": "context_overflow"}, {"kind": "provider_failed"}]}, 2, "provider_failed"),
    ({"cost": 0.0, "_blocks_written": 0, "_consolidation_errors": []}, 0, None),
])
def test_the_event_row_carries_the_block_count_and_the_last_error_kind(
    tmp_path, monkeypatch, usage, blocks_written, error_kind,
):
    """post_task_synthesis turns the usage receipt into the observable event row."""
    import supervisor.state as state

    from ouroboros import post_task_synthesis as pts

    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path, drive_path=lambda p: tmp_path / p)
    memory = SimpleNamespace(load_identity=lambda: "identity")
    monkeypatch.setattr(c, "should_consolidate", lambda *_a, **_k: True)
    monkeypatch.setattr(c, "consolidate", lambda **_k: usage)
    monkeypatch.setattr(state, "update_budget_from_usage", lambda *_a, **_k: None)

    pts._run_chat_consolidation(env, memory, object(), {"id": "task-1"}, logs)

    row = json.loads((logs / "events.jsonl").read_text().splitlines()[-1])
    assert row["type"] == "chat_block_consolidation"
    assert row["blocks_written"] == blocks_written
    assert row["last_error_kind"] == error_kind


# --- unpublished nominations leave a receipt that survives era compression --------


class _Nominating:
    """One nomination per block; the caller decides whether publication succeeds."""

    def __init__(self, topic="people/alex"):
        self.topic, self.count = topic, 0

    def chat(self, **kwargs):
        if kwargs["messages"][0]["content"].startswith("Compress these older memory blocks"):
            return {"content": "### Era\nThe full historical span remains represented."}, {"cost": 0.01}
        self.count += 1
        return {"content": f"Episode {self.count}.\nKNOWLEDGE_ENTRIES_JSON: " + json.dumps(
            [{"topic": self.topic, "content": f"Understanding {self.count}."}])}, {"cost": 0.01}


def test_partial_publication_records_the_batch_receipt_in_meta(tmp_path, fit, monkeypatch):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=100, text_size=0)
    monkeypatch.setattr(c, "_write_knowledge_entries",
                        lambda *_a, **_k: [{"topic": "people/alex", "ok": False, "reason": "revision_conflict"}])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="partial")
    c.consolidate(chat, blocks, meta, _Nominating(), knowledge_context=ctx)
    receipt = json.loads(meta.read_text())["last_unpublished_nominations"]
    assert receipt["failed"] == 1 and receipt["total"] == 1 and receipt["entry_id"]


def test_a_fully_published_batch_clears_the_receipt(tmp_path, fit):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=100, text_size=0)
    meta.parent.mkdir(parents=True, exist_ok=True)
    c.atomic_write_json(meta, {"last_unpublished_nominations": {"entry_id": "old", "failed": 3, "total": 4}})
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="clean")
    c.consolidate(chat, blocks, meta, _Nominating(), knowledge_context=ctx)
    assert "last_unpublished_nominations" not in json.loads(meta.read_text())


def test_a_run_without_nominations_leaves_the_receipt_alone(tmp_path, fit):
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=100, text_size=0)
    meta.parent.mkdir(parents=True, exist_ok=True)
    standing = {"entry_id": "old", "failed": 2, "total": 5}
    c.atomic_write_json(meta, {"last_unpublished_nominations": standing})
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="silent")
    c.consolidate(chat, blocks, meta, _LLM(), knowledge_context=ctx)
    assert json.loads(meta.read_text())["last_unpublished_nominations"] == standing


def test_the_receipt_survives_era_compression(tmp_path, fit, monkeypatch):
    """Era compression replaces blocks with an object carrying no knowledge_writes."""
    chat, blocks, meta = _paths(tmp_path)
    _write_chat(chat, count=1100, text_size=0)
    monkeypatch.setattr(c, "_write_knowledge_entries",
                        lambda _shelf, entries, **_k: [{"topic": "people/alex", "ok": False,
                                                        "reason": "revision_conflict"} for _ in entries])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="era")
    c.consolidate(chat, blocks, meta, _Nominating(), knowledge_context=ctx)
    saved_blocks = json.loads(blocks.read_text())
    assert saved_blocks[0]["type"] == "era"
    assert "knowledge_writes" not in saved_blocks[0]
    receipt = json.loads(meta.read_text())["last_unpublished_nominations"]
    assert receipt["failed"] == 11 and receipt["total"] == 11


# --- the Health block is where stale memory becomes visible -----------------------


def test_health_names_an_incomplete_publication_with_its_recovery_route(tmp_path):
    env = _health_env(tmp_path)
    c.atomic_write_json(tmp_path / "memory" / "dialogue_meta.json",
                        {"last_unpublished_nominations": {"entry_id": "abc123", "failed": 2, "total": 5}})
    lines = context_health._memory_health_lines(env)
    row = next(line for line in lines if "PUBLICATION INCOMPLETE" in line)
    assert "2 of 5 nominations" in row and "abc123" in row
    assert "memory/knowledge_history.jsonl" in row and "read_file(root='runtime_data'" in row


def test_health_names_the_last_consolidation_failure(tmp_path):
    env = _health_env(tmp_path)
    c.atomic_write_json(tmp_path / "memory" / "dialogue_meta.json",
                        {"last_consolidation_error": {"kind": "context_overflow", "cursor_offset": 400}})
    row = next(line for line in context_health._memory_health_lines(env)
               if "LAST DIALOGUE CONSOLIDATION FAILED" in line)
    assert "kind=context_overflow" in row and "at cursor 400" in row


def test_health_stays_silent_when_the_pipeline_is_healthy(tmp_path):
    env = _health_env(tmp_path)
    c.atomic_write_json(tmp_path / "memory" / "dialogue_meta.json", {"last_consolidated_offset": 100})
    lines = context_health._memory_health_lines(env)
    assert not any("DIALOGUE" in line for line in lines)


def test_health_lines_carry_no_timestamp(tmp_path):
    """These are latest-run STATE, not events: a clock in them would read as freshness."""
    env = _health_env(tmp_path)
    c.atomic_write_json(tmp_path / "memory" / "dialogue_meta.json",
                        {"last_unpublished_nominations": {"entry_id": "abc", "failed": 1, "total": 1},
                         "last_consolidation_error": {"kind": "provider_failed", "cursor_offset": 0,
                                                      "ts": "2026-09-14T00:00:00Z"}})
    dialogue = [line for line in context_health._memory_health_lines(env) if "DIALOGUE" in line]
    assert len(dialogue) == 2
    assert not any("2026-" in line for line in dialogue)


def test_memory_health_lines_still_carry_identity_and_scratchpad(tmp_path):
    """The extracted helper keeps the existing own-memory checks it was split from."""
    env = _health_env(tmp_path)
    (tmp_path / "memory" / "identity.md").write_text("thin", encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text("x", encoding="utf-8")
    lines = context_health._memory_health_lines(env)
    assert any("THIN IDENTITY" in line for line in lines)
    assert any("EMPTY SCRATCHPAD" in line for line in lines)


@pytest.mark.parametrize("payload", [{"last_unpublished_nominations": "corrupt"},
                                     {"last_unpublished_nominations": {"failed": 0, "total": 3}},
                                     {"last_consolidation_error": "corrupt"}])
def test_unreadable_receipts_do_not_raise_or_shout(tmp_path, payload):
    env = _health_env(tmp_path)
    c.atomic_write_json(tmp_path / "memory" / "dialogue_meta.json", payload)
    assert not any("DIALOGUE" in line for line in context_health._memory_health_lines(env))
