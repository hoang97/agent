"""Duyệt lệnh khi hội thoại có nội dung untrusted: grant theo môi trường, lệnh cùng loại, agent tự chạy tiếp."""
import json

from kagent import agent, db, gateway
from test_chat import chat, done


def _tainted_conversation(user, env="dev"):
    return chat(user, "tìm log lỗi", env=env)[0]["conversation_id"]


def _pending_search(user, cid, env="dev"):
    message = done(chat(user, "tìm log lỗi", cid, env=env))["message"]
    step = message["steps"][0]
    assert step["status"] == "pending" and gateway.UNTRUSTED_REASON in step["reasons"]
    return message, step


def test_session_grant_covers_tainted_reads_in_dev(users):
    alice = users["alice"]
    cid = _tainted_conversation(alice)
    _, step = _pending_search(alice, cid)
    assert alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_session"}).json()["status"] == "approved"
    again = done(chat(alice, "tìm log lỗi", cid))["message"]["steps"][0]
    assert again["status"] == "ok" and again["reasons"] == ["dùng quyền đã duyệt cho phiên"]


def test_grant_policy_off_keeps_asking(users):
    alice, admin = users["alice"], users["admin"]
    assert admin.patch("/api/tenant", json={"grant_policy": {"dev": {"untrusted": "off"}}}).status_code == 200
    cid = _tainted_conversation(alice)
    _, step = _pending_search(alice, cid)
    alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_session"})
    assert done(chat(alice, "tìm log lỗi", cid))["message"]["steps"][0]["status"] == "pending"


def test_grant_policy_min_confidence(users):
    alice, admin = users["alice"], users["admin"]
    admin.patch("/api/tenant", json={"grant_policy": {"dev": {"untrusted": "read", "min_confidence": 1.0}}})
    cid = _tainted_conversation(alice)
    _, step = _pending_search(alice, cid)
    alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_session"})
    assert done(chat(alice, "tìm log lỗi", cid))["message"]["steps"][0]["status"] == "pending"


def test_read_grant_does_not_cover_tainted_writes():
    policy = gateway.check_grant_policy({})
    forced = [gateway.UNTRUSTED_REASON]
    assert gateway.grant_covers(policy, "dev", "read", 0.1, forced) is True
    assert gateway.grant_covers(policy, "dev", "write", 0.9, forced) is False
    assert gateway.grant_covers(gateway.check_grant_policy({"dev": {"untrusted": "all"}}), "dev", "write", 0.9, forced) is True
    assert gateway.grant_covers(policy, "staging", "read", 0.4, forced) is False
    assert gateway.grant_covers(policy, "prod", "read", 1.0, forced) is False
    # Lý do ép hỏi khác (change freeze, đối tượng mới) không bao giờ được grant vượt qua.
    assert gateway.grant_covers(gateway.check_grant_policy({"dev": {"untrusted": "all"}}), "dev", "write", 1.0, ["đang trong change freeze"]) is False
    assert gateway.grant_covers(policy, "prod", "write", 0.0, []) is True


def test_grant_policy_api_validates_and_requires_admin(users):
    admin, alice = users["admin"], users["alice"]
    got = admin.get("/api/policy").json()
    assert got["grant_policy"] == gateway.DEFAULT_GRANT_POLICY and got["grant_ttl_minutes"] == 60
    assert admin.patch("/api/tenant", json={"grant_policy": {"prod": {"untrusted": "all"}}}).status_code == 400
    assert admin.patch("/api/tenant", json={"grant_policy": {"dev": {"min_confidence": 2}}}).status_code == 400
    assert admin.patch("/api/tenant", json={"grant_policy": {"qa": {}}}).status_code == 400
    assert alice.patch("/api/tenant", json={"grant_policy": {"dev": {"untrusted": "off"}}}).status_code == 403
    saved = admin.patch("/api/tenant", json={"grant_policy": {"staging": {"untrusted": "all", "min_confidence": 0.7}}}).json()["grant_policy"]
    assert saved["staging"] == {"untrusted": "all", "min_confidence": 0.7, "untrusted_auto": "none"} and saved["dev"] == gateway.DEFAULT_GRANT_POLICY["dev"]


def test_session_grant_runs_other_pending_reads_of_same_tool(users):
    alice = users["alice"]
    cid = _tainted_conversation(alice)
    _, first = _pending_search(alice, cid)
    _, second = _pending_search(alice, cid)
    r = alice.post(f"/api/approvals/{first['approval_id']}/decide", json={"action": "approve_session"}).json()
    assert r["also_approved"] == [second["approval_id"]]
    sibling = next(a for a in alice.get("/api/approvals").json() if a["id"] == second["approval_id"])
    assert sibling["status"] == "approved" and sibling["result"]["status"] == "ok"


