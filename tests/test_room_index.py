"""The room index (``ouroboros.room_index``): a derived, disposable map of where each chat and
progress row lies and whose it can be.

It follows the canonical chains (appends, a rotation, a late append to the rotated file), keeps
every coordinate, builds itself again from them when it is damaged or a segment it recorded is
no longer the same file, and the rows it offers a room include every row the room rule admits.
A history read never writes it; without it a room shows its live rows and names the gap.
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import threading
import time
from types import SimpleNamespace

from ouroboros import room_index
from tests._store_shared import seed_result


def _lines(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def _append(root, stream, rows, tail=b""):
    path = root / "logs" / f"{stream}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(_lines(rows) + tail)
    return path


def _archive(root, stream, stamp, rows):
    path = root / "archive" / f"{stream}_{stamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_lines(rows))
    return path


def _chain(root, stream="chat"):
    """``{offset: row}`` of every complete row, read straight from the chain in its order."""
    paths = sorted((root / "archive").glob(f"{stream}_*.jsonl")) + [root / "logs" / f"{stream}.jsonl"]
    rows, base = {}, 0
    for path in paths:
        data = path.read_bytes() if path.exists() else b""
        position = base
        for raw in data.splitlines(keepends=True):
            if raw.endswith(b"\n"):
                rows[position] = json.loads(raw)
            position += len(raw)
        base += len(data)
    return rows


def _indexed(root, stream="chat"):
    db = sqlite3.connect(root / room_index.INDEX_REL)
    try:
        return {offset: (chat, task) for offset, chat, task in db.execute(
            "SELECT offset, chat_id, task_id FROM rows WHERE stream=?", (stream,))}
    except sqlite3.OperationalError:  # the keeper is still creating it
        return {}
    finally:
        db.close()


def _matches_chain(root, stream="chat"):
    chain = _chain(root, stream)
    indexed = _indexed(root, stream)
    assert indexed == {offset: room_index.row_facts(row)[:2] for offset, row in chain.items()}


def test_the_follower_indexes_complete_rows_at_their_chain_coordinates(tmp_path):
    archive = _archive(tmp_path, "chat", "20261001T000000", [{"chat_id": 5, "text": "old"}] * 2)
    _append(tmp_path, "chat", [{"chat_id": 7, "task_id": "t1", "text": "a"}], tail=b'{"chat_id": 7, "te')
    report = room_index.catch_up(tmp_path)
    assert report["pending"] == archive.stat().st_size  # the closed archive waits for the backfill
    assert set(_indexed(tmp_path)) == {archive.stat().st_size}  # the unfinished line is its writer's
    view = room_index.RoomIndexView(tmp_path)
    assert view.frontier("chat") == (archive.stat().st_size, (tmp_path / "logs" / "chat.jsonl").stat().st_size
                                     + archive.stat().st_size - len(b'{"chat_id": 7, "te'))
    view.close()
    _append(tmp_path, "chat", [], tail=b'xt": "b"}\n')
    room_index.catch_up(tmp_path)
    room_index.catch_up(tmp_path)  # a pass with nothing new changes nothing
    room_index.backfill(tmp_path)
    _matches_chain(tmp_path)
    view = room_index.RoomIndexView(tmp_path)
    total = sum(path.stat().st_size for path in (archive, tmp_path / "logs" / "chat.jsonl"))
    assert view.frontier("chat") == (0, total)
    view.close()


def test_a_rotation_and_a_late_append_keep_every_coordinate(tmp_path):
    live = _append(tmp_path, "chat", [{"chat_id": 1, "text": f"m{index}"} for index in range(3)])
    room_index.catch_up(tmp_path)
    before = _indexed(tmp_path)
    rotated = tmp_path / "archive" / "chat_20261016T100000.jsonl"
    rotated.parent.mkdir()
    live.rename(rotated)
    with rotated.open("ab") as handle:  # a writer that still held the old file
        handle.write(_lines([{"chat_id": 2, "text": "late"}]))
    _append(tmp_path, "chat", [{"chat_id": 3, "text": "new live"}])
    assert room_index.catch_up(tmp_path)["pending"] == 0  # the rotated file was begun while live
    after = _indexed(tmp_path)
    assert all(after[offset] == facts for offset, facts in before.items())
    _matches_chain(tmp_path)


def test_a_damaged_index_or_a_changed_segment_is_built_again(tmp_path):
    _archive(tmp_path, "chat", "20261001T000000", [{"chat_id": 5, "text": "a"}])
    live = _append(tmp_path, "chat", [{"chat_id": 6, "text": "b"}])
    room_index.catch_up(tmp_path)
    room_index.backfill(tmp_path)
    (tmp_path / room_index.INDEX_REL).write_bytes(b"not a database" * 100)
    room_index.catch_up(tmp_path)
    room_index.backfill(tmp_path)
    _matches_chain(tmp_path)
    # A live file replaced by another one under its name (a restore) is indexed again; the
    # archives before it stay indexed.
    live.unlink()
    _append(tmp_path, "chat", [{"chat_id": 8, "task_id": "other", "text": "restored" * 4}])
    assert room_index.catch_up(tmp_path)["pending"] == 0
    _matches_chain(tmp_path)
    # An archive that is not the file the index recorded (another name and file at its place).
    (tmp_path / "archive" / "chat_20261001T000000.jsonl").unlink()
    _archive(tmp_path, "chat", "20261002T000000", [{"chat_id": 9, "text": "z"}, {"chat_id": 9, "text": "y"}])
    room_index.catch_up(tmp_path)
    room_index.backfill(tmp_path)
    _matches_chain(tmp_path)


def test_backfill_indexes_the_newest_archives_first(tmp_path):
    archives = [_archive(tmp_path, "progress", f"2026100{index}T000000", [{"chat_id": 1, "content": f"p{index}"}])
                for index in range(4)]
    _append(tmp_path, "progress", [{"chat_id": 1, "content": "live"}])
    room_index.catch_up(tmp_path)
    original = room_index._BACKFILL_BYTES
    try:
        room_index._BACKFILL_BYTES = 1  # one archive per writer turn
        stop = threading.Event()
        seen = []
        real = room_index._index_segment

        def recording(db, chain, stream, base, indexed):
            seen.append(base)
            if len(seen) == 2:
                stop.set()
            return real(db, chain, stream, base, indexed)

        room_index._index_segment = recording
        room_index.backfill(tmp_path, stop=stop)
    finally:
        room_index._BACKFILL_BYTES = original
        room_index._index_segment = real
    sizes = [path.stat().st_size for path in archives]
    assert seen == [sum(sizes[:3]), sum(sizes[:2])]  # the newest two, newest first
    view = room_index.RoomIndexView(tmp_path)
    assert view.frontier("progress")[0] == sum(sizes[:2])  # older rows still wait
    view.close()


def _room_world(root):
    """Two Projects, Main and a transport chat over one chain, with every way the room rule
    admits a row: its chat, a bound task, a descendant by the store's lineage, a row naming its
    parent or root, a task admitted in the Project's chat, an owner row a binding names, and a
    Project question Main mirrors."""
    from ouroboros.project_dialogue import build_owner_message_ref
    from ouroboros.projects_registry import bind_task_to_project, create_project

    first, second = create_project(root, "first", name="First"), create_project(root, "second", name="Second")
    p1, p2 = first["chat_id"], second["chat_id"]
    ask = "Please start the first project"
    bind_task_to_project(root, "b1", first["id"], origin={"ref": build_owner_message_ref(
        chat_id=1, client_message_id="ask-1", ts="2026-10-01T00:00:00Z", text=ask), "text": ask})
    bind_task_to_project(root, "b2", second["id"], origin={"absent": "system"})
    bind_task_to_project(root, "cross", second["id"], origin={"absent": "system"})
    for task, facts in {"b1": {}, "c1": {"parent_task_id": "b1"}, "g1": {"parent_task_id": "c1"},
                        "r1": {"root_task_id": "b1"}, "cross": {"parent_task_id": "b1"}, "b2": {},
                        "x1": {}, "admitted": {"chat_id": p1}}.items():
        seed_result(root, task, {"task_id": task, "status": "completed", "chat_id": 1, **facts})
    rng = random.Random(7)
    chats = [None, 1, 55, p1, p2, "junk"]
    tasks = [None, "b1", "c1", "g1", "r1", "cross", "b2", "x1", "admitted", "rowkid", "unknown"]
    kinds = [None, "quiz", "project_started", "task_summary", "routing_refusal"]
    rows = {"chat": [], "progress": []}
    for stream in rows:
        for index in range(240):
            row = {"ts": f"2026-10-01T00:{index // 60:02d}:{index % 60:02d}Z", "chat_id": rng.choice(chats)}
            task = rng.choice(tasks)
            if task:
                row["task_id"] = task
            if task == "rowkid":
                row[rng.choice(["parent_task_id", "root_task_id"])] = "b1"
            kind = rng.choice(kinds)
            if kind:
                row["type"] = kind
                if kind == "quiz":
                    row["quiz"] = {"quiz_id": f"q{index}", "state": "open"}
            if stream == "chat":
                row.update(direction=rng.choice(["in", "out", "system"]), text=f"row {index}")
            else:
                row["content"] = f"step {index}"
            rows[stream].append(row)
        rows[stream][5] = {"ts": "2026-10-01T00:00:00Z", "chat_id": 1, "direction": "in", "text": ask,
                           "client_message_id": "ask-1"} if stream == "chat" else rows[stream][5]
        rows[stream][6] = {"chat_id": p1, "task_id": "admitted", "direction": "out", "text": "admitted here"}
        rows[stream][7] = {"chat_id": 1, "task_id": "admitted", "direction": "out", "text": "admitted elsewhere"}
        rows[stream][8] = {"chat_id": 1, "task_id": "rowkid", "parent_task_id": "b1", "direction": "out", "text": "kid"}
        for part in range(3):
            _archive(root, stream, f"2026100{part}T000000", rows[stream][part * 60:(part + 1) * 60])
        _append(root, stream, rows[stream][180:])
    return [1, 55, p1, p2], ask


def test_the_rows_offered_to_a_room_include_every_row_the_room_rule_admits(tmp_path):
    from ouroboros.gateway.history import _make_thread_filter, _project_history_context, _stored_chat_id

    rooms_to_read, ask = _room_world(tmp_path)
    room_index.catch_up(tmp_path)
    room_index.backfill(tmp_path)
    narrowed = 0
    for thread in rooms_to_read:
        projects, refs, _notes, bindings, rooms, _classified = _project_history_context(tmp_path, thread)
        admits = _make_thread_filter(thread, projects, refs, rooms)
        spec = room_index.room_spec(tmp_path, thread, projects, refs, bindings)
        view = room_index.RoomIndexView(tmp_path)
        for stream in room_index.STREAMS:
            offered = {locator[0] for locator in view.candidates(stream, spec, before=1 << 40, limit=1 << 20)}
            chain = _chain(tmp_path, stream)
            members = {offset for offset, row in chain.items() if admits(_stored_chat_id(row.get("chat_id"), 1), row)}
            assert members and members <= offered, (thread, stream, sorted(members - offered)[:5])
            narrowed += len(offered) < len(chain)
            if thread == rooms_to_read[2] and stream == "chat":  # every admission path is exercised
                texts = {chain[offset].get("text") for offset in members}
                assert {ask, "admitted here", "admitted elsewhere", "kid"} <= texts, texts
        view.close()
    assert narrowed >= 4  # a Project is offered its own rows, not the whole chain


def _history(root, chat_id):
    from ouroboros.gateway.history import make_chat_history_endpoint

    response = asyncio.run(make_chat_history_endpoint(root)(SimpleNamespace(query_params={"chat_id": str(chat_id)})))
    return response.status_code, json.loads(response.body)


def test_a_read_never_writes_the_index_and_names_what_it_cannot_reach_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(room_index, "room_rows", room_index.room_rows.passive)
    _append(tmp_path, "chat", [{"chat_id": 1, "direction": "in", "text": "fresh install"}])
    status, page = _history(tmp_path, 1)
    assert status == 200 and [row["text"] for row in page["messages"]] == ["fresh install"]
    assert page["window"]["complete"] is True
    _archive(tmp_path, "chat", "20261001T000000", [{"chat_id": 1, "direction": "in", "text": "archived"}])
    status, page = _history(tmp_path, 1)
    assert status == 200 and [row["text"] for row in page["messages"]] == ["fresh install"]
    assert page["window"]["complete"] is False and "chat_room_index_building" in page["window"]["truncated_by"]
    assert not (tmp_path / room_index.INDEX_REL).exists()
    room_index.catch_up(tmp_path)
    room_index.backfill(tmp_path)
    status, page = _history(tmp_path, 1)
    assert [row["text"] for row in page["messages"]] == ["archived", "fresh install"]
    assert "chat_room_index_building" not in page["window"]["truncated_by"]


def _wait(condition, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_the_keeper_follows_the_chains_and_hands_a_large_backfill_to_its_child(tmp_path, monkeypatch):
    monkeypatch.setattr(room_index, "_FOLLOW_SEC", 0.05)
    _archive(tmp_path, "chat", "20261001T000000", [{"chat_id": 1, "text": "archived"}])
    _append(tmp_path, "chat", [{"chat_id": 1, "text": "live"}])
    keeper = room_index.RoomIndexKeeper()
    keeper.start(tmp_path, tmp_path)
    try:  # a few archived bytes are backfilled by the follower itself
        assert _wait(lambda: (tmp_path / room_index.INDEX_REL).exists() and len(_indexed(tmp_path)) == 2)
        _append(tmp_path, "chat", [{"chat_id": 1, "text": "appended"}])
        assert _wait(lambda: len(_indexed(tmp_path)) == 3)
    finally:
        keeper.stop()
        keeper._thread.join(5)
    _matches_chain(tmp_path)

    children = []
    monkeypatch.setattr(room_index, "_BACKFILL_BYTES", 1)
    monkeypatch.setattr(room_index.RoomIndexKeeper, "_run_child", lambda self, root, repo: children.append(root))
    _archive(tmp_path, "chat", "20261002T000000", [{"chat_id": 1, "text": "more"}] * 3)
    keeper = room_index.RoomIndexKeeper()
    keeper.start(tmp_path, tmp_path)
    try:
        assert _wait(lambda: children == [tmp_path])
    finally:
        keeper.stop()
        keeper._thread.join(5)
    assert not keeper._thread.is_alive()


def test_the_child_report_keeps_only_its_known_fields():
    assert room_index._report_fields(b'{"status": "completed", "chat_archives": 3, "progress_archives": 4, "x": 1}') == {
        "status": "completed", "chat_archives": 3, "progress_archives": 4}
    assert room_index._report_fields(b"{") == {} and room_index._report_fields(b'{"status": "odd"}') == {}


def test_a_bad_line_is_named_by_every_room_whose_read_covers_it(tmp_path):
    """Whose a line that is not a JSON object is cannot be known: every room reading across it
    names the parse gap (as a read of the bytes always did), and the follower keeps it indexed."""
    from ouroboros.projects_registry import create_project

    chat = create_project(tmp_path, "covered", name="Covered")["chat_id"]
    _archive(tmp_path, "chat", "20261001T000000", [{"chat_id": chat, "direction": "out", "text": "own"}])
    archive = tmp_path / "archive" / "chat_20261002T000000.jsonl"
    archive.write_bytes(_lines([{"chat_id": 1, "text": "foreign"}]) + b'{"chat_id": 1, "text": "torn\n')
    _append(tmp_path, "chat", [{"chat_id": chat, "direction": "out", "text": "newest"}], tail=b"[1, 2]\n")
    room_index.catch_up(tmp_path)
    room_index.backfill(tmp_path)
    before = _indexed(tmp_path)
    assert len(before) == 5  # the two bad lines are indexed with the three rows
    room_index.catch_up(tmp_path)
    assert _indexed(tmp_path) == before  # a bad newest live line is never mistaken for a replaced file
    for room in (chat, 1):
        status, page = _history(tmp_path, room)
        assert status == 200 and "chat_malformed_jsonl" in page["window"]["truncated_by"], (room, page["window"])
    status, page = _history(tmp_path, chat)
    assert [row["text"] for row in page["messages"]] == ["own", "newest"]
