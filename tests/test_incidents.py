"""Luồng tự xử lý cảnh báo: poll, gom nhóm, điều tra chỉ đọc, hành động qua gateway, xác minh, giao diện API."""
import json

import pytest

import fakes
from conftest import create_connections
from kagent import agent, connections, db, gateway, incidents, llm


def am(labels, fp, state="active", starts="2026-11-12T08:00:00Z", summary=""):
    return {"labels": labels, "status": {"state": state}, "startsAt": starts, "fingerprint": fp, "annotations": {"summary": summary}}


@pytest.fixture()
def flow(users, infra):
    """Tenant có kết nối Alertmanager 'am' và luồng cảnh báo đã bật (chưa điều tra tự động để test chủ động gọi)."""
    admin = users["admin"]
    r = admin.post("/api/connections", json={"template": "alertmanager", "name": "am", "scope": "tenant", "envs": ["dev"], "roles": ["member"],
                                            "params": {"url": "https://am.test", "username": "am"}, "secrets": {"password": "pw"}})
    assert r.status_code == 200, r.text
    cfg = admin.put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "investigate": False}).json()
    assert cfg["enabled"] and cfg["sources"] == ["am"]
    infra.am_alerts[:] = [am({"alertname": "HighCPU", "severity": "warning", "namespace": "payments"}, "f1"),
                          am({"alertname": "HighCPU", "severity": "warning", "namespace": "web"}, "f2"),
                          am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f3")]
    return admin


def tenant(users):
    return users["admin"].tenant


def poll(users):
    with db.session() as conn:
        return incidents.poll(conn, tenant(users))


# ---------- cài đặt và luật gom ----------

@pytest.mark.parametrize("data, message", [
    ({"poll_seconds": 5}, "poll_seconds"), ({"auto_rules": "x"}, "auto_rules"), ({"auto": {"qa": {}}}, "môi trường"),
    ({"sources": ["Bad Name"]}, "sources"), ({"x": 1}, "không hợp lệ"),
])
def test_settings_validation(data, message):
    with pytest.raises(incidents.IncidentError, match=message):
        incidents.check_settings(data)


def test_settings_merge_defaults_per_env():
    cfg = incidents.check_settings({"auto": {"staging": {"enabled": True}}})
    assert cfg["auto"]["staging"] == {"enabled": True} and cfg["auto"]["dev"]["enabled"] is True
    assert incidents.check_settings({"auto": {"dev": {"min_confidence": 0.8}}})["auto"]["dev"] == {"enabled": True}  # cài đặt lưu từ trước auto mode


@pytest.mark.parametrize("a, b, method", [
    ({"namespace": "web", "service": "api"}, {"namespace": "web", "job": "x"}, "rule"),
    ({"namespace": "web", "service": "api"}, {"namespace": "web", "service": "db"}, None),
    ({"namespace": "a", "node": "n1"}, {"namespace": "b", "node": "n1"}, "topology"),
    ({"pod": "checkout-7d9f8c6b5-x2k4q"}, {"deployment": "checkout"}, "topology"),
    ({"pod": "web-0"}, {"pod": "web-1"}, "topology"),
    ({"pod": "web-0", "namespace": "a"}, {"pod": "web-1", "namespace": "b"}, None),
    ({"alertname": "X"}, {"alertname": "Y"}, None),
])
def test_match_rules(a, b, method):
    got = incidents.match(a, b, ["cluster", "namespace", "service", "job"])
    assert (got[0] if got else None) == method


def test_exporter_labels_ignored_and_default_groups_by_namespace():
    ksm = {"job": "kube-state-metrics", "service": "monitor-kube-state-metrics", "pod": "monitor-kube-state-metrics-779f6f8869-qzq9n", "instance": "1.2.3.4:8080"}
    a, b = {**ksm, "namespace": "a", "deployment": "x"}, {**ksm, "namespace": "b", "deployment": "y"}
    assert incidents.match(a, b, ["cluster", "namespace"]) is None  # trước đây "cùng workload monitor-kube-state-metrics"
    assert incidents.target_labels({**ksm, "namespace": "a", "pod": "app-0"})["pod"] == "app-0"  # pod của đối tượng (metric có nhãn pod) được giữ
    keys = incidents.DEFAULTS["group_labels"]
    assert incidents.match({"namespace": "doc", "service": "nginx"}, {"namespace": "doc", "service": "api"}, keys)[0] == "rule"


# ---------- poll và gom ----------

def test_poll_dedups_groups_and_resolves(flow, users, infra):
    first = poll(users)
    assert first["new"] == 3 and first["sources"]["am"]["count"] == 3
    items = flow.get("/api/incidents").json()["items"]
    by_title = {i["title"]: i for i in items}
    assert len(items) == 2 and by_title["HighCPU · web"]["alerts"] == 2 and by_title["HighCPU · payments"]["alerts"] == 1
    detail = flow.get(f"/api/incidents/{by_title['HighCPU · web']['id']}").json()
    joined = next(a for a in detail["alerts"] if a["alertname"] == "Down")
    assert joined["join_method"] == "rule" and "namespace=web" in joined["join_reason"]
    assert poll(users)["new"] == 0  # cùng fingerprint, cùng startsAt: không tạo bản mới

    infra.am_alerts[:] = infra.am_alerts[:1]
    assert poll(users)["resolved"] == 2
    web = flow.get(f"/api/incidents/{by_title['HighCPU · web']['id']}").json()
    assert web["status"] == "resolved" and any(e["kind"] == "resolved" for e in web["timeline"])
    assert {e["text"] for e in web["timeline"] if e["kind"].startswith("alert")} == {"HighCPU bắt đầu", "Down bắt đầu", "HighCPU hết", "Down hết"}
    assert [i["title"] for i in flow.get("/api/incidents").json()["items"]] == ["HighCPU · payments"]

    # bắn lại sau khi hết (startsAt mới) là lần bắn mới, mở sự cố mới vì sự cố cũ đã đóng
    infra.am_alerts.append(am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f3", starts="2026-11-12T09:00:00Z"))
    assert poll(users)["new"] == 1


def test_source_error_is_reported_not_fatal(flow, users, infra):
    with db.session() as conn:
        row = conn.execute("SELECT * FROM connection WHERE name='am'").fetchone()
        connections.update(conn, tenant(users), row["id"], conn.execute("SELECT id FROM app_user WHERE username='admin'").fetchone()["id"],
                           is_admin=True, is_org_admin=True, changes={"secrets": {"password": "sai"}})
    out = poll(users)
    assert out["new"] == 0 and "401" in out["sources"]["am"]["error"]


class Correlating(llm.HeuristicProvider):
    score = 0.9

    def correlate(self, alert, incidents_, feedback):
        self.seen_feedback = feedback
        return {"incident_id": incidents_[0]["id"], "score": self.score, "reason": "payments gọi web"}


def test_ai_grouping_only_above_threshold(flow, users, infra):
    infra.am_alerts[:] = infra.am_alerts[:1]
    poll(users)
    provider = Correlating()
    llm.set_provider(provider)
    infra.am_alerts.append(am({"alertname": "Latency", "namespace": "web"}, "f9"))
    poll(users)
    items = flow.get("/api/incidents").json()["items"]
    assert len(items) == 1 and items[0]["alerts"] == 2
    alert = next(a for a in flow.get(f"/api/incidents/{items[0]['id']}").json()["alerts"] if a["alertname"] == "Latency")
    assert alert["join_method"] == "ai" and alert["join_score"] == 0.9

    provider.score = 0.5
    infra.am_alerts.append(am({"alertname": "DiskFull", "namespace": "db"}, "f10"))
    poll(users)
    assert len(flow.get("/api/incidents").json()["items"]) == 2


def test_merge_split_close_and_feedback(flow, users):
    poll(users)
    items = {i["title"]: i["id"] for i in flow.get("/api/incidents").json()["items"]}
    web, pay = items["HighCPU · web"], items["HighCPU · payments"]
    merged = flow.post(f"/api/incidents/{pay}/merge", json={"into": web}).json()
    assert len(merged["alerts"]) == 3 and not any("gộp" in e["text"] for e in merged["timeline"])  # mốc nội bộ không vào dòng thời gian
    down = next(a for a in merged["alerts"] if a["alertname"] == "Down")
    new_id = flow.post(f"/api/incidents/{web}/split", json={"alert_id": down["id"]}).json()["id"]
    assert flow.get(f"/api/incidents/{new_id}").json()["alerts"][0]["alertname"] == "Down"
    with db.session() as conn:
        feedback = incidents._feedback(conn, tenant(users))
    assert any("tách Down" in f for f in feedback) and any("gộp" in f for f in feedback)  # đưa vào LLM khi gom lần sau
    assert flow.post(f"/api/incidents/{new_id}/close", json={"note": "đã xử lý tay"}).status_code == 200
    assert flow.get(f"/api/incidents/{new_id}").json()["status"] == "closed"
    assert users["carol"].post(f"/api/incidents/{web}/close", json={}).status_code in (200, 403)


def test_settings_need_tenant_admin_and_valid_sources(flow, users):
    assert users["alice"].put("/api/alert-settings", json={"enabled": True}).status_code == 403
    r = flow.put("/api/alert-settings", json={"enabled": True, "sources": ["k8s"]})
    assert r.status_code == 400 and "alertmanager" in r.json()["detail"]
    info = flow.get("/api/alert-settings").json()
    assert [s["name"] for s in info["available_sources"]] == ["am"] and info["can_manage"]


# ---------- điều tra và hành động ----------

class Reporting(llm.HeuristicProvider):
    """verdict: kết quả auto mode cho hành động (None = như chế độ không dùng model)."""

    def __init__(self, confidence, actions, read="get pods -n default", verdict=True):
        self.confidence, self.actions, self.data, self.read, self.verdict, self.judged = confidence, actions, None, read, verdict, []

    def classify_action(self, data):
        self.judged.append(data)
        if self.verdict is None:
            return super().classify_action(data)
        return {"allow": self.verdict, "rule": "" if self.verdict else "Vượt phạm vi", "reason": "" if self.verdict else "không có trong luật tự xử lý"}

    def run_agent(self, task, context, tool_list, call_tool, **kw):
        step = call_tool("k8s.kubectl_read", {"command": self.read})  # bảng pod: kết quả tin cậy
        return {"answer": f"đã xem pod: {step['status']}", "tokens": 0}

    def incident_report(self, data, tools):
        self.data, self.tools = data, tools
        return {"title": "web CrashLoop sau deploy", "root_cause": "image lỗi", "confidence": self.confidence, "evidence": ["restartCount=12"],
                "actions": [a for a in self.actions if a["tool"] in tools]}


RESTART = {"tool": "k8s.kubectl_write", "args": {"command": "rollout restart deployment/web -n default"}, "why": "khởi động lại"}


def _web_incident(users):
    poll(users)
    with db.session() as conn:
        return conn.execute("SELECT id FROM incident WHERE title='HighCPU · web'").fetchone()["id"]


def test_confident_action_runs_then_verified(flow, users, infra, monkeypatch):
    iid = _web_incident(users)
    provider = Reporting(0.9, [RESTART])
    llm.set_provider(provider)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    assert "k8s.kubectl_write" in provider.tools and all(t["tool"] != "k8s.kubectl_read" for t in provider.data["tools"])
    # Auto mode xét theo luật tự xử lý và nhãn cảnh báo, không theo báo cáo viết từ dữ liệu điều tra.
    judged = provider.judged[-1]
    assert "Được khởi động lại" in judged["user_intent"][0] and "HighCPU" in judged["user_intent"][1] and "image lỗi" not in json.dumps(judged)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "verifying", inc["actions"][0]["reasons"]
    assert inc["steps"] == [{"tool": "k8s.kubectl_read", "args": {"command": "get pods -n default"}, "status": "ok", "untrusted": False}]
    assert inc["title"] == "web CrashLoop sau deploy" and inc["confidence"] == 0.9
    assert inc["actions"][0]["status"] == "ok" and inc["actions"][0]["auto"] and "restarted" in inc["actions"][0]["result"]
    assert inc["steps"] and all(s["tool"] != "k8s.kubectl_write" for s in inc["steps"])  # điều tra chỉ đọc
    audit = next(a for a in flow.get("/api/audit").json() if a["tool"] == "k8s.kubectl_write")
    assert audit["user"] == incidents.BOT

    infra.am_alerts[:] = infra.am_alerts[:1]
    with db.session() as conn:
        incidents.poll(conn, tenant(users))
        conn.execute("UPDATE incident SET verify_due=? WHERE id=?", (db.later(seconds=-1), iid))
        incidents.follow_up(conn, tenant(users))
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "handled" and inc["actions"][0]["verified"] is True
    assert flow.get("/api/incidents?status=all").json()["auto_today"] == 1


def test_auto_mode_block_asks_and_approval_is_followed(flow, users, infra, monkeypatch):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    action = inc["actions"][0]
    # Luồng sự cố không có agent để thử cách khác: auto mode chặn thì hỏi người luôn.
    assert inc["stage"] == "awaiting_approval" and action["status"] == "pending"
    assert action["reasons"] == ["auto mode chặn: Vượt phạm vi: không có trong luật tự xử lý"]
    assert flow.post(f"/api/approvals/{action['approval_id']}/decide", json={"action": "approve_once"}).json()["status"] == "approved"
    with db.session() as conn:
        incidents.follow_up(conn, tenant(users))
        conn.execute("UPDATE incident SET verify_due=? WHERE id=?", (db.later(seconds=-1), iid))
        incidents.follow_up(conn, tenant(users))  # cảnh báo vẫn còn
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "needs_human" and inc["actions"][0]["verified"] is False
    # lần sau không tự chạy nữa vì lần trước không làm hết cảnh báo
    llm.set_provider(Reporting(0.99, [RESTART]))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    assert any("lần tự xử lý trước" in r for r in flow.get(f"/api/incidents/{iid}").json()["actions"][-1]["reasons"])


def test_env_disabled_never_auto(flow, users, monkeypatch):
    flow.put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "investigate": False, "auto": {"dev": {"enabled": False}}})
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.99, [RESTART]))
    with db.session() as conn:
        user = incidents.bot_user(conn, tenant(users))
        report = {"title": "", "root_cause": "", "confidence": 0.99, "evidence": [], "actions": [RESTART]}
        incidents.act(conn, tenant(users), iid, report, user, None)
    actions = flow.get(f"/api/incidents/{iid}").json()["actions"]
    assert actions[0]["status"] == "pending" and any("tự xử lý đang tắt ở dev" in r for r in actions[0]["reasons"])


