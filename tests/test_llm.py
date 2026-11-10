"""Kiểm tra AnthropicProvider với client giả: không gọi API thật."""
import json
from types import SimpleNamespace as NS

import pytest

from conftest import tool_steps
from kagent import llm


def usage():
    return NS(input_tokens=10, output_tokens=5)


def text(t):
    return NS(type="text", text=t)


def use(name, args, id_="tu1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def msg(content, stop="end_turn"):
    return NS(content=content, stop_reason=stop, usage=usage())


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def provider(replies):
    fake = FakeMessages(replies)
    return llm.AnthropicProvider(NS(messages=fake, beta=NS(messages=fake))), fake


def test_router_uses_fast_model_without_fallback():
    p, fake = provider([msg([text(json.dumps({"task_type": "k8s_ops", "entities": ["web"]}))])])
    assert p.route("xem pod")["task_type"] == "k8s_ops"
    assert fake.calls[0]["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in fake.calls[0]


def test_reflector_uses_strong_model_with_fallback():
    p, fake = provider([msg([text(json.dumps({"items": []}))])])
    assert p.reflect({"input": {"task": "x"}}) == []
    assert fake.calls[0]["model"] == "claude-opus-5-5" and fake.calls[0]["fallbacks"] == "default"
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_refusal_raises():
    p, _ = provider([msg([], stop="refusal")])
    with pytest.raises(llm.LLMError):
        p.route("x")


def test_curator_ignores_target_outside_candidates():
    p, _ = provider([msg([text(json.dumps({"op": "INVALIDATE", "target": "ku-la", "reason": "r"}))])])
    near = [{"id": "ku-1", "kind": "fact", "title": "t", "content": "c", "similarity": 0.9}]
    assert p.curate({"title": "n"}, near)["op"] == "ADD"


def test_agent_loop_routes_tools_through_gateway_and_marks_untrusted():
    p, fake = provider([msg([use("kibana__search", {"query": "x"})], stop="tool_use"), msg([text("xong")])])
    seen = []

    def call_tool(name, args):
        seen.append((name, args))
        return {"status": "ok", "decision": "auto", "result": {"hits": []}, "untrusted": True}

    out = p.run_agent("tìm log", "ctx", [{"tool": "kibana.search", "description": "d", "input_schema": {"type": "object", "properties": {}}}], call_tool)
    assert seen == [("kibana.search", {"query": "x"})]
    assert out["answer"] == "xong" and out["tokens"] == 30
    result = fake.calls[1]["messages"][2]["content"][0]
    assert result["content"].startswith("<untrusted>") and result["is_error"] is False


def test_agent_step_limit():
    replies = [msg([use("k8s__get", {})], stop="tool_use") for _ in range(10)]
    p, _ = provider(replies)
    with pytest.raises(llm.LLMError):
        p.run_agent("x", "c", [], lambda n, a: {"status": "ok"})


def test_model_error_recorded_as_failed_episode(users):
    class Broken(llm.HeuristicProvider):
        def run_agent(self, *a, **k):
            raise llm.LLMError("mất kết nối")

    llm.set_provider(Broken())
    ep = users["alice"].run("xem pod")
    assert ep["outcome"] == "failure" and "mất kết nối" in ep["output"]
    assert tool_steps(ep) == []
