"""Chạy nhiều công cụ cùng lúc trên connector thật (hạ tầng giả ở tầng HTTP và SQLite)."""
import threading
import time

from conftest import tool_steps
from kagent import agent, db, gateway, llm


def test_parallel_reads_run_concurrently(users, monkeypatch):
    real = gateway._run
    active, peak, lock = [0], [0], threading.Lock()

    def slow(conn, tenant_id, tool_name, args, check=False, user_id=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return real(conn, tenant_id, tool_name, args, check, user_id)

    monkeypatch.setattr(gateway, "_run", slow)
    started = time.time()
    ep = users["alice"].run("chạy `SELECT count(*) FROM orders` và `SELECT count(*) FROM customers` và `SELECT max(amount) FROM orders`")
    steps = tool_steps(ep)
    assert [s["tool"] for s in steps] == ["db.query"] * 3 and all(s["status"] == "ok" for s in steps)
    assert steps[0]["result"]["rows"] == [[6]] and steps[1]["result"]["rows"] == [[4]]
    assert peak[0] == 3 and time.time() - started < 0.85
    assert len({s["parallel"] for s in steps}) == 1


def test_parallel_reads_then_write_waits_for_auto_mode(users):
    alice = users["alice"]
    alice.run("xem pod trong namespace default")
    steps = tool_steps(alice.run("tìm log lỗi rồi xem pod rồi restart deployment web"))
    assert [s["tool"] for s in steps] == ["k8s.kubectl_read", "logs.search_logs", "k8s.kubectl_write"]
    assert steps[0]["status"] == "ok" and steps[1]["status"] == "ok" and steps[0]["parallel"] == steps[1]["parallel"]
    # Không có model thì auto mode không xét được lệnh ghi: chuyển sang hỏi người.
    assert steps[2]["status"] == "pending" and "không dùng model" in steps[2]["reasons"][0]


def test_exec_and_sql_in_one_request(users):
    alice = users["alice"]
    steps = tool_steps(alice.run('exec vào pod api-0 chạy `psql -c "SELECT count(*) FROM orders"` rồi chạy `SELECT status FROM orders WHERE id=102`'))
    assert [s["tool"] for s in steps] == ["db.query", "k8s.kubectl_admin"]  # đọc trước, exec sau
    assert steps[1]["status"] == "pending" and steps[1]["risk_tier"] == "R4"  # exec luôn phải hỏi
    assert steps[0]["status"] == "ok" and steps[0]["result"]["rows"] == [["failed"]]
    approved = alice.post(f"/api/approvals/{steps[1]['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert "6" in approved["result"]["result"]["stdout"] and "-- psql" in approved["result"]["result"]["command"]


def test_parallel_calls_each_audited_and_gated(users):
    alice = users["alice"]
    alice.run("chạy `SELECT 1` và `SELECT 2`")
    audit = [a for a in alice.get("/api/audit").json() if a["tool"] == "db.query"]
    assert len(audit) == 2 and all(a["decision"] == "auto" for a in audit)


def test_anthropic_runs_parallel_tool_uses_in_one_batch():
    from types import SimpleNamespace as NS
    calls, batches = [], []

    class Fake:
        def __init__(self):
            self.replies = [NS(content=[NS(type="tool_use", name="db__query", input={"sql": "SELECT 1"}, id="a"),
                                        NS(type="tool_use", name="db__query", input={"sql": "SELECT 2"}, id="b")], stop_reason="tool_use", usage=NS(input_tokens=1, output_tokens=1)),
                            NS(content=[NS(type="text", text="xong")], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1))]

        def create(self, **kw):
            calls.append(kw)
            return self.replies.pop(0)

    fake = Fake()
    p = llm.AnthropicProvider(NS(messages=fake, beta=NS(messages=fake)))

    def call_tool(n, a):
        raise AssertionError("phải đi qua batch")

    call_tool.batch = lambda items: batches.append(items) or [{"status": "ok", "result": i} for i, _ in enumerate(items)]
    p.run_agent("x", "c", [], call_tool)
    assert batches == [[("db.query", {"sql": "SELECT 1"}), ("db.query", {"sql": "SELECT 2"})]]
    assert [r["tool_use_id"] for r in calls[1]["messages"][2]["content"]] == ["a", "b"]


def test_cancel_before_parallel_batch(users):
    alice = users["alice"]
    cancelled = threading.Event()
    cancelled.set()
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ep = agent.run_task(conn, alice.tenant, uid, "chạy `SELECT 1` và `SELECT 2`", on_event=lambda e: None, cancelled=cancelled)
    assert ep["trace"][-1]["type"] == "cancelled" and not tool_steps(ep)


class Delegating:
    """Agent chính giao việc qua agent.delegate; agent con chạy kịch bản sub(brief, tools, call_tool)."""

    name = "scripted"

    def __init__(self, main, sub):
        self.main, self.sub, self.briefs = main, sub, []

    def route(self, task):
        return {"task_type": "general", "entities": []}

    def run_agent(self, task, context, tools, call_tool, **kw):
        self.tools = {t["tool"] for t in tools}
        return {"answer": self.main(call_tool), "tokens": 10}

    def run_subagent(self, brief, tools, call_tool, cancelled=None):
        self.briefs.append((brief, sorted(t["tool"] for t in tools)))
        return {"answer": self.sub(brief, call_tool), "tokens": 5}


def _run_delegating(users, provider, events=None):
    llm.set_provider(provider)
    try:
        with db.session() as conn:
            uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
            return agent.run_task(conn, users["alice"].tenant, uid, "đếm đơn hàng và khách hàng", on_event=events.append if events is not None else None)
    finally:
        llm.set_provider(None)


def test_delegated_subagents_run_in_parallel_with_only_the_brief(users):
    seen = {}

    def main(call_tool):
        seen["steps"] = call_tool.batch([(agent.DELEGATE_TOOL, {"task": "Đếm số đơn trong bảng orders", "tools": ["db.query"]}),
                                         (agent.DELEGATE_TOOL, {"task": "Đếm số khách trong bảng customers", "tools": ["db.query"]})])
        return "xong"

    def sub(brief, call_tool):
        table = "orders" if "orders" in brief else "customers"
        rows = call_tool("db.query", {"sql": f"SELECT count(*) FROM {table}"})["result"]["rows"]
        assert call_tool("k8s.kubectl_read", {"command": "kubectl get pods"})["status"] == "blocked"  # ngoài tools được giao
        return f"{table}: {rows[0][0]}"

    provider, events = Delegating(main, sub), []
    ep = _run_delegating(users, provider, events)
    assert agent.DELEGATE_TOOL in provider.tools
    first, second = seen["steps"]
    assert {first["result"]["text"], second["result"]["text"]} == {"orders: 6", "customers: 4"}
    assert first["parallel"] == second["parallel"] and {first["sub_id"], second["sub_id"]} == {1, 2}
    # Agent con chỉ nhận môi trường + bản giao việc, không có lịch sử hay tri thức; data.query được thêm để đọc tiếp kết quả lớn.
    assert all(b.startswith("Môi trường: dev\n\n") and tools == ["data.query", "db.query"] for b, tools in provider.briefs)
    subs = [s for s in tool_steps(ep) if s.get("sub")]
    assert sorted((s["tool"], s["status"]) for s in subs) == [("db.query", "ok")] * 2 + [("k8s.kubectl_read", "blocked")] * 2
    assert all(len(s["steps"]) == 2 for s in (first, second))
    assert all(e.get("sub") for e in events if e["type"] == "tool_start" and e["tool"] != agent.DELEGATE_TOOL)
    assert ep["tokens"] == 20


def test_delegate_rejects_unknown_or_write_tools(users):
    seen = {}

    def main(call_tool):
        seen["step"] = call_tool(agent.DELEGATE_TOOL, {"task": "restart web", "tools": ["k8s.kubectl_write"]})
        return "xong"

    provider = Delegating(main, lambda b, c: "không được gọi")
    _run_delegating(users, provider)
    assert seen["step"]["status"] == "invalid" and "k8s.kubectl_write" in seen["step"]["reasons"][0] and "db.query" in seen["step"]["reasons"][0]
    assert provider.briefs == []


def test_anthropic_subagent_uses_fast_model_and_short_prompt():
    from types import SimpleNamespace as NS
    calls = []

    class Fake:
        def create(self, **kw):
            calls.append(kw)
            return NS(content=[NS(type="text", text="6 đơn")], stop_reason="end_turn", model=kw["model"], usage=NS(input_tokens=1, output_tokens=1))

        def stream(self, **kw):
            raise AssertionError("dùng create trong test")

    fake = Fake()
    p = llm.AnthropicProvider(NS(messages=fake, beta=NS(messages=fake)))
    p._create = lambda tier, on_text=None, cancelled=None, **kw: fake.create(model=p.models[tier], **kw)
    tools = [{"tool": "db.query", "description": "SQL", "input_schema": {"type": "object"}}]
    with llm.collect_usage() as usage:
        out = p.run_subagent("Môi trường: dev\n\nĐếm đơn", tools, lambda n, a: {})
    assert out["answer"] == "6 đơn" and calls[0]["model"] == p.models["fast"]
    assert calls[0]["system"][0]["text"] == llm.SUBAGENT_SYSTEM and calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in calls[0]["tools"]] == ["db__query"] and calls[0]["messages"][0]["content"][0]["text"] == "Môi trường: dev\n\nĐếm đơn"
    assert usage[0]["purpose"] == "subagent"


