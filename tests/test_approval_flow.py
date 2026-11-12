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


# ---------- quyết định có ghi chú, như Claude Code ----------

def _pending_write(user):
    evs = chat(user, "scale deployment web lên 4 replica trong namespace default")
    message = done(evs)["message"]
    return evs[0]["conversation_id"], message, message["approvals"][0]


def test_deny_with_note_lets_agent_continue_with_it(users):
    alice = users["alice"]
    cid, message, approval = _pending_write(alice)
    alice.post(f"/api/approvals/{approval['id']}/decide", json={"action": "deny", "reason": "kiểm tra số pod trước khi scale"})
    with db.session() as conn:
        history = agent.load_history(conn, alice.tenant, cid)
    note = history["messages"][-1]["content"]
    assert "người dùng từ chối, ghi chú: kiểm tra số pod trước khi scale" in note and "gọi lại công cụ" in note
    assert any(i.startswith("[người dùng từ chối lệnh] k8s.kubectl_write") and "kiểm tra số pod" in i for i in history["intent"])
    cont = done(chat_continue(alice, cid, message["id"]))["message"]
    assert cont["continuation"] is True and cont["task"] == agent.CONTINUE_TASK


def test_deny_without_note_stops(users):
    alice = users["alice"]
    cid, message, approval = _pending_write(alice)
    alice.post(f"/api/approvals/{approval['id']}/decide", json={"action": "deny"})
    r = alice.post("/api/chat", json={"continue_episode": message["id"], "conversation_id": cid})
    assert r.status_code == 409 and "ghi chú" in r.text
    with db.session() as conn:
        assert "không kèm ghi chú" in agent.load_history(conn, alice.tenant, cid)["messages"][-1]["content"]


def test_approve_with_note_reaches_agent_after_result(users):
    alice = users["alice"]
    cid, _, approval = _pending_write(alice)
    out = alice.post(f"/api/approvals/{approval['id']}/decide", json={"action": "approve_once", "reason": "xong thì kiểm tra rollout status"}).json()
    assert out["status"] == "approved" and out["note"] == "xong thì kiểm tra rollout status"
    with db.session() as conn:
        note = agent.load_history(conn, alice.tenant, cid)["messages"][-1]["content"]
    assert "đã duyệt và chạy" in note and note.rstrip().endswith("ghi chú của người duyệt: xong thì kiểm tra rollout status")


def test_why_shows_on_card_but_never_reaches_connector_or_classifier(users, monkeypatch):
    from kagent import llm
    seen = []

    class Judge:
        def classify_action(self, data):
            seen.append(data)
            return {"allow": None, "rule": "", "reason": "x"}
    monkeypatch.setattr(llm, "get_provider", lambda: Judge())
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        step = gateway.invoke(conn, gateway.CallContext(alice.tenant, uid, "dev", "t", intent=["scale web"]), "k8s.kubectl_write",
                              {"command": "scale deployment/web --replicas=4 -n default", "_why": "web đang quá tải, tăng lên 4 pod"})
        row = conn.execute("SELECT why, args FROM approval_request WHERE id=?", (step["approval_id"],)).fetchone()
    assert step["why"] == "web đang quá tải, tăng lên 4 pod" and "_why" not in step["args"]
    assert row["why"] == step["why"] and "_why" not in row["args"] and "quá tải" not in json.dumps(seen, ensure_ascii=False)


# ---------- agent không được nói "đang chờ duyệt" khi không có lệnh chờ ----------

class Scripted:
    """Provider giả: run_agent chạy kịch bản fn(call_tool) và trả câu trả lời."""

    name = "scripted"

    def __init__(self, fn):
        self.fn = fn

    def route(self, task):
        return {"task_type": "general", "entities": []}

    def run_agent(self, task, context, tools, call_tool, **kw):
        self.tools = {t["tool"] for t in tools}
        return {"answer": self.fn(call_tool), "tokens": 0}


def _run_scripted(users, fn):
    from kagent import llm
    provider = Scripted(fn)
    llm.set_provider(provider)
    try:
        with db.session() as conn:
            uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
            return agent.run_task(conn, users["alice"].tenant, uid, "rollback web"), provider
    finally:
        llm.set_provider(None)


def test_claiming_pending_without_a_call_is_nudged_once(users):
    got = []

    def fn(call_tool):
        got.append(call_tool.review_answer("Lệnh rollback đang chờ duyệt."))
        got.append(call_tool.review_answer("Lệnh rollback đang chờ duyệt."))
        return "ok"
    ep, _ = _run_scripted(users, fn)
    assert got == [agent.PENDING_NUDGE, None] and any(s["type"] == "nudge" for s in ep["trace"])


def test_real_pending_call_is_not_nudged(users):
    def fn(call_tool):
        step = call_tool("k8s.kubectl_write", {"command": "rollout undo deployment/web -n default"})
        assert step["status"] == "pending"
        return call_tool.review_answer("Lệnh rollback đang chờ duyệt.") or "ok"
    ep, _ = _run_scripted(users, fn)
    assert ep["output"] == "ok" and not any(s["type"] == "nudge" for s in ep["trace"])


# ---------- ask_user ----------

QUESTION = {"questions": [{"question": "Rollback về revision nào?", "header": "Revision",
                           "options": [{"label": "47 (khuyên dùng)", "description": "image còn trên registry"}, {"label": "46"}]}]}


def test_ask_user_shows_question_and_ends_turn(users):
    def fn(call_tool):
        asked = call_tool("ask_user", QUESTION)
        after = call_tool("k8s.kubectl_read", {"command": "get pods -n default"})
        return f"{asked['status']} {after['status']}"
    ep, provider = _run_scripted(users, fn)
    assert "ask_user" in provider.tools and ep["output"] == "asked blocked"
    step = next(s for s in ep["trace"] if s.get("tool") == "ask_user")
    assert step["questions"][0]["options"][0] == {"label": "47 (khuyên dùng)", "description": "image còn trên registry"}
    assert ep["outcome"] == "unknown"  # câu hỏi không tính là lệnh thành công hay thất bại


def test_ask_user_rejects_bad_shape(users):
    ep, _ = _run_scripted(users, lambda call_tool: call_tool("ask_user", {"questions": [{"question": "x", "options": [{"label": "a"}]}]})["status"])
    assert ep["output"] == "invalid"
