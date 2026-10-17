"""The dialogue in ``state/dialog.sqlite``: chat and progress rows, their rooms, the one writer seam.

Every chat and progress row of a data root lives here, in the exact JSON text its writer
produced (``body``), with the facts a reader addresses it by in typed columns: the stream,
its ``seq``, the stored ``chat_id``, the task lineage, the direction, the stored ``type``
(``kind``), the client message id and the owner message it originates from (``origin``).
``seq`` is the row's chain coordinate: the byte offset the row has (an imported row) or would
have had (an appended row) in the rotated ``logs/<stream>.jsonl`` chain, so every coordinate
an older body or a client holds -- a ``history_id`` ``<stream>:<offset>``, a page cursor, a
wake boundary, an export -- names the same row here. The two streams are paged separately,
so each has its own coordinate space (``PRIMARY KEY (stream, seq)``).

``append`` is the ONE writer seam: one ``BEGIN IMMEDIATE`` transaction writes the row, dedups
a named owner message through the partial UNIQUE ``(chat_id, client_message_id)`` (a duplicate
returns the existing row's receipt), moves the room's revision, last row and activity, and
records the dispatch obligation when the row needs one (``dispatch='pending'``). Workers write
through it directly; SQLite's busy wait (``WAIT_SEC``) is the whole retry budget, and nothing
here admits a zero-wait write. The obligation is drained by ``supervisor.message_ingress``:
``claim_dispatch`` is the durable "dispatch entered" mark (pending -> dispatched), a retry or
the recovery after a crash takes it exactly once, and ``complete_dispatch`` records the
handoff's return. ``dispatch`` is ``NULL`` for an imported or unnamed row, ``none`` for a named
acceptance that owes no bus dispatch, else ``pending`` / ``dispatched`` / ``completed``.

Readers are addressed queries: a room's candidate rows are the union of one indexed probe per
room fact (its chat, the tasks the room rule can place in it, the owner rows its bindings
name), each bounded by the page; Main probes the chats no Project owns. No query's cost grows
with another room's history. Coverage is a fact (``dialogue_import.coverage``): rows below a
segment the archive import has not reached yet are "still importing", never "no rows". The
store rides ``ouroboros.store``'s core (lock tier in the header, typed errors, lazy per-thread
connections, PERSIST+FULL or gated WAL); it is a separate file so the dialogue never waits in
``current.sqlite``'s import/export window or its task-body journal. The archive import, the
cutover, the rollback export and the frozen archive live in ``ouroboros.dialogue_import``.
"""

from __future__ import annotations

import contextlib
import heapq
import json
import logging
import os
import pathlib
import sqlite3
import uuid
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

from ouroboros import store
from ouroboros.store import StoreBusy, StoreError, StoreLost, StoreMigrating, TIER_ENFORCED, TIER_NAME
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

STORE_REL = pathlib.Path("state/dialog.sqlite")
LOCK_NAME = "dialog.lock"  # the name tier's one lock around every access
IMPORT_LOCK_NAME = "dialog_import.lock"  # creation, cutover, tier changes
SCHEMA_VERSION = 1
STREAMS = ("chat", "progress")
LIVE_REL = {"chat": pathlib.Path("logs/chat.jsonl"), "progress": pathlib.Path("logs/progress.jsonl")}  # the frozen chains
WAIT_SEC = store.STORE_WAIT_SEC
# The header's application id names the lock protocol before the file is opened ('OUDE'/'OUDN').
_APPLICATION_ID = {TIER_ENFORCED: 0x4F554445, TIER_NAME: 0x4F55444E}
DISPATCH_STATES = ("none", "pending", "dispatched", "completed", "abandoned")
# Every probe names its index (``INDEXED BY``): the plan is a fact of the schema, not the planner's
# estimate -- without statistics SQLite walks the primary key (every newer row of every room) instead.
_PROBE_INDEX = {"chat_id": "dialogue_room", "task_id": "dialogue_task", "parent_task_id": "dialogue_parent",
                "root_task_id": "dialogue_root", "origin": "dialogue_origin", "client_message_id": "dialogue_named",
                "kind": "dialogue_kind"}
