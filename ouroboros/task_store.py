"""Task result documents in the current-facts store: the primitives every consumer uses.

Readers select typed facts before decoding only the bodies they need, after closing
the transaction. ``patch_document`` owns document updates; ``patch_handoff`` /
``put_handoffs`` write one operation row of the ``store.ROW_BACKED_FIELDS``
projections (a tool claim, a model wait, a retired consumer) without a body write;
``load`` projects the rows back.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ouroboros import store
from ouroboros.store import Txn, open_store

FACT_KEYS = ("task_id", "status", "ts", "updated_at", "delegation_role", "parent_task_id", "project_id", "root_task_id",
             "child_drive_root", "headless_child_drive_root", "superseded_by", "retry_task_id",
             "presence_binding_id", "presence_authority_recorded")
_NEWEST_ORDER = "ORDER BY ts DESC, task_id DESC"  # NULL ts sorts last: a row without one is oldest


def read_document(root: Any, task_id: str, *, txn: Optional[Txn] = None) -> Optional[Dict[str, Any]]:
    """The whole current document, ``None`` when the store holds no such task."""
    if txn is not None:
        return txn.load(str(task_id))
    with open_store(root, write=False) as txn:
        return txn.load(str(task_id))


def exists(root: Any, task_id: str) -> bool:
    with open_store(root, write=False) as txn:
        return txn.row(str(task_id), "task_id") is not None


def signature(root: Any, task_id: str, *, txn: Optional[Txn] = None) -> Optional[Tuple[int, str]]:
    """``(write_seq, updated_at)``: changes on every write; ``None`` for an absent task. The key
    the memos compare that used to compare a result file's stat."""
    if txn is not None:
        return txn.row(str(task_id), "write_seq", "updated_at")
    with open_store(root, write=False) as txn:
        return signature(root, task_id, txn=txn)


def delete_document(root: Any, task_id: str) -> bool:
    """Remove one task's row and body (a scheduling refusal undoing its own requested row)."""
    with open_store(root, write=True) as txn:
        present = txn.row(str(task_id), "task_id") is not None
        txn.delete_task(str(task_id))
        return present


