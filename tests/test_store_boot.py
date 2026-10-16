"""Boot opens current facts with cost independent of frozen history and child stores."""
from __future__ import annotations

import pytest

from ouroboros import store, store_import
from tests.test_store_import import load, result, write_result


def test_flag_probe_work_does_not_grow_with_classified_history(tmp_path):
    work = []
    for count in (50, 500):
        root = tmp_path / str(count)
        store_import.ensure_current_store(root, with_children=False)
        with store.open_store(root, write=True) as txn:
            for index in range(count):
                txn.store_task(result(f"old-{index}"))
        with store.open_store(root, write=False) as txn:
            steps = []
            txn.conn.set_progress_handler(lambda: steps.append(1) or 0, 1)
            assert not store_import._flags_need_derivation(txn)
            txn.conn.set_progress_handler(None, 0)
            work.append(len(steps))
    assert work[0] == work[1]


def test_clean_flags_and_schema_skip_derivation_and_ddl(tmp_path, monkeypatch):
    write_result(tmp_path, result("task"))
    store_import.ensure_current_store(tmp_path)
    monkeypatch.setattr(store_import, "_derive_flag_columns", lambda *_a, **_kw: pytest.fail("derived clean flags"))
    monkeypatch.setattr(store_import, "_upgrade_review_collection", lambda *_: pytest.fail("upgraded current schema"))
    assert store_import.ensure_current_store(tmp_path)["status"] == "unchanged"


@pytest.mark.parametrize("missing", ["index", "column", "null"])
def test_old_writer_flags_are_prepared_before_use(tmp_path, missing):
    if missing == "column":
        from tests.test_review_collection_index import _legacy_store
        _legacy_store(tmp_path, result("task"), owned_root=tmp_path)
    else:
        write_result(tmp_path, result("task"))
        store_import.ensure_current_store(tmp_path)
        with store.open_store(tmp_path, write=True) as txn:
            txn.conn.execute("DROP INDEX tasks_underived_flags" if missing == "index" else
                             "UPDATE tasks SET has_owner_questions=NULL")
    with store.open_store(tmp_path, write=False) as txn:
        assert store_import._flags_need_derivation(txn)
    store_import.ensure_current_store(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        assert not store_import._flags_need_derivation(txn)
        assert txn.row("task", "has_owner_questions") == 0
        assert txn.meta("schema_version") == store.SCHEMA_VERSION
    assert load(tmp_path, "task") == result("task")