_SCHEMA = """
CREATE TABLE dialogue (
  stream TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL DEFAULT '', chat_id INTEGER,
  task_id TEXT, parent_task_id TEXT, root_task_id TEXT, direction TEXT NOT NULL DEFAULT '', kind TEXT,
  client_message_id TEXT, origin TEXT, dispatch TEXT, dispatch_input TEXT, body TEXT NOT NULL,
  PRIMARY KEY (stream, seq)) WITHOUT ROWID;
CREATE INDEX dialogue_room ON dialogue(stream, chat_id, seq);
CREATE INDEX dialogue_room_ts ON dialogue(stream, chat_id, ts);
CREATE INDEX dialogue_task ON dialogue(stream, task_id, seq) WHERE task_id IS NOT NULL;
CREATE INDEX dialogue_parent ON dialogue(stream, parent_task_id, seq) WHERE parent_task_id IS NOT NULL;
CREATE INDEX dialogue_root ON dialogue(stream, root_task_id, seq) WHERE root_task_id IS NOT NULL;
CREATE INDEX dialogue_kind ON dialogue(stream, kind, seq) WHERE kind IS NOT NULL;
CREATE INDEX dialogue_named ON dialogue(stream, client_message_id, chat_id, seq) WHERE client_message_id IS NOT NULL;
CREATE INDEX dialogue_origin ON dialogue(stream, origin, seq) WHERE origin IS NOT NULL;
CREATE INDEX dialogue_inbound ON dialogue(stream, chat_id, seq) WHERE direction='in';
CREATE UNIQUE INDEX dialogue_accepted ON dialogue(chat_id, client_message_id)
  WHERE direction='in' AND client_message_id IS NOT NULL AND dispatch IS NOT NULL;
CREATE INDEX dialogue_pending ON dialogue(stream, seq) WHERE dispatch='pending';
CREATE TABLE streams (stream TEXT PRIMARY KEY, next_seq INTEGER NOT NULL, cutover_seq INTEGER, files_seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE rooms (chat_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, last_seq INTEGER,
  read_revision INTEGER NOT NULL DEFAULT 0, activity_at TEXT NOT NULL DEFAULT '');
CREATE TABLE legacy_addresses (gen TEXT NOT NULL, line INTEGER NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (gen, line)) WITHOUT ROWID;
CREATE TABLE import_ledger (stream TEXT NOT NULL, name TEXT NOT NULL, base INTEGER NOT NULL, size INTEGER NOT NULL,
  mtime_ns INTEGER, state TEXT NOT NULL, rows INTEGER, bad INTEGER, gen TEXT, imported_at TEXT, PRIMARY KEY (stream, name));
CREATE INDEX ledger_state ON import_ledger(stream, state, base);
CREATE INDEX ledger_gen ON import_ledger(gen) WHERE gen IS NOT NULL;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Row(NamedTuple):
    """One stored row: its coordinate, the decoded body and its dispatch fact."""

    stream: str
    seq: int
    row: Dict[str, Any]
    dispatch: Optional[str]

    @property
    def history_id(self) -> str:
        return f"{self.stream}:{self.seq}"


class Receipt(NamedTuple):
    """What ``append`` returns: the row's coordinate, the row as stored, whether an existing row
    answered (a duplicate named message) and the dispatch fact after the transaction."""

    stream: str
    seq: int
    row: Dict[str, Any]
    duplicate: bool
    dispatch: Optional[str]


# --- the file, its tier and its transactions -------------------------------------------------

def path(root: Any) -> pathlib.Path:
    return pathlib.Path(root) / STORE_REL


def present(root: Any) -> bool:
    return store.file_identity(path(root)) is not None


def encode(row: Dict[str, Any]) -> str:
    """The exact text a chain line held: ``append_jsonl``'s own encoding, without the newline."""
    return json.dumps(row, ensure_ascii=False)


def decode(body: str) -> Dict[str, Any]:
    value = json.loads(body)
    return value if isinstance(value, dict) else {}


def _typed(exc: sqlite3.Error) -> StoreError:
    if isinstance(exc, sqlite3.OperationalError) and store.busy(exc):
        error = StoreBusy(f"dialogue store busy: {exc}", reason="contention")
        if hasattr(exc, "rollback_confirmed"):
            error.rollback_confirmed = exc.rollback_confirmed
        return error
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(
            exc, (sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.ProgrammingError)):
        return StoreLost(f"dialogue store unreadable: {exc}")
    return StoreError(f"dialogue store unavailable: {exc}")


def probed_tier(root: Any) -> str:
    """The tier today's kernel enforces for this root (the boot re-probe, as ``current.sqlite``)."""
    from ouroboros.platform_layer import kernel_file_locks_enforced

    return TIER_ENFORCED if kernel_file_locks_enforced(pathlib.Path(root) / "state" / LOCK_NAME) else TIER_NAME


def header_tier(root: Any) -> Optional[str]:
    """The tier the file's header names; ``None`` for a missing or foreign file."""
    return store.header_tier(path(root), _APPLICATION_ID)


def _tier(root: pathlib.Path) -> str:
    tier = header_tier(root)
    if tier is None:
        if present(root):
            raise StoreLost(f"dialogue store unreadable or foreign, never replaced: {path(root)}")
        raise StoreMigrating(f"dialogue store not created yet: {path(root)}")
    return tier


def hand_off(root: Any) -> bool:
    """``store.hand_off`` for the dialogue store: a WAL file is checkpointed into DELETE mode before
    another body takes the root (a rollback-journal file is left untouched)."""
    target = path(root)
    if not store.wal_file(target):
        return False
    store.prepare_handoff(target, _tier(pathlib.Path(root)))  # WAL is never chosen on the name tier
    return True


def holds_dialogue_files(root: Any) -> bool:
    """Whether the root holds chain files the cutover would import (live rows or archive segments)."""
    root = pathlib.Path(root)
    for stream in STREAMS:
        try:
            if (root / LIVE_REL[stream]).stat().st_size:
                return True
        except OSError:
            pass
    try:
        with os.scandir(root / "archive") as entries:
            return any(entry.name.startswith(("chat_", "progress_")) and entry.name.endswith(".jsonl") for entry in entries)
    except OSError:
        return False


