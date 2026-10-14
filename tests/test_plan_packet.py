"""The delivery builder of a plan ask (``ouroboros/tools/plan_packet.py``): what one
participant receives, in the stated order, and what it is never given."""
from __future__ import annotations

import pytest

from ouroboros.tools import plan_packet

REVISION = {"n": 2, "ts": "2026-10-15T02:00:00+00:00", "goal": "Ship the deck", "plan": "outline\nthen the story",
            "done_when": [{"id": "c1", "claim": "a five-slide story", "support": "read it"}], "affected_paths": [], "body": False}
PREVIOUS = {"n": 1, "ts": "2026-10-15T01:00:00+00:00", "goal": "Ship the deck", "plan": "outline",
            "done_when": [{"id": "c1", "claim": "five slides", "support": "count"}], "affected_paths": ["deck.md"], "body": False}


def test_stable_prompt_carries_the_delivered_checklist_and_contract_and_no_pack_for_a_non_body_plan():
    prompt = plan_packet.build_stable_prompt(checklist_section="## Plan Review Checklist\n\nROW ONE", body=False, enforcement="blocking")
    assert "ROW ONE" in prompt and "## Reply contract" in prompt and '"withdrawn"' in prompt
    assert "weighs an INTENTION" in prompt and "nobody votes" in prompt
    assert "BIBLE" not in prompt and "navigation map" not in prompt and "ARCHITECTURE" not in prompt
    missing = plan_packet.build_stable_prompt(checklist_section="", body=False, enforcement="advisory")
    assert "OMISSION NOTE: Plan Review Checklist section not supplied" in missing


def test_a_body_plan_carries_the_pack_inline_or_as_mandatory_reads_and_refuses_without_it():
    inline = plan_packet.build_stable_prompt(checklist_section="x", body=True, enforcement="blocking",
                                             bible_text="BIBLE BODY", architecture_text="ARCH BODY", design_nav_map="## DESIGN map")
    assert "## BIBLE.md (constitution" in inline and "BIBLE BODY" in inline and "ARCH BODY" in inline and "## DESIGN map" in inline
    assert "has no repository tools" in inline
    retrieving = plan_packet.build_stable_prompt(checklist_section="x", body=True, enforcement="blocking",
                                                 bible_text="BIBLE BODY", architecture_text="ARCH BODY",
                                                 bible_locator="/repo/BIBLE.md", architecture_locator="/repo/docs/ARCHITECTURE.md",
                                                 governance_by_retrieval=True, design_nav_map="## DESIGN map")
    assert "MANDATORY FULL READS" in retrieving and "/repo/BIBLE.md" in retrieving and "BIBLE BODY" not in retrieving
    assert "Read relevant original DESIGN sections" in retrieving
    with pytest.raises(plan_packet.PlanPacketError):
        plan_packet.build_stable_prompt(checklist_section="x", body=True, enforcement="blocking", bible_text="", architecture_text="ARCH")
    with pytest.raises(plan_packet.PlanPacketError):
        plan_packet.build_stable_prompt(checklist_section="x", body=True, enforcement="blocking", bible_text="B", architecture_text=None)


def _content(**over):
    fields = dict(objective="Deliver the thing", owner_words="## OWNER WORDS\n\nmake it good", dialogue_section="## OWN ROOM DIALOGUE\n\nrows\n",
                  revision=REVISION, delta=None, cross_talk="", own_objections=[], message="First look.", evidence_section="(nothing attached)\n",
                  first_time=True)
    fields.update(over)
    return plan_packet.build_participant_content(**fields)


