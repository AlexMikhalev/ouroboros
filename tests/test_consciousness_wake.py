"""The wake-up message and envelope (Background Consciousness redesign P2), and the
server's projection of the alarm clock's snapshot into one honest status line."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from ouroboros import consciousness_wake as wake

REPO = pathlib.Path(__file__).resolve().parents[1]
T0 = 1_800_000_000.0


def _iso(ts):
    return wake._iso(ts)


def _result(root, task_id, *, status="completed", ts, cost=1.25, direct=False, quizzes=None, description=""):
    (root / "task_results").mkdir(exist_ok=True)
    row = {"task_id": task_id, "status": status, "ts": _iso(ts), "updated_at": _iso(ts), "_schema_version": 1,
           "accounted_upper_bound_usd": cost, "description": description or f"do {task_id}",
           "metadata": {}, "_is_direct_chat": direct}
    if quizzes:
        row["owner_quiz"] = quizzes
    (root / "task_results" / f"{task_id}.json").write_text(json.dumps(row), encoding="utf-8")


def test_wake_task_metadata_carries_origin_level_and_tree_cap(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_AUTONOMY", "act")
    meta = wake.wake_task_metadata("observe", "heartbeat", root_cost_ceiling_usd=3.5)
    assert meta["initiator"] == "consciousness" and meta["usage_category"] == "consciousness"
    assert meta["consciousness_autonomy"] == "observe" and meta["runtime_mode_cap"] == "light"
    assert meta["model_role"] == "consciousness" and meta["wake_reason"] == "heartbeat"
    # P3e: the cap the wake's own root scope binds, never the non-root member ceiling.
    assert "promote_chat_to_task" in meta["disabled_tools"] and meta["root_cost_ceiling_usd"] == 3.5
    assert "root_limit_usd" not in meta
    full = wake.wake_task_metadata("full", "event:x")
    assert full["disabled_tools"] == [] and full["runtime_mode_cap"] == "" and "root_cost_ceiling_usd" not in full
    # A non-positive cap is not stamped: it would read as "no narrowing" downstream and
    # the alarm never launches on an exhausted allowance anyway.
    assert "root_cost_ceiling_usd" not in wake.wake_task_metadata("act", "heartbeat", root_cost_ceiling_usd=0.0)
    assert wake.wake_task_metadata("bogus", "")["consciousness_autonomy"] == "act"  # falls back to the setting


def test_events_list_settled_tasks_open_cards_and_owner_messages_since_the_last_wake(tmp_path):
    since = T0 - 3600
    _result(tmp_path, "old01", ts=since - 10)
    _result(tmp_path, "new01", ts=since + 10, status="failed", cost=0.5, description="build the thing")
    _result(tmp_path, "new02", ts=since + 50, status="completed", cost=1.0, description="later thing")
    _result(tmp_path, "run01", ts=since + 20, status="running")
    _result(tmp_path, "chat1", ts=since + 30, direct=True)  # an owner's own turn: already in Recent chat
    _result(tmp_path, "ask01", ts=since - 100, status="running",
            quizzes={"q1": {"state": "open", "asked_at": _iso(since + 5)}, "q2": {"state": "answered", "answered_at": "x"}})
    # A backlog of five older cards on one task: only the newest CARD_LINES_MAX cards are listed,
    # newest first, so old cards never starve the settled lines below (opus round 3).
    _result(tmp_path, "ask02", ts=since - 200, status="running",
            quizzes={f"c{i}": {"state": "open", "asked_at": _iso(since - 1000 + i)} for i in range(5)})
    # The previous wake's own card, left behind when its turn ended (expired_terminal, В17a:
    # still answerable), is exactly the "I'll come back to it" case — it must be listed even
    # though the wake's row itself is excluded from the settled-task lines.
    _result(tmp_path, "prev1", ts=since + 40, direct=True,
            quizzes={"q3": {"state": "expired_terminal", "asked_at": _iso(since + 40)},
                     "q4": {"state": "expired_terminal", "answered_at": "y"}})
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("\n".join([
        json.dumps({"direction": "in", "ts": _iso(since + 5), "text": "hi"}),
        json.dumps({"direction": "out", "ts": _iso(since + 6), "text": "hello"}),
        json.dumps({"direction": "in", "ts": _iso(since - 5), "text": "earlier"}),
        json.dumps({"direction": "in", "ts": _iso(since + 7), "text": "again"}),
    ]) + "\n", encoding="utf-8")
    lines = wake.wake_events(tmp_path, since=since, now=T0, exclude_task_id="prev1")
    assert "- open question card q1 on task ask01 (no answer yet)" in lines
    assert "- open question card q3 on task prev1 (no answer yet)" in lines
    assert not [line for line in lines if "q2" in line or "q4" in line]
    cards = [line.split()[4] for line in lines if line.startswith("- open question card ")]
    assert cards == ["q3", "q1", "c4", "c3"]  # newest four; c2..c0 wait for their turn
    assert "- task new01 failed, $0.50: build the thing" in lines
    settled = [line for line in lines if line.startswith("- task ")]
    assert [line.split()[2] for line in settled] == ["new02", "new01"]  # newest first
    assert lines.index("- open question card q1 on task ask01 (no answer yet)") < lines.index(settled[0])
    assert "- 2 message(s) from your human (see Recent chat)" in lines
    assert not [line for line in lines if "old01" in line or "run01" in line or "chat1" in line or "- task prev1 " in line]
    assert wake.wake_events(tmp_path / "missing", since=since, now=T0) == []


def test_render_substitutes_every_placeholder_and_truncates_events_honestly(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "CONSCIOUSNESS.md").write_text(
        (REPO / "prompts" / "CONSCIOUSNESS.md").read_text(encoding="utf-8"), encoding="utf-8")
    for index in range(15):
        _result(tmp_path, f"t{index:02d}", ts=T0 - 100 + index)
    text = wake.render_wake_message(
        tmp_path, repo, reason="task_finished:t14:completed", last_wake_at=T0 - 5400, since=T0 - 5400, now=T0,
        level="act", disabled_tools=["toggle_evolution", "request_restart"], spent_usd=4.0, daily_usd=20.0,
        running=1, max_tasks=2, interval=3300)
    for key in wake.PLACEHOLDERS:
        assert "{" + key + "}" not in text, key
    assert text.startswith("[Wake-up · task_finished:t14:completed]")
    assert "1 h 30 min ago" in text and "autonomy act — everything your runtime mode allows except" in text
    assert "toggle_evolution, request_restart (calling them is refused)" in text
    assert "Allowance (last 24 h): 4.00 / 20.00 USD" in text and "still running: 1/2" in text
    assert "wake-up interval is 3300 s" in text
    assert text.count("- task t") == wake.EVENT_LINES_MAX and "(+5 more; see recent_tasks)" in text
    quiet = wake.render_wake_message(
        tmp_path, repo, reason="heartbeat", last_wake_at=0.0, since=T0 + 1, now=T0, level="full",
        disabled_tools=[], spent_usd=None, daily_usd=0, running=0, max_tasks=0, interval=900)
    assert "no wake since this process started" in quiet and "nothing new" in quiet
    assert "withheld at this level: none" in quiet and "Allowance (last 24 h): unknown / 0.00 USD" in quiet
    assert "including evolution" in quiet


def test_render_survives_a_missing_template_and_an_unreadable_task_store(tmp_path):
    (tmp_path / "task_results").mkdir()
    (tmp_path / "task_results" / "broken.json").write_text("{not json", encoding="utf-8")
    text = wake.render_wake_message(
        tmp_path, tmp_path / "no-repo", reason="heartbeat", last_wake_at=0.0, since=T0 - 1, now=T0, level="act",
        disabled_tools=[], spent_usd=0.0, daily_usd=20.0, running=0, max_tasks=2, interval=3300)
    assert text.startswith("[Wake-up · heartbeat]") and "{" not in text


def test_template_names_only_its_placeholders_and_the_wake_hints():
    template = (REPO / "prompts" / "CONSCIOUSNESS.md").read_text(encoding="utf-8")
    import re

    assert set(re.findall(r"\{([a-z_]+)\}", template)) == set(wake.PLACEHOLDERS)
    for hint in ("Doing nothing is a fine outcome", "ask only when the answer changes what you do",
                 "say what you assume meanwhile", "choose how long", "do not request an acceptance review",
                 "One maintenance item per wake is a good rhythm", "`set_next_wakeup`", "Allowance (last 24 h)",
                 "be brief, no essays unless something matters", "people you talk with"):
        assert hint in template, hint
    assert "up to 10 rounds" not in template and "300 seconds" not in template


# --- the server projection --------------------------------------------------------


@pytest.fixture
def describe(monkeypatch):
    import server

    holder = {}
    monkeypatch.setattr(server, "_consciousness", SimpleNamespace(status_snapshot=lambda: dict(holder)))
    return lambda snapshot, enabled=True: (holder.clear(), holder.update(snapshot), server._describe_bg_consciousness_state(enabled))[2]


BASE = {"enabled": True, "level": "act", "next_wake_at": "2027-01-15T12:30:00+00:00", "pending_reason": "",
        "last_wake_at": "", "last_wake_task_id": "", "last_wake_outcome": "", "last_error": "",
        "spent_24h_usd": 1.0, "daily_usd": 20.0, "allowance_resets_at": "", "tasks_running": 0, "max_tasks": 2,
        "live_wake_task_id": ""}


def test_projection_names_every_honest_status(describe):
    import server

    assert describe(BASE, enabled=False)["status"] == "disabled"
    assert describe(BASE, enabled=False)["enabled"] is False  # the caller's flag, not the snapshot's
    sleeping = describe(BASE)
    assert sleeping["status"] == "sleeping" and sleeping["detail"] == f"Sleeping until {server._clock_of(BASE['next_wake_at'])}."
    assert sleeping["next_wake_at"] == BASE["next_wake_at"] and sleeping["enabled"] is True
    pending = describe({**BASE, "pending_reason": "task_finished:a:completed"})
    assert pending["detail"].endswith(" Early wake pending: task_finished:a:completed.")
    thinking = describe({**BASE, "live_wake_task_id": "wake0001"})
    assert thinking["status"] == "thinking" and "wake0001" in thinking["detail"]
    first = describe({**BASE, "last_wake_outcome": "skipped:waiting_for_first_conversation"})
    assert first["status"] == "waiting_for_first_conversation"
    exhausted = describe({**BASE, "last_wake_outcome": "skipped:allowance_exhausted", "spent_24h_usd": 21.5,
                          "allowance_resets_at": "2027-01-15T18:00:00+00:00"})
    assert exhausted["status"] == "allowance_exhausted"
    assert "$21.50 of $20.00" in exhausted["detail"] and server._clock_of("2027-01-15T18:00:00+00:00") in exhausted["detail"]
    assert "at least" not in exhausted["detail"] and "degraded" not in exhausted["detail"]
    # PLAN 5.5: an unmetered or quarantined ledger makes the number a floor, and the status says so.
    floor = describe({**BASE, "last_wake_outcome": "skipped:allowance_exhausted", "spent_24h_usd": 21.5,
                      "unknown_unmetered": 2, "integrity_degraded": True})
    assert "at least $21.50 of $20.00" in floor["detail"] and "ledger integrity degraded" in floor["detail"]
    unknown = describe({**BASE, "last_wake_outcome": "skipped:allowance_unknown", "last_error": "OSError: ledger"})
    assert unknown["status"] == "allowance_unknown" and "OSError: ledger" in unknown["detail"]
    rejected = describe({**BASE, "last_wake_outcome": "rejected:budget_exhausted"})
    assert rejected["status"] == "wake_rejected" and "budget_exhausted" in rejected["detail"]
    failed = describe({**BASE, "last_wake_outcome": "failed", "last_error": "wake-up w1 failed in its runner"})
    assert failed["status"] == "wake_failed" and "backing off" in failed["detail"]
    done = describe({**BASE, "last_wake_outcome": "done", "last_wake_at": "2027-01-15T11:35:00+00:00"})
    assert done["status"] == "sleeping"


def test_projection_without_a_constructed_clock_is_stopped_not_running(monkeypatch):
    import server

    monkeypatch.setattr(server, "_consciousness", None)
    described = server._describe_bg_consciousness_state(True)
    assert described["status"] == "stopped" and "not constructed" in described["detail"]
    assert server._describe_bg_consciousness_state(False)["status"] == "disabled"


def test_render_substitutes_placeholders_in_one_pass(tmp_path):
    """A task title that happens to contain "{daily_usd}" is a fact, not a placeholder."""
    since = T0 - 3600
    repo = tmp_path / "repo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "CONSCIOUSNESS.md").write_text("spent {spent_usd} / {daily_usd}; events: {events}", encoding="utf-8")
    _result(tmp_path, "odd01", ts=since + 10, status="completed", cost=0.1, description="check {daily_usd} later")
    text = wake.render_wake_message(tmp_path, repo, reason="heartbeat", last_wake_at=since,
                                    since=since, now=T0, level="act", disabled_tools=[], spent_usd=1.0,
                                    daily_usd=20.0, running=0, max_tasks=2, interval=900)
    assert text.startswith("spent 1.00 / 20.00;") and "check {daily_usd} later" in text
    floor = wake.render_wake_message(tmp_path, repo, reason="heartbeat", last_wake_at=since,
                                     since=since, now=T0, level="act", disabled_tools=[], spent_usd=1.0,
                                     daily_usd=20.0, running=0, max_tasks=2, interval=900, spent_is_floor=True)
    assert floor.startswith("spent at least 1.00 / 20.00;")