def create(root: Any, *, tier: Optional[str] = None) -> pathlib.Path:
    """The empty store, built as a sibling and published by rename (idempotent; under the import lock)."""
    root = pathlib.Path(root)
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with store.process_lock(target), store.name_lock(root, IMPORT_LOCK_NAME, timeout_sec=WAIT_SEC, stale_sec=600.0):
        if present(root):
            return target
        tier = tier or probed_tier(root)
        tmp = target.with_name(f"{target.name}.import-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        try:
            with contextlib.closing(store.connect(tmp, tier, create=True)) as conn:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA application_id = {_APPLICATION_ID[tier]}")
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany("INSERT INTO streams (stream, next_seq) VALUES (?, 0)", [(stream,) for stream in STREAMS])
                for key, value in (("schema_version", SCHEMA_VERSION), ("lock_tier", tier), ("created_at", utc_now_iso())):
                    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
                store.commit(conn)
            os.replace(tmp, target)
        except sqlite3.Error as exc:
            raise _typed(exc) from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        store.fsync_directory(target.parent)
    return target


def retier(root: Any) -> Optional[str]:
    """Bring the header to today's probed tier (the boot re-probe); returns the former tier when it changed."""
    root = pathlib.Path(root)
    wanted, current = probed_tier(root), header_tier(root)
    if current is None or current == wanted:
        return None
    with store.process_lock(path(root)), store.name_lock(root, IMPORT_LOCK_NAME, timeout_sec=WAIT_SEC, stale_sec=600.0):
        store.close_connections(path(root))
        try:
            with contextlib.closing(store.connect(path(root), current, wait_sec=WAIT_SEC)) as conn:
                conn.execute(f"PRAGMA application_id = {_APPLICATION_ID[wanted]}")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('lock_tier', ?)", (json.dumps(wanted),))
                store.commit(conn)
        except sqlite3.Error as exc:
            raise _typed(exc) from exc
    return current


def ensure(root: Any) -> pathlib.Path:
    """The store ready for use: present, or created and cut over from this root's own chain
    files (the first row or read of a child root pays for its own small files, never a boot;
    the main root is cut over by the boot coordinator before the dialogue opens)."""
    if not present(root):
        from ouroboros.dialogue_import import cutover

        cutover(root)
    return path(root)


@contextlib.contextmanager
def transaction(root: Any, *, write: bool, wait_sec: float = WAIT_SEC) -> Iterator[sqlite3.Connection]:
    """One transaction on the root's store (``store.connection`` + ``store.transaction``): the name
    lock around a name-tier access, ``StoreBusy`` on contention, the cutover for a root whose
    chain files were never cut over (its own files; unknown is never answered empty), an empty
    in-memory schema for a read of a root that holds no dialogue at all."""
    root = pathlib.Path(root)
    if not present(root):
        if write or holds_dialogue_files(root):
            ensure(root)
        else:
            with contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(_SCHEMA)
                with store.transaction(conn, write=False):
                    yield conn
            return
    tier = _tier(root)
    with contextlib.ExitStack() as stack:
        if tier == TIER_NAME:
            stack.enter_context(store.name_lock(root, LOCK_NAME, timeout_sec=wait_sec))
        try:
            conn = stack.enter_context(store.connection(path(root), tier, wait_sec=wait_sec))
            with store.transaction(conn, write=write, commit_wait_sec=wait_sec if tier == TIER_ENFORCED else None):
                yield conn
        except sqlite3.Error as exc:
            raise _typed(exc) from exc


def meta(root: Any, key: str) -> Any:
    with transaction(root, write=False) as conn:
        return _meta(conn, key)


def _meta(conn: sqlite3.Connection, key: str) -> Any:
    record = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if record is None else json.loads(record["value"])


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))


# --- the row's typed facts -------------------------------------------------------------------

def facts(row: Dict[str, Any]) -> Dict[str, Any]:
    """The columns a row is addressed by, read from the body as every chain reader read them
    (an absent or empty key is ``None``: the partial indexes hold only rows that carry the fact)."""
    chat_id: Optional[int]
    try:
        chat_id = int(row["chat_id"]) if row.get("chat_id") not in (None, "") else None
    except (TypeError, ValueError):
        chat_id = None

    def text(key: str) -> str:
        value = row.get(key)
        return "" if value is None or isinstance(value, (dict, list)) else str(value)

    origin = None
    ref = row.get("origin_message_ref")
    if isinstance(ref, dict) and ref.get("client_message_id"):
        try:
            origin = f"{int(ref.get('chat_id') or 0)}:{ref['client_message_id']}"
        except (TypeError, ValueError):
            origin = None
    return {"ts": text("ts"), "chat_id": chat_id, "task_id": text("task_id") or None,
            "parent_task_id": text("parent_task_id") or None, "root_task_id": text("root_task_id") or None,
            "direction": text("direction"), "kind": text("type") or None,
            "client_message_id": text("client_message_id") or None, "origin": origin}


def _row(record: sqlite3.Row) -> Row:
    return Row(record["stream"], record["seq"], decode(record["body"]), record["dispatch"])


_SELECT = "SELECT stream, seq, body, dispatch FROM dialogue"


# --- the one writer seam ---------------------------------------------------------------------

