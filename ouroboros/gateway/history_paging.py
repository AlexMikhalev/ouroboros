"""A room's own pages over the dialogue store (``state/dialog.sqlite``).

A page is counted in the room's rows, never in bytes of a shared chain: the
newest page holds the room's newest rows however far back they lie, and each
older page the next older ones (owner decisions 2026-10-05, DESIGN "History
edges"). The chat and progress streams keep separate quotas (``human``,
``progress``), each counted by its own predicate. A room's rows are addressed
(``RoomLens``): one indexed probe per fact of the room -- its chat, the lineage
columns of its bound tasks, the owner ids its bindings name -- so a page costs
the room's own rows, never another room's history (``dialogue_store.probe_rows``).
Coordinates are the chain coordinates the store keeps: every cursor an older
body or a client holds names the same rows. The archive import's frontier is a
fact: a page never claims rows below a segment the import has not reached
(``window.truncated_by`` names the stream ``<stream>_importing``), and the older
page that would cross it answers "still importing" (``StoreMigrating``, 503),
never "no older history".
"""

from __future__ import annotations

import base64
import hashlib
import json
import pathlib
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from ouroboros import dialogue_store as ds
from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.dialogue_positions import skipped_between
from ouroboros.project_dialogue import _ANNOTATIONS_NAME, MAIN_PINNED_ROW_TYPES, ORIGIN_ADDRESSED_NOTICE_TYPES
from ouroboros.store import StoreMigrating
from ouroboros.subagent_messages import CARD_ROW_PLACEMENTS, is_task_card_message, subagent_message_meta

_SOURCES = ("chat", "progress")
# One indexed probe page per room fact: a page's cost is the room's facts times this, never its history.
_PROBE_ROWS = 200
# Bound on the candidate rows one request decodes per stream; the cursor resumes where a read
# stopped, so it bounds latency, never what a room can reach.
_READ_CEILING_ROWS = 10_000
# Bound on what one ``after`` read replays per stream (coordinates are bytes): a window that fell
# further behind (a client offline for hours) takes a fresh recent page instead (``history_delta_reset``).
_DELTA_CEILING_BYTES = 2 * 1024 * 1024
# The newest-arrival search every recent Project read pays for, in candidate rows; a bound reached
# first is carried on by the chain's cursors (``latest_before``/``quiet``).
_ARRIVAL_SEARCH_ROWS = 200
# The coverage span's chain witness: the store's coordinates are durable, so one witness names every read.
_CHAIN_WITNESS = "dialog"
# Bound on an opaque cursor's length (a query parameter): an ``after`` cursor carries the versions
# of the tasks its window shows, and a window too large to name them reads its newest page instead.
_CURSOR_CHARS = 8192
# A projection that failed part-way: the rows after the failing one are missing
# from the response, so neither its coverage nor its newest arrival is known.
PROJECTION_FAILED = "projection_failed"
# A stream's page stopped at the archive import's frontier: older rows exist and are not readable yet.
IMPORTING = "importing"
# A frozen archive segment that changed under the import: its rows are a gap, never coverage.
UNIMPORTABLE = "segment_unimportable"
# The gap names the chain readers gave the lines the import did not store (``dialogue_store.SKIPPED_KINDS``).
_GAP_NAMES = {"invalid": "malformed_jsonl", "non_object": "invalid_jsonl_row"}
# Stored kinds chat.js shows as a bubble of their own whoever sent them: it
# renders them before a child's words are routed to its card, and their
# producers (message_bus ``send_photo`` ...) count every one.
_STANDALONE_KINDS = ("document", "links", "photo", "video", "quiz")


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
    """Fingerprint the existing room lens, without introducing membership state.

    Every room hashes every binding: a task's resolved room outranks the room its
    rows were written to, so a conversion anywhere can move rows out of this room."""
    project = thread_id in project_ids
    value = [thread_id, project, [] if project else sorted(project_ids), dict(bindings),
             sorted(source_refs, key=lambda row: json.dumps(row, sort_keys=True))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value, thread_id, view):
    try:
        if not isinstance(value, str) or len(value) > _CURSOR_CHARS:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(decoded)
        if not isinstance(cursor, dict) or cursor.get("v") != 1 or cursor.get("kind") not in {"older", "page", "after"}:
            raise ValueError
        for field in ("upper", "before"):
            if set(cursor[field]) != set(_SOURCES):
                raise ValueError
            if any(type(cursor[field][source]) is not int or cursor[field][source] < 0 for source in _SOURCES):
                raise ValueError
        if any(cursor["before"][source] > cursor["upper"][source] for source in _SOURCES):
            raise ValueError
        unfinished = cursor.setdefault("unfinished", [])
        if not isinstance(unfinished, list) or unfinished != sorted(set(unfinished) & set(_SOURCES)):
            raise ValueError
        if type(cursor.setdefault("quiet", False)) is not bool:  # see ``latest_arrival``
            raise ValueError
        if type(cursor.setdefault("notes", "")) is not str or len(cursor["notes"]) > 64:  # see ``notes_stamp``
            raise ValueError
        tasks = cursor.setdefault("tasks", {})  # see ``history_page_tokens``
        if not isinstance(tasks, dict) or any(type(seq) is not int or seq < 0 for seq in tasks.values()):
            raise ValueError
        if type(cursor.setdefault("newest", "")) is not str or len(cursor["newest"]) > 64:  # see ``latest_arrival``
            raise ValueError
        if type(cursor.setdefault("lifecycle", False)) is not bool:
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


