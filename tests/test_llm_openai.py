"""OpenAIProvider qua SDK thật, chặn ở tầng HTTP nên kiểm tra được định dạng yêu cầu và ánh xạ lỗi."""
import json

import httpx
import openai
import pytest

from kagent import llm, vault
from kagent.config import settings

URL = "/api/settings/llm-key"
KEY = "sk-openai-test-0123456789abcdWXYZ"
CRED = vault.Credential(KEY, None, "api_key", "openai", "model-a", "model-b")


def completion(content=None, tool_calls=None, finish="stop"):
    return {"id": "c1", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


def tool_call(name, args, id_="call_1"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": args}}


def provider(*replies):
    """replies: dict JSON, hoặc tuple (status, dict)."""
    seen = []
    queue = list(replies)

    def handler(request):
        seen.append({"body": json.loads(request.content), "auth": request.headers.get("authorization"), "path": request.url.path})
        reply = queue.pop(0)
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=body)

    client = openai.OpenAI(api_key=KEY, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return llm.OpenAIProvider(client, CRED), seen


def test_json_uses_strict_schema_and_chosen_tier():
    p, seen = provider(completion(json.dumps({"task_type": "docs", "entities": ["a"]})))
    out = p.route("viết tài liệu")
    assert out == {"task_type": "docs", "entities": ["a"], "tokens": 5}
    body = seen[0]["body"]
    assert body["model"] == "model-b" and body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True
    assert seen[0]["auth"] == f"Bearer {KEY}" and seen[0]["path"].endswith("/chat/completions")


def test_falls_back_when_json_schema_unsupported():
    bad = (400, {"error": {"message": "response_format not supported"}})
    p, seen = provider(bad, completion('Đây là kết quả:\n```json\n{"items": []}\n```'))
    assert p.reflect({"input": {"task": "x"}}) == []
    assert "response_format" not in seen[1]["body"] and "schema" in seen[1]["body"]["messages"][0]["content"]
    assert seen[1]["body"]["model"] == "model-a"


def test_agent_loop_runs_tools_through_callback_and_marks_untrusted():
    p, seen = provider(completion(tool_calls=[tool_call("logs__search_logs", '{"query": "x"}')], finish="tool_calls"), completion("xong"))
    calls = []

    def call_tool(name, args):
        calls.append((name, args))
        return {"status": "ok", "decision": "auto", "result": {"hits": []}, "untrusted": True}

    tools = [{"tool": "logs.search_logs", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    out = p.run_agent("tìm log", "ctx", tools, call_tool)
    assert calls == [("logs.search_logs", {"query": "x"})] and out == {"answer": "xong", "tokens": 10}
    assert seen[0]["body"]["tools"][0]["function"]["name"] == "logs__search_logs"
    assistant, result = seen[1]["body"]["messages"][2:4]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert result["role"] == "tool" and result["tool_call_id"] == "call_1" and result["content"].startswith("<untrusted>")


def test_agent_invalid_tool_arguments_not_executed():
    p, seen = provider(completion(tool_calls=[tool_call("k8s__list_pods", "{khong-json")], finish="tool_calls"), completion("thôi"))
    calls = []
    p.run_agent("x", "c", [], lambda n, a: calls.append(n))
    assert calls == [] and "invalid" in seen[1]["body"]["messages"][-1]["content"]


def test_agent_step_limit_ends_with_summary():
    replies = [completion(tool_calls=[tool_call("k8s__list_pods", "{}")], finish="tool_calls") for _ in range(settings.max_agent_steps)]
    p, fake = provider(*replies, completion("tổng kết"))
    assert p.run_agent("x", "c", [], lambda n, a: {"status": "ok"})["answer"] == "tổng kết"


@pytest.mark.parametrize("finish,text", [("content_filter", "từ chối"), ("length", "bị cắt")])
def test_finish_reasons(finish, text):
    p, _ = provider(completion("{}", finish=finish))
    with pytest.raises(llm.LLMError, match=text):
        p.route("x")


@pytest.mark.parametrize("status,text", [(401, "không hợp lệ"), (429, "tốc độ"), (500, "lỗi API 500")])
def test_api_errors_mapped(status, text):
    p, _ = provider((status, {"error": {"message": "x"}}))
    with pytest.raises(llm.LLMError, match=text):
        p.route("x")


def test_malformed_model_output_is_filtered():
    good = {"kind": "lesson", "title": "t", "content": "c", "applies_when": "a", "entities": [], "evidence": "e", "source": "tool", "blame_ku": None}
    items = [good, {**good, "kind": "khac"}, {"kind": "lesson"}, "sai"]
    p, _ = provider(completion(json.dumps({"items": items})), completion(json.dumps({"task_type": "khac"})), completion(json.dumps({"score": "abc"})), completion("không phải json"))
    assert p.reflect({"input": {}}) == [{**good, "scope": "tenant"}]
    assert p.route("x")["task_type"] == "general"
    assert p.check_plan("t", "k", {}) == 0.0
    with pytest.raises(llm.LLMError):
        p.route("x")


@pytest.mark.parametrize("status,text", [(401, "từ chối"), (404, "quyền dùng model model-a"), (429, "giới hạn"), (500, "lỗi 500")])
def test_validate_maps_errors(monkeypatch, status, text):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "x"}})

    real = openai.OpenAI
    monkeypatch.setattr(llm.openai, "OpenAI", lambda **kw: real(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    with pytest.raises(llm.LLMError, match=text):
        llm.validate_key(CRED)


@pytest.fixture()
def auto_provider(monkeypatch):
    settings.llm_provider = "auto"
    monkeypatch.setattr(llm, "validate_key", lambda cred: None)


def test_save_openai_activates_provider(users, auto_provider):
    admin = users["admin"]
    r = admin.post(URL, tenant=False, json={"key": KEY, "provider": "openai", "model_strong": "model-a", "model_fast": "model-b"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert KEY not in r.text and body["provider"] == "openai" and body["model_strong"] == "model-a" and body["last4"] == "WXYZ"
    assert admin.get("/api/me", tenant=False).json()["llm_provider"] == "openai"
    current = llm.get_provider()
    assert isinstance(current, llm.OpenAIProvider) and current.client.api_key == KEY and current.models == {"strong": "model-a", "fast": "model-b"}


def test_openai_requires_model(users, auto_provider):
    r = users["admin"].post(URL, tenant=False, json={"key": KEY, "provider": "openai"})
    assert r.status_code == 400 and "model" in r.text


@pytest.mark.parametrize("field,value", [("provider", "khac"), ("model_strong", "co dau cach"), ("model_strong", "x" * 101), ("model_fast", "a;b")])
def test_invalid_settings_rejected(users, auto_provider, field, value):
    assert users["admin"].post(URL, tenant=False, json={"key": KEY, "provider": "openai", "model_strong": "m", **{field: value}}).status_code == 400


def test_fast_model_defaults_to_chosen_strong_model():
    assert llm.AnthropicProvider(object(), vault.Credential(KEY, "https://x.test", "token", "anthropic", "claude-x")).models == {"strong": "claude-x", "fast": "claude-x"}


def test_default_models_kept_without_override():
    assert llm.AnthropicProvider(object(), vault.Credential(KEY)).models == {"strong": settings.model_strong, "fast": settings.model_fast}


def test_legacy_stored_credential_still_decrypts(users, auto_provider):
    from kagent import db
    with db.session() as conn:
        old = json.dumps({"key": KEY, "base_url": "https://r.test", "auth_type": "token"})
        conn.execute("INSERT INTO app_secret(name,ciphertext,last4,updated_at,updated_by) VALUES (?,?,?,?,?)",
                     (vault.LEGACY_NAME, vault._fernet().encrypt(old.encode()).decode(), "WXYZ", db.iso(), "x"))
    cred = vault.resolve()[0]
    assert cred.key == KEY and cred.provider == "anthropic" and cred.auth_type == "token"
    # Bản ghi cũ được chuyển thành cấu hình "Mặc định" đang dùng, rồi xóa khỏi app_secret.
    with db.session() as conn:
        profiles = vault.profiles(conn)
        assert [(p["name"], p["active"]) for p in profiles] == [("Mặc định", True)] and cred.profile == "Mặc định"
        assert conn.execute("SELECT 1 FROM app_secret WHERE name=?", (vault.LEGACY_NAME,)).fetchone() is None
