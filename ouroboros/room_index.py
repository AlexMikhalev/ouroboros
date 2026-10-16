"""Where each row of the chat and progress chains lies and whose it can be.

``state/room_index.sqlite`` is a derived, disposable index beside the canonical JSONL chains
(``logs/chat.jsonl``, ``logs/progress.jsonl`` and their ``archive/`` segments, which stay the only
authority: BIBLE P1, P7 "a rebuildable index ... is not a second source of truth"). For every
complete row it keeps the row's chain coordinate (the ``<stream>:<offset>`` a history row is
named by), its length and the immutable facts a room is chosen by: the stored ``chat_id``, the
row's ``task_id``/``parent_task_id``/``root_task_id``, its ``type`` and, for an owner row, its
origin identity. A room read asks for the room's candidate rows (``candidates``) -- a superset
the existing ``project_dialogue.room_membership`` rule still decides -- and reads exactly those.

It is filled only by reading the canonical files, so a writer that bypasses any seam (a worker
process, a tool) is indexed all the same. The server's ``keeper`` (on every door, with or without
a provider) follows the chains: ``catch_up`` indexes what was appended or rotated since its last
pass, ``backfill`` the closed archives never indexed, newest first -- in the follower itself while
they are few, in a maintenance child (``python -m ouroboros.room_index``) otherwise. A history
read never writes it: it reads the indexed part through it, the bytes after its frontier straight
from the file, and names closed archives not yet indexed as a gap. Segment
geometry is kept, so a coordinate never shifts; a shrunken or replaced segment, a damaged file
or a schema change deletes the index and builds it again (one writer at a time holds
``state/room_index.lock``; readers open the file read-only).
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)
INDEX_REL = pathlib.Path("state/room_index.sqlite")
LOCK_REL = pathlib.Path("state/room_index.lock")
STREAMS = ("chat", "progress")
SCHEMA_VERSION = 2  # a changed schema deletes the index and builds it again
_BATCH_ROWS = 2000  # rows per INSERT batch inside one indexing transaction
_SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
  stream TEXT NOT NULL, base INTEGER NOT NULL, name TEXT NOT NULL, dev INTEGER, ino INTEGER,
  size INTEGER NOT NULL, indexed INTEGER NOT NULL, live INTEGER NOT NULL, PRIMARY KEY (stream, base));
CREATE TABLE IF NOT EXISTS rows (
  stream TEXT NOT NULL, offset INTEGER NOT NULL, length INTEGER NOT NULL, chat_id INTEGER,
  task_id TEXT, parent_id TEXT, root_id TEXT, kind TEXT, origin TEXT, bad INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (stream, offset)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS rows_chat ON rows(stream, chat_id, offset);
CREATE INDEX IF NOT EXISTS rows_task ON rows(stream, task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS rows_parent ON rows(stream, parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS rows_root ON rows(stream, root_id) WHERE root_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS rows_origin ON rows(stream, origin) WHERE origin IS NOT NULL;
CREATE INDEX IF NOT EXISTS rows_bad ON rows(stream, offset) WHERE bad = 1;
"""


class IndexUnavailable(RuntimeError):
    """The index cannot answer now (absent, being rebuilt, unreadable): the reader names a gap."""


def index_path(root: Any) -> pathlib.Path:
    return pathlib.Path(root) / INDEX_REL


def _stream_path(root: pathlib.Path, stream: str) -> pathlib.Path:
    return root / "logs" / f"{stream}.jsonl"


def parse_row(raw: bytes) -> Tuple[Optional[Dict[str, Any]], bool]:
    """``(row, bad)`` of one stored line as history parses it (``utils.iter_jsonl_objects``): a blank
    line is nothing, undecodable bytes are replaced, and a line that is not a JSON object is
    ``bad`` -- whose it is cannot be known, so every room's read takes it and names the gap."""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None, False
    try:
        row = json.loads(line)
    except ValueError:
        return None, True
    return (row, False) if isinstance(row, dict) else (None, True)