def notes_stamp(data_dir):
    """The owner-message annotations file's identity and length, taken before a read uses them: an
    ``after`` read that finds them changed cannot bring the window's older owner rows up to date,
    so it refuses (``history_delta_reset``) and the window takes its newest page again."""
    try:
        stat = (pathlib.Path(data_dir) / "logs" / _ANNOTATIONS_NAME).stat()
    except OSError:
        return ""
    return f"{stat.st_ino}:{stat.st_size}"


class RoomLens(NamedTuple):
    """How one stream of a room is addressed in the store: one indexed probe per ``keys`` entry
    (a ``dialogue_store.probe_rows`` column and value) and, for the chat stream, the coordinates
    (``seqs``) of the owner rows a binding names without a client id, resolved by identity once per
    request. ``project_dialogue.room_membership`` decides each candidate; the lens is its superset."""

    keys: Tuple[Tuple[str, Any], ...]
    seqs: Tuple[int, ...] = ()


def import_floor(root: pathlib.Path, source: str) -> int:
    """The coordinate below which the archive import has not reached yet (0 when it has): the
    end of the newest pending segment. Rows below it are "still importing", never absent."""
    from ouroboros.dialogue_import import pending_segments

    return max((int(segment["base"]) + int(segment["size"]) for segment in pending_segments(root)
                if segment["stream"] == source), default=0)


