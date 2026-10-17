"""The one background maintenance process: history-sized named jobs, never on a server thread.

Work whose cost grows with accumulated history (evidence discovery and packing, and the other
named jobs in ``JOBS``) runs in ONE child process at background priority: under one GIL a server
thread doing it turns every request's I/O into waiting. Each job keeps its facts in its
``maintenance_jobs`` row of ``state/current.sqlite``, so "once" is a fact of the installation,
a restart of either process resumes from the recorded facts, and a second run over unchanged
inputs decodes no history body, copies nothing and commits nothing.

Server half: ``child`` (``MaintenanceChild``). The supervisor's maintenance dispatcher calls
``tick`` at the end of each reconcile pass and on its first tick of a process (``owed``;
``request`` asks again, e.g. after a boot scheduled a job). ``tick`` only reserves the one slot:
its thread reads cheap facts (``due``: the job rows, the store revision, the money and delivery
file identities) and spawns the child only when one of them says work may wait, never because
time passed. The spawn is the existing custodied one (``startup_historical_audit.
spawn_maintenance_job``: session custody, its own process group, a parent lifeline; ``stop`` and
``live_children`` serve the Panic and exit census). Queue-owned work (orphan recovery, drive
settlement under the queue lock and the supervisor's liveness probe) stays in the server: a
fresh child owns no queue.

Child half: ``main`` (``python -m ouroboros.maintenance``) lowers its priority and runs ``JOBS``
in order, each asking its own facts again, and prints one bounded JSON report.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import pathlib
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ouroboros import maintenance_jobs, store
from ouroboros.startup_historical_audit import _REPORT_BYTES, finish_maintenance_job, spawn_maintenance_job

log = logging.getLogger(__name__)
PURPOSE = "maintenance"  # the custody purpose and the module the child runs


def _plain(value: Any) -> Any:
    """The JSON form a job row stores, so a fresh reading compares equal to a recorded one."""
    return json.loads(json.dumps(value, default=str))


def _store_facts(root: pathlib.Path) -> Dict[str, Any]:
    """The dispatcher's store reading, one read transaction: the revision and each pending
    promotion member's row signature (indexed flag column; no body)."""
    from ouroboros.obligations import members_in

    with store.open_store(root, write=False) as txn:
        return {"revision": txn.revision(),
                "promotions": {task_id: _plain(txn.row(task_id, "write_seq", "updated_at"))
                               for task_id in members_in(txn, "promotions")}}


def _record_settled(root: pathlib.Path, name: str, baseline: int, *, quiet: bool = True, **facts: Any) -> Dict[str, Any]:
    """Merge FACTS into the job row and record ``quiet_at``: the store revision this very write
    leaves when nothing but this job committed since BASELINE (read before the job looked) and
    the job left nothing to look at again (QUIET), else ``None``, which keeps the job due."""
    key = maintenance_jobs.PREFIX + name
    with store.open_store(root, write=True) as txn:
        current = txn.meta(key)
        row = {**(current if isinstance(current, dict) else {}), **facts}
        row["quiet_at"] = baseline + 1 if quiet and txn.revision() == baseline else None
        row["updated_at"] = _now()
        txn.set_meta(key, row)
    return row


def _now() -> str:
    from ouroboros.utils import utc_now_iso

    return utc_now_iso()


# ---- jobs ------------------------------------------------------------------------------------

