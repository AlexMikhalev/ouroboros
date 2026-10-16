"""The cost contract: boot and interactive boundaries do not grow with unrelated history.

BIBLE P5 «Dialogue must not wait for bookkeeping over unrelated history» and P13 «ordinary
enforcement does not enumerate or reconstruct unrelated completed history». Two data roots
(tests/_scale_root.py) hold the SAME current facts; the second holds 30 times the unrelated
history of the first (settled tasks with 20-200 KB bodies, child drives with their own stores,
frozen legacy result files, archive segments of other rooms newer than the requested room's
rows, closed custody records). Each boundary is driven through its real public entry point in a
fresh interpreter (tests/_cost_driver.py) and its work is counted (tests/_cost_meter.py):
connections, statements, SQLite VM steps, file-system calls and bytes read by location, JSON
decodes, child processes and threads. The contract is EQUALITY of those counters between the two
roots; the one allowance is ``VM_ALLOWANCE`` SQLite VM steps for a deeper B-tree (logarithmic in
the history, never a share of it). A full scan hidden inside one SELECT grows the VM steps and
fails; reading the requested rooms' own rows is the same work at every size and passes.

Counting is not timing: this file holds no clock and proves no latency; it makes the next
history-sized reader on these paths fail by name instead of arriving silently.

Where today's code breaks the contract the test is ``xfail(strict=True)`` naming the cause, its
measured numbers and the unit that removes it, so this file is green now and turns red (XPASS)
the moment the fix lands; the marker is then removed. The k=100 variant runs in the opt-in
``integration`` lane (``-m integration -o addopts=''``). ``B6`` is the rest law of the named
maintenance jobs: a second run over unchanged inputs commits nothing and reads no history.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import sqlite3
import subprocess
import sys
from typing import Any, Callable, Dict, Iterable, List

import pytest

from tests._cost_meter import HISTORY, CostMeter, tagged

pytestmark = pytest.mark.scale

REPO = pathlib.Path(__file__).resolve().parents[1]
K_SMALL = 1
CHILD_TIMEOUT_SEC = 900  # a transport bound for one child; the launcher's --timeout stops the whole tree
LOCATIONS = ("root", *HISTORY)
# Counted per location; lock files (``lock``) are left out: how often a lock is probed and retried
# follows the contention between this process's threads at that instant, not the history.
WORK = ("connect", "statements", "begins", "commits", "writes", "json_loads", "json_chars", "popen", "thread_start",
        *(f"{kind}:{where}" for kind in ("connect", "stat", "scandir", "listdir", "open", "os_open", "read_bytes")
          for where in (*LOCATIONS, "outside")))


# Bytes read and characters decoded may differ by the digits of a timestamp or a duration written
# during the boot itself (9.98 s vs 10.02 s); 4 KiB stays below the smallest unit of history here
# (one 20 KB settled body, one ~25 KB archive segment), so one extra history item still breaks it.
BYTE_JITTER = 4096
BYTE_KEYS = ("json_chars", *(f"read_bytes:{where}" for where in (*LOCATIONS, "outside")))


def vm_allowance(large: int, small: int = K_SMALL) -> int:
    """The SQLite VM steps a deeper index may add: logarithmic in the history, never a share of it."""
    return 128 * math.ceil(math.log2(large / small))


# ---- measurement -------------------------------------------------------------------------------

def _child(module: str, args: List[str], env: Dict[str, str], log: pathlib.Path) -> None:
    from ouroboros.platform_layer import kill_process_tree

    with open(log, "wb") as output:
        proc = subprocess.Popen([sys.executable, "-m", module, *args], cwd=REPO, env=env, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=CHILD_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            proc.wait()
            raise
    if code != 0:
        raise AssertionError(f"{module} exited {code}:\n{log.read_text(encoding='utf-8', errors='replace')[-6000:]}")


def measure(base: pathlib.Path, k: int) -> Dict[str, Any]:
    """Build the k-root in one interpreter, then boot and query it in another (cold)."""
    home = base / f"k{k:03d}"
    root, repo = home / "data", home / "repo"
    root.mkdir(parents=True)
    repo.mkdir()
    env = {**os.environ, "OUROBOROS_DATA_DIR": str(root), "OUROBOROS_SETTINGS_PATH": str(root / "settings.json"),
           "OUROBOROS_REPO_DIR": str(repo), "OUROBOROS_HOST_SERVICE_PORT": "0"}
    facts_path, spec_path, out_path = home / "facts.json", home / "spec.json", home / "measured.json"
    _child("tests._scale_root", [str(root), str(k), str(facts_path)], env, home / "build.log")
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    spec_path.write_text(json.dumps({"root": str(root), "facts": facts}), encoding="utf-8")
    _child("tests._cost_driver", [str(spec_path), str(out_path)], env, home / "drive.log")
    measured = json.loads(out_path.read_text(encoding="utf-8"))
    assert not measured["errors"], f"k={k}: the driver failed:\n" + "\n".join(measured["errors"])
    measured["root"] = str(root)
    return measured


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> Dict[str, Any]:
    return measure(tmp_path_factory.mktemp("cost"), K_SMALL)


@pytest.fixture(scope="module", params=[pytest.param(30, id="k30"),
                                        pytest.param(100, id="k100", marks=pytest.mark.integration)])
def measured(request, small, tmp_path_factory) -> Dict[int, Dict[str, Any]]:
    large = request.param
    return {K_SMALL: small, large: measure(tmp_path_factory.mktemp("cost"), large), "large": large}


# ---- the comparison ----------------------------------------------------------------------------

def contract_breaches(small: Dict[str, int], large: Dict[str, int], k_large: int,
                      keys: Iterable[str] = WORK) -> Dict[str, Any]:
    """Every counter that differs between the roots, and VM steps beyond ``vm_allowance``."""
    breaches: Dict[str, Any] = {key: (small.get(key, 0), large.get(key, 0)) for key in keys
                                if abs(small.get(key, 0) - large.get(key, 0)) > (BYTE_JITTER if key in BYTE_KEYS else 0)}
    allowed = vm_allowance(k_large)
    if large.get("vm_steps", 0) > small.get("vm_steps", 0) + allowed:
        breaches["vm_steps"] = (small.get("vm_steps", 0), large.get("vm_steps", 0), f"allowance {allowed}")
    return breaches


def boundary(measured: Dict[Any, Any], tag: str, k: int) -> Dict[str, Any]:
    entry = measured[k].get(tag)
    assert entry is not None, f"boundary {tag} was not reached at k={k}"
    return entry


def assert_flat(measured: Dict[Any, Any], tag: str) -> None:
    large = measured["large"]
    big = boundary(measured, tag, large)
    breaches = contract_breaches(boundary(measured, tag, K_SMALL)["counts"], big["counts"], large)
    touched = sorted(big.get("paths", {}).items(), key=lambda item: -item[1])[:8]
    assert not breaches, (f"{tag} grows with unrelated history (k={K_SMALL} -> k={large}): {breaches}; "
                          f"history touched at k={large}: {touched}")


# ---- B1-B5 and the room ------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "WU3a/WU2a: the lifespan runs projects_registry.reconcile_projects -> _backfill_thread_activity, which opens "
    "every archive chat/progress segment before /api/health can answer (k=1 -> k=30: open:archive 5 -> 92, "
    "read 0.18 -> 4.9 MB, json_loads 535 -> 13585). WU3a turns the scan into a one-time job; WU2a moves the step "
    "after the yield."))
def test_b1_lifespan_before_health_is_flat(measured):
    assert_flat(measured, "B1")
    assert_flat(measured, "B1h")


@pytest.mark.xfail(strict=True, reason=(
    "WU2a: four child processes run before the first /api/health answer at every size: git x2 "
    "(extension_health.code_stamp during the extension reload) and ps x2 (record_service_binding of the host "
    "service; macOS reads process identity through ps). WU2a moves both after the yield."))
def test_b1_no_child_process_before_health(measured):
    for k in (K_SMALL, measured["large"]):
        assert measured[k]["popen_before_health"] == 0, (k, measured[k]["B1"]["names"])


@pytest.mark.xfail(strict=True, reason=(
    "WU1: the boot import re-verifies history before the chat bus exists: store_import.ensure_current_store "
    "stats every frozen task_results/*.json and opens and commits every child drive's store (k=1 -> k=30: "
    "connect 15 -> 131 (child stores 4 -> 91), statements 150 -> 2006, stat under state/headless_tasks "
    "215 -> 4942, under task_results 12 -> 302). Verified: XPASS on wu1/startup 9f3a6d471."))
def test_b2_publication_to_dialogue_is_flat(measured):
    assert_flat(measured, "B2_dialogue")


@pytest.mark.xfail(strict=True, reason=(
    "WU1: the same per-boot import walk precedes supervisor readiness (k=1 -> k=30: connect 70 -> 186, "
    "statements 523 -> 2379, stat under state/headless_tasks 218 -> 4945). Verified: XPASS on wu1/startup 9f3a6d471."))
def test_b2_publication_to_supervisor_ready_is_flat(measured):
    assert_flat(measured, "B2_ready")


@pytest.mark.xfail(strict=True, reason=(
    "WU3b: /api/state reads every owner_questions member one store connection each, including answered "
    "questions of settled tasks retained by quiz_needs_retention (k=1 -> k=30: connect 16 -> 74, "
    "statements 105 -> 482, json_chars 17642 -> 51876)."))
def test_b3_state_cold_is_flat(measured):
    assert_flat(measured, "B3")


@pytest.mark.xfail(strict=True, reason=(
    "WU3b: the same owner_questions reads on every poll (warm, k=1 -> k=30: connect 11 -> 40, statements 73 -> 276)."))
def test_b3_state_warm_is_flat(measured):
    assert_flat(measured, "B3w")


@pytest.mark.xfail(strict=True, reason=(
    "WU-Q1 / room-speed accepted-id record (IMPL_COMMON answer 4=A keeps it out of wave A): the first owner message "
    "after a start folds the whole chat chain in supervisor.message_ingress._resync (k=1 -> k=30: open:archive "
    "3 -> 32, read 0.08 -> 1.68 MB, json_loads 98 -> 2273)."))
def test_b4_owner_message_on_a_cold_process_is_flat(measured):
    assert_flat(measured, "B4")
    for k in (K_SMALL, measured["large"]):
        assert boundary(measured, "B4", k)["counts"].get("open:archive", 0) == 0, k


def test_b4_owner_message_on_a_warm_process_is_flat(measured):
    assert_flat(measured, "B4w")


@pytest.mark.xfail(strict=True, reason=(
    "Unassigned in wave A (R2_opus s6 commit 5, Stop by root/parent ids): the Stop cascade's digest "
    "(_cascade_descendant_rows -> list_task_results) decodes every task body (k=1 -> k=30: json_chars "
    "1.6 MB -> 44.8 MB, json_loads 108 -> 717, vm_steps 7000 -> 32700)."))
def test_b5_stop_of_a_tree_is_flat(measured):
    assert_flat(measured, "B5")


@pytest.mark.xfail(strict=False, reason="room history owned by the room-speed PR")
@pytest.mark.parametrize("tag", ["R_active", "R_late", "R_activew"])
def test_room_history_is_flat(measured, tag):
    assert_flat(measured, tag)


# ---- B6: the rest law of the named maintenance jobs ---------------------------------------------

def second_run(run: Callable[[pathlib.Path], Any], root: pathlib.Path) -> Dict[str, int]:
    """The counters of a job's second run over the inputs its first run left."""
    run(root)
    with CostMeter(root) as meter:
        tagged("rest", lambda: run(root))
    return meter.snapshot("rest")["counts"]


