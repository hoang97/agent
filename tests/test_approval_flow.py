"""Duyệt lệnh: grant theo phiên, lệnh cùng loại, agent tự chạy tiếp sau khi duyệt."""
import json

from kagent import agent, db, gateway
from test_chat import chat, done

# Lệnh đọc R1 ở prod của tenant mẫu phải hỏi (auto_execute=read_only), nên dùng để dựng lệnh chờ duyệt.
ENV = "prod"


def _conversation(user):
    return chat(user, "xem pod", env=ENV)[0]["conversation_id"]


def _pending_search(user, cid):
    message = done(chat(user, "tìm log lỗi", cid, env=ENV))["message"]
    step = message["steps"][0]
    assert step["status"] == "pending" and any("read_only" in r for r in step["reasons"])
    return message, step


def test_session_grant_covers_same_read_later(users):
    alice = users["alice"]
    cid = _conversation(alice)
    _, step = _pending_search(alice, cid)
    assert alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_session"}).json()["status"] == "approved"
    again = done(chat(alice, "tìm log lỗi", cid, env=ENV))["message"]["steps"][0]
    assert again["status"] == "ok" and again["reasons"] == ["dùng quyền đã duyệt cho phiên"]


def test_grant_never_covers_forced_reasons(users):
    admin, alice = users["admin"], users["alice"]
    ep = alice.run("scale deployment web lên 4 replica trong namespace default")
    step = [s for s in ep["trace"] if s["type"] == "tool_call"][0]
    assert step["status"] == "pending"
    alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_session"})
    admin.patch("/api/tenant", json={"change_freeze": True})
    again = [s for s in alice.run("scale deployment web lên 4 replica trong namespace default")["trace"] if s["type"] == "tool_call"][0]
    assert again["status"] == "pending" and "đang trong change freeze" in again["reasons"]


def test_session_grant_runs_other_pending_reads_of_same_tool(users):
    alice = users["alice"]
    cid = _conversation(alice)
    _, first = _pending_search(alice, cid)
    _, second = _pending_search(alice, cid)
    r = alice.post(f"/api/approvals/{first['approval_id']}/decide", json={"action": "approve_session"}).json()
    assert r["also_approved"] == [second["approval_id"]]
    sibling = next(a for a in alice.get("/api/approvals").json() if a["id"] == second["approval_id"])
    assert sibling["status"] == "approved" and sibling["result"]["status"] == "ok"


def test_continue_after_approval(users):
    alice = users["alice"]
    cid = _conversation(alice)
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
    cid = _conversation(alice)
    message, step = _pending_search(alice, cid)
    big = {"status": "ok", "result": {"hits": [{"request_id": f"req-{i:04d}", "message": "x" * 40} for i in range(60)]}}
    with db.session() as conn:
        conn.execute("UPDATE approval_request SET status='approved', result=? WHERE id=?", (json.dumps(big), step["approval_id"]))
        history = agent.load_history(conn, alice.tenant, cid)
    last = history["messages"][-1]["content"]
    # Trước đây chỉ 300 ký tự đầu của kết quả vào lịch sử, nên agent không bao giờ thấy các bản ghi sau.
    assert "req-0059" in last and "<untrusted>" in last and "đã cắt" not in last
    # Lệnh đã duyệt là ý của người dùng cho auto mode; kết quả của nó thì không.
    assert any(i.startswith("[người dùng đã duyệt lệnh] logs.search_logs") for i in history["intent"])
    assert "req-0059" not in json.dumps(history["intent"]) + json.dumps(history["calls"])
