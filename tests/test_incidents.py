"""Luồng tự xử lý cảnh báo: poll, gom nhóm, điều tra chỉ đọc, hành động qua gateway, xác minh, giao diện API."""
import json
from datetime import timedelta

import pytest

import fakes
from conftest import create_connections
from aiops import agent, connections, db, gateway, incidents, live, llm


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

    # bắn lại sau khi hết (startsAt mới) là lần bắn mới; trong reopen_hours thì mở lại sự cố cũ thay vì mở sự cố mới
    infra.am_alerts.append(am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f3", starts="2026-11-12T09:00:00Z"))
    assert poll(users)["new"] == 1
    web = flow.get(f"/api/incidents/{by_title['HighCPU · web']['id']}").json()
    assert web["status"] == "open" and len(web["alerts"]) == 3 and any(e["kind"] == "reopened" and "Down bắn lại" in e["text"] for e in web["timeline"])
    assert web["repeats"] == 1
    assert sorted(i["title"] for i in flow.get("/api/incidents").json()["items"]) == ["HighCPU · payments", "HighCPU · web"]


def test_long_running_incident_still_takes_refired_alerts(flow, users, infra, clock):
    poll(users)
    clock(60)  # quá window_minutes kể từ cảnh báo cuối nhập vào; sự cố vẫn còn cảnh báo đang bắn
    infra.am_alerts.append(am({"alertname": "CrashLoop", "severity": "warning", "namespace": "web"}, "f4", starts="2026-11-12T09:30:00Z"))
    poll(users)
    titles = [i["title"] for i in flow.get("/api/incidents").json()["items"]]
    assert sorted(titles) == ["HighCPU · payments", "HighCPU · web"]
    with db.session() as conn:
        assert conn.execute("SELECT i.title FROM alert_event a JOIN incident i ON i.id=a.incident_id WHERE a.alertname='CrashLoop'").fetchone()["title"] == "HighCPU · web"


def test_refire_after_reopen_window_or_human_close_opens_new_incident(flow, users, infra):
    poll(users)
    web = next(i["id"] for i in flow.get("/api/incidents").json()["items"] if i["title"] == "HighCPU · web")
    infra.am_alerts[:] = infra.am_alerts[:1]
    poll(users)
    with db.session() as conn:
        conn.execute("UPDATE incident SET resolved_at=? WHERE id=?", (db.iso(db.now() - timedelta(hours=25)), web))
    infra.am_alerts.append(am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f3", starts="2026-11-13T09:00:00Z"))
    poll(users)
    assert flow.get(f"/api/incidents/{web}").json()["status"] == "resolved"
    new = next(i for i in flow.get("/api/incidents").json()["items"] if i["title"] == "Down · web")
    flow.post(f"/api/incidents/{new['id']}/close", json={"note": ""})
    infra.am_alerts[:] = infra.am_alerts[:1]
    poll(users)
    infra.am_alerts.append(am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f3", starts="2026-11-13T10:00:00Z"))
    poll(users)
    assert flow.get(f"/api/incidents/{new['id']}").json()["status"] == "closed"  # người đã đóng: không mở lại
    assert len([i for i in flow.get("/api/incidents").json()["items"] if i["title"] == "Down · web"]) == 1


def _separate_incident(users, labels: dict, name="Crash") -> str:
    """Mở sự cố riêng cho một cảnh báo, bỏ qua bước gom (như sự cố mở trước khi có bản sửa gom)."""
    t = tenant(users)
    with db.session() as conn:
        aid, now = db.new_id(), db.iso()
        conn.execute("INSERT INTO alert_event(id,tenant_id,source,fingerprint,starts_at,alertname,severity,labels,summary,env,first_seen,last_seen) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (aid, t, "am", aid, now, name, "warning", json.dumps(labels), "", "dev", now, now))
        a = conn.execute("SELECT * FROM alert_event WHERE id=?", (aid,)).fetchone()
        with db.tx(conn):
            return incidents._attach(conn, t, a, labels, None, incidents.settings(conn, t))


def test_auto_merge_rule_matches_into_oldest_but_respects_manual_split(flow, users):
    poll(users)
    items = {i["title"]: i["id"] for i in flow.get("/api/incidents").json()["items"]}
    web = items["HighCPU · web"]
    dup = _separate_incident(users, {"alertname": "Crash", "namespace": "web"})
    other = _separate_incident(users, {"alertname": "Crash", "namespace": "billing"}, "Crash2")
    with db.session() as conn:
        assert incidents.auto_merge(conn, tenant(users)) == 1
        feedback = incidents._feedback(conn, tenant(users))
    assert not any("tự gộp" in f for f in feedback)  # không coi là phản hồi của người cho AI gom
    dst = flow.get(f"/api/incidents/{web}").json()
    assert len(dst["alerts"]) == 3 and dst["merged_from"][0]["id"] == dup and dst["merged_from"][0]["auto"]
    assert flow.get(f"/api/incidents/{dup}").json()["status"] == "closed"
    assert flow.get(f"/api/incidents/{other}").json()["status"] == "open"

    down = next(a for a in dst["alerts"] if a["alertname"] == "Down")
    split = flow.post(f"/api/incidents/{web}/split", json={"alert_id": down["id"]}).json()["id"]
    with db.session() as conn:
        assert incidents.auto_merge(conn, tenant(users)) == 0  # người đã tách: không gộp lại
    assert flow.get(f"/api/incidents/{split}").json()["status"] == "open"


def test_merge_carries_pending_approval_and_skips_busy_source(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    pending = flow.get(f"/api/incidents/{iid}").json()
    assert pending["stage"] == "awaiting_approval"
    dup = _separate_incident(users, {"alertname": "Crash", "namespace": "web"})
    with db.session() as conn:
        conn.execute("UPDATE incident SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (dup,))  # dup cũ hơn: web là nguồn
        assert incidents.auto_merge(conn, tenant(users)) == 0  # nguồn đang chờ duyệt: không tự gộp
    approval = pending["actions"][0]["approval_id"]
    assert flow.post(f"/api/incidents/{iid}/merge", json={"into": dup}).status_code == 200
    dst = flow.get(f"/api/incidents/{dup}").json()
    assert dst["stage"] == "awaiting_approval" and [a["approval_id"] for a in dst["actions"]] == [approval]
    assert flow.get(f"/api/incidents/{iid}").json()["actions"] == []


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
        return {"symptom": "web CrashLoop sau deploy", "root_cause": "image lỗi", "confidence": self.confidence, "evidence": ["restartCount=12"],
                "actions": [a for a in self.actions if a["tool"] in tools]}


RESTART = {"tool": "k8s.kubectl_write", "args": {"command": "rollout restart deployment/web -n default"}, "why": "khởi động lại"}


def _recorded(iid, kind):
    """Sự kiện lệnh chưa chạy (đề xuất, từ chối, hết hạn, bỏ qua) vẫn được ghi nhưng không hiện trên dòng sự kiện."""
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM incident_event WHERE incident_id=? AND kind=? ORDER BY id DESC", (iid, kind)).fetchone())


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
    assert inc["steps"] == [{"tool": "k8s.kubectl_read", "args": {"command": "get pods -n default"}, "status": "ok", "untrusted": False, "link": None}]
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


def test_investigation_checks_changes_before_the_first_alert(flow, users):
    iid = _web_incident(users)
    provider = Reporting(0.5, [])
    tasks = []
    provider.run_agent = lambda task, *a, **kw: tasks.append(task) or {"answer": "xong", "tokens": 0}
    llm.set_provider(provider)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    assert "data.recent_changes với around=2026-11-12T08:00:00Z, env=dev" in tasks[0] and "rollout history" in tasks[0]


def test_no_safe_action_means_reported(flow, users):
    iid = _web_incident(users)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)  # heuristic: không đề xuất hành động
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "reported" and _recorded(iid, "no_action") and not any(e["kind"] == "no_action" for e in inc["timeline"])


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
    ran = next(e for e in inc["timeline"] if e["kind"] == "ran")
    assert (ran["who"], ran["text"]) == ("admin duyệt", "kubectl_write rollout restart deployment/web -n default → deployment.apps/web restarted")
    assert ran["action_id"] == inc["actions"][0]["id"] and ran["actor"] == inc["actions"][0]["decided_by"]
    assert (ran["title"], ran["cmd"], ran["result"]) == ("Khởi động lại", "kubectl_write rollout restart deployment/web -n default", "deployment.apps/web restarted")
    assert not any(e["kind"] == "proposed" for e in inc["timeline"])  # chỉ hiện lệnh đã thực sự chạy
    proposed = _recorded(iid, "proposed")
    assert proposed["actor"] == "aiops" and proposed["text"].startswith("kubectl_write rollout restart") and "→ cần duyệt" in proposed["text"]
    investigated = next(e for e in inc["timeline"] if e["kind"] == "investigated")
    assert investigated["who"] == "aiops · điều tra lần 1" and investigated["text"] == "1 lệnh đọc → Image lỗi (độ tin 0.60)"
    assert (investigated["title"], investigated["result"]) == ("Image lỗi", "1 lệnh đọc · độ tin 0.60")
    assert not any("bắt đầu điều tra" in t for t in texts) and inc["cause"]["round"] == 1 and inc["cause"]["text"] == "image lỗi"
    infra.am_alerts[:] = infra.am_alerts[:1]
    with db.session() as conn:
        conn.execute("UPDATE incident SET verify_due=? WHERE id=?", (db.later(seconds=-1), iid))
        incidents.tick(conn)  # luồng tắt: tick vẫn xác minh, tự poll để có số liệu mới
    assert flow.get(f"/api/incidents/{iid}").json()["stage"] == "handled"


def test_write_in_incident_chat_is_logged_and_denial_recorded(flow, users):
    from test_approval_flow import Scripted
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    cid = flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    llm.set_provider(Scripted(lambda call_tool: [call_tool("k8s.kubectl_write", {"command": "rollout undo deployment/web -n default --dry-run=server"}),
                                                 call_tool("k8s.kubectl_write", {"command": "rollout undo deployment/web -n default"})][-1]["status"]))
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        agent.run_task(conn, users["alice"].tenant, uid, "rollback web", conversation_id=cid)
        rows = conn.execute("SELECT * FROM incident_action WHERE incident_id=?", (iid,)).fetchall()
        assert len(rows) == 1  # lệnh chạy thử không thành hành động khắc phục
        row = dict(rows[0])
    assert row["origin"] == "chat" and row["requested_by"] == uid and row["status"] == "pending" and row["approval_id"]
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["actions"] == []  # thẻ Hành động cũ chỉ hiện lệnh do điều tra đề xuất
    proposed = _recorded(iid, "proposed")
    assert proposed["actor"] == uid and proposed["text"].startswith("kubectl_write rollout undo deployment/web -n default → cần duyệt")
    item = flow.get(f"/api/incidents/{iid}").json()["waiting"][0]
    assert item["kind"] == "approval" and item["approval_id"] == row["approval_id"] and item["requested_by"] == "alice"
    flow.post(f"/api/approvals/{row['approval_id']}/decide", json={"action": "deny", "reason": "chưa rollback, xem log trước"})
    assert not any(e.get("action_id") for e in flow.get(f"/api/incidents/{iid}").json()["timeline"])  # lệnh bị từ chối không hiện
    denied = _recorded(iid, "denied")
    assert denied["text"] == "kubectl_write rollout undo deployment/web -n default → từ chối: chưa rollback, xem log trước"
    assert denied["action_id"] == row["id"] and denied["actor"] not in (None, uid)


def test_expired_approval_is_logged_and_hands_to_human(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        conn.execute("UPDATE approval_request SET expires_at=?", (db.later(seconds=-1),))
        gateway.expire_approvals(conn, tenant(users))
        incidents.follow_up(conn, tenant(users))
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "needs_human" and inc["actions"][0]["status"] == "expired"
    assert not any(e.get("action_id") for e in inc["timeline"])
    assert _recorded(iid, "expired")["text"] == "kubectl_write rollout restart deployment/web -n default → hết hạn duyệt, chưa chạy"


def test_waiting_shows_expired_once_then_retry_and_dismiss(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))

    def expire():
        with db.session() as conn:
            conn.execute("UPDATE approval_request SET expires_at=? WHERE status='pending'", (db.later(seconds=-1),))
            gateway.expire_approvals(conn, tenant(users))
            incidents.follow_up(conn, tenant(users))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["state"]["label"] == "Chờ duyệt" and [w["kind"] for w in inc["waiting"]] == ["approval"]
    assert inc["grouped_by"] == ["namespace=web"] and {g["alertname"]: g["firing"] for g in inc["alert_groups"]} == {"HighCPU": 1, "Down": 1}
    assert inc["title"] == "web CrashLoop sau deploy" and inc["milestones"]["first_alert"] == "2026-11-12T08:00:00Z"
    expire()
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)  # lượt hai đề xuất lại đúng lệnh đó, rồi cũng hết hạn
    expire()
    inc = flow.get(f"/api/incidents/{iid}").json()
    retry = [w for w in inc["waiting"] if w["kind"] == "retry"]
    assert inc["state"]["label"] == "Cần người xử lý" and len(retry) == 1 and retry[0]["status"] == "expired"

    out = flow.post(f"/api/incidents/{iid}/actions/{retry[0]['action_id']}/retry").json()
    assert out["status"] == "pending" and "người dùng nhờ đề xuất lại lệnh đã hết hạn duyệt" in out["reasons"]
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "awaiting_approval" and [w["kind"] for w in inc["waiting"]] == ["approval"] and inc["waiting"][0]["requested_by"] == "admin"
    assert flow.post(f"/api/incidents/{iid}/actions/{retry[0]['action_id']}/retry").status_code == 409

    expire()
    item = next(w for w in flow.get(f"/api/incidents/{iid}").json()["waiting"] if w["kind"] == "retry")
    assert flow.post(f"/api/incidents/{iid}/actions/{item['action_id']}/dismiss").status_code == 200
    assert not any(w["kind"] == "retry" for w in flow.get(f"/api/incidents/{iid}").json()["waiting"])


def test_migration_copies_json_actions_with_original_times(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        approval = conn.execute("SELECT approval_id FROM incident_action WHERE incident_id=?", (iid,)).fetchone()["approval_id"]
        conn.execute("UPDATE approval_request SET status='expired', decided_at='2026-11-13T10:18:07+00:00' WHERE id=?", (approval,))
        old = [{"tool": "k8s.kubectl_write", "args": {"command": "rollout restart deployment/web -n default"}, "why": "khởi động lại",
                "at": "2026-11-12T17:00:00+00:00", "status": "ok", "auto": True, "verified": False, "approval_id": None,
                "result": json.dumps({"stdout": "deployment.apps/web restarted\n"})},
               {"tool": "k8s.kubectl_write", "args": {"command": "rollout undo deployment/web -n default"}, "why": "rollback",
                "at": "2026-11-13T09:48:07+00:00", "status": "pending", "auto": False, "approval_id": approval, "reasons": ["tự xử lý đang tắt ở dev"]}]
        conn.execute("UPDATE incident SET actions=? WHERE id=?", (json.dumps(old), iid))
        conn.execute("DELETE FROM incident_event WHERE kind IN ('proposed','ran','failed','denied','expired')")
        conn.execute("DROP TABLE incident_action")
        db.init_schema(conn)
        rows = [dict(r) for r in conn.execute("SELECT * FROM incident_action WHERE incident_id=? ORDER BY proposed_at", (iid,))]
    assert [(r["status"], r["auto"], r["verified"], r["proposed_at"]) for r in rows] == [
        ("ok", 1, 0, "2026-11-12T17:00:00+00:00"), ("expired", 0, None, "2026-11-13T09:48:07+00:00")]
    assert rows[0]["result_line"] == "deployment.apps/web restarted"
    events = [(e["at"], e["who"], e["text"]) for e in flow.get(f"/api/incidents/{iid}").json()["timeline"] if e.get("action_id")]
    assert events == [("2026-11-12T17:00:00+00:00", "tự động", "kubectl_write rollout restart deployment/web -n default → deployment.apps/web restarted")]
    assert _recorded(iid, "proposed")["at"] == "2026-11-13T09:48:07+00:00" and _recorded(iid, "expired")["at"] == "2026-11-13T10:18:07+00:00"


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
        conn.execute("DELETE FROM incident_action WHERE incident_id=?", (iid,))
        incidents.investigate(conn, tenant(users), iid)
    msgs = flow.get(f"/api/conversations/{inc['conversation_id']}").json()["messages"]
    ran = msgs[-1]["incident_actions"][0]
    assert ran["status"] == "ok" and ran["auto"] and "restarted" in json.dumps(ran["result"]) and msgs[0]["incident_actions"] == []


def test_describe_names_manifest_objects():
    manifest = "apiVersion: v1\nkind: Service\nmetadata:\n  name: svc-document-wikijs\n  namespace: svc-document\nspec: {}\n"
    assert incidents.describe({"command": "apply -f -", "manifest": manifest}) == "tạo hoặc cập nhật Service svc-document-wikijs (namespace svc-document)"
    two = "kind: ConfigMap\nmetadata: {name: a}\n---\nkind: Deployment\nmetadata: {name: b}\n"
    assert incidents.describe({"command": "delete -n web -f -", "manifest": two}) == "xóa ConfigMap a (namespace web), Deployment b (namespace web)"
    assert incidents.describe({"command": "rollout restart deployment/x"}) is None
    assert incidents.describe({"command": "apply -f -", "manifest": "a: [b"}) is None


class Summarizing(Reporting):
    def __init__(self, tasks=()):
        super().__init__(0.6, [], verdict=False)
        self.inputs, self.tasks = [], list(tasks)

    def incident_summary(self, data):
        self.inputs.append(data)
        return {"summary": f"tóm tắt lần {len(self.inputs)}", "human_tasks": self.tasks}


@pytest.fixture()
def clock(monkeypatch):
    """Đẩy đồng hồ của aiops về sau thay cho việc chờ thật (episode chỉ cho ghi thêm nên không lùi mốc cũ được)."""
    real, offset = db.now, [0]
    monkeypatch.setattr(db, "now", lambda: real() + timedelta(minutes=offset[0]))

    def advance(minutes):
        offset[0] += minutes
    return advance


def test_summary_written_after_quiet_and_again_only_after_new_activity(flow, users, clock):
    iid = _web_incident(users)
    provider = Summarizing()
    llm.set_provider(provider)
    t = tenant(users)
    with db.session() as conn:
        incidents.investigate(conn, t, iid)
        assert incidents.due_summaries(conn, t) == []  # vừa điều tra xong: chưa yên đủ 5 phút
        clock(6)
        due = incidents.due_summaries(conn, t)
        assert [d[0] for d in due] == [iid]
        view = incidents.summarize(conn, t, iid, due[0][1])
        assert view["text"] == "tóm tắt lần 1" and not view["stale"]
        data = provider.inputs[0]
        assert data["turns"][0]["by"] == "aiops điều tra tự động" and data["incident"]["env"] == "dev" and data["previous_summary"] is None
        assert incidents.due_summaries(conn, t) == []  # không có gì mới: không viết lại

        incidents._event(conn, t, iid, "verified", "đã xác minh")
        assert incidents.summary_view(conn, conn.execute("SELECT * FROM incident WHERE id=?", (iid,)).fetchone())["stale"]
        assert incidents.due_summaries(conn, t) == []
        clock(6)
        due = incidents.due_summaries(conn, t)
        assert [d[0] for d in due] == [iid]
        incidents.summarize(conn, t, iid, due[0][1])
        assert provider.inputs[1]["previous_summary"] == "tóm tắt lần 1"
        # bản tóm tắt không bị coi là hoạt động, và không hiện trên dòng thời gian
        assert incidents.due_summaries(conn, t) == []
        assert all(e["kind"] != "summarized" for e in incidents.get(conn, t, iid)["timeline"])

    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["summary"]["text"] == "tóm tắt lần 2"
    conv = flow.get(f"/api/conversations/{inc['conversation_id']}").json()
    assert conv["incident_summary"]["text"] == "tóm tắt lần 2"


def test_summary_waits_for_running_turn_and_respects_settings(flow, users, clock):
    iid = _web_incident(users)
    llm.set_provider(Summarizing())
    t = tenant(users)
    with db.session() as conn:
        incidents.investigate(conn, t, iid)
        clock(6)
        conv = conn.execute("SELECT conversation_id FROM incident WHERE id=?", (iid,)).fetchone()["conversation_id"]
        run = live.start(conv, "r1", "hỏi thêm")
        assert incidents.due_summaries(conn, t) == []  # đang có lượt chạy
        live.finish(run)
        assert incidents.due_summaries(conn, t) == []  # lượt vừa xong là hoạt động mới
        clock(6)
        assert [d[0] for d in incidents.due_summaries(conn, t)] == [iid]
    r = users["admin"].put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "investigate": False, "summary": {"enabled": False}})
    assert r.status_code == 200 and r.json()["summary"] == {"enabled": False, "idle_minutes": 5}
    with db.session() as conn:
        assert incidents.due_summaries(conn, t) == []
    assert users["admin"].put("/api/alert-settings", json={"summary": {"idle_minutes": 0}}).status_code == 400


def test_summary_cut_at_sentence_end():
    long = "Câu một nói chuyện gì xảy ra. " * 30
    out = llm.check_summary({"summary": long})
    assert len(out) <= llm.SUMMARY_MAX and out.endswith("xảy ra.")
    assert llm.check_summary({"summary": "a; " * 400}).endswith("a…")
    assert llm.check_summary({"summary": "  ngắn\n gọn  "}) == "ngắn gọn"
    with pytest.raises(llm.LLMError):
        llm.check_summary({"summary": " "})


def test_summary_now_endpoint(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Summarizing())
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    r = flow.post(f"/api/incidents/{iid}/summary")
    assert r.status_code == 200 and r.json()["text"] == "tóm tắt lần 1"
    assert flow.get(f"/api/incidents/{iid}/summary").json()["text"] == "tóm tắt lần 1"


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


def test_model_error_during_investigation_marks_error_without_report(flow, users):
    class Dropped(Reporting):
        def run_agent(self, *a, **kw):
            raise llm.LLMError("mất kết nối tới API giữa chừng, thử lại sau")

    iid = _web_incident(users)
    provider = Dropped(0.99, [RESTART])
    llm.set_provider(provider)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "error" and inc["actions"] == [] and provider.data is None  # không viết báo cáo từ câu "Lỗi khi gọi model"
    assert "mất kết nối" in inc["waiting"][0]["text"] and not any(e["kind"] == "error" for e in inc["timeline"])
    msg = flow.get(f"/api/conversations/{inc['conversation_id']}").json()["messages"][0]
    assert "mất kết nối" in msg["error"]


def test_quota_exhausted_stops_until_user_continues(flow, users, clock, monkeypatch):
    iid = _web_incident(users)
    with db.session() as conn:
        conn.execute("UPDATE tenant SET token_quota_daily=0 WHERE id=?", (tenant(users),))
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "waiting_quota" and inc["state"]["label"] == "Chờ quota" and inc["investigate_after"] is None
    assert inc["waiting"][0]["kind"] == "quota" and inc["waiting"][0]["source"] == "tenant"
    assert not any(e["kind"] == "quota" for e in inc["timeline"])
    item = next(i for i in flow.get("/api/incidents?status=attention").json()["items"] if i["id"] == iid)
    assert item["state"]["key"] == "waiting_quota" and "Tiếp tục" in item["line"]

    started = []
    monkeypatch.setattr(incidents, "start_investigation", lambda t, i: started.append(i) or True)
    flow.put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "investigate": True})
    with db.session() as conn:
        clock(24 * 60)
        incidents.tick(conn)
        clock(-24 * 60)  # phiên đăng nhập của test hết hạn nếu để đồng hồ ở ngày sau
    assert iid not in started  # không tự chạy lại khi sang ngày
    assert flow.post(f"/api/incidents/{iid}/investigate").status_code == 200 and iid in started


def test_provider_rate_limit_waits_for_quota_not_error(flow, users):
    iid = _web_incident(users)

    class Limited(Reporting):
        def run_agent(self, task, context, tool_list, call_tool, **kw):
            raise llm.LLMError("endpoint đang giới hạn tốc độ, thử lại sau")
    llm.set_provider(Limited(0.6, []))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "waiting_quota" and inc["waiting"][0]["source"] == "provider" and "giới hạn tốc độ" in inc["waiting"][0]["text"]


def test_related_and_merge_suggestion_then_repeats_after_merge(flow, users):
    iid = _web_incident(users)
    dup = _separate_incident(users, {"alertname": "Crash", "namespace": "web"})
    other = _separate_incident(users, {"alertname": "Crash", "namespace": "billing"})
    old = _separate_incident(users, {"alertname": "Down", "namespace": "web"})
    with db.session() as conn:
        conn.execute("UPDATE incident SET status='resolved', resolved_at=? WHERE id=?", (db.iso(), old))
    rel = flow.get(f"/api/incidents/{iid}").json()["related"]
    assert [r["id"] for r in rel["open"]] == [dup] and "namespace=web" in rel["open"][0]["reason"]
    assert [(r["id"], r["count"]) for r in rel["history"]] == [(old, 1)] and rel["history_count"] == 1 and rel["days"] == 30
    assert all(r["id"] != other for r in rel["open"] + rel["history"])

    listed = flow.get("/api/incidents").json()
    assert [(g["from"]["id"], g["into"]["id"]) for g in listed["suggestions"]] == [(dup, iid)]
    assert flow.post(f"/api/incidents/{dup}/merge", json={"into": iid}).status_code == 200
    assert flow.get("/api/incidents").json()["suggestions"] == []
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["repeats"] == 1 and inc["related"]["open"] == []  # sự cố đã gộp vào đây không còn là sự cố liên quan

    with db.session() as conn:
        incidents.split(conn, tenant(users), iid, next(a["id"] for a in inc["alerts"] if a["alertname"] == "Crash"), "x")
    assert flow.get("/api/incidents").json()["suggestions"] == []  # người đã tách: không gợi ý gộp lại


def test_summary_tasks_marked_done_and_incident_state_reaches_chat_context(flow, users):
    from test_approval_flow import Scripted
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    provider = Summarizing(["Đẩy lại image web lên registry"])
    llm.set_provider(provider)
    flow.post(f"/api/incidents/{iid}/summary")
    task = next(w for w in flow.get(f"/api/incidents/{iid}").json()["waiting"] if w["kind"] == "task")
    assert task["text"] == "Đẩy lại image web lên registry" and task["source"] == "summary"
    added = flow.post(f"/api/incidents/{iid}/tasks", json={"text": "Báo đội hạ tầng"}).json()
    assert flow.post(f"/api/incidents/{iid}/tasks/{task['task_id']}/done", json={}).status_code == 200
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert [w["text"] for w in inc["waiting"] if w["kind"] == "task"] == ["Báo đội hạ tầng"]
    assert [t["text"] for t in inc["tasks_done"]] == ["Đẩy lại image web lên registry"] and inc["tasks_done"][0]["done_by"] == "admin"
    assert any(e["kind"] == "task_done" and e["who"] == "admin đánh dấu đã xong" for e in inc["timeline"])

    provider.tasks = ["đẩy lại image web lên  registry", "Kiểm tra DNS nội bộ"]  # việc đã xong không thêm lại
    flow.post(f"/api/incidents/{iid}/summary")
    assert provider.inputs[-1]["human_tasks"] == [{"text": "Đẩy lại image web lên registry", "done": True}, {"text": "Báo đội hạ tầng", "done": False}]
    assert [w["text"] for w in flow.get(f"/api/incidents/{iid}").json()["waiting"] if w["kind"] == "task"] == ["Báo đội hạ tầng", "Kiểm tra DNS nội bộ"]
    provider.tasks = []
    flow.post(f"/api/incidents/{iid}/summary")  # tóm tắt không còn nêu: bỏ việc nó nêu, giữ việc người thêm
    assert [w["task_id"] for w in flow.get(f"/api/incidents/{iid}").json()["waiting"] if w["kind"] == "task"] == [added["id"]]

    seen = {}

    def fn(call_tool):
        return "ok"

    class Capture(Scripted):
        def run_agent(self, task, context, tools, call_tool, **kw):
            seen["system"], seen["turn"] = context, kw.get("turn_context", "")
            return super().run_agent(task, context, tools, call_tool, **kw)
    llm.set_provider(Capture(fn))
    cid = flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        agent.run_task(conn, users["alice"].tenant, uid, "giờ làm gì tiếp", conversation_id=cid)
    assert "<incident" in seen["turn"] and "việc cần người: Báo đội hạ tầng" in seen["turn"] and "đã xong (người đánh dấu): Đẩy lại image" in seen["turn"]
    assert "Tóm tắt: tóm tắt lần 3" in seen["turn"] and "<incident" not in seen["system"]  # system prompt không đổi theo sự cố


def test_chat_fix_in_incident_conversation_triggers_verify(flow, users):
    from test_approval_flow import Scripted
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        conn.execute("UPDATE approval_request SET expires_at=?", (db.later(seconds=-1),))
        gateway.expire_approvals(conn, tenant(users))
        incidents.follow_up(conn, tenant(users))
        conn.execute("UPDATE incident SET stage='needs_human', verify_due=NULL WHERE id=?", (iid,))
    cid = flow.get(f"/api/incidents/{iid}").json()["conversation_id"]
    llm.set_provider(Scripted(lambda call_tool: call_tool("k8s.kubectl_write", {"command": "rollout restart deployment/web -n default"})["status"]))
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        agent.run_task(conn, users["alice"].tenant, uid, "khởi động lại web", conversation_id=cid)
        approval = conn.execute("SELECT approval_id FROM incident_action WHERE incident_id=? AND origin='chat'", (iid,)).fetchone()["approval_id"]
    flow.post(f"/api/approvals/{approval}/decide", json={"action": "approve_once"})
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "verifying" and inc["verify_due"]  # sửa trong chat xong thì aiops xác minh cảnh báo đã hết chưa


def test_new_message_in_incident_conversation_skips_proposed_fix(flow, users):
    from test_chat import chat, done
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "awaiting_approval" and [w["kind"] for w in inc["waiting"]] == ["approval"]
    llm.set_provider(None)
    done(chat(flow, "khoan, xem log trước đã", inc["conversation_id"]))
    inc = flow.get(f"/api/incidents/{iid}").json()
    assert inc["stage"] == "needs_human" and not any(w["kind"] in ("approval", "retry") for w in inc["waiting"])
    assert "bỏ qua" in _recorded(iid, "skipped")["text"] and not any(e.get("action_id") for e in inc["timeline"])
    fix = flow.get(f"/api/conversations/{inc['conversation_id']}").json()["messages"][0]["incident_actions"][0]
    assert fix["status"] == "skipped"


def test_timeline_hides_old_dry_run_rows(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    action = flow.get(f"/api/incidents/{iid}").json()["actions"][0]
    flow.post(f"/api/approvals/{action['approval_id']}/decide", json={"action": "approve_once"})
    assert any(e["kind"] == "ran" for e in flow.get(f"/api/incidents/{iid}").json()["timeline"])
    with db.session() as conn:  # dữ liệu cũ: lệnh chạy thử từng được ghi như lệnh đã chạy
        conn.execute("UPDATE incident_action SET args=? WHERE id=?", (json.dumps({"command": "rollout restart deployment/web --dry-run=server"}), action["id"]))
    assert not any(e.get("action_id") for e in flow.get(f"/api/incidents/{iid}").json()["timeline"])


def test_timeline_attaches_change_check_and_hides_sandbox_scripts(flow, users):
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.6, [RESTART], verdict=False))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    action = flow.get(f"/api/incidents/{iid}").json()["actions"][0]
    flow.post(f"/api/approvals/{action['approval_id']}/decide", json={"action": "approve_once"})
    with db.session() as conn:
        t = tenant(users)
        for cid, args in (("c1", RESTART["args"]), ("c2", {"command": "apply -f - --dry-run=server"})):
            conn.execute("INSERT INTO change_record(id, tenant_id, approval_id, incident_id, user_id, env, tool, args, recipe, plan, status, check_result, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (cid, t, action["approval_id"] if cid == "c1" else None, iid, "u", "dev", RESTART["tool"],
                                                                 json.dumps(args), "r", "{}", "ok", json.dumps({"ready": True, "detail": "deployment/web 1/1 sẵn sàng"}), db.iso()))
            incidents.common._event(conn, t, iid, "change_checked", "Kiểm tra sau khi chạy …: ổn", {"change_id": cid}, actor="system")
        incidents.actions._insert(conn, t, iid, "investigation", "u", {"tool": "data.script", "args": {"code": "result = 1"}, "status": "ok", "result": {"result": 1}},
                                  None, None)
    tl = flow.get(f"/api/incidents/{iid}").json()["timeline"]
    assert not any(e["kind"] == "change_checked" for e in tl)  # gắn vào lệnh; kiểm tra của lệnh chạy thử thì bỏ
    ran = [e for e in tl if e["kind"] == "ran"]
    assert len(ran) == 1 and ran[0]["check"]["ok"] and ran[0]["check"]["text"] == "deployment/web 1/1 sẵn sàng"


