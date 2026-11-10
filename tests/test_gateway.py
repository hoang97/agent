import pytest

from kagent import gateway
from conftest import tool_steps


def policy(overlay=None):
    return gateway.merge_policy(overlay)


@pytest.mark.parametrize("tier,env,conf,expected", [
    ("R0", "prod", 0.0, "auto"),
    ("R1", "prod", 0.79, "ask"),
    ("R1", "prod", 0.8, "auto"),
    ("R2", "dev", 0.0, "auto"),
    ("R2", "staging", 0.69, "ask"),
    ("R2", "staging", 0.7, "auto"),
    ("R2", "prod", 1.0, "ask"),
    ("R3", "dev", 0.79, "ask"),
    ("R3", "dev", 0.8, "auto"),
    ("R3", "staging", 1.0, "ask"),
    ("R3", "prod", 1.0, "ask"),
    ("R4", "dev", 1.0, "ask"),
    ("R4", "staging", 1.0, "block"),
    ("R4", "prod", 1.0, "block"),
])
def test_baseline_matrix(tier, env, conf, expected):
    assert gateway.evaluate(policy(), tier, "x.y", env, conf, []).outcome == expected


def test_r3_prod_needs_two_approvers():
    assert gateway.evaluate(policy(), "R3", "x", "prod", 1.0, []).required == 2


def test_deny_beats_everything():
    p = policy({"deny": [{"tool": "*.delete*", "env": "dev"}]})
    d = gateway.evaluate(p, "R0", "k8s.delete", "dev", 1.0, [])
    assert d.outcome == "block" and d.door == "deny"


def test_forced_ask_ignores_score():
    assert gateway.evaluate(policy(), "R0", "x", "dev", 1.0, ["untrusted"]).outcome == "ask"


def test_read_only_switch():
    p = policy({"environments": {"prod": {"auto_execute": "read_only"}}})
    assert gateway.evaluate(p, "R0", "x", "prod", 0, []).outcome == "auto"
    assert gateway.evaluate(p, "R1", "x", "prod", 1.0, []).outcome == "ask"


def test_off_switch_stops_reads():
    p = policy({"environments": {"dev": {"auto_execute": "off"}}})
    assert gateway.evaluate(p, "R0", "x", "dev", 1.0, []).outcome == "ask"


@pytest.mark.parametrize("overlay", [
    {"matrix": {"R2": {"prod": "auto"}}},
    {"matrix": {"R4": {"prod": "ask"}}},
    {"thresholds": {"staging": {"R2": 0.5}}},
    {"min_approvers": {"prod": {"R3": 1}}},
    {"unknown": 1},
])
def test_overlay_cannot_loosen(overlay):
    with pytest.raises(gateway.PolicyError):
        gateway.merge_policy(overlay)


def test_overlay_can_tighten():
    p = gateway.merge_policy({"matrix": {"R0": {"dev": "ask"}}, "thresholds": {"staging": {"R2": 0.9}}})
    assert p["matrix"]["R0"]["dev"] == "ask"
    assert p["matrix"]["R2"]["staging"] == {"ask_below": 0.9}


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
    assert decided["result"]["result"]["to"] == 5


def strict(users):
    """Tắt tự duyệt: quay về luật cũ, lệnh phải do người có quyền khác duyệt."""
    assert users["admin"].patch("/api/tenant", json={"self_approve": False}).json()["self_approve"] is False


def test_requester_self_approves_by_default(users):
    alice = users["alice"]  # alice chỉ là member, không có vai trò approver
    step = tool_steps(alice.run("scale deployment web lên 4 replica"))[0]
    pending = alice.get("/api/approvals?status=pending").json()[0]
    assert pending["can_decide"] is True and pending["mine"] is True
    decided = alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert decided["status"] == "approved" and decided["result"]["result"]["to"] == 4
    audit = next(a for a in alice.get("/api/audit").json() if a["decision"] == "approved")
    assert audit["user"] == "alice" and audit["tool"] == "k8s.scale"


