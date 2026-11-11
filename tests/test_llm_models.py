"""Lấy danh sách model qua Models API, chạy SDK thật trên transport giả."""
import anthropic
import httpx
import httpx2
import openai
import pytest

from kagent import llm, vault

URL = "/api/settings/llm-models"
KEY = "sk-ant-test-0123456789abcdefWXYZ"
RELAY = "https://relay.example.test"


@pytest.fixture()
def public_dns(monkeypatch):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])


@pytest.fixture()
def anthropic_api(monkeypatch):
    seen = []
    pages = {None: (["claude-opus-5-5", "claude-haiku-5-5"], True), "claude-haiku-5-5": (["claude-sonnet-5-5"], False)}

    def handler(request):
        seen.append(request)
        ids, more = pages[request.url.params.get("after_id")]
        data = [{"type": "model", "id": i, "display_name": i.replace("-", " ").title(), "created_at": "2026-01-01T00:00:00Z"} for i in ids]
        return httpx2.Response(200, json={"data": data, "has_more": more, "first_id": ids[0], "last_id": ids[-1]})

    real = anthropic.Anthropic
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kw: real(**kw, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))))
    return seen


def test_anthropic_models_follow_pagination(anthropic_api):
    models = llm.list_models(vault.Credential(KEY))
    assert [m["id"] for m in models] == ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]
    assert models[1]["name"] == "Claude Opus 5 5"
    sent = anthropic_api[0].headers
    assert len(anthropic_api) == 2 and KEY in (sent.get("x-api-key"), sent.get("authorization", "").removeprefix("Bearer "))


def test_relay_token_is_sent_as_bearer(anthropic_api):
    llm.list_models(vault.Credential(KEY, RELAY, "token"))
    req = anthropic_api[0]
    assert str(req.url).startswith(RELAY + "/v1/models") and req.headers["authorization"] == f"Bearer {KEY}"


def test_openai_models(monkeypatch):
    def handler(request):
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"object": "list", "data": [{"id": "gpt-x", "object": "model", "created": 0, "owned_by": "o"},
                                                                     {"id": "local-llama", "object": "model", "created": 0, "owned_by": "o"}]})

    real = openai.OpenAI
    monkeypatch.setattr(llm.openai, "OpenAI", lambda **kw: real(**kw, http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    models = llm.list_models(vault.Credential(KEY, RELAY, "api_key", "openai", "gpt-x"))
    assert models == [{"id": "gpt-x", "name": "gpt-x"}, {"id": "local-llama", "name": "local-llama"}]


@pytest.mark.parametrize("status, text", [(404, "không hỗ trợ liệt kê"), (401, "từ chối"), (500, "lỗi 500")])
def test_errors_are_explained(monkeypatch, status, text):
    real = anthropic.Anthropic
    transport = httpx2.MockTransport(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "x"}}))
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kw: real(**kw, http_client=httpx2.Client(transport=transport)))
    with pytest.raises(llm.LLMError, match=text):
        llm.list_models(vault.Credential(KEY, RELAY, "token"))


def test_endpoint_requires_org_admin(client, users):
    assert client.post(URL, json={"key": KEY}).status_code == 401
    assert users["alice"].post(URL, tenant=False, json={"key": KEY}).status_code == 403


def test_endpoint_lists_with_typed_key_and_never_echoes_it(users, public_dns, monkeypatch):
    got = {}
    monkeypatch.setattr(llm, "list_models", lambda cred: got.setdefault("cred", cred) and [{"id": "m1", "name": "M1"}])
    r = users["admin"].post(URL, tenant=False, json={"key": KEY, "base_url": RELAY + "/", "auth_type": "token"})
    assert r.status_code == 200 and r.json() == {"models": [{"id": "m1", "name": "M1"}]} and KEY not in r.text
    assert (got["cred"].key, got["cred"].base_url, got["cred"].auth_type) == (KEY, RELAY, "token")


def test_stored_key_reused_only_for_stored_endpoint(users, public_dns, monkeypatch):
    admin = users["admin"]
    monkeypatch.setattr(llm, "validate_key", lambda cred: None)
    assert admin.post("/api/settings/llm-key", tenant=False, json={"key": KEY, "base_url": RELAY, "auth_type": "token"}).status_code == 200
    sent = []
    monkeypatch.setattr(llm, "list_models", lambda cred: sent.append(cred) or [])
    assert admin.post(URL, tenant=False, json={"base_url": RELAY}).status_code == 200
    assert sent[0].key == KEY and sent[0].auth_type == "token"
    # Đổi endpoint mà không nhập key thì không được gửi key đã lưu đi nơi khác.
    for body in ({"base_url": "https://other.example.test"}, {}, {"base_url": RELAY, "provider": "openai"}):
        assert admin.post(URL, tenant=False, json=body).status_code == 400
    assert len(sent) == 1


def test_endpoint_rejects_private_base_url(users, monkeypatch):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    r = users["admin"].post(URL, tenant=False, json={"key": KEY, "base_url": RELAY})
    assert r.status_code == 400 and "nội bộ" in r.text


def test_list_error_becomes_400(users, monkeypatch):
    def fail(cred):
        raise llm.LLMError("endpoint không hỗ trợ liệt kê model; hãy nhập tên model bằng tay")

    monkeypatch.setattr(llm, "list_models", fail)
    r = users["admin"].post(URL, tenant=False, json={"key": KEY})
    assert r.status_code == 400 and "nhập tên model bằng tay" in r.text
