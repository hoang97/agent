"""HTTP API và phục vụ giao diện. Mọi truy vấn đều đi kèm tenant_id đã kiểm tra quyền thành viên."""
from __future__ import annotations

import contextvars
import difflib
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import stat
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent as agent_mod
from . import compiler, connections, db, entities, gateway, incidents, knowledge, learned, learning, llm, net, proxy, tools, vault
from .connectors import k8s_rbac
from .connectors.base import ConnectorError
from .config import settings
from .retrieval import retrieve
from .scheduler import Scheduler

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "kagent_session"
log = logging.getLogger("kagent")
_failures: dict[str, list[float]] = defaultdict(list)
GLOBAL_ROLES = {"tenant_admin", "member", "viewer"}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def seed() -> None:
    """Tạo dữ liệu nền một lần. Mật khẩu lấy từ môi trường, nếu thiếu thì sinh và ghi vào file quyền 600."""
    with db.session() as conn:
        db.init_schema(conn)
        if conn.execute("SELECT 1 FROM org").fetchone():
            connections.sync_all(conn)
            connections.ensure_builtin(conn)
            return
        creds: dict[str, str] = {}

        def password(name: str, configured: str) -> str:
            value = configured or secrets.token_urlsafe(14)
            if not configured:
                creds[name] = value
            return value

        org = db.new_id()
        conn.execute("INSERT INTO org(id,name) VALUES (?,?)", (org, "Tổ chức demo"))
        policy = {
            "environments": {"prod": {"auto_execute": "read_only"}},
            "matrix": {"R2": {"staging": "ask"}},
            "deny": [{"tool": "k8s.kubectl_admin", "env": "prod"}, {"tool": "*.delete*", "env": "prod"}, {"tool": "k8s.get_secret_value", "env": "*"}],
        }
        tenants = {}
        for name in ("team-platform", "team-finance"):
            tid = db.new_id()
            tenants[name] = tid
            conn.execute("INSERT INTO tenant(id,org_id,name,policy) VALUES (?,?,?,?)", (tid, org, name, json.dumps(policy)))
        users = {"admin": (settings.admin_password, 1), "alice": (settings.demo_password, 0), "bob": (settings.demo_password, 0), "carol": (settings.demo_password, 0)}
        ids = {}
        for name, (configured, is_admin) in users.items():
            ids[name] = db.new_id()
            conn.execute("INSERT INTO app_user(id,username,pw_hash,is_org_admin) VALUES (?,?,?,?)",
                         (ids[name], name, hash_password(password(name, configured)), is_admin))
        platform = tenants["team-platform"]
        grants = [(platform, "admin", "tenant_admin"), (tenants["team-finance"], "admin", "tenant_admin"), (platform, "alice", "member"),
                  (platform, "bob", "member"), (platform, "bob", "approver:dev"), (platform, "bob", "approver:staging"), (platform, "bob", "approver:prod"),
                  (platform, "carol", "member"), (platform, "carol", "approver:prod")]
        for tenant, user, role in grants:
            conn.execute("INSERT INTO membership(tenant_id,user_id,role) VALUES (?,?,?)", (tenant, ids[user], role))
        connections.ensure_builtin(conn)
        if creds:
            path = settings.data_dir / "initial-credentials.txt"
            path.write_text("\n".join(f"{k}: {v}" for k, v in creds.items()) + "\n", encoding="utf-8")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            log.warning("Đã sinh mật khẩu ban đầu tại %s", path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    seed()
    with db.session() as conn:
        entities.renormalize(conn)
    resume_consolidations()
    sched = Scheduler()
    if settings.scheduler_enabled:
        sched.start()
    yield
    sched.stop()


app = FastAPI(title="Knowledge Agent", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api/docs"):
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class Principal:
    user_id: str
    username: str
    is_org_admin: bool


@dataclass
class Tenant:
    principal: Principal
    tenant_id: str
    roles: set[str]

    @property
    def user_id(self) -> str:
        return self.principal.user_id

    @property
    def is_admin(self) -> bool:
        return self.principal.is_org_admin or "tenant_admin" in self.roles

    def require_admin(self) -> None:
        if not self.is_admin:
            raise HTTPException(403, "cần quyền tenant_admin")

    def require_writer(self) -> None:
        if self.is_admin or "member" in self.roles or any(r.startswith("approver:") for r in self.roles):
            return
        raise HTTPException(403, "vai trò này chỉ được xem")


def _set_session_cookie(response: Response, token: str, seconds: int) -> None:
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=settings.cookie_secure, max_age=max(0, seconds), path="/")


def current_user(request: Request, response: Response, conn=Depends(get_conn)) -> Principal:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "chưa đăng nhập")
    r = conn.execute("SELECT u.id, u.username, u.is_org_admin, s.expires_at, s.created_at FROM session s JOIN app_user u ON u.id=s.user_id "
                     "WHERE s.token_hash=? AND s.expires_at>?", (_token_hash(token), db.iso())).fetchone()
    if r is None:
        raise HTTPException(401, "phiên đã hết hạn")
    # Phiên trượt: đang dùng thì gia hạn (khi còn dưới một nửa thời hạn để không ghi DB mỗi request), không quá tuổi tối đa.
    now = db.now()
    expires = datetime.fromisoformat(r["expires_at"])
    if (expires - now).total_seconds() < settings.session_hours * 1800:
        created = datetime.fromisoformat(r["created_at"]) if r["created_at"] else expires - timedelta(hours=settings.session_hours)
        new_expiry = min(now + timedelta(hours=settings.session_hours), created + timedelta(days=settings.session_max_days))
        if new_expiry > expires:
            conn.execute("UPDATE session SET expires_at=? WHERE token_hash=?", (db.iso(new_expiry), _token_hash(token)))
            _set_session_cookie(response, token, int((new_expiry - now).total_seconds()))
    return Principal(r["id"], r["username"], bool(r["is_org_admin"]))


def current_tenant(principal: Principal = Depends(current_user), x_tenant_id: str = Header(...), conn=Depends(get_conn)) -> Tenant:
    roles = {r["role"] for r in conn.execute("SELECT role FROM membership WHERE tenant_id=? AND user_id=?", (x_tenant_id, principal.user_id)).fetchall()}
    exists = conn.execute("SELECT 1 FROM tenant WHERE id=?", (x_tenant_id,)).fetchone()
    if not exists or (not roles and not principal.is_org_admin):
        raise HTTPException(403, "không thuộc tenant này")
    return Tenant(principal, x_tenant_id, roles)


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


def _throttle_key(request: Request, username: str) -> str:
    return f"{request.client.host if request.client else '?'}|{username.lower()}"


