"""The dialogue's cutover from the JSONL chains, the archive import job and the rollback export.

``cutover(root)`` is the one durable transition of a data root from files to
``state/dialog.sqlite``: the live ``logs/chat.jsonl`` and ``logs/progress.jsonl`` are rotated
into ``archive/`` one last time under today's names, every archive segment is planned into the
``import_ledger`` with its chain coordinate (``base`` = the sizes of the segments before it, by
name), and the final live segments are imported at once -- small, so the recent client ids
dedup before the first accepted message. It records the cutover coordinate per stream and is
cheap afterwards: a stat of two files per boot. A non-empty live file at a later boot means a
store-unaware body appended (a downgrade ran): it is rotated and imported the same way, so
nothing a body wrote anywhere is lost; a row the export already placed in the file keeps its
coordinate and is ignored on re-import. Child roots are cut over at their first row, never at
boot.

``run_dialogue_import(root)`` imports the frozen archive as a resumable, idempotent job in the
shape of ``store_import.run_legacy_reconcile`` (the maintenance child runs it): newest segment
first, chat before progress, one segment per transaction, the ledger as the cursor. Coverage is
a fact: ``coverage`` answers whether a coordinate range is imported, and a reader returns "no
older rows" only inside the imported range. The archive stays frozen and is never deleted
(BIBLE P1).

``export_for_older_body(root)`` appends every row the files do not hold yet (``seq >= files_seq``)
back to ``logs/<stream>.jsonl`` in the exact line format, so an older body reads a chain whose
coordinates are the store's; ``store_switch`` calls it before a root is handed to such a body.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ouroboros import dialogue_store as ds
from ouroboros import store
from ouroboros.store import StoreBusy
from ouroboros.utils import jsonl_append_lock_path, utc_now_iso

log = logging.getLogger(__name__)

JOB = "dialogue_import"
JOB_LOCK_NAME = "dialogue_import_job.lock"  # one runner of the archive import per root


# --- the chain's segments --------------------------------------------------------------------

def segment_plan(root: pathlib.Path, stream: str) -> List[Dict[str, Any]]:
    """Every archive segment of ``stream`` by name (the chain's order), with its size and base."""
    prefix = f"{stream}_"
    try:
        with os.scandir(root / "archive") as entries:
            names = sorted(entry.name for entry in entries
                           if entry.name.startswith(prefix) and entry.name.endswith(".jsonl") and entry.is_file())
    except FileNotFoundError:
        return []
    plan, base = [], 0
    for name in names:
        info = os.stat(root / "archive" / name)
        plan.append({"name": name, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "base": base})
        base += info.st_size
    return plan


def rotate_final(root: pathlib.Path, stream: str) -> Optional[pathlib.Path]:
    """Rename a non-empty live ``logs/<stream>.jsonl`` into ``archive/<stream>_<UTC>.jsonl`` under
    the chain's append lock (today's naming, the ``_<n>`` collision suffix included); ``None``
    when there is nothing to rotate. A held append lock is ``StoreBusy``: the next boot retries."""
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    live = root / ds.LIVE_REL[stream]
    try:
        if live.stat().st_size == 0:
            return None
    except FileNotFoundError:
        return None
    lock = jsonl_append_lock_path(live)
    fd = acquire_exclusive_file_lock(lock, timeout_sec=ds.WAIT_SEC, stale_sec=10.0, owner_aware_stale=True)
    if fd is None:
        raise StoreBusy(f"dialogue cutover: the append lock of {live} is held", reason="contention")
    try:
        if not live.exists() or live.stat().st_size == 0:
            return None
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        target = root / "archive" / f"{stream}_{stamp}.jsonl"
        suffix = 0
        while target.exists():
            suffix += 1
            target = root / "archive" / f"{stream}_{stamp}_{suffix}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(live, target)
        live.touch()
        store.fsync_directory(target.parent)
        store.fsync_directory(live.parent)
        return target
    finally:
        release_exclusive_file_lock(lock, fd)


def _gen(data: bytes) -> str:
    """The generation signature a legacy address carries (``utils.jsonl_generation_signature``)."""
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            return hashlib.sha256(line.strip().encode("utf-8", errors="replace")).hexdigest()
    return ""


def segment_rows(data: bytes, base: int) -> Iterator[Tuple[int, int, str, Optional[Dict[str, Any]]]]:
    """``(seq, line number, body, row)`` for every line: ``row`` is None for a blank or malformed
    line (its bytes still count in the coordinates); the body is the exact line without its newline."""
    position = base
    for number, raw in enumerate(data.splitlines(keepends=True), 1):
        line = raw[:-1] if raw.endswith(b"\n") else raw
        if line.endswith(b"\r"):
            line = line[:-1]
        row = None
        if line.strip():
            try:
                value = json.loads(line.decode("utf-8", errors="replace").strip())
            except ValueError:
                value = None
            row = value if isinstance(value, dict) else None
        yield position, number, line.decode("utf-8", errors="replace"), row
        position += len(raw)


# --- the cutover ------------------------------------------------------------------------------

def cutover(root: Any) -> Dict[str, Any]:
    """Files -> store, once per root; afterwards the cheap re-check described above. Returns a report."""
    root = pathlib.Path(root)
    report: Dict[str, Any] = {"created": False}
    if ds.present(root):
        former = ds.retier(root)
        if former:
            report["retiered_from"] = former
    else:
        ds.create(root)
        report["created"] = True
    with store.process_lock(ds.path(root)), store.name_lock(root, ds.IMPORT_LOCK_NAME, timeout_sec=ds.WAIT_SEC, stale_sec=600.0):
        first = ds.meta(root, "cutover") is None
        for stream in ds.STREAMS:
            rotated = rotate_final(root, stream)
            plan = segment_plan(root, stream)
            report[stream] = _plan(root, stream, plan)
            report[stream]["rotated"] = rotated.name if rotated else None
            if rotated is not None:
                report[stream]["imported"] = import_segment(root, stream, rotated.name)
        with ds.transaction(root, write=True) as conn:
            marks = {record["stream"]: dict(record) for record in conn.execute("SELECT * FROM streams")}
            facts = {"at": utc_now_iso(), **{stream: marks[stream] for stream in ds.STREAMS}}
            ds._set_meta(conn, "cutover" if first else "reconciled", facts)
    report["first"] = first
    return report


def _plan(root: pathlib.Path, stream: str, plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Record the chain's segments in the ledger (new ones pending) and move the stream's
    coordinates: ``next_seq`` and ``files_seq`` never below the chain's length. A segment a
    store-unaware body appended WITHOUT the rollback export (its chain place lies beyond what
    the files hold, under coordinates the store already gave to appended rows) is placed after
    ``next_seq`` instead: its rows are never lost, at the price of their chain offsets."""
    total = sum(segment["size"] for segment in plan)
    added = mismatched = shifted = 0
    with ds.transaction(root, write=True) as conn:
        marks = conn.execute("SELECT next_seq, files_seq FROM streams WHERE stream=?", (stream,)).fetchone()
        next_seq, files_seq = int(marks["next_seq"]), int(marks["files_seq"])
        known = {record["name"]: dict(record) for record in conn.execute(
            "SELECT * FROM import_ledger WHERE stream=?", (stream,))}
        end = total
        for segment in plan:
            kept = known.get(segment["name"])
            if kept is None:
                base = segment["base"]
                if files_seq <= base < next_seq:
                    base, shifted = next_seq, shifted + 1
                next_seq = max(next_seq, base + segment["size"])
                end = max(end, base + segment["size"])
                conn.execute("INSERT INTO import_ledger (stream, name, base, size, mtime_ns, state) VALUES (?,?,?,?,?,'pending')",
                             (stream, segment["name"], base, segment["size"], segment["mtime_ns"]))
                added += 1
            elif (kept["base"], kept["size"]) != (segment["base"], segment["size"]) and kept["state"] != "mismatch" \
                    and not (kept["base"] > segment["base"] and kept["size"] == segment["size"]):
                # A frozen segment that changed size or place: its rows may not be where the ledger says
                # (a segment placed after ``next_seq`` keeps its recorded place).
                conn.execute("UPDATE import_ledger SET state='mismatch' WHERE stream=? AND name=?", (stream, segment["name"]))
                mismatched += 1
        conn.execute("UPDATE streams SET next_seq=max(next_seq, ?), files_seq=max(files_seq, ?), "
                     "cutover_seq=coalesce(cutover_seq, ?) WHERE stream=?",
                     (end, total if not shifted else files_seq, total, stream))
    return {"segments": len(plan), "added": added, "mismatched": mismatched, "shifted": shifted, "chain_bytes": total}


# --- importing segments ----------------------------------------------------------------------

def pending_segments(root: Any) -> List[Dict[str, Any]]:
    """The ledger's pending segments in import order: chat before progress, newest first."""
    with ds.transaction(root, write=False) as conn:
        records = conn.execute("SELECT * FROM import_ledger WHERE state='pending' "
                               "ORDER BY CASE stream WHEN 'chat' THEN 0 ELSE 1 END, base DESC").fetchall()
    return [dict(record) for record in records]


def import_segment(root: Any, stream: str, name: str) -> Dict[str, Any]:
    """One frozen segment into the store in ONE transaction: its rows at their chain coordinates,
    the chat rows' legacy addresses, the rooms they touched, the ledger row ``done``. A segment
    already imported (by this process or another) is left alone; one whose size is not the
    ledger's is recorded ``mismatch`` and skipped."""
    root = pathlib.Path(root)
    with ds.transaction(root, write=False) as conn:
        ledger = conn.execute("SELECT * FROM import_ledger WHERE stream=? AND name=?", (stream, name)).fetchone()
    if ledger is None or ledger["state"] != "pending":
        return {"status": "skipped", "state": None if ledger is None else ledger["state"]}
    data = (root / "archive" / name).read_bytes()
    if len(data) != ledger["size"]:
        with ds.transaction(root, write=True) as conn:
            conn.execute("UPDATE import_ledger SET state='mismatch' WHERE stream=? AND name=? AND state='pending'", (stream, name))
        return {"status": "mismatch", "size": len(data), "expected": ledger["size"]}
    gen = _gen(data) if stream == "chat" else ""
    rows, addresses, bad = [], [], 0
    touched: Dict[int, Tuple[Optional[int], str]] = {}
    for seq, number, body, row in segment_rows(data, int(ledger["base"])):
        if row is None:
            bad += bool(body.strip())
            continue
        columns = ds.facts(row)
        rows.append((stream, seq, columns["ts"], columns["chat_id"], columns["task_id"], columns["parent_task_id"],
                     columns["root_task_id"], columns["direction"], columns["kind"], columns["client_message_id"],
                     columns["origin"], body))
        if gen:
            addresses.append((gen, number, seq))
        if columns["chat_id"] is not None:
            last_seq, last_ts = touched.get(columns["chat_id"], (None, ""))
            touched[columns["chat_id"]] = (max(last_seq or -1, seq) if stream == "chat" else last_seq, max(last_ts, columns["ts"]))
    with ds.transaction(root, write=True) as conn:
        current = conn.execute("SELECT state FROM import_ledger WHERE stream=? AND name=?", (stream, name)).fetchone()
        if current is None or current["state"] != "pending":
            return {"status": "skipped", "state": None if current is None else current["state"]}
        conn.executemany(
            "INSERT OR IGNORE INTO dialogue (stream, seq, ts, chat_id, task_id, parent_task_id, root_task_id, direction, "
            "kind, client_message_id, origin, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if addresses:
            conn.executemany("INSERT OR REPLACE INTO legacy_addresses (gen, line, seq) VALUES (?,?,?)", addresses)
        for chat_id, (last_seq, last_ts) in touched.items():
            ds.touch_room(conn, chat_id, last_seq, last_ts, 0)
        conn.execute("UPDATE import_ledger SET state='done', rows=?, bad=?, gen=?, imported_at=? WHERE stream=? AND name=?",
                     (len(rows), bad, gen or None, utc_now_iso(), stream, name))
    return {"status": "done", "rows": len(rows), "bad": bad}


def run_dialogue_import(root: Any, *, progress: Optional[Callable[[dict], None]] = None,
                        should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """The archive import as a named job (``maintenance_jobs`` row ``dialogue_import``): resumable
    through the ledger, idempotent (a done segment is never read again), stoppable between
    segments (the row stays ``pending`` with its cursor), failures recorded with the segment."""
    from ouroboros import maintenance_jobs

    root = pathlib.Path(root)
    with store.name_lock(root, JOB_LOCK_NAME, timeout_sec=ds.WAIT_SEC, stale_sec=600.0):
        cutover(root)  # the store and its plan; a stat of two files when nothing changed

        def record(**facts: Any) -> dict:
            updated = maintenance_jobs.record(root, JOB, **facts)
            if progress is not None:
                progress(updated)
            return updated

        pending = pending_segments(root)
        row = maintenance_jobs.job(root, JOB) or {}
        if not pending:
            return row if row.get("state") == "done" else record(state="done", done=int(row.get("done") or 0), total=int(row.get("done") or 0))
        done = int(row.get("done") or 0)
        if should_stop is not None and should_stop():
            return record(state="pending", done=done, total=done + len(pending))
        segment: Dict[str, Any] = {}
        try:
            record(state="running", done=done, total=done + len(pending), error=None, waiting_on=None)
            for segment in pending:
                if should_stop is not None and should_stop():
                    return record(state="pending")
                outcome = import_segment(root, segment["stream"], segment["name"])
                if outcome["status"] == "done":
                    done += 1
                record(cursor=f"{segment['stream']}:{segment['name']}", done=done)
            return record(state="done", total=done)
        except BaseException as exc:
            maintenance_jobs.record(root, JOB, state="failed" if isinstance(exc, Exception) else "pending",
                                    error=f"{type(exc).__name__}: {exc}",
                                    waiting_on={"segment": segment.get("name"), "stream": segment.get("stream")})
            raise


# --- coverage ---------------------------------------------------------------------------------

def coverage(root: Any, stream: str, seq: Optional[int] = None) -> Dict[str, Any]:
    """Whether every segment below ``seq`` (the whole stream when None) is imported: ``imported``
    False means a reader that found no rows there must say "still importing", never "no rows";
    ``pending`` counts the segments not yet imported below ``seq``, ``mismatch`` the ones it never will."""
    if not ds.present(root):
        return {"imported": not ds.holds_dialogue_files(root), "pending": 0, "mismatch": 0}
    bound = (1 << 62) if seq is None else int(seq)
    with ds.transaction(root, write=False) as conn:
        counts = {record["state"]: record["n"] for record in conn.execute(
            "SELECT state, count(*) AS n FROM import_ledger WHERE stream=? AND state<>'done' AND base<? GROUP BY state",
            (stream, bound))}
    pending, mismatch = int(counts.get("pending", 0)), int(counts.get("mismatch", 0))
    return {"imported": not pending and not mismatch, "pending": pending, "mismatch": mismatch}


def import_status(root: Any) -> Dict[str, Any]:
    """Per stream: segments by state, rows imported, bytes still pending (the health document's job view)."""
    status: Dict[str, Any] = {}
    if not ds.present(root):
        return status
    with ds.transaction(root, write=False) as conn:
        for stream in ds.STREAMS:
            summary = {"segments": 0, "done": 0, "pending": 0, "mismatch": 0, "rows": 0, "pending_bytes": 0}
            for record in conn.execute("SELECT state, count(*) AS n, coalesce(sum(rows), 0) AS rows, sum(size) AS bytes "
                                       "FROM import_ledger WHERE stream=? GROUP BY state", (stream,)):
                summary["segments"] += record["n"]
                summary[record["state"]] = record["n"]
                summary["rows"] += record["rows"]
                if record["state"] == "pending":
                    summary["pending_bytes"] = record["bytes"]
            status[stream] = summary
    return status


def scan_pending(root: Any, chat_id: Optional[int], client_message_id: str) -> Optional[ds.Row]:
    """While the chat archive is still importing, the inbound row an id names in the segments not
    yet imported (newest first; only lines holding the encoded id are parsed). ``None`` when
    the pending segments hold none -- then the store's answer was complete."""
    root = pathlib.Path(root)
    with ds.transaction(root, write=False) as conn:
        pending = conn.execute("SELECT name, base FROM import_ledger WHERE stream='chat' AND state='pending' ORDER BY base DESC").fetchall()
    if not pending:
        return None
    # A writer may have escaped the id (``ensure_ascii``) or not: both spellings are candidates.
    needles = {json.dumps(client_message_id, ensure_ascii=False).encode("utf-8"),
               json.dumps(client_message_id, ensure_ascii=True).encode("utf-8")}
    for record in pending:
        try:
            data = (root / "archive" / record["name"]).read_bytes()
        except OSError:
            continue
        if b'"in"' not in data or not any(needle in data for needle in needles):
            continue
        found = None
        for seq, _number, body, row in segment_rows(data, int(record["base"])):
            if row is None or not any(needle in body.encode("utf-8") for needle in needles):
                continue
            if row.get("direction") == "in" and row.get("client_message_id") == client_message_id \
                    and ds.facts(row)["chat_id"] == chat_id:
                found = ds.Row("chat", seq, row, None)  # the newest row naming the id, as the store answers
        if found is not None:
            return found
    return None


# --- the rollback export ----------------------------------------------------------------------

def export_for_older_body(root: Any) -> Dict[str, Any]:
    """Append every row the chain does not hold yet (``seq >= files_seq``) to ``logs/<stream>.jsonl``
    in the exact line format, root and every descendant drive with a store; then the files hold
    everything (``files_seq = next_seq``). Run with the writers stopped (the switch seam)."""
    from ouroboros.store_import import descendant_drives

    root = pathlib.Path(root)
    report: Dict[str, Any] = {}
    for drive in (root, *descendant_drives(root)):
        if ds.present(drive):
            report[str(drive)] = {stream: _export_stream(drive, stream) for stream in ds.STREAMS}
    return report


def _export_stream(root: pathlib.Path, stream: str) -> Dict[str, Any]:
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    with ds.transaction(root, write=False) as conn:
        marks = conn.execute("SELECT next_seq, files_seq FROM streams WHERE stream=?", (stream,)).fetchone()
        # The rows the files do not hold: appended after ``files_seq`` and not inside an imported segment.
        records = conn.execute(
            "SELECT seq, body FROM dialogue WHERE stream=? AND seq>=? AND NOT EXISTS (SELECT 1 FROM import_ledger "
            "WHERE import_ledger.stream=dialogue.stream AND state='done' AND base<=seq AND seq<base+size) ORDER BY seq",
            (stream, marks["files_seq"])).fetchall()
    if not records:
        return {"rows": 0, "from": marks["files_seq"], "to": marks["next_seq"]}
    live = root / ds.LIVE_REL[stream]
    live.parent.mkdir(parents=True, exist_ok=True)
    lock = jsonl_append_lock_path(live)
    fd = acquire_exclusive_file_lock(lock, timeout_sec=ds.WAIT_SEC, stale_sec=10.0, owner_aware_stale=True)
    if fd is None:
        raise StoreBusy(f"dialogue export: the append lock of {live} is held", reason="contention")
    try:
        data = b"".join(record["body"].encode("utf-8") + b"\n" for record in records)
        handle = os.open(str(live), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(handle, view):]
            os.fsync(handle)
        finally:
            os.close(handle)
        store.fsync_directory(live.parent)
    finally:
        release_exclusive_file_lock(lock, fd)
    with ds.transaction(root, write=True) as conn:
        conn.execute("UPDATE streams SET files_seq=? WHERE stream=?", (marks["next_seq"], stream))
        ds._set_meta(conn, f"export:{stream}", {"at": utc_now_iso(), "from": marks["files_seq"], "to": marks["next_seq"],
                                                "rows": len(records)})
    return {"rows": len(records), "from": marks["files_seq"], "to": marks["next_seq"], "bytes": len(data)}