def test_no_safe_action_means_reported(flow, users):
    iid = _web_incident(users)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)  # heuristic: không đề xuất hành động
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "reported" and any(e["kind"] == "no_action" for e in inc["timeline"])


def test_reinvestigate_creates_conversation_before_background_run(flow, users):
    iid = _web_incident(users)
    assert not flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    with db.session() as conn, db.tx(conn):
        incidents.reinvestigate(conn, tenant(users), iid)
    cid = flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    assert cid and flow.get(f"/api/incidents/{iid}").json()["conversation_id"] == cid


def test_queued_investigation_is_reported_until_a_slot_frees(flow, users):
    import time
    iid = _web_incident(users)
    with db.session() as conn, db.tx(conn):
        incidents.reinvestigate(conn, tenant(users), iid)
    cid = flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    for _ in range(2):
        incidents._slots.acquire()
    try:
        assert incidents.start_investigation(tenant(users), iid)
        assert flow.get(f"/api/incidents/{iid}").json()["queued"] == "slot"
        conv = flow.get(f"/api/conversations/{cid}").json()
        assert conv["queued"] == "slot" and not conv["running"]
    finally:
        for _ in range(2):
            incidents._slots.release()
    deadline = time.time() + 10
    while incidents.queued(iid) and time.time() < deadline:
        time.sleep(0.05)
    conv = flow.get(f"/api/conversations/{cid}").json()
    assert conv["queued"] is None and conv["messages"]


