from types import SimpleNamespace

from ouroboros.loop_model_call import _main_context_profile
from ouroboros.usage_ledger import _validate_candidate_facts


def test_nano_uses_owner_nano_profile_and_rendered_mode():
    plan = SimpleNamespace(preferred_mode="nano")
    assert _main_context_profile(plan, "nano") == "owner_nano"


def test_usage_ledger_accepts_nano_physical_context():
    row = {
        "physical_context": {
            "profile": "owner_nano", "rendered_mode": "nano",
            "measurement_basis": "fresh_route_usage", "route_fp": "route",
            "round_id": "round", "target_total_tokens": None,
            "capacity_total_tokens": None, "context_target_miss": False,
            "automatic_pass_used": False,
        }
    }
    _validate_candidate_facts(row, 1)


def test_chat_carries_nano_mode_to_physical_target(monkeypatch):
    from ouroboros.llm import LLMClient

    client = LLMClient()
    captured = {}
    monkeypatch.setattr(client, "_resolve_remote_target", lambda _model: {"provider": "openai"})

    def remote(target, *_args, **_kwargs):
        captured.update(target)
        return {"content": "ok"}, {}

    monkeypatch.setattr(client, "_chat_remote", remote)
    client.chat([{"role": "user", "content": "hello"}], "openai::test", context_mode="nano")
    assert captured["context_mode"] == "nano"
