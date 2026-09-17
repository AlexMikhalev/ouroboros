"""The same Markdown source/address and revision survive every KB operation."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

from ouroboros import knowledge as store
from ouroboros.tools import knowledge as tools
from ouroboros.tools.registry import ToolContext


def address(tmp_path, topic="people/Антон", scope="global"):
    return store.resolve_knowledge_address(tmp_path, topic, scope)


def history(addr):
    return [json.loads(line) for line in (addr.shelf.parent / "knowledge_history.jsonl").read_text(encoding="utf-8").splitlines()]


def test_plain_new_note_gets_minimal_format_without_invented_summary(tmp_path):
    target = address(tmp_path)
    result = store.write_knowledge_note(target, "# Антон\n\nA personal observation, not a permanent rule.")
    assert result.ok
    assert result.current.metadata == {"type": "note"}
    assert result.current.summary == ""
    assert "personal observation" not in (target.shelf / store.INDEX_FILE).read_text(encoding="utf-8")
    assert result.current.text.endswith("not a permanent rule.")
    assert result.current.revision == hashlib.sha256(target.path.read_bytes()).hexdigest()


def test_authored_multiline_summary_and_unknown_metadata_survive_revision(tmp_path):
    target = address(tmp_path)
    source = """---
type: evolving-understanding
title: Anton
summary: |
  Brevity helps when he is hurried.
  Detailed reasoning is welcome when he asks for it.
custom:
  evidence: [episode-1, episode-2]
---

# Anton

