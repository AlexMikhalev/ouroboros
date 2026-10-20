"""Stored paths grant no cross-install write authority; isolated and own drives still work."""
import json
import logging
import pathlib
import shutil
from types import SimpleNamespace

import pytest

from ouroboros import install_identity as identity, store
from ouroboros.task_results import load_task_result, write_task_result


def boot(monkeypatch, root):
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    return identity.initialize_install()


@pytest.fixture
def installs(tmp_path, monkeypatch):
    a, b = tmp_path / "A", tmp_path / "B"
    boot(monkeypatch, a)
    write_task_result(a, "task", "completed", result="Original result", artifact_status="ready",
                      root_task_id="task", root_phase_checkpoint={"post_task_synthesis": "pending_once"})
    boot(monkeypatch, b)
    yield a, b
    store.close_connections()


@pytest.mark.parametrize("journal", ["PERSIST", "WAL"])
@pytest.mark.parametrize("service", [False, True])
def test_foreign_store_refuses_before_any_connection_but_local_history_reads(
        installs, monkeypatch, journal, service):
    a, b = installs
    monkeypatch.setattr(store, "_journal_mode", lambda *_: journal)
    with store.open_store(b, write=True):
        pass  # The import owner publishes before service-mode transactions.
    connections, real = [], store.connection

    def connect(path, *args, **kwargs):
        connections.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(store, "connection", connect)
    with pytest.raises(identity.StoreForeign, match="belongs to another Ouroboros install") as refused:
        with store.open_store(a, write=True, service=service):
            pytest.fail("foreign write transaction opened")
    assert refused.value.reason == "foreign_install_root" and connections == []
    with pytest.raises(identity.StoreForeign):
        with store.open_store(a, write=False):
            pytest.fail("read transaction opened a foreign rw connection")
    assert connections == []
    with store.open_store(b, write=True, service=service) as txn:
        txn.set_meta("own_write", journal)
        assert txn.conn.execute("PRAGMA journal_mode").fetchone()[0] == journal.lower()
    with store.open_store(b, write=False) as txn:
        assert txn.meta("own_write") == journal