def rest_breaches(counts: Dict[str, int]) -> Dict[str, int]:
    """A second run writes nothing (a read transaction of the store also ends with COMMIT, so the
    written statements are what counts) and opens, lists or reads nothing of the history."""
    keys = ("writes", *(f"{kind}:{where}" for kind in ("open", "os_open", "scandir", "listdir", "read_bytes")
                        for where in HISTORY))
    return {key: counts[key] for key in keys if counts.get(key)}


def _maintenance():
    try:
        from ouroboros import maintenance
    except ImportError:
        return None
    return maintenance


# The jobs the plan names; the maintenance module's own registry joins them once it exists.
JOB_NAMES = tuple(dict.fromkeys(("activity_backfill", "child_ref_promotion", "drive_settlement", "evidence_discovery",
                                 "temp_sweep", "legacy_reconcile", "sqlite_analyze",
                                 *getattr(_maintenance(), "JOBS", {}))))


def job_runner(name: str) -> Callable[[pathlib.Path], Any]:
    """One named job's run in this process: ``maintenance.run_job(root, name)`` once the job is registered,
    WU1's ``store_import.run_legacy_reconcile`` for the legacy reconcile; skipped while absent."""
    maintenance = _maintenance()
    if maintenance is not None and name in getattr(maintenance, "JOBS", {}):
        return lambda root: maintenance.run_job(root, name, should_stop=lambda: False)
    if name == "legacy_reconcile":
        from ouroboros import store_import
        runner = getattr(store_import, "run_legacy_reconcile", None)
        if runner is not None:
            return lambda root: runner(root, progress=None, should_stop=lambda: False)
    pytest.skip(f"maintenance job {name!r} does not exist on this tree yet")


