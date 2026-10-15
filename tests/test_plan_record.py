"""The plan record (``ouroboros/plan_record.py``): revisions, the reply parser, the
objection fold, who-saw-what, the gate projection, the acceptance view and the
read-only legacy import. Every rule is pinned in both directions."""
from __future__ import annotations

import json

import pytest

from ouroboros import plan_record as pr
from tests._store_shared import read_result, seed_result

SEAT_A, SEAT_B, CRITIC = "triad_a", "triad_b", "critic_x"


def _participant(seat, *, owner_marked=True, reply=None, status=None, dispatched=True, model="m/x"):
    row = {"seat_id": seat, "model": model, "route": "api_chat", "owner_marked": owner_marked,
           "dispatched": dispatched, "status": status or ("answered" if reply is not None else "pending")}
    if reply is not None:
        row["reply"] = {"prose": "", "objections": [], "withdrawn": [], **reply}
    return row


def _record(*asks, revisions=1):
    record = pr.empty_record()
    for n in range(1, revisions + 1):
        record["revisions"].append({"n": n, "ts": f"2026-10-15T00:0{n}:00+00:00", "goal": "ship", "plan": f"plan v{n}",
                                    "done_when": [{"id": "c1", "claim": f"claim v{n}", "support": "run it"}],
                                    "affected_paths": [], "body": False, "content_sha256": f"{n:064d}"})
    for index, participants in enumerate(asks, start=1):
        record["asks"].append({"ask_id": f"a{index}", "ts": f"2026-10-15T01:0{index}:00+00:00",
                               "revision": min(index, revisions), "message": "", "participants": list(participants)})
    return record


def _gate(record, **over):
    kwargs = {"enforcement": "blocking", "cyber_pro": False, "hurry": False, "force_plan": False}
    kwargs.update(over)
    return pr.gate_projection(record, **kwargs)


# ---------------------------------------------------------------- revisions

def test_compose_revision_carries_omitted_fields_over_and_normalizes_done_when():
    first = pr.compose_revision(None, goal="ship", plan="do it", done_when=[{"claim": "five slides", "support": "count"}],
                                affected_paths=[" a.py ", ""])
    assert first["affected_paths"] == ["a.py"]
    assert first["done_when"] == [{"id": "claim_1", "claim": "five slides", "support": "count"}]
    second = pr.compose_revision(first, plan="do it better")
    assert second["goal"] == "ship" and second["done_when"] == first["done_when"] and second["affected_paths"] == ["a.py"]
    assert second["content_sha256"] != first["content_sha256"]
    # Identical content composes the identical identity: no revision would be created.
    assert pr.compose_revision(first, plan="do it")["content_sha256"] == first["content_sha256"]


def test_revision_delta_names_reworded_claims_and_diffs_the_prose():
    prev = {"n": 1, "goal": "g", "plan": "a\nb\n", "done_when": [{"id": "c1", "claim": "old", "support": ""}], "affected_paths": ["x"]}
    cur = {"n": 2, "goal": "g", "plan": "a\nc\n", "done_when": [{"id": "c1", "claim": "new", "support": ""}, {"id": "c2", "claim": "more", "support": ""}],
           "affected_paths": []}
    delta = pr.revision_delta(prev, cur)
    assert delta["first"] is False and delta["goal_changed"] is False and delta["plan_changed"] is True
    assert delta["done_when_reworded"][0]["before"]["claim"] == "old" and delta["done_when_reworded"][0]["after"]["claim"] == "new"
    assert [r["id"] for r in delta["done_when_added"]] == ["c2"] and delta["affected_paths_removed"] == ["x"]
    assert any(line.startswith("-b") for line in delta["plan_diff"]) and any(line.startswith("+c") for line in delta["plan_diff"])
    assert pr.revision_delta(None, cur) == {"first": True}


# ---------------------------------------------------------------- reply parser

