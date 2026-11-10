"""HTTP API và phục vụ giao diện. Mọi truy vấn đều đi kèm tenant_id đã kiểm tra quyền thành viên."""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import secrets
import stat
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent as agent_mod
from . import compiler, db, gateway, knowledge, learning, llm, tools, vault
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
            tools.seed_registry(conn, tid)
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


@app.get("/api/settings/llm-key")
def llm_key_status(_: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    return vault.status(conn)


@app.post("/api/settings/llm-key")
def llm_key_save(body: LLMKey, principal: Principal = Depends(require_org_admin), conn=Depends(get_conn)):
    key = body.key.strip()
    if not 20 <= len(key) <= 300 or any(c.isspace() or not c.isprintable() for c in key):
        raise HTTPException(400, "key không đúng định dạng")
    try:
        llm.validate_key(key)
    except llm.LLMError as exc:
        raise HTTPException(400, f"không lưu key: {exc}") from exc
    vault.save(conn, key, principal.username)
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
    return {"username": principal.username, "is_org_admin": principal.is_org_admin, "tenants": tenants, "llm_provider": llm.get_provider().name}


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
           "FROM knowledge_unit WHERE tenant_id=?")
    args: list = [t.tenant_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return db.rows(conn.execute(sql + " ORDER BY recorded_at DESC, id LIMIT 500", args).fetchall(), "entities", "source_eps")


@app.get("/api/ku/{ku_id}")
def ku_detail(ku_id: str, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    ku = knowledge.get(conn, t.tenant_id, ku_id)
    if ku is None:
        raise HTTPException(404, "không tìm thấy KU")
    ku.pop("embedding", None)
    ku["events"] = db.rows(conn.execute("SELECT id, op, actor, reason, at FROM ku_event WHERE ku_id=? AND tenant_id=? ORDER BY id DESC", (ku_id, t.tenant_id)).fetchall())
    ku["edges"] = db.rows(conn.execute("SELECT src, dst, rel FROM ku_edge WHERE tenant_id=? AND (src=? OR dst=?)", (t.tenant_id, ku_id, ku_id)).fetchall())
    return ku


class NewKU(BaseModel):
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
    return _public(knowledge.add(conn, t.tenant_id, kind=body.kind, title=body.title, content=body.content, applies_when=body.applies_when, entities=body.entities,
                                 domain=body.domain, source_eps=[], source_trust="user", actor=t.principal.username, reason="thêm thủ công"))


def _public(ku: dict) -> dict:
    return {k: v for k, v in ku.items() if k != "embedding"}


class Review(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=500)


@app.post("/api/ku/{ku_id}/review")
def ku_review(ku_id: str, body: Review, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    ku = knowledge.get(conn, t.tenant_id, ku_id)
    if ku is None:
        raise HTTPException(404, "không tìm thấy KU")
    if ku["status"] != "candidate":
        raise HTTPException(409, f"KU đang ở trạng thái {ku['status']}")
    if body.approve:
        return _public(knowledge.promote(conn, t.tenant_id, ku_id, t.principal.username, body.reason or "người duyệt chấp nhận"))
    return _public(knowledge.update(conn, t.tenant_id, ku_id, {"status": "rejected"}, "DEPRECATE", t.principal.username, body.reason or "người duyệt từ chối"))


@app.post("/api/ku/events/{event_id}/rollback")
def ku_rollback(event_id: int, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
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
    return db.rows(conn.execute(sql + " ORDER BY a.created_at DESC, a.rowid DESC LIMIT 100", args).fetchall(), "args", "dry_run", "reasons", "decided_by", "result")


class ApprovalDecision(BaseModel):
    action: str
    reason: str | None = Field(default=None, max_length=1000)


@app.post("/api/approvals/{approval_id}/decide")
def approval_decide(approval_id: str, body: ApprovalDecision, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    try:
        return gateway.decide(conn, t.tenant_id, approval_id, t.user_id, t.roles, t.principal.is_org_admin, body.action, body.reason)
    except gateway.ApprovalError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@app.get("/api/tools")
def tool_list(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    return db.rows(conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? ORDER BY tool", (t.tenant_id,)).fetchall(), "input_schema", "envs")


class ToolPatch(BaseModel):
    enabled: bool | None = None
    risk_tier: str | None = None


@app.patch("/api/tools/{tool}")
def tool_patch(tool: str, body: ToolPatch, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    current = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (t.tenant_id, tool)).fetchone()
    if current is None:
        raise HTTPException(404, "không tìm thấy công cụ")
    if body.enabled is not None:
        conn.execute("UPDATE tool_registry SET enabled=?, approved_by=? WHERE tenant_id=? AND tool=?", (int(body.enabled), t.principal.username, t.tenant_id, tool))
    if body.risk_tier is not None:
        if body.risk_tier not in gateway.TIERS:
            raise HTTPException(400, "mức rủi ro không hợp lệ")
        if gateway.TIERS.index(body.risk_tier) < gateway.TIERS.index(current["risk_tier"]) and not t.principal.is_org_admin:
            raise HTTPException(403, "chỉ org_admin được hạ mức rủi ro")
        conn.execute("UPDATE tool_registry SET risk_tier=?, approved_by=? WHERE tenant_id=? AND tool=?", (body.risk_tier, t.principal.username, t.tenant_id, tool))
    return db.row(conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (t.tenant_id, tool)).fetchone(), "input_schema", "envs")


@app.get("/api/policy")
def policy_get(t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    r = conn.execute("SELECT policy, auto_promote, change_freeze FROM tenant WHERE id=?", (t.tenant_id,)).fetchone()
    overlay = json.loads(r["policy"])
    return {"overlay": overlay, "effective": gateway.merge_policy(overlay), "baseline": gateway.BASELINE,
            "auto_promote": bool(r["auto_promote"]), "change_freeze": bool(r["change_freeze"])}


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


@app.patch("/api/tenant")
def tenant_patch(body: TenantSettings, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    t.require_admin()
    if body.auto_promote is not None:
        conn.execute("UPDATE tenant SET auto_promote=? WHERE id=?", (int(body.auto_promote), t.tenant_id))
    if body.change_freeze is not None:
        conn.execute("UPDATE tenant SET change_freeze=? WHERE id=?", (int(body.change_freeze), t.tenant_id))
    return policy_get(t, conn)


class Simulate(BaseModel):
    tool: str
    env: str
    confidence: float = Field(ge=0, le=1)


@app.post("/api/policy/simulate")
def policy_simulate(body: Simulate, t: Tenant = Depends(current_tenant), conn=Depends(get_conn)):
    meta = conn.execute("SELECT risk_tier FROM tool_registry WHERE tenant_id=? AND tool=?", (t.tenant_id, body.tool)).fetchone()
    if meta is None or body.env not in gateway.ENVS:
        raise HTTPException(400, "công cụ hoặc môi trường không hợp lệ")
    decision = gateway.evaluate(gateway.get_policy(conn, t.tenant_id), meta["risk_tier"], body.tool, body.env, body.confidence, [])
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
