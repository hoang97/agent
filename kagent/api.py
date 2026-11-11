"""HTTP API và phục vụ giao diện. Mọi truy vấn đều đi kèm tenant_id đã kiểm tra quyền thành viên."""
from __future__ import annotations

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
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent as agent_mod
from . import compiler, connections, db, gateway, knowledge, learned, learning, llm, tools, vault
from .config import settings
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
            "deny": [{"tool": "k8s.exec", "env": "prod"}, {"tool": "*.delete*", "env": "prod"}, {"tool": "k8s.get_secret_value", "env": "*"}],
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
        if creds:
            path = settings.data_dir / "initial-credentials.txt"
            path.write_text("\n".join(f"{k}: {v}" for k, v in creds.items()) + "\n", encoding="utf-8")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            log.warning("Đã sinh mật khẩu ban đầu tại %s", path)


@asynccontextmanager
async def lifespan(_: FastAPI):
    seed()
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


def current_user(request: Request, conn=Depends(get_conn)) -> Principal:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "chưa đăng nhập")
    r = conn.execute("SELECT u.id, u.username, u.is_org_admin FROM session s JOIN app_user u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                     (_token_hash(token), db.iso())).fetchone()
    if r is None:
        raise HTTPException(401, "phiên đã hết hạn")
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
    conn.execute("INSERT INTO session(token_hash,user_id,expires_at) VALUES (?,?,?)", (_token_hash(token), r["id"], db.later(hours=settings.session_hours)))
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=settings.cookie_secure, max_age=settings.session_hours * 3600, path="/")
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


class LLMKey(BaseModel):
    # Kiểm tra độ dài trong handler: lỗi 422 của pydantic sẽ trả lại nguyên giá trị đầu vào.
    key: str
    base_url: str | None = None
    auth_type: str = "api_key"
    provider: str = "anthropic"
    model_strong: str | None = None
    model_fast: str | None = None


_MODEL_NAME = re.compile(r"[\w.:/-]{1,100}")


def _clean_model(value: str | None) -> str | None:
    value = (value or "").strip()
    if value and not _MODEL_NAME.fullmatch(value):
        raise HTTPException(400, "tên model không hợp lệ")
    return value or None