def insert_row(conn: sqlite3.Connection, stream: str, seq: int, row: Dict[str, Any], body: str, *,
               dispatch: Optional[str] = None, dispatch_input: Optional[Dict[str, Any]] = None,
               revision: int = 0) -> None:
    """One row and its room facts inside the caller's transaction (``append`` and the importer)."""
    columns = facts(row)
    conn.execute(
        "INSERT INTO dialogue (stream, seq, ts, chat_id, task_id, parent_task_id, root_task_id, direction, kind, "
        "client_message_id, origin, dispatch, dispatch_input, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (stream, seq, columns["ts"], columns["chat_id"], columns["task_id"], columns["parent_task_id"],
         columns["root_task_id"], columns["direction"], columns["kind"], columns["client_message_id"], columns["origin"],
         dispatch, None if dispatch_input is None else json.dumps(dispatch_input, ensure_ascii=False), body))
    if columns["chat_id"] is not None:
        touch_room(conn, columns["chat_id"], seq if stream == "chat" else None, columns["ts"], revision)


def touch_room(conn: sqlite3.Connection, chat_id: int, last_seq: Optional[int], activity_at: str, revision: int) -> None:
    conn.execute(
        "INSERT INTO rooms (chat_id, revision, last_seq, read_revision, activity_at) VALUES (?, ?, ?, 0, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET revision = rooms.revision + excluded.revision, "
        "last_seq = CASE WHEN excluded.last_seq IS NULL THEN rooms.last_seq "
        "ELSE max(coalesce(rooms.last_seq, -1), excluded.last_seq) END, "
        "activity_at = max(rooms.activity_at, excluded.activity_at)",
        (chat_id, revision, last_seq, activity_at))


def _existing_named(conn: sqlite3.Connection, chat_id: Optional[int], client_message_id: str) -> Optional[sqlite3.Row]:
    """The newest inbound row the id names in this chat: the answer a full chain scan gave."""
    return conn.execute(
        f"{_SELECT} INDEXED BY dialogue_named WHERE stream='chat' AND client_message_id=? AND chat_id IS ? "
        "AND direction='in' ORDER BY seq DESC LIMIT 1", (client_message_id, chat_id)).fetchone()