def test_action_view_reads_gitlab_writes():
    from aiops.incidents.present import action_view
    commit = {"tool": "gl.api_admin", "status": "ok", "why": "Tạo nhánh fix/x và sửa values. Chi tiết khác.",
              "args": json.dumps({"method": "POST", "path": "/projects/1/repository/commits", "body": {
                  "branch": "fix/x", "commit_message": "fix: nới probe\n\nthân dài", "actions": [{"action": "update", "file_path": "values.yaml", "content": "x" * 5000}]}}),
              "result": '{"status": 201, "json": {"id": "8a2db5b3aaaa", "short_id": "8a2db5b3", "title": "fix: nới', "result_line": '{"status": 201, "json"'}
    v = action_view(commit)
    assert v["title"] == "Commit «fix: nới probe» lên nhánh fix/x (values.yaml)" and v["cmd"] == "POST /projects/1/repository/commits"
    assert v["result"] == "HTTP 201 · commit 8a2db5b3" and v["why"] == "Tạo nhánh fix/x và sửa values" and v["detail"].endswith("…")
    mr = {**commit, "args": json.dumps({"method": "POST", "path": "/projects/1/merge_requests", "body": {"title": "fix: nới", "source_branch": "fix/x", "target_branch": "main"}}),
          "result": '{"status": 201, "json": {"id": 5, "iid": 783, "title"'}
    assert (action_view(mr)["title"], action_view(mr)["result"]) == ("Mở MR «fix: nới» (fix/x → main)", "HTTP 201 · !783")
    patch = {"tool": "k8s.kubectl_write", "status": "error", "why": None, "result_line": "Error from server (NotFound)",
             "args": json.dumps({"command": "patch sts web -n a --type=json -p '[{\"op\":\"replace\"}]'"})}
    v = action_view(patch)
    assert v["title"] == v["cmd"] == "kubectl_write patch sts web -n a --type=json …" and v["result"] == "lỗi: Error from server (NotFound)"


