from types import SimpleNamespace as NS

import httpx
import pytest
import anthropic
from cryptography.fernet import Fernet

from kagent import db, llm, vault
from kagent.config import settings

URL = "/api/settings/llm-key"
KEY = "sk-ant-test-0123456789abcdefWXYZ"


@pytest.fixture()
def auto_provider(monkeypatch):
    settings.llm_provider = "auto"
    monkeypatch.setattr(llm, "validate_key", lambda key: None)


def provider_name(user):
    return user.get("/api/me", tenant=False).json()["llm_provider"]


def test_requires_login_and_org_admin(client, users):
    assert client.get(URL).status_code == 401
    alice = users["alice"]
    assert alice.get(URL, tenant=False).status_code == 403
    assert alice.post(URL, tenant=False, json={"key": KEY}).status_code == 403
    assert alice._call("DELETE", URL, tenant=False).status_code == 403


def test_save_never_returns_key_and_activates_claude(users, auto_provider):
    admin = users["admin"]
    assert provider_name(admin) == "heuristic"
    saved = admin.post(URL, tenant=False, json={"key": f"  {KEY}\n"})
    assert saved.status_code == 200, saved.text
    assert KEY not in saved.text and saved.json() == {**saved.json(), "source": "ui", "last4": "WXYZ", "updated_by": "admin"}
    assert KEY not in admin.get(URL, tenant=False).text
    assert provider_name(admin) == "anthropic"
    assert llm.get_provider().client.api_key == KEY


def test_key_is_encrypted_at_rest(users, auto_provider):
    users["admin"].post(URL, tenant=False, json={"key": KEY})
    with db.session() as conn:
        stored = conn.execute("SELECT * FROM llm_profile").fetchone()
    assert KEY not in stored["ciphertext"]
    assert KEY.encode() not in settings.db_path.read_bytes()


def test_rejected_key_is_not_saved(users, monkeypatch):
    admin = users["admin"]

    def reject(_):
        raise llm.LLMError("Anthropic từ chối key này")

    monkeypatch.setattr(llm, "validate_key", reject)
    r = admin.post(URL, tenant=False, json={"key": KEY})
    assert r.status_code == 400 and KEY not in r.text
    assert admin.get(URL, tenant=False).json()["source"] == "none"


@pytest.mark.parametrize("bad", ["ngan", "sk-ant with space 0123456789", "x" * 301, "sk-ant-abc\x00def0123456789012"])
def test_malformed_key_rejected_without_echo(users, auto_provider, bad):
    r = users["admin"].post(URL, tenant=False, json={"key": bad})
    assert r.status_code == 400
    if len(bad) > 4:
        assert bad not in r.text


def test_delete_returns_to_heuristic(users, auto_provider):
    admin = users["admin"]
    admin.post(URL, tenant=False, json={"key": KEY})
    assert admin._call("DELETE", URL, tenant=False).json()["source"] == "none"
    assert provider_name(admin) == "heuristic"


def test_ui_key_overrides_env_key(users, auto_provider):
    admin = users["admin"]
    settings.anthropic_key = "sk-ant-env-key-000000000000ENVV"
    env = admin.get(URL, tenant=False).json()
    assert env["source"] == "env" and env["last4"] == "ENVV" and env["auth_type"] == "api_key"
    admin.post(URL, tenant=False, json={"key": KEY})
    assert vault.resolve()[0].key == KEY and vault.resolve()[1] == "ui"


def test_changed_master_key_falls_back_without_crash(users, auto_provider):
    admin = users["admin"]
    admin.post(URL, tenant=False, json={"key": KEY})
    settings.secret_key = Fernet.generate_key().decode()
    assert admin.get(URL, tenant=False).json()["source"] == "none"
    assert provider_name(admin) == "heuristic"


def _status_error(cls, code):
    return cls("x", response=httpx.Response(code, request=httpx.Request("GET", "https://api.test")), body=None)


@pytest.mark.parametrize("cls,code,text", [
    (anthropic.AuthenticationError, 401, "từ chối"),
    (anthropic.NotFoundError, 404, "quyền dùng model"),
    (anthropic.RateLimitError, 429, "giới hạn"),
    (anthropic.InternalServerError, 500, "lỗi 500"),
])
def test_validate_key_maps_errors(monkeypatch, cls, code, text):
    class Models:
        def retrieve(self, _):
            raise _status_error(cls, code)

    class Client:
        def __init__(self, **_):
            self.models = Models()

    monkeypatch.setattr(llm.anthropic, "Anthropic", Client)
    with pytest.raises(llm.LLMError, match=text):
        llm.validate_key(vault.Credential(KEY))