def row_facts(row: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """The immutable room facts of one stored row: chat id, task/parent/root ids, type, origin."""
    from ouroboros.project_dialogue import _text_sha256

    try:
        chat = int(row["chat_id"]) if row.get("chat_id") not in (None, "") else None
    except (TypeError, ValueError):
        chat = None

    def text(key: str) -> Optional[str]:
        value = str(row.get(key) or "").strip()
        return value or None

    origin = (f"{chat if chat is not None else 1}:{_text_sha256(row.get('text'))}"
              if str(row.get("direction") or "") == "in" else None)
    return chat, text("task_id"), text("parent_task_id"), text("root_task_id"), text("type"), origin


# -- writing ---------------------------------------------------------------------------------------

@contextlib.contextmanager
def _writer(root: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """The one writer: the lock file, the schema, and a fresh file when the old one is unusable."""
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    lock = root / LOCK_REL
    fd = acquire_exclusive_file_lock(lock, timeout_sec=10, stale_sec=120, owner_aware_stale=True)
    if fd is None:
        raise IndexUnavailable("room index writer lock unavailable")
    db = None
    try:
        path = index_path(root)
        try:
            db = sqlite3.connect(path, timeout=10)
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, SCHEMA_VERSION):
                raise sqlite3.DatabaseError("room index schema changed")
            db.executescript(_SCHEMA)
        except sqlite3.DatabaseError:
            if db is not None:
                db.close()
            path.unlink(missing_ok=True)  # only an index: rebuild it from the authority
            db = sqlite3.connect(path, timeout=10)
            db.executescript(_SCHEMA)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        # Disposable: a commit lost to a power cut leaves rows and their segment's progress out
        # together, and a damaged file is rebuilt (above), so the index never pays a disk sync.
        db.execute("PRAGMA synchronous = OFF")
        yield db
    finally:
        if db is not None:
            db.close()
        release_exclusive_file_lock(lock, fd)


def _reset(db: sqlite3.Connection, stream: str) -> None:
    db.execute("DELETE FROM rows WHERE stream=?", (stream,))
    db.execute("DELETE FROM segments WHERE stream=?", (stream,))


def _sync_segments(db: sqlite3.Connection, root: pathlib.Path, stream: str):
    """Record the chain's current geometry; returns the chain snapshot and its segment rows.

    A rotation renames the live file into ``archive/`` keeping its inode and base, so the
    recorded live segment becomes that archive; a copied data directory keeps names and sizes.
    A recorded segment that vanished, shrank or became another file means the coordinates no
    longer name the same bytes: the stream is indexed again."""
    from ouroboros.jsonl_tail import JsonlChainSnapshot

    chain = JsonlChainSnapshot(_stream_path(root, stream))  # strict: never index a partial chain
    current = {}
    for index, (path, stat, live) in enumerate(chain.entries):
        base = chain.ends[index - 1] if index else 0
        current[base] = (path.name, stat.st_dev, stat.st_ino, stat.st_size, int(bool(live)))
    known = {record[0]: record[1:] for record in db.execute(
        "SELECT base, name, dev, ino, size, indexed, live FROM segments WHERE stream=?", (stream,))}
    for base, (name, dev, ino, size, indexed, live) in list(known.items()):
        now = current.get(base)
        if live and now is not None and now[0] == name and (
                now[3] < indexed or (indexed and not _still_reads(db, chain, stream, base + indexed))):
            # The live file replaced by another one under its name (a restore): only its rows go.
            db.execute("DELETE FROM rows WHERE stream=? AND offset >= ?", (stream, base))
            db.execute("UPDATE segments SET indexed=0 WHERE stream=? AND base=?", (stream, base))
            known[base] = (name, dev, ino, size, 0, live)
        elif now is None or now[3] < (indexed if live else size) or (now[0] != name and (now[1], now[2]) != (dev, ino)):
            _reset(db, stream)
            known = {}
            break
    for base, (name, dev, ino, size, live) in current.items():
        if base in known:
            if (name, dev, ino, size, live) != (*known[base][:4], known[base][5]):
                db.execute("UPDATE segments SET name=?, dev=?, ino=?, size=?, live=? WHERE stream=? AND base=?",
                           (name, dev, ino, size, live, stream, base))
        else:
            db.execute("INSERT INTO segments (stream, base, name, dev, ino, size, indexed, live) "
                       "VALUES (?,?,?,?,?,?,0,?)", (stream, base, name, dev, ino, size, live))
    rows = list(db.execute("SELECT base, name, size, indexed, live FROM segments WHERE stream=? ORDER BY base",
                           (stream,)))
    return chain, rows


def _still_reads(db: sqlite3.Connection, chain: Any, stream: str, end: int) -> bool:
    """Whether the newest indexed row below ``end`` still reads as indexed: a live file replaced
    by another one (a restore) keeps its name and may keep growing past what was indexed."""
    last = db.execute("SELECT offset, length, chat_id, task_id, parent_id, root_id, kind, origin FROM rows "
                      "WHERE stream=? AND offset < ? AND bad = 0 ORDER BY offset DESC LIMIT 1", (stream, end)).fetchone()
    if last is None:
        return True
    try:
        row, _bad = parse_row(chain._read(last[0], last[0] + last[1]))
    except Exception:  # noqa: BLE001 -- unreadable where the index says: not the same file
        return False
    return row is not None and tuple(row_facts(row)) == tuple(last[2:])


def _scan(chain: Any, index: int, lower: int) -> Tuple[List[Tuple[int, int, Optional[Dict[str, Any]]]], int]:
    """``([(offset, length, row)], end)``: one segment's complete lines from ``lower`` (``row`` None
    for a bad one, ``parse_row``); an unfinished live line stops the scan (its writer owns it)."""
    base, end = chain.segment(index)
    start = max(base, lower)
    if start >= end:
        return [], start
    found, position = [], start
    for raw in io.BytesIO(chain._read(start, end)):  # lines end at "\n" only, as every chain reader splits them
        if not raw.endswith(b"\n") and chain.entries[index][2]:
            break
        row, bad = parse_row(raw)
        if row is not None or bad:
            found.append((position, len(raw), row))
        position += len(raw)
    return found, position


def _index_segment(db: sqlite3.Connection, chain: Any, stream: str, base: int, indexed: int) -> int:
    """Index one segment's complete rows from ``indexed``; returns its new indexed length."""
    ends = [chain.ends[index - 1] if index else 0 for index in range(len(chain.ends))]
    if base not in ends:
        return indexed
    found, end = _scan(chain, ends.index(base), base + indexed)
    for start in range(0, len(found), _BATCH_ROWS):
        db.executemany("INSERT OR REPLACE INTO rows (stream, offset, length, chat_id, task_id, parent_id, root_id, "
                       "kind, origin, bad) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       [(stream, offset, length, *(row_facts(row) if row is not None else (None,) * 6), int(row is None))
                        for offset, length, row in found[start:start + _BATCH_ROWS]])
    db.execute("UPDATE segments SET indexed=? WHERE stream=? AND base=?", (end - base, stream, base))
    return end - base


