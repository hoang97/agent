"""Kết nối: một template + tham số của riêng tenant hoặc người dùng, gắn với các môi trường.

- Kết nối của tenant (dùng chung) chỉ tenant_admin tạo, sửa, xóa.
- Kết nối cá nhân do chính người dùng tạo; chỉ họ thấy và dùng công cụ của nó.
- Kết nối vào mạng nội bộ cần org_admin bật allow_private.
- Secret được mã hóa bằng khóa chủ của vault, không bao giờ trả về client.
- Công cụ của kết nối được đồng bộ vào tool_registry với tên "<tên_kết_nối>.<công_cụ>".
"""
from __future__ import annotations

import json
import logging
import re

from . import db, net, vault
from .connectors.base import ConnectorError
from .tools import ENVS, ROLE_CHOICES, TEMPLATES, TIERS, Template, max_tier, spec_of

NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
log = logging.getLogger("kagent.connections")


class ConnectionError_(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _err(message: str, status: int = 400) -> ConnectionError_:
    return ConnectionError_(message, status)


def _encrypt(data: dict) -> str:
    return vault._fernet().encrypt(json.dumps(data).encode()).decode()


def _decrypt(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(vault._fernet().decrypt(token.encode()).decode())
    except Exception:
        log.warning("không giải mã được secret của kết nối; khóa chủ có thể đã đổi")
        return {}


def _row(r) -> dict | None:
    return db.row(r, "params", "envs", "roles")


def get(conn, tenant_id: str, connection_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM connection WHERE id=? AND tenant_id=?", (connection_id, tenant_id)).fetchone())


def _clean(template: Template, params: dict, secrets: dict, old_params: dict, old_secrets: dict, allow_private: bool) -> tuple[dict, dict]:
    clean, hidden = {}, {}
    unknown = (set(params) | set(secrets)) - {f.name for f in template.fields}
    if unknown:
        raise _err(f"tham số không có trong template: {sorted(unknown)}")
    for f in template.fields:
        if f.secret:
            # Client cũ (JS còn trong cache) có thể gửi secret kiểu mới trong params; nhận luôn thay vì lặng lẽ bỏ qua.
            value = secrets.get(f.name) or params.get(f.name)
            value = value.strip() if isinstance(value, str) and value.strip() else old_secrets.get(f.name)
            if f.required and not value:
                raise _err(f"thiếu {f.name}")
            if value:
                if len(value) > (100_000 if f.type == "secret_text" else 20000):
                    raise _err(f"{f.name} quá dài")
                hidden[f.name] = value
            continue
        value = params.get(f.name, old_params.get(f.name, f.default))
        if value in (None, ""):
            if f.required:
                raise _err(f"thiếu {f.name}")
            continue
        if f.type == "boolean":
            if not isinstance(value, bool):
                raise _err(f"{f.name} phải là true/false")
        elif f.type == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise _err(f"{f.name} phải là số nguyên") from exc
        elif f.type == "list":
            items = value if isinstance(value, list) else str(value).split(",")
            value = [str(v).strip() for v in items if str(v).strip()][:100]
        else:
            if not isinstance(value, str) or len(value) > (20000 if f.type == "text" else 500):
                raise _err(f"{f.name} không hợp lệ")
            value = value.strip()
            if f.type == "url":
                try:
                    net.check_url(value, allow_private)
                except net.NetError as exc:
                    raise _err(f"{f.name}: {exc}") from exc
        clean[f.name] = value
    if template.validate:
        try:
            template.validate({**clean, **hidden, "_allow_private": allow_private})
        except (ConnectorError, net.NetError) as exc:
            raise _err(str(exc)) from exc
    if "host" in clean:  # kết nối CSDL: kiểm tra địa chỉ ngay khi tạo
        try:
            net.check_host(clean["host"], int(clean.get("port") or 0) or 5432, allow_private)
        except net.NetError as exc:
            raise _err(f"host: {exc}") from exc
    return clean, hidden


def _envs(envs) -> list[str]:
    if not isinstance(envs, list) or not envs or set(envs) - set(ENVS):
        raise _err(f"môi trường phải là danh sách không rỗng gồm {list(ENVS)}")
    return [e for e in ENVS if e in envs]


def _roles(roles) -> list[str]:
    if not isinstance(roles, list) or not roles or set(roles) - set(ROLE_CHOICES):
        raise _err(f"vai trò phải là danh sách không rỗng gồm {list(ROLE_CHOICES)}")
    return [r for r in ROLE_CHOICES if r in roles]


def create(conn, tenant_id: str, user_id: str, *, is_admin: bool, is_org_admin: bool, template: str, name: str, title: str = "",
           scope: str = "user", envs=None, roles=None, params=None, secrets=None, allow_private: bool = False, enable_all: bool = False) -> dict:
    tpl = TEMPLATES.get(template)
    if tpl is None:
        raise _err("template không tồn tại")
    if scope not in ("tenant", "user"):
        raise _err("phạm vi phải là tenant hoặc user")
    if scope == "tenant" and not is_admin:
        raise _err("chỉ tenant_admin được tạo kết nối dùng chung", 403)
    if allow_private and not is_org_admin:
        raise _err("chỉ org_admin được cho phép kết nối vào mạng nội bộ", 403)
    if not isinstance(name, str) or not NAME.match(name):
        raise _err("tên kết nối gồm 2-31 ký tự chữ thường, số, gạch dưới, bắt đầu bằng chữ")
    if conn.execute("SELECT 1 FROM connection WHERE tenant_id=? AND name=? AND status!='removed'", (tenant_id, name)).fetchone():
        raise _err(f"tenant đã có kết nối tên {name}", 409)
    clean, hidden = _clean(tpl, params or {}, secrets or {}, {}, {}, allow_private)
    cid, now = db.new_id(), db.iso()
    conn.execute(
        "INSERT INTO connection(id,tenant_id,name,template,title,scope,owner_user,envs,roles,params,secrets,allow_private,status,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
        (cid, tenant_id, name, template, (title or tpl.title)[:120], scope, user_id if scope == "user" else None, json.dumps(_envs(envs or ["dev"])),
         json.dumps(_roles(roles or ["member"])), json.dumps(clean, ensure_ascii=False), _encrypt(hidden), int(allow_private), user_id, now, now))
    row = get(conn, tenant_id, cid)
    sync(conn, row, initial=True, enable_all=enable_all, actor=user_id)
    log.info("tenant %s: %s tạo kết nối %s (%s, %s)", tenant_id, user_id, name, template, scope)
    return row


def can_manage(row: dict, user_id: str, is_admin: bool) -> bool:
    return row["owner_user"] == user_id if row["scope"] == "user" else is_admin


def update(conn, tenant_id: str, connection_id: str, user_id: str, *, is_admin: bool, is_org_admin: bool, changes: dict) -> dict:
    row = get(conn, tenant_id, connection_id)
    if row is None or row["status"] == "removed" or (row["scope"] == "user" and row["owner_user"] != user_id):
        raise _err("không tìm thấy kết nối", 404)
    if not can_manage(row, user_id, is_admin):
        raise _err("chỉ tenant_admin được sửa kết nối dùng chung", 403)
    allow_private = bool(changes.get("allow_private", row["allow_private"]))
    if allow_private != bool(row["allow_private"]) and not is_org_admin:
        raise _err("chỉ org_admin được đổi quyền vào mạng nội bộ", 403)
    tpl = TEMPLATES.get(row["template"])
    if tpl is None:
        raise _err("template của kết nối không còn tồn tại", 409)
    clean, hidden = _clean(tpl, changes.get("params") or {}, changes.get("secrets") or {}, row["params"], _decrypt(row["secrets"]), allow_private)
    status = changes.get("status", row["status"])
    if status not in ("active", "disabled", "removed"):
        raise _err("trạng thái không hợp lệ")
    conn.execute("UPDATE connection SET title=?, envs=?, roles=?, params=?, secrets=?, allow_private=?, status=?, updated_at=? WHERE id=?",
                 ((changes.get("title") or row["title"])[:120], json.dumps(_envs(changes.get("envs", row["envs"]))),
                  json.dumps(_roles(changes.get("roles", row["roles"]))), json.dumps(clean, ensure_ascii=False), _encrypt(hidden),
                  int(allow_private), status, db.iso(), connection_id))
    row = get(conn, tenant_id, connection_id)
    sync(conn, row, actor=user_id)
    log.info("tenant %s: %s cập nhật kết nối %s: %s", tenant_id, user_id, row["name"], sorted(k for k in changes if k != "secrets"))
    return row


def sync(conn, row: dict, initial: bool = False, enable_all: bool = False, actor: str | None = None) -> None:
    """Đưa công cụ của kết nối vào tool_registry, giữ lựa chọn bật/tắt, mức nâng, môi trường và vai trò của từng công cụ."""
    tpl = TEMPLATES.get(row["template"])
    status = "active" if row["status"] == "active" and tpl else "removed"
    names = set()
    now = db.iso()
    for spec in (tpl.tools.values() if tpl else []):
        name = f"{row['name']}.{spec.name}"
        names.add(name)
        meta = (row["name"], spec.action_class, spec.risk_tier, spec.reversible, spec.blast_radius, spec.data_sensitivity, spec.dry_run,
                int(spec.untrusted_output), spec.description, json.dumps(spec.input_schema, ensure_ascii=False), row["id"], row["template"],
                row["owner_user"], spec.canonical)
        existing = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (row["tenant_id"], name)).fetchone()
        if existing is None or existing["connection_id"] != row["id"]:
            enabled = enable_all or spec.action_class == "read"
            if existing is not None:
                conn.execute("DELETE FROM tool_registry WHERE tenant_id=? AND tool=?", (row["tenant_id"], name))
            conn.execute(
                "INSERT INTO tool_registry(server,action_class,catalog_tier,reversible,blast_radius,data_sensitivity,dry_run,untrusted_output,description,"
                "input_schema,connection_id,template,owner_user,canonical,tenant_id,tool,risk_tier,envs,roles,enabled,approved_by,status,synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*meta, row["tenant_id"], name, spec.risk_tier, json.dumps(row["envs"]), json.dumps(row["roles"]), int(enabled),
                 actor if initial else None, status, now))
            continue
        tier = max_tier(spec.risk_tier, existing["risk_floor"]) if existing["risk_floor"] in TIERS else spec.risk_tier
        envs = [e for e in json.loads(existing["envs"]) if e in row["envs"]] or row["envs"]
        conn.execute(
            "UPDATE tool_registry SET server=?,action_class=?,catalog_tier=?,reversible=?,blast_radius=?,data_sensitivity=?,dry_run=?,untrusted_output=?,"
            "description=?,input_schema=?,connection_id=?,template=?,owner_user=?,canonical=?,risk_tier=?,envs=?,status=?,synced_at=? WHERE tenant_id=? AND tool=?",
            (*meta, tier, json.dumps(envs), status, now, row["tenant_id"], name))
    for r in conn.execute("SELECT tool FROM tool_registry WHERE tenant_id=? AND connection_id=?", (row["tenant_id"], row["id"])).fetchall():
        if r["tool"] not in names:
            conn.execute("UPDATE tool_registry SET status='removed', synced_at=? WHERE tenant_id=? AND tool=?", (now, row["tenant_id"], r["tool"]))


def sync_all(conn) -> None:
    """Khi khởi động: cập nhật theo template hiện tại; công cụ không thuộc kết nối nào (bản mô phỏng cũ) chuyển removed."""
    with db.tx(conn):
        for r in conn.execute("SELECT * FROM connection").fetchall():
            sync(conn, _row(r))
        conn.execute("UPDATE tool_registry SET status='removed' WHERE connection_id IS NULL AND status!='removed'")


BUILTIN = ("data", "data")  # (tên kết nối, template): công cụ xử lý dữ liệu nội bộ, tenant nào cũng có


def ensure_builtin(conn) -> None:
    """Mỗi tenant có sẵn kết nối data (data.query, data.script) để agent lọc kết quả lớn đã lưu."""
    name, template = BUILTIN
    for t in conn.execute("SELECT id FROM tenant").fetchall():
        if conn.execute("SELECT 1 FROM connection WHERE tenant_id=? AND template=? AND status='active'", (t["id"], template)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM connection WHERE tenant_id=? AND name=? AND status!='removed'", (t["id"], name)).fetchone():
            log.warning("tenant %s đã có kết nối khác tên %s; bỏ qua kết nối xử lý dữ liệu nội bộ", t["id"], name)
            continue
        create(conn, t["id"], "system", is_admin=True, is_org_admin=False, template=template, name=name, scope="tenant",
               envs=list(ENVS), roles=list(ROLE_CHOICES), enable_all=True)
    # Công cụ nội bộ mới thêm vào template (đồng bộ mặc định tắt) được bật luôn, trừ khi admin đã từng chỉnh nó.
    conn.execute("UPDATE tool_registry SET enabled=1 WHERE template=? AND approved_by IS NULL AND enabled=0 AND status='active'", (template,))


def resolve(conn, tenant_id: str, tool_name: str) -> tuple[dict, dict, object, dict]:
    """(dòng registry, kết nối, ToolSpec, params gồm secret đã giải mã) để thực thi."""
    meta = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, tool_name)).fetchone()
    if meta is None:
        raise ConnectorError("công cụ chưa được đăng ký")
    row = get(conn, tenant_id, meta["connection_id"]) if meta["connection_id"] else None
    spec = spec_of(meta)
    if row is None or row["status"] != "active" or spec is None:
        raise ConnectorError("kết nối của công cụ không còn hoạt động")
    params = {**row["params"], **_decrypt(row["secrets"]), "_allow_private": bool(row["allow_private"])}
    return dict(meta), row, spec, params