def test_continue_after_approval(users):
    alice = users["alice"]
    cid = _tainted_conversation(alice)
    message, step = _pending_search(alice, cid)
    early = alice.post("/api/chat", json={"continue_episode": message["id"], "conversation_id": cid})
    assert early.status_code == 409 and "chờ duyệt" in early.text
    alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    evs = chat_continue(alice, cid, message["id"])
    cont = done(evs)["message"]
    assert cont["continuation"] is True and cont["task"] == agent.CONTINUE_TASK
    msgs = alice.get(f"/api/conversations/{cid}").json()["messages"]
    assert msgs[-2]["id"] == message["id"] and msgs[-2]["continued"] is True
    assert alice.post("/api/chat", json={"continue_episode": message["id"], "conversation_id": cid}).status_code == 409


def test_continue_rejects_old_turn_and_turn_without_approvals(users):
    alice = users["alice"]
    cid = chat(alice, "xem pod trong namespace default")[0]["conversation_id"]
    first = alice.get(f"/api/conversations/{cid}").json()["messages"][0]
    r = alice.post("/api/chat", json={"continue_episode": first["id"], "conversation_id": cid})
    assert r.status_code == 409 and "đã được duyệt" in r.text
    chat(alice, "xem pod", cid)
    assert alice.post("/api/chat", json={"continue_episode": first["id"], "conversation_id": cid}).status_code == 409


def chat_continue(user, cid, episode_id):
    from test_chat import events
    return events(user.post("/api/chat", json={"continue_episode": episode_id, "conversation_id": cid}))


def test_history_carries_full_result_of_latest_approved_commands(users):
    alice = users["alice"]
    cid = _tainted_conversation(alice)
    message, step = _pending_search(alice, cid)
    big = {"status": "ok", "result": {"hits": [{"request_id": f"req-{i:04d}", "message": "x" * 40} for i in range(60)]}}
    with db.session() as conn:
        conn.execute("UPDATE approval_request SET status='approved', result=? WHERE id=?", (json.dumps(big), step["approval_id"]))
        history = agent.load_history(conn, alice.tenant, cid)
    last = history["messages"][-1]["content"]
    # Trước đây chỉ 300 ký tự đầu của kết quả vào lịch sử, nên agent không bao giờ thấy các bản ghi sau.
    assert "req-0059" in last and "<untrusted>" in last and "đã cắt" not in last
    assert history["tainted"] is True


def test_tainted_r0_read_runs_in_dev_but_asks_in_staging(users):
    alice = users["alice"]
    cid = _tainted_conversation(alice)
    step = done(chat(alice, "tìm dashboard", cid))["message"]["steps"][0]
    assert step["tool"] == "grafana.search_dashboards" and step["status"] == "ok" and gateway.UNTRUSTED_REASON not in step["reasons"]
    # kubectl_read là R1 (đọc được log) nên vượt mức R0 mặc định của dev
    assert done(chat(alice, "xem pod", cid))["message"]["steps"][0]["status"] == "pending"
    cid = _tainted_conversation(alice, env="staging")
    step = done(chat(alice, "tìm dashboard", cid, env="staging"))["message"]["steps"][0]
    assert step["status"] == "pending" and gateway.UNTRUSTED_REASON in step["reasons"]


def test_tainted_auto_tier_is_configurable_and_limited_in_prod(users):
    admin, alice = users["admin"], users["alice"]
    assert admin.patch("/api/tenant", json={"grant_policy": {"prod": {"untrusted_auto": "R1"}}}).status_code == 400
    assert admin.patch("/api/tenant", json={"grant_policy": {"dev": {"untrusted_auto": "R2"}}}).status_code == 400
    admin.patch("/api/tenant", json={"grant_policy": {"dev": {"untrusted_auto": "none"}}})
    cid = _tainted_conversation(alice)
    assert done(chat(alice, "xem pod", cid))["message"]["steps"][0]["status"] == "pending"
    admin.patch("/api/tenant", json={"grant_policy": {"dev": {"untrusted_auto": "R1"}}})
    cid = _tainted_conversation(alice)
    assert done(chat(alice, "tìm log lỗi", cid))["message"]["steps"][0]["status"] == "ok"  # search_logs là R1
