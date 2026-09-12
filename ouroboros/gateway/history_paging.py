"""Physical continuation over the existing retained chat/progress JSONL chains."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from bisect import bisect_left
from pathlib import Path

from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.gateway import _helpers
from ouroboros.gateway._helpers import _TAIL_WINDOW_START_BYTES
from ouroboros.utils import JsonlChainUnreadable, jsonl_chain_handles

_SOURCES = ("chat", "progress")
_READ_BYTES = 64 * 1024
_PAGE_SCAN_BYTES = 512 * 1024
_PAGE_SCAN_ROWS = 1000


def progress_quota_predicate(row_matches_thread, stored_chat_id):
    def counts(entry):
        if not isinstance(entry, dict) or entry.get("type") in {"review_reference", "task_model_wait"}:
            return False
        if is_a2a_chat_id(entry.get("chat_id", 1)):
            return False
        return bool(
            row_matches_thread(stored_chat_id(entry.get("chat_id"), 1), {"is_progress": True, **entry})
            and str(entry.get("content", entry.get("text", "")))
            and str(entry.get("delegation_role") or "").lower() != "subagent"
            and not entry.get("subagent_event")
        )
    return counts


class HistoryCursorError(ValueError):
    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason, self.status = reason, status


def room_view_fingerprint(thread_id, project_ids, source_refs, bindings):
    """Fingerprint the existing room lens, without introducing membership state."""
    project = thread_id in project_ids
    relevant = {key: value for key, value in bindings.items()
                if not project or value == thread_id}
    value = [thread_id, project, [] if project else sorted(project_ids), relevant,
             sorted(source_refs, key=lambda row: json.dumps(row, sort_keys=True))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value, thread_id, view):
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(decoded)
        if not isinstance(cursor, dict) or cursor.get("v") != 1 or cursor.get("kind") not in {"older", "page"}:
            raise ValueError
        for field in ("upper", "before"):
            if set(cursor[field]) != set(_SOURCES):
                raise ValueError
            if any(type(cursor[field][source]) is not int or cursor[field][source] < 0 for source in _SOURCES):
                raise ValueError
        if any(cursor["before"][source] > cursor["upper"][source] for source in _SOURCES):
            raise ValueError
        if set(cursor["quotas"]) != {"human", "progress"} or any(
            type(value) is not int or value < 0 for value in cursor["quotas"].values()
        ):
            raise ValueError
        if cursor["kind"] == "page":
            if type(cursor.get("recent")) is not bool or set(cursor["lower"]) != set(_SOURCES):
                raise ValueError
            if any(type(cursor["lower"][source]) is not int
                   or not 0 <= cursor["lower"][source] <= cursor["before"][source] for source in _SOURCES):
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise HistoryCursorError("history_cursor_invalid", 400) from None
    if cursor.get("chat_id") != thread_id or cursor.get("view") != view:
        raise HistoryCursorError("history_view_changed")
    return cursor


class HistorySource:
    """One metadata snapshot, with no handles retained across a read batch."""

    def __init__(self, path: Path, source: str, upper=None):
        self.path, self.source, self.snapshot = path, source, {}
        with jsonl_chain_handles(path, strict=True, start_offset=0, snapshot=self.snapshot):
            pass
        self.upper = self.snapshot["total"] if upper is None else upper
        if self.upper > self.snapshot["total"]:
            raise JsonlChainUnreadable("history source is shorter than its captured boundary")
        if upper is None and self.snapshot["entries"] and self.snapshot["entries"][-1][2]:
            base, end = self._segment_range(len(self.snapshot["entries"]) - 1)
            # A writer owns an unfinished live line. Freeze only complete rows,
            # so completing/rotating that line later cannot alter this page.
            while end > base and self._read(end - 1, end) != b"\n":
                start = max(base, end - _READ_BYTES)
                data = self._read(start, end)
                newline = data.rfind(b"\n")
                end = start + newline + 1 if newline >= 0 else start
            self.upper = end

    def _read(self, start, end):
        parts = []
        while start < end:
            with jsonl_chain_handles(self.path, strict=True, start_offset=start,
                                     snapshot=self.snapshot) as handles:
                if not handles:
                    raise JsonlChainUnreadable("history source ended before its captured boundary")
                _path, handle = handles[0]
                index = bisect_left(self.snapshot["ends"], start + 1)
                size = min(end, self.snapshot["ends"][index]) - start
                data = handle.read(size)
                if len(data) != size:
                    raise JsonlChainUnreadable("history source read ended before its captured boundary")
                parts.append(data)
                start += size
        return b"".join(parts)

    def _entries(self, start, end, gaps):
        data = self._read(start, end)
        position = start
        stream = self.source
        is_live_end = bool(self.snapshot["entries"] and self.snapshot["entries"][-1][2]
                           and end == self.upper)

        class Lines:
            row_start = row_end = start

            def __iter__(self):
                nonlocal position
                for raw in io.BytesIO(data):
                    self.row_start, self.row_end = position, position + len(raw)
                    position = self.row_end
                    if not raw.endswith(b"\n") and is_live_end:
                        gaps.add("incomplete_live_line")
                        continue
                    yield raw

        lines = Lines()
        rows = []
        for entry in _helpers.iter_jsonl_objects(self.path, _handle=lines, gap_reasons=gaps):
            rows.append({**entry, "history_id": f"{stream}:{lines.row_start}",
                         "history_position": {"source": stream, "offset": lines.row_start},
                         "_history_end": lines.row_end})
        return rows

    def _segment_range(self, index):
        return (self.snapshot["ends"][index - 1] if index else 0,
                min(self.snapshot["ends"][index], self.upper))

    def _aligned_start(self, start, end, base):
        if start <= base or self._read(start - 1, start) == b"\n":
            return start
        data = self._read(start, end)
        newline = data.find(b"\n")
        return end if newline < 0 else start + newline + 1

    def recent(self, want, counts):
        """The existing recent byte-window and three-archive selection, with ids."""
        entries, gaps, before, archive_count = [], set(), self.upper, 0
        for index in reversed(range(len(self.snapshot["entries"]))):
            base, end = self._segment_range(index)
            if base >= self.upper:
                continue
            was_live = self.snapshot["entries"][index][2]
            if not was_live:
                if sum(map(counts, entries)) >= want or archive_count >= 3:
                    break
                archive_count += 1
            window = _TAIL_WINDOW_START_BYTES if was_live else end - base
            while True:
                start = self._aligned_start(max(base, end - window), end, base)
                selected = self._entries(start, end, gaps)
                if start == base or sum(map(counts, selected)) >= want:
                    break
                window *= 2
            entries = selected + entries
            before = start
        return entries, before, gaps

    def older(self, before, want, counts):
        """Consume one backward page; foreign/invalid bytes advance the position."""
        if want <= 0:
            return [], before, set()
        selected, gaps, counted, scanned, scanned_rows = [], set(), 0, 0, 0
        while before > 0 and scanned < _PAGE_SCAN_BYTES and scanned_rows < _PAGE_SCAN_ROWS:
            index = bisect_left(self.snapshot["ends"], before)
            base, _end = self._segment_range(index)
            window = _READ_BYTES
            while True:
                start = self._aligned_start(max(base, before - window), before, base)
                if start < before or start == base:
                    break
                window *= 2  # one large JSONL row keeps its existing support
            entries = self._entries(start, before, gaps)
            for entry in reversed(entries):
                selected.append(entry)
                scanned += before - entry["history_position"]["offset"]
                before = entry["history_position"]["offset"]
                scanned_rows += 1
                counted += bool(counts(entry))
                if counted >= want or scanned >= _PAGE_SCAN_BYTES or scanned_rows >= _PAGE_SCAN_ROWS:
                    return list(reversed(selected)), before, gaps
            scanned += before - start
            before = start
        return list(reversed(selected)), before, gaps

    def replay(self, lower, before):
        gaps, rows = set(), []
        for index in range(len(self.snapshot["entries"])):
            base, end = self._segment_range(index)
            start, end = max(base, lower), min(end, before)
            if start < end:
                rows.extend(self._entries(start, end, gaps))
        return rows, lower, gaps


def select_history_page(data_dir, thread_id, view, cursor, quotas, predicates, caps):
    continuation = decode_cursor(cursor, thread_id, view) if cursor else None
    if continuation:
        quotas = continuation["quotas"]
        if any(quotas[key] > caps[key] for key in quotas):
            raise HistoryCursorError("history_cursor_invalid", 400)
    recent = not continuation or (continuation["kind"] == "page" and continuation["recent"])
    selections, upper, before, page_ends = {}, {}, {}, {}
    paths = {"chat": data_dir / "logs" / "chat.jsonl", "progress": data_dir / "logs" / "progress.jsonl"}
    for source, quota in (("chat", "human"), ("progress", "progress")):
        try:
            reader = HistorySource(paths[source], source,
                                   continuation["upper"][source] if continuation else None)
            upper[source] = reader.upper
            page_ends[source] = continuation["before"][source] if continuation else reader.upper
            if continuation and continuation["kind"] == "page":
                selections[source] = reader.replay(continuation["lower"][source], page_ends[source])
            elif continuation:
                selections[source] = reader.older(page_ends[source], quotas[quota], predicates[source])
            else:
                selections[source] = reader.recent(quotas[quota], predicates[source])
        except OSError:
            if continuation:
                raise
            # The existing recent collector can still show readable rows. An
            # unknown chain prefix cannot establish global offsets or EOF.
            selections[source] = (None, 0, {"source_unavailable"})
        before[source] = selections[source][1]
    return {"v": 1, "chat_id": thread_id, "view": view, "upper": upper, "quotas": quotas,
            "recent": recent, "selections": selections, "before": before, "page_ends": page_ends}


def history_page_tokens(page):
    if any(selection[0] is None for selection in page["selections"].values()):
        return {"has_more": True, "next_cursor": None, "page_cursor": None,
                "reason_code": "history_source_unavailable"}
    state = {key: page[key] for key in ("v", "chat_id", "view", "upper", "quotas")}
    before = {source: position if page["quotas"]["human" if source == "chat" else source] else 0
              for source, position in page["before"].items()}
    has_more = any(before.values())
    return {
        "has_more": has_more,
        "next_cursor": encode_cursor({**state, "kind": "older", "before": before}) if has_more else None,
        "page_cursor": encode_cursor({**state, "kind": "page", "before": page["page_ends"],
                                      "lower": {source: value[1] for source, value in page["selections"].items()},
                                      "recent": page["recent"]}),
    }


def projected_history_ids(messages):
    """Folded attempts still account for the exact physical rows they contain."""
    ids = set()
    for message in messages:
        if message.get("history_id"):
            ids.add(message["history_id"])
        group = message.get("review_group")
        if isinstance(group, dict):
            ids.update(row["history_id"] for row in group.get("attempts", []) if row.get("history_id"))
    return ids


def deferred_before(source, entries, candidates, messages, before):
    """Do not advance a recent cursor beyond any quota-deferred physical row."""
    if entries is None:
        return before
    deferred = projected_history_ids(candidates) - projected_history_ids(messages)
    references = {(row.get("surface"), row.get("presentation_owner_task_id") or row.get("task_id"))
                  for row in messages if row.get("system_type") == "review_reference"}
    for row in candidates:
        if (row.get("system_type") == "review_reference"
                and (row.get("surface"), row.get("presentation_owner_task_id") or row.get("task_id")) in references):
            # Older invalidations of one already-present review owner are
            # intentionally folded by the existing projection, not quota loss.
            deferred.discard(row.get("history_id"))
    return max([before, *(entry["_history_end"] for entry in entries
                          if entry.get("history_id") in deferred
                          and entry["history_position"]["source"] == source)])


def replay_evidence_rows(messages, evidence):
    """Carry cross-page evidence only when this page has no equivalent fact."""
    visible = {row.get("history_id") for row in messages if row.get("history_id")}
    answered = {(row.get("task_id"), row["quiz"].get("quiz_id")) for row in messages
                if row.get("msg_type") == "quiz" and row["quiz"].get("state") == "answered"}
    terminals = {row["task_id"]: row["historical_terminal"] for row in messages
                 if row.get("task_id") and row.get("historical_terminal")}
    return [row for row in evidence if row.get("history_id") not in visible
            and not (row.get("system_type") == "quiz_answer"
                     and (row.get("task_id"), row["quiz"].get("quiz_id")) in answered)
            and not (row.get("historical_terminal")
                     and terminals.get(row.get("task_id")) == row["historical_terminal"])]