def test_writes_run_alone_and_in_order_between_parallel_reads(users):
    seen = {}

    def main(call_tool):
        seen["steps"] = call_tool.batch([("db.query", {"sql": "SELECT 1"}), ("db.query", {"sql": "SELECT 2"}),
                                         ("k8s.kubectl_write", {"command": "rollout restart deployment/web -n default"}),
                                         ("db.query", {"sql": "SELECT 3"})])
        return "xong"

    events = []
    _run_delegating(users, Delegating(main, None), events)
    r1, r2, w, r3 = seen["steps"]
    assert r1["parallel"] == r2["parallel"] and "parallel" not in w and "parallel" not in r3
    order = [(e["type"], e.get("tool") or e["step"]["tool"]) for e in events if e["type"] in ("tool_start", "tool_end")]
    # Lệnh ghi chỉ bắt đầu khi hai lệnh đọc trước đã xong, lệnh đọc sau chỉ bắt đầu khi lệnh ghi đã xong.
    assert order[4:] == [("tool_start", "k8s.kubectl_write"), ("tool_end", "k8s.kubectl_write"), ("tool_start", "db.query"), ("tool_end", "db.query")]


def _peak_reads(users, monkeypatch, n):
    real = gateway._run
    active, peak, lock = [0], [0], threading.Lock()

    def slow(conn, tenant_id, tool_name, args, check=False, user_id=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return None

    monkeypatch.setattr(gateway, "_run", slow)
    users["alice"].run(" và ".join(f"`SELECT {i}`" for i in range(n)))
    monkeypatch.setattr(gateway, "_run", real)
    return peak[0]


def test_tenant_can_only_lower_parallel_limit(users, monkeypatch):
    admin = users["admin"]
    assert _peak_reads(users, monkeypatch, 5) == 5
    assert admin.put("/api/policy", json={"overlay": {"max_parallel_tools": 20}}).status_code == 400
    assert admin.put("/api/policy", json={"overlay": {"max_parallel_tools": 2}}).status_code == 200
    assert _peak_reads(users, monkeypatch, 4) == 2


def test_one_connection_runs_at_most_per_connection_calls(users, monkeypatch):
    monkeypatch.setattr(gateway, "PER_CONNECTION", 2)
    gateway._slots.clear()
    active, peak, lock = [0], [0], threading.Lock()

    def work():
        with gateway.connection_slot("c1"):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    gateway._slots.clear()
    assert peak[0] == 2


def _slow_sql(monkeypatch, marker, seconds):
    real = gateway._run

    def run(conn, tenant_id, tool_name, args, check=False, user_id=None):
        if marker in str(args.get("sql", "")):
            time.sleep(seconds)
        return real(conn, tenant_id, tool_name, args, check, user_id)

    monkeypatch.setattr(gateway, "_run", run)


def test_background_job_result_arrives_after_other_work(users, monkeypatch):
    _slow_sql(monkeypatch, "orders", 0.5)
    seen = {}

    def main(call_tool):
        started = time.monotonic()
        seen["bg"] = call_tool(agent.BACKGROUND_TOOL, {"tool": "db.query", "args": {"sql": "SELECT count(*) FROM orders"}})
        seen["quick"] = call_tool("db.query", {"sql": "SELECT 1"})
        seen["quick_s"] = time.monotonic() - started
        seen["early"] = call_tool.drain()  # lệnh nền chưa xong: chưa có gì
        seen["final"] = call_tool.drain(final=True)  # định kết thúc lượt: đợi lệnh nền
        return "xong"

    ep = _run_delegating(users, Delegating(main, None))
    assert seen["bg"]["status"] == "ok" and seen["bg"]["result"]["job_id"] == "bg1"
    assert seen["quick"]["status"] == "ok" and seen["quick_s"] < 0.4 and seen["early"] is None
    assert seen["final"].startswith("[Lệnh nền bg1 (db.query) đã xong]") and "[[6]]" in seen["final"]
    bg = next(s for s in tool_steps(ep) if s.get("background") == "bg1")
    assert bg["tool"] == "db.query" and bg["result"]["rows"] == [[6]]


def test_background_only_takes_read_tools(users):
    seen = {}

    def main(call_tool):
        seen["step"] = call_tool(agent.BACKGROUND_TOOL, {"tool": "k8s.kubectl_write", "args": {"command": "rollout restart deployment/web"}})
        return "xong"

    _run_delegating(users, Delegating(main, None))
    assert seen["step"]["status"] == "invalid" and "db.query" in seen["step"]["reasons"][0]


def test_unfinished_background_job_is_dropped_at_end_of_turn(users, monkeypatch):
    _slow_sql(monkeypatch, "customers", 0.5)

    def main(call_tool):
        call_tool(agent.BACKGROUND_TOOL, {"tool": "db.query", "args": {"sql": "SELECT count(*) FROM customers"}})
        return "xong"

    ep = _run_delegating(users, Delegating(main, None))
    assert {"type": "background_dropped", "jobs": {"bg1": "db.query"}} in ep["trace"]
    time.sleep(0.7)
    with db.session() as conn:
        trace = agent.get_episode(conn, users["alice"].tenant, ep["id"])["trace"]
    assert not any(s.get("background") for s in trace)  # kết quả đến muộn không chen vào episode đã ghi