class HistorySource:
    """One stream of one room, frozen at ``upper`` (the stream's end when None).

    ``recent``/``older`` page the room's rows newest first through the lens's probes until the
    quota of counted rows, the import frontier (``floor``) or the read ceiling; ``replay`` re-reads
    a frozen page by its coordinates. Every answer carries the coordinate the page reached and the
    gaps its range holds (lines the import skipped, a segment it could not import)."""

    def __init__(self, root: pathlib.Path, source: str, lens: RoomLens, upper=None):
        self.root, self.source, self.lens = pathlib.Path(root), source, lens
        end = ds.stream_end(self.root, source)
        self.upper = end if upper is None else int(upper)
        if not 0 <= self.upper <= end:
            raise HistoryCursorError("history_cursor_invalid", 400)  # a boundary this store never gave
        self.warm = None  # the room rule's lineage batch read (``RoomResolver.warm``), before rows are counted
        self.floor = import_floor(self.root, source)

    def _warmed(self, rows):
        """``rows``, after their tasks' lineage (and their ancestors') was read in batches."""
        if self.warm is not None and rows:
            self.warm({str(row.get(key) or "") for row in rows for key in ("task_id", "parent_task_id", "root_task_id")
                       if isinstance(row, dict)} - {""})
        return rows

    def _entries(self, rows):
        return self._warmed([{**row.row, "history_id": row.history_id,
                              "history_position": {"source": self.source, "offset": row.seq},
                              "_history_end": row.end} for row in rows])

    def _candidates(self, lower: int, before: int, *, newest_first: bool = True):
        """One probe page of the room's rows in ``[lower, before)`` plus the named owner rows the
        page's range covers (a short page covers the whole range)."""
        rows = ds.probe_rows(self.root, self.source, self.lens.keys, before=before, lower=lower,
                             limit=_PROBE_ROWS, newest_first=newest_first)
        if not self.lens.seqs:
            return rows
        found = {row.seq: row for row in rows}
        if len(rows) < _PROBE_ROWS:
            covered = (lower, before)
        else:
            covered = (rows[-1].seq, before) if newest_first else (lower, rows[-1].seq + 1)
        for seq in self.lens.seqs:
            if covered[0] <= seq < covered[1] and seq not in found:
                row = ds.row_at(self.root, self.source, seq)
                if row is not None:
                    found[seq] = row
        return sorted(found.values(), key=lambda row: row.seq, reverse=newest_first)

    def gaps(self, lower: int, upper: int) -> set:
        """The gap names of ``[lower, upper)``: the lines the import skipped there, a segment it
        could not import. A range the import has not reached is not a gap but "still importing"."""
        if lower >= upper:
            return set()
        coverage = ds.range_coverage(self.root, self.source, lower=lower, upper=upper)
        if coverage["pending"]:
            raise StoreMigrating("Dialogue history is still importing")
        names = {UNIMPORTABLE} if coverage["mismatch"] else set()
        if coverage["gaps"]:
            names.update(_GAP_NAMES.get(gap.kind, gap.kind) for gap in skipped_between(self.root, self.source, lower, upper))
        return names

    def recent(self, want, counts):
        """The room's newest rows: newest first until ``want`` counted rows, the import frontier or
        the read ceiling."""
        return self._page(self.upper, want, counts, _READ_CEILING_ROWS)

    def older(self, before, want, counts, ceiling=None):
        """Consume one backward page of ``want`` counted rows below ``before``; the read ceiling
        ends it early. At the import frontier nothing older is readable yet: the page is empty and
        stays at the frontier (the request's caller answers "still importing")."""
        if want <= 0:
            return [], before, set()
        if 0 < self.floor >= before:
            return [], self.floor, set()
        return self._page(before, want, counts, _READ_CEILING_ROWS if ceiling is None else ceiling)

    def _page(self, before, want, counts, ceiling):
        top, selected, counted, read = before, [], 0, 0
        while True:
            rows = self._candidates(self.floor, before)
            for entry in self._entries(rows):  # newest first
                selected.append(entry)
                before = entry["history_position"]["offset"]
                counted += bool(counts(entry))
                read += 1
                if counted >= want or read >= ceiling:
                    return list(reversed(selected)), before, self.gaps(before, top)
            if len(rows) < _PROBE_ROWS:  # the room's rows above the frontier are exhausted
                return list(reversed(selected)), self.floor, self.gaps(self.floor, top)

    def replay(self, lower, before):
        """Re-read one frozen page by its coordinates: the same room view, the same rows."""
        rows, start = [], lower
        while start < before:
            page = self._candidates(start, before, newest_first=False)
            rows.extend(page)
            if len(page) < _PROBE_ROWS:
                break
            start = page[-1].seq + 1
        return self._entries(rows), lower, self.gaps(lower, before)


