"""A live store's WAL index (its ``-shm``) is never opened by a raw read in this process.

Under WAL every open connection holds POSIX record locks on the index (its read marks and the
dead-man switch). Closing ANY descriptor of a file drops every lock the process holds on it
(sqlite.org/howtocorrupt.html §2.2): after a raw open and close of the index, another process takes
the TRUNCATE checkpoint those locks forbid and resets the log under a live read transaction, which
then reads that process's later commit (reproduced on macOS with SQLite 3.53.1). The Files panel,
the read tools, artifact capture and copies refuse the index before opening it (a copy takes a
store's database as its snapshot through a counted connection, artifact capture through its borrowed
descriptor, never a clone that opens it); the ``-wal`` log carries no lock and reads as any file
does, as does any file merely named like a sidecar.
"""
from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import sys

import pytest

from ouroboros import store
from tests._store_journal_shared import use_journal

pytestmark = pytest.mark.serial  # another interpreter probes the store's locks

_RESET = ("import sqlite3, sys\n"
          "conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=0)\n"
          "print(conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0])\n")
_KINDS = ["preview", "agent_read", "copy", "download", "stream", "artifact", "artifact_copy"]


def _reset_refused(database) -> bool:
    """Whether another process is refused the log reset: true while a read transaction of this
    process holds its read mark in the index."""
    out = subprocess.run([sys.executable, "-c", _RESET, str(database)], capture_output=True, text=True,
                         timeout=60, check=True).stdout.strip()
    return out == "1"


def _live_store(root):
    """A WAL store whose last commit is still in its log."""
    from ouroboros import store_import, task_store

    store_import.ensure_current_store(root)
    task_store.patch_document(root, "t1", lambda _current: {"task_id": "t1", "status": "running"})
    database = store.store_path(root)
    assert store.wal_file(database) and store.log_identity(database) is not None
    return database


def _read(kind, root, target):
    """One in-process reader of TARGET, as the product reads a file someone asked for."""
    from ouroboros.artifacts import copy_artifact_file, stream_artifact_file
    from ouroboros.gateway import files
    from ouroboros.tools import core_file_tools

    if kind == "preview":
        return files._read_prefix(target, 4096)
    if kind == "agent_read":
        with contextlib.suppress(UnicodeDecodeError):  # a log is not text; the read itself happened
            return core_file_tools._read_source_text(target, None)
        return None
    if kind == "copy":
        files._copy_path(target, root / "copied")
        return (root / "copied").read_bytes()
    if kind == "download":
        return files._file_response(target, filename=target.name)
    if kind == "stream":
        return store.read_file_bytes(target)
    if kind == "artifact_copy":  # on Linux a block-sharing clone would open and close the source itself
        return copy_artifact_file(target, root / "copied")
    return stream_artifact_file(target)


