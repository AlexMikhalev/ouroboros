"""A download or copy of a SQLite database from the Files panel is a consistent snapshot.

Under WAL a store's last commits stay in its ``-wal`` log until a checkpoint moves them into the
file, so a byte copy of the file alone misses them (SQLite 3.53.1 and 3.50.4 alike), and a file and
its log copied at two moments can come from two generations of the log. ``store.snapshot_file``
takes one read transaction's ``VACUUM INTO`` instead, in either journal mode; a folder copy takes
each database that way and leaves the database's own journal files behind.
"""
from __future__ import annotations

import asyncio
import contextlib
import sqlite3

import pytest

from ouroboros import store, store_import, task_store
from tests._store_journal_shared import use_journal

_ROWS = [("t1", "running"), ("t2", "running")]


def _live_store(root):
    """A store whose last commits are still in its WAL log (in the file itself under the rollback journal)."""
    store_import.ensure_current_store(root)
    for task_id in ("t1", "t2"):
        row = {"task_id": task_id, "status": "running"}
        task_store.patch_document(root, task_id, lambda _current, row=row: row)
    return store.store_path(root)


def _rows(path):
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT task_id, status FROM tasks ORDER BY task_id").fetchall()


def _body(response) -> bytes:
    async def read():
        return b"".join([chunk async for chunk in response.body_iterator])
    return asyncio.run(read())


@pytest.mark.parametrize("kind", ["download", "copy", "folder_copy"])
@pytest.mark.parametrize("journal", ["PERSIST", "WAL"])
def test_a_download_or_copy_of_a_live_store_holds_its_last_commits(tmp_path, monkeypatch, journal, kind):
    from ouroboros.gateway import files

    use_journal(monkeypatch, journal)
    root = tmp_path / "data"
    database = _live_store(root)
    log = store.log_identity(database)
    assert store.wal_file(database) is (log is not None) is (journal == "WAL")  # WAL: the commits are in the log
    with store.open_store(root, write=False):  # a connection of this process holds the store meanwhile
        if kind == "download":
            copy = tmp_path / "downloaded.sqlite"
            copy.write_bytes(_body(files._file_response(database, filename=database.name)))
        elif kind == "copy":
            copy = tmp_path / "copied.sqlite"
            files._copy_path(database, copy)
        else:
            files._copy_path(database.parent, tmp_path / "state-copy")
            copy = tmp_path / "state-copy" / database.name
            assert not [path.name for path in copy.parent.iterdir() if path.name.startswith(database.name + "-")]
        assert store.log_identity(database) == log and store.wal_file(database) is (journal == "WAL")
    assert _rows(copy) == _ROWS
    assert not [path.name for path in tmp_path.iterdir() if path.name.endswith(".partial")]


@pytest.mark.parametrize("kind", ["download", "copy"])
def test_a_file_sqlite_cannot_read_goes_as_its_bytes(tmp_path, kind):
    """A file with the magic but no database behind it (damaged, foreign) is no snapshot: its bytes go."""
    from ouroboros.gateway import files

    damaged = tmp_path / "damaged.sqlite"
    damaged.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4080)
    assert store.is_sqlite_file(damaged)
    if kind == "download":
        data = _body(files._file_response(damaged, filename=damaged.name))
    else:
        files._copy_path(damaged, tmp_path / "copied.sqlite")
        data = (tmp_path / "copied.sqlite").read_bytes()
    assert data == damaged.read_bytes()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        ["damaged.sqlite"] + (["copied.sqlite"] if kind == "copy" else []))
