import json

import pytest

from kagent import db, gateway
from conftest import tool_steps


def policy(overlay=None):
    return gateway.merge_policy(overlay)


@pytest.mark.parametrize("tier,env,expected", [
    ("R0", "prod", "auto"), ("R1", "staging", "auto"), ("R1", "prod", "judge"),
    ("R2", "dev", "judge"), ("R2", "staging", "judge"), ("R2", "prod", "ask"),
    ("R3", "dev", "judge"), ("R3", "staging", "ask"), ("R3", "prod", "ask"),
    ("R4", "dev", "ask"), ("R4", "staging", "block"), ("R4", "prod", "block"),
])
def test_baseline_matrix(tier, env, expected):
    assert gateway.evaluate(policy(), tier, "x.y", env, []).outcome == expected


def test_r3_prod_needs_two_approvers():
    assert gateway.evaluate(policy(), "R3", "x", "prod", []).required == 2


def test_deny_beats_everything():
    p = policy({"deny": [{"tool": "*.delete*", "env": "dev"}]})
    d = gateway.evaluate(p, "R0", "k8s.delete", "dev", [])
    assert d.outcome == "block" and d.door == "deny"


def test_forced_reason_always_asks():
    assert gateway.evaluate(policy(), "R0", "x", "dev", ["đang trong change freeze"]).outcome == "ask"
    assert gateway.evaluate(policy(), "R2", "x", "dev", ["đang trong change freeze"]).outcome == "ask"


def test_read_only_switch():
    p = policy({"environments": {"prod": {"auto_execute": "read_only"}}})
    assert gateway.evaluate(p, "R0", "x", "prod", []).outcome == "auto"
    assert gateway.evaluate(p, "R1", "x", "prod", []).outcome == "ask"


def test_off_switch_stops_reads():
    p = policy({"environments": {"dev": {"auto_execute": "off"}}})
    assert gateway.evaluate(p, "R0", "x", "dev", []).outcome == "ask"


@pytest.mark.parametrize("overlay", [
    {"matrix": {"R2": {"prod": "auto"}}},
    {"matrix": {"R4": {"prod": "ask"}}},
    {"matrix": {"R2": {"prod": "judge"}}},
    {"min_approvers": {"prod": {"R3": 1}}},
    {"unknown": 1},
])
def test_overlay_cannot_loosen(overlay):
    with pytest.raises(gateway.PolicyError):
        gateway.merge_policy(overlay)


def test_overlay_can_tighten():
    p = gateway.merge_policy({"matrix": {"R0": {"dev": "ask"}, "R1": {"dev": "judge"}}})
    assert p["matrix"]["R0"]["dev"] == "ask" and p["matrix"]["R1"]["dev"] == "judge"


def test_overlay_saved_before_auto_mode_still_loads():
    """Ngưỡng điểm tin cậy cũ thành ô judge; overlay đã lưu không làm hỏng chính sách."""
    p = gateway.merge_policy({"matrix": {"R1": {"staging": {"ask_below": 0.9}}}, "thresholds": {"dev": {"R1": 0.5}}})
    assert p["matrix"]["R1"]["staging"] == "judge" and p["matrix"]["R1"]["dev"] == "judge"


def test_policy_api_rejects_loosening(users):
    r = users["admin"].put("/api/policy", json={"overlay": {"matrix": {"R2": {"prod": "auto"}}}})
    assert r.status_code == 400


def test_read_tool_auto_runs(users):
    step = tool_steps(users["alice"].run("xem pod trong namespace default"))[0]
    assert step["decision"] == "auto" and step["status"] == "ok"


def test_write_on_unseen_namespace_requires_approval_then_runs(users):
    alice, bob = users["alice"], users["bob"]
    step = tool_steps(alice.run("scale deployment web lên 5 replica"))[0]
    assert step["status"] == "pending"
    decided = bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert decided["status"] == "approved"
    assert "scaled" in decided["result"]["result"]["stdout"]


