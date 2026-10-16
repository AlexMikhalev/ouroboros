"""A duty's member that cannot progress rests (``obligations.DutyRest``): its typed outcome holds
while every recorded input holds, the pass selects it without reading a body or writing, and the
first change of an input (a new fact on the task, an external stamp, its own retry instant) puts
it back exactly once. A rest is a kept fact, never debt and never a retirement of the debt."""
import pytest

from ouroboros import evidence_packs, obligations as o, store, task_store
from ouroboros.task_results import write_task_result

SET = "nonterminal"


def revision(root):
    with store.open_store(root, write=False) as txn:
        return txn.revision()


def marks(root):
    with store.open_store(root, write=False) as txn:
        return txn.set_rows(o.REST_SET)


@pytest.fixture
def root(tmp_path, monkeypatch):
    for tid in ("a", "b"):
        write_task_result(tmp_path, tid, "running")
    # A pass reads members, signatures and rests; it never decodes a task body.
    monkeypatch.setattr(task_store, "read_document", lambda *a, **k: pytest.fail("a rest pass read a body"))
    return tmp_path


def test_a_member_rests_until_a_new_fact_puts_it_back_exactly_once(root, monkeypatch):
    first = o.DutyRest(root, "probe", name=SET)
    assert first.examine == ["a", "b"] and not first.resting  # no rest: every member is examined
    first.wait("a", "owner_resume")
    first.commit()
    assert set(marks(root)) == {"probe:a"} and marks(root)["probe:a"]["waits_on"] == ["owner_resume"]

    before = revision(root)
    second = o.DutyRest(root, "probe", name=SET)
    assert second.examine == ["b"] and set(second.resting) == {"a"}
    second.commit()
    assert revision(root) == before  # a quiet pass writes nothing

    monkeypatch.undo()
    write_task_result(root, "a", "running", note="a new fact")  # any write of the task moves its signature
    third = o.DutyRest(root, "probe", name=SET)
    assert third.examine == ["a", "b"]  # back exactly once ...
    third.wait("a", "owner_resume")
    third.commit()
    assert o.DutyRest(root, "probe", name=SET).examine == ["b"]  # ... and at rest again


def test_an_external_stamp_or_the_retry_instant_wakes_a_rest(root):
    rest = o.DutyRest(root, "probe", name=SET, inputs={"registry": [1, 2, 3]})
    rest.wait("a", "terminal_delivery", inputs=("registry",))
    rest.wait("b", "delivery_retry", due_at="2999-01-01T00:00:00+00:00")
    rest.commit()
    assert o.DutyRest(root, "probe", name=SET, inputs={"registry": [1, 2, 3]}).examine == []
    assert o.DutyRest(root, "probe", name=SET, inputs={"registry": [1, 2, 4]}).examine == ["a"]
    assert o.DutyRest(root, "probe", name=SET, inputs={}).examine == ["a"]  # an unknown stamp proves nothing
    later = o.DutyRest(root, "probe", name=SET, inputs={"registry": [1, 2, 3]}, now="2999-01-01T00:00:01+00:00")
    assert later.examine == ["b"]  # its own persisted retry instant arrived


def test_wake_examines_a_rest_without_dropping_it(root):
    rest = o.DutyRest(root, "probe", name=SET)
    rest.wait("a", "owner_resume")
    rest.commit()
    woken = o.DutyRest(root, "probe", name=SET, wake=("owner_resume",))
    assert woken.examine == ["a", "b"]
    woken.wait("a", "owner_resume")
    before = revision(root)
    woken.commit()
    assert revision(root) == before and set(marks(root)) == {"probe:a"}


def test_a_departed_member_drops_its_rest_once(root, monkeypatch):
    rest = o.DutyRest(root, "probe", name=SET)
    rest.wait("a", "owner_resume")
    rest.wait("b", "owner_resume")
    rest.commit()
    monkeypatch.undo()
    write_task_result(root, "a", "completed", result="done")  # leaves the set
    o.DutyRest(root, "probe", name=SET).commit()
    assert set(marks(root)) == {"probe:b"}
    before = revision(root)
    o.DutyRest(root, "probe", name=SET).commit()
    assert revision(root) == before


def test_a_write_racing_the_pass_is_never_rested_over(root, monkeypatch):
    rest = o.DutyRest(root, "probe", name=SET)
    monkeypatch.undo()
    write_task_result(root, "a", "running", note="landed after the selection")
    rest.wait("a", "owner_resume")
    rest.commit()
    assert "probe:a" not in marks(root)
    assert o.DutyRest(root, "probe", name=SET).examine == ["a", "b"]


def test_a_rest_is_a_kept_fact_never_debt(root):
    assert o.REST_SET in o.RECORD_SETS
    rest = o.DutyRest(root, "probe", candidates={"a": {}})
    rest.wait("a", "owner_resume")
    rest.commit()
    owed = evidence_packs.settlement_obligations(root)
    assert o.REST_SET not in owed.get("a", {}) and "nonterminal" in owed["a"]
