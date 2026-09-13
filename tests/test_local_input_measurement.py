"""The owned server counts with its actual formatter and never invokes inference."""
import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

from ouroboros.local_model_server import input_fingerprint, measure_chat_input, with_measurement_route


@pytest.fixture
def native_model():
    llama = pytest.importorskip("llama_cpp")
    seen = []

    def formatter(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(prompt="actual rendered template", added_special=True)

    def tokenize(text, *, add_bos, special):
        assert text == b"actual rendered template" and not add_bos and special
        return list(range(60826))

    handler = llama.llama_chat_format.chat_formatter_to_chat_completion_handler(formatter)
    return SimpleNamespace(chat_handler=None, chat_format="actual-configured-format",
        _chat_handlers={"actual-configured-format": handler}, n_ctx=lambda: 81920, tokenize=tokenize,
        create_completion=lambda **kwargs: pytest.fail("Measurement must not generate"), seen=seen)


def test_selected_formatter_tokenizer_and_serving_capacity_are_used(native_model):
    payload = {"model": "local-model", "messages": [{"role": "user", "content": "exact"}],
               "tools": [{"type": "function", "function": {"name": "inspect"}}], "tool_choice": "auto"}
    result = measure_chat_input(native_model, payload)
    assert result["input_tokens"] == 60826 and result["context_window"] == 81920
    assert result["input_is_exact"] and result["output_limit_enforced"]
    assert result["native_input_sha256"] == input_fingerprint(payload)
    assert native_model.seen == [{**{key: None for key in ("functions", "function_call")},
                                 **{key: payload[key] for key in ("messages", "tools", "tool_choice")}}]
    assert "actual rendered template" not in str(result)


def test_unrecognized_handler_is_not_called_to_guess_its_behavior(native_model):
    native_model.chat_handler = lambda **kwargs: pytest.fail("Opaque handler was invoked")
    result = measure_chat_input(native_model, {"messages": []})
    assert not result["supported"] and result["input_tokens"] is None
    assert not result["output_limit_enforced"] and native_model.seen == []


def test_count_waits_for_generation_without_using_its_interrupt_lock(native_model, monkeypatch):
    # FastAPI's installed dependency is paired with this existing Python-3.10
    # annotated-doc wheel in the local operator cache; production code does not
    # install or alter either package.
    monkeypatch.syspath_prepend("/Users/anton/miniforge3/envs/py310/lib/python3.10/site-packages")
    from fastapi import FastAPI
    from llama_cpp.server import app as server

    async def run():
        inner, outer = asyncio.Lock(), asyncio.Lock()
        monkeypatch.setattr(server, "llama_inner_lock", inner)
        monkeypatch.setattr(server, "llama_outer_lock", outer)
        monkeypatch.setattr(server, "_llama_proxy", lambda _model: native_model)
        app = with_measurement_route(FastAPI())
        app.dependency_overrides[server.authenticate] = lambda: None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            await inner.acquire()
            request = asyncio.create_task(client.post("/extras/measure_chat", json={
                "model": "local-model", "messages": [{"role": "user", "content": "exact"}]}))
            await asyncio.sleep(0.02)
            assert not request.done() and not outer.locked() and not native_model.seen
            inner.release()
            result = await request
            assert result.status_code == 200
            assert result.json()["input_tokens"] == 60826 and result.json()["process_id"] == os.getpid()
            assert not inner.locked() and not outer.locked()

    asyncio.run(run())
