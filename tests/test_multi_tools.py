"""Chạy nhiều công cụ cùng lúc, SQL mô phỏng, exec vào pod."""
import threading
import time

import pytest

from conftest import tool_steps
from kagent import agent, db, gateway, llm
from kagent.backends import sim, sql
from kagent.backends.sim import ToolError


def state():
    return sim.initial_state()


def test_sql_query_reads_seed_data():
    out = sql.query(state(), {"sql": "SELECT status, count(*) n FROM orders GROUP BY status ORDER BY status"})
    assert out["columns"] == ["status", "n"] and ["failed", 2] in out["rows"]


@pytest.mark.parametrize("bad", [
    "DELETE FROM orders", "DROP TABLE orders", "ATTACH DATABASE 'x.db' AS x", "PRAGMA table_info(orders)",
    "SELECT load_extension('x')", "SELECT 1; DELETE FROM orders", "CREATE TABLE t(a)",
])
def test_sql_query_is_read_only(bad):
    st = state()
    with pytest.raises(ToolError):
        sql.query(st, {"sql": bad})
    assert len(st["sql"]["orders"]["rows"]) == 6


def test_sql_query_stops_runaway_and_caps_rows():
    with pytest.raises(ToolError, match="quá lâu"):
        sql.query(state(), {"sql": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c"})
    out = sql.query(state(), {"sql": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 500) SELECT x FROM c"})
    assert len(out["rows"]) == sql.MAX_ROWS and out["truncated"] is True


def test_sql_execute_persists_and_dry_run_does_not():
    st = state()
    assert sql.execute_check(st, {"sql": "UPDATE orders SET status='paid' WHERE status='failed'"})["would_affect_rows"] == 2
    assert sum(r[3] == "failed" for r in st["sql"]["orders"]["rows"]) == 2
    assert sql.execute(st, {"sql": "UPDATE orders SET status='paid' WHERE status='failed'"})["affected_rows"] == 2
    assert sum(r[3] == "failed" for r in st["sql"]["orders"]["rows"]) == 0
    with pytest.raises(ToolError):
        sql.execute(st, {"sql": "DROP TABLE orders"})


def test_exec_runs_psql_inside_pod():
    out = sim.k8s_exec(state(), {"pod": "api", "command": 'psql -U app -c "SELECT count(*) FROM customers"'})
    assert out["stdout"]["rows"] == [[4]]
    assert sim.k8s_exec(state(), {"pod": "api", "command": "ls"})["stdout"].startswith("(mô phỏng)")


def test_parallel_reads_run_concurrently(users, monkeypatch):
    """Ba truy vấn độc lập chạy song song: tổng thời gian gần bằng một truy vấn."""
    real = gateway._execute
    active, peak, lock = [0], [0], threading.Lock()

    def slow(conn, tenant_id, env, tool, args):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return real(conn, tenant_id, env, tool, args)

    monkeypatch.setattr(gateway, "_execute", slow)
    started = time.time()
    ep = users["alice"].run("chạy `SELECT count(*) FROM orders` và `SELECT count(*) FROM customers` và `SELECT max(amount) FROM orders`")
    steps = tool_steps(ep)
    assert [s["tool"] for s in steps] == ["sql.query"] * 3 and all(s["status"] == "ok" for s in steps)
    assert steps[0]["result"]["rows"] == [[6]] and steps[1]["result"]["rows"] == [[4]]
    assert peak[0] == 3 and time.time() - started < 0.85
    assert len({s["parallel"] for s in steps}) == 1


def test_parallel_batch_does_not_taint_its_own_members_but_taints_next(users):
    alice = users["alice"]
    alice.run("xem pod trong namespace default")
    ep = alice.run("tìm log lỗi rồi xem pod rồi tạo tài liệu tổng hợp")
    steps = tool_steps(ep)
    assert [s["tool"] for s in steps] == ["k8s.get", "kibana.search", "docs.create"]
    assert steps[0]["status"] == "ok" and steps[1]["status"] == "ok" and steps[0]["parallel"] == steps[1]["parallel"]
    assert steps[2]["status"] == "pending" and any("untrusted" in r for r in steps[2]["reasons"])


def test_exec_and_sql_in_one_request(users):
    alice, bob = users["alice"], users["bob"]
    ep = alice.run('exec vào pod api chạy `psql -c "SELECT count(*) FROM orders"` rồi chạy `SELECT status FROM orders WHERE id=102`')
    steps = tool_steps(ep)
    assert [s["tool"] for s in steps] == ["k8s.exec", "sql.query"]
    assert steps[0]["status"] == "pending" and steps[0]["risk_tier"] == "R4"  # exec luôn phải hỏi
    assert steps[1]["status"] == "ok" and steps[1]["result"]["rows"] == [["failed"]]
    approved = bob.post(f"/api/approvals/{steps[0]['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert approved["result"]["result"]["stdout"]["rows"] == [[6]]


def test_parallel_calls_each_audited_and_gated(users):
    alice = users["alice"]
    alice.run("chạy `SELECT 1` và `SELECT 2`")
    audit = [a for a in alice.get("/api/audit").json() if a["tool"] == "sql.query"]
    assert len(audit) == 2 and all(a["decision"] == "auto" for a in audit)


def test_anthropic_runs_parallel_tool_uses_in_one_batch():
    from types import SimpleNamespace as NS
    calls, batches = [], []

    class Fake:
        def __init__(self):
            self.replies = [NS(content=[NS(type="tool_use", name="sql__query", input={"sql": "SELECT 1"}, id="a"),
                                        NS(type="tool_use", name="sql__query", input={"sql": "SELECT 2"}, id="b")], stop_reason="tool_use", usage=NS(input_tokens=1, output_tokens=1)),
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
    assert batches == [[("sql.query", {"sql": "SELECT 1"}), ("sql.query", {"sql": "SELECT 2"})]]
    results = calls[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_cancel_before_parallel_batch(users):
    alice = users["alice"]
    cancelled = threading.Event()
    cancelled.set()
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ep = agent.run_task(conn, alice.tenant, uid, "chạy `SELECT 1` và `SELECT 2`", on_event=lambda e: None, cancelled=cancelled)
    assert ep["trace"][-1]["type"] == "cancelled" and not tool_steps(ep)
