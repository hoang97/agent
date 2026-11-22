"""Đo token theo từng lần gọi LLM và prompt caching trong vòng lặp agent."""
import json
from types import SimpleNamespace as NS

from kagent import db, llm, vault
from test_llm import msg, provider, text, use


def usage(i=100, o=20, read=0, write=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=read, cache_creation_input_tokens=write)


def test_agent_loop_marks_cache_breakpoints_and_records_usage():
    p, fake = provider([NS(content=[use("k8s__kubectl_read", {})], stop_reason="tool_use", usage=usage(500, 30, 0, 4000), model="claude-opus-5-5"),
                        NS(content=[text("xong")], stop_reason="end_turn", usage=usage(80, 10, 4000, 600), model="claude-opus-5-5")])
    tools = [{"tool": "k8s.kubectl_read", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    with llm.collect_usage() as got:
        p.run_agent("xem pod", "ctx", tools, lambda name, args: {"status": "ok", "result": {"pods": []}})
    first, second = fake.calls
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
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
    p, _ = provider([msg([text(json.dumps({"task_type": "k8s_ops", "entities": []}))]), msg([text(json.dumps({"block": False}))])])
    with llm.collect_usage() as got:
        p.route("xem pod")
        assert p.classify_action({"action": {}})["allow"] is True
    assert [u["purpose"] for u in got] == ["route", "auto_mode"]


def test_openai_cached_tokens_are_split_out():
    resp = NS(usage=NS(prompt_tokens=1000, completion_tokens=50, prompt_tokens_details=NS(cached_tokens=800)), model="gpt-x")
    p = llm.OpenAIProvider(client=object(), credential=vault.Credential("k", provider="openai", model_strong="gpt-x", profile="LLM nội bộ"))
    with llm.collect_usage() as got:
        assert p._tokens(resp) == 530
        assert p._tokens(NS(usage=resp.usage)) == 530  # phản hồi stream không có tên model: lấy model đã gọi
    assert got[0] == {"purpose": "other", "model": "gpt-x", "input_tokens": 200, "output_tokens": 50, "cache_read": 800, "cache_write": 0, "llm": "LLM nội bộ",
                      "cache_known": True, "cache_write_1h": 0}
    assert got[1]["model"] == "gpt-x"


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
    tool = next(x for x in t["tools"] if x["tool"] == "k8s.kubectl_read")
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
    loop = NS(content=[use("k8s__kubectl_read", {})], stop_reason="tool_use", usage=usage(), model="m")
    p, fake = provider([loop, loop, NS(content=[text("Tóm tắt: đã xem pod, chưa xong phần log.")], stop_reason="end_turn", usage=usage(), model="m")])
    out = p.run_agent("x", "", [{"tool": "k8s.kubectl_read", "description": "d", "input_schema": {"type": "object", "properties": {}}}],
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


def test_metrics_split_usage_by_llm_and_day(users):
    alice = users["alice"]
    with db.session() as conn:
        llm.save_usage(conn, alice.tenant, None, [
            {"purpose": "agent", "model": "claude-opus-5", "llm": "relay", "input_tokens": 10, "output_tokens": 5, "cache_read": 100, "cache_write": 0},
            {"purpose": "route", "model": "claude-sonnet-5", "llm": "relay", "input_tokens": 3, "output_tokens": 1, "cache_read": 0, "cache_write": 0},
            {"purpose": "agent", "model": "qwen", "llm": "LLM nội bộ", "input_tokens": 50, "output_tokens": 20, "cache_read": 0, "cache_write": 0}])
    t = alice.get("/api/metrics").json()["tokens"]
    assert {r["label"]: r["total"] for r in t["by_llm"]} == {"relay · claude-opus-5": 115, "relay · claude-sonnet-5": 4, "LLM nội bộ · qwen": 70}
    assert len(t["llm_days"]) == 14 and t["llm_days"][-1] == db.iso().split("T")[0]
    assert sum(r["total"] for r in t["llm_daily"]) == 189 and {r["day"] for r in t["llm_daily"]} == {t["llm_days"][-1]}


def test_turn_context_goes_into_the_task_message_not_the_system():
    p, fake = provider([msg([text("a")]), msg([text("b")])])
    p.run_agent("xem pod", "<knowledge section=\"core\">luật</knowledge>", [], lambda n, a: {}, turn_context="<knowledge section=\"units\">ku A</knowledge>")
    p.run_agent("xem log", "<knowledge section=\"core\">luật</knowledge>", [], lambda n, a: {}, turn_context="<knowledge section=\"units\">ku B</knowledge>")
    first, second = fake.calls
    assert first["system"] == second["system"] and "ku A" not in first["system"][0]["text"]
    blocks = second["messages"][-1]["content"]
    assert "ku B" in blocks[0]["text"] and blocks[-1]["text"] == "xem log"


def test_openai_turn_context_is_prepended_to_the_task():
    seen = []
    resp = NS(choices=[NS(message=NS(content="ok", tool_calls=None), finish_reason="stop")], usage=None, model="m")
    client = NS(chat=NS(completions=NS(create=lambda **kw: seen.append(json.loads(json.dumps(kw))) or resp)))
    p = llm.OpenAIProvider(client=client, credential=vault.Credential("k", provider="openai", model_strong="m"))
    p.run_agent("xem pod", "luật", [], lambda n, a: {}, turn_context="ku A")
    system, task = seen[0]["messages"][0], seen[0]["messages"][-1]
    assert "ku A" not in system["content"] and task["content"].startswith("<context") and task["content"].endswith("xem pod")


def test_relay_rejecting_ttl_keeps_default_cache(monkeypatch):
    p, fake = provider([msg([text("ok")]), msg([text("lần hai")])])
    real = p._create
    calls = []

    def create(tier, **params):
        calls.append(params)
        if "ttl" in json.dumps(params.get("system"), default=str):
            raise llm.LLMError("yêu cầu không hợp lệ: system.0.cache_control.ttl: Extra inputs are not permitted")
        return real(tier, **params)

    monkeypatch.setattr(p, "_create", create)
    assert p.run_agent("x", "", [], lambda n, a: {})["answer"] == "ok"
    assert calls[-1]["system"][0]["cache_control"] == {"type": "ephemeral"} and p.cache_ok is True and p.ttl_ok is False
    assert p.run_agent("y", "", [], lambda n, a: {})["answer"] == "lần hai"
    assert len(calls) == 3


def test_structured_calls_cache_system_and_classifier_shares_rules_prefix():
    p, fake = provider([msg([text(json.dumps({"block": False}))]), msg([text(json.dumps({"block": False}))])])
    rules = {"hard_deny": ["luật"]}
    p.classify_action({"rules": rules, "user_intent": ["scale web"], "previous_calls": [], "action": {"tool": "a"}})
    p.classify_action({"rules": rules, "user_intent": ["scale web"], "previous_calls": [{"tool": "a"}], "action": {"tool": "b"}})
    first, second = fake.calls
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    head, tail = first["messages"][0]["content"]
    assert head["cache_control"] == {"type": "ephemeral"} and "cache_control" not in tail
    assert json.loads(head["text"]) == {"rules": rules, "user_intent": ["scale web"]} and json.loads(tail["text"])["action"] == {"tool": "a"}
    # Lệnh sau trong cùng lượt: phần đầu giống hệt, chỉ phần cuối đổi.
    assert second["messages"][0]["content"][0] == head and json.loads(second["messages"][0]["content"][1]["text"])["action"] == {"tool": "b"}


def test_cache_hit_rate_ignores_llms_that_do_not_report_cache(users):
    alice = users["alice"]
    unreported = NS(usage=NS(prompt_tokens=1000, completion_tokens=5, prompt_tokens_details=None), model="qwen")
    p = llm.OpenAIProvider(client=object(), credential=vault.Credential("k", provider="openai", model_strong="qwen", profile="AI Gateway"))
    with llm.collect_usage() as got:
        p._tokens(unreported)
    assert got[0]["cache_known"] is False
    with db.session() as conn:
        llm.save_usage(conn, alice.tenant, None, [
            *got, {"purpose": "agent", "model": "claude-opus-5", "llm": "relay", "input_tokens": 10, "output_tokens": 5, "cache_read": 90, "cache_write": 0}])
    t = alice.get("/api/metrics").json()["tokens"]
    assert t["cache_hit_pct"] == 90.0 and t["cache_unmeasured"] == 1000
    assert {r["label"]: r["cache_known"] for r in t["by_llm"]} == {"AI Gateway · qwen": 0, "relay · claude-opus-5": 1}


def test_metrics_report_knowledge_sent_and_skipped(users):
    alice = users["alice"]
    ep = alice.run("xem pod trong namespace default")
    with db.session() as conn:
        trace = json.loads(conn.execute("SELECT trace FROM episode WHERE id=?", (ep["id"],)).fetchone()[0])
        ctx = next(s for s in trace if s["type"] == "context")
        llm.save_usage(conn, alice.tenant, ep["id"], [
            {"purpose": "agent", "model": "m", "input_tokens": 1000, "output_tokens": 10, "cache_read": 1000, "cache_write": 0}] * 2)
    t = alice.get("/api/metrics").json()["tokens"]
    k = t["knowledge"]
    assert k["turns"] >= 1 and k["sent_chars"] >= ctx["knowledge_chars"] and k["skipped_chars"] >= 0
    assert k["tokens"] >= ctx["knowledge_chars"] // 4 * 2 and k["agent_input"] >= 4000
    row = next(r for r in t["top_turns"] if r["id"] == ep["id"])
    assert row["knowledge_chars"] == ctx["knowledge_chars"] and row["skipped_chars"] == ctx["skipped_chars"]


def test_tenant_route_key_goes_into_metadata_user_id():
    p, fake = provider([msg([text("a")]), msg([text("b")]), msg([text("c")]), msg([text("d")])])
    with llm.collect_usage("tenant-a"):
        p.run_agent("x", "", [], lambda n, a: {})
        p.run_agent("y", "", [], lambda n, a: {})
    with llm.collect_usage("tenant-b"):
        p.run_agent("z", "", [], lambda n, a: {})
    with llm.collect_usage():
        p.run_agent("w", "", [], lambda n, a: {})
    keys = [c.get("metadata", {}).get("user_id") for c in fake.calls]
    assert keys[0] == keys[1] and keys[2] not in (None, keys[0]) and keys[3] is None
    assert "tenant-a" not in keys[0]
    assert fake.calls[0]["fallbacks"] == "default"  # metadata không làm mất fallback phía server


def test_one_hour_cache_writes_are_recorded_and_reported(users):
    alice = users["alice"]
    u = NS(**vars(usage(10, 5, 0, 900)), cache_creation=NS(ephemeral_1h_input_tokens=700, ephemeral_5m_input_tokens=200))
    p, _ = provider([NS(content=[text("ok")], stop_reason="end_turn", usage=u, model="claude-opus-5")])
    with llm.collect_usage(alice.tenant) as got:
        p.run_agent("x", "", [], lambda n, a: {})
    assert got[0]["cache_write"] == 900 and got[0]["cache_write_1h"] == 700
    with db.session() as conn:
        llm.save_usage(conn, alice.tenant, None, got)
    t = alice.get("/api/metrics").json()["tokens"]
    assert [(r["cache_write"], r["cache_write_1h"]) for r in t["by_purpose"]] == [(900, 700)]
    assert t["by_llm"][0]["cache_write_1h"] == 700


def test_record_returns_cost_weighted_tokens():
    # input 1×, cache đọc 0,1×, ghi 5 phút 1,25×, ghi 1 giờ 2×, output 5×
    assert llm.weighted(100, 20, cache_read=1000, cache_write=400, cache_write_1h=100) == 100 + 100 + 375 + 200 + 100
    assert llm._record("m", 100, 20, 1000, 400, cache_write_1h=100) == 875


def test_reflector_view_drops_transcript():
    from kagent import learning
    ep = {"id": "e", "input": {"task": "x"}, "trace": [], "transcript": json.dumps([{"role": "user", "content": "y" * 5000}])}
    assert "transcript" not in learning._episode_view(ep) and "transcript" in ep


def test_learning_calls_add_to_episode_tokens(users):
    from kagent import learning
    alice = users["alice"]
    ep = alice.run("xem pod trong namespace default")

    class Reflecting(llm.HeuristicProvider):
        def reflect(self, episode):
            with llm.purpose("reflect"):
                llm._record("m", 1000, 10)
            return []

    with db.session() as conn:
        before = conn.execute("SELECT tokens FROM episode WHERE id=?", (ep["id"],)).fetchone()["tokens"]
        llm.set_provider(Reflecting())
        learning.run_learning(conn, alice.tenant)
        after = conn.execute("SELECT tokens FROM episode WHERE id=?", (ep["id"],)).fetchone()["tokens"]
    assert after - before == 1050


def test_metrics_report_weighted_cost_and_learning_share(users):
    alice = users["alice"]
    ep = alice.run("xem pod trong namespace default")
    with db.session() as conn:
        conn.execute("DELETE FROM llm_call")
        llm.save_usage(conn, alice.tenant, ep["id"], [
            {"purpose": "agent", "model": "m", "input_tokens": 0, "output_tokens": 100, "cache_read": 10000, "cache_write": 800},
            {"purpose": "reflect", "model": "m", "input_tokens": 2000, "output_tokens": 0, "cache_read": 0, "cache_write": 0}])
    t = alice.get("/api/metrics").json()["tokens"]
    assert {r["purpose"]: r["cost"] for r in t["by_purpose"]} == {"agent": 500 + 1000 + 1000, "reflect": 2000}
    top = t["top_turns"][0]
    assert (top["id"], top["llm_calls"], top["cache_read"], top["cost"], top["learn_cost"]) == (ep["id"], 1, 10000, 4500, 2000)
    assert t["by_llm"][0]["cost"] == 4500


def test_schema_migration_recomputes_episode_tokens_once(users):
    alice = users["alice"]
    ep = alice.run("xem pod")
    with db.session() as conn:
        conn.execute("DELETE FROM llm_call")
        llm.save_usage(conn, alice.tenant, ep["id"], [{"purpose": "agent", "model": "m", "input_tokens": 0, "output_tokens": 10, "cache_read": 5000, "cache_write": 0}])
        conn.execute("UPDATE episode SET tokens=5010 WHERE id=?", (ep["id"],))
        conn.execute("DELETE FROM app_setting WHERE key='episode_tokens_weighted'")
        db.init_schema(conn)
        assert conn.execute("SELECT tokens FROM episode WHERE id=?", (ep["id"],)).fetchone()["tokens"] == 550
        conn.execute("UPDATE episode SET tokens=7 WHERE id=?", (ep["id"],))
        db.init_schema(conn)
        assert conn.execute("SELECT tokens FROM episode WHERE id=?", (ep["id"],)).fetchone()["tokens"] == 7