RELAY = "https://relay.example.test"


@pytest.fixture()
def public_dns(monkeypatch):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])


def test_relay_token_uses_bearer_and_base_url(users, auto_provider, public_dns):
    admin = users["admin"]
    saved = admin.post(URL, tenant=False, json={"key": KEY, "base_url": RELAY + "/", "auth_type": "token"}).json()
    assert saved["base_url"] == RELAY and saved["auth_type"] == "token" and KEY not in str(saved)
    provider = llm.get_provider()
    assert provider.client.auth_token == KEY and not provider.client.api_key
    assert str(provider.client.base_url).rstrip("/") == RELAY
    assert provider.client.auth_headers["Authorization"] == f"Bearer {KEY}"
    assert provider.direct is False


def test_relay_requests_skip_server_side_fallback():
    calls = []

    class Fake:
        def create(self, **kw):
            calls.append(kw)
            return NS(content=[NS(type="text", text='{"items": []}')], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1))

    fake = Fake()
    p = llm.AnthropicProvider(NS(messages=fake, beta=NS(messages=fake)), vault.Credential(KEY, RELAY, "token"))
    p.reflect({"input": {"task": "x"}})
    assert "fallbacks" not in calls[0] and "betas" not in calls[0]


def test_relay_validation_sends_small_message_not_models_api(monkeypatch):
    seen = {}

    class Messages:
        def create(self, **kw):
            seen.update(kw)

    class Client:
        def __init__(self, **kw):
            seen["init"] = kw
            self.messages = Messages()

    monkeypatch.setattr(llm.anthropic, "Anthropic", Client)
    llm.validate_key(vault.Credential(KEY, RELAY, "token"))
    assert seen["max_tokens"] == 16 and seen["init"]["auth_token"] == KEY and seen["init"]["base_url"] == RELAY


@pytest.mark.parametrize("bad", ["http://relay.example.test", "ftp://x.test", "https://u:p@relay.example.test", "https://relay.example.test/?a=1", "relay.example.test"])
def test_bad_base_url_rejected(users, auto_provider, public_dns, bad):
    assert users["admin"].post(URL, tenant=False, json={"key": KEY, "base_url": bad}).status_code == 400


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1"])
def test_base_url_to_private_address_rejected(users, auto_provider, monkeypatch, ip):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 443))])
    r = users["admin"].post(URL, tenant=False, json={"key": KEY, "base_url": RELAY})
    assert r.status_code == 400 and ("nội bộ" in r.text or "bị chặn" in r.text)


def test_invalid_auth_type_rejected(users, auto_provider):
    assert users["admin"].post(URL, tenant=False, json={"key": KEY, "auth_type": "basic"}).status_code == 400


# ---------- LLM tự host trong mạng nội bộ ----------

def _dns(monkeypatch, ip):
    monkeypatch.setattr(llm.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 443))])


def test_internal_base_url_needs_opt_in(monkeypatch):
    _dns(monkeypatch, "10.20.30.40")
    with pytest.raises(llm.LLMError, match="cho phép địa chỉ mạng nội bộ"):
        llm.check_base_url("http://llm.internal:8000/v1")
    with pytest.raises(llm.LLMError, match="nội bộ"):
        llm.check_base_url("https://llm.internal:8000/v1")
    assert llm.check_base_url("http://llm.internal:8000/v1/", allow_private=True) == "http://llm.internal:8000/v1"
    assert llm.check_base_url("https://llm.internal/v1", allow_private=True) == "https://llm.internal/v1"


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"])
def test_loopback_and_metadata_blocked_even_with_opt_in(monkeypatch, ip):
    _dns(monkeypatch, ip)
    with pytest.raises(llm.LLMError, match="bị chặn"):
        llm.check_base_url("http://llm.internal/v1", allow_private=True)


def test_http_to_public_address_rejected_even_with_opt_in(monkeypatch):
    _dns(monkeypatch, "93.184.216.34")
    with pytest.raises(llm.LLMError, match="địa chỉ công khai phải dùng https"):
        llm.check_base_url("http://relay.example.test/v1", allow_private=True)


