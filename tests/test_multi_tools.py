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