def test_parse_reply_reads_the_one_object_in_prose_or_fence_and_dedups_ids():
    text = 'Thoughts first.\n```json\n{"prose": "fine", "objections": [{"id": "o1", "text": "a"}, {"id": "o1", "text": "b"}], "withdrawn": []}\n```'
    reply, error = pr.parse_reply(text)
    assert error == "" and reply["prose"] == "fine" and reply["objections"] == [{"id": "o1", "text": "b"}]
    bare, error = pr.parse_reply('{"prose": "ok"}')
    assert error == "" and bare == {"prose": "ok", "objections": [], "withdrawn": []}
    nested, error = pr.parse_reply('I say {"prose": "with {braces} inside \\"quoted\\"", "withdrawn": [{"id": "o2", "why": "done"}]} end')
    assert error == "" and nested["withdrawn"] == [{"id": "o2", "why": "done"}]


@pytest.mark.parametrize("text", ["just prose", "[1, 2]", '{"objections": "nope"}', '{"objections": [{"text": "no id"}]}',
                                  '{"withdrawn": [{"id": "bad id!", "why": "x"}]}', '{"prose": 3}'])
def test_parse_reply_refuses_unreadable_or_malformed_replies_whole(text):
    reply, error = pr.parse_reply(text)
    assert reply is None and error


# ---------------------------------------------------------------- the fold

def test_objection_stands_until_its_raiser_withdraws_it():
    raised = _record([_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "the bar is too low"}]})])
    [objection] = pr.open_objections(raised)
    assert objection["seat_id"] == SEAT_A and objection["id"] == "o1" and objection["holds"] is True
    assert objection["not_adjudicated"] is False and objection["raised_in"] == "a1"
    withdrawn = _record(raised["asks"][0]["participants"],
                        [_participant(SEAT_A, reply={"withdrawn": [{"id": "o1", "why": "revision 2 raised the bar"}]})], revisions=2)
    assert pr.open_objections(withdrawn) == []
    [closed] = pr.fold_objections(withdrawn)
    assert closed["open"] is False and closed["withdrawn_in"] == "a2" and closed["why"] == "revision 2 raised the bar"


def test_another_seat_cannot_withdraw_it_and_omission_keeps_it_flagged():
    record = _record(
        [_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "x"}]}), _participant(SEAT_B, reply={"prose": "fine"})],
        [_participant(SEAT_B, reply={"withdrawn": [{"id": "o1", "why": "I disagree with A"}]}),
         _participant(SEAT_A, reply={"prose": "nothing about o1"})],
        revisions=2)
    [open_one] = pr.open_objections(record)
    assert open_one["seat_id"] == SEAT_A and open_one["not_adjudicated"] is True
    foreign = [o for o in pr.fold_objections(record) if o.get("unknown_withdrawal")]
    assert len(foreign) == 1 and foreign[0]["seat_id"] == SEAT_B
    # Restating it clears the flag; the text and the ask it answered move with it.
    restated = _record(*[a["participants"] for a in record["asks"]],
                       [_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "still: x"}]})], revisions=3)
    [again] = pr.open_objections(restated)
    assert again["not_adjudicated"] is False and again["text"] == "still: x" and again["ask_id"] == "a3" and again["raised_in"] == "a1"


def test_unparseable_reply_changes_nothing_and_mind_added_critics_never_hold():
    record = _record(
        [_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "x"}]}),
         _participant(CRITIC, owner_marked=False, reply={"objections": [{"id": "k1", "text": "critic says no"}]})],
        [_participant(SEAT_A, status="unparseable")])
    opened = {(o["seat_id"], o["id"]): o for o in pr.open_objections(record)}
    assert opened[(SEAT_A, "o1")]["holds"] is True and opened[(SEAT_A, "o1")]["not_adjudicated"] is False
    assert opened[(CRITIC, "k1")]["holds"] is False and opened[(CRITIC, "k1")]["open"] is True


def test_participant_views_track_last_seen_revision_and_last_answered_ask():
    record = _record(
        [_participant(SEAT_A, reply={"prose": "first"}), _participant(SEAT_B)],
        [_participant(SEAT_A), _participant(SEAT_B, dispatched=False, status="not_dispatched")],
        revisions=2)
    views = pr.participant_views(record)
    assert views[SEAT_A]["last_seen_revision"] == 2 and views[SEAT_A]["last_answered_ask"] == "a1" and views[SEAT_A]["latest_prose"] == "first"
    assert views[SEAT_B]["last_seen_revision"] == 1 and views[SEAT_B]["last_answered_ask"] == "" and views[SEAT_B]["status"] == "not_dispatched"
    assert [a["ask_id"] for a in pr.pending_asks(record)] == ["a1", "a2"]