def test_cause_parts_splits_run_on_paragraph():
    from aiops.incidents.present import cause_parts, headline
    text = ("Chưa chắc chắn hoàn toàn. Bằng chứng cho thấy probe timeout 1s quá ngắn (scrape ~2,1s). Hệ quả: flower restart 32 lần. "
            "Chưa đọc được ngưỡng của rule CPUThrottlingHigh.")
    assert cause_parts(text) == {"hedge": "Chưa chắc chắn hoàn toàn", "lead": "Probe timeout 1s quá ngắn (scrape ~2,1s).",
                                 "points": ["Hệ quả: flower restart 32 lần."], "gaps": ["Chưa đọc được ngưỡng của rule CPUThrottlingHigh."]}
    assert headline(text) == "Probe timeout 1s quá ngắn (scrape ~2,1s)"
    assert cause_parts("image lỗi") == {"hedge": None, "lead": "Image lỗi", "points": [], "gaps": []} and cause_parts("") is None


class FiveWhys(Reporting):
    """Báo cáo có chuỗi 5 Whys; same: id sự cố cũ mà so nguyên nhân trả về là trùng."""

    def __init__(self, whys, stop="no_evidence", next_question="", same=(), **kw):
        super().__init__(0.8, kw.pop("actions", []), **kw)
        self.whys, self.stop, self.next_question, self.same, self.compared = whys, stop, next_question, list(same), []

    def incident_report(self, data, tools):
        out = super().incident_report(data, tools)
        return {**out, "whys": self.whys, "stop": self.stop, "next_question": self.next_question,
                "factors": [{"type": "detect", "text": "không có alert cho pod chưa sẵn sàng", "refs": ["S1"]}]}

    def same_cause(self, new, olds):
        self.compared.append((new, olds))
        return [o["id"] for o in olds if o["id"] in self.same]


