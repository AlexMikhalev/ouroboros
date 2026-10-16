"""Persistent SQLite connections: cost, lifetime, durability and old-body compatibility."""
import contextlib
import multiprocessing
import os
import sqlite3
import threading

import pytest

from ouroboros import store, store_import, store_switch, usage_store
from tests.test_store import publish


@pytest.fixture(autouse=True)
def release_connections():
    store.close_connections()
    yield
    store.close_connections()


@pytest.mark.parametrize("money", [False, True])
@pytest.mark.parametrize("tier", [store.TIER_ENFORCED, store.TIER_NAME])
def test_fifty_reads_open_one_connection_per_thread(tmp_path, monkeypatch, money, tier):
    if money:
        from ouroboros import platform_layer
        monkeypatch.setattr(platform_layer, "kernel_file_locks_enforced", lambda _path: tier == store.TIER_ENFORCED)
        usage_store.migrate_from_journal(tmp_path)
    else:
        publish(tmp_path, tier=tier)
    store.close_connections()
    opened, statements, observed, failures = [], [], [], []
    real = sqlite3.connect

    def counted(*args, **kwargs):
        conn = real(*args, **kwargs)
        opened.append(conn)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counted)
    def read():
        try:
            conns = []
            for _ in range(50):
                with usage_store.read(tmp_path) if money else store.open_store(tmp_path, write=False) as txn:
                    assert txn.meta("lock_tier") == tier
                    conns.append(txn.conn)
                assert not txn.conn.in_transaction
            assert all(conn is conns[0] for conn in conns)
            observed.append(conns[0])
        except BaseException as exc:
            failures.append(exc)

    read()
    worker = threading.Thread(target=read)
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and failures == []
    assert len(opened) == 2 and observed[0] is not observed[1]
    assert statements.count("PRAGMA synchronous = FULL") == 2
    assert statements.count("PRAGMA journal_mode = PERSIST") == 2
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observed[1].execute("SELECT 1")  # the thread's end disposes its connections
    assert observed[0].execute("SELECT 1").fetchone()[0] == 1


def test_connection_reuse_keeps_revision_barrier_and_rollback_semantics(tmp_path):
    publish(tmp_path)
    with store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("value", 1)
        conn = txn.conn
    for barrier in (False, True):
        with store.open_store(tmp_path, write=True, barrier=barrier) as txn:
            assert txn.conn is conn and txn.revision() == 1
    with pytest.raises(ValueError):
        with store.open_store(tmp_path, write=True) as txn:
            txn.set_meta("value", 2)
            raise ValueError("rollback")
    with pytest.raises(store.StoreError, match="barrier transaction wrote"):
        with store.open_store(tmp_path, write=True, barrier=True) as txn:
            txn.set_meta("value", 3)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("value") == 1 and txn.revision() == 1
    assert not conn.in_transaction