def select_history_page(data_dir, thread_id, view, cursor, quotas, predicates, caps, lens, notes="", warm=None):
    """``lens``: the room's ``RoomLens`` per stream (``history._room_lens``); ``notes``:
    ``notes_stamp``; ``warm``: the room rule's lineage batch read, given rows before they are counted."""
    continuation = decode_cursor(cursor, thread_id, view) if cursor else None
    if continuation and continuation["kind"] == "after" and (
            continuation["notes"] != notes or _settled_since(data_dir, continuation["tasks"])):
        raise HistoryCursorError("history_delta_reset")
    if continuation:
        quotas = continuation["quotas"]
        if any(quotas[key] > caps[key] for key in quotas):
            raise HistoryCursorError("history_cursor_invalid", 400)
    recent = not continuation or (continuation["kind"] == "page" and continuation["recent"])
    after = bool(continuation) and continuation["kind"] == "after"
    selections, upper, before, page_ends, importing = {}, {}, {}, {}, []
    for source, quota in (("chat", "human"), ("progress", "progress")):
        # Rows an ``after`` read has not seen are everything stored after the window's last read,
        # so it freezes the stream fresh; every other read keeps the boundary its cursor froze.
        reader = HistorySource(data_dir, source, lens[source],
                               continuation["upper"][source] if continuation and not after else None)
        reader.warm = warm
        upper[source] = reader.upper
        page_ends[source] = continuation["before"][source] if continuation and not after else reader.upper
        if after:
            seen = continuation["upper"][source]
            if not seen <= reader.upper or reader.upper - seen > _DELTA_CEILING_BYTES:
                raise HistoryCursorError("history_delta_reset")
            selections[source] = reader.replay(seen, reader.upper)
            if selections[source][2]:  # rows it could not read stay unaccounted for
                raise HistoryCursorError("history_delta_reset")
        elif continuation and continuation["kind"] == "page":
            selections[source] = reader.replay(continuation["lower"][source], page_ends[source])
        elif continuation:
            selections[source] = reader.older(page_ends[source], quotas[quota], predicates[source])
        else:
            selections[source] = reader.recent(quotas[quota], predicates[source])
        before[source] = selections[source][1]
        if not after and 0 < reader.floor == before[source]:
            importing.append(source)
    if importing and not any(selections[source][0] for source in _SOURCES):
        raise StoreMigrating("Dialogue history is still importing")  # nothing on this page is readable yet
    return {"v": 1, "chat_id": thread_id, "view": view, "upper": upper, "unfinished": [],
            "quotas": quotas, "recent": recent, "after": after, "replayed": bool(continuation),
            "quiet": bool(continuation and continuation["quiet"]), "lens": lens, "notes": notes,
            "selections": selections, "before": before, "page_ends": page_ends, "importing": importing,
            **({key: continuation[key] for key in ("tasks", "newest", "lifecycle")} if after else {})}


def _settled_since(data_dir, tasks):
    """Whether a task the window shows changed since its read while it no longer runs: a question
    that closed, a late phase that settled. Its own rows cannot say so, so the window reads its
    newest page again; a running task's rows carry its facts. Unknown is a change."""
    if not tasks:
        return False
    from ouroboros import task_store
    from ouroboros.task_status import FINAL_STATUSES

    try:
        now = task_store.task_versions(data_dir, tasks)
    except Exception:
        return True
    for task_id, seen in tasks.items():
        if task_id not in now:
            return True
        seq, status, owes = now[task_id]
        if seq != seen and status in FINAL_STATUSES and not (owes and status in ("completed", "failed")):
            return True  # settled since (a cancelled record is terminal even while it owes its late phase)
    return False