def test_empty_own_read_does_not_create_install_identity(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    with store.open_store(root, write=False) as txn:
        assert txn.task_ids() == []
    assert not root.exists()


@pytest.mark.parametrize("kind", ["custody", "mail", "projection"])
@pytest.mark.parametrize("layout", ["", "task_drives/task", "state/headless_tasks/task/data"])
def test_foreign_locks_never_create_a_lock_file(installs, kind, layout):
    from ouroboros.task_custody import task_custody_lock, task_mail_lock
    from ouroboros.terminal_projection import _publication_lock

    a, b = installs
    lock = {"custody": task_custody_lock, "mail": task_mail_lock, "projection": _publication_lock}[kind]
    foreign = a / layout
    before = {path.relative_to(a) for path in a.rglob("*")}
    with pytest.raises(identity.StoreForeign):
        with lock(foreign, "task"):
            pytest.fail("foreign lock acquired")
    assert {path.relative_to(a) for path in a.rglob("*")} == before
    with lock(b / layout, "task") as acquired:
        assert acquired


def test_projection_prepare_and_mailbox_refuse_foreign_effects(installs):
    from ouroboros.terminal_projection import _prepare
    from ouroboros.owner_mailbox import _mailbox_path, write_task_message

    a, b = installs
    with pytest.raises(identity.StoreForeign):
        _prepare(a, "task", {}, {})
    before = {path.relative_to(a) for path in a.rglob("*")}
    with pytest.raises(identity.StoreForeign):
        write_task_message(a, "must stay here", "task", source_task_id="sender")
    assert {path.relative_to(a) for path in a.rglob("*")} == before
    assert not _mailbox_path(a, "task").exists()
    assert write_task_message(b, "addressed", "task", source_task_id="sender")
    assert "addressed" in _mailbox_path(b, "task").read_text()


def test_owned_process_set_rejects_foreign_root_and_accepts_own_child(installs):
    from ouroboros import owned_shutdown

    a, b = installs
    entry = {"pid": 123, "purpose": "test", "scope": "task", "owner_task": "task", "fingerprint": {}}
    with pytest.raises(identity.StoreForeign):
        owned_shutdown.record_ledgered_process(a, entry)
    assert not owned_shutdown.owned_processes_path(a).exists()
    child = b / "task_drives" / "task"
    assert owned_shutdown.record_ledgered_process(child, entry)
    assert owned_shutdown.installation_root(child) == b
    assert owned_shutdown.owned_records(b)[0]["drive_root"] == str(child)


def test_copied_owned_process_pointer_never_reaches_the_stop_owner(installs, monkeypatch):
    from ouroboros import owned_shutdown

    a, b = installs
    called = []
    monkeypatch.setattr(owned_shutdown, "_stop_ledgered", lambda *args: called.append(args) or True)
    with pytest.raises(identity.StoreForeign):
        owned_shutdown._stop_entry(b, {"drive_root": str(a), "kind": "supervised"}, 0, set())
    assert called == []
    assert owned_shutdown._stop_entry(b, {"drive_root": str(b), "kind": "supervised"}, 0, set())
    assert len(called) == 1



@pytest.mark.parametrize("kind", ["foreground", "service"])
@pytest.mark.parametrize("pointer", ["drive_root", "record_path"])
def test_copied_registry_paths_never_reach_executor_load_stop_or_forget(installs, monkeypatch, kind, pointer):
    from ouroboros import owned_shutdown, workspace_executor as executor

    a, b = installs
    own_path = b / "state" / executor._PROCESS_STATE_DIR / "own.json"
    foreign = {"record_id": "foreign", "kind": kind, "drive_root": str(b),
               "record_path": str(own_path.with_name("foreign.json"))}
    foreign[pointer] = str(a if pointer == "drive_root" else a / "state" / "processes" / "foreign.json")
    own = {"record_id": "own", "kind": kind, "drive_root": str(b), "record_path": str(own_path)}
    owned_shutdown.owned_processes_path(b).write_text(json.dumps({"records": {"foreign": foreign, "own": own}}))
    loaded, stopped, forgotten = [], [], []
    monkeypatch.setattr(executor, "_FOREGROUND", {})
    monkeypatch.setattr(executor, "_SERVICES", {})
    monkeypatch.setattr(executor, "_load_process_record",
                        lambda path: loaded.append(path) or {"record_type": kind, "id": path.stem})
    monkeypatch.setattr(executor, "_valid_process_record", lambda *a, **k: True)
    monkeypatch.setattr(executor, "_stop_record_process", lambda path, *a, **k: stopped.append(path) or True)
    monkeypatch.setattr(executor, "_forget_process", lambda path: forgotten.append(path))
    cleanup = executor.kill_all_foreground if kind == "foreground" else executor._kill_durable_service_records
    assert len(cleanup(b)) == 1
    assert loaded == stopped == forgotten == [own_path]
    assert "foreign" in {entry["record_id"] for entry in owned_shutdown.owned_records(b)}


@pytest.mark.parametrize("relocation", ["copy", "move", "original"])
def test_legacy_process_import_requires_original_install_provenance(tmp_path, monkeypatch, relocation):
    from ouroboros import owned_shutdown, workspace_executor as executor

    a, b = tmp_path / "A", tmp_path / "B"
    boot(monkeypatch, a)
    payload = {"record_type": "service", "service_id": "task:svc", "task_id": "task", "name": "svc",
               "executor_type": "docker_exec", "executor_id": "docker", "container_name": "bench",
               "backend_pid": "4242", "host_pid": 0}
    path = executor._register_process(a, payload)
    before = path.read_bytes()
    owned_shutdown.owned_processes_path(a).unlink()  # legacy installation, before its one-time import
    if relocation == "copy":
        shutil.copytree(a, b)
    elif relocation == "move":
        a.rename(b)
    else:
        b = a
    boot(monkeypatch, b)
    loaded, real = [], executor._load_process_record
    monkeypatch.setattr(executor, "_load_process_record", lambda path: loaded.append(path) or real(path))
    copied_path = b / path.relative_to(a)
    assert owned_shutdown.import_inherited_records(b) == (1 if relocation == "original" else 0)
    assert loaded == ([copied_path] if relocation == "original" else [])
    mark = json.loads(owned_shutdown.owned_processes_path(b).read_text())["inherited_import"]
    if relocation != "original":
        assert mark["reason_code"] == "foreign_install_root"
        assert not owned_shutdown.owned_records(b)
    assert copied_path.read_bytes() == before
    assert owned_shutdown.import_inherited_records(b) is None
    own_path = executor._register_process(b, payload)
    assert own_path in owned_shutdown.executor_record_paths(b, "service")


def test_foreign_stop_does_not_start_this_installs_shutdown_grace(installs, monkeypatch):
    from ouroboros import owned_shutdown

    a, b = installs
    stop, stamped = owned_shutdown._Stop(), []
    monkeypatch.setattr(owned_shutdown, "_GENERATION_STOP", stop)
    monkeypatch.setattr(owned_shutdown, "_stamp", lambda root, *a, **k: stamped.append(root))
    with pytest.raises(identity.StoreForeign):
        owned_shutdown.begin_owned_stop(a)
    assert stop.deadline is None and not stop.started and stamped == []
    owned_shutdown.begin_owned_stop(b)
    assert stop.deadline is not None and stamped == [b]


@pytest.mark.parametrize("pointer", identity.TASK_ROOT_FIELDS)
def test_foreign_synthesis_member_rests_without_reading_its_authority(installs, monkeypatch, pointer):
    from ouroboros import agent_task_pipeline as pipeline, obligations

    a, b = installs
    write_task_result(b, "task", "completed", result="Copied answer", root_task_id="task",
                      **{pointer: str(a)}, root_phase_checkpoint={"post_task_synthesis": "pending_once"})
    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *a, **k: pytest.fail("foreign dispatch"))
    bodies, real = [], store.Txn.body_text

    def body(txn, tid):
        assert pathlib.Path(txn.root) != a, "foreign task was polled"
        bodies.append(tid)
        return real(txn, tid)

    monkeypatch.setattr(store.Txn, "body_text", body)
    assert pipeline.recover_pending_root_post_task_synthesis(b) == 0
    with store.open_store(b, write=False) as txn:
        saved = txn.load("task")
        mark = txn.get_row("duty_rest", "synthesis_recovery:task")
        revision = txn.revision()
    assert saved["reason_code"] == "foreign_install_root"
    assert str(a) in saved["reason_detail"] and "move it explicitly" in saved["reason_detail"]
    assert mark["waits_on"] == ["foreign_install_root"]
    assert "task" in obligations.members(b, "synthesis")
    bodies.clear()
    for _ in range(3):
        assert pipeline.recover_pending_root_post_task_synthesis(b) == 0
    assert bodies == []
    with store.open_store(b, write=False) as txn:
        assert txn.revision() == revision