@pytest.mark.parametrize("kind", _KINDS)
def test_a_reader_refuses_the_index_and_the_read_transaction_keeps_its_locks(tmp_path, monkeypatch, kind):
    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    index = database.with_name(database.name + "-shm")
    with store.open_store(tmp_path, write=False):  # a read transaction: its read mark is locked
        assert _reset_refused(database)
        if kind == "download":
            response = _read(kind, tmp_path, index)
            assert response.status_code == 403 and b"WAL index" in response.body
        else:
            with pytest.raises(store.WalIndexRefused):
                _read(kind, tmp_path, index)
        assert _reset_refused(database), "a descriptor of the index was opened and closed"
    assert not (tmp_path / "copied").exists()


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("name", ["current.sqlite-wal", "notes-wal", "notes-shm", "current.sqlite"])
def test_the_log_and_ordinary_files_still_read_and_keep_the_locks(tmp_path, monkeypatch, kind, name):
    """The store's own log, a file merely named like a sidecar (one beside a sibling that is no
    database) and the database itself (through its borrowed descriptor) read as before."""
    from starlette.responses import FileResponse, StreamingResponse

    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    target = database.with_name(name)
    if name.startswith("notes"):
        (database.parent / "notes").write_text("a sibling that is not a database", encoding="utf-8")
        target.write_text("plain text\n", encoding="utf-8")
    with store.open_store(tmp_path, write=False):
        size = target.stat().st_size
        result = _read(kind, tmp_path, target)
        assert _reset_refused(database), "reading this file dropped the index's locks"
    if kind == "download":
        assert isinstance(result, (FileResponse, StreamingResponse)) and result.status_code == 200
    elif kind == "preview":
        assert len(result) == min(size, 4096)
    elif kind == "copy" and name == "current.sqlite":  # the database goes as its snapshot, the log's commit in it
        with contextlib.closing(sqlite3.connect(tmp_path / "copied")) as conn:
            assert conn.execute("SELECT task_id, status FROM tasks").fetchall() == [("t1", "running")]
    elif kind in {"copy", "stream"}:
        assert len(result) == size
    elif kind in {"artifact", "artifact_copy"}:
        assert result["size"] == size
    elif name.startswith("notes"):
        assert result == "plain text\n"


def test_the_header_probe_answers_without_opening_the_index(tmp_path, monkeypatch):
    """``is_sqlite_file`` reads a header to answer, so asked about the index it must not look."""
    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    index = database.with_name(database.name + "-shm")
    with store.open_store(tmp_path, write=False):
        assert store.is_wal_index(index) and not store.is_sqlite_file(index)
        assert _reset_refused(database)
    assert store.is_sqlite_file(database) and not store.is_wal_index(database)


def test_the_tests_tree_snapshot_passes_the_index_by_name(tmp_path, monkeypatch):
    """The tests' own "a read writes nothing" snapshot (``tree_state``) walks a folder holding a live
    store: it must leave the index unopened, or it drops the locks a read transaction holds."""
    from tests._store_journal_shared import tree_state

    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    with store.open_store(tmp_path, write=False):
        assert _reset_refused(database)
        snapshot = tree_state(tmp_path, mtimes=True)
        assert _reset_refused(database), "the snapshot opened and closed a descriptor of the index"
    assert "state/current.sqlite" in snapshot and "state/current.sqlite-shm" not in snapshot


def test_a_folder_copy_takes_the_database_whole_and_leaves_its_journal_files(tmp_path, monkeypatch):
    """Each database goes as its snapshot (``tests/test_store_snapshot.py``), so neither its index nor
    its log is copied: a log copied after the file can come from a later generation of it."""
    from ouroboros.gateway import files

    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    copy = tmp_path / "state-copy"
    with store.open_store(tmp_path, write=False):
        files._copy_path(database.parent, copy)
        assert _reset_refused(database)
    copied = {path.name for path in copy.iterdir()}
    assert "current.sqlite" in copied and not {"current.sqlite-wal", "current.sqlite-shm"} & copied
    with contextlib.closing(sqlite3.connect(copy / "current.sqlite")) as conn:  # the log's commit is in the file
        assert conn.execute("SELECT status FROM tasks WHERE task_id='t1'").fetchone() == ("running",)


def test_search_skips_sqlite_journal_log_and_index_files_by_name(tmp_path):
    """The search fallback reads every file it walks with a plain open: SQLite's files stay out of it."""
    from ouroboros.code_search_rg import search_skip_reason

    for name in ("current.sqlite-shm", "current.sqlite-wal", "x.sqlite-journal", "x.db-shm", "x.sqlite3-wal"):
        (tmp_path / name).write_bytes(b"\0")
        assert search_skip_reason(tmp_path / name) == "excluded_name", name
    for name in ("notes-shm", "notes-wal", "notes.txt"):
        (tmp_path / name).write_text("text", encoding="utf-8")
        assert search_skip_reason(tmp_path / name) == "", name
