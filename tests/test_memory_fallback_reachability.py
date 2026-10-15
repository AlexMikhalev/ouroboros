"""Real fitting/selection regressions found by the independent contribution review."""
import json

import pytest

from ouroboros import memory_fallback as mf
from ouroboros import memory_inventory as mi
from ouroboros.chronicle_store import ChronicleStore
from tests import _memory_inventory_shared as shared
from tests.test_memory_fallback import _consciousness, _Light, _mind_page, _run, _shortage
from tests.test_memory_fallback import light as _light

light = _light


def test_rejected_fitted_page_reaches_untouched_row_suffix(tmp_path, monkeypatch, light):
    shared.world(tmp_path)
    _consciousness(monkeypatch, False)
    _mind_page(tmp_path, "1", 10, 19)
    for n in range(4):
        shared.append(tmp_path / "logs/chat.jsonl", shared.msg(
            f"2026-09-04T00:0{n}:00+00:00", f"UNTOUCHED-{n}: " + "x" * 1800,
            client_message_id=f"row-{n}"))
    store = ChronicleStore(tmp_path)
    trace = _shortage("1", 23, tmp_path)
    unit = next(mf.candidate_units(tmp_path, store, mi.shortage_from_trace(trace), allow_legacy=False))
    segment = mf.replace(unit.segment, rows=unit.segment.rows[:1], to_addr=unit.segment.rows[0][0])
    single = mf.build_writer_input(tmp_path, store, mf.replace(unit, segment=segment), budget_tokens=None)
    monkeypatch.setattr(mf, "_light_fit", lambda _: (len(single.prompt) + 10, len))
    first = _run(tmp_path, _Light(), direct=True, trace=trace)
    assert first.outcome == "published"
    assert store.get(first.record_id)["covers"]["count"] == 1
    assert store.decide(first.record_id, False, shared.MIND, "The helper misattributed that first row.").ok
    shared.append(tmp_path / "logs/chat.jsonl", shared.msg(
        "2026-09-04T00:04:00+00:00", "A later owner turn must not make untouched history unreachable.",
        client_message_id="later-owner"))
    llm = _Light()
    second = _run(tmp_path, llm, direct=True, trace=_shortage("1", 24, tmp_path))
    assert second.outcome == "published", "unattempted row suffix must stay reachable"
    assert len(llm.prompts) == 1
    assert "UNTOUCHED-1" in llm.prompts[0] and "UNTOUCHED-0" not in llm.prompts[0]
    assert store.get(first.record_id)["metadata"]["input_sha256"] != store.get(second.record_id)["metadata"]["input_sha256"]


def test_part_prefix_search_handles_later_terminal_stamp_reduction(tmp_path, monkeypatch, light):
    shared.world(tmp_path)
    _consciousness(monkeypatch, False)
    store, ids = ChronicleStore(tmp_path), []
    earlier = [{"task_id": f"task-{n:03d}", "status": "completed", "outcome_phase": "done",
                "source": "task_results"} for n in range(24)]
    later = [{**row, "source": "terminal_root_projection", "reason_code": ""} for row in earlier]
    for n in range(5):
        result = store.publish_page(room_id="1", text=f"Report {n}." + ("x" * 9000 if n == 4 else ""),
            author=shared.MIND, covers={"rows": [f"row-{n}"], "stream_span": [100+n, 100+n]},
            host_stamp={"tasks": later if n >= 3 else earlier})
        assert result.ok
        ids.append(result.record["id"])
    unit = mf.FallbackUnit("part", "1", "part:1:" + ids[0], member_ids=tuple(ids),
                           head_sequence=store.room_head("1"))
    _, measure = mf._light_fit(mf.light_binding())
    sizes = [measure(mf.build_writer_input(tmp_path, store, mf.replace(unit, member_ids=tuple(ids[:n])),
                                          budget_tokens=None).prompt) for n in range(1, 6)]
    budget = sizes[3] + 1
    assert sizes[3] < min(sizes[:3]) and sizes[4] > budget
    selected = mf.build_writer_input(tmp_path, store, unit, budget_tokens=budget, measure=measure)
    assert selected.input_tokens <= budget
    assert selected.member_ids == tuple(ids[:4])
    light.window = mf.ANSWER_RESERVE_TOKENS + budget
    trace = {mi.VIEW_TRACE_KEY: {"role": "integrator", "room_id": "1", "floor": {
        "steps": {"F5": 5}, "pointer_records": ids, "by_budget": 0, "mode": "max", "window_tokens": 272000}}}
    llm = _Light()
    assert _run(tmp_path, llm, direct=True, trace=trace).outcome == "published"
    assert len(llm.prompts) == 1