def catch_up(root: Any) -> Dict[str, Any]:
    """Index what was appended to the live files (and the segments rotated since the last pass);
    ``pending`` names the source bytes of closed archives still waiting for ``backfill``."""
    root = pathlib.Path(root)
    report: Dict[str, Any] = {"pending": 0}
    with _writer(root) as db:
        for stream in STREAMS:
            chain, segments = _sync_segments(db, root, stream)
            for base, _name, size, indexed, live in segments:
                # The live file and a segment begun while live; never-indexed archives wait for backfill.
                if (live or 0 < indexed < size) and indexed < size:
                    report[stream] = _index_segment(db, chain, stream, base, indexed)
                elif not live and indexed < size:
                    report["pending"] += size - indexed
            db.commit()
    return report


_BACKFILL_BYTES = 16 * 1024 * 1024  # source bytes indexed per writer turn (the follower waits at most one)


def backfill(root: Any, *, stop: Optional[threading.Event] = None, pause: float = 0.0) -> Dict[str, Any]:
    """Index the closed archives not yet indexed, newest first (recent history is ready first),
    a few megabytes of source per transaction; ``pause`` between transactions lets the server's
    follower take the writer in turn."""
    root = pathlib.Path(root)
    done = {stream: 0 for stream in STREAMS}
    for stream in STREAMS:
        pending = True
        while pending and not (stop is not None and stop.is_set()):
            if done[stream] and pause:
                time.sleep(pause)
            with _writer(root) as db:
                chain, segments = _sync_segments(db, root, stream)
                todo = [(base, size, indexed) for base, _name, size, indexed, live in segments
                        if not live and indexed < size]
                budget = _BACKFILL_BYTES
                while todo and budget > 0:
                    base, size, indexed = todo.pop()
                    _index_segment(db, chain, stream, base, indexed)
                    done[stream] += 1
                    budget -= size - indexed
                pending = bool(todo)
                db.commit()
    return done


# -- reading ---------------------------------------------------------------------------------------