TECH = {"question": "Tại sao CPU cao?", "answer": "pod web vòng lặp retry", "refs": ["S1"], "weakness": False}
WEAK = {"question": "Tại sao retry không giới hạn?", "answer": "client không có backoff", "refs": [], "weakness": True}


def test_report_saves_5_whys_and_asks_next_question(flow, users, infra):
    iid = _web_incident(users)
    provider = FiveWhys([TECH], next_question="Tại sao bản deploy mới bật retry?")
    llm.set_provider(provider)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    assert provider.data["steps"][0]["code"] == "S1"
    rc = flow.get(f"/api/incidents/{iid}").json()["rootcause"]
    assert rc["whys"][0]["answer"] == "pod web vòng lặp retry" and rc["whys"][0]["grounded"] and rc["stop"] == "no_evidence"
    assert rc["level"] == 0 and rc["factors"][0]["label"] == "phát hiện chậm" and rc["factors"][0]["grounded"]
    task = next(w for w in flow.get(f"/api/incidents/{iid}").json()["waiting"] if w["kind"] == "task")
    assert task["source"] == "why" and "bật retry" in task["text"]
    with db.session() as conn:
        assert "5 Whys (chưa rõ nguyên nhân" in incidents.context_block(conn, tenant(users), iid)


def test_confirm_with_weakness_and_prevention_reaches_level_3(flow, users, infra):
    iid = _web_incident(users)
    llm.set_provider(FiveWhys([TECH], next_question="Tại sao retry không giới hạn?"))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    body = {"whys": [{"question": TECH["question"], "answer": TECH["answer"]}, {"question": WEAK["question"], "answer": WEAK["answer"], "weakness": True}],
            "prevent": "Thêm backoff cho client web"}
    assert flow.post(f"/api/incidents/{iid}/rootcause/confirm", json={"whys": [], "prevent": ""}).status_code == 400
    lv = flow.post(f"/api/incidents/{iid}/rootcause/confirm", json=body).json()
    assert lv["level"] == 3 and lv["confirmed"] and lv["weakness"] and lv["prevent"]
    d = flow.get(f"/api/incidents/{iid}").json()
    whys = d["rootcause"]["whys"]
    assert whys[0]["grounded"] and not whys[0]["by_user"] and whys[1]["by_user"] and whys[1]["weakness"]
    assert d["rootcause"]["prevent"] == ["Thêm backoff cho client web"] and d["rootcause"]["confirmed_by"]
    assert not any(w["kind"] == "task" and w["source"] == "why" for w in d["waiting"])  # chuỗi đã dừng ở điểm yếu: bỏ câu hỏi mở
    assert any(e["kind"] == "rc_confirmed" for e in d["timeline"])
    # Điều tra lại thay chuỗi: xác nhận cũ hết hiệu lực.
    with db.session() as conn:
        conn.execute("UPDATE incident SET stage='reported' WHERE id=?", (iid,))
        incidents.investigate(conn, tenant(users), iid)
    assert flow.get(f"/api/incidents/{iid}").json()["rootcause"]["confirmed"] is False


