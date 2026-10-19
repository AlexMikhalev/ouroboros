"""The WAL index (``-shm``) of a store this process holds is never opened by a raw read in it.

Under WAL every open connection holds POSIX record locks on the index (its read marks and the
dead-man switch). Closing ANY descriptor of a file drops every lock the process holds on it
(sqlite.org/howtocorrupt.html §2.2): after a raw open and close of the index, another process takes
the TRUNCATE checkpoint those locks forbid and resets the log under a live read transaction, which
then reads that process's later commit (reproduced on macOS with SQLite 3.53.1). The Files panel,
the read tools, artifact capture and copies refuse such an index before opening it (and copy a
store's database through its borrowed descriptor, never a clone that opens it). Only this process's
locks are at stake: the index of a database no connection here holds (another process's, one left
behind) reads as a file, as the ``-wal`` log and any file merely named like a sidecar do. Windows
locks belong to their handle, so nothing is refused there.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys

import pytest

from ouroboros import store
from tests._store_journal_shared import use_journal

pytestmark = pytest.mark.serial  # another interpreter probes the store's locks
posix_locks = pytest.mark.skipif(os.name == "nt", reason="Windows locks belong to their handle: nothing is refused")

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


@posix_locks
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
    elif kind in {"copy", "stream"}:
        assert len(result) == size
    elif kind in {"artifact", "artifact_copy"}:
        assert result["size"] == size
    elif name.startswith("notes"):
        assert result == "plain text\n"


@posix_locks
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


@posix_locks
def test_the_held_index_is_refused_by_any_name_that_reaches_it(tmp_path, monkeypatch):
    """A symbolic link to the index, and on a case-insensitive volume its upper-case name, open the
    same file: each is refused as the index is, and the read transaction keeps its lock."""
    from ouroboros.gateway import files

    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    index = database.with_name(database.name + "-shm")
    link = tmp_path / "linked-index"
    link.symlink_to(index)
    names = [link] + ([index.with_name(database.name + "-SHM")]
                      if index.with_name(database.name + "-SHM").exists() else [])  # a case-insensitive volume
    with store.open_store(tmp_path, write=False):
        for name in names:
            assert store.is_wal_index(name)
            with pytest.raises(store.WalIndexRefused):
                files._read_prefix(name, 4096)
        assert _reset_refused(database)


_HOLDER = ("import sqlite3, sys\n"
           "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
           "conn.execute('PRAGMA journal_mode=WAL')\n"
           "conn.execute('CREATE TABLE IF NOT EXISTS t(v)')\n"
           "conn.execute(\"INSERT INTO t VALUES ('dev server row')\")\n"
           "print('ready', flush=True)\n"
           "sys.stdin.read()\n")  # holds the database open until the test closes its input


def test_another_process_s_index_reads_as_a_file_and_a_workspace_capture_keeps_the_changes(tmp_path):
    """A delegated child's worktree may hold another process's live WAL database (a dev server, a
    killed test run): no connection of this process holds it, so its index is an ordinary untracked
    file there, and the workspace capture keeps the child's source change."""
    from ouroboros import workspace_patch_capture as capture

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "fix.py").write_text("def fixed():\n    return 1\n", encoding="utf-8")
    holder = subprocess.Popen([sys.executable, "-I", "-c", _HOLDER, str(repo / "app.db")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready" and (repo / "app.db-shm").exists()
        _, manifest = capture.write_workspace_patch_artifacts(repo, tmp_path / "capture", task={})
        unheld = not store.is_wal_index(repo / "app.db-shm") and store.is_sqlite_file(repo / "app.db")
    finally:
        holder.stdin.close()
        try:
            holder.wait(timeout=60)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
    assert manifest["status"] == "ready_with_changes", manifest["errors"]
    assert "fix.py" in manifest["untracked_included"] and unheld


@posix_locks
def test_a_folder_copy_leaves_the_index_behind_and_the_copy_opens_whole(tmp_path, monkeypatch):
    from ouroboros.gateway import files

    use_journal(monkeypatch, "WAL")
    database = _live_store(tmp_path)
    copy = tmp_path / "state-copy"
    with store.open_store(tmp_path, write=False):
        files._copy_path(database.parent, copy)
        assert _reset_refused(database)
    copied = {path.name for path in copy.iterdir()}
    assert {"current.sqlite", "current.sqlite-wal"} <= copied and "current.sqlite-shm" not in copied
    with contextlib.closing(sqlite3.connect(copy / "current.sqlite")) as conn:  # its index rebuilt from the copied log
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
