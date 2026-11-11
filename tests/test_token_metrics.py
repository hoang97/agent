"""Đo token theo từng lần gọi LLM và prompt caching trong vòng lặp agent."""
import json
from types import SimpleNamespace as NS

from kagent import db, llm
from test_llm import msg, provider, text, use


def usage(i=100, o=20, read=0, write=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=read, cache_creation_input_tokens=write)


def test_agent_loop_marks_cache_breakpoints_and_records_usage():
    p, fake = provider([NS(content=[use("k8s__list_pods", {})], stop_reason="tool_use", usage=usage(500, 30, 0, 4000), model="claude-opus-5-5"),
                        NS(content=[text("xong")], stop_reason="end_turn", usage=usage(80, 10, 4000, 600), model="claude-opus-5-5")])
    tools = [{"tool": "k8s.list_pods", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    with llm.collect_usage() as got:
        p.run_agent("xem pod", "ctx", tools, lambda name, args: {"status": "ok", "result": {"pods": []}})
    first, second = fake.calls
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Vòng sau: breakpoint dời xuống tool_result mới; bản gốc của message cũ không bị gắn thêm.
    assert second["messages"][-1]["content"][-1]["type"] == "tool_result" and "cache_control" in second["messages"][-1]["content"][-1]
    assert second["messages"][0]["content"] == "xem pod"
    assert [(u["purpose"], u["cache_read"], u["cache_write"]) for u in got] == [("agent", 0, 4000), ("agent", 4000, 600)]


def test_relay_rejecting_cache_control_falls_back_without_cache(monkeypatch):
    p, fake = provider([msg([text("ok")]), msg([text("lần hai")])])
    real = p._create
    calls = []

    def create(tier, **params):
        calls.append(params)
        if isinstance(params.get("system"), list):
            raise llm.LLMError("yêu cầu không hợp lệ: Extra inputs are not permitted: cache_control")
        return real(tier, **params)

    monkeypatch.setattr(p, "_create", create)
    assert p.run_agent("x", "", [], lambda n, a: {})["answer"] == "ok"
    assert len(calls) == 2 and isinstance(calls[1]["system"], str) and p.cache_ok is False
    # Lần sau không thử lại cache nữa.
    assert p.run_agent("y", "", [], lambda n, a: {})["answer"] == "lần hai"
    assert len(calls) == 3 and isinstance(calls[2]["system"], str)


def test_purpose_tags_structured_calls():
    p, _ = provider([msg([text(json.dumps({"task_type": "k8s_ops", "entities": []}))]), msg([text(json.dumps({"score": 0.5}))])])
    with llm.collect_usage() as got:
        p.route("xem pod")
        p.check_plan("xem pod", "k8s.list_pods", {})
    assert [u["purpose"] for u in got] == ["route", "check_plan"]


def test_openai_cached_tokens_are_split_out():
    resp = NS(usage=NS(prompt_tokens=1000, completion_tokens=50, prompt_tokens_details=NS(cached_tokens=800)), model="gpt-x")
    with llm.collect_usage() as got:
        assert llm.OpenAIProvider._tokens(resp) == 1050
    assert got == [{"purpose": "other", "model": "gpt-x", "input_tokens": 200, "output_tokens": 50, "cache_read": 800, "cache_write": 0}]


def test_metrics_endpoint_reports_tokens_cache_and_tool_sizes(users):
    alice = users["alice"]
    ep = alice.run("xem pod trong namespace default")
    with db.session() as conn:
        llm.save_usage(conn, alice.tenant, ep["id"], [
            {"purpose": "agent", "model": "m", "input_tokens": 1000, "output_tokens": 100, "cache_read": 3000, "cache_write": 0},
            {"purpose": "route", "model": "m", "input_tokens": 50, "output_tokens": 5, "cache_read": 0, "cache_write": 0}])
    t = alice.get("/api/metrics").json()["tokens"]
    assert t["cache_hit_pct"] == round(100 * 3000 / 4050, 1)
    assert {r["purpose"]: r["calls"] for r in t["by_purpose"]} == {"agent": 1, "route": 1}
    tool = next(x for x in t["tools"] if x["tool"] == "k8s.list_pods")
    assert tool["calls"] == 1 and tool["result_chars"] > 0
    assert t["top_turns"][0]["id"] == ep["id"] and t["top_turns"][0]["cache_read"] == 3000


def test_run_task_saves_usage_rows(users, monkeypatch):
    alice = users["alice"]

    class Counting(llm.HeuristicProvider):
        def route(self, task):
            with llm.purpose("route"):
                llm._record("m", 10, 2)
            return super().route(task)

    llm.set_provider(Counting())
    ep = alice.run("xem pod")
    with db.session() as conn:
        rows = conn.execute("SELECT purpose, input_tokens FROM llm_call WHERE episode_id=?", (ep["id"],)).fetchall()
    assert [(r["purpose"], r["input_tokens"]) for r in rows] == [("route", 10)]


def test_step_limit_forces_a_summary_instead_of_failing(monkeypatch):
    from kagent.config import settings
    monkeypatch.setattr(settings, "max_agent_steps", 2)
    loop = NS(content=[use("k8s__list_pods", {})], stop_reason="tool_use", usage=usage(), model="m")
    p, fake = provider([loop, loop, NS(content=[text("Tóm tắt: đã xem pod, chưa xong phần log.")], stop_reason="end_turn", usage=usage(), model="m")])
    out = p.run_agent("x", "", [{"tool": "k8s.list_pods", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
                      lambda n, a: {"status": "ok", "result": {}})
    assert out["answer"].endswith("Tóm tắt: đã xem pod, chưa xong phần log.")
    last = fake.calls[-1]
    assert last["tool_choice"] == {"type": "none"} and last["messages"][-1]["content"][-1]["text"] == llm.WRAP_UP


def test_reflector_view_clips_large_step_results():
    from kagent import learning
    ep = {"id": "e", "input": {"task": "x"}, "trace": [{"type": "tool_call", "tool": "logs.search_logs", "status": "ok", "result": {"hits": ["y" * 50] * 2000},
                                                      "confidence_parts": {"a": 1}}]}
    view = learning._episode_view(ep)
    step = view["trace"][0]
    assert isinstance(step["result"], str) and len(step["result"]) < 2000 and "đã cắt" in step["result"] and "confidence_parts" not in step
    assert ep["trace"][0]["result"]["hits"]  # không sửa episode gốc