# ---------------------------------------------------------------- the gate

def test_blocking_holds_only_on_an_open_objection_of_an_owner_marked_seat():
    held = _record([_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "x"}]})])
    decision = _gate(held)
    assert decision["status"] == "held" and decision["allow"] is False and decision["holding"] == [f"{SEAT_A}:o1"]
    critic_only = _record([_participant(CRITIC, owner_marked=False, reply={"objections": [{"id": "k1", "text": "x"}]})])
    assert _gate(critic_only)["status"] == "objections_open" and _gate(critic_only)["allow"] is True
    withdrawn = _record(held["asks"][0]["participants"], [_participant(SEAT_A, reply={"withdrawn": [{"id": "o1", "why": "ok"}]})])
    assert _gate(withdrawn)["status"] == "clear" and _gate(withdrawn)["allow"] is True


def test_silence_refusals_and_pending_asks_never_hold():
    silent = _record([_participant(SEAT_A), _participant(SEAT_B, dispatched=False, status="not_dispatched"),
                      _participant(CRITIC, owner_marked=False, status="failed", dispatched=True)])
    decision = _gate(silent)
    assert decision["allow"] is True and decision["status"] == "awaiting" and decision["review_only_awaited"] is True
    assert decision["pending_asks"] == ["a1"]
    nothing_sent = _record([_participant(SEAT_A, dispatched=False, status="not_dispatched")])
    assert _gate(nothing_sent)["status"] == "clear"


def test_advisory_cyber_and_hurry_disclose_instead_of_holding():
    held = _record([_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "x"}]})])
    advisory = _gate(held, enforcement="advisory")
    assert advisory["allow"] is True and advisory["status"] == "objections_open" and advisory["holding"] == [f"{SEAT_A}:o1"]
    cyber = _gate(held, cyber_pro=True)
    assert cyber["allow"] is True and cyber["decision_authority"] == "cyber_pro"
    hurry = _gate(held, hurry=True)
    assert hurry["allow"] is True and hurry["owner_hurry_local_advisory"] is True and hurry["configured_enforcement"] == "blocking"
    rail = _gate(held, hard_rail="round_limit")
    assert rail["allow"] is True and rail["status"] == "rail_degraded" and rail["reason"] == "round_limit"


def test_force_plan_is_met_only_by_an_ask_that_reached_a_reviewer():
    absent = _gate(pr.empty_record(), force_plan=True)
    assert absent["required"] is True and absent["allow"] is False and absent["status"] == "force_plan_unmet"
    assert _gate(pr.empty_record(), force_plan=True, enforcement="advisory")["allow"] is False  # no plan at all: write one
    assert _gate(pr.empty_record(), force_plan=True, cyber_pro=True)["allow"] is True
    # A plan that exists but reached nobody: Blocking holds; Advisory and the owner's hurry disclose.
    unasked = _record(revisions=1)
    assert _gate(unasked, force_plan=True)["allow"] is False
    advisory = _gate(unasked, force_plan=True, enforcement="advisory")
    assert advisory["allow"] is True and advisory["status"] == "force_plan_unmet" and advisory["force_plan_met"] is False
    assert _gate(unasked, force_plan=True, hurry=True)["allow"] is True
    nobody = _record([_participant(SEAT_A, dispatched=False, status="not_dispatched")])
    assert _gate(nobody, force_plan=True)["status"] == "force_plan_unmet"
    reached = _record([_participant(SEAT_A)])
    met = _gate(reached, force_plan=True)
    assert met["force_plan_met"] is True and met["status"] == "awaiting" and met["allow"] is True and met["self_opened"] is False


def test_a_task_without_a_plan_is_not_required_and_an_unreadable_record_is_engaged():
    assert _gate(pr.empty_record()) == {"required": False, "allow": True, "status": "not_required", "enforcement": "blocking",
                                        "open_objections": [], "holding": [], "pending_asks": [], "revision": 0}
    unreadable = _gate(pr.empty_record(), unreadable=True)
    assert unreadable["required"] is True and unreadable["allow"] is False and unreadable["status"] == "unreadable"
    assert _gate(pr.empty_record(), unreadable=True, enforcement="advisory")["allow"] is False
    assert _gate(pr.empty_record(), unreadable=True, cyber_pro=True)["allow"] is True


