import json
import threading
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from conftest import Session
from kagent import agent, api as api_mod, db, llm, vault


def events(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")]


def chat(user, message, conversation_id=None, env="dev"):
    return events(user.post("/api/chat", json={"message": message, "conversation_id": conversation_id, "env": env}))


def done(evs):
    return next(e for e in evs if e["type"] == "done")


def test_stream_event_order_and_text(users):
    evs = chat(users["alice"], "xem pod trong namespace default")
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert kinds.index("tool_start") < kinds.index("tool_end") < kinds.index("delta")
    text = "".join(e["text"] for e in evs if e["type"] == "delta")
    final = done(evs)["message"]
    assert text == final["output"] and final["outcome"] == "success" and final["steps"][0]["tool"] == "k8s.get"
    assert final["id"] == evs[0]["run_id"]


def test_new_conversation_created_with_title_and_listed(users):
    alice = users["alice"]
    evs = chat(alice, "  xem   pod\ntrong default  ")
    listed = alice.get("/api/conversations").json()
    assert len(listed) == 1 and listed[0]["id"] == evs[0]["conversation_id"] and listed[0]["title"] == "xem pod trong default"


def test_conversation_keeps_turns_in_order(users):
    alice = users["alice"]
    cid = chat(alice, "xem pod")[0]["conversation_id"]
    chat(alice, "xem pod lần hai", cid)
    data = alice.get(f"/api/conversations/{cid}").json()
    assert [m["task"] for m in data["messages"]] == ["xem pod", "xem pod lần hai"]
    assert len(alice.get("/api/conversations").json()) == 1


def test_history_passed_to_provider_on_next_turn(users):
    seen = []

    class Recorder(llm.HeuristicProvider):
        def run_agent(self, task, context, tools, call_tool, history=None, on_text=None, cancelled=None):
            seen.append(history)
            on_text("trả lời " + task)
            return {"answer": "trả lời " + task, "tokens": 0}

    llm.set_provider(Recorder())
    alice = users["alice"]
    cid = chat(alice, "câu một")[0]["conversation_id"]
    chat(alice, "câu hai", cid)
    assert seen[0] == []
    assert seen[1] == [{"role": "user", "content": "câu một"}, {"role": "assistant", "content": "trả lời câu một"}]


def test_history_limited_by_budget(users):
    from kagent.config import settings
    settings.budget_history = 50

    class Long(llm.HeuristicProvider):
        def run_agent(self, task, context, tools, call_tool, history=None, on_text=None, cancelled=None):
            return {"answer": "x" * 150, "tokens": 0}

    llm.set_provider(Long())
    alice = users["alice"]
    cid = chat(alice, "một")[0]["conversation_id"]
    chat(alice, "hai", cid)
    with db.session() as conn:
        h = agent.load_history(conn, alice.tenant, cid)
    assert len(h["messages"]) == 2 and h["messages"][0]["content"] == "hai"


def test_approval_result_visible_in_next_turn_history(users):
    alice, bob = users["alice"], users["bob"]
    evs = chat(alice, "scale deployment web lên 5 replica")
    approval = done(evs)["message"]["approvals"][0]
    assert approval["status"] == "pending" and approval["tool"] == "k8s.scale"
    bob.post(f"/api/approvals/{approval['id']}/decide", json={"action": "approve_once"})
    cid = evs[0]["conversation_id"]
    with db.session() as conn:
        content = agent.load_history(conn, alice.tenant, cid)["messages"][1]["content"]
    assert "[Lệnh k8s.scale: approved" in content
    assert alice.get(f"/api/conversations/{cid}").json()["messages"][0]["approvals"][0]["status"] == "approved"


def test_chat_approval_card_lets_requester_decide(users):
    alice = users["alice"]
    evs = chat(alice, "scale deployment web lên 6 replica")
    approval = done(evs)["message"]["approvals"][0]
    assert approval["can_decide"] is True and approval["mine"] is True
    alice.post(f"/api/approvals/{approval['id']}/decide", json={"action": "approve_once"})
    after = alice.get(f"/api/conversations/{evs[0]['conversation_id']}").json()["messages"][0]["approvals"][0]
    assert after["status"] == "approved" and after["can_decide"] is False


def test_untrusted_content_taints_following_turns(users):
    alice = users["alice"]
    cid = chat(alice, "tìm log lỗi")[0]["conversation_id"]
    message = done(chat(alice, "xem pod", cid))["message"]
    assert message["steps"][0]["status"] == "pending"
    assert any("untrusted" in r for r in message["steps"][0]["reasons"])


def test_new_conversation_is_not_tainted(users):
    alice = users["alice"]
    chat(alice, "tìm log lỗi")
    assert done(chat(alice, "xem pod"))["message"]["steps"][0]["status"] == "ok"


def test_other_user_cannot_access_conversation(users):
    alice, bob = users["alice"], users["bob"]
    cid = chat(alice, "xem pod")[0]["conversation_id"]
    assert bob.get(f"/api/conversations/{cid}").status_code == 404
    assert bob.post("/api/chat", json={"message": "x", "conversation_id": cid}).status_code == 404
    assert bob.patch(f"/api/conversations/{cid}", json={"title": "y"}).status_code == 404
    assert bob._call("DELETE", f"/api/conversations/{cid}").status_code == 404
    assert bob.get("/api/conversations").json() == []


def test_other_tenant_cannot_access_conversation(client, users):
    admin = users["admin"]
    cid = chat(admin, "xem pod")[0]["conversation_id"]
    finance = Session(client, "admin", "team-finance")
    assert finance.get(f"/api/conversations/{cid}").status_code == 404


def test_rename_and_archive_keeps_episodes(users):
    alice = users["alice"]
    evs = chat(alice, "xem pod")
    cid = evs[0]["conversation_id"]
    assert alice.patch(f"/api/conversations/{cid}", json={"title": "Tên mới"}).status_code == 200
    assert alice.get("/api/conversations").json()[0]["title"] == "Tên mới"
    assert alice._call("DELETE", f"/api/conversations/{cid}").status_code == 200
    assert alice.get("/api/conversations").json() == []
    assert alice.get(f"/api/conversations/{cid}").status_code == 404
    assert alice.get(f"/api/episodes/{evs[0]['run_id']}").status_code == 200


def test_viewer_cannot_chat(users):
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        conn.execute("DELETE FROM membership WHERE user_id=?", (uid,))
        conn.execute("INSERT INTO membership(tenant_id,user_id,role) VALUES (?,?,'viewer')", (alice.tenant, uid))
    assert alice.post("/api/chat", json={"message": "x"}).status_code == 403


@pytest.mark.parametrize("body", [{"message": "   "}, {"message": "x", "env": "qa"}])
def test_invalid_chat_requests(users, body):
    assert users["alice"].post("/api/chat", json=body).status_code == 400


def test_quota_reported_as_error_event(users):
    alice = users["alice"]
    with db.session() as conn:
        conn.execute("UPDATE tenant SET token_quota_daily=0 WHERE id=?", (alice.tenant,))
    evs = chat(alice, "xem pod")
    assert evs[-1]["type"] == "error" and "quota" in evs[-1]["message"]


def test_unexpected_failure_does_not_leak_details(users):
    class Boom(llm.HeuristicProvider):
        def run_agent(self, *a, **k):
            raise RuntimeError("bí mật nội bộ")

    llm.set_provider(Boom())
    evs = chat(users["alice"], "xem pod")
    assert evs[-1]["type"] == "error" and "bí mật" not in evs[-1]["message"]


def test_cancel_records_partial_answer(users):
    alice = users["alice"]
    cancelled = threading.Event()

    class Stops(llm.HeuristicProvider):
        def run_agent(self, task, context, tools, call_tool, history=None, on_text=None, cancelled=None):
            on_text("phần đầu")
            cancelled.set()
            llm.check_cancelled(cancelled)

    llm.set_provider(Stops())
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ep = agent.run_task(conn, alice.tenant, uid, "xem pod", on_event=lambda e: None, cancelled=cancelled)
    assert ep["output"] == "phần đầu" and ep["outcome"] == "unknown"
    assert ep["trace"][-1]["type"] == "cancelled"


def test_cancel_stops_before_next_tool(users):
    alice = users["alice"]
    cancelled = threading.Event()
    cancelled.set()
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ep = agent.run_task(conn, alice.tenant, uid, "xem pod", on_event=lambda e: None, cancelled=cancelled)
    assert not [s for s in ep["trace"] if s["type"] == "tool_call"]


def test_cancel_endpoint_owner_only(users):
    alice, bob = users["alice"], users["bob"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
    event = threading.Event()
    api_mod._runs["run-1"] = (alice.tenant, uid, event)
    try:
        assert bob.post("/api/chat/run-1/cancel").status_code == 404
        assert not event.is_set()
        assert alice.post("/api/chat/run-1/cancel").status_code == 200
        assert event.is_set()
        assert alice.post("/api/chat/khong-co/cancel").status_code == 404
    finally:
        api_mod._runs.pop("run-1", None)


def test_chat_static_assets(client):
    html = client.get("/").text
    assert html.index("chat.js") < html.index("app.js")
    assert client.get("/static/chat.js").status_code == 200


# --- Stream ở tầng provider ---

class FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.final


def anthropic_provider(replies):
    calls = []

    class Api:
        def stream(self, **kw):
            calls.append(kw)
            return replies.pop(0)

    api = Api()
    return llm.AnthropicProvider(NS(messages=api, beta=NS(messages=api))), calls


def message(content, stop="end_turn"):
    return NS(content=content, stop_reason=stop, usage=NS(input_tokens=1, output_tokens=1))


def text_block(t):
    return NS(type="text", text=t)


def test_anthropic_stream_with_tools_joins_turns():
    first = FakeStream(["Để ", "tôi xem"], message([text_block("Để tôi xem"), NS(type="tool_use", name="k8s__get", input={}, id="t1")], "tool_use"))
    second = FakeStream(["Xong"], message([text_block("Xong")]))
    p, calls = anthropic_provider([first, second])
    out = []
    result = p.run_agent("x", "c", [], lambda n, a: {"status": "ok"}, history=[{"role": "user", "content": "cũ"}], on_text=out.append)
    assert "".join(out) == "Để tôi xem\n\nXong" == result["answer"]
    assert calls[0]["messages"][0]["content"] == "cũ"


def test_anthropic_stream_cancel():
    cancelled = threading.Event()
    cancelled.set()
    p, _ = anthropic_provider([FakeStream(["a"], message([text_block("a")]))])
    with pytest.raises(llm.Cancelled):
        p.run_agent("x", "c", [], lambda n, a: {}, on_text=lambda t: None, cancelled=cancelled)


def sse(*objs):
    return "".join(f"data: {json.dumps(o)}\n\n" for o in objs) + "data: [DONE]\n\n"


def delta(d, finish=None):
    return {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [{"index": 0, "delta": d, "finish_reason": finish}]}


def openai_provider(*bodies):
    seen = []
    queue = list(bodies)

    def handler(request):
        seen.append(json.loads(request.content))
        reply = queue.pop(0)
        if isinstance(reply, tuple):
            return httpx.Response(reply[0], json=reply[1])
        return httpx.Response(200, text=reply, headers={"content-type": "text/event-stream"})

    client = openai.OpenAI(api_key="k", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return llm.OpenAIProvider(client, vault.Credential("k", None, "api_key", "openai", "m")), seen


USAGE = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}


def test_openai_stream_assembles_tool_call_fragments():
    first = sse(delta({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "k8s__get", "arguments": ""}}]}),
                delta({"tool_calls": [{"index": 0, "function": {"arguments": '{"names'}}]}),
                delta({"tool_calls": [{"index": 0, "function": {"arguments": 'pace": "default"}'}}]}, "tool_calls"), USAGE)
    second = sse(delta({"content": "Đã "}), delta({"content": "xong"}, "stop"), USAGE)
    p, seen = openai_provider(first, second)
    called, out = [], []

    def call_tool(n, a):
        called.append((n, a))
        return {"status": "ok"}

    result = p.run_agent("x", "c", [], call_tool, on_text=out.append)
    assert called == [("k8s.get", {"namespace": "default"})]
    assert "".join(out) == "Đã xong" == result["answer"] and result["tokens"] == 14
    assert seen[0]["stream"] is True and seen[0]["stream_options"] == {"include_usage": True}
    assert seen[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"namespace": "default"}'


def test_openai_stream_retries_without_stream_options():
    bad = (400, {"error": {"message": "unknown field stream_options"}})
    p, seen = openai_provider(bad, sse(delta({"content": "ok"}, "stop")))
    out = []
    assert p.run_agent("x", "c", [], lambda n, a: {}, on_text=out.append)["answer"] == "ok"
    assert "stream_options" in seen[0] and "stream_options" not in seen[1]


def test_openai_history_order():
    p, seen = openai_provider(sse(delta({"content": "ok"}, "stop")))
    p.run_agent("mới", "c", [], lambda n, a: {}, history=[{"role": "user", "content": "cũ"}, {"role": "assistant", "content": "đáp"}], on_text=lambda t: None)
    assert [m["role"] for m in seen[0]["messages"]] == ["system", "user", "assistant", "user"]


def test_migration_adds_conversation_column_to_existing_db(client):
    with db.session() as conn:
        assert "conversation_id" in {r["name"] for r in conn.execute("PRAGMA table_info(episode)")}
        db.init_schema(conn)  # chạy lại không lỗi