class RoomIndexView:
    """One read's view of the index: segment geometry and candidate queries (read-only)."""

    def __init__(self, root: Any) -> None:
        path = index_path(root)
        if not path.is_file():
            raise IndexUnavailable("room index not built yet")
        try:
            self.db = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, timeout=5)
            if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise IndexUnavailable("room index schema differs")
            self.segments = {stream: [] for stream in STREAMS}
            for stream, base, size, indexed, live in self.db.execute(
                    "SELECT stream, base, size, indexed, live FROM segments ORDER BY stream, base"):
                self.segments.setdefault(stream, []).append((base, size, indexed, bool(live)))
        except sqlite3.Error as exc:
            raise IndexUnavailable(f"room index unreadable: {exc}") from exc

    def close(self) -> None:
        self.db.close()

    def frontier(self, stream: str) -> Tuple[int, int]:
        """``(complete_from, tail_from)``: every row in [complete_from, tail_from) is indexed; the
        bytes from ``tail_from`` on (what the follower has not reached yet: the live file's new
        lines, a just-rotated segment's last ones) a reader takes from the file; bytes below
        ``complete_from`` lie in archives still waiting for backfill."""
        segments = self.segments.get(stream) or []
        position = len(segments)
        tail_from = segments[-1][0] + segments[-1][1] if segments else 0
        complete_from = None
        while position:  # the follower's own lag: the live segment, then a segment it began while live
            base, size, indexed, live = segments[position - 1]
            if not (live or 0 < indexed < size):
                break
            tail_from, position = base + indexed, position - 1
            if indexed:
                complete_from = base  # its indexed head is complete
                break
        complete_from = tail_from if complete_from is None else complete_from
        while position:
            base, size, indexed, live = segments[position - 1]
            if indexed < size:
                break  # a hole: it and everything below it wait for backfill
            complete_from, position = base, position - 1
        return complete_from, tail_from

    def candidates(self, stream: str, room: Dict[str, Any], *, before: int, lower: int = 0,
                   limit: int = 500) -> List[Tuple[int, int, int]]:
        """``[(offset, length, bad)]`` newest first in [lower, before): the room's possible rows."""
        if room.get("exclude_chats") is not None:
            query, params = _MAIN_CANDIDATES, (stream, before, lower, json.dumps(sorted(room["exclude_chats"])),
                                               json.dumps(sorted(room.get("pinned_kinds") or ())), limit)
        else:
            query, params = _CHAT_CANDIDATES, (stream, int(room["chat_id"]), before, lower,
                                               json.dumps(sorted(room.get("tasks") or ())),
                                               json.dumps(sorted(room.get("origins") or ())), limit)
        try:
            (text,) = self.db.execute(query, params).fetchone()
        except sqlite3.Error as exc:
            raise IndexUnavailable(f"room index query failed: {exc}") from exc
        return [tuple(pair) for pair in json.loads(text or "[]")]


# A chat's possible rows (``room_membership`` decides each): its own, every row of a task the room
# rule can place in it or that wrote to this chat, the owner rows its bindings name, and every bad
# line (``parse_row``). One indexed search per fact; the offset-ordered scan the planner would
# pick reads the whole stream.
_CHAT_CANDIDATES = """SELECT json_group_array(json_array(offset, length, bad)) FROM (
  SELECT offset, length, bad FROM rows INDEXED BY rows_chat WHERE stream=?1 AND chat_id=?2 AND offset<?3 AND offset>=?4
  UNION SELECT offset, length, bad FROM rows INDEXED BY rows_task WHERE stream=?1 AND offset<?3 AND offset>=?4
    AND task_id IN (SELECT value FROM json_each(?5) UNION SELECT task_id FROM rows INDEXED BY rows_chat
                    WHERE stream IN ('chat', 'progress') AND chat_id=?2 AND task_id IS NOT NULL)
  UNION SELECT offset, length, bad FROM rows INDEXED BY rows_parent WHERE stream=?1 AND offset<?3 AND offset>=?4
    AND parent_id IN (SELECT value FROM json_each(?5))
  UNION SELECT offset, length, bad FROM rows INDEXED BY rows_root WHERE stream=?1 AND offset<?3 AND offset>=?4
    AND root_id IN (SELECT value FROM json_each(?5))
  UNION SELECT offset, length, bad FROM rows INDEXED BY rows_origin WHERE stream=?1 AND offset<?3 AND offset>=?4
    AND origin IN (SELECT value FROM json_each(?6))
  UNION SELECT offset, length, bad FROM rows INDEXED BY rows_bad WHERE stream=?1 AND bad = 1 AND offset<?3 AND offset>=?4
  ORDER BY offset DESC LIMIT ?7)"""