def test_resolved_or_closed_incident_no_longer_waits_on_human(flow, users, infra):
    iid = _web_incident(users)
    with db.session() as conn:
        conn.execute("UPDATE incident SET stage='needs_human' WHERE id=?", (iid,))  # vd lệnh khắc phục bị từ chối
    infra.am_alerts[:] = infra.am_alerts[:1]
    poll(users)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["status"] == "resolved" and (inc["stage"], inc["stage_label"]) == ("cleared", "cảnh báo đã hết")
    assert next(i for i in flow.get("/api/incidents?status=all").json()["items"] if i["id"] == iid)["stage"] == "cleared"
    other = next(i["id"] for i in flow.get("/api/incidents").json()["items"])
    flow.post(f"/api/incidents/{other}/close", json={"note": ""})
    assert flow.get(f"/api/incidents/{other}").json()["stage"] == "closed"


def test_read_only_run_blocks_write_tools_even_if_model_calls_them(users):
    class Rogue(llm.HeuristicProvider):
        def run_agent(self, task, context, tool_list, call_tool, **kw):
            self.visible = {t["tool"] for t in tool_list}
            step = call_tool("k8s.kubectl_write", {"command": "scale deployment/web --replicas=0"})
            return {"answer": step["status"], "tokens": 0}

    rogue = Rogue()
    llm.set_provider(rogue)
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ep = agent.run_task(conn, users["alice"].tenant, uid, "điều tra", read_only=True)
    step = next(s for s in ep["trace"] if s["type"] == "tool_call")
    assert step["status"] == "blocked" and "chỉ được dùng công cụ đọc" in step["reasons"][0]
    assert "k8s.kubectl_write" not in rogue.visible and "k8s.kubectl_read" in rogue.visible


