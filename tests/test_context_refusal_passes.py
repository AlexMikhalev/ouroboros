"""The refusal path sizes its model-free passes on this refusal and this conversation (17.10.2026).

86aa died on a 933k-token request the host had estimated at 678k with another task's density,
so no pass knew how much to shed. These pins keep the target measured on the conversation's
own anchor (unknown when nothing comparable measures it) and each pass shedding only what its
earlier steps left of that target: the gradual order the owner asked for, never a fixed share.
"""

from __future__ import annotations

from types import SimpleNamespace

from ouroboros import loop_model_call as lmc
from ouroboros.context_anchor import REFUSED_INPUTS_KEY


def _fit(estimate, *, anchor=874_829, accepted=921_840, reply=65_536):
    return SimpleNamespace(measurement=SimpleNamespace(
        estimated_input_tokens=estimate, anchor_input_tokens=anchor, accepted_input_tokens=accepted,
        capacity_total_tokens=None, window_reading_tokens=None, reply_allowance_tokens=reply,
        capacity_deficit_tokens=None))


def _ctx(message=""):
    return SimpleNamespace(accumulated_usage={"_last_llm_provider_message": message}, active_context_mode="max",
                           context_fit_plan=SimpleNamespace(provider="claudexor", model="codex=gpt-6-astra"))