def history_page_coverage(page, stream_gaps):
    """Delivered spans, called AFTER quota/lineage deferrals.

    A projected origin, detail overlay or shared row ID proves no delivered span.
    A quota-disabled stream is unknown, even when it emits [] and no continuation.
    Gaps (lines the import skipped, a segment it could not import) remain part of
    the coverage evidence. The chain witness is one constant: the store's coordinates
    are durable, so every read names the same chain.
    """
    return {"v": 1, "view": page["view"], "upper": dict(page["upper"]), "spans": {
        source: ({"from": page["before"][source], "to": page["page_ends"][source],
                  "chain": _CHAIN_WITNESS, "gaps": sorted(stream_gaps[source])}
                 if page["quotas"]["human" if source == "chat" else source] else None)
        for source in _SOURCES}}


def history_page_tokens(page, messages=(), reads=None):
    """The conversation decides whether older history remains: narration (progress)
    keeps paging alongside it but never keeps a press alive on its own, so no press
    lands narration alone. A narration-only read (no human quota) pages narration.

    The ``after`` cursor also carries what its window shows besides rows: the version of each
    task the projection read (``reads``, the page's ``TaskReads``), the newest message's time and
    whether a running skill operation was shown. A later ``after`` read refuses when a shown task
    settled or changed without running, or the operation is gone (``history_delta_reset``)."""
    state = {key: page[key] for key in ("v", "chat_id", "view", "upper", "unfinished", "quotas")}
    before = {source: position if page["quotas"]["human" if source == "chat" else source] else 0
              for source, position in page["before"].items()}
    has_more = bool(before["chat"]) if page["quotas"]["human"] else any(before.values())
    versions, overlaid = getattr(reads, "versions", {}), getattr(reads, "overlaid", set)
    # Tasks whose facts the rows show: not an owner's row, a notice naming another room's task or a pointer.
    shown = {str(row.get("task_id")) for row in messages if row.get("task_id") and row.get("role") != "user"
             and row.get("system_type") not in ("project_question_pointer", *ORIGIN_ADDRESSED_NOTICE_TYPES,
                                                *MAIN_PINNED_ROW_TYPES)}
    lifecycle = any(row.get("lifecycle_virtual") for row in messages)
    if page.get("after") and page.get("lifecycle") and not lifecycle:
        raise HistoryCursorError("history_delta_reset")  # the shown operation ended: nothing here says so
    arrivals = {**state, "kind": "after", "before": dict(page["upper"]), "quiet": False, "notes": page["notes"],
                "tasks": {**page.get("tasks", {}),
                          **{tid: versions[tid] for tid in sorted(overlaid(shown)) if tid in versions}},
                "newest": page.get("newest", ""), "lifecycle": lifecycle}
    after_cursor = encode_cursor(arrivals)
    after_cursor = after_cursor if len(after_cursor) <= _CURSOR_CHARS else None
    if page.get("after"):  # a read of what arrived after the window's last read: it never pages older
        if after_cursor is None:
            raise HistoryCursorError("history_delta_reset")
        return {"has_more": False, "next_cursor": None, "page_cursor": None, "after_cursor": after_cursor}
    return {
        "has_more": has_more,
        "next_cursor": encode_cursor({**state, "kind": "older", "before": before,
                                      "quiet": page.get("quiet_below", False)}) if has_more else None,
        "page_cursor": encode_cursor({**state, "kind": "page", "before": page["page_ends"], "quiet": page["quiet"],
                                      "lower": {source: value[1] for source, value in page["selections"].items()},
                                      "recent": page["recent"]}),
        # The newest page names where the next read of new arrivals starts.
        **({"after_cursor": after_cursor} if after_cursor and page["recent"] and not page["replayed"] else {}),
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


def _card_content(row, kind, child=lambda _row: False):
    """Task-card content: a host placement, or a child's own words (``child``: by the
    lineage its task result recovers). What a child delivers stands alone, and so does
    a routing notice: its task_id names the task it refused, never its speaker."""
    return row.get("card_row") in CARD_ROW_PLACEMENTS or (
        kind not in _STANDALONE_KINDS and kind not in ORIGIN_ADDRESSED_NOTICE_TYPES
        and (is_task_card_message(row) or child(row)))


def _feed_message(row):
    """A standalone conversation row with a physical chat position.

    Progress, a folded review, a task summary and task-card content change a
    card, not the conversation; a skill review is appended beside it and never
    counts as unread."""
    position = row.get("history_position")
    kind = str(row.get("system_type") or "")  # the stored ``type``
    return (isinstance(position, dict) and position.get("source") == "chat"
            and not row.get("is_progress") and not isinstance(row.get("review_group"), dict)
            and kind not in ("task_summary", "skill_review") and not _card_content(row, kind))


def _skipped_row_after(root, after, upper):
    """Whether a line the import did not store lies at or after ``after`` in the chat stream."""
    return bool(skipped_between(root, "chat", after, upper))


def _stored_message(row_matches_thread, stored_chat_id, child):
    """A stored chat row the history projection turns into a standalone message."""
    def message(entry):
        kind = str(entry.get("type") or "")
        return (str(entry.get("direction") or "").lower() in ("out", "system")
                and kind not in ("routing_options", "quiz_answer", "quiz_closed", "task_summary", "skill_review")
                and not is_a2a_chat_id(entry.get("chat_id", 1))
                and (str(entry.get("text") or "").strip() not in ("", "\u200b") or kind in _STANDALONE_KINDS)
                and row_matches_thread(stored_chat_id(entry.get("chat_id"), 1), entry)
                and not _card_content(entry, kind, child))  # last: it may read the row's task result
    return message


def latest_arrival(rows, page, data_dir, row_matches_thread, stored_chat_id, projected_gaps=frozenset(),
                   task_result=lambda _task_id: {}):
    """``window.latest_message`` of a recent Project read (DESIGN "Project unread dot").

    The window is ordered and tailed by event time, but a Project message counts
    as unread when it ARRIVES: an answer the terminal outbox delivers late keeps
    its original ``ts``, so it can sort above messages that arrived before it
    (``out_of_order``) or below the window's floor, where the first older page
    (``deferred_before``) shows it. Reading the room means reading the standalone
    message that arrived last, named from the persisted rows before the tail and
    annotation rewrite them. A child's words are card content by their stored
    lineage or, on rows written before rows carried it, by the lineage their
    ``task_result`` or this page's progress recovers — the projection's own authority, which shows such a
    row in the child's card; a photo, video, file, link card or question a child
    delivers is shown alone and is a message. ``None``: its arrival is unknown — its
    projection failed part-way (``projected_gaps`` holds ``PROJECTION_FAILED``: any row
    after the fault may be the newest) or a line the import could not store lies after
    the newest readable row — and on a replayed recent page, which cannot see what
    arrived after its frozen boundary.

    Card rows, a child's words and the owner's own messages can fill the recent
    selection's quota. One bounded older read of the room (``_ARRIVAL_SEARCH_ROWS``)
    then names the newest stored message before it — ``out_of_order``, since this
    read cannot place it relative to the bottom — or proves the chat holds none
    (absent); an unreadable row reached first leaves the arrival unknown. Its
    bound reached first leaves it unknown too, but searched: ``latest_before``
    says none lies at or after that offset, and the chain's cursors carry that
    fact (``quiet``: none at or after the cursor's ``before``). An older page of
    a quiet chain names its own newest message — then the newest below the
    chain's frozen ``upper`` — or, holding none, passes the fact on, and the one
    that reaches the start of the chat without a gap proves the chat holds none
    below that ``upper`` (``latest_absent``); any other older page names nothing.
    A legacy child's lineage may first appear on another page. This bounded
    read can then name its final before the client places it in a child card;
    that unavailable standalone node remains unacknowledged, not guessed read.
    """
    quiet = page["quiet"]
    if page["replayed"] and page["recent"]:
        return {"latest_message": None}
    if not (page["recent"] or quiet or page["after"]):
        return {}
    entries, start, gaps = page["selections"]["chat"]
    end = page["page_ends"].get("chat", 0)
    # The client learns these facts before placing any row. A scheduling row's
    # task_id may name the parent; the shared projection names the actual child.
    known_children = {meta["subagent_task_id"] for row in rows if (meta := subagent_message_meta(row))}

    def child(row):
        task_id = str(row.get("task_id") or "")
        return bool(task_id) and (task_id in known_children
                                 or bool(subagent_message_meta(task_result(task_id), task_id=task_id)))

    offset = lambda row: row["history_position"]["offset"]  # noqa: E731
    words = lambda row: _card_content(row, str(row.get("system_type") or ""), child)  # noqa: E731
    feed = [row for row in rows if _feed_message(row)]
    spoken = sorted((row for row in feed if row.get("role") != "user" and (
        str(row.get("text") or "").strip() not in ("", "\u200b") or row.get("msg_type"))), key=offset)
    latest = next((row for row in reversed(spoken) if not words(row)), None)
    # The newest time a message this read shows was sent: a later arrival sent before it sorts above
    # it, which the window's next ``after`` read can only tell from its cursor.
    newest = next((str(row.get("ts") or "") for row in sorted(feed, key=lambda row: str(row.get("ts") or ""),
                                                              reverse=True) if not words(row)), "")
    shown = page.get("newest", "")
    page["newest"] = max(shown, newest) if page["after"] else newest
    if PROJECTION_FAILED in projected_gaps or (
            gaps and _skipped_row_after(data_dir, offset(latest) if latest else start, end)):
        return {"latest_message": None}
    if page["after"]:  # what arrived since the window's last read: its newest message, if one did
        return {"latest_message": {"history_id": latest["history_id"], "out_of_order": str(latest.get("ts") or "") < shown
                                   or any(offset(row) < offset(latest) and str(row.get("ts") or "") > str(latest.get("ts") or "")
                                          and not words(row) for row in feed)}} if latest else {}
    if not page["recent"]:
        page["quiet_below"] = latest is None
        if latest:
            return {"latest_message": {"history_id": latest["history_id"], "out_of_order": True}}
        return {"latest_absent": True} if start == 0 and not gaps else {}
    if latest is None and start > 0:
        message = _stored_message(row_matches_thread, stored_chat_id, child)
        entries, before, gaps = HistorySource(data_dir, "chat", page["lens"]["chat"], end).older(
            start, 1, message, min(_ARRIVAL_SEARCH_ROWS, _READ_CEILING_ROWS))
        found = next(filter(message, entries), None)  # ``older`` stops at the newest one
        if gaps and _skipped_row_after(data_dir, offset(found) if found else before, start):
            return {"latest_message": None}
        if not found and before > 0:
            page["quiet_below"] = True
            return {"latest_message": None, "latest_before": before}
        return {"latest_message": {"history_id": found["history_id"], "out_of_order": True}} if found else {}
    if latest is None:
        return {}
    ts = str(latest.get("ts") or "")
    return {"latest_message": {"history_id": latest["history_id"], "out_of_order": any(
        offset(row) < offset(latest) and str(row.get("ts") or "") > ts and not words(row) for row in feed)}}


def replay_evidence_rows(messages, evidence):
    """Carry cross-page evidence only when this page has no equivalent fact."""
    visible = {row.get("history_id") for row in messages if row.get("history_id")}
    settled = {(row.get("task_id"), row["quiz"].get("quiz_id"), row["quiz"].get("state")) for row in messages
               if row.get("msg_type") == "quiz" and row["quiz"].get("state") in {"answered", "closed"}}
    terminals = {row["task_id"]: row["historical_terminal"] for row in messages
                 if row.get("task_id") and row.get("historical_terminal")}
    return [row for row in evidence if row.get("history_id") not in visible
            and not (row.get("system_type") in {"quiz_answer", "quiz_closed"}
                     and (row.get("task_id"), row["quiz"].get("quiz_id"), row["quiz"].get("state")) in settled)
            and not (row.get("historical_terminal")
                     and terminals.get(row.get("task_id")) == row["historical_terminal"])]