def _rests_at_every_size(measured: Dict[Any, Any], run: Callable[[pathlib.Path], Any], what: str) -> None:
    large = measured["large"]
    rested = {k: second_run(run, pathlib.Path(measured[k]["root"])) for k in (K_SMALL, large)}
    for k, counts in rested.items():
        assert not rest_breaches(counts), (what, k, rest_breaches(counts))
    assert not contract_breaches(rested[K_SMALL], rested[large], large), (what, rested)


@pytest.mark.parametrize("name", JOB_NAMES)
def test_b6_each_job_rests_on_its_second_run(measured, name):
    _rests_at_every_size(measured, job_runner(name), name)


def test_b6_the_maintenance_pass_rests_on_its_second_run(measured):
    maintenance = _maintenance()
    if maintenance is None or not callable(getattr(maintenance, "run", None)):
        pytest.skip("the maintenance child's pass does not exist on this tree yet")
    _rests_at_every_size(measured, lambda root: maintenance.run(root, should_stop=lambda: False), "maintenance.run")


# ---- the instrument itself, both directions --------------------------------------------------------

def _rooms(root: pathlib.Path, rows: int) -> pathlib.Path:
    db = root / "state" / "current.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY, room INTEGER, body TEXT)")
    conn.execute("CREATE INDEX rows_room ON rows(room)")
    conn.executemany("INSERT INTO rows (room, body) VALUES (?, ?)",
                     [(7 if index < 5 else 1000 + index, "x" * 40) for index in range(rows)])
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize("hidden_scan", [False, True])
def test_the_contract_catches_a_scan_hidden_in_one_select(tmp_path, hidden_scan):
    """The same requested rows read through an index stay flat; read through a predicate no index serves,
    the one SELECT walks the whole table and the VM steps grow with it."""
    query = ("SELECT body FROM rows WHERE room + 0 = 7" if hidden_scan else "SELECT body FROM rows WHERE room = 7")
    counted = {}
    for k in (K_SMALL, 30):
        db = _rooms(tmp_path / f"k{k:03d}", 200 * k)
        with CostMeter(tmp_path / f"k{k:03d}") as meter:
            def read():
                conn = sqlite3.connect(db)
                assert len(conn.execute(query).fetchall()) == 5
                conn.close()
            tagged("read", read)
        counted[k] = meter.snapshot("read")["counts"]
    assert counted[K_SMALL]["statements"] == counted[30]["statements"] == 1
    assert bool(contract_breaches(counted[K_SMALL], counted[30], 30)) is hidden_scan, counted


