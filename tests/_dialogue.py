"""Test helpers over ``state/dialog.sqlite``: the rows a root holds, as the chain files held them.

Tests that used to read ``logs/chat.jsonl`` / ``logs/progress.jsonl`` after a write assert on
these instead (same assertions, the SQL truth). ``seed`` writes legacy chain files for the
importer and the cutover to read.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Iterable, List

from ouroboros import dialogue_store


def rows(root: Any, stream: str = "chat") -> List[Dict[str, Any]]:
    """Every stored row of ``stream`` in chain order (the decoded bodies)."""
    return [entry.row for entry in dialogue_store.newest(pathlib.Path(root), stream, limit=1 << 40)]


def chat_rows(root: Any) -> List[Dict[str, Any]]:
    return rows(root, "chat")


def progress_rows(root: Any) -> List[Dict[str, Any]]:
    return rows(root, "progress")


def lines(root: Any, stream: str = "chat") -> List[str]:
    """The exact line texts (what the chain file would hold, without newlines)."""
    return [dialogue_store.encode(entry.row) for entry in dialogue_store.newest(pathlib.Path(root), stream, limit=1 << 40)]


def write_segment(path: pathlib.Path, entries: Iterable[Any]) -> pathlib.Path:
    """A legacy chain file: dict rows are encoded as ``append_jsonl`` did, strings are written verbatim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for entry in entries:
            if isinstance(entry, dict):
                handle.write((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            else:
                handle.write(entry.encode("utf-8") if isinstance(entry, str) else entry)
    return path


def seed(root: pathlib.Path, *, archives: Dict[str, Iterable[Any]] = None, live: Dict[str, Iterable[Any]] = None) -> None:
    """Archive segments (``archive/<name>``) and live files (``logs/<stream>.jsonl``) on a root."""
    for name, entries in (archives or {}).items():
        write_segment(root / "archive" / name, entries)
    for stream, entries in (live or {}).items():
        write_segment(root / "logs" / f"{stream}.jsonl", entries)