def test_self_approval_still_respects_hard_blocks(users):
    # Luật chặn của tenant: k8s.exec ở prod (khớp theo tên template, cho mọi kết nối Kubernetes)
    step = tool_steps(users["alice"].run("exec vào pod web-0 chạy `ls`", env="prod"))[0]
    assert step["decision"] == "blocked" and "approval_id" not in step


def test_self_approval_makes_prod_r3_single_confirmation(users):
    alice = users["alice"]
    step = tool_steps(alice.run("silence alert CPU cao", env="prod"))[0]
    assert step["required_approvals"] == 1
    assert alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()["status"] == "approved"


def test_self_deny_requires_reason_and_becomes_lesson(users):
    alice, admin = users["alice"], users["admin"]
    step = tool_steps(alice.run("scale deployment web lên 9 replica"))[0]
    url = f"/api/approvals/{step['approval_id']}/decide"
    assert alice.post(url, json={"action": "deny"}).status_code == 400
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


def test_deny_requires_reason_and_creates_lesson(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    step = tool_steps(alice.run("scale deployment web lên 9 replica"))[0]
    url = f"/api/approvals/{step['approval_id']}/decide"
    assert bob.post(url, json={"action": "deny"}).status_code == 400
    assert bob.post(url, json={"action": "deny", "reason": "đang giờ cao điểm"}).json()["status"] == "denied"
    admin.post("/api/learning/run")
    lessons = alice.get("/api/ku?kind=lesson").json()
    assert any("đang giờ cao điểm" in k["content"] for k in lessons)


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
    assert step["decision"] == "blocked" and step["tool"] == "k8s.delete_pod" and "*.delete*" in step["reasons"][0]


def test_untrusted_output_taints_following_commands(users):
    alice = users["alice"]
    alice.run("xem pod trong namespace default")
    ep = alice.run("tìm log lỗi rồi restart deployment web")
    steps = tool_steps(ep)
    assert [s["tool"] for s in steps] == ["logs.search_logs", "k8s.rollout_restart"]
    assert steps[0]["status"] == "ok" and steps[0]["untrusted"] is True
    assert steps[1]["status"] == "pending"
    assert any("untrusted" in r for r in steps[1]["reasons"])


def test_change_freeze_forces_ask(users):
    admin, alice = users["admin"], users["alice"]
    admin.patch("/api/tenant", json={"change_freeze": True})
    alice.run("xem pod trong namespace default")
    step = tool_steps(alice.run("scale deployment web lên 4 replica"))[0]
    assert step["status"] == "pending" and any("freeze" in r for r in step["reasons"])


def test_disabled_tool_is_hidden_from_agent_and_blocked_by_gateway(users):
    from kagent import db
    alice = users["alice"]
    users["admin"].patch("/api/tools/k8s.list_pods", json={"enabled": False})
    assert tool_steps(alice.run("xem pod")) == []
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        step = gateway.invoke(conn, gateway.CallContext(alice.tenant, uid, "dev", "xem pod"), "k8s.list_pods", {})
    assert step["decision"] == "blocked" and "chưa bật" in step["reasons"][0]


def test_tenant_admin_cannot_lower_risk_but_can_raise(users):
    from kagent import db
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        conn.execute("INSERT INTO membership(tenant_id,user_id,role) VALUES (?,?,'tenant_admin')", (alice.tenant, uid))
    assert alice.patch("/api/tools/k8s.list_pods", json={"risk_tier": "R2"}).status_code == 200
    assert alice.patch("/api/tools/k8s.scale", json={"risk_tier": "R0"}).status_code == 403


def test_audit_records_each_call(users):
    users["alice"].run("xem pod")
    audit = users["alice"].get("/api/audit").json()
    assert audit[0]["tool"] == "k8s.list_pods" and audit[0]["decision"] == "auto" and audit[0]["user"] == "alice"