def test_first_time_content_is_whole_and_ordered():
    content = _content()
    order = ["## TASK OBJECTIVE", "## OWNER WORDS", "## OWN ROOM DIALOGUE", "## THE PLAN", "### Goal", "### done_when",
             "### affected_paths", "### Plan", "## THE COUNCIL SO FAR", "## YOUR OPEN OBJECTIONS", "## WHAT OUROBOROS ASKS YOU NOW",
             "## ATTACHED EVIDENCE"]
    positions = [content.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "You see this plan for the first time" in content and "a five-slide story" in content and "then the story" in content
    assert "every first answer is blind" in content and "You have no open objection" in content and "First look." in content
    assert plan_packet.plan_user_stable_len(content) == content.index("## THE PLAN")


def test_later_content_carries_the_delta_cross_talk_and_own_objections_first():
    from ouroboros.plan_record import revision_delta

    asks = [{"ask_id": "a1", "revision": 1, "ts": "t", "message": "look", "participants": [
        {"seat_id": "s1", "model": "m/a", "status": "answered", "reply": {"prose": "mine", "objections": [{"id": "o1", "text": "low bar"}], "withdrawn": []}},
        {"seat_id": "s2", "model": "m/b", "status": "answered", "reply": {"prose": "Peer says fine.", "objections": [], "withdrawn": [{"id": "z", "why": "w"}]}},
        {"seat_id": "s3", "model": "m/c", "status": "failed", "error": "transport died"},
        {"seat_id": "s4", "model": "m/d", "status": "not_dispatched", "not_dispatched_reason": "health_skip"},
    ]}]
    cross = plan_packet.render_cross_talk(asks, seat_id="s1")
    assert "### Ask a1" in cross and "Ouroboros asked: look" in cross
    assert "s2 (m/b) answered:" in cross and "Peer says fine." in cross and "withdrew z: w" in cross
    assert "s3 (m/c): failed — transport died" in cross and "s4 (m/d): not_dispatched — health_skip" in cross
    assert "mine" not in cross  # a participant's own replies are not repeated to it
    assert plan_packet.render_cross_talk([], seat_id="s1") == ""
    own = [{"id": "o1", "text": "low bar", "raised_in": "a1", "revision": 1, "not_adjudicated": True}]
    content = _content(delta=revision_delta(PREVIOUS, REVISION), cross_talk=cross, own_objections=own, first_time=False,
                       message="I raised the bar; is o1 answered?")
    assert "Changes since revision 1" in content and "done_when reworded: c1: «five slides» → «a five-slide story»" in content
    assert "affected path removed: deck.md" in content and "```diff" in content and "+then the story" in content
    assert "The current text follows whole." in content and "a five-slide story" in content
    assert "- o1 (raised in ask a1 about revision 1) (flagged: not adjudicated in your last reply): low bar" in content
    assert content.index("## THE COUNCIL SO FAR") < content.index("## YOUR OPEN OBJECTIONS") < content.index("I raised the bar")
    quiet = _content(delta={"first": False, "from": 1, "to": 2}, first_time=False)
    assert "nothing in the recorded fields changed" in quiet and "Nothing was said since your last answer." in quiet


def test_evidence_section_names_every_absence():
    manifest = {"declared": ["notes.md", "https://x"], "attached": [{"locator": "notes.md", "kind": "path", "sha256": "abc", "bytes": 5,
                                                                     "attached_bytes": 5, "text": "hello", "secrets_redacted": True}],
                "omissions": [{"locator": "https://x", "reason": "url_not_fetched"}]}
    rendered = plan_packet.render_evidence(manifest)
    assert "----- BEGIN notes.md -----\nhello\n----- END notes.md -----" in rendered and "secrets_redacted=true" in rendered
    assert "| https://x | url_not_fetched |" in rendered
    assert plan_packet.render_evidence({"declared": [], "attached": [], "omissions": []}) == "(nothing attached)\n"
    assert plan_packet.plan_user_stable_len("no marker here") == 0


def test_the_plan_surface_session_schema_is_the_reply_contract_and_can_carry_an_objection():
    """The retrieving-session route of a plan ask returns the SAME reply contract an API seat
    returns: prose plus typed objections and withdrawals — never a findings array or a
    verdict. The generic review surfaces keep their generic schema."""
    from ouroboros.plan_record import parse_reply
    from ouroboros.review_execution import review_session_output_schema

    schema = review_session_output_schema("plan_review")
    assert schema is plan_packet.PLAN_REPLY_SESSION_OUTPUT_SCHEMA
    props = schema["properties"]
    assert set(props) == {"prose", "objections", "withdrawn"} and schema["required"] == ["prose"]
    assert set(props["objections"]["items"]["properties"]) == {"id", "text"}
    assert set(props["withdrawn"]["items"]["properties"]) == {"id", "why"}
    assert "findings" not in props and "verdict" not in props and "class" not in str(schema)
    reply, error = parse_reply('{"prose": "No.", "objections": [{"id": "o1", "text": "the bar is too low"}], "withdrawn": []}')
    assert error == "" and reply["objections"] == [{"id": "o1", "text": "the bar is too low"}]
    assert review_session_output_schema("triad") is not schema

