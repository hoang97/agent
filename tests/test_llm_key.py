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
        stored = conn.execute("SELECT * FROM app_secret").fetchone()
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
    assert admin.get(URL, tenant=False).json() == {"source": "env", "last4": "ENVV", "updated_at": None, "updated_by": None}
    admin.post(URL, tenant=False, json={"key": KEY})
    assert vault.resolve() == (KEY, "ui")


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
        llm.validate_key(KEY)
