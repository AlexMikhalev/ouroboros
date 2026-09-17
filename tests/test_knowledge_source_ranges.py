"""Partial note reads retain one complete source identity and honest coverage."""

import hashlib
import json

from ouroboros.tools.knowledge import _knowledge_read
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.tool_result import _install_tool_result_sidecar, _published_tool_result, _restore_tool_result_sidecar


def test_partial_reads_reconstruct_exact_note_with_one_revision(tmp_path):
    text = "---\ntype: note\nsummary: Authored understanding.\n---\n\n" + "αβγ\r\n" * 40
    path = tmp_path / "memory/knowledge/person.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode())
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    parts = []
    for start, end in [(0, 80), (80, len(text))]:
        sentinel = object()
        token = _install_tool_result_sidecar(ctx, sentinel)
        try:
            result = _knowledge_read(ctx, "person", start_char=start, end_char=end)
            meta = _published_tool_result(ctx, sentinel).meta
        finally:
            _restore_tool_result_sidecar(token)
        assert not meta["knowledge_source_complete"]
        source = meta["knowledge_source"]
        assert source["revision"] == hashlib.sha256(text.encode()).hexdigest()
        assert source["complete_chars"] == len(text)
        assert (source["start_char"], source["end_char"]) == (start, end)
        body = result[meta["knowledge_body_start"]:]
        assert body == text[start:end]
        parts.append(body)
        assert json.loads(result.splitlines()[0].removeprefix("[Knowledge source] ")) == source
    assert "".join(parts) == text


def test_invalid_range_cannot_claim_full_source(tmp_path):
    path = tmp_path / "memory/knowledge/note.md"
    path.parent.mkdir(parents=True)
    path.write_text("Source")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    assert "range" in _knowledge_read(ctx, "note", start_char=10, end_char=20)