def test_the_target_is_measured_on_this_conversation_or_stays_unknown():
    ctx = _ctx()
    # 86aa r1482: E = its raw 848,751 at its own density 874,829/795,458; A = the largest input Codex accepted.
    assert lmc._refusal_target(ctx, _fit(933_440)) == 933_440 - 921_840 + 115_230  # + ceil(A/8), L33 2=A
    assert ctx.accumulated_usage[REFUSED_INPUTS_KEY]  # recorded: the facts stop stating free room
    assert lmc._refusal_target(_ctx(), _fit(900_000)) is None  # E at or below A contradicts the reading
    assert lmc._refusal_target(_ctx(), _fit(933_440, anchor=None)) is None  # a cold count sizes nothing
    stated = _ctx("This model's maximum context length is 400,000 tokens.")
    assert lmc._refusal_target(stated, _fit(390_000, accepted=None, reply=20_000)) == 10_000 + 50_000
    # Anthropic and Gemini wordings state both numbers; the provider's own count beats the estimate.
    for message, size, limit in (
            ("prompt is too long: 215000 tokens > 200000 maximum", 215_000, 200_000),
            ("input length and `max_tokens` exceed context limit: 190000 + 65536 > 200000", 190_000, 200_000),
            ("The input token count (1050000) exceeds the maximum number of tokens allowed (1048576).",
             1_050_000, 1_048_576),
            ("This model's maximum context length is 128000 tokens. However, your messages resulted in "
             "131000 tokens.", 131_000, 128_000)):
        assert lmc._refusal_target(_ctx(message), _fit(1, anchor=None, accepted=None, reply=8_192)) == (
            max(0, size + 8_192 - limit) + -(-limit // 8)), message
    # A local serving instance that refused before sending gives its own exact counts (#1018 lane).
    local = SimpleNamespace(input_tokens=34_000, context_window_tokens=32_768)
    assert lmc._refusal_target(_ctx(), _fit(30_000, anchor=None, accepted=None, reply=8_192), local) == (
        34_000 + 8_192 - 32_768 + 4_096)
    assert lmc._refusal_target(_ctx(), _fit(30_000, anchor=None, accepted=None, reply=8_192)) is None  # cold, numberless
    # A stated limit beside a COLD estimate measures nothing (chars/4 under-counts code and Cyrillic): unknown;
    # with the provider's own count of the refused input it measures even the first refusal (review r2, 18.10).
    assert lmc._refusal_target(stated, _fit(390_000, anchor=None, accepted=None, reply=20_000)) is None
    sized = _ctx("This endpoint's maximum context length is 400000 tokens. However, you requested about 410000 tokens")
    assert lmc._refusal_target(sized, _fit(300_000, anchor=None, accepted=None, reply=20_000)) == 30_000 + 50_000
    assert sized.accumulated_usage[REFUSED_INPUTS_KEY]["claudexor|codex=gpt-6-astra"] == 410_000 or 410_000 in (
        sized.accumulated_usage[REFUSED_INPUTS_KEY].values())  # the provider's count is the refused size


def test_bodies_shed_only_what_the_host_copies_left_of_the_target(monkeypatch):
    calls, shed = [], {}

    def address(_ctx, _fit, *, rung, goal_tokens=None):
        calls.append((rung, goal_tokens))
        return SimpleNamespace(status="applied" if rung in shed else "no_eligible", reclaimed_tokens=shed.get(rung, 0))

    monkeypatch.setattr(lmc, "_run_emergency_address_pass", address)
    first = ("host_copies", "bodies")
    shed.update(host_copies=60_000, bodies=70_000)
    assert lmc._apply_refusal_pass(_ctx(), first, _fit(1), 126_830) is True
    assert calls == [("host_copies", None), ("bodies", 66_830)]  # the rest, not the whole target again
    calls.clear()
    assert lmc._apply_refusal_pass(_ctx(), first, _fit(1), 50_000) is True
    assert calls == [("host_copies", None)]  # superseded host state freed the room: every body stays
    calls.clear()
    assert lmc._apply_refusal_pass(_ctx(), first, _fit(1), None) is True
    assert calls == [("host_copies", None), ("bodies", None)]  # unknown: each step takes its category
    calls.clear()
    shed.clear()
    assert lmc._apply_refusal_pass(_ctx(), first, _fit(1), 126_830) is False  # nothing changed, no retry


def test_memory_narrowed_enough_addresses_no_unseen_body(monkeypatch):
    import ouroboros.loop as loop

    calls, sizes = [], []
    monkeypatch.setattr(lmc, "_coarsen_memory_view", lambda _ctx, _fit, *, saving=None: calls.append(("floor", saving)) or True)
    monkeypatch.setattr(lmc, "_reproject_actual_overflow_low",
                        lambda ctx: calls.append(("books",)) or setattr(ctx, "active_context_mode", "low"))
    monkeypatch.setattr(loop, "_measure_round_main_fit", lambda _ctx, **_kw: _fit(sizes.pop(0)))
    monkeypatch.setattr(lmc, "_run_emergency_address_pass", lambda _ctx, _fit, *, rung, goal_tokens=None: (
        calls.append((rung, goal_tokens)) or SimpleNamespace(status="applied", reclaimed_tokens=goal_tokens)))
    second = ("floor", "unseen_bodies")
    unsized = _fit(900_000, accepted=None)  # no window reading anywhere: no slack, the whole target is excess
    sizes[:] = [800_000]
    assert lmc._apply_refusal_pass(_ctx(), second, unsized, 80_000) is True
    assert calls == [("floor", 80_000)]  # memory alone saved it: books stay, no unseen body leaves
    calls.clear()
    sizes[:] = [800_000, 760_000]
    assert lmc._apply_refusal_pass(_ctx(), second, unsized, 150_000) is True
    assert calls == [("floor", 150_000), ("books",), ("unseen_bodies", 10_000)]  # books, then only the rest
    calls.clear()
    sizes[:] = [800_000, 760_000]
    assert lmc._apply_refusal_pass(_ctx(), second, unsized, None) is True
    assert calls == [("floor", lmc.WHOLE_CATEGORY), ("books",), ("unseen_bodies", None)]  # unknown: each whole
    calls.clear()
    sizes[:] = [800_000, 760_000]
    known = SimpleNamespace(measurement=SimpleNamespace(**{**vars(unsized.measurement), "capacity_deficit_tokens": 18_887}))
    assert lmc._apply_refusal_pass(_ctx(), second, known, None) is True
    assert calls[0] == ("floor", 18_887)  # no target, but a known window's deficit sizes memory, not "everything"
    # Memory narrows by the measured excess only; the return-after-fold slack falls to books and sources
    # (review r1, 17.10: narrowing memory for slack showed this room's own rows by address).
    calls.clear()
    sizes[:] = [880_000, 700_000]
    stated = _ctx("This model's maximum context length is 400,000 tokens.")  # slack 50,000
    assert lmc._apply_refusal_pass(stated, second, _fit(900_000, accepted=None), 70_000) is True
    # 70,000 = 20,000 excess + 50,000 slack: memory met the excess, so books stay and unseen bodies take the slack.
    assert calls == [("floor", 20_000), ("unseen_bodies", 50_000)]


def test_a_failed_candidates_walk_keeps_the_rounds_refusal_and_the_conversation_facts():
    """Review r2 (18.10): restoring the primary's route-fit state after a failed fallback erased the round's
    size-refusal stamp (so the renewal never ran) and the per-model anchors. Route-fit keys still restore."""
    from ouroboros.context_anchor import ACCEPTED_INPUTS_KEY, INPUT_ANCHOR_KEY, REFUSED_INPUTS_KEY

    usage = {"_context_capacity_total_tokens": 872_000, INPUT_ANCHOR_KEY: {"a": {}}}
    snapshot = lmc._snapshot_context_fit_usage(usage)
    usage.update({"_context_capacity_total_tokens": 128_000, lmc.CONTEXT_REFUSAL_ROUND_KEY: 7,
                  INPUT_ANCHOR_KEY: {"a": {}, "b": {}}, ACCEPTED_INPUTS_KEY: {"b": 90_000}, REFUSED_INPUTS_KEY: {"b": 130_000}})
    lmc._restore_context_fit_usage(usage, snapshot)
    assert usage["_context_capacity_total_tokens"] == 872_000  # route-fit state is the primary's again
    assert usage[lmc.CONTEXT_REFUSAL_ROUND_KEY] == 7 and set(usage[INPUT_ANCHOR_KEY]) == {"a", "b"}
    assert usage[ACCEPTED_INPUTS_KEY] == {"b": 90_000} and usage[REFUSED_INPUTS_KEY] == {"b": 130_000}


def test_a_local_pre_send_refusal_uses_the_reply_its_exact_request_reserved():
    """Review r2 (18.10): a 131,072 window, 125,000 exact input and the 8,192 reply floor of the refused
    request exceed by 2,120, not by the estimate's 14,688-token reply."""
    local = SimpleNamespace(input_tokens=125_000, context_window_tokens=131_072, max_completion_tokens=8_192)
    target = lmc._refusal_target(_ctx(), _fit(100_000, anchor=None, accepted=None, reply=14_688), local)
    assert target == 2_120 + -(-131_072 // 8)