@pytest.mark.parametrize("walks_history", [False, True])
def test_the_contract_catches_a_history_walk(tmp_path, walks_history):
    """A reader that opens only the requested segment is flat; one that walks every segment is not."""
    counted = {}
    for k in (K_SMALL, 30):
        archive = tmp_path / f"k{k:03d}" / "archive"
        archive.mkdir(parents=True)
        for index in range(k):
            (archive / f"chat_{index:04d}.jsonl").write_text('{"chat_id": 9}\n' * 20, encoding="utf-8")
        (archive / "chat_0000.jsonl").write_text('{"chat_id": 7}\n' * 20, encoding="utf-8")

        def read(archive=archive):
            names = sorted(os.listdir(archive)) if walks_history else ["chat_0000.jsonl"]
            for name in names:
                with open(archive / name, encoding="utf-8") as handle:
                    [json.loads(line) for line in handle]

        with CostMeter(tmp_path / f"k{k:03d}") as meter:
            tagged("read", read)
        counted[k] = meter.snapshot("read")["counts"]
    assert bool(contract_breaches(counted[K_SMALL], counted[30], 30)) is walks_history, counted


@pytest.mark.parametrize("rests", [False, True])
def test_the_rest_law_catches_a_job_that_redoes_its_work(tmp_path, rests):
    """A job that records its result reads nothing the second time; one that forgets re-reads the archive."""
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "chat_0001.jsonl").write_text('{"chat_id": 9}\n' * 20, encoding="utf-8")
    done = tmp_path / "state" / "job.done"
    done.parent.mkdir()

    def job(root: pathlib.Path) -> None:
        if rests and done.exists():
            return
        with open(root / "archive" / "chat_0001.jsonl", encoding="utf-8") as handle:
            [json.loads(line) for line in handle]
        done.write_text("done", encoding="utf-8")

    assert bool(rest_breaches(second_run(job, tmp_path))) is not rests