# ---------------------------------------------------------------- acceptance

def test_latest_revision_binds_always_and_the_exhibit_carries_every_revisions_text():
    record = _record([_participant(SEAT_A, reply={"objections": [{"id": "o1", "text": "the bar is low"}]})], revisions=2)
    claims, source, exhibit = pr.acceptance_view(record, [])
    assert source == "plan_review" and [c["claim"] for c in claims] == ["claim v2"] and claims[0]["id"] == "c1"
    assert exhibit["binding"] == "latest_revision:2"
    assert [r["done_when"][0]["claim"] for r in exhibit["revisions"]] == ["claim v1", "claim v2"]
    assert exhibit["revisions"][0]["seen_by_asks"] == ["a1"] and exhibit["revisions"][1]["seen_by_asks"] == []
    assert exhibit["open_objections"][0]["id"] == "o1"


def test_ingress_claims_win_and_adopted_promises_stay_visible():
    record = _record(revisions=1)
    ingress = [{"id": "task_1", "claim": "from the owner", "surface": "", "support": "", "priority": "must"}]
    claims, source, exhibit = pr.acceptance_view(record, ingress)
    assert claims == ingress and source == "ingress_contract" and exhibit["binding"] == "ingress_contract"
    assert exhibit["adopted_promises"] == [{"id": "c1", "claim": "claim v1", "support": "run it"}]
    assert pr.acceptance_view(pr.empty_record(), [])[0:2] == ([], "")


# ---------------------------------------------------------------- legacy import

def _v2_state(*, closed, blocking_findings=(), claims=("a", "b"), fingerprint="f" * 64):
    return {"schema_version": 2, "cycles_paid": 1, "current_attempt": {"fingerprint": fingerprint, "status": "open", "reason": ""},
            "waves": [{"request_fingerprint": fingerprint, "closed": closed, "aggregate": "GREEN" if closed else "REVISE_PLAN", "paid": True,
                       "spec": {"acceptance_claims": list(claims)},
                       "findings": [{"finding_id": f"{seat}:f1", "class": "blocking", "slot": seat, "summary": "wrong shape"} for seat in blocking_findings]
                       + [{"finding_id": "triad_z:n1", "class": "note", "slot": "triad_z", "summary": "a note"}]}]}


def test_legacy_open_blocking_findings_become_holding_objections_of_their_seats():
    legacy = pr.import_legacy_v2(_v2_state(closed=False, blocking_findings=("triad_old",)))
    assert legacy["engaged"] is True and legacy["done_when"] == [] and legacy["asks_dispatched"] == 1
    assert legacy["open_objections"] == [{"seat_id": "triad_old", "id": "triad_old:f1", "owner_marked": True, "text": "wrong shape",
                                          "fingerprint": "f" * 64}]
    decision = _gate(pr.empty_record(), legacy=legacy)
    assert decision["status"] == "held" and decision["source"] == "legacy_v2" and decision["open_objections"][0]["legacy"] is True
    assert _gate(pr.empty_record(), legacy=legacy, enforcement="advisory")["allow"] is True
    # The seat that raised it releases it through the new organ; nobody else can.
    released = _record([_participant("triad_old", reply={"withdrawn": [{"id": "triad_old:f1", "why": "fixed"}]})])
    assert _gate(released, legacy=legacy)["status"] == "clear"
    other = _record([_participant(SEAT_B, reply={"withdrawn": [{"id": "triad_old:f1", "why": "I say so"}]})])
    assert _gate(other, legacy=legacy)["status"] == "held"