def _identity(path: pathlib.Path) -> Optional[List[int]]:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return [stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _activity_waiting_on(root: pathlib.Path) -> Dict[str, Any]:
    """What a failed activity backfill read besides one segment: the registry and the archive."""
    from ouroboros import projects_registry as registry

    return {"registry": _identity(registry.registry_file(root)), "archive": _identity(root / "archive")}


def _activity_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Once per installation; a failed run waits for the registry or the archive to change. A root
    without a registry holds no project to look for, so it is no reason to spawn the child."""
    from ouroboros import projects_registry as registry

    state = row.get("state")
    if state == "done":
        return False
    if state == "failed":
        return row.get("waiting_on") != _activity_waiting_on(root)
    return child or registry.registry_file(root).is_file()


def _activity_sources(root: pathlib.Path) -> List[tuple]:
    """``(family, order key, path)``: the live chat and progress files, then each archived family
    oldest first. Archive names sort chronologically, so a segment rotated later sorts after any
    cursor and an interrupted run resumes past what it read."""
    sources = [("live", f"{index}-{name}", root / "logs" / name)
               for index, name in enumerate(("chat.jsonl", "progress.jsonl"))]
    for family in ("chat", "progress"):
        sources.extend((family, path.name, path) for path in sorted((root / "archive").glob(f"{family}_*.jsonl")))
    return sources


def _activity_scan(path: pathlib.Path, wanted: set) -> set:
    """The WANTED chat ids that have a row in PATH; a digit prefilter skips decoding other rows."""
    tokens = {chat_id: str(chat_id).encode("ascii") for chat_id in wanted}
    found: set = set()
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return found  # rotated meanwhile: the rows sit in a newer segment of the chain
    with handle:
        for raw in handle:
            if not any(token in raw for chat_id, token in tokens.items() if chat_id not in found):
                continue
            try:
                chat_id = int(json.loads(raw).get("chat_id") or 1)
            except (ValueError, TypeError, AttributeError):
                continue
            if chat_id in tokens:
                found.add(chat_id)
                if len(found) == len(tokens):
                    break
    return found


def _activity_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """One pass over the chat chain for the legacy candidates the first look fixed
    (``projects_registry.legacy_activity_candidates``): a project seen is flagged through the
    registry's own ``update_project``; the row records the cursor after every source, then the
    flagged and the empty ids with the coverage read. A project created later is never a
    candidate: its activity is written with its rows (``visible_revision``)."""
    from ouroboros import projects_registry as registry

    row = maintenance_jobs.job(root, "activity_backfill") or {}
    try:
        if not isinstance(row.get("candidates"), dict):
            candidates = registry.legacy_activity_candidates(root)
            row = maintenance_jobs.record(root, "activity_backfill", state="running", error=None, waiting_on=None,
                                          candidates={str(key): pid for key, pid in candidates.items()},
                                          seen=[], cursor={}, done=0, total=None)
        candidates = {int(key): str(pid) for key, pid in row["candidates"].items()}
        seen, cursor = {int(key) for key in row.get("seen") or []}, dict(row.get("cursor") or {})
        done = int(row.get("done") or 0)
        for family, key, path in (_activity_sources(root) if set(candidates) - seen else ()):
            wanted = set(candidates) - seen
            if not wanted:
                break
            if should_stop():
                return {"state": "pending", "items": done}
            if key <= str(cursor.get(family) or ""):
                continue
            seen |= _activity_scan(path, wanted)
            cursor[family], done = key, done + 1
            maintenance_jobs.record(root, "activity_backfill", cursor=cursor, seen=sorted(seen), done=done)
    except Exception as exc:  # the registry or a segment could not be read: wait for them to change
        if not _contention(exc):
            maintenance_jobs.record(root, "activity_backfill", state="failed", error=type(exc).__name__,
                                    waiting_on=_activity_waiting_on(root))
        raise
    flagged = [pid for key, pid in sorted(candidates.items())
               if key in seen and registry.update_project(root, pid, thread_activity_seen=True) is not None]
    empty = sorted(pid for key, pid in candidates.items() if key not in seen)
    maintenance_jobs.record(root, "activity_backfill", state="done", flagged=flagged, empty=empty,
                            coverage={"sources_read": done, "through": cursor}, total=done)
    return {"state": "done", "items": done}


def _promotion_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """A pending promotion member with no remembered refusal at its current row and body: a new
    member, a transient failure, or a row that changed since (``source_retention.RetentionMemo``)."""
    from ouroboros.source_retention import memo_epoch

    observed, epoch = row.get("observed") or {}, memo_epoch()
    return any((observed.get(task_id) or {}).get("signature") != signature
               or (observed.get(task_id) or {}).get("epoch") != epoch
               for task_id, signature in facts["promotions"].items())


def _promotion_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """The pending child-ref promotion retry (copies a settled child's call history and sources
    into the canonical store under the task's custody file lock); its durable observations are
    this job's row, its state is written only when it changes."""
    from ouroboros.observability import retry_pending_child_ref_promotions

    report = retry_pending_child_ref_promotions(root, stop=should_stop)
    row = maintenance_jobs.job(root, "child_ref_promotion") or {}
    state = "pending" if _promotion_due(root, row, _store_facts(root), True) else "done"
    if row.get("state") != state:
        maintenance_jobs.record(root, "child_ref_promotion", state=state)
    return {"state": state, "items": len(report["retried"])}


def _any_entry(directory: pathlib.Path) -> bool:
    """Whether DIRECTORY holds anything (one scandir that stops at the first entry)."""
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def _legacy_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Scheduled (``pending``) by the boot detector or the explicit command (``store_import``);
    an interrupted run (``running``) resumes at its cursor; a failed one waits to be scheduled."""
    return row.get("state") in {"pending", "running"}


def _legacy_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """``store_import.run_legacy_reconcile``: the reconcile of the files a store-unaware body wrote,
    root and every descendant drive, with its own durable cursor in this job's row."""
    from ouroboros import store_import

    reconcile = getattr(store_import, "run_legacy_reconcile", None)
    if reconcile is None:  # a body without the job: the row waits for one that has it
        return {"state": "pending", "items": 0}
    row = reconcile(root, should_stop=should_stop) or {}
    return {"state": str(row.get("state") or "unknown"), "items": int(row.get("done") or 0)}


def _trash_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Drives a settlement moved aside (``state/custody_trash``) wait for their deletion; an entry
    the last run could not remove waits for its identity to change."""
    from ouroboros.task_custody import custody_trash_entries

    stuck = row.get("stuck") or {}
    return any(stuck.get(name) != identity for name, identity in custody_trash_entries(root))


def _trash_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """Delete the moved drives (each was custodied under the queue lock before its move; nothing
    serves or references it), so a child drive's tree is removed off the serving process."""
    from ouroboros.task_custody import remove_custody_trash

    report = remove_custody_trash(root, stop=should_stop)
    state = "failed" if report["stuck"] else "done"
    maintenance_jobs.record(root, "custody_trash", state=state, stuck=report["stuck"], removed=report["removed"])
    return {"state": state, "items": report["removed"]}


def _release() -> str:
    from ouroboros import __version__

    return str(__version__)


def _temp_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Once per installed release: a hard kill's orphaned atomic temps are reaped by the next
    release's first child run. Never by itself a reason to spawn the child."""
    return child and row.get("release") != _release()


def _temp_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """The atomic writers' directories only (``utils.ATOMIC_TEMP_DIRS``), never the whole root or
    ``observability/``; the age guard keeps any in-flight write's temp. The startup sweep of the
    ``tmp_scripts`` fallback (no script can be live then) stays the server's."""
    from ouroboros.utils import sweep_stale_temp_files

    removed = sweep_stale_temp_files(root, scripts=False)
    maintenance_jobs.record(root, "temp_sweep", state="done", release=_release(), removed=removed)
    return {"state": "done", "items": removed}


def _task_rows(root: pathlib.Path) -> int:
    with store.open_store(root, write=False) as txn:
        return int(txn.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def _analyze_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Planner statistics once per store file (this row lives in it), and again once the task
    table grew tenfold since: statistics describe the data's magnitude, so a fresh install's
    would mislead the planner as it grows. Never by itself a reason to spawn the child."""
    if not child:
        return False
    return row.get("state") != "done" or _task_rows(root) >= 10 * max(1, int(row.get("tasks") or 0))


def _analyze_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    with store.open_store(root, write=True) as txn:
        tasks = int(txn.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        txn.conn.execute("ANALYZE")
    maintenance_jobs.record(root, "sqlite_analyze", state="done", tasks=tasks)
    return {"state": "done", "items": tasks}


def _packing_inputs(root: pathlib.Path) -> Dict[str, Any]:
    from ouroboros import evidence_packer

    return {"external": _plain(evidence_packer.external_revision(root)),
            "archive_pending": evidence_packer.archive_pending(root),
            "loose": any(_any_entry(root / rel) for rel in ("observability/calls", "task_results/artifacts"))}


def _packing_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Settled evidence may wait when archived usage segments wait, or when loose evidence exists
    and the store moved since the last quiet pass or the money/delivery files changed identity."""
    inputs = _packing_inputs(root)
    return inputs["archive_pending"] or (inputs["loose"] and (
        row.get("quiet_at") != facts["revision"] or row.get("external") != inputs["external"]))


def _packing_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """Discovery (archive first, then the first changed task stamp) and, when it finds work, one
    packing pass with its own fresh census. A pass that unlinked moved its tasks' directories,
    which owes one more look (it records their new stamps), so it records no quiet fact. A raised
    discovery or pass records nothing either: the job stays due for the next look (per-task
    faults are the pass's own counters)."""
    from ouroboros import evidence_packer, evidence_packs as packs

    baseline = _store_facts(root)["revision"]
    inputs = _packing_inputs(root)
    report: Dict[str, Any] = {"status": "completed", **dict.fromkeys(evidence_packer.COUNTERS, 0)}
    if evidence_packer.packing_pending(root, should_stop):
        report = evidence_packer.run_pass(root)
        packs.record_storage_pack(root, phase=report["status"], **{key: report[key] for key in evidence_packer.COUNTERS})
    if should_stop():
        return {"state": "pending", "items": report["examined"]}
    _record_settled(root, "evidence_packing", baseline, quiet=not report["unlinked"], state="done",
                    external=inputs["external"], packed=report["packed"], examined=report["examined"])
    return {"state": "done", "items": report["examined"]}


def _dialogue_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Archive segments the cutover planned and the import has not read (``import_ledger`` pending);
    a failed segment waits on its own input, which the runner compares, so it is no reason to spawn."""
    from ouroboros import dialogue_import, dialogue_store

    if row.get("state") == "failed":
        return child
    if not dialogue_store.present(root):
        return False
    return any(int(view.get("pending") or 0) for view in dialogue_import.import_status(root).values()
               if isinstance(view, dict))


def _dialogue_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    from ouroboros.dialogue_import import run_dialogue_import

    row = run_dialogue_import(root, should_stop=should_stop) or {}
    return {"state": str(row.get("state") or "unknown"), "items": int(row.get("done") or 0)}


def _hot_facts_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Rows written before the hot-fact columns (or by an older writer since) wait for a classification
    pass (``store_hot_facts.run_hot_facts``); a root without such rows has nothing to do. A failed pass
    waits on its damaged input, which the runner compares itself, so it is no reason to spawn the child."""
    from ouroboros.store_hot_facts import underived

    if row.get("state") == "failed":
        return child
    return underived(root)


def _hot_facts_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    from ouroboros.store_hot_facts import run_hot_facts

    row = run_hot_facts(root, should_stop=should_stop) or {}
    return {"state": str(row.get("state") or "unknown"), "items": int(row.get("done") or 0)}


# name -> (due(root, row, store facts, child), run(root, should_stop)); the child runs them in this
# order. ``child`` is False for the server's look: a job may then decline to be a reason to spawn
# (it still runs whenever the child runs for another reason).
JOBS: Dict[str, tuple] = {
    "legacy_reconcile": (_legacy_due, _legacy_run),
    "dialogue_import": (_dialogue_due, _dialogue_run),  # the owner's history: newest segments first
    "hot_facts": (_hot_facts_due, _hot_facts_run),
    "activity_backfill": (_activity_due, _activity_run),
    "child_ref_promotion": (_promotion_due, _promotion_run),
    "custody_trash": (_trash_due, _trash_run),
    "temp_sweep": (_temp_due, _temp_run),
    "sqlite_analyze": (_analyze_due, _analyze_run),
    "evidence_packing": (_packing_due, _packing_run),  # last: its quiet fact follows every other write
}
JOB_NAMES = tuple(JOBS)


def due(root: Any) -> List[str]:
    """The jobs whose facts say work may wait (cheap reads only; nothing is walked here)."""
    root = pathlib.Path(root)
    rows = maintenance_jobs.jobs(root)
    facts = _store_facts(root)
    return [name for name, (is_due, _run) in JOBS.items() if is_due(root, rows.get(name) or {}, facts, False)]


def _contention(exc: BaseException) -> bool:
    """A held store or lock (current facts, packs index, money): the job stays due, nothing failed."""
    return isinstance(exc, store.StoreBusy) or getattr(exc, "reason", None) == "contention"


def run_job(root: Any, name: str, *, should_stop: Optional[Callable[[], bool]] = None) -> Optional[Dict[str, Any]]:
    """Run one named job in THIS process when its own facts say work may wait (the child's step;
    tests and the cost contract call it); ``None`` when the job is at rest: only its facts were read."""
    root = pathlib.Path(root)
    is_due, run_one = JOBS[name]
    if not is_due(root, maintenance_jobs.job(root, name) or {}, _store_facts(root), True):
        return None
    return run_one(root, should_stop or (lambda: False))


def run(root: Any, *, should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """The child's pass: every due job in order; one job's failure never skips the next."""
    root, stop = pathlib.Path(root), should_stop or (lambda: False)
    report: Dict[str, Any] = {"status": "completed", "jobs": {}}
    for name in JOBS:
        if stop():
            break
        try:
            outcome = run_job(root, name, should_stop=stop)
        except Exception as exc:
            if not _contention(exc):
                log.warning("Maintenance job %s failed", name, exc_info=True)
                report["status"] = "failed"
            outcome = {"state": "pending" if _contention(exc) else "failed", "items": 0}
        if outcome is not None:
            report["jobs"][name] = {"state": str(outcome.get("state") or "unknown"),
                                    "items": int(outcome.get("items") or 0)}
    return report


# ---- the server half -------------------------------------------------------------------------

def _record(data_root: Any, phase: str, **facts: Any) -> None:
    from ouroboros.utils import append_jsonl

    append_jsonl(pathlib.Path(data_root) / "logs" / "supervisor.jsonl",
                 {"ts": _now(), "type": "maintenance", "phase": phase, **facts})


def _report_fields(raw: bytes) -> Dict[str, Any]:
    """The child's report under a closed schema; anything else is ``{}`` (phase unknown)."""
    try:
        value = json.loads(raw) if len(raw) <= _REPORT_BYTES else None
        if not isinstance(value, dict) or value.get("status") not in {"completed", "unknown", "failed"}:
            return {}
        jobs = value.get("jobs")
        if not isinstance(jobs, dict) or set(jobs) - set(JOB_NAMES):
            return {}
        fields: Dict[str, Any] = {"status": value["status"], "jobs": {}}
        for name, outcome in jobs.items():
            state, items = (outcome or {}).get("state"), (outcome or {}).get("items")
            if state not in {*maintenance_jobs.STATES, "unknown"} or type(items) is not int or items < 0:
                return {}
            fields["jobs"][name] = {"state": state, "items": items}
        return fields
    except (ValueError, UnicodeError, TypeError, AttributeError):
        return {}


class MaintenanceChild:
    """The one maintenance child of this server process (module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._owed = True  # the first maintenance tick of a process looks at the job facts once
        self._process = None
        self._thread: Optional[threading.Thread] = None

    def request(self) -> None:
        """Ask the next supervisor maintenance tick to look at the job facts (a job was scheduled)."""
        self._owed = True

    def owed(self) -> bool:
        return self._owed and not self._stopped.is_set()

    def tick(self, data_root: Any, repo_dir: Any) -> bool:
        """Reserve the one slot; True means the dispatch thread started (it spawns the child only
        when a job is due). No filesystem or store access on the caller's thread."""
        with self._lock:
            if self._stopped.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            self._owed = False
            self._thread = threading.Thread(target=self._run, args=(pathlib.Path(data_root), pathlib.Path(repo_dir)),
                                            name="maintenance-child", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stopped.set()
        process = self._process
        if process is not None:
            self._signal(process)

    def live_children(self) -> list:
        """Exit census after Stop: include an unfinished spawn until its owner returns."""
        process, thread = self._process, self._thread
        if process is not None and process.poll() is None:
            return [process]
        return [thread] if thread is not None and thread.is_alive() else []

    @staticmethod
    def _signal(process) -> None:
        with contextlib.suppress(OSError):
            process.kill()  # its own process group: the signal reaches only our child

    def _run(self, data_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        started = time.monotonic()
        try:
            try:
                with store.acquisition_budget(0):  # contention defers to the next look, never waits
                    names = due(data_root)
            except Exception:
                log.debug("Maintenance job facts unavailable; the next look retries", exc_info=True)
                return
            if not names or self._stopped.is_set():
                return
            process = spawn_maintenance_job(data_root, repo_dir, PURPOSE)
            finish_maintenance_job(self, process, data_root, started, _report_fields, _record)
        except Exception as exc:
            with contextlib.suppress(Exception):
                _record(data_root, phase="failed", exception_class=type(exc).__name__,
                        duration_seconds=time.monotonic() - started)
        finally:
            with self._lock:
                self._process = None


child = MaintenanceChild()


# ---- the child half --------------------------------------------------------------------------

def _lower_priority() -> None:
    """Background priority: POSIX nice, the Windows below-normal class; best effort."""
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except (OSError, AttributeError):
        log.debug("Maintenance priority unchanged", exc_info=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--custody-session", default="")
    args = parser.parse_args(argv)
    os.environ["OUROBOROS_DATA_DIR"] = str(pathlib.Path(args.data_root).resolve())
    # Roots are bound before importing readers that capture config globals.
    from ouroboros.process_custody import adopt_session_id, start_parent_lifeline

    adopt_session_id(args.custody_session)
    start_parent_lifeline(label=PURPOSE)
    _lower_priority()
    try:
        report = run(pathlib.Path(args.data_root))
    except Exception as exc:
        report = {"status": "failed", "jobs": {}, "exception_class": type(exc).__name__}
    print(json.dumps({key: report[key] for key in ("status", "jobs")}, separators=(",", ":")), flush=True)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