def test_review_per_round_and_confirm_infers_verdict(flow, users, infra):
    iid = _web_incident(users)
    assert flow.post(f"/api/incidents/{iid}/review", json={"verdict": "correct"}).status_code == 400  # chưa điều tra
    llm.set_provider(FiveWhys([TECH, {**WEAK, "weakness": False}]))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    assert flow.post(f"/api/incidents/{iid}/review", json={"verdict": "maybe"}).status_code == 400
    # Xác nhận mà bỏ một trong hai mắt xích của agent: đúng một phần.
    flow.post(f"/api/incidents/{iid}/rootcause/confirm", json={"whys": [{"question": TECH["question"], "answer": TECH["answer"]}], "prevent": ""})
    rv = flow.get(f"/api/incidents/{iid}").json()["review"]
    assert rv["verdict"] == "partial" and rv["source"] == "confirm" and "1/2" in rv["note"]
    # Chấm tay thắng, và xác nhận lại sau đó không ghi đè.
    assert flow.post(f"/api/incidents/{iid}/review", json={"verdict": "wrong", "note": "thật ra do node hết RAM"}).json()["verdict"] == "wrong"
    flow.post(f"/api/incidents/{iid}/rootcause/confirm", json={"prevent": ""})
    d = flow.get(f"/api/incidents/{iid}").json()
    assert d["review"]["verdict"] == "wrong" and d["review"]["source"] == "manual" and any(e["kind"] == "reviewed" for e in d["timeline"])
    # Lượt điều tra mới: chưa chấm; xác nhận nguyên chuỗi của agent là đúng.
    with db.session() as conn:
        conn.execute("UPDATE incident SET stage='reported' WHERE id=?", (iid,))
        incidents.investigate(conn, tenant(users), iid)
    assert flow.get(f"/api/incidents/{iid}").json()["review"] is None
    flow.post(f"/api/incidents/{iid}/rootcause/confirm", json={"prevent": ""})
    assert flow.get(f"/api/incidents/{iid}").json()["review"]["verdict"] == "correct"
    st = flow.get("/api/incidents/stats?days=30").json()
    assert st["rounds"] == 2 and st["reviews"] == {"correct": 1, "partial": 0, "wrong": 1, "total": 2, "manual": 1}
    assert st["accuracy"] == 0.5 and st["review_coverage"] == 1.0 and st["wrong"][0]["note"] == "thật ra do node hết RAM"
    assert st["first_insight_minutes"]["n"] == 1