def strict(users):
    """Tắt tự duyệt: quay về luật cũ, lệnh phải do người có quyền khác duyệt."""
    assert users["admin"].patch("/api/tenant", json={"self_approve": False}).json()["self_approve"] is False


def test_requester_self_approves_by_default(users):
    alice = users["alice"]  # alice chỉ là member, không có vai trò approver
    step = tool_steps(alice.run("scale deployment web lên 4 replica"))[0]
    pending = alice.get("/api/approvals?status=pending").json()[0]
    assert pending["can_decide"] is True and pending["mine"] is True
    decided = alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert decided["status"] == "approved" and "scaled" in decided["result"]["result"]["stdout"]
    audit = next(a for a in alice.get("/api/audit").json() if a["decision"] == "approved")
    assert audit["user"] == "alice" and audit["tool"] == "k8s.kubectl_write"


def test_self_approval_still_respects_hard_blocks(users):
    # Luật chặn của tenant: k8s.kubectl_admin ở prod (khớp theo tên template, cho mọi kết nối Kubernetes)
    step = tool_steps(users["alice"].run("exec vào pod web-0 chạy `ls`", env="prod"))[0]
    assert step["decision"] == "blocked" and "approval_id" not in step


def test_self_approval_makes_prod_r3_single_confirmation(users):
    alice = users["alice"]
    step = tool_steps(alice.run("silence alert CPU cao", env="prod"))[0]
    assert step["required_approvals"] == 1
    assert alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()["status"] == "approved"


def test_self_deny_note_becomes_lesson(users):
    alice, admin = users["alice"], users["admin"]
    step = tool_steps(alice.run("scale deployment web lên 9 replica"))[0]
    url = f"/api/approvals/{step['approval_id']}/decide"
    assert alice.post(url, json={"action": "deny", "reason": "nhầm số replica"}).json()["status"] == "denied"
    admin.post("/api/learning/run")
    assert any("nhầm số replica" in k["content"] for k in alice.get("/api/ku?kind=lesson").json())


def test_other_member_cannot_decide_someone_elses_request(users):
    step = tool_steps(users["bob"].run("scale deployment web lên 4 replica"))[0]
    pending = users["alice"].get("/api/approvals?status=pending").json()[0]
    assert pending["can_decide"] is False and pending["mine"] is False
    r = users["alice"].post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert r.status_code == 403


def test_only_admin_can_toggle_self_approval(users):
    assert users["alice"].patch("/api/tenant", json={"self_approve": False}).status_code == 403
    assert users["admin"].get("/api/policy").json()["self_approve"] is True


def test_requester_cannot_self_approve_in_strict_tenant(users):
    strict(users)
    step = tool_steps(users["bob"].run("scale deployment web lên 4 replica"))[0]
    assert users["bob"].get("/api/approvals?status=pending").json()[0]["can_decide"] is False
    r = users["bob"].post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert r.status_code == 403


def test_member_without_approver_role_cannot_approve(users):
    step = tool_steps(users["bob"].run("scale deployment web lên 4 replica"))[0]
    r = users["alice"].post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert r.status_code == 403


def test_prod_r3_needs_two_people(users):
    strict(users)
    alice, bob, carol = users["alice"], users["bob"], users["carol"]
    # silence không có namespace nên không dính luật đối tượng mới
    step = tool_steps(alice.run("silence alert CPU cao", env="prod"))[0]
    assert step["required_approvals"] == 2
    first = bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert first["status"] == "pending"
    again = bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert again.status_code == 409
    second = carol.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert second["status"] == "approved"


def test_session_grant_skips_next_ask(users):
    alice, bob = users["alice"], users["bob"]
    alice.run("xem pod trong namespace default")  # namespace đã thấy
    first = tool_steps(alice.run("restart deployment web"))[0]
    assert first["status"] == "pending"
    bob.post(f"/api/approvals/{first['approval_id']}/decide", json={"action": "approve_session"})
    second = tool_steps(alice.run("restart deployment web"))[0]
    assert second["status"] == "ok"


