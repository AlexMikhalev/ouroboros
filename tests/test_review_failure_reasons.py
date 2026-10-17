"""Failed seats retain a reason when the full response artifact is pruned."""

from dataclasses import asdict

import pytest

from ouroboros.review_records import ReviewActorRecord
from ouroboros.review_ledger import build_rows
from ouroboros.triad_review import parse_seat_answers


@pytest.mark.parametrize("status,error", [
    ("error", "Connection reset while reading the daemon response"),
    ("not_dispatched", "The complete request exceeds the transport limit"),
    ("empty", ""),
])
def test_producer_records_have_a_durable_reason(status, error):
    actor = ReviewActorRecord(slot_id="s", model="m", status=status, error=error)
    row = asdict(actor)
    assert row["failure_code"]
    assert row["reported_cause"]
    if error:
        assert row["reported_cause"] == error


@pytest.mark.parametrize("verdict,text", [
    ("ERROR", "Provider connection dropped mid-run"),
    ("UNKNOWN", "I could not produce the required JSON"),
    ("UNKNOWN", ""),
])
def test_parser_reasons_reach_ledger_without_raw_text(verdict, text):
    result = parse_seat_answers({"results": [
        {"slot_id": "s", "model": "m", "verdict": verdict, "text": text},
    ]}, {"s": ["change"]})
    rows = build_rows({"triad_raw": [record.to_dict() for record in result.actor_records]})
    row = rows[0]
    row.pop("raw_text", None)
    assert row["failure_code"]
    assert row["reported_cause"]
    if text:
        assert text in row["reported_cause"]


def test_typed_causes_survive_and_success_has_no_failure_facts():
    actor = ReviewActorRecord(slot_id="s", model="m", status="error", error="raw",
        failure_code="credential_pool_exhausted", reported_cause="All accounts exhausted",
        reset_at="2030-01-01T00:00:00Z", http_status=429)
    assert actor.failure_code == "credential_pool_exhausted"
    assert actor.reported_cause == "All accounts exhausted"
    assert actor.http_status == 429 and actor.reset_at
    result = parse_seat_answers({"results": [
        {"slot_id": "s", "model": "m", "text": "[]"},
    ]}, {"s": ["change"]})
    row = result.actor_records[0].to_dict()
    assert row["status"] == "responded"
    assert not row["failure_code"] and not row.get("reported_cause")


def test_fallback_is_short_and_redacted():
    secret = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8" * 4
    actor = ReviewActorRecord(slot_id="s", model="m", status="error",
        error=f"Authentication failed for {secret}\n" + "provider detail " * 200)
    assert actor.reported_cause and len(actor.reported_cause) <= 512
    assert secret not in actor.reported_cause and "\n" not in actor.reported_cause


def test_withheld_packet_seat_keeps_the_size_refusal_on_its_ledger_row():
    from ouroboros.tools.review_admission import triad_not_dispatched_records

    records = triad_not_dispatched_records({"models": ["m"], "slot_ids": ["s"]},
                                          "The complete packet exceeds the configured input window")
    row = build_rows({"triad_raw": records})[0]
    row.pop("raw_text", None)
    assert row["failure_code"] == "review_not_dispatched"
    assert row["reported_cause"] == "The complete packet exceeds the configured input window"