# Main: every row not addressed to a Project, plus the pinned Project kinds it mirrors and every bad line.
_MAIN_CANDIDATES = """SELECT json_group_array(json_array(offset, length, bad)) FROM (SELECT offset, length, bad FROM rows
  WHERE stream=?1 AND offset<?2 AND offset>=?3 AND (chat_id IS NULL OR chat_id NOT IN (SELECT value FROM json_each(?4))
  OR kind IN (SELECT value FROM json_each(?5)) OR bad = 1) ORDER BY offset DESC LIMIT ?6)"""


_MAIN_PINNED_KINDS = ("quiz", "quiz_answer", "quiz_closed")  # a Project's questions Main mirrors (history)


# The tasks the room rule (``projects_registry.RoomResolver``) can place in a Project by its
# bindings: the bound tasks, the tasks rooted at one of them, and their descendants by the
# store's lineage. A task admitted in the Project's chat (the rule's last resort) has rows in that
# chat, and ``_CHAT_CANDIDATES`` admits every row of such a task.
_ROOM_TASKS = """WITH RECURSIVE tree(id) AS (
  SELECT value FROM json_each(?1)
  UNION SELECT task_id FROM tasks WHERE root_task_id IN (SELECT value FROM json_each(?1))
  UNION SELECT t.task_id FROM tasks t JOIN tree ON t.parent_task_id = tree.id)
SELECT json_group_array(id) FROM tree"""