@app.post("/api/auth/login")
def login(body: Login, request: Request, response: Response, conn=Depends(get_conn)):
    key = _throttle_key(request, body.username)
    window = [t for t in _failures[key] if t > time.time() - 300]
    _failures[key] = window
    if len(window) >= 5:
        raise HTTPException(429, "đăng nhập sai quá nhiều lần, thử lại sau 5 phút")
    r = conn.execute("SELECT * FROM app_user WHERE username=?", (body.username,)).fetchone()
    if r is None or not verify_password(body.password, r["pw_hash"]):
        _failures[key].append(time.time())
        raise HTTPException(401, "sai tên đăng nhập hoặc mật khẩu")
    _failures.pop(key, None)
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM session WHERE expires_at<=?", (db.iso(),))
    conn.execute("INSERT INTO session(token_hash,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                 (_token_hash(token), r["id"], db.later(hours=settings.session_hours), db.iso()))
    _set_session_cookie(response, token, settings.session_hours * 3600)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, conn=Depends(get_conn)):
    token = request.cookies.get(COOKIE)
    if token:
        conn.execute("DELETE FROM session WHERE token_hash=?", (_token_hash(token),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


@app.post("/api/auth/password")
def change_password(body: ChangePassword, request: Request, principal: Principal = Depends(current_user), conn=Depends(get_conn)):
    key = "pw|" + _throttle_key(request, principal.username)
    window = [t for t in _failures[key] if t > time.time() - 300]
    _failures[key] = window
    if len(window) >= 5:
        raise HTTPException(429, "nhập sai quá nhiều lần, thử lại sau 5 phút")
    stored = conn.execute("SELECT pw_hash FROM app_user WHERE id=?", (principal.user_id,)).fetchone()
    if not verify_password(body.current_password, stored["pw_hash"]):
        _failures[key].append(time.time())
        raise HTTPException(403, "mật khẩu hiện tại không đúng")
    if body.new_password == body.current_password:
        raise HTTPException(400, "mật khẩu mới phải khác mật khẩu hiện tại")
    _failures.pop(key, None)
    with db.tx(conn):
        conn.execute("UPDATE app_user SET pw_hash=? WHERE id=?", (hash_password(body.new_password), principal.user_id))
        # Đổi mật khẩu thì đăng xuất mọi phiên khác của tài khoản này.
        conn.execute("DELETE FROM session WHERE user_id=? AND token_hash!=?", (principal.user_id, _token_hash(request.cookies[COOKIE])))
    return {"ok": True}


def require_org_admin(principal: Principal = Depends(current_user)) -> Principal:
    if not principal.is_org_admin:
        raise HTTPException(403, "cần quyền org_admin")
    return principal


class ProxyBody(BaseModel):
    url: str | None = Field(default=None, max_length=2000)


def _proxy_status(load: bool) -> dict:
    if load and proxy.configured_url():
        proxy._current()
    return proxy.status()


@app.get("/api/settings/proxy")
def proxy_get(_: Principal = Depends(require_org_admin)):
    return _proxy_status(load=False)


@app.put("/api/settings/proxy")
def proxy_put(body: ProxyBody, principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    try:
        with db.tx(conn):
            proxy.set_url(conn, body.url)
    except proxy.ProxyError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("%s đặt URL file PAC: %s", principal.username, body.url or "(bỏ)")
    return _proxy_status(load=True)


@app.post("/api/settings/proxy/reload")
def proxy_reload(_: Principal = Depends(require_org_admin)):
    proxy.reset()
    return _proxy_status(load=True)


class ProxyTest(BaseModel):
    url: str = Field(max_length=2000)


@app.post("/api/settings/proxy/test")
def proxy_test(body: ProxyTest, _: Principal = Depends(require_org_admin)):
    try:
        routes = proxy.routes(body.url, "auto")
    except proxy.ProxyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"URL không hợp lệ ({exc.__class__.__name__})") from exc
    return {"url": body.url, "routes": routes, "text": "; ".join(net.describe_route(r) for r in routes)}


class LLMKey(BaseModel):
    # Kiểm tra độ dài trong handler: lỗi 422 của pydantic sẽ trả lại nguyên giá trị đầu vào.
    key: str = ""  # để trống khi sửa cấu hình đã lưu thì giữ key cũ
    name: str | None = Field(default=None, max_length=100)
    profile_id: str | None = None  # sửa cấu hình này thay vì tạo mới
    activate: bool = True
    base_url: str | None = None
    auth_type: str = "api_key"
    provider: str = "anthropic"
    model_strong: str | None = None
    model_fast: str | None = None
    allow_private: bool = False


_MODEL_NAME = re.compile(r"[\w.:/-]{1,100}")


def _clean_model(value: str | None) -> str | None:
    value = (value or "").strip()
    if value and not _MODEL_NAME.fullmatch(value):
        raise HTTPException(400, "tên model không hợp lệ")
    return value or None


@app.get("/api/settings/llm-key")
def llm_key_status(_: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    return vault.status(conn)


def _clean_key(value: str, local: bool = False) -> str:
    """local: dịch vụ tương thích OpenAI trong mạng nội bộ, thường không cần key thật (ví dụ Ollama nhận bất kỳ chuỗi nào)."""
    key = value.strip()
    if not (1 if local else 20) <= len(key) <= 300 or any(c.isspace() or not c.isprintable() for c in key):
        raise HTTPException(400, "key không đúng định dạng" + ("" if local else " (với LLM nội bộ không cần key, nhập một chuỗi bất kỳ và bật cho phép mạng nội bộ)"))
    return key


def _check_endpoint(body) -> None:
    if body.auth_type not in ("api_key", "token"):
        raise HTTPException(400, "loại xác thực không hợp lệ")
    if body.provider not in ("anthropic", "openai"):
        raise HTTPException(400, "nhà cung cấp không hợp lệ")


class LLMModelsQuery(BaseModel):
    key: str = ""  # để trống thì dùng key của cấu hình profile_id, hoặc cấu hình đang dùng
    profile_id: str | None = None
    base_url: str | None = None
    auth_type: str = "api_key"
    provider: str = "anthropic"
    allow_private: bool = False


def _stored_key_for(conn, body, base_url: str | None) -> vault.Credential:
    """Key đã lưu chỉ được dùng lại cho đúng endpoint đã lưu: đổi Base URL mà không nhập key sẽ gửi key đó tới máy chủ khác."""
    current = vault.get_profile(conn, body.profile_id) if body.profile_id else vault.resolve()[0]
    if current is None or current.base_url != base_url or current.provider != body.provider:
        raise HTTPException(400, "hãy nhập key, hoặc giữ nguyên nhà cung cấp và Base URL của cấu hình đã lưu")
    return current


@app.post("/api/settings/llm-models")
def llm_models(body: LLMModelsQuery, _: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    _check_endpoint(body)
    try:
        base_url = llm.check_base_url(body.base_url, body.allow_private)
        if body.key.strip():
            cred = vault.Credential(_clean_key(body.key, body.allow_private and body.provider == "openai"), base_url, body.auth_type, body.provider,
                                    allow_private=body.allow_private)
        else:
            cred = _stored_key_for(conn, body, base_url)
        return {"models": llm.list_models(cred)}
    except llm.LLMError as exc:
        raise HTTPException(400, f"không lấy được danh sách model: {exc}") from exc


@app.post("/api/settings/llm-key")
def llm_key_save(body: LLMKey, principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    _check_endpoint(body)
    strong, fast = _clean_model(body.model_strong), _clean_model(body.model_fast)
    if body.provider == "openai" and not strong:
        raise HTTPException(400, "OpenAI cần tên model")
    try:
        base_url = llm.check_base_url(body.base_url, body.allow_private)
        if body.key.strip() or not body.profile_id:
            key = _clean_key(body.key, body.allow_private and body.provider == "openai")
        else:
            key = _stored_key_for(conn, body, base_url).key
        cred = vault.Credential(key, base_url, body.auth_type, body.provider, strong, fast, body.allow_private)
        llm.validate_key(cred)
    except llm.LLMError as exc:
        raise HTTPException(400, f"không lưu key: {exc}") from exc
    try:
        vault.save_profile(conn, cred, principal.username, name=body.name, profile_id=body.profile_id, activate=body.activate)
    except vault.VaultError as exc:
        raise HTTPException(400, str(exc)) from exc
    return vault.status(conn)


@app.post("/api/settings/llm-profiles/{profile_id}/activate")
def llm_profile_activate(profile_id: str, principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    try:
        vault.activate(conn, profile_id, principal.username)
    except vault.VaultError as exc:
        raise HTTPException(404, str(exc)) from exc
    return vault.status(conn)


@app.delete("/api/settings/llm-profiles/{profile_id}")
def llm_profile_delete(profile_id: str, principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    try:
        vault.delete_profile(conn, profile_id, principal.username)
    except vault.VaultError as exc:
        raise HTTPException(404, str(exc)) from exc
    return vault.status(conn)


@app.delete("/api/settings/llm-key")
def llm_key_delete(principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    vault.delete(conn, principal.username)
    return vault.status(conn)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/me")
def me(principal: Principal = Depends(current_user), conn=Depends(get_conn)):
    if principal.is_org_admin:
        rs = conn.execute("SELECT id, name FROM tenant ORDER BY name").fetchall()
    else:
        rs = conn.execute("SELECT DISTINCT t.id, t.name FROM tenant t JOIN membership m ON m.tenant_id=t.id WHERE m.user_id=? ORDER BY t.name", (principal.user_id,)).fetchall()
    tenants = []
    for r in rs:
        roles = [m["role"] for m in conn.execute("SELECT role FROM membership WHERE tenant_id=? AND user_id=?", (r["id"], principal.user_id)).fetchall()]
        tenants.append({"id": r["id"], "name": r["name"], "roles": roles})
    return {"username": principal.username, "is_org_admin": principal.is_org_admin, "tenants": tenants, "llm_provider": llm.get_provider().name,
            "profile": _profile(conn, principal.user_id)}


STYLES = ("default", "concise", "detailed")


def _profile(conn, user_id: str) -> dict:
    r = conn.execute("SELECT instructions, style, default_env FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
    return dict(r) if r else {"instructions": "", "style": "default", "default_env": "dev"}


class ProfileBody(BaseModel):
    instructions: str = Field(default="", max_length=2000)
    style: str = "default"
    default_env: str = "dev"


@app.get("/api/me/profile")
def profile_get(principal: Principal = Depends(current_user), conn=Depends(get_conn)):
    return _profile(conn, principal.user_id)


@app.put("/api/me/profile")
def profile_put(body: ProfileBody, principal: Principal = Depends(current_user), conn=Depends(get_conn)):
    if body.style not in STYLES or body.default_env not in gateway.ENVS:
        raise HTTPException(400, "phong cách hoặc môi trường không hợp lệ")
    conn.execute("INSERT INTO user_profile(user_id,instructions,style,default_env,updated_at) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET instructions=excluded.instructions, style=excluded.style, default_env=excluded.default_env, updated_at=excluded.updated_at",
                 (principal.user_id, knowledge.scrub(body.instructions.strip()), body.style, body.default_env, db.iso()))
    return _profile(conn, principal.user_id)


class RunBody(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    env: str = "dev"
    project: str | None = Field(default=None, max_length=64)


@app.post("/api/runs")
def run(body: RunBody, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    if body.env not in gateway.ENVS:
        raise HTTPException(400, "môi trường không hợp lệ")
    try:
        return agent_mod.run_task(conn, t.tenant_id, t.user_id, body.task, body.env, body.project)
    except agent_mod.QuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc


_runs: dict[str, tuple[str, str, threading.Event, str]] = {}  # run_id → (tenant, user, cancelled, conversation)
_runs_lock = threading.Lock()


class ChatBody(BaseModel):
    message: str = Field(default="", max_length=8000)
    conversation_id: str | None = None
    continue_episode: str | None = None  # chạy tiếp lượt này sau khi các lệnh chờ duyệt của nó đã được quyết định
    env: str = "dev"
    project: str | None = Field(default=None, max_length=64)


def _continued(conn, tenant_id: str, episode_id: str) -> bool:
    return conn.execute("SELECT 1 FROM episode WHERE tenant_id=? AND json_extract(input, '$.continuation_of')=? LIMIT 1",
                        (tenant_id, episode_id)).fetchone() is not None


def _check_continuable(conn, t: "Tenant", conversation_id: str, episode_id: str) -> str:
    """Chỉ chạy tiếp lượt mới nhất của hội thoại, khi nó không còn lệnh chờ, có ít nhất một lệnh đã chạy, và chưa được chạy tiếp."""
    latest = conn.execute("SELECT id FROM episode WHERE tenant_id=? AND conversation_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                          (t.tenant_id, conversation_id)).fetchone()
    if latest is None or latest["id"] != episode_id:
        raise HTTPException(409, "chỉ chạy tiếp được lượt mới nhất của hội thoại")
    counts = conn.execute("SELECT coalesce(sum(status='pending'),0) pending, coalesce(sum(status='approved'),0) approved "
                          "FROM approval_request WHERE tenant_id=? AND episode_id=?", (t.tenant_id, episode_id)).fetchone()
    if counts["pending"]:
        raise HTTPException(409, "lượt này còn lệnh chờ duyệt")
    if not counts["approved"]:
        raise HTTPException(409, "lượt này không có lệnh nào đã được duyệt để chạy tiếp")
    if _continued(conn, t.tenant_id, episode_id):
        raise HTTPException(409, "lượt này đã được chạy tiếp")
    return episode_id


def _own_conversation(conn, t: Tenant, conversation_id: str) -> dict:
    c = db.row(conn.execute("SELECT * FROM conversation WHERE id=? AND tenant_id=? AND user_id=? AND archived_at IS NULL",
                            (conversation_id, t.tenant_id, t.user_id)).fetchone())
    if c is None:
        raise HTTPException(404, "không tìm thấy hội thoại")
    return c


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
def chat(body: ChatBody, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Chạy một lượt và stream tiến trình bằng SSE. Lượt vẫn chạy tiếp và được ghi lại nếu client ngắt kết nối."""
    t.require_writer()
    if body.env not in gateway.ENVS:
        raise HTTPException(400, "môi trường không hợp lệ")
    message = body.message.strip()
    continuation_of = None
    if body.continue_episode:
        if not body.conversation_id:
            raise HTTPException(400, "thiếu hội thoại")
        _own_conversation(conn, t, body.conversation_id)
        continuation_of = _check_continuable(conn, t, body.conversation_id, body.continue_episode)
        message = agent_mod.CONTINUE_TASK
    if not message:
        raise HTTPException(400, "nội dung trống")
    if body.conversation_id:
        conversation_id = _own_conversation(conn, t, body.conversation_id)["id"]
    else:
        conversation_id = db.new_id()
        now = db.iso()
        title = " ".join(message.split())[:60]
        conn.execute("INSERT INTO conversation(id,tenant_id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                     (conversation_id, t.tenant_id, t.user_id, title, now, now))
    run_id = db.new_id()
    cancelled = threading.Event()
    with _runs_lock:
        _runs[run_id] = (t.tenant_id, t.user_id, cancelled, conversation_id)
    events: queue.Queue = queue.Queue()
    events.put({"type": "start", "run_id": run_id, "conversation_id": conversation_id})

    def worker() -> None:
        try:
            with db.session() as worker_conn:
                episode = agent_mod.run_task(worker_conn, t.tenant_id, t.user_id, message, body.env, body.project, conversation_id=conversation_id,
                                             episode_id=run_id, on_event=events.put, cancelled=cancelled, continuation_of=continuation_of)
                events.put({"type": "done", "conversation_id": conversation_id, "message": _chat_message(worker_conn, t, episode)})
        except agent_mod.QuotaExceeded as exc:
            events.put({"type": "error", "message": str(exc)})
        except Exception:
            log.exception("lỗi khi chạy lượt chat %s", run_id)
            events.put({"type": "error", "message": "lỗi máy chủ khi xử lý yêu cầu"})
        finally:
            with _runs_lock:
                _runs.pop(run_id, None)
            events.put(None)

    threading.Thread(target=worker, name=f"chat-{run_id[:8]}", daemon=True).start()

    def stream():
        while True:
            try:
                event = events.get(timeout=15)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if event is None:
                return
            yield _sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat/{run_id}/cancel")
def chat_cancel(run_id: str, t: Tenant = Depends(current_tenant)):
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None or run[0] != t.tenant_id or run[1] != t.user_id:
        raise HTTPException(404, "không có lượt đang chạy")
    run[2].set()
    return {"ok": True}


def _approval_views(conn, t: "Tenant", rows: list[dict]) -> list[dict]:
    self_ok = gateway.self_approve_enabled(conn, t.tenant_id)
    for a in rows:
        a["can_decide"] = gateway.can_decide(a, t.user_id, t.roles, t.principal.is_org_admin, self_ok)
        a["mine"] = a.get("requester_id", t.user_id) == t.user_id
    return rows


def _chat_message(conn, t: "Tenant", ep: dict) -> dict:
    tenant_id = t.tenant_id
    approvals = db.rows(conn.execute(
        "SELECT a.id, a.tool, a.env, a.args, a.dry_run, a.risk_tier, a.confidence, a.reasons, a.rollback_hint, a.status, a.required_approvals, a.decided_by, "
        "a.deny_reason, a.result, a.expires_at, a.requester_id FROM approval_request a WHERE a.episode_id=? AND a.tenant_id=? ORDER BY a.created_at, a.rowid",
        (ep["id"], tenant_id)).fetchall(), "args", "dry_run", "reasons", "decided_by", "result")
    approvals = _approval_views(conn, t, approvals)
    return {"id": ep["id"], "task": ep["input"]["task"], "output": ep["output"], "outcome": ep["outcome"], "outcome_src": ep["outcome_src"], "env": ep["env"],
            "task_type": ep["task_type"], "started_at": ep["started_at"], "tokens": ep["tokens"], "ku_used": len(ep["ku_used"]),
            "edited": ep["human_edit"] is not None, "cancelled": any(s["type"] == "cancelled" for s in ep["trace"]),
            "steps": [s for s in ep["trace"] if s["type"] == "tool_call"], "error": next((s["message"] for s in ep["trace"] if s["type"] == "error"), None),
            "approvals": approvals, "continuation": bool(ep["input"].get("continuation_of")), "continued": _continued(conn, tenant_id, ep["id"])}


@app.get("/api/conversations")
def conversations(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return db.rows(conn.execute("SELECT id, title, created_at, updated_at FROM conversation WHERE tenant_id=? AND user_id=? AND archived_at IS NULL "
                                "ORDER BY updated_at DESC, rowid DESC LIMIT 200", (t.tenant_id, t.user_id)).fetchall())


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    c = _own_conversation(conn, t, conversation_id)
    gateway.expire_approvals(conn, t.tenant_id)
    rs = conn.execute("SELECT * FROM episode WHERE conversation_id=? AND tenant_id=? ORDER BY started_at, rowid", (conversation_id, t.tenant_id)).fetchall()
    with _runs_lock:
        # Lượt vẫn chạy trên server sau khi người dùng tải lại trang; giao diện dùng cờ này để chờ câu trả lời.
        running = any(r[0] == t.tenant_id and r[1] == t.user_id and r[3] == conversation_id for r in _runs.values())
    return {**c, "running": running, "messages": [_chat_message(conn, t, db.row(r, "input", "trace", "ku_used", "human_edit")) for r in rs]}


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.patch("/api/conversations/{conversation_id}")
def conversation_rename(conversation_id: str, body: ConversationPatch, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    _own_conversation(conn, t, conversation_id)
    conn.execute("UPDATE conversation SET title=? WHERE id=?", (body.title.strip() or "Hội thoại", conversation_id))
    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}")
def conversation_archive(conversation_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Chỉ ẩn hội thoại; episode là nhật ký bất biến nên được giữ lại."""
    _own_conversation(conn, t, conversation_id)
    conn.execute("UPDATE conversation SET archived_at=? WHERE id=?", (db.iso(), conversation_id))
    return {"ok": True}


@app.get("/api/episodes")
def episodes(limit: int = 50, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    rs = conn.execute("SELECT e.id, e.task_type, e.env, e.started_at, e.input, e.outcome, e.outcome_src, e.tokens, e.processed_at, u.username AS user "
                      "FROM episode e LEFT JOIN app_user u ON u.id=e.user_id WHERE e.tenant_id=? ORDER BY e.started_at DESC, e.rowid DESC LIMIT ?",
                      (t.tenant_id, min(limit, 200))).fetchall()
    return db.rows(rs, "input")


@app.get("/api/episodes/{episode_id}")
def episode(episode_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    found = agent_mod.get_episode(conn, t.tenant_id, episode_id)
    if found is None:
        raise HTTPException(404, "không tìm thấy episode")
    found["approvals"] = db.rows(conn.execute("SELECT id, tool, status, deny_reason, result, decided_at FROM approval_request WHERE episode_id=? AND tenant_id=?",
                                              (episode_id, t.tenant_id)).fetchall(), "result")
    return found


class Feedback(BaseModel):
    outcome: str
    final_text: str | None = Field(default=None, max_length=20000)


@app.post("/api/episodes/{episode_id}/feedback")
def feedback(episode_id: str, body: Feedback, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    if body.outcome not in ("success", "failure", "partial"):
        raise HTTPException(400, "kết quả không hợp lệ")
    found = agent_mod.get_episode(conn, t.tenant_id, episode_id)
    if found is None:
        raise HTTPException(404, "không tìm thấy episode")
    edit = None
    if body.final_text is not None and body.final_text != (found["output"] or ""):
        diff = "\n".join(difflib.unified_diff((found["output"] or "").splitlines(), body.final_text.splitlines(), "agent", "human", lineterm=""))
        edit = json.dumps({"final": body.final_text, "diff": diff}, ensure_ascii=False)
    conn.execute("UPDATE episode SET outcome=?, outcome_src='user_feedback', human_edit=coalesce(?,human_edit), processed_at=NULL WHERE id=? AND tenant_id=?",
                 (body.outcome, edit, episode_id, t.tenant_id))
    return agent_mod.get_episode(conn, t.tenant_id, episode_id)


@app.get("/api/ku")
def ku_list(status: str | None = None, kind: str | None = None, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    sql = ("SELECT id,kind,scope,domain,title,content,applies_when,entities,confidence,helpful_cnt,harmful_cnt,status,valid_to,recorded_at,source_eps,source_trust,version "
           "FROM knowledge_unit WHERE tenant_id=? AND (scope NOT LIKE 'user:%' OR scope=?)")
    args: list = [t.tenant_id, f"user:{t.user_id}"]
    if status:
        sql += " AND status=?"
        args.append(status)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return db.rows(conn.execute(sql + " ORDER BY recorded_at DESC, id LIMIT 500", args).fetchall(), "entities", "source_eps")


def _visible_ku(conn, t, ku_id: str) -> dict:
    """Ký ức cá nhân chỉ chủ sở hữu thấy, kể cả với admin."""
    ku = knowledge.get(conn, t.tenant_id, ku_id)
    if ku is None or (ku["scope"].startswith("user:") and ku["scope"] != f"user:{t.user_id}"):
        raise HTTPException(404, "không tìm thấy KU")
    return ku


@app.get("/api/me/memories")
def memories(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    rs = conn.execute("SELECT id, kind, title, content, status, recorded_at, source_eps FROM knowledge_unit WHERE tenant_id=? AND scope=? "
                      "AND status IN ('candidate','active') ORDER BY recorded_at DESC", (t.tenant_id, f"user:{t.user_id}")).fetchall()
    return db.rows(rs, "source_eps")


@app.delete("/api/me/memories/{ku_id}")
def memory_delete(ku_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    ku = knowledge.get(conn, t.tenant_id, ku_id)
    if ku is None or ku["scope"] != f"user:{t.user_id}":
        raise HTTPException(404, "không tìm thấy ký ức")
    knowledge.update(conn, t.tenant_id, ku_id, {"status": "rejected", "valid_to": db.iso()}, "DEPRECATE", t.principal.username, "người dùng xóa ký ức của mình")
    return {"ok": True}


@app.get("/api/ku/{ku_id}")
def ku_detail(ku_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    ku = _visible_ku(conn, t, ku_id)
    ku.pop("embedding", None)
    ku["events"] = db.rows(conn.execute("SELECT id, op, actor, reason, at FROM ku_event WHERE ku_id=? AND tenant_id=? ORDER BY id DESC", (ku_id, t.tenant_id)).fetchall())
    ku["edges"] = db.rows(conn.execute("SELECT src, dst, rel FROM ku_edge WHERE tenant_id=? AND (src=? OR dst=?)", (t.tenant_id, ku_id, ku_id)).fetchall())
    return ku


class NewKU(BaseModel):
    scope: str = "tenant"
    kind: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2000)
    applies_when: str | None = Field(default=None, max_length=500)
    entities: list[str] = []
    domain: str = "general"


GRAPH_LIMIT = 200


@app.get("/api/knowledge/graph")
def knowledge_graph(q: str = "", entity: str = "", include_candidates: bool = False, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """L3 để tra cứu: KU nối với thực thể và với nhau. q dùng đúng truy xuất lai của agent; entity lọc quanh một thực thể."""
    statuses = ("active", "candidate") if include_candidates else ("active",)
    def visible(a: str = "") -> str:
        return f"{a}tenant_id=? AND {a}status IN ({','.join('?' * len(statuses))}) AND ({a}scope NOT LIKE 'user:%' OR {a}scope=?)"
    args = [t.tenant_id, *statuses, f"user:{t.user_id}"]
    hits: dict[str, float] = {}
    if q.strip():
        for ku in retrieve(conn, t.tenant_id, q.strip(), user_id=t.user_id, k=30):
            hits[ku["id"]] = ku["score"]
        ids = list(hits)
        rows = conn.execute(f"SELECT * FROM knowledge_unit WHERE {visible()} AND id IN ({','.join('?' * len(ids))})", (*args, *ids)).fetchall() if ids else []
    elif entity.strip():
        rows = conn.execute(f"SELECT k.* FROM knowledge_unit k, json_each(k.entities) e WHERE {visible('k.')} "
                            "AND e.value=? LIMIT ?", (*args, entity.strip(), GRAPH_LIMIT)).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM knowledge_unit WHERE {visible()} ORDER BY helpful_cnt DESC, recorded_at DESC LIMIT ?", (*args, GRAPH_LIMIT)).fetchall()
    kus = db.rows(rows, "entities", "source_eps")
    ids = [k["id"] for k in kus]
    edges = db.rows(conn.execute(f"SELECT src, dst, rel FROM ku_edge WHERE tenant_id=? AND src IN ({','.join('?' * len(ids))}) AND dst IN ({','.join('?' * len(ids))})",
                                 (t.tenant_id, *ids, *ids)).fetchall()) if ids else []
    counts = db.rows(conn.execute(f"SELECT e.value entity, count(*) n FROM knowledge_unit k, json_each(k.entities) e WHERE "
                                  f"{visible('k.')} "
                                  "GROUP BY e.value ORDER BY n DESC LIMIT 60", args).fetchall())
    nodes = [{k2: k[k2] for k2 in ("id", "kind", "status", "title", "content", "applies_when", "entities", "source_trust", "helpful_cnt", "harmful_cnt", "domain")}
             | ({"score": hits[k["id"]]} if k["id"] in hits else {}) for k in kus]
    if hits:
        nodes.sort(key=lambda n: -n.get("score", 0))
    return {"nodes": nodes, "edges": edges, "entities": counts}


class EntityMerge(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=50)
    target: str = Field(min_length=1, max_length=80)


@app.post("/api/knowledge/entities/merge")
def entity_merge(body: EntityMerge, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    try:
        return entities.merge(conn, t.tenant_id, body.sources, body.target, t.principal.username)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ku")
def ku_add(body: NewKU, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    if body.kind not in ("rule", "fact", "procedure", "lesson", "insight"):
        raise HTTPException(400, "loại KU không hợp lệ")
    if body.scope not in ("tenant", "user"):
        raise HTTPException(400, "phạm vi không hợp lệ")
    personal = body.scope == "user"
    return _public(knowledge.add(conn, t.tenant_id, kind=body.kind, title=body.title, content=body.content, applies_when=body.applies_when, entities=body.entities,
                                 domain=body.domain, source_eps=[], source_trust="user", actor=t.principal.username, reason="thêm thủ công",
                                 scope=f"user:{t.user_id}" if personal else "tenant", status="active" if personal else "candidate"))


def _public(ku: dict) -> dict:
    return {k: v for k, v in ku.items() if k != "embedding"}


class Review(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=500)


@app.post("/api/ku/{ku_id}/review")
def ku_review(ku_id: str, body: Review, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    ku = _visible_ku(conn, t, ku_id)
    if ku["status"] != "candidate":
        raise HTTPException(409, f"KU đang ở trạng thái {ku['status']}")
    if body.approve:
        return _public(knowledge.promote(conn, t.tenant_id, ku_id, t.principal.username, body.reason or "người duyệt chấp nhận"))
    return _public(knowledge.update(conn, t.tenant_id, ku_id, {"status": "rejected"}, "DEPRECATE", t.principal.username, body.reason or "người duyệt từ chối"))


class BatchReview(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    approve: bool
    reason: str | None = Field(default=None, max_length=500)


@app.post("/api/ku/review-batch")
def ku_review_batch(body: BatchReview, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Chấp nhận hoặc loại nhiều KU ứng viên một lần; KU không còn là ứng viên thì bỏ qua."""
    t.require_admin()
    done, skipped = [], []
    for ku_id in dict.fromkeys(body.ids):
        try:
            ku = _visible_ku(conn, t, ku_id)
        except HTTPException:
            skipped.append(ku_id)
            continue
        if ku["status"] != "candidate":
            skipped.append(ku_id)
            continue
        if body.approve:
            knowledge.promote(conn, t.tenant_id, ku_id, t.principal.username, body.reason or "người duyệt chấp nhận hàng loạt")
        else:
            knowledge.update(conn, t.tenant_id, ku_id, {"status": "rejected"}, "DEPRECATE", t.principal.username, body.reason or "người duyệt loại hàng loạt")
        done.append(ku_id)
    return {"done": len(done), "skipped": len(skipped)}


class Consolidate(BaseModel):
    kind: str | None = None


MAX_CONSOLIDATE = 300
CONSOLIDATE_BATCH = 40  # mỗi lần gọi LLM; lô lớn hơn làm request chậm (khoảng 60 giây cho 33 KU qua relay) và dễ chạm giới hạn output
# Job lưu trong bảng ku_consolidation để sống qua lần khởi động lại server: lô nào xong được ghi ngay,
# khởi động lại thì chạy tiếp các lô còn dở. Khóa này chỉ tuần tự hóa việc ghi kết quả của các lô chạy song song.
_consolidation_write = threading.Lock()
_consolidation_threads: set[str] = set()


def _batches(items: list[dict]) -> list[list[dict]]:
    """Chia theo loại rồi theo lô: KU trùng ý hầu như luôn cùng loại, và lô nhỏ thì mỗi lần gọi nhanh, chạy song song được."""
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_kind[it["kind"]].append(it)
    return [group[i:i + CONSOLIDATE_BATCH] for group in by_kind.values() if len(group) >= 2 for i in range(0, len(group), CONSOLIDATE_BATCH)]


def _job(conn, job_id: str) -> dict | None:
    return db.row(conn.execute("SELECT * FROM ku_consolidation WHERE id=?", (job_id,)).fetchone(),
                  "items", "batches", "done_batches", "groups", "drops", "errors")


def _record_batch(job_id: str, index: int, part: dict | None, error: str | None) -> None:
    with _consolidation_write, db.session() as conn:
        job = _job(conn, job_id)
        if job is None or index in job["done_batches"]:
            return
        scope = {i: it["scope"] for i, it in job["items"].items()}
        groups = job["groups"]
        if part:
            # Gom chỉ trong cùng phạm vi: bỏ id khác phạm vi với KU đầu nhóm.
            for g in part["groups"]:
                g["ids"] = [i for i in g["ids"] if scope.get(i) == scope.get(g["ids"][0])]
            groups += [g for g in part["groups"] if len(g["ids"]) >= 2]
        done = job["done_batches"] + [index]
        errors = job["errors"] + ([error] if error else [])
        status = "running" if len(done) < len(job["batches"]) else ("error" if len(errors) == len(job["batches"]) else "done")
        conn.execute("UPDATE ku_consolidation SET groups=?, drops=?, errors=?, done_batches=?, status=?, updated_at=? WHERE id=?",
                     (json.dumps(groups, ensure_ascii=False), json.dumps(job["drops"] + (part["drop"] if part else []), ensure_ascii=False),
                      json.dumps(errors, ensure_ascii=False), json.dumps(done), status, db.iso(), job_id))


def _run_consolidation(job_id: str, tenant_id: str) -> None:
    """Chạy các lô chưa xong của job (lần đầu, hoặc tiếp tục sau khi server khởi động lại)."""
    try:
        with db.session() as conn:
            job = _job(conn, job_id)
        if job is None:
            return
        todo = [(i, [{"id": x, **job["items"][x]} for x in ids if x in job["items"]]) for i, ids in enumerate(job["batches"]) if i not in job["done_batches"]]
        provider = llm.get_provider()

        def one(index: int, batch: list[dict]) -> None:
            with llm.collect_usage() as usage:
                try:
                    part, error = provider.consolidate(batch), None
                except llm.LLMError as exc:
                    part, error = None, str(exc)
                except Exception:
                    log.exception("lỗi khi tổng hợp KU")
                    part, error = None, "lỗi máy chủ khi tổng hợp một lô"
                finally:
                    if usage:
                        with db.session() as conn:
                            llm.save_usage(conn, tenant_id, None, usage)
            _record_batch(job_id, index, part, error)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="consolidate") as pool:
            list(pool.map(lambda job_batch: contextvars.copy_context().run(one, *job_batch), todo))
    finally:
        _consolidation_threads.discard(job_id)


def _start_consolidation(job_id: str, tenant_id: str) -> None:
    if job_id in _consolidation_threads:
        return
    _consolidation_threads.add(job_id)
    threading.Thread(target=_run_consolidation, args=(job_id, tenant_id), name=f"consolidate-{job_id[:8]}", daemon=True).start()


def resume_consolidations() -> None:
    """Gọi khi khởi động: job còn dở (server bị dừng giữa chừng) chạy tiếp các lô chưa xong; job quá 1 ngày thì bỏ."""
    with db.session() as conn:
        stale = db.iso(db.now() - timedelta(days=1))
        conn.execute("UPDATE ku_consolidation SET status='error', errors=json_insert(errors, '$[#]', 'quá hạn, hãy tổng hợp lại') "
                     "WHERE status='running' AND created_at<?", (stale,))
        rows = conn.execute("SELECT id, tenant_id FROM ku_consolidation WHERE status='running'").fetchall()
    for r in rows:
        log.info("chạy tiếp job tổng hợp KU %s sau khi khởi động lại", r["id"])
        _start_consolidation(r["id"], r["tenant_id"])


def _job_view(conn, job: dict, tenant_id: str) -> dict:
    """Bản trả về client. Bỏ KU đã được xử lý trong lúc đó để không gộp hay loại lại."""
    ids = list(job["items"])
    live = {r["id"] for r in conn.execute(f"SELECT id FROM knowledge_unit WHERE tenant_id=? AND status='candidate' AND id IN ({','.join('?' * len(ids))})",
                                          (tenant_id, *ids)).fetchall()} if ids else set()
    groups = [g for g in ({**g, "ids": [i for i in g["ids"] if i in live]} for g in job["groups"]) if len(g["ids"]) >= 2]
    drop = [d for d in job["drops"] if d["id"] in live]
    taken = {i for g in groups for i in g["ids"]} | {d["id"] for d in drop}
    # Ứng viên trong phạm vi đề xuất mà không trùng với mục nào và không bị đề xuất loại: người duyệt chấp nhận hàng loạt được.
    singles = [i for i in ids if i in live and i not in taken] if job["status"] == "done" else []
    outside = conn.execute("SELECT count(*) FROM knowledge_unit WHERE tenant_id=? AND status='candidate' AND scope NOT LIKE 'user:%'",
                           (tenant_id,)).fetchone()[0] - len(live)
    return {"id": job["id"], "status": job["status"], "considered": job["considered"], "total": len(job["batches"]), "done": len(job["done_batches"]),
            "errors": job["errors"], "groups": groups, "drop": drop, "singles": singles, "outside": outside, "kind": job.get("kind"),
            "items": {i: {k: it[k] for k in ("kind", "title", "content", "source_trust")} for i, it in job["items"].items()}}


@app.post("/api/ku/consolidate")
def ku_consolidate(body: Consolidate, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Bắt đầu đề xuất gom KU ứng viên trùng ý và loại KU ít giá trị, chạy nền theo lô. Chỉ đề xuất, chưa thay đổi gì."""
    t.require_admin()
    sql = "SELECT id, kind, title, content, applies_when, source_trust, scope FROM knowledge_unit WHERE tenant_id=? AND status='candidate' AND scope NOT LIKE 'user:%'"
    args: list = [t.tenant_id]
    if body.kind:
        sql += " AND kind=?"
        args.append(body.kind)
    items = db.rows(conn.execute(sql + " ORDER BY recorded_at DESC LIMIT ?", (*args, MAX_CONSOLIDATE)).fetchall())
    batches = [[it["id"] for it in b] for b in _batches(items)]
    job_id, now = db.new_id(), db.iso()
    conn.execute("DELETE FROM ku_consolidation WHERE tenant_id=?", (t.tenant_id,))  # mỗi tenant giữ một đề xuất gần nhất
    conn.execute("INSERT INTO ku_consolidation(id,tenant_id,status,kind,considered,items,batches,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (job_id, t.tenant_id, "running" if batches else "done", body.kind or None, len(items),
                  json.dumps({it["id"]: {k: it[k] for k in ("kind", "title", "content", "applies_when", "source_trust", "scope")} for it in items}, ensure_ascii=False),
                  json.dumps(batches), now, now))
    if batches:
        _start_consolidation(job_id, t.tenant_id)
    return _job_view(conn, _job(conn, job_id), t.tenant_id)


@app.get("/api/ku/consolidate/{job_id}")
def ku_consolidate_status(job_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    job = _job(conn, job_id)
    if job is None or job["tenant_id"] != t.tenant_id:
        raise HTTPException(404, "không có đề xuất này (có thể đã có lần tổng hợp mới hơn)")
    return _job_view(conn, job, t.tenant_id)


class Merge(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=50)
    kind: str
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=2000)
    applies_when: str | None = Field(default=None, max_length=500)
    activate: bool = False


@app.post("/api/ku/merge")
def ku_merge(body: Merge, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    for ku_id in body.ids:
        _visible_ku(conn, t, ku_id)
    try:
        ku = knowledge.merge(conn, t.tenant_id, body.ids, body.model_dump(), t.principal.username, body.activate)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return _public(ku)


@app.post("/api/ku/events/{event_id}/rollback")
def ku_rollback(event_id: int, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    ev = conn.execute("SELECT ku_id FROM ku_event WHERE id=? AND tenant_id=?", (event_id, t.tenant_id)).fetchone()
    if ev is not None:
        _visible_ku(conn, t, ev["ku_id"])
    try:
        return _public(knowledge.rollback(conn, t.tenant_id, event_id, t.principal.username))
    except KeyError as exc:
        raise HTTPException(404, "không tìm thấy sự kiện") from exc


@app.post("/api/learning/run")
def learning_run(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    return learning.run_learning(conn, t.tenant_id)


def _compile_error(exc: compiler.CompileError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@app.post("/api/compile")
def compile_now(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    try:
        return _run_view(compiler.compile_tenant(conn, t.tenant_id, t.principal.username))
    except compiler.CompileError as exc:
        raise _compile_error(exc) from exc


@app.get("/api/compile/runs")
def compile_runs(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    rows = db.rows(conn.execute("SELECT * FROM compile_run WHERE tenant_id=? AND status NOT IN ('stale','noop') ORDER BY created_at DESC, rowid DESC LIMIT 50",
                                (t.tenant_id,)).fetchall(), "changed")
    return [_run_view(r) for r in rows]


def _run_view(r: dict) -> dict:
    if r and isinstance(r.get("summary"), str):
        r["summary"] = json.loads(r["summary"].split("\n", 1)[0])  # dòng sau là dấu vân tay nội bộ
    return r


class RetireRule(BaseModel):
    ku_id: str


@app.post("/api/compile/retire-rule")
def compile_retire_rule(body: RetireRule, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    _visible_ku(conn, t, body.ku_id)
    try:
        return _run_view(compiler.retire_rule(conn, t.tenant_id, body.ku_id, t.principal.username))
    except compiler.CompileError as exc:
        raise _compile_error(exc) from exc


class Decide(BaseModel):
    approve: bool


@app.post("/api/compile/{run_id}/decide")
def compile_decide(run_id: str, body: Decide, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    try:
        return _run_view(compiler.decide(conn, t.tenant_id, run_id, body.approve, t.principal.username))
    except compiler.CompileError as exc:
        raise _compile_error(exc) from exc


@app.post("/api/compile/rollback")
def compile_rollback(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    try:
        return compiler.rollback(conn, t.tenant_id, t.principal.username)
    except compiler.CompileError as exc:
        raise _compile_error(exc) from exc


@app.get("/api/knowledge/files")
def knowledge_files(t: Tenant = Depends(current_tenant)):
    return sorted(compiler.read_published(t.tenant_id))


@app.get("/api/knowledge/file")
def knowledge_file(path: str, t: Tenant = Depends(current_tenant)):
    try:
        return {"path": path, "content": compiler.read_file(t.tenant_id, path)}
    except compiler.CompileError as exc:
        raise _compile_error(exc) from exc


class Golden(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    expect_domain: str
    expect_terms: list[str] = []


@app.get("/api/golden")
def golden_list(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return db.rows(conn.execute("SELECT * FROM golden_task WHERE tenant_id=?", (t.tenant_id,)).fetchall(), "expect_terms")


@app.post("/api/golden")
def golden_add(body: Golden, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    gid = db.new_id()
    conn.execute("INSERT INTO golden_task(id,tenant_id,query,expect_domain,expect_terms) VALUES (?,?,?,?,?)",
                 (gid, t.tenant_id, body.query, body.expect_domain, json.dumps(body.expect_terms, ensure_ascii=False)))
    return {"id": gid}


@app.get("/api/approvals")
def approvals(status: str | None = None, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    gateway.expire_approvals(conn, t.tenant_id)
    sql = "SELECT a.*, u.username AS requester FROM approval_request a LEFT JOIN app_user u ON u.id=a.requester_id WHERE a.tenant_id=?"
    args: list = [t.tenant_id]
    if status:
        sql += " AND a.status=?"
        args.append(status)
    rows = db.rows(conn.execute(sql + " ORDER BY a.created_at DESC, a.rowid DESC LIMIT 100", args).fetchall(), "args", "dry_run", "reasons", "decided_by", "result")
    return _approval_views(conn, t, rows)


class ApprovalDecision(BaseModel):
    action: str
    reason: str | None = Field(default=None, max_length=1000)


@app.post("/api/approvals/{approval_id}/decide")
def approval_decide(approval_id: str, body: ApprovalDecision, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    try:
        return gateway.decide(conn, t.tenant_id, approval_id, t.user_id, t.roles, t.principal.is_org_admin, body.action, body.reason)
    except gateway.ApprovalError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _tool_view(conn, row) -> dict:
    d = db.row(row, "input_schema", "envs", "roles")
    spec = tools.spec_of(row)
    c = connections.get(conn, row["tenant_id"], row["connection_id"]) if row["connection_id"] else None
    eff = tools.effective(row)
    d.update(effective_envs=eff["envs"], effective_roles=eff["roles"], catalog_envs=c["envs"] if c else [],
             role_choices=list(tools.ROLE_CHOICES), personal=bool(row["owner_user"]),
             tier_choices=list(tools.TIERS[tools.TIERS.index(d["catalog_tier"]):]) if d["catalog_tier"] in tools.TIERS else [],
             classified=spec.classified if spec else False, warnings=spec.warnings if spec else [], rollback_hint=spec.rollback_hint if spec else "",
             server_description=f"{c['title']} · {tools.TEMPLATES[c['template']].title if c['template'] in tools.TEMPLATES else c['template']}" if c else "",
             is_new=d["approved_by"] is None)
    return d


def _tool_row(conn, t: Tenant, tool: str):
    row = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (t.tenant_id, tool)).fetchone()
    if row is None or (row["owner_user"] and row["owner_user"] != t.user_id):
        raise HTTPException(404, "không tìm thấy công cụ")
    return row


@app.get("/api/tools")
def tool_list(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Công cụ của tenant và công cụ cá nhân của chính mình; công cụ đã gỡ không hiện."""
    rows = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND status='active' AND (owner_user IS NULL OR owner_user=?) ORDER BY server, tool",
                        (t.tenant_id, t.user_id)).fetchall()
    return [_tool_view(conn, r) for r in rows]


class ToolPatch(BaseModel):
    enabled: bool | None = None
    risk_tier: str | None = None
    envs: list[str] | None = None
    roles: list[str] | None = None


@app.patch("/api/tools/{tool}")
def tool_patch(tool: str, body: ToolPatch, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Công cụ dùng chung: chỉ tenant_admin. Công cụ cá nhân: chính chủ. Chỉ được siết so với template và kết nối."""
    row = _tool_row(conn, t, tool)
    if not row["owner_user"]:
        t.require_admin()
    spec = tools.spec_of(row)
    c = connections.get(conn, t.tenant_id, row["connection_id"]) if row["connection_id"] else None
    if spec is None or c is None or row["status"] != "active":
        raise HTTPException(409, "công cụ đã bị gỡ hoặc kết nối không còn hoạt động")
    changes: dict = {}
    if body.risk_tier is not None:
        if body.risk_tier not in tools.TIERS:
            raise HTTPException(400, "mức rủi ro không hợp lệ")
        if tools.TIERS.index(body.risk_tier) < tools.TIERS.index(spec.risk_tier):
            raise HTTPException(403, f"không được thấp hơn mức {spec.risk_tier} của template")
        changes.update(risk_tier=body.risk_tier, risk_floor=None if body.risk_tier == spec.risk_tier else body.risk_tier)
    if body.envs is not None:
        if not body.envs or set(body.envs) - set(c["envs"]):
            raise HTTPException(400, f"môi trường phải là tập con không rỗng của môi trường của kết nối {c['envs']}")
        changes["envs"] = json.dumps([e for e in tools.ENVS if e in body.envs])
    if body.roles is not None:
        if not body.roles or set(body.roles) - set(tools.ROLE_CHOICES):
            raise HTTPException(400, f"vai trò phải là tập con không rỗng của {list(tools.ROLE_CHOICES)}")
        changes["roles"] = json.dumps([r for r in tools.ROLE_CHOICES if r in body.roles])
    if body.enabled is not None:
        changes["enabled"] = int(body.enabled)
    if changes:
        changes["approved_by"] = t.principal.username
        cols = ", ".join(f"{k}=?" for k in changes)
        conn.execute(f"UPDATE tool_registry SET {cols} WHERE tenant_id=? AND tool=?", (*changes.values(), t.tenant_id, tool))
        log.info("tenant %s: %s đổi cấu hình công cụ %s: %s", t.tenant_id, t.principal.username, tool, sorted(changes))
    return _tool_view(conn, conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (t.tenant_id, tool)).fetchone())


@app.get("/api/templates")
def template_list(_: Principal = Depends(current_user)):
    return [{"name": tpl.name, "title": tpl.title, "description": tpl.description,
             "fields": [{"name": f.name, "type": f.type, "required": f.required, "default": f.default, "description": f.description} for f in tpl.fields],
             "tools": [{"name": s.name, "description": s.description, "action_class": s.action_class, "risk_tier": s.risk_tier,
                        "untrusted_output": s.untrusted_output} for s in tpl.tools.values()]} for tpl in tools.TEMPLATES.values()]


def _connection_error(exc: "connections.ConnectionError_") -> HTTPException:
    return HTTPException(exc.status, str(exc))


def _connection_view(conn, t: Tenant, row: dict) -> dict:
    tool_rows = conn.execute("SELECT tool, enabled, action_class, risk_tier FROM tool_registry WHERE tenant_id=? AND connection_id=? AND status='active' ORDER BY tool",
                             (t.tenant_id, row["id"])).fetchall()
    return {**connections.view(row), "can_manage": connections.can_manage(row, t.user_id, t.is_admin), "tools": db.rows(tool_rows)}


class ConnectionBody(BaseModel):
    template: str
    name: str = Field(max_length=40)
    title: str = Field(default="", max_length=120)
    scope: str = "user"
    envs: list[str] = ["dev"]
    roles: list[str] = ["member"]
    params: dict = {}
    secrets: dict = {}
    allow_private: bool = False


@app.get("/api/connections")
def connection_list(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return [_connection_view(conn, t, r) for r in connections.visible(conn, t.tenant_id, t.user_id)]


@app.post("/api/connections")
def connection_create(body: ConnectionBody, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    try:
        with db.tx(conn):
            row = connections.create(conn, t.tenant_id, t.user_id, is_admin=t.is_admin, is_org_admin=t.principal.is_org_admin, template=body.template,
                                     name=body.name, title=body.title, scope=body.scope, envs=body.envs, roles=body.roles, params=body.params,
                                     secrets=body.secrets, allow_private=body.allow_private)
    except connections.ConnectionError_ as exc:
        raise _connection_error(exc) from exc
    return _connection_view(conn, t, row)


class ConnectionPatch(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    envs: list[str] | None = None
    roles: list[str] | None = None
    params: dict | None = None
    secrets: dict | None = None
    allow_private: bool | None = None
    status: str | None = None


@app.patch("/api/connections/{connection_id}")
def connection_patch(connection_id: str, body: ConnectionPatch, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        with db.tx(conn):
            row = connections.update(conn, t.tenant_id, connection_id, t.user_id, is_admin=t.is_admin, is_org_admin=t.principal.is_org_admin, changes=changes)
    except connections.ConnectionError_ as exc:
        raise _connection_error(exc) from exc
    return _connection_view(conn, t, row)


@app.delete("/api/connections/{connection_id}")
def connection_delete(connection_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Gỡ kết nối: công cụ của nó bị chặn ngay; audit và lịch sử được giữ. ServiceAccount kagent đã tạo trong cụm được gỡ theo."""
    row = connections.get(conn, t.tenant_id, connection_id)
    warning = None
    if row and row["template"] == "k8s" and row["params"].get("_rbac") and connections.can_manage(row, t.user_id, t.is_admin):
        try:
            k8s_rbac.remove(connections.params_of(row), row["id"])
        except (ConnectorError, net.NetError) as exc:
            warning = (f"không gỡ được ServiceAccount và quyền kagent đã tạo trong cụm ({exc}); "
                       f"hãy xóa tay các object có nhãn {k8s_rbac.LABEL_CONN}={row['id']}")
    try:
        with db.tx(conn):
            connections.update(conn, t.tenant_id, connection_id, t.user_id, is_admin=t.is_admin, is_org_admin=t.principal.is_org_admin,
                               changes={"status": "removed"})
    except connections.ConnectionError_ as exc:
        raise _connection_error(exc) from exc
    return {"ok": True, **({"warning": warning} if warning else {})}


@app.post("/api/connections/{connection_id}/test")
def connection_test(connection_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    try:
        return connections.test(conn, t.tenant_id, connection_id, t.user_id)
    except connections.ConnectionError_ as exc:
        raise _connection_error(exc) from exc


def _k8s_connection(conn, t: Tenant, connection_id: str, manage: bool = True) -> dict:
    row = connections.get(conn, t.tenant_id, connection_id)
    if row is None or row["status"] == "removed" or (row["scope"] == "user" and row["owner_user"] != t.user_id):
        raise HTTPException(404, "không tìm thấy kết nối")
    if row["template"] != "k8s":
        raise HTTPException(400, "chỉ kết nối Kubernetes mới có quyền Kubernetes")
    if manage:
        t.require_writer()
        if not connections.can_manage(row, t.user_id, t.is_admin):
            raise HTTPException(403, "chỉ người quản lý kết nối được đổi quyền Kubernetes")
    return row


def _cluster_call(fn):
    try:
        return fn()
    except net.NetError as exc:
        raise HTTPException(400, f"kết nối bị chặn: {exc}") from exc
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/connections/{connection_id}/k8s-rbac")
def k8s_rbac_get(connection_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    row = _k8s_connection(conn, t, connection_id, manage=False)
    state = row["params"].get("_rbac") or {}
    secrets = connections.params_of(row)
    return {"config": state.get("config") or k8s_rbac.default_config(row["params"].get("namespace") or "default"),
            "applied": bool(state.get("config")), "applied_at": state.get("applied_at"), "applied_by": state.get("applied_by"),
            "base_name": k8s_rbac.base_name(row["id"], row["name"]), "label": f"{k8s_rbac.LABEL_CONN}={row['id']}",
            "identities": {p: bool(secrets.get(k8s_rbac.TOKEN_KEY[p])) for p in k8s_rbac.PROFILES},
            "can_manage": connections.can_manage(row, t.user_id, t.is_admin),
            "limits": {"verbs": {p: list(v) if v else None for p, v in k8s_rbac.VERBS.items()}, "sensitive": list(k8s_rbac.SENSITIVE),
                       "tools": k8s_rbac.TOOL}}


class RbacBody(BaseModel):
    config: dict


@app.post("/api/connections/{connection_id}/k8s-rbac/plan")
def k8s_rbac_plan(connection_id: str, body: RbacBody, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    row = _k8s_connection(conn, t, connection_id)
    return _cluster_call(lambda: k8s_rbac.plan(connections.params_of(row), row["id"], row["name"], body.config))


@app.post("/api/connections/{connection_id}/k8s-rbac/apply")
def k8s_rbac_apply(connection_id: str, body: RbacBody, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    row = _k8s_connection(conn, t, connection_id)
    if row["status"] != "active":
        raise HTTPException(409, "kết nối đang tạm tắt")
    result, tokens = _cluster_call(lambda: k8s_rbac.apply(connections.params_of(row), row["id"], row["name"], body.config))
    with db.tx(conn):
        connections.set_internal(conn, row, params={"_rbac": {"config": result["config"], "applied_at": db.iso(), "applied_by": t.user_id}},
                                 secrets={k8s_rbac.TOKEN_KEY[p]: tokens[p] for p in k8s_rbac.PROFILES})
    log.info("tenant %s: %s áp dụng quyền Kubernetes cho kết nối %s (%d thay đổi)", t.tenant_id, t.user_id, row["name"], result["changes"])
    return result


@app.delete("/api/connections/{connection_id}/k8s-rbac")
def k8s_rbac_remove(connection_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    row = _k8s_connection(conn, t, connection_id)
    removed = _cluster_call(lambda: k8s_rbac.remove(connections.params_of(row), row["id"]))
    with db.tx(conn):
        connections.set_internal(conn, row, params={"_rbac": None}, secrets={k8s_rbac.TOKEN_KEY[p]: None for p in k8s_rbac.PROFILES})
    log.info("tenant %s: %s gỡ quyền Kubernetes của kết nối %s (%d object)", t.tenant_id, t.user_id, row["name"], removed)
    return {"ok": True, "removed": removed}


def _incident_call(fn):
    try:
        return fn()
    except incidents.IncidentError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.get("/api/incidents")
def incident_list(status: str = "open", t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    if status not in ("open", "resolved", "closed", "all"):
        raise HTTPException(400, "status phải là open, resolved, closed hoặc all")
    cfg = incidents.settings(conn, t.tenant_id)
    return {"items": incidents.list_incidents(conn, t.tenant_id, status), "enabled": cfg["enabled"], "source_names": cfg["sources"],
            **incidents.source_status(t.tenant_id),
            "counts": {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) n FROM incident WHERE tenant_id=? GROUP BY status", (t.tenant_id,))},
            "auto_today": conn.execute("SELECT count(*) n FROM incident_event WHERE tenant_id=? AND kind='action' AND text LIKE '%: đã tự chạy%' AND at>=?",
                                       (t.tenant_id, db.now().date().isoformat())).fetchone()["n"]}


@app.get("/api/incidents/{incident_id}")
def incident_get(incident_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return _incident_call(lambda: incidents.get(conn, t.tenant_id, incident_id))


class IncidentMerge(BaseModel):
    into: str


class IncidentSplit(BaseModel):
    alert_id: str


class IncidentClose(BaseModel):
    note: str = Field(default="", max_length=500)


@app.post("/api/incidents/{incident_id}/merge")
def incident_merge(incident_id: str, body: IncidentMerge, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    with db.tx(conn):
        _incident_call(lambda: incidents.merge(conn, t.tenant_id, incident_id, body.into, t.user_id))
    return incidents.get(conn, t.tenant_id, body.into)


@app.post("/api/incidents/{incident_id}/split")
def incident_split(incident_id: str, body: IncidentSplit, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    with db.tx(conn):
        new_id = _incident_call(lambda: incidents.split(conn, t.tenant_id, incident_id, body.alert_id, t.user_id))
    return {"id": new_id}


@app.post("/api/incidents/{incident_id}/close")
def incident_close(incident_id: str, body: IncidentClose, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    with db.tx(conn):
        _incident_call(lambda: incidents.close(conn, t.tenant_id, incident_id, t.user_id, body.note))
    return {"ok": True}


@app.post("/api/incidents/{incident_id}/investigate")
def incident_investigate(incident_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    with db.tx(conn):
        _incident_call(lambda: incidents.reinvestigate(conn, t.tenant_id, incident_id))
    incidents.start_investigation(t.tenant_id, incident_id)
    return {"ok": True}


@app.get("/api/alert-settings")
def alert_settings_get(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    available = [{"name": r["name"], "template": r["template"], "envs": json.loads(r["envs"])} for r in conn.execute(
        "SELECT name, template, envs FROM connection WHERE tenant_id=? AND status='active' AND scope='tenant' AND template IN (%s) ORDER BY name"
        % ",".join("?" * len(incidents.SOURCE_TEMPLATES)), (t.tenant_id, *incidents.SOURCE_TEMPLATES)).fetchall()]
    return {"settings": incidents.settings(conn, t.tenant_id), "defaults": incidents.DEFAULTS, "available_sources": available,
            "can_manage": t.is_admin, **incidents.source_status(t.tenant_id)}


@app.put("/api/alert-settings")
def alert_settings_put(body: dict, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    with db.tx(conn):
        cfg = _incident_call(lambda: incidents.save_settings(conn, t.tenant_id, body))
        if cfg["enabled"]:
            incidents.bot_user(conn, t.tenant_id)
    log.info("tenant %s: %s đổi cài đặt luồng cảnh báo (bật=%s, nguồn=%s)", t.tenant_id, t.user_id, cfg["enabled"], cfg["sources"])
    return cfg


@app.post("/api/alert-settings/poll")
def alert_poll_now(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    result = incidents.poll(conn, t.tenant_id)
    incidents.follow_up(conn, t.tenant_id)
    return result


def _learned_view(conn, t: Tenant, row: dict) -> dict:
    return {**row, "risk_tier": learned.risk(conn, t.tenant_id, row["steps"]), "flow": learned.flow(row), "personal": row["scope"].startswith("user:"),
            "success_rate": round(row["successes"] / row["uses"], 2) if row["uses"] else None}


@app.get("/api/learned-tools")
def learned_list(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    """Công cụ tenant (admin thấy cả đề xuất) và công cụ cá nhân của chính mình."""
    out = []
    for row in learned.visible_rows(conn, t.tenant_id, t.user_id):
        if row["scope"] == "tenant" and not t.is_admin and row["status"] not in ("active", "disabled"):
            continue
        out.append(_learned_view(conn, t, row))
    return out


class FromEpisode(BaseModel):
    episode_id: str
    name: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    scope: str = "user"


@app.post("/api/learned-tools/from-episode")
def learned_from_episode(body: FromEpisode, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_writer()
    if body.scope not in ("user", "tenant"):
        raise HTTPException(400, "phạm vi không hợp lệ")
    try:
        row = learned.from_episode(conn, t.tenant_id, t.user_id, body.episode_id, body.name, body.description, body.scope)
    except learned.LearnedError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return _learned_view(conn, t, row)


class LearnedPatch(BaseModel):
    status: str | None = None
    description: str | None = Field(default=None, max_length=300)
    steps: list | None = None
    params: dict | None = None


@app.patch("/api/learned-tools/{tool_id}")
def learned_patch(tool_id: str, body: LearnedPatch, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        row = learned.update(conn, t.tenant_id, tool_id, t.user_id, t.is_admin, changes)
    except learned.LearnedError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    log.info("tenant %s: %s cập nhật công cụ học được %s: %s", t.tenant_id, t.principal.username, row["name"], sorted(changes))
    return _learned_view(conn, t, row)


@app.get("/api/policy")
def policy_get(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    r = conn.execute("SELECT policy, auto_promote, change_freeze, self_approve FROM tenant WHERE id=?", (t.tenant_id,)).fetchone()
    overlay = json.loads(r["policy"])
    return {"overlay": overlay, "effective": gateway.merge_policy(overlay), "baseline": gateway.BASELINE,
            "auto_promote": bool(r["auto_promote"]), "change_freeze": bool(r["change_freeze"]), "self_approve": bool(r["self_approve"]),
            "grant_policy": gateway.grant_policy(conn, t.tenant_id), "grant_ttl_minutes": settings.grant_ttl_minutes,
            "rule_autopublish": compiler.autopublish_policy(conn, t.tenant_id)}


class PolicyBody(BaseModel):
    overlay: dict


@app.put("/api/policy")
def policy_put(body: PolicyBody, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    try:
        gateway.merge_policy(body.overlay)
    except (gateway.PolicyError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(400, f"chính sách không hợp lệ: {exc}") from exc
    conn.execute("UPDATE tenant SET policy=? WHERE id=?", (json.dumps(body.overlay, ensure_ascii=False), t.tenant_id))
    return policy_get(t, conn)


class TenantSettings(BaseModel):
    auto_promote: bool | None = None
    change_freeze: bool | None = None
    self_approve: bool | None = None
    grant_policy: dict | None = None
    rule_autopublish: dict | None = None


@app.patch("/api/tenant")
def tenant_patch(body: TenantSettings, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    if body.auto_promote is not None:
        conn.execute("UPDATE tenant SET auto_promote=? WHERE id=?", (int(body.auto_promote), t.tenant_id))
    if body.change_freeze is not None:
        conn.execute("UPDATE tenant SET change_freeze=? WHERE id=?", (int(body.change_freeze), t.tenant_id))
    if body.self_approve is not None:
        conn.execute("UPDATE tenant SET self_approve=? WHERE id=?", (int(body.self_approve), t.tenant_id))
        log.info("tenant %s: %s đặt self_approve=%s", t.tenant_id, t.principal.username, body.self_approve)
    if body.rule_autopublish is not None:
        try:
            merged = compiler.check_autopublish(body.rule_autopublish)
        except ValueError as exc:
            raise HTTPException(400, f"cấu hình tự publish quy ước không hợp lệ: {exc}") from exc
        conn.execute("UPDATE tenant SET rule_autopublish=? WHERE id=?", (json.dumps(merged), t.tenant_id))
        log.info("tenant %s: %s đặt rule_autopublish=%s", t.tenant_id, t.principal.username, merged)
    if body.grant_policy is not None:
        try:
            merged = gateway.check_grant_policy(body.grant_policy)
        except gateway.PolicyError as exc:
            raise HTTPException(400, f"chính sách grant không hợp lệ: {exc}") from exc
        conn.execute("UPDATE tenant SET grant_policy=? WHERE id=?", (json.dumps(merged), t.tenant_id))
        log.info("tenant %s: %s đặt grant_policy=%s", t.tenant_id, t.principal.username, merged)
    return policy_get(t, conn)


class Simulate(BaseModel):
    tool: str
    env: str
    confidence: float = Field(ge=0, le=1)


@app.post("/api/policy/simulate")
def policy_simulate(body: Simulate, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    meta = conn.execute("SELECT risk_tier, canonical FROM tool_registry WHERE tenant_id=? AND tool=?", (t.tenant_id, body.tool)).fetchone()
    if meta is None or body.env not in gateway.ENVS:
        raise HTTPException(400, "công cụ hoặc môi trường không hợp lệ")
    decision = gateway.evaluate(gateway.get_policy(conn, t.tenant_id), meta["risk_tier"], body.tool, body.env, body.confidence, [], meta["canonical"])
    return {"risk_tier": meta["risk_tier"], "outcome": decision.outcome, "required": decision.required, "reasons": decision.reasons}


@app.get("/api/audit")
def audit(limit: int = 100, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    rs = conn.execute("SELECT a.*, u.username AS user FROM tool_call_audit a LEFT JOIN app_user u ON u.id=a.user_id WHERE a.tenant_id=? ORDER BY a.id DESC LIMIT ?",
                      (t.tenant_id, min(limit, 500))).fetchall()
    return db.rows(rs, "args", "reasons")


@app.get("/api/metrics")
def metrics(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    tid = t.tenant_id
    e = conn.execute("SELECT count(*) n, coalesce(sum(outcome!='unknown'),0) labeled, coalesce(sum(human_edit IS NOT NULL),0) edited, coalesce(sum(outcome='failure'),0) failed, "
                     "coalesce(avg(tokens),0) tokens, coalesce(sum(processed_at IS NOT NULL),0) processed FROM episode WHERE tenant_id=?", (tid,)).fetchone()
    status = {r["status"]: r["c"] for r in conn.execute("SELECT status, count(*) c FROM knowledge_unit WHERE tenant_id=? GROUP BY status", (tid,)).fetchall()}
    decisions = {r["decision"]: r["c"] for r in conn.execute("SELECT decision, count(*) c FROM tool_call_audit WHERE tenant_id=? GROUP BY decision", (tid,)).fetchall()}
    weekly = db.rows(conn.execute("SELECT strftime('%Y-W%W', started_at) week, count(*) tasks, sum(human_edit IS NOT NULL) edited, sum(outcome='failure') failed, avg(tokens) tokens "
                                  "FROM episode WHERE tenant_id=? GROUP BY week ORDER BY week DESC LIMIT 8", (tid,)).fetchall())
    decided = status.get("active", 0) + status.get("rejected", 0)
    n = e["n"] or 1
    return {**_base_metrics(e, n, status, decided, decisions, weekly), "tokens": token_metrics(conn, tid)}


def token_metrics(conn, tid: str, days: int = 7) -> dict:
    """Token theo mục đích, tỷ lệ cache, công cụ trả kết quả lớn nhất và các lượt tốn nhất trong N ngày."""
    since = db.iso(db.now() - timedelta(days=days))
    by_purpose = db.rows(conn.execute(
        "SELECT purpose, count(*) calls, sum(input_tokens) input, sum(output_tokens) output, sum(cache_read) cache_read, sum(cache_write) cache_write "
        "FROM llm_call WHERE tenant_id=? AND at>=? GROUP BY purpose ORDER BY sum(input_tokens+cache_read+cache_write+output_tokens) DESC", (tid, since)).fetchall())
    total_in = sum(r["input"] + r["cache_read"] + r["cache_write"] for r in by_purpose)
    cached = sum(r["cache_read"] for r in by_purpose)
    tools: dict[str, dict] = {}
    for r in conn.execute("SELECT trace FROM episode WHERE tenant_id=? AND started_at>=?", (tid, since)).fetchall():
        for step in json.loads(r["trace"]):
            if step.get("type") == "tool_call":
                t = tools.setdefault(step["tool"], {"tool": step["tool"], "calls": 0, "result_chars": 0, "max_chars": 0})
                size = step.get("result_chars") or len(json.dumps(step.get("result"), ensure_ascii=False))
                t["calls"] += 1
                t["result_chars"] += size
                t["max_chars"] = max(t["max_chars"], size)
    top_turns = db.rows(conn.execute(
        "SELECT e.id, e.started_at, substr(json_extract(e.input, '$.task'), 1, 80) task, count(c.id) llm_calls, sum(c.input_tokens) input, "
        "sum(c.cache_read) cache_read, sum(c.cache_write) cache_write, sum(c.output_tokens) output "
        "FROM episode e JOIN llm_call c ON c.episode_id=e.id AND c.purpose!='reflect' AND c.purpose!='curate' AND c.purpose!='judge' "
        "WHERE e.tenant_id=? AND e.started_at>=? GROUP BY e.id ORDER BY sum(c.input_tokens+c.cache_read+c.cache_write+c.output_tokens) DESC LIMIT 10",
        (tid, since)).fetchall())
    return {"days": days, "by_purpose": by_purpose, "cache_hit_pct": round(100 * cached / total_in, 1) if total_in else None,
            "tools": sorted(tools.values(), key=lambda t: -t["result_chars"])[:15], "top_turns": top_turns, **llm_usage(conn, tid)}


def llm_usage(conn, tid: str, days: int = 14) -> dict:
    """Token theo từng LLM (cấu hình · model): tổng và theo ngày, để vẽ biểu đồ."""
    since = db.iso(db.now() - timedelta(days=days - 1)).split("T")[0]
    label = "CASE WHEN llm IS NULL OR llm='' THEN coalesce(nullif(model,''),'(không rõ)') ELSE llm || ' · ' || coalesce(nullif(model,''),'?') END"
    total = "input_tokens+cache_read+cache_write+output_tokens"
    by_llm = db.rows(conn.execute(
        f"SELECT {label} label, count(*) calls, sum(input_tokens) input, sum(cache_read) cache_read, sum(cache_write) cache_write, "
        f"sum(output_tokens) output, sum({total}) total, min(at) first_at FROM llm_call WHERE tenant_id=? AND substr(at,1,10)>=? "
        f"GROUP BY label ORDER BY total DESC", (tid, since)).fetchall())
    daily = db.rows(conn.execute(
        f"SELECT substr(at,1,10) day, {label} label, sum({total}) total, count(*) calls FROM llm_call WHERE tenant_id=? AND substr(at,1,10)>=? "
        f"GROUP BY day, label ORDER BY day", (tid, since)).fetchall())
    day_list = [db.iso(db.now() - timedelta(days=d)).split("T")[0] for d in range(days - 1, -1, -1)]
    return {"llm_days": day_list, "by_llm": by_llm, "llm_daily": daily}


def _base_metrics(e, n, status, decided, decisions, weekly) -> dict:
    return {"episodes": e["n"], "labeled_pct": round(100 * e["labeled"] / n, 1), "edited_pct": round(100 * e["edited"] / n, 1), "failed_pct": round(100 * e["failed"] / n, 1),
            "avg_tokens": round(e["tokens"], 1), "processed": e["processed"], "ku_status": status, "ku_accept_pct": round(100 * status.get("active", 0) / decided, 1) if decided else None,
            "tool_decisions": decisions, "weekly": weekly}


@app.exception_handler(ValueError)
def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


class _RevalidatedStatic(StaticFiles):
    """Không có Cache-Control thì trình duyệt tự cache theo phỏng đoán và chạy JS cũ sau khi nâng cấp; no-cache vẫn nhận 304 nhờ ETag."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatedStatic(directory=STATIC_DIR), name="static")