def test(conn, tenant_id: str, connection_id: str, user_id: str) -> dict:
    row = get(conn, tenant_id, connection_id)
    if row is None or row["status"] == "removed" or (row["scope"] == "user" and row["owner_user"] != user_id):
        raise _err("không tìm thấy kết nối", 404)
    tpl = TEMPLATES[row["template"]]
    params = {**row["params"], **_decrypt(row["secrets"]), "_allow_private": bool(row["allow_private"])}
    try:
        return {"ok": True, "detail": tpl.ping(params, {})}
    except (ConnectorError, net.NetError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        log.exception("lỗi khi kiểm tra kết nối %s", row["name"])
        return {"ok": False, "error": f"lỗi không xác định ({exc.__class__.__name__})"}


def view(row: dict) -> dict:
    """Bản an toàn để trả về client: secret chỉ cho biết đã đặt hay chưa."""
    tpl = TEMPLATES.get(row["template"])
    secrets = _decrypt(row["secrets"])
    return {k: v for k, v in row.items() if k != "secrets"} | {
        "personal": row["scope"] == "user", "secrets_set": {f.name: f.name in secrets for f in (tpl.fields if tpl else []) if f.secret},
        "template_title": tpl.title if tpl else row["template"]}


def visible(conn, tenant_id: str, user_id: str) -> list[dict]:
    rs = conn.execute("SELECT * FROM connection WHERE tenant_id=? AND status!='removed' AND (scope='tenant' OR owner_user=?) ORDER BY scope, name",
                      (tenant_id, user_id)).fetchall()
    return [_row(r) for r in rs]