def test_legacy_closed_wave_and_advisory_author_selection_bind_as_done_when():
    closed = pr.import_legacy_v2(_v2_state(closed=True))
    assert closed["open_objections"] == [] and closed["done_when_source"] == "legacy_closed_wave"
    claims, source, exhibit = pr.acceptance_view(pr.empty_record(), [], legacy=closed)
    assert [c["claim"] for c in claims] == ["a", "b"] and source == "plan_review" and exhibit["binding"] == "legacy_closed_wave"
    author = pr.import_legacy_v2(_v2_state(closed=False), author_plan={
        "spec": {"acceptance_claims": ["selected by the author"]}, "author_disposition": {"action": "finish", "enforcement": "advisory"}})
    assert author["done_when_source"] == "legacy_author_plan" and author["done_when"][0]["claim"] == "selected by the author"
    assert pr.import_legacy_v2({"schema_version": 2, "waves": [], "current_attempt": {}})["engaged"] is False
    assert pr.import_legacy_v2("garbage")["engaged"] is False
    malformed = pr.import_legacy_v2({"schema_version": 2, "current_attempt": {"fingerprint": "x", "status": "open", "reason": ""},
                                     "waves": [{"request_fingerprint": "x", "closed": False, "findings": ["not a dict", {"class": "blocking"}]}]})
    assert malformed["engaged"] is True and malformed["open_objections"] == []



def test_legacy_in_flight_wave_is_one_pending_ask_that_discloses_and_never_holds():
    """An in-flight v2 wave at upgrade: its unanswered reviewers are ONE pending ask the final
    answer discloses (silence never holds); a blocking finding already landed still holds."""
    from ouroboros.owner_hurry import plan_review_disclosure

    state = _v2_state(closed=False)
    wave = state["waves"][0]
    wave.update(aggregate="DEGRADED", custody_pending=True,
                actors=[{"slot": "s1", "ok": False, "operation_state": "pending_dispatch", "late_result_pending": True}])
    legacy = pr.import_legacy_v2(state)
    assert legacy["pending_asks"] == ["legacy wave " + "f" * 12] and legacy["open_objections"] == []
    decision = _gate(pr.empty_record(), legacy=legacy)
    assert decision["status"] == "awaiting" and decision["allow"] is True and decision["pending_asks"] == legacy["pending_asks"]
    disclosed = plan_review_disclosure(decision)
    assert "legacy wave ffffffffffff" in disclosed and "had not arrived" in disclosed and "silence holds nothing" in disclosed
    # answered and settled: nothing pending, nothing disclosed
    wave.update(custody_pending=False, actors=[{"slot": "s1", "ok": True, "operation_state": "settled", "late_result_pending": False}])
    settled = pr.import_legacy_v2(state)
    assert settled["pending_asks"] == [] and _gate(pr.empty_record(), legacy=settled)["status"] == "clear"
    assert pr.import_legacy_v2(_v2_state(closed=True))["pending_asks"] == []
    # a landed blocking finding holds whether or not the rest of the wave is still owed
    held_state = _v2_state(closed=False, blocking_findings=("triad_old",))
    held_state["waves"][0]["custody_pending"] = True
    held = _gate(pr.empty_record(), legacy=pr.import_legacy_v2(held_state))
    assert held["status"] == "held" and held["pending_asks"] == ["legacy wave " + "f" * 12]

def test_legacy_objections_stand_when_the_author_moved_on_but_claims_do_not_follow():
    """A changed spec at the cap or an author plan moves the attempt's fingerprint to a wave that
    never ran: the latest wave's open blocking findings still hold (a Blocking hold is not dropped
    by deployment), while a closed wave the author left behind binds no claims."""
    moved = _v2_state(closed=False, blocking_findings=("triad_old",))
    moved["current_attempt"] = {"fingerprint": "e" * 64, "status": "cycles_exhausted", "reason": "spent"}
    legacy = pr.import_legacy_v2(moved)
    assert [o["seat_id"] for o in legacy["open_objections"]] == ["triad_old"] and legacy["open_objections"][0]["fingerprint"] == "f" * 64
    assert _gate(pr.empty_record(), legacy=legacy)["status"] == "held"
    left_behind = _v2_state(closed=True)
    left_behind["current_attempt"] = {"fingerprint": "e" * 64, "status": "open", "reason": "author_current_plan"}
    stale = pr.import_legacy_v2(left_behind)
    assert stale["open_objections"] == [] and stale["done_when"] == [] and stale["done_when_source"] == ""
    assert pr.acceptance_view(pr.empty_record(), [], legacy=stale)[2]["binding"] == "none"


# ---------------------------------------------------------------- durable index