def test_deny_note_creates_lesson_and_deny_without_note_does_not(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    noted = tool_steps(alice.run("scale deployment web lên 9 replica"))[0]
    silent = tool_steps(alice.run("scale deployment web lên 7 replica"))[0]
    assert bob.post(f"/api/approvals/{silent['approval_id']}/decide", json={"action": "deny"}).json()["deny_reason"] is None
    assert bob.post(f"/api/approvals/{noted['approval_id']}/decide", json={"action": "deny", "reason": "đang giờ cao điểm"}).json()["status"] == "denied"
    admin.post("/api/learning/run")
    lessons = alice.get("/api/ku?kind=lesson").json()
    assert any("đang giờ cao điểm" in k["content"] for k in lessons) and not any(k["content"].endswith("từ chối: ") for k in lessons)


def test_expired_approval_cannot_be_used(users, client):
    from kagent import db
    alice, bob = users["alice"], users["bob"]
    step = tool_steps(alice.run("scale deployment web lên 6 replica"))[0]
    with db.session() as conn:
        conn.execute("UPDATE approval_request SET expires_at=? WHERE id=?", (db.later(minutes=-1), step["approval_id"]))
    r = bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert r.status_code == 409


def test_prod_delete_blocked_by_tenant_deny(users):
    step = tool_steps(users["alice"].run("xóa pod web-0", env="prod"))[0]
    assert step["decision"] == "blocked" and step["tool"] == "k8s.kubectl_admin" and "k8s.kubectl_admin" in step["reasons"][0]


def test_untrusted_output_no_longer_forces_ask(users):
    """Auto mode không đọc kết quả công cụ nên không cần cờ nhiễm: đọc log xong, lệnh đọc sau vẫn tự chạy."""
    alice = users["alice"]
    logs = tool_steps(alice.run("tìm log lỗi"))[0]
    assert logs["status"] == "ok" and logs["untrusted"] is True
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ctx = gateway.CallContext(alice.tenant, uid, "dev", "xem pod", intent=["tìm log lỗi", "xem pod"],
                                  calls=[{"tool": logs["tool"], "args": logs["args"], "status": "ok"}])
        assert gateway.invoke(conn, ctx, "k8s.kubectl_read", {"command": "get pods -n default"})["status"] == "ok"


class FakeJudge:
    """Classifier giả: trả lần lượt các kết quả cho sẵn, ghi lại đầu vào."""

    def __init__(self, *verdicts):
        self.verdicts, self.seen = list(verdicts), []

    def classify_action(self, data):
        self.seen.append(data)
        allow = self.verdicts.pop(0) if self.verdicts else True
        return {"allow": allow, "rule": "" if allow else "Đoán đối tượng", "reason": "" if allow else "web không có trong yêu cầu"}


def _write(users, judge, monkeypatch, task="scale deployment web lên 4 replica trong namespace default", n=1):
    from kagent import db, llm
    monkeypatch.setattr(llm, "get_provider", lambda: judge)
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ctx = gateway.CallContext(alice.tenant, uid, "dev", task, intent=[task])
        return [gateway.invoke(conn, ctx, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"}) for _ in range(n)]


def test_auto_mode_allows_r3_in_dev(users, monkeypatch):
    judge = FakeJudge(True)
    step = _write(users, judge, monkeypatch)[0]
    assert step["status"] == "ok" and step["reasons"][0].startswith("auto mode cho qua")
    action = judge.seen[0]["action"]
    assert action["tool"] == "k8s.kubectl_write" and action["risk_tier"] == "R3" and judge.seen[0]["user_intent"][-1].startswith("scale")
    assert "dry_run" in action and "result" not in json.dumps(judge.seen[0]["previous_calls"])


def test_auto_mode_block_returns_to_agent_then_asks_after_three(users, monkeypatch):
    steps = _write(users, FakeJudge(False, False, False), monkeypatch, n=3)
    assert [s["status"] for s in steps] == ["blocked", "blocked", "pending"]
    assert steps[0]["reasons"] == ["auto mode chặn: Đoán đối tượng: web không có trong yêu cầu"]
    assert "3 lần liên tiếp" in steps[2]["reasons"][1] and steps[2]["required_approvals"] == 1


def test_auto_mode_unavailable_asks(users, monkeypatch):
    class Broken:
        def classify_action(self, data):
            raise RuntimeError("model hỏng")
    step = _write(users, Broken(), monkeypatch)[0]
    assert step["status"] == "pending" and "không đánh giá được" in step["reasons"][0]


def test_classifier_sees_calls_but_not_results(users, monkeypatch):
    judge = FakeJudge(True, True)
    from kagent import db, llm
    monkeypatch.setattr(llm, "get_provider", lambda: judge)
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ctx = gateway.CallContext(alice.tenant, uid, "dev", "t", intent=["restart web"])
        read = gateway.invoke(conn, ctx, "k8s.kubectl_read", {"command": "get pods -n default"})
        assert read["status"] == "ok" and read["result"]["stdout"]
        gateway.invoke(conn, ctx, "k8s.kubectl_write", {"command": "rollout restart deployment/web -n default"})
    assert judge.seen[-1]["previous_calls"] == [{"tool": "k8s.kubectl_read", "args": {"command": "get pods -n default"}, "status": "ok"}]


def test_auto_mode_rules_api(users):
    admin, alice = users["admin"], users["alice"]
    got = admin.get("/api/policy").json()["auto_mode"]
    assert got["custom"] == {} and got["effective"]["soft_deny"] == got["defaults"]["soft_deny"]
    saved = admin.patch("/api/tenant", json={"auto_mode": {"soft_deny": ["$defaults", "Không restart deployment payments"], "allow": ["Được scale web"]}}).json()
    eff = saved["auto_mode"]["effective"]
    assert eff["soft_deny"][-1] == "Không restart deployment payments" and len(eff["soft_deny"]) == len(got["defaults"]["soft_deny"]) + 1
    assert eff["allow"] == ["Được scale web"]  # không có $defaults thì thay hẳn mục đó
    assert admin.patch("/api/tenant", json={"auto_mode": {"qa": []}}).status_code == 400
    assert admin.patch("/api/tenant", json={"auto_mode": {"allow": "x"}}).status_code == 400
    assert alice.patch("/api/tenant", json={"auto_mode": {}}).status_code == 403


def test_change_freeze_forces_ask(users):
    admin, alice = users["admin"], users["alice"]
    admin.patch("/api/tenant", json={"change_freeze": True})
    alice.run("xem pod trong namespace default")
    step = tool_steps(alice.run("scale deployment web lên 4 replica"))[0]
    assert step["status"] == "pending" and any("freeze" in r for r in step["reasons"])


def test_disabled_tool_is_hidden_from_agent_and_blocked_by_gateway(users):
    from kagent import db
    alice = users["alice"]
    users["admin"].patch("/api/tools/k8s.kubectl_read", json={"enabled": False})
    assert tool_steps(alice.run("xem pod")) == []
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        step = gateway.invoke(conn, gateway.CallContext(alice.tenant, uid, "dev", "xem pod"), "k8s.kubectl_read", {"command": "get pods"})
    assert step["decision"] == "blocked" and "chưa bật" in step["reasons"][0]


def test_tenant_admin_cannot_lower_risk_but_can_raise(users):
    from kagent import db
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        conn.execute("INSERT INTO membership(tenant_id,user_id,role) VALUES (?,?,'tenant_admin')", (alice.tenant, uid))
    assert alice.patch("/api/tools/k8s.kubectl_read", json={"risk_tier": "R2"}).status_code == 200
    assert alice.patch("/api/tools/k8s.kubectl_write", json={"risk_tier": "R0"}).status_code == 403


def test_audit_records_each_call(users):
    users["alice"].run("xem pod")
    audit = users["alice"].get("/api/audit").json()
    assert audit[0]["tool"] == "k8s.kubectl_read" and audit[0]["decision"] == "auto" and audit[0]["user"] == "alice"
