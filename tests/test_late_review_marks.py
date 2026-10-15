"""Late review feedback kept in view as a host mark of its owning room (#1738), end to end.

A real publisher (a late acceptance settlement) persists the full source, then keeps ONE host
mark in the room that owns the work. The next request of that
room carries it through the real memory view and context assembly; another room's request does
not; consciousness reads every room's. Showing, reading, a page seal and a fitted window never
release it; only the integrating mind's ``memory_mark(release_id, reason)`` does, and a replay
never brings it back, also after the index is lost. An unproven room keeps no mark. No test
calls a model or the network.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from types import SimpleNamespace

import pytest

from ouroboros import memory_floor
from ouroboros import memory_view as mv
from ouroboros.artifacts import store_actor_source_bytes
from ouroboros.chronicle_store import ChronicleStore, is_late_review_mark
from ouroboros.late_review_marks import LINE_LIMIT, TEXT_LIMIT, late_review_mark_id, publish_late_review
from ouroboros.tools.chronicle import _chronicle_write, _memory_mark, _memory_read
from ouroboros.tools.registry import ToolContext
from tests import _memory_inventory_shared as shared
from tests._memory_view_context import blocks, room_text, world
from tests._store_shared import seed_result

PRESENCE = {"presence": {"binding_id": "b1", "event": {"provider": "telegram", "conversation_id": "c1"}}}
CHILD = {"delegation_role": "subagent", "parent_task_id": "t1", "root_task_id": "t1"}
NANNY = {**CHILD, "configured_subagent": {"route": {"kind": "agent_session"}}}
WAKE = {"usage_category": "consciousness"}
SECTION = "## Late review feedback (host)"


def _source(root, task_id, text):
    data = json.dumps({"review": text}).encode("utf-8")
    return store_actor_source_bytes(root, task_id, category="context_checkpoints", source_id=f"late-{task_id}",
                                    data=data, extension="json"), data


def _record(root, task_id, chat_id, words, **extra):
    ref, _data = _source(root, task_id, words)
    return {"surface": "task_acceptance", "task_id": task_id, "chat_id": chat_id,
            "publication_id": f"acceptance-late:{task_id}", "settled_at": "2026-10-14T05:00:00+00:00",
            "lines": ["On the delivered version of this answer, reviewers later rejected it.",
                      f"- model/x: rejected it — {words}"],
            "subject": {"panel_id": f"panel-{task_id}", "reviewed_revision": "delivered"},
            "source_ref": ref, **extra}


def _late_marks(root):
    return [mark for mark in ChronicleStore(root).active_marks(None) if is_late_review_mark(mark)]


def _ctx(root, task_id, *, chat=1, meta=None, direct=False):
    return ToolContext(repo_dir=root, drive_root=root, task_id=task_id, current_chat_id=chat,
                       task_metadata=dict(meta or {}), is_direct_chat=direct)


def _release(root, mark_id, *, reason="dealt with it", **ctx):
    return json.loads(_memory_mark(_ctx(root, ctx.pop("task_id", "main1"), **ctx), release_id=mark_id,
                                   reason=reason))


# --- the helper: room, identity, text ------------------------------------------------------------

def test_a_publication_is_one_host_mark_of_its_proven_room_and_a_replay_changes_nothing(tmp_path):
    rooms = shared.world(tmp_path)  # Main, Projects alpha and beta, task "bound" bound to alpha
    seed_result(tmp_path, "pres1", {"status": "completed", "chat_id": 555, "metadata": PRESENCE})
    cases = {"main1": (1, "1"), "bound": (1, str(rooms["alpha"])), "beta1": (rooms["beta"], str(rooms["beta"])),
             "pres1": (555, "555")}
    for task_id, (chat, room) in cases.items():
        record = _record(tmp_path, task_id, chat, f"defect in {task_id}")
        first = publish_late_review(tmp_path, record)
        assert (first.status, first.room_id) == ("saved", room), (task_id, first)
        assert first.mark_id == late_review_mark_id("task_acceptance", task_id, f"acceptance-late:{task_id}")
        assert publish_late_review(tmp_path, record) == first._replace(status="unchanged")
    marks = {mark["review"]["task_id"]: mark for mark in _late_marks(tmp_path)}
    assert sorted(marks) == sorted(cases)
    main = marks["main1"]
    assert main["author"] == {"kind": "host", "operation": "late_review", "surface": "task_acceptance",
                              "task_id": "main1"}
    assert main["target_ref"]["task_id"] == "main1" and main["target_ref"]["kind"] == "task_source"
    assert main["text"].startswith("Late result review of task main1, settled 2026-10-14T05:00:00+00:00.")
    assert "Reviewed: panel_id=panel-main1; reviewed_revision=delivered" in main["text"]
    assert "- model/x: rejected it — defect in main1" in main["text"]


@pytest.mark.parametrize("chat,meta", [(777, None), (0, None), (None, None), (-5, None), (777, {"presence": {}})])
def test_an_unproven_room_keeps_no_mark_and_never_defaults_to_main(tmp_path, chat, meta):
    shared.world(tmp_path)
    seed_result(tmp_path, "other1", {"status": "completed", **({"chat_id": chat} if chat is not None else {}),
                                     **({"metadata": meta} if meta else {})})
    before = ChronicleStore(tmp_path).observation_snapshot()[1]
    outcome = publish_late_review(tmp_path, _record(tmp_path, "other1", chat, "a real defect"))
    assert outcome.status == "not_activated" and outcome.room_id == ""
    assert "not proven" in outcome.detail and "source_handles/context_checkpoints/late-other1-" in outcome.detail
    assert ChronicleStore(tmp_path).observation_snapshot()[1] == before, "nothing was written"


def test_other_facts_under_one_publication_conflict_and_bad_records_write_nothing(tmp_path):
    shared.world(tmp_path)
    record = _record(tmp_path, "main1", 1, "first words")
    assert publish_late_review(tmp_path, record).status == "saved"
    changed = publish_late_review(tmp_path, {**record, "lines": ["another verdict"]})
    assert changed.status == "conflict" and "identity collision" in changed.detail
    assert [mark["text"].count("first words") for mark in _late_marks(tmp_path)] == [1]
    ref = record["source_ref"]
    for bad in ({**record, "lines": ["", " "]}, {**record, "source_ref": {**ref, "sha256": ""}},
                {**record, "source_ref": {"kind": "review_ledger_record", "path": "state/x.json", "sha256": "ab"}},
                {**record, "subject": {"nested": {"a": 1}}}, {**record, "publication_id": ""}, ["not", "a", "map"]):
        assert publish_late_review(tmp_path, bad).status == "invalid"
    read = {"tool": "read_file", "arguments": {"root": "runtime_data", "path": "state/x.json"}}
    other = {**record, "publication_id": "change-late:1", "source_read": read,
             "source_ref": {"kind": "review_ledger_record", "path": "state/x.json", "sha256": "ab"}}
    assert publish_late_review(tmp_path, other).status == "saved", "a non-task source names its own reader"
    assert len(_late_marks(tmp_path)) == 2


def test_a_store_that_cannot_serve_is_a_failure_the_writer_keeps_never_an_exception(tmp_path, monkeypatch):
    from ouroboros.store import StoreError

    shared.world(tmp_path)
    record = _record(tmp_path, "main1", 1, "words")
    for error in (TimeoutError("chronicle publication lock unavailable"), StoreError("migrating")):
        def refuse(self, records, *, scan_state=None, _error=error):
            raise _error
        monkeypatch.setattr(ChronicleStore, "publish", refuse)
        outcome = publish_late_review(tmp_path, record)
        assert outcome.status == "failed" and type(error).__name__ in outcome.detail
    monkeypatch.undo()
    assert publish_late_review(tmp_path, record).status == "saved"


def test_the_text_is_the_writers_words_bounded_with_a_disclosed_omission(tmp_path):
    shared.world(tmp_path)
    record = _record(tmp_path, "main1", 1, "x")
    record["lines"] = ["verdict"] + [f"- seat {n}: " + "w" * 5_000 for n in range(3)]
    publish_late_review(tmp_path, record)
    text = _late_marks(tmp_path)[0]["text"]
    assert len(text) <= TEXT_LIMIT and "⚠️ OMISSION NOTE: truncated at" in text
    assert f"truncated at {LINE_LIMIT} chars; original length 5010" in text


# --- the consumer: the next request of the owning room --------------------------------------------

def test_the_next_request_of_the_owning_room_carries_it_and_another_room_never_does(tmp_path):
    env, memory, rooms = world(tmp_path)
    root = memory.drive_root
    seed_result(root, "pres1", {"status": "completed", "chat_id": 555, "metadata": PRESENCE})
    marks = {}
    for task_id, chat in (("main1", 1), ("bound", 1), ("pres1", 555)):  # "bound" belongs to Project alpha
        marks[task_id] = publish_late_review(root, _record(root, task_id, chat, f"MAIN-DEFECT-{task_id}")).mark_id
    main_c = blocks(env, memory, {"id": "next-main", "chat_id": 1})[2]
    alpha_c = blocks(env, memory, {"id": "next-alpha", "chat_id": rooms["alpha"]})[2]
    assert SECTION in main_c and "MAIN-DEFECT-main1" in main_c
    assert f"memory_mark(release_id='{marks['main1']}', reason=…)" in main_c
    assert "full source (sha256 " in main_c and "memory_read(source_ref=" in main_c
    assert "MAIN-DEFECT-bound" not in main_c and "MAIN-DEFECT-pres1" not in main_c
    assert SECTION in alpha_c and "MAIN-DEFECT-bound" in alpha_c and "MAIN-DEFECT-main1" not in alpha_c
    assert "MAIN-DEFECT-bound" in alpha_c.split(SECTION, 1)[1].split("\n## ", 1)[0], "in its own section"
    wake = room_text(root, {"id": "wake1", "chat_id": 1, "metadata": WAKE})
    assert all(f"MAIN-DEFECT-{task}" in wake for task in marks), "consciousness reads every room's"
    presence = room_text(root, {"id": "pt1", "chat_id": 555, "metadata": PRESENCE})
    assert "MAIN-DEFECT-pres1" in presence and "MAIN-DEFECT-main1" not in presence
    beta = room_text(root, {"id": "tb2", "chat_id": rooms["beta"]})
    assert SECTION not in beta
    child = room_text(root, {"id": "kid7", "chat_id": 1, **CHILD})  # the room's marks, as the child already reads
    assert "MAIN-DEFECT-main1" in child and "MAIN-DEFECT-bound" not in child
    helper_section = child.split(SECTION, 1)[1].split("\n## ", 1)[0]
    assert "the integrating mind releases it" in helper_section and "release_id=" not in helper_section
    assert "until I release it with my reason" in main_c


def test_showing_reading_sealing_and_a_fitted_window_never_release_it(tmp_path):
    from ouroboros import chat_chain, memory_inventory
    from ouroboros.llm_local import _compact_local_text

    shared.world(tmp_path)
    record = _record(tmp_path, "main1", 1, "KEEP-THIS-CRITIQUE")
    mark_id = publish_late_review(tmp_path, record).mark_id
    task = {"id": "next-main", "chat_id": 1}
    for _ in range(2):  # a first show and a second
        assert "KEEP-THIS-CRITIQUE" in room_text(tmp_path, task)
    ctx = _ctx(tmp_path, "root0001")
    assert "KEEP-THIS-CRITIQUE" in _memory_read(ctx, node_id=mark_id)
    assert mark_id in _memory_read(ctx, room_id="1")
    shown = re.search(r"memory_read\(source_ref=(\{.*?\})\)", room_text(tmp_path, task)).group(1)
    source = _memory_read(ctx, source_ref=json.loads(shown))  # the call exactly as the view prints it
    assert f"sha256 {record['source_ref']['sha256'][:12]}" in source and "KEEP-THIS-CRITIQUE" in source
    assert json.loads(shown) == {key: _late_marks(tmp_path)[0]["target_ref"][key]
                                 for key in ("kind", "root", "path", "size", "sha256", "task_id")}
    rows = memory_inventory.open_room_rows(tmp_path, "1")
    sealed = json.loads(_chronicle_write(ctx, kind="page", text="Main so far, sealed.",
                                         covers={"to": chat_chain.format_address(rows[-1][0])}))
    assert sealed["ok"], sealed
    snapshot = mv.capture_memory_view(tmp_path, task, mv.view_spec_for_task(task, tmp_path))
    level = memory_floor.fit_memory_view(snapshot, {"margin": 1, "physical": 1, "budget": 1})
    assert level.addressed, "the window took everything it may take"
    fitted = mv.render_room(snapshot, level)
    assert "KEEP-THIS-CRITIQUE" in fitted and f"release_id='{mark_id}'" in fitted and "memory_read(source_ref=" in fitted
    assert "KEEP-THIS-CRITIQUE" in _compact_local_text(fitted, "dynamic")
    assert [mark["id"] for mark in _late_marks(tmp_path)] == [mark_id]


# --- release: only the integrating mind, with a reason; never resurrected ---------------------------

def test_only_the_integrating_mind_releases_it_with_a_reason(tmp_path):
    shared.world(tmp_path)
    ids = {task: publish_late_review(tmp_path, _record(tmp_path, task, 1, task)).mark_id
           for task in ("m1", "m2", "m3", "m4")}
    for meta in (CHILD, NANNY):
        refused = _release(tmp_path, ids["m1"], task_id="kid1", meta=meta)
        assert refused["reason"] == "not_integrator" and refused["conflict_ids"] == [ids["m1"]]
    assert _release(tmp_path, ids["m1"], reason="")["reason"] == "invalid"
    assert ChronicleStore(tmp_path).release_mark(ids["m1"], {"kind": "host", "operation": "late_review"},
                                                 "the host never decides").reason == "not_integrator"
    assert len(_late_marks(tmp_path)) == 4
    for mark_id, ctx in ((ids["m1"], {"direct": True}), (ids["m2"], {"meta": WAKE}),
                         (ids["m3"], {"chat": 555, "meta": PRESENCE}), (ids["m4"], {})):
        released = _release(tmp_path, mark_id, reason="read the critique; fixed in the follow-up", **ctx)
        assert released["ok"] and released["operation"] == "mark_release", (ctx, released)
    assert _late_marks(tmp_path) == []
    # An ordinary mark keeps its rules: a delegated child still releases its own.
    child_ctx = _ctx(tmp_path, "kid1", meta=CHILD)
    note = json.loads(_chronicle_write(_ctx(tmp_path, "root0001"), kind="note", text="a note"))
    own = json.loads(_memory_mark(child_ctx, text="mine", node_id=note["node_id"]))
    assert json.loads(_memory_mark(child_ctx, release_id=own["mark_id"], reason="done"))["ok"]


def test_a_released_mark_stays_released_across_replay_restart_and_index_loss(tmp_path):
    shared.world(tmp_path)
    record = _record(tmp_path, "main1", 1, "ONCE")
    mark_id = publish_late_review(tmp_path, record).mark_id
    assert _release(tmp_path, mark_id, direct=True)["ok"]
    assert publish_late_review(tmp_path, record).status == "unchanged"
    assert _late_marks(tmp_path) == [] and "ONCE" not in room_text(tmp_path)
    (tmp_path / "memory" / "chronicle" / "index.sqlite3").unlink()  # cold: the journal replays into a new index
    assert publish_late_review(tmp_path, record).status == "unchanged"
    assert _late_marks(tmp_path) == [] and "ONCE" not in room_text(tmp_path)
    kinds = [row["kind"] for row in ChronicleStore(tmp_path).records() if mark_id in (row["id"], row.get("target_id"))]
    assert kinds == ["mark", "mark_release"], "one mark, one release, nothing resurrected"


def test_the_marks_source_reader_survives_retention_packing_its_bytes(tmp_path):
    from ouroboros import evidence_packer
    from ouroboros.artifacts import task_artifact_dir_path

    shared.world(tmp_path)
    seed_result(tmp_path, "main1", {"status": "completed", "chat_id": 1})
    record = _record(tmp_path, "main1", 1, "PACKED-CRITIQUE")
    assert publish_late_review(tmp_path, record).status == "saved"
    loose = task_artifact_dir_path(tmp_path, "main1") / record["source_ref"]["path"]
    report = evidence_packer.run_pass(tmp_path)
    assert not loose.exists(), report  # retention moved the exact bytes into the task's evidence pack
    shown = re.search(r"memory_read\(source_ref=(\{.*?\})\)", room_text(tmp_path)).group(1)
    source = _memory_read(_ctx(tmp_path, "root0001"), source_ref=json.loads(shown))
    assert "PACKED-CRITIQUE" in source and f"sha256 {record['source_ref']['sha256'][:12]}" in source


# --- real publisher: a late acceptance settlement -----------------------------------------------

def _late_wave(tmp_path, monkeypatch, verdict="FAIL", chat_id=1):
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.task_results import write_task_result
    from tests.test_acceptance_async_operation import _released_wave, _SlotModel, _terminal_ctx

    settled = threading.Event()
    module = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])
    original = module._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)
    gates = {"model/a": threading.Event()}
    ctx = _terminal_ctx(tmp_path, task_id="late-root")  # chat 1
    if chat_id != 1:
        write_task_result(tmp_path, "late-root", "completed", chat_id=chat_id, result="The delivered answer.")
    try:
        first = _released_wave(tmp_path, ctx, slots=[ReviewSlot(slot_id="a", model="model/a", effort="high",
                                                                timeout_sec=20)],
                               model=_SlotModel(gates, {"model/a": verdict}), task_id="late-root")
        run = {**json.loads(json.dumps(dataclasses.asdict(first))), "authority": "host_root",
               "panel_id": "panel_1", "binding_hash": "binding-one", "candidate_hash": "c1"}
        ctx._execution_trace["review_runs"].append(run)
        gates["model/a"].set()
        assert settled.wait(10)
        events = []
        while not any(e.get("system_type") == "acceptance_late_settlement" for e in events):
            events.append(ctx.event_queue.get(timeout=10))
    finally:
        gates["model/a"].set()
    return ctx, events


def test_a_late_acceptance_settlement_keeps_its_verdict_in_view_once(tmp_path, monkeypatch):
    from ouroboros.acceptance_settlement import enqueue_late_acceptance_settlement, late_publication_owed
    from ouroboros.task_results import load_task_result

    ctx, events = _late_wave(tmp_path, monkeypatch)
    row = next(e for e in events if e.get("system_type") == "acceptance_late_settlement")
    stored = load_task_result(tmp_path, "late-root")
    panel = stored["review_projection"]["panels"][0]
    marks = _late_marks(tmp_path)
    assert len(marks) == 1 and marks[0]["room_id"] == "1"
    mark = marks[0]
    assert mark["id"] == late_review_mark_id("task_acceptance", "late-root", "acceptance-late:acceptance-subject-one")
    assert row["text"] in mark["text"] and "rejected it" in mark["text"], "the card's own sentence, verbatim"
    assert mark["target_ref"] == {**panel["applied_source_ref"], "task_id": "late-root"}
    source = _memory_read(_ctx(tmp_path, "root0001"), source_ref=mark["target_ref"])
    raw = __import__("ouroboros.artifacts", fromlist=["x"]).read_actor_source_bytes(
        tmp_path, "late-root", panel["applied_source_ref"])
    assert hashlib.sha256(raw).hexdigest() == panel["applied_source_ref"]["sha256"]
    assert f"sha256 {panel['applied_source_ref']['sha256'][:12]}" in source
    assert row["text"].splitlines()[0] in room_text(tmp_path)
    # Every settler of the wave ends in the same publisher: a recovery replay keeps one mark.
    assert enqueue_late_acceptance_settlement(ctx, "late-root", "acceptance-subject-one", stored, panel) == "published"
    assert [m["id"] for m in _late_marks(tmp_path)] == [mark["id"]]
    assert _release(tmp_path, mark["id"], direct=True)["ok"]
    assert enqueue_late_acceptance_settlement(ctx, "late-root", "acceptance-subject-one", stored, panel) == "published"
    assert _late_marks(tmp_path) == [] and not late_publication_owed("late-root", "acceptance-subject-one")


def test_an_unproven_room_publishes_the_card_and_keeps_no_mark(tmp_path, monkeypatch):
    _ctx_unused, events = _late_wave(tmp_path, monkeypatch, verdict="PASS", chat_id=777)
    assert any(e.get("system_type") == "acceptance_late_settlement" for e in events)
    assert _late_marks(tmp_path) == []


def test_a_failed_acceptance_mark_stays_owed_past_a_restart_and_maintenance_places_it(tmp_path, monkeypatch):
    from ouroboros import acceptance_settlement
    from ouroboros.late_review_marks import replay_owed_late_reviews

    real = ChronicleStore.publish
    monkeypatch.setattr(ChronicleStore, "publish", _refuse_publication)
    _ctx_unused, _events = _late_wave(tmp_path, monkeypatch)
    assert _late_marks(tmp_path) == [] and len(_owed_rows(tmp_path)) == 1
    monkeypatch.setattr(ChronicleStore, "publish", real)
    with acceptance_settlement._LATE_LOCK:  # a restart drops the process-local publication duty
        acceptance_settlement._LATE_UNPUBLISHED.clear()
    assert replay_owed_late_reviews(tmp_path)["settled"] == 1 and _owed_rows(tmp_path) == {}
    [mark] = _late_marks(tmp_path)
    assert mark["review"]["publication_id"] == "acceptance-late:acceptance-subject-one" and "rejected it" in mark["text"]


def test_a_failed_mark_keeps_the_late_publication_owed_until_a_replay_places_it(tmp_path, monkeypatch):
    from ouroboros import late_review_marks
    from ouroboros.acceptance_settlement import enqueue_late_acceptance_settlement, late_publication_owed
    from ouroboros.task_results import load_task_result

    real = late_review_marks.publish_late_review
    monkeypatch.setattr(late_review_marks, "publish_late_review",
                        lambda root, record: late_review_marks.LateReviewMark("failed", detail="lock timeout"))
    ctx, _events = _late_wave(tmp_path, monkeypatch)
    assert late_publication_owed("late-root", "acceptance-subject-one") and _late_marks(tmp_path) == []
    monkeypatch.setattr(late_review_marks, "publish_late_review", real)
    stored = load_task_result(tmp_path, "late-root")
    panel = stored["review_projection"]["panels"][0]
    assert enqueue_late_acceptance_settlement(ctx, "late-root", "acceptance-subject-one", stored, panel) == "published"
    assert len(_late_marks(tmp_path)) == 1 and not late_publication_owed("late-root", "acceptance-subject-one")


def _owed_rows(root):
    from ouroboros import store
    from ouroboros.late_review_marks import OWED_SET

    with store.open_store(root, write=False) as txn:
        return txn.set_rows(OWED_SET)


def _refuse_publication(self, records, *, scan_state=None):
    raise TimeoutError("chronicle publication lock unavailable")


def _sweep(root, monkeypatch):
    """The real off-loop maintenance pass, its unrelated steps idle: owed late reviews are replayed there."""
    import supervisor.workspace_preparation as preparation
    from ouroboros import evidence_packer
    from ouroboros import server_maintenance as maintenance

    monkeypatch.setattr(maintenance, "DATA_DIR", root)
    for name in ("_STARTUP_PRUNES_OWED", "_STARTUP_TREES_OWED", "_STARTUP_SOURCE_PRUNES_OWED", "_STARTUP_TEMP_SWEEP_OWED"):
        monkeypatch.setattr(maintenance, name, [False])
    monkeypatch.setattr(maintenance, "_periodic_zombie_reconcile", lambda **_kw: None)
    monkeypatch.setattr(maintenance, "_run_drive_custody_pass", lambda *_a: None)
    monkeypatch.setattr(preparation, "recover_interrupted_preparations", lambda *_a, **_kw: None)
    monkeypatch.setattr(evidence_packer.packer, "tick", lambda *_a, **_kw: None)
    maintenance._run_periodic_reconcile_sweep([0], latch=SimpleNamespace(release=lambda: None))


def test_a_failed_publication_stays_owed_and_one_replay_after_restart_places_it_once(tmp_path, monkeypatch):
    from ouroboros import store
    from ouroboros.late_review_marks import OWED_SET, replay_owed_late_reviews

    shared.world(tmp_path)
    seed_result(tmp_path, "main1", {"status": "completed", "chat_id": 1})
    record = _record(tmp_path, "main1", 1, "OWED-CRITIQUE")
    monkeypatch.setattr(ChronicleStore, "publish", _refuse_publication)
    failed = publish_late_review(tmp_path, record)
    assert failed.status == "failed" and list(_owed_rows(tmp_path)) == [failed.mark_id]
    assert replay_owed_late_reviews(tmp_path)["failed"] == 1 and list(_owed_rows(tmp_path)) == [failed.mark_id]
    monkeypatch.undo()  # a restart: nothing in memory, the owed row is the whole duty
    (tmp_path / "memory" / "chronicle" / "index.sqlite3").unlink(missing_ok=True)
    assert replay_owed_late_reviews(tmp_path) == {"settled": 1, "waiting": 0, "retired": 0, "failed": 0}
    assert [mark["id"] for mark in _late_marks(tmp_path)] == [failed.mark_id] and _owed_rows(tmp_path) == {}
    assert "OWED-CRITIQUE" in room_text(tmp_path)
    assert replay_owed_late_reviews(tmp_path)["settled"] == 0, "a discharged duty replays nothing"
    assert _release(tmp_path, failed.mark_id, direct=True)["ok"]
    with store.open_store(tmp_path, write=True) as txn:  # a stale duplicate of the same duty
        txn.put_row(OWED_SET, failed.mark_id, {"record": record})
    assert replay_owed_late_reviews(tmp_path)["settled"] == 1 and _owed_rows(tmp_path) == {}
    assert _late_marks(tmp_path) == [], "a replay never resurrects a released mark"