def test_stats_mttr_from_first_alert_and_levels(flow, users, infra):
    t = tenant(users)
    poll(users)
    with db.session() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM incident WHERE tenant_id=?", (t,))]
        conn.execute("UPDATE alert_event SET starts_at='2026-11-12T08:00:00Z' WHERE tenant_id=?", (t,))
        conn.execute("UPDATE incident SET status='resolved', resolved_at='2026-11-12T08:30:00+00:00' WHERE id=?", (ids[0],))
        conn.execute("UPDATE incident SET status='resolved', resolved_at='2026-11-12T09:30:00+00:00' WHERE id=?", (ids[1],))
    st = flow.get("/api/incidents/stats").json()
    assert st["incidents"] == len(ids) and st["mttr_minutes"] == {"n": 2, "median": 60.0, "mean": 60.0}
    assert st["levels"]["1"] == 2 and st["accuracy"] is None and st["reviews"]["total"] == 0
    assert flow.get("/api/incidents/stats?env=prod").json()["incidents"] == 0
    assert flow.get("/api/incidents/stats?days=0").status_code == 400


def test_incident_page_links_steps_and_whys_to_source_tool(flow, users, infra):
    iid = _web_incident(users)

    class Querying(FiveWhys):
        def run_agent(self, task, context, tool_list, call_tool, **kw):
            call_tool("grafana.prometheus_query", {"expr": "rate(cpu[5m])"})
            call_tool("k8s.kubectl_read", {"command": "get pods -n web"})
            return {"answer": "xong", "tokens": 0}

    llm.set_provider(Querying([{**TECH, "refs": ["S1", "S2"]}]))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    d = flow.get(f"/api/incidents/{iid}").json()
    linked = [s for s in d["steps"] if s["link"]]
    assert [s["tool"] for s in linked] == ["grafana.prometheus_query"] and "/explore?" in linked[0]["link"]["url"]
    assert [x["label"] for x in d["rootcause"]["whys"][0]["links"]] == ["Mở trong Grafana Explore"]