[Recent work](../work/research.md)
An interpretation grounded in two episodes.
"""
    first = store.write_knowledge_note(target, source)
    assert first.ok and first.current.text == source
    index = (target.shelf / store.INDEX_FILE).read_text(encoding="utf-8")
    assert "Brevity helps" in index and "Detailed reasoning" in index
    second = store.write_knowledge_note(target, "---\nsummary: A revised interpretation.\n---\nNew episode.",
                                       expected_revision=first.current.revision)
    assert second.ok
    assert second.current.metadata["type"] == "evolving-understanding"
    assert second.current.metadata["custom"] == {"evidence": ["episode-1", "episode-2"]}
    assert second.current.metadata["title"] == "Anton"
    assert second.current.summary == "A revised interpretation."
    assert history(target)[-1]["old_content"] == source
    assert history(target)[-1]["new_content"] == second.current.text


def test_legacy_read_append_and_explicit_overwrite_do_not_force_migration(tmp_path):
    target = address(tmp_path, "old")
    target.shelf.mkdir(parents=True)
    target.path.write_bytes(b"# Legacy\r\nA plain note.")
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    read = store.read_knowledge_note(target)
    assert read.raw == b"# Legacy\r\nA plain note."
    assert read.metadata == {} and read.summary == ""
    store.inventory_knowledge(target)
    assert before == {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    appended = store.write_knowledge_note(target, "New evidence.", "append")
    assert appended.ok and appended.current.raw == read.raw + b"\nNew evidence."
    changed = store.write_knowledge_note(target, "# Legacy\nReconsidered.", expected_revision=appended.current.revision)
    assert changed.ok and changed.current.text == "# Legacy\nReconsidered."


def test_stale_overwrite_preserves_newer_note_index_and_history(tmp_path):
    target = address(tmp_path)
    first = store.write_knowledge_note(target, "First understanding.").current
    second = store.write_knowledge_note(target, "New evidence.", expected_revision=first.revision).current
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    stale = store.write_knowledge_note(target, "Outdated rewrite.", expected_revision=first.revision)
    assert not stale.ok and stale.reason == "revision_conflict"
    assert stale.current.raw == second.raw
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    no_revision = store.write_knowledge_note(target, "Unchecked replacement.")
    assert not no_revision.ok and no_revision.reason == "revision_required"
    assert no_revision.current.revision == second.revision


def test_two_readers_one_revision_cannot_both_overwrite(tmp_path):
    target = address(tmp_path)
    original = store.write_knowledge_note(target, "Original.").current
    start = threading.Barrier(3)
    results = []

    def write(content):
        start.wait()
        results.append(store.write_knowledge_note(target, content, expected_revision=original.revision))

    workers = [threading.Thread(target=write, args=(text,)) for text in ("Episode A", "Episode B")]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(3)
    assert all(not worker.is_alive() for worker in workers)
    assert sorted(result.ok for result in results) == [False, True]
    winner = next(result.current for result in results if result.ok)
    assert store.read_knowledge_note(target).revision == winner.revision
    assert history(target)[-1]["new_content"] == winner.text


def test_noop_does_not_publish_history_or_rewrite_index(tmp_path):
    target = address(tmp_path)
    first = store.write_knowledge_note(target, "---\ntype: note\n---\nSame.").current
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = store.write_knowledge_note(target, first.text, expected_revision=first.revision)
    assert result.ok and result.reason == "unchanged"
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_nested_inventory_and_links_keep_addresses_not_display_identity(tmp_path):
    for topic in ("people/a", "people/b", "work/research"):
        assert store.write_knowledge_note(address(tmp_path, topic), f"---\ntype: note\ntitle: Same name\n---\n{topic}").ok
    first = store.read_knowledge_note(address(tmp_path, "people/a"))
    store.write_knowledge_note(address(tmp_path, "people/b", "project:p"), "Another person with the same name.")
    changed = store.write_knowledge_note(first.address, "[Known](b.md)\n[Unknown](../not-written.md)\n[Project](../../../projects/p/knowledge/people/b.md)", expected_revision=first.revision)
    rows = store.inventory_knowledge(first.address)
    assert {row["topic"] for row in rows} == {"people/a", "people/b", "work/research"}
    links = store.knowledge_links(changed.current)
    assert links[0]["address"]["topic"] == "people/b" and links[0]["status"] == "present"
    assert links[1]["status"] == "unwritten"
    assert links[2]["path"] == str(tmp_path / "projects" / "p" / "knowledge" / "people" / "b.md")
    assert links[2]["address"]["scope"] == "project:p"
    assert links[2]["status"] == "present"


def test_project_and_global_scope_refs_resolve_from_forked_context(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path / "child", project_id="p")
    ctx.budget_drive_root = str(tmp_path)
    tools._knowledge_write(ctx, "same", "Project fact.")
    tools._knowledge_write(ctx, "same", "Global understanding.", scope="global")
    local = store.read_knowledge_note(address(tmp_path, "same", "project:p"))
    shared = store.read_knowledge_note(address(tmp_path, "same", "global"))
    assert local.raw != shared.raw
    assert not (ctx.drive_root / "memory" / "knowledge").exists()
    for note in (local, shared):
        ref = note.source_ref()
        rendered = tools._knowledge_read(ctx, **ref["read"]["arguments"])
        assert note.revision in rendered and note.text in rendered
        assert ref["canonical_root"] == str(tmp_path)
    assert "Project fact." not in tools._knowledge_read(ctx, "same", scope="global")


def test_model_visible_read_body_and_revision_have_exact_metadata(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx._active_builtin_tool_result = None
    note = store.write_knowledge_note(address(tmp_path, "facts"), "# Facts\nExact body.").current
    rendered = tools._knowledge_read(ctx, "facts")
    result = ctx._active_builtin_tool_result
    assert result.meta["knowledge_source"]["revision"] == note.revision
    assert note.revision in rendered
    start, length = result.meta["knowledge_body_start"], result.meta["knowledge_body_chars"]
    assert rendered[start:start + length] == note.text
    assert rendered.endswith(note.text)


def test_malformed_legacy_yaml_is_readable_and_does_not_break_inventory(tmp_path):
    bad = address(tmp_path, "broken")
    bad.shelf.mkdir(parents=True)
    raw = b"---\ncustom: [unfinished\n---\n# Still readable\nOriginal evidence.\n"
    bad.path.write_bytes(raw)
    read = store.read_knowledge_note(bad)
    assert read.raw == raw and read.parse_error
    good = store.write_knowledge_note(address(tmp_path, "good"), "Useful note.")
    assert good.ok
    rows = store.inventory_knowledge(bad)
    assert {row["topic"] for row in rows} == {"broken", "good"}
    assert "source metadata unavailable" in (bad.shelf / store.INDEX_FILE).read_text(encoding="utf-8")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    assert "Original evidence." in tools._knowledge_read(ctx, "broken")


def test_history_failure_leaves_previous_source_and_index_intact(tmp_path, monkeypatch):
    target = address(tmp_path)
    original = store.write_knowledge_note(target, "Known state.").current
    index = (target.shelf / store.INDEX_FILE).read_bytes()
    monkeypatch.setattr(store, "append_jsonl", lambda *a, **k: False)
    result = store.write_knowledge_note(target, "Not safely recorded.", expected_revision=original.revision)
    assert not result.ok and result.reason == "history_unavailable"
    assert target.path.read_bytes() == original.raw
    assert (target.shelf / store.INDEX_FILE).read_bytes() == index


def test_index_publication_failure_reports_actual_new_source_and_retains_capture(tmp_path, monkeypatch):
    target = address(tmp_path)
    original = store.write_knowledge_note(target, "Known state.").current

    def fail_index(_address):
        raise OSError("index cannot publish")

    monkeypatch.setattr(store, "rebuild_knowledge_index", fail_index)
    result = store.write_knowledge_note(target, "New state.", expected_revision=original.revision)
    assert not result.ok and result.reason == "publication_incomplete"
    assert result.current.raw == target.path.read_bytes()
    assert "New state." in result.current.text
    assert history(target)[-1]["old_content"] == original.text
    assert history(target)[-1]["new_content"] == result.current.text


@pytest.mark.parametrize("value", ["null", "12", "[]", "''"])
def test_new_formal_note_requires_string_type_without_a_type_catalog(tmp_path, value):
    target = address(tmp_path)
    result = store.write_knowledge_note(target, f"---\ntype: {value}\n---\nBody.")
    assert not result.ok and result.reason.startswith("invalid_note")
    assert not target.path.exists()


@pytest.mark.parametrize("topic", ["../escape", "/absolute", "a/../escape", "index-full", "a\\b"])
def test_invalid_address_is_not_normalized_into_another_source(tmp_path, topic):
    with pytest.raises(ValueError):
        address(tmp_path, topic)


def test_missing_read_and_list_create_no_state(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path / "not-created")
    assert "not found" in tools._knowledge_read(ctx, "missing")
    assert "empty" in tools._knowledge_list(ctx)
    assert not ctx.drive_root.exists()


def test_legacy_index_context_survives_until_authored_overview_without_recursive_growth(tmp_path):
    target = address(tmp_path, "details")
    target.shelf.mkdir(parents=True)
    original = "# Knowledge Base Index\n\n- **old**: A useful older understanding.\n"
    index = target.shelf / store.INDEX_FILE
    index.write_bytes(original.encode("utf-8"))
    assert store.write_knowledge_note(target, "New detailed evidence.").ok
    for topic in ("second", "third"):
        assert store.write_knowledge_note(address(tmp_path, topic), "A detail.").ok
        text = index.read_text(encoding="utf-8")
        assert text.count(original) == 1
        assert text.count("## Earlier generated context") == 1
        assert "historical context, not current authored summaries" in text
    assert sum(row.get("type") == "knowledge_index_source" for row in history(target)) == 1
    assert store.write_knowledge_note(address(tmp_path, "overview"),
                                      "# What I currently understand\n\nA reconsidered overview with [details](details.md).").ok
    assert "Earlier generated context" not in index.read_text(encoding="utf-8")
    assert any(row.get("old_content") == original for row in history(target))