def test_durable_record_round_trips_revisions_asks_and_replies(tmp_path):
    root, task = tmp_path, "task-1"
    seed_result(root, task, {"task_id": task, "status": "running"})
    fields = pr.compose_revision(None, goal="ship", plan="first", done_when=["five slides"], affected_paths=[])
    record, revision, created = pr.append_revision(root, task, fields, body=False, body_note="not body")
    assert created is True and revision["n"] == 1 and revision["text_ref"]["kind"] == "task_source"
    again, same, created_again = pr.append_revision(root, task, fields, body=False)
    assert created_again is False and same["n"] == 1 and len(again["revisions"]) == 1
    record = pr.append_ask(root, task, {"ask_id": pr.next_ask_id(record), "ts": "t", "revision": 1, "message": "look",
                                        "participants": [_participant(SEAT_A)]})
    assert record["asks"][0]["ask_id"] == "a1" and record["asks"][0]["participants"][0]["status"] == "pending"
    record = pr.update_participant(root, task, ask_id="a1", seat_id=SEAT_A, facts={
        "status": "answered", "reply": {"prose": "ok", "objections": [{"id": "o1", "text": "x"}], "withdrawn": []}})
    assert pr.open_objections(record)[0]["id"] == "o1"
    assert pr.load_plan_record(root, task) == record
    stored = read_result(root, task)[pr.PLAN_RECORD_KEY]
    assert stored["schema_version"] == 3
    with pytest.raises(pr.PlanRecordInvalid):
        pr.append_ask(root, task, {"ask_id": "a1", "ts": "t", "revision": 1, "participants": []})
    with pytest.raises(pr.PlanRecordInvalid):
        pr.update_participant(root, task, ask_id="a1", seat_id="nobody", facts={"status": "failed"})


def test_absent_record_reads_empty_and_malformed_records_refuse(tmp_path):
    assert pr.load_plan_record(tmp_path, "absent") == pr.empty_record()
    seed_result(tmp_path, "bad", {"task_id": "bad", pr.PLAN_RECORD_KEY: {"schema_version": 3, "revisions": "x", "asks": []}})
    with pytest.raises(pr.PlanRecordInvalid):
        pr.load_plan_record(tmp_path, "bad")
    seed_result(tmp_path, "v2", {"task_id": "v2", "plan_review_state": {"schema_version": 2}})
    assert pr.load_plan_record(tmp_path, "v2") == pr.empty_record()  # a v2 record is not a v3 record
    for broken in ({"schema_version": 3, "revisions": [{"n": 2, "done_when": [], "affected_paths": []}], "asks": []},
                   {"schema_version": 3, "revisions": [], "asks": [{"ask_id": "a1", "revision": 1, "participants": []}]},
                   {"schema_version": 3, "revisions": [{"n": 1, "done_when": [], "affected_paths": []}], "asks": [
                       {"ask_id": "a1", "revision": 1, "participants": [{"seat_id": "s", "status": "weird"}]}]}):
        with pytest.raises(pr.PlanRecordInvalid):
            pr.validate_record(broken)


def test_oversize_index_moves_the_oldest_prose_out_and_keeps_the_latest_whole(tmp_path, monkeypatch):
    root, task = tmp_path, "task-fit"
    seed_result(root, task, {"task_id": task, "status": "running"})
    monkeypatch.setattr(pr, "_PLAN_RECORD_MAX_BYTES", 4_000)
    big = "x" * 3_000
    pr.append_revision(root, task, pr.compose_revision(None, goal="g", plan=big + "1", done_when=[], affected_paths=[]), body=False)
    record, latest, _ = pr.append_revision(root, task, pr.compose_revision(None, goal="g", plan=big + "2", done_when=[], affected_paths=[]), body=False)
    assert record["revisions"][0]["plan_inline"] is False and record["revisions"][0]["plan"] == ""
    assert record["revisions"][0]["text_ref"]["sha256"] and latest["plan"] == big + "2" and latest["plan_inline"] is True
    exact = json.loads((root / "artifacts" / "tasks" / task / record["revisions"][0]["text_ref"]["path"]).read_text(encoding="utf-8")) \
        if (root / "artifacts" / "tasks" / task / record["revisions"][0]["text_ref"]["path"]).exists() else None
    assert exact is None or exact["plan"] == big + "1"