def test_same_cause_recurrence_downgrades_old_incident_and_blames_lessons(flow, users, infra):
    from aiops import knowledge, sources
    t = tenant(users)
    old = _web_incident(users)
    llm.set_provider(FiveWhys([TECH]))
    with db.session() as conn:
        incidents.investigate(conn, t, old)
        conn.execute("UPDATE incident SET status='resolved', created_at=? WHERE id=?", (db.later(days=-3), old))
        doc = sources.add(conn, t, title="Sự cố cũ", content="x", kind="incident", trust="verified_tool", ref=f"{old}@x")
        ku = knowledge.add(conn, t, kind="lesson", title="retry gây CPU", content="c", source_eps=[f"doc:{doc['id']}#1"], source_trust="verified_tool")
        fact = knowledge.add(conn, t, kind="fact", title="web có 2 pod", content="c", source_eps=[f"doc:{doc['id']}#1"], source_trust="verified_tool")
        # Sự cố mới cùng nhóm.
        new = db.new_id()
        conn.execute("INSERT INTO incident(id,tenant_id,env,title,status,stage,created_at,updated_at,last_alert_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (new, t, "dev", "HighCPU · web lần 2", "open", "new", db.iso(), db.iso(), db.iso()))
        conn.execute("UPDATE alert_event SET incident_id=? WHERE id=(SELECT id FROM alert_event WHERE incident_id=? LIMIT 1)", (new, old))
        conn.execute("INSERT INTO alert_event(id,tenant_id,source,fingerprint,starts_at,alertname,severity,labels,summary,env,first_seen,last_seen,incident_id,status) "
                     "SELECT ?,tenant_id,source,fingerprint||'b',starts_at,alertname,severity,labels,summary,env,first_seen,last_seen,?,'resolved' "
                     "FROM alert_event WHERE incident_id=? LIMIT 1", (db.new_id(), old, new))
        provider = FiveWhys([TECH], same=[old])
        llm.set_provider(provider)
        incidents.investigate(conn, t, new)
        assert provider.compared and provider.compared[0][1][0]["id"] == old
        row = conn.execute("SELECT * FROM incident WHERE id=?", (old,)).fetchone()
        assert row["rc_recurred_by"] == new and incidents.rootcause.level(conn, row)["recurred"]["id"] == new
        assert knowledge.get(conn, t, ku["id"])["harmful_cnt"] == 1 and knowledge.get(conn, t, fact["id"])["harmful_cnt"] == 0
        incidents.rootcause.check_recurrence(conn, t, new, provider)  # đã đánh dấu: không so lại, không trừ điểm hai lần
        assert knowledge.get(conn, t, ku["id"])["harmful_cnt"] == 1


def test_restart_is_mitigation_even_if_report_calls_it_fix(flow, users, infra):
    from aiops.incidents.rootcause import action_kind, is_mitigation
    assert is_mitigation("k8s.kubectl_write", {"command": "rollout restart deployment/web -n default"})
    assert is_mitigation("k8s.kubectl_write", {"command": "kubectl scale deploy/web --replicas=3"})
    assert is_mitigation("os.api_write", {"path": "/servers/x/action", "body": {"reboot": {"type": "SOFT"}}})
    assert not is_mitigation("k8s.kubectl_write", {"command": "rollout undo deployment/web"})
    assert action_kind("k8s.kubectl_write", {"command": "rollout restart deploy/web"}, "fix") == "mitigate"
    iid = _web_incident(users)
    llm.set_provider(FiveWhys([TECH], actions=[{**RESTART, "kind": "fix"}]))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        assert conn.execute("SELECT kind FROM incident_action WHERE incident_id=?", (iid,)).fetchone()["kind"] == "mitigate"


def test_export_drafts_issue_and_postmortem_without_guessing(flow, users, infra):
    iid = _web_incident(users)
    llm.set_provider(FiveWhys([TECH], next_question="Tại sao retry không giới hạn?", actions=[RESTART]))
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
    flow.post(f"/api/incidents/{iid}/rootcause/confirm", json={"whys": [{"question": TECH["question"], "answer": TECH["answer"]},
                                                                      {"question": WEAK["question"], "answer": WEAK["answer"], "weakness": True}],
                                                             "prevent": "Thêm backoff cho client web"})
    d = flow.get(f"/api/incidents/{iid}/export").json()
    issue, pm = d["issue"], d["postmortem"]
    assert issue["title"].startswith("[<SERVICE>] ") and " tại " in issue["title"]
    assert "## Phần A: Thông tin incident" in issue["body"] and "| Ảnh hưởng khách hàng | Chưa xác định |" in issue["body"]
    assert "@TODO" in issue["body"] and '~"issuetype::Incident"' in issue["body"]
    assert any("rollout restart" in c and "→" in c for c in issue["comments"]), issue["comments"]
    assert "1. Tại sao CPU cao? pod web vòng lặp retry" in pm and "2. Tại sao retry không giới hạn? client không có backoff — điểm yếu sửa được" in pm
    assert "| Thêm backoff cho client web | phòng ngừa | @TODO | YYYY-MM-DD | TODO |" in pm
    assert "| không có alert cho pod chưa sẵn sàng | phát hiện |" in pm and "| Lỗ hổng monitoring | không có alert cho pod chưa sẵn sàng |" in pm
    assert "Biện pháp tạm thời còn giữ:** TODO: còn giữ" in pm  # restart là giảm nhẹ
    assert "**Tác nhân kích hoạt:** TODO" in pm and "Số khách hàng" not in pm  # không đoán ảnh hưởng khách hàng


# ---------- mức S1–S4 theo ảnh hưởng ----------

@pytest.mark.parametrize("answers, sev", [
    ({"customers": "few", "data_loss": True}, "S1"),  # mất dữ liệu luôn là S1
    ({"customers": "many"}, "S1"),
    ({"customers": "few"}, "S2"),
    ({"customers": "none", "control_plane": True}, "S2"),  # lỗi control plane là ảnh hưởng khách hàng
    ({"customers": "unknown"}, "S2"),  # không chắc: chọn mức cao hơn
    ({"customers": "none", "redundancy_lost": True}, "S3"),
    ({"customers": "internal"}, "S4"),
    ({"customers": "none"}, "S4"),
])
def test_grade_follows_incident_repo_rules(answers, sev):
    assert incidents.impact.grade(answers)[0] == sev


def test_suggested_level_until_a_human_assesses(flow, users, infra):
    iid = _web_incident(users)
    d = flow.get(f"/api/incidents/{iid}").json()
    assert d["impact"]["sev"] == "S4" and d["impact"]["source"] == "suggested" and not d["impact"]["postmortem"]["required"]
    with db.session() as conn:
        conn.execute("UPDATE incident SET env='prod', severity='critical' WHERE id=?", (iid,))
    d = flow.get(f"/api/incidents/{iid}").json()
    assert d["impact"]["sev"] == "S2" and d["impact"]["postmortem"] == {"required": True, "reasons": ["mức S2"], "due": None, "suggested_only": True}
    assert flow.get(f"/api/incidents/{iid}/export").json()["issue"]["severity"] == "TODO"  # gợi ý ở prod không thành mức chính thức


def test_assess_impact_sets_level_postmortem_and_export(flow, users, infra):
    iid = _web_incident(users)
    assert flow.post(f"/api/incidents/{iid}/impact", json={"customers": "lots"}).status_code == 400
    with db.session() as conn:
        conn.execute("UPDATE incident SET env='prod', status='resolved', resolved_at='2026-11-20T10:00:00+00:00' WHERE id=?", (iid,))  # thứ sáu
    out = flow.post(f"/api/incidents/{iid}/impact", json={"customers": "none", "control_plane": True, "missed_by_alert": True,
                                                          "note": "API server cluster VKE của 1 khách hàng"}).json()
    assert out["sev"] == "S2" and out["source"] == "human"
    assert out["postmortem"]["reasons"] == ["mức S2", "cảnh báo không phát hiện sự cố"]
    assert out["postmortem"]["due"] == "2026-11-27T10:00:00+00:00"  # 5 ngày làm việc, bỏ thứ bảy và chủ nhật
    d = flow.get(f"/api/incidents/{iid}").json()
    assert any(e["kind"] == "impact_assessed" and "S2" in e["text"] for e in d["timeline"])
    assert next(i for i in flow.get("/api/incidents?status=all").json()["items"] if i["id"] == iid)["sev"] == {"sev": "S2", "source": "human"}
    issue = flow.get(f"/api/incidents/{iid}/export").json()["issue"]
    assert issue["severity"] == "S2" and "control plane không gọi được (API server cluster VKE của 1 khách hàng)" in issue["body"]
    assert "- [x] Cần postmortem. Lý do: mức S2; cảnh báo không phát hiện sự cố" in issue["body"]


def test_s3_needs_postmortem_when_it_recurs_within_30_days(flow, users, infra):
    iid = _web_incident(users)
    flow.post(f"/api/incidents/{iid}/impact", json={"customers": "none", "redundancy_lost": True})
    assert flow.get(f"/api/incidents/{iid}").json()["impact"]["postmortem"]["required"] is False
    with db.session() as conn:
        conn.execute("UPDATE incident SET repeats=1 WHERE id=?", (iid,))
    assert flow.get(f"/api/incidents/{iid}").json()["impact"]["postmortem"]["reasons"] == ["S3 lặp lại 2 lần trong 30 ngày"]


# ---------- thông báo qua webhook ----------

def _notify(conn, users):
    return incidents.notify.dispatch(conn, tenant(users), background=False)


def test_notify_settings_hide_url_need_admin_and_check_target(flow, users, infra):
    assert users["alice"].get("/api/notify-settings").status_code == 403
    assert flow.put("/api/notify-settings", json={"enabled": True}).status_code == 400  # chưa có URL
    assert flow.put("/api/notify-settings", json={"enabled": True, "url": "https://internal.test/hook"}).status_code == 400  # mạng nội bộ
    assert flow.put("/api/notify-settings", json={"enabled": True, "url": "https://hook.test/x?token=abc", "events": ["bogus"]}).status_code == 400
    out = flow.put("/api/notify-settings", json={"enabled": True, "url": "https://hook.test/x?token=abc"}).json()
    assert out["url_set"] and out["url_hint"] == "https://hook.test/…" and "abc" not in json.dumps(flow.get("/api/notify-settings").json())
    assert out["public_url"] == "http://testserver" and out["since"]
    # Lưu lại không kèm URL thì giữ URL cũ và mốc since.
    again = flow.put("/api/notify-settings", json={"events": ["new"]}).json()
    assert again["url_set"] and again["since"] == out["since"] and again["events"] == ["new"]
    assert flow.post("/api/notify-settings/test").json() == {"ok": True} and infra.hooks[-1]["event"] == "test"
    infra.hook_status = 500
    assert "500" in flow.post("/api/notify-settings/test").json()["detail"]


def test_notify_new_incident_once_only_after_enabling(flow, users, infra):
    poll(users)  # sự cố có trước khi bật: không báo
    flow.put("/api/notify-settings", json={"enabled": True, "url": "https://hook.test/x"})
    with db.session() as conn:
        assert _notify(conn, users) == 0
    infra.am_alerts.append(am({"alertname": "DiskFull", "severity": "critical", "namespace": "db"}, "f9"))
    poll(users)
    with db.session() as conn:
        assert _notify(conn, users) == 1 and _notify(conn, users) == 0  # không gửi trùng
    hook = infra.hooks[-1]
    assert hook["event"] == "new" and "DiskFull" in hook["text"] and "[aiops · dev]" in hook["text"]
    assert hook["incident"]["url"] == f"http://testserver/#/t/{tenant(users)}/incidents/{hook['incident']['id']}"
    assert flow.get("/api/notify-settings").json()["recent"][0]["status"] == "sent"


def test_notify_pending_approval_and_needs_human(flow, users, infra, monkeypatch):
    flow.put("/api/notify-settings", json={"enabled": True, "url": "https://hook.test/x", "events": ["approval", "needs_human"]})
    iid = _web_incident(users)
    llm.set_provider(Reporting(0.9, [RESTART], verdict=False))  # auto mode chặn: thành yêu cầu duyệt
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        assert _notify(conn, users) == 1
    hook = infra.hooks[-1]
    assert hook["event"] == "approval" and hook["action"]["tool"] == "k8s.kubectl_write" and "rollout restart" in hook["text"]
    with db.session() as conn:
        conn.execute("UPDATE incident SET stage='needs_human', updated_at=? WHERE id=?", (db.iso(), iid))
        assert _notify(conn, users) == 1 and _notify(conn, users) == 0
        assert infra.hooks[-1]["event"] == "needs_human"
        # Gửi lỗi: ghi lỗi, không thử lại; lượt điều tra mới cần người thì báo lại.
        infra.hook_status = 502
        conn.execute("UPDATE incident SET investigations=investigations+1, updated_at=? WHERE id=?", (db.iso(), iid))
        assert _notify(conn, users) == 1 and _notify(conn, users) == 0
    assert flow.get("/api/notify-settings").json()["recent"][0] == {**flow.get("/api/notify-settings").json()["recent"][0],
                                                                     "status": "failed", "error": "webhook trả mã 502"}
