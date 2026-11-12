"""Nút dừng: dừng ngay việc chỉ đọc (stream model, lệnh đọc, kubectl), đợi lệnh ghi xong; ai được dừng lượt nào."""
import stat
import threading
import time

import pytest

from kagent import agent, db, gateway, incidents, live, llm
from kagent.connectors import kubectl
from kagent.connectors.base import ConnectorError


def test_abort_on_closes_blocked_stream_from_other_thread():
    flag, closed = live.StopFlag(), threading.Event()

    def blocked():
        closed.wait(10)  # như socket đang chờ token đầu tiên
        raise OSError("connection closed")

    threading.Timer(0.2, flag.set).start()
    started = time.monotonic()
    with pytest.raises(llm.Cancelled):
        with llm.abort_on(flag, closed.set):
            blocked()
    assert time.monotonic() - started < 2


def test_abort_on_passes_real_errors_through():
    with pytest.raises(OSError):
        with llm.abort_on(live.StopFlag(), lambda: None):
            raise OSError("lỗi thật, không phải do dừng")


@pytest.fixture()
def slow_kubectl(tmp_path, monkeypatch):
    script = tmp_path / "kubectl"
    script.write_text("#!/bin/sh\nsleep \"${SLEEP:-10}\"\necho done\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(kubectl, "KUBECTL", str(script))
    monkeypatch.setattr(kubectl, "runner", None)
    return script


def test_read_only_kubectl_killed_on_stop(slow_kubectl):
    flag = live.StopFlag()
    token = live.current_stop.set(flag)
    try:
        threading.Timer(0.3, flag.set).start()
        started = time.monotonic()
        with pytest.raises(ConnectorError, match="đã dừng"):
            kubectl._exec(["get", "pods"], "apiVersion: v1\n", None, 30, interruptible=True)
        assert time.monotonic() - started < 3
    finally:
        live.current_stop.reset(token)


def test_write_kubectl_runs_to_end_despite_stop(slow_kubectl, monkeypatch):
    slow_kubectl.write_text("#!/bin/sh\nsleep 1\necho applied\n")
    flag = live.StopFlag()
    flag.set()
    token = live.current_stop.set(flag)
    try:
        code, out, _, timed_out = kubectl._exec(["apply", "-f", "-"], "apiVersion: v1\n", "kind: X", 30, interruptible=False)
    finally:
        live.current_stop.reset(token)
    assert code == 0 and out.strip() == "applied" and not timed_out


class Slow(llm.HeuristicProvider):
    """Agent gọi một lệnh rồi trả lời; lệnh chạy chậm để kịp bấm dừng."""

    def __init__(self, tool, args):
        self.tool, self.args = tool, args

    def run_agent(self, task, context, tool_list, call_tool, **kw):
        step = call_tool(self.tool, self.args)
        return {"answer": step["status"], "tokens": 0}


def _slow_invoke(monkeypatch, seconds):
    real = gateway.invoke

    def invoke(conn, ctx, name, args):
        time.sleep(seconds)
        return real(conn, ctx, name, args)

    monkeypatch.setattr(gateway, "invoke", invoke)


def test_stop_abandons_running_read_at_once(users, monkeypatch):
    _slow_invoke(monkeypatch, 3)
    llm.set_provider(Slow("k8s.kubectl_read", {"command": "get pods"}))
    flag = live.StopFlag()
    threading.Timer(0.3, lambda: flag.stop("u-1")).start()
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        started = time.monotonic()
        ep = agent.run_task(conn, users["alice"].tenant, uid, "xem pod", cancelled=flag)
    assert time.monotonic() - started < 2
    assert {"type": "cancelled", "by": "u-1"} in ep["trace"]


def test_stop_waits_for_running_write(users, monkeypatch):
    _slow_invoke(monkeypatch, 1)
    llm.set_provider(Slow("k8s.kubectl_write", {"command": "scale deployment/web --replicas=2"}))
    flag, events = live.StopFlag(), []
    threading.Timer(0.2, lambda: flag.stop("u-1")).start()
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ep = agent.run_task(conn, users["alice"].tenant, uid, "scale", cancelled=flag, on_event=events.append)
    stopping = [e for e in events if e["type"] == "stopping"]
    assert stopping and stopping[0]["waiting_for"] == ["k8s.kubectl_write"]
    assert any(e["type"] == "tool_end" for e in events)  # lệnh ghi chạy xong (thành yêu cầu duyệt) trước khi dừng
    assert any(s["type"] == "cancelled" for s in ep["trace"])


def test_who_may_stop(users):
    alice, bob = users["alice"], users["bob"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        cid = db.new_id()
        conn.execute("INSERT INTO conversation(id,tenant_id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                     (cid, alice.tenant, uid, "riêng", db.iso(), db.iso()))
    run = live.start(cid, "r1", "x", owner=uid)
    try:
        assert bob.post(f"/api/conversations/{cid}/stop").status_code == 404  # không thấy hội thoại riêng của người khác
        assert alice.post(f"/api/conversations/{cid}/stop").json() == {"ok": True} and run.stop.is_set() and run.stop.by == uid
    finally:
        live.finish(run)
    assert alice.post(f"/api/conversations/{cid}/stop").status_code == 404  # hết lượt chạy


def test_member_stops_incident_investigation(users, infra):
    admin, alice = users["admin"], users["alice"]
    admin.post("/api/connections", json={"template": "alertmanager", "name": "am", "scope": "tenant", "envs": ["dev"], "roles": ["member"],
                                         "params": {"url": "https://am.test", "username": "am"}, "secrets": {"password": "pw"}})
    admin.put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "investigate": False})
    with db.session() as conn:
        incidents.poll(conn, admin.tenant)
        iid = conn.execute("SELECT id FROM incident WHERE title='HighCPU · web'").fetchone()["id"]

    class Waiting(llm.HeuristicProvider):
        def run_agent(self, task, context, tool_list, call_tool, cancelled=None, **kw):
            call_tool("k8s.kubectl_read", {"command": "get pods -n web"})
            while not cancelled.wait(0.05):
                pass
            raise llm.Cancelled()

    llm.set_provider(Waiting())
    worker = threading.Thread(target=_investigate, args=(admin.tenant, iid))
    worker.start()
    for _ in range(100):
        cid = admin.get(f"/api/incidents/{iid}").json()["conversation_id"]
        if cid and live.current(cid):
            break
        time.sleep(0.05)
    assert alice.post(f"/api/conversations/{cid}/stop").json() == {"ok": True}
    worker.join(5)
    inc = admin.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "needs_human" and not inc["actions"]
    assert any(e["kind"] == "stopped" and "alice" in e["text"] for e in inc["timeline"])


def _investigate(tenant_id, iid):
    with db.session() as conn:
        incidents.investigate(conn, tenant_id, iid)