def append(root: Any, stream: str, row: Dict[str, Any], *, dispatch: Optional[Dict[str, Any]] = None,
           feed: bool = False, wait_sec: float = WAIT_SEC) -> Receipt:
    """Store one row: ONE transaction for the row, the dedup, the room and the dispatch obligation.

    ``row`` is stored as the exact JSON text ``append_jsonl`` would have written. An inbound row
    with a ``client_message_id`` is a named acceptance: an existing inbound row of that id in
    that chat answers instead (``duplicate``), the partial UNIQUE index refusing the race two
    processes lose by a hair. ``dispatch`` (the message the bus still owes a dispatch for,
    JSON-serializable, bytes omitted) records the obligation as ``pending``; a named acceptance
    without one is ``none``. ``feed`` advances the room's revision (a standalone message the
    owner has not read). ``StoreBusy``/``StoreError`` are the caller's: a best-effort writer logs
    and goes on, a required acceptance raises.
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown dialogue stream: {stream!r}")
    body = encode(row)
    columns = facts(row)
    named = columns["direction"] == "in" and bool(columns["client_message_id"])
    state = None if not named else ("pending" if dispatch is not None else "none")
    root = pathlib.Path(root)
    with transaction(root, write=True, wait_sec=wait_sec) as conn:
        if named:
            existing = _existing_named(conn, columns["chat_id"], columns["client_message_id"])
            if existing is not None:
                return Receipt(existing["stream"], existing["seq"], decode(existing["body"]), True, existing["dispatch"])
        seq = int(conn.execute("SELECT next_seq FROM streams WHERE stream=?", (stream,)).fetchone()["next_seq"])
        try:
            insert_row(conn, stream, seq, row, body, dispatch=state, dispatch_input=dispatch, revision=int(bool(feed)))
        except sqlite3.IntegrityError:
            existing = _existing_named(conn, columns["chat_id"], columns["client_message_id"]) if named else None
            if existing is None:
                raise
            return Receipt(existing["stream"], existing["seq"], decode(existing["body"]), True, existing["dispatch"])
        conn.execute("UPDATE streams SET next_seq=? WHERE stream=?", (seq + len(body.encode("utf-8")) + 1, stream))
    if stream == "progress":
        _stream_live(row)
    return Receipt(stream, seq, row, False, state)


def append_quietly(root: Any, stream: str, row: Dict[str, Any], **kwargs: Any) -> Optional[Receipt]:
    """``append`` for a best-effort writer (a progress frame, a host summary): a store failure is
    logged and answered ``None``, as ``append_jsonl`` answered ``False``; the live frame still goes out."""
    try:
        return append(root, stream, row, **kwargs)
    except (StoreError, OSError, ValueError):
        log.warning("dialogue row not stored in %s (%s)", path(root), stream, exc_info=True)
        return None


def _stream_live(row: Dict[str, Any]) -> None:
    """The live frame a progress row's writer owed through ``append_jsonl``'s sink (``utils.set_log_sink``):
    the file is gone, the one live frame is not. Chat rows have their own live channel."""
    from ouroboros import utils

    sink = getattr(utils, "_log_sink", None)
    if sink is not None:
        try:
            sink(row)
        except Exception:
            pass


# --- the dispatch obligation -----------------------------------------------------------------

def dispatch_facts(root: Any, chat_id: Optional[int], client_message_id: str) -> Optional[Dict[str, Any]]:
    """The accepted row's dispatch facts: ``state`` and, once handed over, the ``process`` (the
    custody session that took it) and ``dispatcher``; ``None`` for no row, an imported row or
    an unnamed one."""
    if not client_message_id or not present(root):
        return None
    with transaction(root, write=False) as conn:
        record = conn.execute(
            "SELECT dispatch, dispatch_input FROM dialogue INDEXED BY dialogue_named WHERE stream='chat' AND client_message_id=? "
            "AND chat_id IS ? AND direction='in' ORDER BY seq DESC LIMIT 1", (client_message_id, chat_id)).fetchone()
    if record is None or record["dispatch"] is None:
        return None
    recorded = json.loads(record["dispatch_input"] or "{}")
    return {"state": record["dispatch"], "process": str(recorded.get("process") or ""),
            "dispatcher": str(recorded.get("dispatcher") or "")}


def dispatch_state(root: Any, chat_id: Optional[int], client_message_id: str) -> Optional[str]:
    facts_ = dispatch_facts(root, chat_id, client_message_id)
    return None if facts_ is None else facts_["state"]


def _move_dispatch(root: Any, chat_id: Optional[int], client_message_id: str, *, states: Tuple[str, ...],
                   process: str) -> bool:
    if not client_message_id or not present(root):
        return False
    with transaction(root, write=True) as conn:
        cursor = conn.execute(
            "UPDATE dialogue INDEXED BY dialogue_named SET dispatch='dispatched', "
            "dispatch_input=json_set(coalesce(dispatch_input, '{}'), '$.process', ?) WHERE stream='chat' AND client_message_id=? "
            f"AND chat_id IS ? AND direction='in' AND dispatch IN ({','.join('?' * len(states))})",
            (process, client_message_id, chat_id, *states))
        return cursor.rowcount == 1


def claim_dispatch(root: Any, chat_id: Optional[int], client_message_id: str, *, process: str = "") -> bool:
    """Take the owed dispatch of an accepted row (pending -> dispatched): the durable "entered"
    mark, with the ``process`` (custody session) that takes it. Exactly one caller wins -- the
    acceptor, a same-id retry or the recovery."""
    return _move_dispatch(root, chat_id, client_message_id, states=("pending",), process=process)


def note_dispatched(root: Any, chat_id: Optional[int], client_message_id: str, *, process: str = "") -> bool:
    """The entry mark of a named acceptance that owed no bus dispatch (``none`` -> dispatched):
    its caller hands it to its own dispatcher now."""
    return _move_dispatch(root, chat_id, client_message_id, states=("none", "pending"), process=process)


def complete_dispatch(root: Any, chat_id: Optional[int], client_message_id: str) -> bool:
    """The handoff returned (dispatched -> completed; the recorded input shrinks to the dispatcher
    and the process); a callback that raised after entry leaves ``dispatched``, which is
    ambiguous and never replayed."""
    if not client_message_id or not present(root):
        return False
    with transaction(root, write=True) as conn:
        cursor = conn.execute(
            "UPDATE dialogue INDEXED BY dialogue_named SET dispatch='completed', dispatch_input=json_object("
            "'dispatcher', json_extract(dispatch_input, '$.dispatcher'), 'process', json_extract(dispatch_input, '$.process')) "
            "WHERE stream='chat' AND client_message_id=? AND chat_id IS ? AND direction='in' AND dispatch='dispatched'",
            (client_message_id, chat_id))
        return cursor.rowcount == 1


def abandon_dispatch(root: Any, chat_id: Optional[int], client_message_id: str, reason: str) -> bool:
    """A pending obligation nobody can dispatch (its inputs are gone): the typed outcome."""
    if not present(root):
        return False
    with transaction(root, write=True) as conn:
        cursor = conn.execute(
            "UPDATE dialogue INDEXED BY dialogue_named SET dispatch='abandoned', "
            "dispatch_input=json_set(coalesce(dispatch_input, '{}'), '$.abandoned', ?) "
            "WHERE stream='chat' AND client_message_id=? AND chat_id IS ? AND direction='in' AND dispatch='pending'",
            (reason, client_message_id, chat_id))
        return cursor.rowcount == 1


def pending_dispatches(root: Any) -> List[Tuple[Row, Dict[str, Any]]]:
    """Every accepted row whose dispatch is still owed, oldest first, with the input it recorded
    (the partial index ``dialogue_pending``; no chain is read)."""
    if not present(root):
        return []
    with transaction(root, write=False) as conn:
        records = conn.execute("SELECT stream, seq, body, dispatch, dispatch_input FROM dialogue INDEXED BY dialogue_pending "
                               "WHERE stream='chat' AND dispatch='pending' ORDER BY seq").fetchall()
    return [(_row(record), json.loads(record["dispatch_input"] or "{}")) for record in records]


# --- addressed reads -------------------------------------------------------------------------

def row_at(root: Any, stream: str, seq: int) -> Optional[Row]:
    with transaction(root, write=False) as conn:
        record = conn.execute(f"{_SELECT} WHERE stream=? AND seq=?", (stream, seq)).fetchone()
    return None if record is None else _row(record)


def rows_between(root: Any, stream: str, lower: int, upper: int) -> List[Row]:
    """Every row with ``lower <= seq < upper`` in order (a frozen page replayed by its coordinates)."""
    with transaction(root, write=False) as conn:
        records = conn.execute(f"{_SELECT} WHERE stream=? AND seq>=? AND seq<? ORDER BY seq", (stream, lower, upper)).fetchall()
    return [_row(record) for record in records]


def rows_after(root: Any, stream: str, seq: int, *, limit: int = 1000) -> List[Row]:
    """The rows appended after ``seq`` (a live follower's next batch, a wake's window), in order."""
    with transaction(root, write=False) as conn:
        records = conn.execute(f"{_SELECT} WHERE stream=? AND seq>? ORDER BY seq LIMIT ?", (stream, seq, limit)).fetchall()
    return [_row(record) for record in records]


def iter_rows(root: Any, stream: str, *, lower: int = 0, upper: Optional[int] = None, batch: int = 500) -> Iterator[Row]:
    """Every row with ``lower <= seq < upper`` in order, one short read transaction per
    ``batch`` rows (a whole-stream reader -- consolidation, the chronicle import, an audit --
    never holds the store's read lock across its own work)."""
    bound = _bound(upper)
    while True:
        with transaction(root, write=False) as conn:
            records = conn.execute(f"{_SELECT} WHERE stream=? AND seq>=? AND seq<? ORDER BY seq LIMIT ?",
                                   (stream, lower, bound, batch)).fetchall()
        for record in records:
            yield _row(record)
        if len(records) < batch:
            return
        lower = records[-1]["seq"] + 1


def last_inbound(root: Any, chat_id: Optional[int], *, before: Optional[int] = None) -> Optional[Row]:
    """The newest inbound (owner) row of one chat below ``before`` (the partial index
    ``dialogue_inbound``: the chat's own inbound rows, not its outbound history)."""
    with transaction(root, write=False) as conn:
        record = conn.execute(f"{_SELECT} INDEXED BY dialogue_inbound WHERE stream='chat' AND chat_id IS ? AND direction='in' "
                              "AND seq<? ORDER BY seq DESC LIMIT 1", (chat_id, _bound(before))).fetchone()
    return None if record is None else _row(record)


def newest(root: Any, stream: str, *, limit: int, before: Optional[int] = None) -> List[Row]:
    """The stream's newest rows below ``before`` (its end when None), oldest first."""
    with transaction(root, write=False) as conn:
        records = conn.execute(f"{_SELECT} WHERE stream=? AND seq<? ORDER BY seq DESC LIMIT ?",
                               (stream, _bound(before), limit)).fetchall()
    return [_row(record) for record in reversed(records)]


def _bound(before: Optional[int]) -> int:
    return (1 << 62) if before is None else int(before)


def probe_rows(root: Any, stream: str, keys: Iterable[Tuple[str, Any]], *, before: Optional[int] = None,
               lower: int = 0, limit: int = 200, newest_first: bool = True) -> List[Row]:
    """The union of one indexed probe per ``(column, value)`` key in ``[lower, before)``, newest
    first, at most ``limit`` rows: the candidate rows of a room or a task tree. Each probe reads
    at most ``limit`` rows through its own index, so the cost is the number of keys times the
    page -- never another room's history. ``("chat_id", None)`` probes rows without a chat id."""
    with transaction(root, write=False) as conn:
        return _probe(conn, stream, keys, before=before, lower=lower, limit=limit, newest_first=newest_first)


def _probe(conn: sqlite3.Connection, stream: str, keys: Iterable[Tuple[str, Any]], *, before: Optional[int],
           lower: int, limit: int, newest_first: bool) -> List[Row]:
    found: Dict[int, sqlite3.Row] = {}
    order = "DESC" if newest_first else "ASC"
    for column, value in dict.fromkeys(tuple(keys)):
        index = _PROBE_INDEX.get(column)
        if index is None:
            raise ValueError(f"not a dialogue probe column: {column!r}")
        if value is None and column != "chat_id":
            continue  # the partial indexes hold only rows that carry the fact: nothing to probe
        # ``IS`` reaches the NULL chat rows through the full room index; a partial index needs ``=``.
        operator = "IS" if column == "chat_id" else "="
        for record in conn.execute(f"{_SELECT} INDEXED BY {index} WHERE stream=? AND {column} {operator} ? AND seq<? AND seq>=? "
                                   f"ORDER BY seq {order} LIMIT ?", (stream, value, _bound(before), lower, limit)):
            found.setdefault(record["seq"], record)
    chosen = heapq.nlargest(limit, found) if newest_first else heapq.nsmallest(limit, found)
    return [_row(found[seq]) for seq in chosen]


def room_candidates(root: Any, stream: str, chat_id: int, *, tasks: Iterable[str] = (), origins: Iterable[str] = (),
                    before: Optional[int] = None, lower: int = 0, limit: int = 200) -> List[Row]:
    """A room's possible rows, newest first (``project_dialogue.room_membership`` decides each):
    the rows written to its chat, every row of a task the room rule can place in it (by task,
    parent or root id) and the owner rows its bindings name (``origin`` = ``<chat>:<client id>``)."""
    keys: List[Tuple[str, Any]] = [("chat_id", int(chat_id))]
    for task in tasks:
        keys.extend((("task_id", task), ("parent_task_id", task), ("root_task_id", task)))
    keys.extend(("origin", origin) for origin in origins)
    return probe_rows(root, stream, keys, before=before, lower=lower, limit=limit)


def main_candidates(root: Any, stream: str, *, exclude_chats: Iterable[int], pinned_kinds: Iterable[str] = (),
                    before: Optional[int] = None, lower: int = 0, limit: int = 200) -> List[Row]:
    """Main's possible rows, newest first: the rows of every chat no Project owns (the rooms the
    store knows minus ``exclude_chats``, plus rows without a chat id) and the Project rows of the
    kinds Main mirrors (``pinned_kinds``: its questions)."""
    excluded = {int(chat) for chat in exclude_chats}
    with transaction(root, write=False) as conn:
        chats = [record["chat_id"] for record in conn.execute("SELECT chat_id FROM rooms ORDER BY chat_id")
                 if record["chat_id"] not in excluded]
        keys: List[Tuple[str, Any]] = [("chat_id", chat) for chat in chats] + [("chat_id", None)]
        keys.extend(("kind", kind) for kind in pinned_kinds)
        return _probe(conn, stream, keys, before=before, lower=lower, limit=limit, newest_first=True)


def task_rows(root: Any, task_ids: Iterable[str], *, stream: str, after: int = -1, limit: int = 1000) -> List[Row]:
    """A task tree's rows in one stream, oldest first: every row whose task, parent or root is one
    of ``task_ids`` and lies after ``after`` (the timeline, the SSE follower, the recent-activity window)."""
    keys: List[Tuple[str, Any]] = []
    for task in task_ids:
        keys.extend((("task_id", task), ("parent_task_id", task), ("root_task_id", task)))
    return probe_rows(root, stream, keys, lower=after + 1, limit=limit, newest_first=False)


def accepted_row(root: Any, chat_id: Optional[int], client_message_id: str) -> Optional[Row]:
    """The inbound row ``(chat_id, client_message_id)`` names (the newest), as the full chain scan
    answered. While the chat archive is still importing, a miss is checked against the segments
    not yet imported (``dialogue_import.scan_pending``): unknown is never answered "absent"."""
    if not client_message_id:
        return None
    with transaction(root, write=False) as conn:
        record = _existing_named(conn, chat_id, client_message_id)
        importing = record is None and conn.execute(
            "SELECT 1 FROM import_ledger WHERE stream='chat' AND state='pending' LIMIT 1").fetchone() is not None
    if record is not None:
        return _row(record)
    if importing:
        from ouroboros.dialogue_import import scan_pending

        return scan_pending(root, chat_id, client_message_id)
    return None


def rows_by_client_id(root: Any, client_message_id: str, *, stream: str = "chat", chat_id: Any = -1,
                      limit: int = 50) -> List[Row]:
    """The rows carrying a client message id (any direction), newest first; ``chat_id`` narrows
    when given (``None`` for rows without one)."""
    if not client_message_id:
        return []
    where, params = "", ()
    if chat_id != -1:
        where, params = " AND chat_id IS ?", (chat_id,)
    with transaction(root, write=False) as conn:
        records = conn.execute(f"{_SELECT} INDEXED BY dialogue_named WHERE stream=? AND client_message_id=?{where} "
                               "ORDER BY seq DESC LIMIT ?", (stream, client_message_id, *params, limit)).fetchall()
    return [_row(record) for record in records]


def rows_by_identity(root: Any, chat_id: Optional[int], ts: str, *, stream: str = "chat") -> List[Row]:
    """The rows of one chat stamped ``ts`` (a ``chat_chain`` row address's chat and time), in order."""
    with transaction(root, write=False) as conn:
        records = conn.execute(f"{_SELECT} INDEXED BY dialogue_room_ts WHERE stream=? AND chat_id IS ? AND ts=? ORDER BY seq",
                               (stream, chat_id, ts)).fetchall()
    return [_row(record) for record in records]


def resolve_legacy(root: Any, gen: str, line: int) -> Optional[Row]:
    """The chat row a legacy ``(gen, line)`` address names: ``gen`` the generation's first-line
    sha256 (``utils.jsonl_generation_signature``), ``line`` its physical line number."""
    if not gen or not line:
        return None
    with transaction(root, write=False) as conn:
        record = conn.execute(
            f"{_SELECT} WHERE stream='chat' AND seq=(SELECT seq FROM legacy_addresses WHERE gen=? AND line=?)",
            (gen, int(line))).fetchone()
    return None if record is None else _row(record)


def legacy_address(root: Any, seq: int) -> Optional[Tuple[str, int]]:
    """The ``(gen, line)`` an imported chat row had (``None`` for a row appended here)."""
    with transaction(root, write=False) as conn:
        record = conn.execute("SELECT gen, line FROM legacy_addresses WHERE seq=? LIMIT 1", (seq,)).fetchone()
    return None if record is None else (record["gen"], record["line"])


def legacy_generation(root: Any, gen: str) -> Optional[Dict[str, Any]]:
    """The imported chat segment whose first-line signature is ``gen`` (its ledger row: ``name``,
    ``base`` -- the coordinate of its first byte --, ``size``, ``rows``), so a legacy cursor
    ``(gen, byte offset)`` is the coordinate ``base + offset``. ``None`` while the segment is
    not imported yet (its signature is read at import) or never existed: a caller then reports
    the range as still importing when ``dialogue_import.coverage`` says so, never as gone."""
    if not gen:
        return None
    with transaction(root, write=False) as conn:
        record = conn.execute("SELECT * FROM import_ledger WHERE stream='chat' AND gen=? AND state='done'", (gen,)).fetchone()
    return None if record is None else dict(record)


def stream_end(root: Any, stream: str) -> int:
    """The stream's next coordinate: a page's frozen upper bound, and the one change counter a
    reader compares instead of a file's stat."""
    with transaction(root, write=False) as conn:
        record = conn.execute("SELECT next_seq FROM streams WHERE stream=?", (stream,)).fetchone()
    return 0 if record is None else int(record["next_seq"])


def room(root: Any, chat_id: int) -> Optional[Dict[str, Any]]:
    with transaction(root, write=False) as conn:
        record = conn.execute("SELECT * FROM rooms WHERE chat_id=?", (int(chat_id),)).fetchone()
    return None if record is None else dict(record)


def rooms(root: Any) -> List[Dict[str, Any]]:
    """Every room the store knows (its revision, last chat row and activity), by chat id."""
    with transaction(root, write=False) as conn:
        return [dict(record) for record in conn.execute("SELECT * FROM rooms ORDER BY chat_id")]


def mark_room_read(root: Any, chat_id: int, revision: int) -> Dict[str, Any]:
    """The owner read the room through ``revision`` (never backwards); returns the room."""
    with transaction(root, write=True) as conn:
        conn.execute("INSERT INTO rooms (chat_id, read_revision) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                     "read_revision = max(rooms.read_revision, excluded.read_revision)", (int(chat_id), int(revision)))
        return dict(conn.execute("SELECT * FROM rooms WHERE chat_id=?", (int(chat_id),)).fetchone())


def counts(root: Any) -> Dict[str, int]:
    """Rows per stream (tests and the import report; a full count, not a hot-path read)."""
    with transaction(root, write=False) as conn:
        return {record["stream"]: record["n"] for record in conn.execute("SELECT stream, count(*) AS n FROM dialogue GROUP BY stream")}


def iter_probes(root: Any, stream: str, keys: Iterable[Tuple[str, Any]], *, lower: int = 0,
                upper: Optional[int] = None, batch: int = 200, newest_first: bool = False) -> Iterator[Row]:
    """Added by Q1b: iterate an addressed union in a frozen range, in short transactions.

    Each page reads only the indexed keys, deduplicates overlapping probes and advances
    by coordinate. A caller may stop early without holding a connection or a read lock.
    """
    if batch < 1:
        raise ValueError("a dialogue probe batch must be positive")
    keys = tuple(keys)
    if not keys:
        return
    upper = stream_end(root, stream) if upper is None else int(upper)
    while lower < upper:
        rows = probe_rows(root, stream, keys, lower=lower, before=upper,
                          limit=batch, newest_first=newest_first)
        yield from rows
        if len(rows) < batch:
            return
        if newest_first:
            upper = rows[-1].seq
        else:
            lower = rows[-1].seq + 1


def range_coverage(root: Any, stream: str, *, lower: int = 0,
                   upper: Optional[int] = None) -> Dict[str, Any]:
    """Added by Q1b: import coverage of exactly ``[lower, upper)``.

    Same answer as ``dialogue_import.coverage``, restricted to intersecting segments:
    an older pending archive does not make a fully imported recent page incomplete.
    Reading starts the root's normal cutover if needed, never the archive import job.
    """
    if stream not in STREAMS:
        raise ValueError(f"not a dialogue stream: {stream!r}")
    with transaction(root, write=False) as conn:
        counts = {record["state"]: record["n"] for record in conn.execute(
            "SELECT state, count(*) AS n FROM import_ledger INDEXED BY ledger_state "
            "WHERE stream=? AND state IN ('pending', 'mismatch') AND base<? AND base+size>? "
            "GROUP BY state", (stream, _bound(upper), lower))} if lower < _bound(upper) else {}
    pending, mismatch = int(counts.get("pending", 0)), int(counts.get("mismatch", 0))
    return {"imported": not pending and not mismatch, "pending": pending, "mismatch": mismatch}


def require_imported(root: Any, stream: str, *, lower: int = 0, upper: Optional[int] = None) -> None:
    """Added by Q1b: an absence/complete-fold consumer cannot turn pending import into truth.

    Readers with a coverage field use ``range_coverage`` directly; readers whose public
    answer cannot express an unknown interval propagate the store's typed failure.
    """
    coverage = range_coverage(root, stream, lower=lower, upper=upper)
    if not coverage["imported"]:
        if coverage["mismatch"]:
            raise StoreError("Dialogue history: a segment could not be imported")
        raise StoreMigrating("Dialogue history is still importing")
