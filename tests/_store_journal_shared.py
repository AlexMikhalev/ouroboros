"""What each SQLite journal mode makes busy, and what a reader leaves behind, for store tests.

``store.connect`` gives a store WAL where the engine, the lock tier and the filesystem allow it,
else the rollback journal (PERSIST; ``store._journal_mode``). The two modes block different things:

* a held write lock (``BEGIN IMMEDIATE``) makes another writer's acquisition busy in both;
* a held read makes a rollback-journal COMMIT wait for it to drain; a WAL COMMIT waits for no reader;
* ``BEGIN EXCLUSIVE`` makes every rollback-journal reader busy too, while under WAL it is only the
  write lock: a WAL reader is busy only while another connection holds the database file itself
  exclusively, as one in exclusive locking mode, the last close's checkpoint or a recovery do
  (sqlite.org/wal.html, "Sometimes Queries Return SQLITE_BUSY In WAL Mode").

A WAL reader also writes SQLite's own bookkeeping, none of it a commit: the ``-shm`` index on every
read, the ``-wal``/``-shm`` pair its first open creates and the last close removes, and the header's
journal-format fields a rollback-journal file gets at its first WAL open (sqlite.org/fileformat2.html).
"""
from __future__ import annotations

import contextlib
import pathlib
import sqlite3
import time
from typing import Any, Dict, Tuple

from ouroboros import store

# A database's own journal files: what they hold is the database's committed content.
_JOURNAL_SUFFIXES = ("-wal", "-shm", "-journal")


def use_journal(monkeypatch, mode: str) -> None:
    """Every store this test opens from now on runs in MODE ("PERSIST" or "WAL"), whatever this
    interpreter's SQLite would choose: for a subject that exists in one mode only (a reader holding
    a COMMIT is the rollback journal's), or a test that compares the two. The idle handles opened
    so far close, so the next open turns an existing file into MODE."""
    monkeypatch.setattr(store, "_journal_mode", lambda *_args: mode)
    store.close_connections()


def exclusive_hold(path: Any, *, wait_sec: float = 0.0) -> sqlite3.Connection:
    """A connection inside a transaction under which every other connection's read and write of
    PATH is busy until it rolls back and closes (the caller does both): ``BEGIN EXCLUSIVE`` on a
    rollback-journal file; on a WAL file the same in exclusive locking mode, which takes the file's
    exclusive lock, once this process's idle handles in the folder closed (each WAL handle keeps a
    shared lock on the file while it is open). The file's journal mode is left as it is.
    ``wait_sec`` waits out other holders."""
    path = pathlib.Path(path)
    if not store.wal_file(path):
        conn = store.connect(path, store.TIER_ENFORCED, wait_sec=wait_sec, journal_mode="")
        return _begin(conn, "BEGIN EXCLUSIVE")
    deadline = time.monotonic() + wait_sec
    while True:
        store.close_connections_under(path.parent, idle_only=True)  # a leased handle stays: retried below
        conn = store.connect(path, store.TIER_ENFORCED, journal_mode="")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")  # before this connection's first read
        try:
            return _begin(conn, "BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            if not store.busy(exc) or time.monotonic() >= deadline:
                raise
        time.sleep(0.01)


def read_hold(path: Any, *, wait_sec: float = 0.0) -> sqlite3.Connection:
    """A connection inside a read transaction on PATH: on a rollback journal it holds the next
    COMMIT (which waits for readers to drain); under WAL it blocks nothing."""
    return _begin(store.connect(pathlib.Path(path), store.TIER_ENFORCED, wait_sec=wait_sec), "BEGIN")


def write_hold(path: Any, *, wait_sec: float = 0.0) -> sqlite3.Connection:
    """A connection inside a transaction that makes the next write of PATH busy, the way the file's
    journal mode contends a write, while PATH stays readable: a held read on a rollback journal (the
    writer's COMMIT waits for it to drain), the held write lock under WAL (whose COMMIT waits for no
    reader, so only the next writer's acquisition can be contended)."""
    conn = store.connect(pathlib.Path(path), store.TIER_ENFORCED, wait_sec=wait_sec)
    wal = conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    return _begin(conn, "BEGIN IMMEDIATE" if wal else "BEGIN")


def _begin(conn: sqlite3.Connection, statement: str) -> sqlite3.Connection:
    try:
        conn.execute(statement)
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()  # the transaction's lock, taken now
    except BaseException:
        conn.close()
        raise
    return conn


def committed_content(path: Any) -> Tuple[Any, ...]:
    """A SQLite file's committed content whatever its journal mode: its application id, user
    version, schema and rows, read through an independent connection (the WAL log's frames
    included). Equal content is what "no commit" means; a WAL file's bytes are no such proof,
    since a reader's first open rewrites header fields and a checkpoint moves frames into it."""
    with contextlib.closing(sqlite3.connect(pathlib.Path(path), isolation_level=None)) as conn:
        header = tuple(conn.execute(f"PRAGMA {name}").fetchone()[0] for name in ("application_id", "user_version"))
        return header + tuple(conn.iterdump())


def tree_state(root: Any, *, mtimes: bool = False, directories: bool = True) -> Dict[str, Any]:
    """Every path under ROOT with what a write changes, keyed by its relative POSIX name: a file's
    bytes (and its mtime with ``mtimes``), a SQLite database's ``committed_content`` (its own
    journal files left out: they come and go with readers, and their frames are in that content),
    a directory as ``None`` (with ``mtimes``: its mtime, unless it holds a database, beside which
    SQLite creates and removes the ``-wal``/``-shm`` pair)."""
    root = pathlib.Path(root)
    state: Dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.name.endswith(_JOURNAL_SUFFIXES) and store.is_sqlite_file(path.with_name(path.name.rsplit("-", 1)[0])):
            continue
        if path.is_dir():
            if directories:
                holds_database = any(store.is_sqlite_file(child) for child in path.iterdir())
                state[name] = path.stat().st_mtime_ns if mtimes and not holds_database else None
        elif store.is_sqlite_file(path):
            state[name] = committed_content(path)
        else:
            state[name] = (path.read_bytes(), path.stat().st_mtime_ns) if mtimes else path.read_bytes()
    return state