def test_projection_duty_rests_a_foreign_saved_child(installs, monkeypatch):
    from ouroboros import terminal_projection as projection

    a, b = installs
    write_task_result(b, "task", "completed", result="Copied result", root_task_id="task",
                      drive_root=str(a), artifact_status="ready")
    assert projection.reconcile_terminal_projections(b) == 0
    with store.open_store(b, write=False) as txn:
        assert txn.get_row("duty_rest", "terminal_projection:task")["waits_on"] == ["foreign_install_root"]
    monkeypatch.setattr(projection, "settle_terminal_projection", lambda *a, **k: pytest.fail("rested member polled"))
    assert projection.reconcile_terminal_projections(b) == 0


def test_headless_refuses_foreign_child_before_copy_or_cleanup(installs, monkeypatch):
    from ouroboros import headless

    a, b = installs
    cache = a / "state" / "code_intel"
    cache.mkdir()
    (cache / "keep").write_text("original")
    monkeypatch.setattr(headless, "copy_child_task_result", lambda *a, **k: pytest.fail("foreign copy"))
    report = headless.prepare_terminal_task_files(b, {"id": "task", "drive_root": str(a)})
    assert report["reason_code"] == "foreign_install_root"
    assert (cache / "keep").read_text() == "original"


def test_own_child_terminal_files_settle_and_bare_root_still_writes(installs, tmp_path):
    from ouroboros import headless
    from ouroboros.post_task_checkpoint import root_checkpoint_roots

    _a, b = installs
    child = b / "state" / "headless_tasks" / "child" / "data"
    write_task_result(child, "child", "completed", result="Own child result", artifact_status="ready")
    report = headless.prepare_terminal_task_files(b, {"id": "child", "drive_root": str(child)})
    assert not report["error"] and report["result"]["result"] == "Own child result"
    assert root_checkpoint_roots(SimpleNamespace(drive_root=child), {"budget_drive_root": str(b)}) == [b]
    bare = tmp_path / "isolated"
    write_task_result(bare, "isolated", "completed", result="Still supported")
    assert identity.owned_root(bare) and not (bare / identity.IDENTITY_PATH).exists()
    assert load_task_result(bare, "isolated")["result"] == "Still supported"


@pytest.mark.parametrize("move", [False, True])
def test_copied_or_moved_root_mints_its_own_identity_once(tmp_path, monkeypatch, caplog, move):
    a, b = tmp_path / "A", tmp_path / "B"
    original = boot(monkeypatch, a)
    if move:
        a.rename(b)
    else:
        shutil.copytree(a, b)
    with caplog.at_level(logging.WARNING, logger=identity.__name__):
        current = boot(monkeypatch, b)
        assert identity.initialize_install() == current
    assert current["id"] != original["id"] and current["root"] == str(b)
    assert len(caplog.records) == 1 and "separate Ouroboros install" in caplog.text
    assert identity.owned_root(b)
    write_task_result(b, "own", "completed", result="Works after relocation")
    if not move:
        assert json.loads((a / identity.IDENTITY_PATH).read_text()) == original
        assert not identity.owned_root(a)
    else:
        assert not a.exists()
        with pytest.raises(identity.StoreForeign):
            write_task_result(a, "must-not-recreate", "completed")
        assert not a.exists()
        c = tmp_path / "C"
        store.close_connections()
        b.rename(c)
        boot(monkeypatch, c)
        assert identity.owned_root(c)
        assert not identity.owned_root(a) and not identity.owned_root(b)
        assert not a.exists() and not b.exists()


def test_unreadable_identity_never_becomes_an_isolated_root(installs):
    a, b = installs
    (a / identity.IDENTITY_PATH).write_text("broken")
    with pytest.raises(store.StoreError, match="identity unreadable"):
        identity.owned_root(a / "task_drives" / "task")
    assert identity.owned_root(b)