def test_tick_polls_and_starts_due_investigations(flow, users, monkeypatch):
    started = []
    monkeypatch.setattr(incidents, "start_investigation", lambda t, i: started.append(i))
    flow.put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "debounce_seconds": 0})
    incidents._state.clear()
    with db.session() as conn:
        incidents.tick(conn)
        incidents.tick(conn)  # lần 2 chưa tới hạn poll, không lấy lại
    assert len(started) >= 2 and flow.get("/api/incidents").json()["last_poll"]


def test_bot_cannot_log_in(flow, users, client):
    assert client.post("/api/auth/login", json={"username": incidents.BOT, "password": "00$disabled"}).status_code == 401


def test_without_model_write_actions_ask_even_if_report_is_confident(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.99, [RESTART], verdict=None))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    action = flow.get(f"/api/incidents/{iid}").json()["actions"][0]
    assert action["status"] == "pending" and "không dùng model" in action["reasons"][0]


def test_reading_logs_does_not_block_auto_and_logs_never_reach_classifier(flow, users, monkeypatch):
    iid = _web_incident(users)
    provider = Reporting(0.99, [RESTART], read="logs web-0 -n default")
    llm.set_provider(provider)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["steps"][0]["untrusted"] and inc["actions"][0]["status"] == "ok"
    seen = json.dumps(provider.judged, ensure_ascii=False)
    assert "IGNORE PREVIOUS" not in seen and '"logs web-0 -n default"' in seen  # thấy lệnh đã gọi, không thấy nội dung log