def test_reused_connection_obeys_each_acquisition_budget(tmp_path):
    publish(tmp_path)
    for budget, expected in ((None, 10000), (0, 0), (0.125, 125), (None, 10000)):
        with store.acquisition_budget(budget) if budget is not None else contextlib.nullcontext():
            with store.open_store(tmp_path, write=False) as txn:
                assert txn.conn.execute("PRAGMA busy_timeout").fetchone()[0] == expected
    with store.admission_acquisition():
        with store.open_store(tmp_path, write=False) as txn:
            assert txn.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_idle_reader_sees_a_later_independent_commit(tmp_path):
    path = publish(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        conn = txn.conn
        assert txn.meta("later") is None
    with contextlib.closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as other:
        other.execute("INSERT INTO meta VALUES ('later', '42')")
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.conn is conn and txn.meta("later") == 42


def test_delete_and_persist_writers_interleave_with_full_durability(tmp_path):
    path = publish(tmp_path)
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as old:
        assert old.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        for number in range(5):
            old.execute("INSERT OR REPLACE INTO meta VALUES ('number', ?)", (str(number),))
            with store.open_store(tmp_path, write=True) as txn:
                assert txn.meta("number") == number
                assert txn.conn.execute("PRAGMA journal_mode").fetchone()[0] == "persist"
                assert txn.conn.execute("PRAGMA synchronous").fetchone()[0] == 2
                assert txn.conn.execute("PRAGMA journal_size_limit").fetchone()[0] == store.JOURNAL_SIZE_LIMIT
                txn.set_meta("reply", number)
            assert old.execute("SELECT value FROM meta WHERE key='reply'").fetchone()[0] == str(number)
        assert old.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_handoff_closes_other_threads_idle_connections_before_rename(tmp_path):
    path = publish(tmp_path)
    ready, release = threading.Event(), threading.Event()
    held, failures = [], []
    def reader():
        try:
            with store.open_store(tmp_path, write=False) as txn:
                held.append(txn.conn)
            ready.set()
            assert release.wait(10)
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=reader)
    worker.start()
    try:
        assert ready.wait(5)
        store.prepare_handoff(path, store.TIER_ENFORCED)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            held[0].execute("SELECT 1")
        assert store.held_descriptor(path) is None
        retired = path.with_name("retired.sqlite")
        os.replace(path, retired)
        with contextlib.closing(sqlite3.connect(retired)) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and failures == []


def test_handoff_refuses_active_transaction_and_succeeds_after_release(tmp_path):
    path = publish(tmp_path)
    with store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("kept", True)
        with pytest.raises(store.StoreBusy):
            store.prepare_handoff(path, store.TIER_ENFORCED)
        assert txn.meta("kept") is True and txn.conn.in_transaction
    store.prepare_handoff(path, store.TIER_ENFORCED)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("kept") is True


@pytest.mark.parametrize("money", [False, True])
def test_export_closes_connections_and_leaves_delete_file(tmp_path, money):
    if money:
        usage_store.migrate_from_journal(tmp_path)
        with usage_store.read(tmp_path) as txn:
            conn = txn.conn
        result = usage_store.export_journal(tmp_path)
        path = tmp_path / "state" / result["store_retired_as"]
    else:
        publish(tmp_path)
        with store.open_store(tmp_path, write=True) as txn:
            conn = txn.conn
            txn.set_meta("phase", store.EXPORTING)
        store_import.export_facts(tmp_path)
        path = tmp_path / "state" / store_switch._retire(tmp_path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with contextlib.closing(sqlite3.connect(path)) as exported:
        assert exported.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert exported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not path.with_name(path.name + "-journal").exists()


def test_replaced_file_drops_the_old_cached_connection(tmp_path):
    path = publish(tmp_path)
    replacement = publish(tmp_path / "new")
    with store.open_store(tmp_path, write=True) as txn:
        old = txn.conn
        txn.set_meta("old", True)
    if os.name == "nt":
        store.close_connections(path)  # Windows cannot replace an open database
    os.replace(replacement, path)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.conn is not old and txn.meta("old") is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        old.execute("SELECT 1")


@pytest.mark.serial
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_closes_parent_cache_and_child_opens_its_own_connection(tmp_path):
    publish(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        inherited = txn.conn
    ctx = multiprocessing.get_context("fork")
    receiving, sending = ctx.Pipe(duplex=False)
    def child():
        try:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                inherited.execute("SELECT 1")
            with store.open_store(tmp_path, write=True) as txn:
                assert txn.conn is not inherited
                txn.set_meta("child", os.getpid())
            sending.send((os.getpid(), None))
        except BaseException as exc:
            sending.send((os.getpid(), repr(exc)))
        finally:
            sending.close()
    process = ctx.Process(target=child)
    process.start()
    sending.close()
    try:
        assert receiving.poll(10)
        pid, failure = receiving.recv()
        process.join(10)
        assert process.exitcode == 0 and failure is None and pid != os.getpid()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            inherited.execute("SELECT 1")
        with store.open_store(tmp_path, write=False) as txn:
            assert txn.conn is not inherited and txn.meta("child") == pid
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        receiving.close()