@pytest.mark.parametrize("count", [12, 24])
def test_nested_part_suffixes_reuse_period_sources_and_rendered_members(tmp_path, monkeypatch, light, count):
    shared.world(tmp_path)
    store, ids = ChronicleStore(tmp_path), []
    for n in range(count):
        members = []
        for j in range(2):
            page = store.publish_page(room_id="1", text=f"Source {n}-{j}.", author=shared.MIND,
                covers={"rows": [f"nested-{n}-{j}"], "stream_span": [100+n*2+j, 100+n*2+j]})
            assert page.ok
            members.append(page.record["id"])
        part = store.publish_part(room_id="1", text=f"Part {n}: " + "history " * 200,
            member_ids=members, author=shared.MIND, expected_sequence=store.room_head("1"))
        assert part.ok
        ids.append(part.record["id"])
    unit = mf.FallbackUnit("part", "1", "part:1:"+ids[0], member_ids=tuple(ids), head_sequence=store.room_head("1"))
    for ident in ids:
        single = mf.replace(unit, key="part:1:"+ident, member_ids=(ident,))
        draft = mf.build_writer_input(tmp_path, store, single, budget_tokens=None)
        mf._set_refusal(store, single.key, {"input_sha256": draft.input_sha256, "light_binding": mf.light_binding()})
    budget = len(draft.prompt) + 20
    # Isolate the selection/fitting work from the pointer discovery cost.
    monkeypatch.setattr(mf, "candidate_units", lambda *a, **k: iter([unit]))
    reads, rendered = [], []
    original_get, original_indent = store.get, mf._indented
    def get(ident):
        reads.append(ident)
        return original_get(ident)
    def indent(text):
        rendered.append(text)
        return original_indent(text)
    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(mf, "_indented", indent)
    assert mf.select_unit(tmp_path, store, None, allow_legacy=False,
        binding=mf.light_binding(), fit=lambda: (budget, len)) is None
    assert len(reads) <= count, (count, len(reads))
    assert sum(str(text).startswith("Part ") for text in rendered) == count
    # A new selection pass observes new source content, rather than a persistent cache.
    assert store.correct(ids[0], "The earlier account needs this new qualification.", shared.MIND).ok
    unit = mf.replace(unit, head_sequence=store.room_head("1"))
    chosen = mf.select_unit(tmp_path, store, None, allow_legacy=False,
        binding=mf.light_binding(), fit=lambda: (None, len))
    assert chosen is not None and "new qualification" in chosen[1].prompt


def test_rejected_legacy_prefix_keeps_its_untouched_rows_reachable(tmp_path, monkeypatch, light):
    rooms = shared.world(tmp_path, activate=False)
    _consciousness(monkeypatch, False)
    # A real imported legacy segment; both source rows remain in the original archive.
    archive = tmp_path / 'archive' / shared.ARCHIVE_TWO
    rows = []
    for line in archive.read_text().splitlines(keepends=True):
        if 'beta question' in line or 'beta answer' in line:
            row = json.loads(line)
            row['text'] += ' x' * 900
            row['direction'] = 'in'  # Both are spoken rows that may not become address-only input.
            row.pop('task_id', None)
            line = json.dumps(row) + '\n'
        rows.append(line)
    archive.write_text(''.join(rows))
    store = ChronicleStore(tmp_path)
    store.ensure_activated()
    unit = next(u for u in mf.candidate_units(tmp_path, store, None, allow_legacy=True)
                if u.room_id == str(rooms['beta']))
    assert unit.kind == 'legacy' and len(unit.segment.rows) == 2
    segment = mf.replace(unit.segment, rows=unit.segment.rows[:1], to_addr=unit.segment.rows[0][0])
    single = mf.build_writer_input(tmp_path, store, mf.replace(unit, segment=segment), budget_tokens=None)
    monkeypatch.setattr(mf, '_light_fit', lambda _: (len(single.prompt) + 10, len))
    monkeypatch.setattr(mf, 'candidate_units', lambda *a, **k: iter([unit]))
    first = _run(tmp_path, _Light())
    assert first.outcome == 'published' and store.get(first.record_id)['covers']['count'] == 1
    assert store.decide(first.record_id, False, shared.MIND, 'Keep the source.').ok
    second_llm = _Light()
    second = _run(tmp_path, second_llm)
    assert second.outcome == 'published' and len(second_llm.prompts) == 1
    assert 'beta answer' in second_llm.prompts[0] and 'beta question' not in second_llm.prompts[0]
    assert second.unit.record_id == unit.record_id and second.unit.key != first.unit.key
    assert store.decide(second.record_id, False, shared.MIND, 'Keep this source too.').ok
    third_llm = _Light()
    assert _run(tmp_path, third_llm).outcome == 'nothing' and not third_llm.prompts


def test_fitted_endpoint_includes_addressed_rows_not_only_sealed_rows(tmp_path, monkeypatch, light):
    shared.world(tmp_path)
    store = ChronicleStore(tmp_path)
    unit = next(mf.candidate_units(tmp_path, store, mi.shortage_from_trace(_shortage('1', 19, tmp_path)),
                                   allow_legacy=False))
    assert len(unit.segment.rows) >= 3
    # The first row was read, the second addressed, the remaining ones unattempted.
    draft = mf.WriterInput('bounded input', 'hash', None, covers={'last': unit.segment.rows[0][0]},
                           fitted_to=unit.segment.rows[1][0])
    suffix = mf._untouched_suffix(unit, draft)
    assert suffix.segment.rows == unit.segment.rows[2:]
    assert suffix.segment.from_addr == unit.segment.rows[2][0]


def test_snapshot_periods_equal_live_periods_for_nested_and_missing_sources(tmp_path):
    from tests import _memory_production_shape as shape

    shape.production(tmp_path)
    store = ChronicleStore(tmp_path)
    records = {r['id']: r for r in store.records()}
    units = {u.record_id: u for u in mi.legacy_units(store, tmp_path)}
    for record in records.values():
        assert mi.record_period(store, record, units, records=records) == mi.record_period(store, record, units)
    missing = {'kind': 'part', 'covers': {'member_ids': ['missing']}}
    assert mi.record_period(store, missing, units, records=records) == mi.record_period(store, missing, units)