def test_dry_run_flag_removed_from_proposed_command(flow, users):
    iid = _web_incident(users)
    cmd = {**RESTART, "args": {"command": "rollout undo deployment/web -n default --dry-run=server", "timeout_seconds": 60}}
    llm.set_provider(Reporting(0.6, [cmd]))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    action = flow.get(f"/api/incidents/{iid}").json()["actions"][0]
    assert action["args"] == {"command": "rollout undo deployment/web -n default", "timeout_seconds": 60}
    assert incidents.strip_dry_run({"command": "get pods"}) == {"command": "get pods"}


def test_approval_updates_incident_at_once_and_timeline_shows_fix(flow, users, infra):
    flow.put("/api/alert-settings", json={"enabled": False, "sources": ["am"]})  # luồng tắt vẫn phải theo dõi lệnh đã xin duyệt
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    action = flow.get(f"/api/incidents/{iid}").json()["actions"][0]
    flow.post(f"/api/approvals/{action['approval_id']}/decide", json={"action": "approve_once"})
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "verifying" and inc["actions"][0]["status"] == "ok" and "restarted" in inc["actions"][0]["result"]
    texts = [e["text"] for e in inc["timeline"]]
    assert "chạy kubectl_write rollout restart deployment/web -n default" in texts
    assert any(t.startswith("nhận định: web CrashLoop") for t in texts) and not any("duyệt" in t or "bắt đầu điều tra" in t for t in texts)
    infra.am_alerts[:] = infra.am_alerts[:1]
    with db.session() as conn:
        conn.execute("UPDATE incident SET verify_due=? WHERE id=?", (db.later(seconds=-1), iid))
        incidents.tick(conn)  # luồng tắt: tick vẫn xác minh, tự poll để có số liệu mới
    assert flow.get(f"/api/incidents/{iid}").json()["stage"] == "handled"