def test_save_and_list_models_for_local_openai_compatible(users, monkeypatch):
    _dns(monkeypatch, "10.20.30.40")
    admin = users["admin"]
    seen = []
    monkeypatch.setattr(llm, "validate_key", lambda cred: seen.append(cred))
    monkeypatch.setattr(llm, "list_models", lambda cred: seen.append(cred) or [{"id": "qwen", "name": "qwen"}])
    body = {"key": "ollama", "base_url": "http://llm.internal:11434/v1", "provider": "openai", "model_strong": "qwen"}
    assert admin.post(URL, tenant=False, json=body).status_code == 400  # chưa bật cho phép mạng nội bộ
    r = admin.post("/api/settings/llm-models", tenant=False, json={**body, "allow_private": True})
    assert r.status_code == 200 and r.json()["models"] == [{"id": "qwen", "name": "qwen"}]
    saved = admin.post(URL, tenant=False, json={**body, "allow_private": True})
    assert saved.status_code == 200, saved.text
    assert saved.json()["allow_private"] is True and saved.json()["base_url"] == "http://llm.internal:11434/v1"
    assert seen[-1].allow_private is True and seen[-1].key == "ollama"
    assert vault.resolve()[0].allow_private is True
    # Key ngắn chỉ được nhận cho dịch vụ tương thích OpenAI trong mạng nội bộ.
    assert admin.post(URL, tenant=False, json={"key": "short"}).status_code == 400


# ---------- nhiều cấu hình LLM ----------

KEY2 = "sk-ant-second-0123456789abcdefQRST"


def test_save_several_profiles_and_switch(users, auto_provider):
    admin = users["admin"]
    first = admin.post(URL, tenant=False, json={"key": KEY, "name": "Claude trực tiếp"}).json()
    second = admin.post(URL, tenant=False, json={"key": KEY2, "name": "Claude dự phòng"}).json()
    assert [p["name"] for p in second["profiles"]] == ["Claude dự phòng", "Claude trực tiếp"]
    assert second["profile"] == "Claude dự phòng" and llm.get_provider().client.api_key == KEY2
    assert KEY not in str(second) and KEY2 not in str(second)
    old = next(p for p in second["profiles"] if p["name"] == "Claude trực tiếp")
    switched = admin.post(f"/api/settings/llm-profiles/{old['id']}/activate", tenant=False).json()
    assert switched["profile"] == "Claude trực tiếp" and llm.get_provider().client.api_key == KEY
    assert llm.get_provider().label == "Claude trực tiếp"
    assert first["profile_id"] == old["id"]


def test_edit_profile_keeps_key_only_for_same_endpoint(users, auto_provider, public_dns):
    admin = users["admin"]
    saved = admin.post(URL, tenant=False, json={"key": KEY, "name": "relay", "base_url": RELAY, "auth_type": "token"}).json()
    pid = saved["profile_id"]
    renamed = admin.post(URL, tenant=False, json={"profile_id": pid, "name": "relay chính", "base_url": RELAY, "auth_type": "token", "model_strong": "claude-opus-5"})
    assert renamed.status_code == 200, renamed.text
    assert [p["name"] for p in renamed.json()["profiles"]] == ["relay chính"] and vault.resolve()[0].key == KEY
    # Đổi Base URL mà để trống key thì không được gửi key cũ tới máy chủ khác.
    moved = admin.post(URL, tenant=False, json={"profile_id": pid, "base_url": "https://other.example.test", "auth_type": "token"})
    assert moved.status_code == 400 and "giữ nguyên nhà cung cấp và Base URL" in moved.text


def test_profile_name_unique_and_delete_active_falls_back(users, auto_provider):
    admin = users["admin"]
    admin.post(URL, tenant=False, json={"key": KEY, "name": "A"})
    assert admin.post(URL, tenant=False, json={"key": KEY2, "name": "A"}).status_code == 400
    pid = admin.get(URL, tenant=False).json()["profile_id"]
    after = admin._call("DELETE", f"/api/settings/llm-profiles/{pid}", tenant=False).json()
    assert after["source"] == "none" and after["profiles"] == [] and provider_name(admin) == "heuristic"
    assert admin._call("DELETE", f"/api/settings/llm-profiles/{pid}", tenant=False).status_code == 404


def test_profile_endpoints_require_org_admin(users):
    alice = users["alice"]
    assert alice.post("/api/settings/llm-profiles/x/activate", tenant=False).status_code == 403
    assert alice._call("DELETE", "/api/settings/llm-profiles/x", tenant=False).status_code == 403


def test_list_models_with_saved_profile_key(users, auto_provider, monkeypatch):
    admin = users["admin"]
    pid = admin.post(URL, tenant=False, json={"key": KEY, "name": "A"}).json()["profile_id"]
    admin.post(URL, tenant=False, json={"key": KEY2, "name": "B"})  # B đang dùng
    seen = []
    monkeypatch.setattr(llm, "list_models", lambda cred: seen.append(cred.key) or [])
    assert admin.post("/api/settings/llm-models", tenant=False, json={"profile_id": pid}).status_code == 200
    assert seen == [KEY]