def patch_document(root: Any, task_id: str, mutator: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Mutate CURRENT in one write; None writes nothing. Return the current document."""
    from ouroboros.obligations import pending_drive_rule

    task_id = str(task_id)
    seen: Dict[str, Any] = {}

    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        seen["current"] = current
        return mutator(current)

    updated = store.update_task(root, task_id, _mutate, pending_drive=pending_drive_rule)
    return seen.get("current", {}) if updated is None else {**updated, "task_id": task_id}


def _fetch(txn: Txn, where: str = "", params: Iterable[Any] = (), order: str = "ORDER BY t.task_id") -> Tuple[list, list]:
    """The matching rows and their handoff rows as stored (text): all the store is held for.
    A reader decodes AFTER its transaction closed (``_decode``), so a writer's COMMIT never
    waits behind a listing's ``json.loads``."""
    clause = f"WHERE {where}" if where else ""
    rows = txn.conn.execute(
        f"SELECT b.body, t.* FROM tasks AS t JOIN task_bodies AS b ON b.task_id = t.task_id {clause} {order}",
        tuple(params)).fetchall()
    return rows, txn.handoff_records([record["task_id"] for record in rows])


def _decode(rows: list, handoff_records: list) -> List[Dict[str, Any]]:
    handoffs = store.decode_handoffs([record["task_id"] for record in rows], handoff_records)
    return [store.project_handoffs(json.loads(record["body"]), handoffs[record["task_id"]]) for record in rows]


class Reprepare(Exception):
    """A mutator's answer when the write needs an input it may only prepare OUTSIDE the lock
    (a mailbox read, an event-chain snapshot, an artifact write): the transaction rolls back
    empty and ``prepared_write`` repeats the whole write with that input prepared first."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def prepared_write(write: Callable[[set], Any]) -> Any:
    """Run ``write(needs)`` until its mutator stops asking: every ``Reprepare`` adds its name to
    ``needs`` and the next round prepares it before ``BEGIN``. No I/O happens under the lock."""
    needs: set = set()
    for _round in range(4):
        try:
            return write(needs)
        except Reprepare as exc:
            needs.add(exc.what)
    return write(needs)


DELETE_ROW = object()  # a handoff mutator's answer: drop the row


def patch_handoff(root: Any, task_id: str, kind: str, op_id: str,
                  mutator: Callable[[Optional[Dict[str, Any]]], Any]) -> Optional[Dict[str, Any]]:
    """One operation row of a task's projection ``kind`` (a ``store.ROW_BACKED_FIELDS`` name) in
    its own write transaction, no body write: the mutator sees the current row (``None`` when
    absent) and returns the new row, ``None`` to leave the store untouched, or ``DELETE_ROW``.
    ``LookupError`` when the task has no lifecycle row: a projection needs its owner. Returns the
    row that is current when the transaction ends."""
    task_id, op_id = str(task_id), str(op_id)
    with open_store(root, write=True) as txn:
        if not txn.row(task_id, "status"):
            raise LookupError(f"task {task_id} has no lifecycle row")
        current = txn.handoff(task_id, op_id)
        updated = mutator(json.loads(json.dumps(current)) if current is not None else None)
        if updated is None:
            return current
        if updated is DELETE_ROW:
            txn.delete_handoff(task_id, op_id)
        else:
            txn.put_handoff(task_id, kind, op_id, updated)
        txn.touch(task_id)
        return None if updated is DELETE_ROW else updated


def put_handoffs(root: Any, task_id: str, kind: str, rows: Dict[str, Any]) -> None:
    """Upsert several operation rows of one projection in one transaction (``LookupError`` as above)."""
    task_id = str(task_id)
    with open_store(root, write=True) as txn:
        if not txn.row(task_id, "status"):
            raise LookupError(f"task {task_id} has no lifecycle row")
        for op_id, facts in rows.items():
            txn.put_handoff(task_id, kind, str(op_id), facts)
        txn.touch(task_id)


def list_documents(root: Any, *, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every document, or those whose stored status is one of ``statuses`` (the WHERE prefilter;
    a caller filtering on the EFFECTIVE status still filters the overlaid rows itself)."""
    wanted = None if statuses is None else sorted({str(status) for status in statuses})
    if wanted is not None and not wanted:
        return []
    with open_store(root, write=False) as txn:
        fetched = _fetch(txn) if wanted is None else _fetch(txn, f"t.status IN ({','.join('?' * len(wanted))})", wanted)
    return _decode(*fetched)


def lineage_where(parent: str, root_id: str, *, direct_only: bool) -> Tuple[str, Tuple[str, ...]]:
    """The ``find_child_tasks`` prefilter as SQL: every row carrying a retry or supersession
    pointer (lineage any child may hang on), plus every ``subagent`` row naming PARENT as its
    parent or (subtree scope) ROOT_ID as its root."""
    selects = ["SELECT task_id FROM tasks WHERE superseded_by IS NOT NULL",
               "SELECT task_id FROM tasks WHERE retry_task_id IS NOT NULL"]
    params = []
    if parent:  # a UNION of indexed selects: each term reads its own index, never the table
        selects.append("SELECT task_id FROM tasks WHERE parent_task_id=? AND delegation_role='subagent'")
        params.append(parent)
    if root_id and not direct_only:
        selects.append("SELECT task_id FROM tasks WHERE root_task_id=? AND delegation_role='subagent'")
        params.append(root_id)
    return f"t.task_id IN ({' UNION '.join(selects)})", tuple(params)


def select_lineage(root: Any, parent: str, root_id: str, *, direct_only: bool = False) -> List[Dict[str, Any]]:
    """The child-lookup candidates in ``task_id`` order (``lineage_where``); the Python projection decides the rest."""
    where, params = lineage_where(str(parent or ""), str(root_id or ""), direct_only=direct_only)
    with open_store(root, write=False) as txn:
        fetched = _fetch(txn, where, params)
    return _decode(*fetched)


def task_facts(root: Any, *, where: str = "", params: Iterable[Any] = ()) -> Dict[str, Dict[str, Any]]:
    """Typed facts of every task (keyed by task id) without reading a body: the keys
    ``FACT_KEYS`` as strings (``""`` for no fact), ``presence_authority_recorded`` as a bool."""
    clause = f"WHERE {where}" if where else ""
    with open_store(root, write=False) as txn:
        records = txn.conn.execute(
            f"SELECT {','.join(FACT_KEYS)} FROM tasks {clause} ORDER BY task_id", tuple(params)).fetchall()
    facts = {}
    for record in records:
        row = {key: str(record[key] or "") for key in FACT_KEYS if key != "presence_authority_recorded"}
        row["presence_authority_recorded"] = bool(record["presence_authority_recorded"])
        facts[row["task_id"]] = row
    return facts


def newest_ids(root: Any, limit: Optional[int] = None, *, where: str = "", params: Iterable[Any] = ()) -> List[str]:
    """Task ids newest first by the stored ``ts`` (a row without one sorts oldest), ties by id."""
    clause = f"WHERE {where} " if where else ""
    with open_store(root, write=False) as txn:
        bound = "" if limit is None else f" LIMIT {int(limit)}"
        return [record["task_id"] for record in
                txn.conn.execute(f"SELECT task_id FROM tasks {clause}{_NEWEST_ORDER}{bound}", tuple(params))]


ROOT_RESULT_WHERE = "parent_task_id IS NULL AND (delegation_role IS NULL OR delegation_role != 'subagent')"


def documents_by_ids(root: Any, task_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = [str(task_id) for task_id in task_ids]
    if not ids:
        return {}
    with open_store(root, write=False) as txn:
        fetched = _fetch(txn, f"t.task_id IN ({','.join('?' * len(ids))})", ids)
    return {str(document.get("task_id")): document for document in _decode(*fetched)}


def task_ids(root: Any) -> List[str]:
    with open_store(root, write=False) as txn:
        return txn.task_ids()


def ids_with_fields(root: Any, markers: Iterable[str]) -> List[str]:
    """Task ids whose body names any of the top-level ``markers`` (the settlement sweep's
    candidates, found with one body scan inside the store instead of a directory read)."""
    patterns = [f'%"{marker}":%' for marker in markers]
    if not patterns:
        return []
    with open_store(root, write=False) as txn:
        where = " OR ".join("body LIKE ?" for _ in patterns)
        return [record["task_id"] for record in
                txn.conn.execute(f"SELECT task_id FROM task_bodies WHERE {where} ORDER BY task_id", patterns)]


def inventory(root: Any) -> List[Dict[str, Any]]:
    """Every task's paging identity, newest write first: ``name`` (the task id), ``write_seq``
    (changes with every write, the role the file size and mtime played) and ``updated_at``."""
    with open_store(root, write=False) as txn:
        records = txn.conn.execute("SELECT task_id, write_seq, updated_at FROM tasks "
                                   "ORDER BY updated_at DESC, task_id DESC").fetchall()
    return [{"name": record["task_id"], "write_seq": int(record["write_seq"] or 0),
             "updated_at": str(record["updated_at"] or "")} for record in records]