def test_investigation_lives_in_shared_conversation(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        incidents.investigate(conn, tenant(users), iid)  # điều tra lại: lượt mới trong cùng hội thoại
    inc = flow.get(f"/api/incidents/{iid}").json()
    alice = users["alice"]
    conv = alice.get(f"/api/conversations/{inc['conversation_id']}").json()
    assert conv["incident_id"] == iid and len(conv["messages"]) == 2 and conv["title"].startswith("Sự cố: ")
    assert conv["messages"][0]["approvals"][0]["tool"] == "k8s.kubectl_write"  # duyệt ngay trong chat
    assert alice.patch(f"/api/conversations/{inc['conversation_id']}", json={"title": "x"}).status_code == 404  # chỉ chủ hội thoại đổi tên
    assert all(c["id"] != inc["conversation_id"] for c in alice.get("/api/conversations").json())  # chưa nhắn thì không chen vào danh sách


def test_member_can_continue_in_incident_conversation(flow, users):
    from test_chat import chat
    iid = _web_incident(users)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    cid = flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    llm.set_provider(llm.HeuristicProvider())
    done = next(e for e in chat(users["alice"], "xem pod trong namespace default", cid) if e["type"] == "done")
    assert done["conversation_id"] == cid and done["message"]["steps"][0]["tool"] == "k8s.kubectl_read"
    assert any(c["id"] == cid for c in users["alice"].get("/api/conversations").json())
    assert len(users["bob"].get(f"/api/conversations/{cid}").json()["messages"]) == 2


def test_conversation_shows_incident_actions_on_investigation_turn(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    msg = flow.get(f"/api/conversations/{inc['conversation_id']}").json()["messages"][0]
    fix = msg["incident_actions"][0]
    assert msg["id"] == inc["episode_id"] and fix["status"] == "pending" and fix["approval_id"] == inc["actions"][0]["approval_id"]
    assert fix["approval_id"] in [a["id"] for a in msg["approvals"]] and fix["action_class"] == "write"

    llm.set_provider(Reporting(0.99, [RESTART]))
    with db.session() as conn:
        conn.execute("UPDATE incident SET actions='[]' WHERE id=?", (iid,))
        incidents.investigate(conn, tenant(users), iid)
    msgs = flow.get(f"/api/conversations/{inc['conversation_id']}").json()["messages"]
    ran = msgs[-1]["incident_actions"][0]
    assert ran["status"] == "ok" and ran["auto"] and "restarted" in json.dumps(ran["result"]) and msgs[0]["incident_actions"] == []


def test_investigation_lost_on_restart_is_released(flow, users):
    iid = _web_incident(users)
    with db.session() as conn:
        conn.execute("UPDATE incident SET stage='investigating' WHERE id=?", (iid,))  # luồng điều tra đã mất cùng tiến trình cũ
        incidents._running.add("other")  # điều tra còn sống của sự cố khác không bị đụng tới
        try:
            incidents.tick(conn)
        finally:
            incidents._running.discard("other")
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "error" and not inc["investigating"]
    with db.session() as conn:
        incidents.reinvestigate(conn, tenant(users), iid)  # trước đây bị từ chối vì stage vẫn là investigating


def test_running_investigation_is_not_released(flow, users):
    iid = _web_incident(users)
    with db.session() as conn:
        conn.execute("UPDATE incident SET stage='investigating' WHERE id=?", (iid,))
        incidents._running.add(iid)
        try:
            incidents.tick(conn)
        finally:
            incidents._running.discard(iid)
        assert conn.execute("SELECT stage FROM incident WHERE id=?", (iid,)).fetchone()["stage"] == "investigating"