def room_spec(root: Any, thread_id: int, project_chat_ids: Iterable[int], source_refs: Iterable[Dict[str, Any]],
              bindings: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """The candidate superset of one room (``project_dialogue.room_membership`` decides each row);
    None when it cannot be known now (the store unreadable): the room is read without the index.

    A Project: its own chat, every task the room rule can place in it and the owner rows its
    bindings name. Main, and every other room (history reads them through Main's lens): every
    row not addressed to a Project, plus the Project questions Main mirrors."""
    projects = set(project_chat_ids)
    if thread_id not in projects:
        return {"exclude_chats": projects, "pinned_kinds": _MAIN_PINNED_KINDS}
    from ouroboros.project_dialogue import _source_ref_identity
    from ouroboros.store import open_store

    bound = sorted(task for task, chat in (bindings or {}).items() if chat == thread_id)
    try:
        with open_store(root, write=False) as txn:
            (tasks,) = txn.conn.execute(_ROOM_TASKS, (json.dumps(bound),)).fetchone()
    except Exception:  # noqa: BLE001 -- an unknown superset must not hide rows: read without the index
        log.debug("room task lineage unavailable", exc_info=True)
        return None
    origins = {f"{key[0]}:{key[3]}" for ref in source_refs or () if isinstance(ref, dict)
               if (key := _source_ref_identity(ref)) is not None}
    return {"chat_id": thread_id, "tasks": set(json.loads(tasks or "[]")) | set(bound), "origins": origins}


class RoomRows:
    """One room's rows in one read: the history source's ``lens`` (frontier and candidates)."""

    def __init__(self, view: RoomIndexView, room: Dict[str, Any]) -> None:
        self.view, self.room = view, room

    def frontier(self, stream: str) -> Tuple[int, int]:
        return self.view.frontier(stream)

    def candidates(self, stream: str, *, before: int, lower: int = 0, limit: int = 500) -> List[Tuple[int, int]]:
        return self.view.candidates(stream, self.room, before=before, lower=lower, limit=limit)

    def close(self) -> None:
        self.view.close()


def room_rows(root: Any, room: Dict[str, Any]) -> Optional[RoomRows]:
    """The room's rows through the index, or None while it cannot answer (absent, rebuilding)."""
    try:
        return RoomRows(RoomIndexView(root), room)
    except IndexUnavailable:
        return None


# -- the keeper ------------------------------------------------------------------------------------

_FOLLOW_SEC = 1.0  # between follower passes: a reader takes the newer bytes from the file meanwhile
_REPORT_BYTES = 4096


def chain_stamp(root: Any) -> Tuple[Any, ...]:
    """What a pass would find changed: each live file's identity and size and the archive listing."""
    stamp = []
    for path in (*(_stream_path(pathlib.Path(root), stream) for stream in STREAMS), pathlib.Path(root) / "archive"):
        try:
            stat = os.stat(path)
            stamp.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


class RoomIndexKeeper:
    """The server's one index owner: a follower thread that catches the index up with the chains
    after every change it sees, and backfills the closed archives it has never indexed -- inline
    while they fit one writer turn, else in a supervised maintenance child (the server's GIL is not
    spent on history-sized work)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._lifespan = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backfill: Optional[threading.Thread] = None
        self._process = None

    def start(self, data_root: Any, repo_dir: Any, lifespan: Optional[threading.Event] = None) -> None:
        """Follow ``data_root`` until ``stop`` or the server lifespan's own stop (``lifespan``) is set."""
        with self._lock:
            if self._thread is not None or self._stopped.is_set():
                return
            self._lifespan = lifespan or self._lifespan
            self._thread = threading.Thread(target=self._follow, args=(pathlib.Path(data_root), pathlib.Path(repo_dir)),
                                            name="room-index", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        process = self._process
        if process is not None:
            self._signal(process)

    def live_children(self) -> list:
        """Exit census after Stop: the backfill child while it runs."""
        process = self._process
        return [process] if process is not None and process.poll() is None else []

    @staticmethod
    def _signal(process: Any) -> None:
        with contextlib.suppress(OSError):
            process.kill()  # its own process group: the signal reaches only our child

    def _follow(self, root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        seen = None
        while not (self._stopped.is_set() or self._lifespan.is_set()):
            stamp = chain_stamp(root)
            if stamp != seen:
                try:
                    pending = catch_up(root)["pending"]
                    seen = stamp
                    if pending and not (self._backfill is not None and self._backfill.is_alive()):
                        if pending <= _BACKFILL_BYTES:
                            backfill(root, stop=self._stopped)
                        else:
                            self._backfill = threading.Thread(target=self._run_child, args=(root, repo_dir),
                                                              name="room-index-backfill", daemon=True)
                            self._backfill.start()
                except Exception:  # noqa: BLE001 -- the next change retries; readers name the lag meanwhile
                    log.debug("room index pass failed", exc_info=True)
            self._stopped.wait(_FOLLOW_SEC)

    def _run_child(self, root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        from ouroboros.startup_historical_audit import finish_maintenance_job, spawn_maintenance_job

        started = time.monotonic()
        try:
            if not self._stopped.is_set():
                finish_maintenance_job(self, spawn_maintenance_job(root, repo_dir, "room_index"), root, started,
                                       _report_fields, _record)
        except Exception as exc:  # noqa: BLE001 -- recorded; the next change starts it again
            _record(root, phase="failed", exception_class=type(exc).__name__,
                    duration_seconds=time.monotonic() - started)
        finally:
            with self._lock:
                self._process = None


def _record(data_root: Any, phase: str, **facts: Any) -> None:
    from ouroboros.utils import append_jsonl, utc_now_iso

    append_jsonl(pathlib.Path(data_root) / "logs" / "supervisor.jsonl",
                 {"ts": utc_now_iso(), "type": "room_index_backfill", "phase": phase, **facts})


def _report_fields(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw) if len(raw) <= _REPORT_BYTES else None
    except (ValueError, UnicodeError):
        return {}
    if not isinstance(value, dict) or value.get("status") not in {"completed", "failed"}:
        return {}
    return {key: value[key] for key in ("status", *(f"{stream}_archives" for stream in STREAMS))
            if type(value.get(key)) in (int, str)}


keeper = RoomIndexKeeper()


def main(argv: Optional[List[str]] = None) -> int:
    """The backfill child: index the closed archives and report what it did as one JSON line."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m ouroboros.room_index")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--custody-session", default="")
    args = parser.parse_args(argv)
    os.environ["OUROBOROS_DATA_DIR"] = str(pathlib.Path(args.data_root).resolve())
    from ouroboros.process_custody import adopt_session_id, start_parent_lifeline

    adopt_session_id(args.custody_session)
    start_parent_lifeline(label="room_index")
    try:
        report = {"status": "completed",
                  **{f"{key}_archives": value for key, value in backfill(args.data_root, pause=0.1).items()}}
    except Exception as exc:  # the report names the failure; the next start resumes from the index
        report = {"status": "failed", "error": type(exc).__name__}
    sys.stdout.write(json.dumps(report) + "\n")
    sys.stdout.flush()
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