@app.get("/api/settings/llm-key")
def llm_key_status(_: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    return vault.status(conn)


def _clean_key(value: str) -> str:
    key = value.strip()
    if not 20 <= len(key) <= 300 or any(c.isspace() or not c.isprintable() for c in key):
        raise HTTPException(400, "key không đúng định dạng")
    return key


def _check_endpoint(body) -> None:
    if body.auth_type not in ("api_key", "token"):
        raise HTTPException(400, "loại xác thực không hợp lệ")
    if body.provider not in ("anthropic", "openai"):
        raise HTTPException(400, "nhà cung cấp không hợp lệ")


class LLMModelsQuery(BaseModel):
    key: str = ""  # để trống thì dùng credential đang dùng
    base_url: str | None = None
    auth_type: str = "api_key"
    provider: str = "anthropic"


@app.post("/api/settings/llm-models")
def llm_models(body: LLMModelsQuery, _: Principal = Depends(require_org_admin)):
    _check_endpoint(body)
    try:
        base_url = llm.check_base_url(body.base_url)
        if body.key.strip():
            cred = vault.Credential(_clean_key(body.key), base_url, body.auth_type, body.provider)
        else:
            current = vault.resolve()[0]
            # Chỉ dùng lại key đã lưu cho đúng endpoint đã lưu: đổi Base URL mà không nhập key sẽ gửi key đó tới máy chủ khác.
            if current is None or current.base_url != base_url or current.provider != body.provider:
                raise HTTPException(400, "hãy nhập key, hoặc giữ nguyên nhà cung cấp và Base URL đã lưu")
            cred = current
        return {"models": llm.list_models(cred)}
    except llm.LLMError as exc:
        raise HTTPException(400, f"không lấy được danh sách model: {exc}") from exc


@app.post("/api/settings/llm-key")
def llm_key_save(body: LLMKey, principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    key = _clean_key(body.key)
    _check_endpoint(body)
    strong, fast = _clean_model(body.model_strong), _clean_model(body.model_fast)
    if body.provider == "openai" and not strong:
        raise HTTPException(400, "OpenAI cần tên model")
    try:
        cred = vault.Credential(key, llm.check_base_url(body.base_url), body.auth_type, body.provider, strong, fast)
        llm.validate_key(cred)
    except llm.LLMError as exc:
        raise HTTPException(400, f"không lưu key: {exc}") from exc
    vault.save(conn, cred, principal.username)
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


_runs: dict[str, tuple[str, str, threading.Event]] = {}
_runs_lock = threading.Lock()


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None
    env: str = "dev"
    project: str | None = Field(default=None, max_length=64)


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
        _runs[run_id] = (t.tenant_id, t.user_id, cancelled)
    events: queue.Queue = queue.Queue()
    events.put({"type": "start", "run_id": run_id, "conversation_id": conversation_id})

    def worker() -> None:
        try:
            with db.session() as worker_conn:
                episode = agent_mod.run_task(worker_conn, t.tenant_id, t.user_id, message, body.env, body.project, conversation_id=conversation_id,
                                             episode_id=run_id, on_event=events.put, cancelled=cancelled)
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
            "approvals": approvals}


@app.get("/api/conversations")
def conversations(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return db.rows(conn.execute("SELECT id, title, created_at, updated_at FROM conversation WHERE tenant_id=? AND user_id=? AND archived_at IS NULL "
                                "ORDER BY updated_at DESC, rowid DESC LIMIT 200", (t.tenant_id, t.user_id)).fetchall())


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    c = _own_conversation(conn, t, conversation_id)
    gateway.expire_approvals(conn, t.tenant_id)
    rs = conn.execute("SELECT * FROM episode WHERE conversation_id=? AND tenant_id=? ORDER BY started_at, rowid", (conversation_id, t.tenant_id)).fetchall()
    return {**c, "messages": [_chat_message(conn, t, db.row(r, "input", "trace", "ku_used", "human_edit")) for r in rs]}


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
        return compiler.compile_tenant(conn, t.tenant_id, t.principal.username)
    except compiler.CompileError as exc:
        raise _compile_error(exc) from exc


@app.get("/api/compile/runs")
def compile_runs(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return db.rows(conn.execute("SELECT * FROM compile_run WHERE tenant_id=? ORDER BY created_at DESC, rowid DESC LIMIT 50", (t.tenant_id,)).fetchall(), "changed")


class Decide(BaseModel):
    approve: bool


@app.post("/api/compile/{run_id}/decide")
def compile_decide(run_id: str, body: Decide, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    try:
        return compiler.decide(conn, t.tenant_id, run_id, body.approve, t.principal.username)
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
    """Gỡ kết nối: công cụ của nó bị chặn ngay; audit và lịch sử được giữ."""
    try:
        with db.tx(conn):
            connections.update(conn, t.tenant_id, connection_id, t.user_id, is_admin=t.is_admin, is_org_admin=t.principal.is_org_admin,
                               changes={"status": "removed"})
    except connections.ConnectionError_ as exc:
        raise _connection_error(exc) from exc
    return {"ok": True}


@app.post("/api/connections/{connection_id}/test")
def connection_test(connection_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    try:
        return connections.test(conn, t.tenant_id, connection_id, t.user_id)
    except connections.ConnectionError_ as exc:
        raise _connection_error(exc) from exc


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
            "auto_promote": bool(r["auto_promote"]), "change_freeze": bool(r["change_freeze"]), "self_approve": bool(r["self_approve"])}


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
    return {"episodes": e["n"], "labeled_pct": round(100 * e["labeled"] / n, 1), "edited_pct": round(100 * e["edited"] / n, 1), "failed_pct": round(100 * e["failed"] / n, 1),
            "avg_tokens": round(e["tokens"], 1), "processed": e["processed"], "ku_status": status, "ku_accept_pct": round(100 * status.get("active", 0) / decided, 1) if decided else None,
            "tool_decisions": decisions, "weekly": weekly}


@app.exception_handler(ValueError)
def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
