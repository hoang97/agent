"""Công cụ học được: chuỗi bước gọi công cụ trong danh mục, sinh ra từ cách người dùng làm việc.

- Mỗi bước vẫn đi qua gateway như một lệnh thường, nên ghép bước không tạo thêm quyền.
- Tham số chỉ thay nguyên giá trị ("{{ten}}"), không nội suy vào chuỗi lệnh.
- Phạm vi tenant cần tenant_admin duyệt; phạm vi cá nhân do chính chủ bật.
- Tỷ lệ thành công tụt dưới 50% sau từ 4 lần dùng thì tự tắt.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict

from . import db, gateway
from .retrieval import tokenize
from .tools import callable_by, max_tier

PREFIX = "learned."
PLACEHOLDER = re.compile(r"^\{\{([a-z_][a-z0-9_]{0,30})\}\}$")
PARAM_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,30}$")
PARAM_TYPES = ("string", "integer")
MIN_EPISODES = 3
MAX_STEPS = 8
STATUSES = ("proposed", "active", "rejected", "disabled")


class LearnedError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def slug(text: str) -> str:
    """Bỏ dấu tiếng Việt rồi chuẩn hóa: "Thống kê đơn hàng" → "thong_ke_don_hang"."""
    plain = unicodedata.normalize("NFKD", (text or "").lower().replace("đ", "d"))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9_]+", "_", plain).strip("_")[:40] or "cong_cu"


def _placeholder(value) -> str | None:
    m = PLACEHOLDER.match(value) if isinstance(value, str) else None
    return m.group(1) if m else None


def _registry(conn, tenant_id: str, tool: str):
    return conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=? AND status='active'", (tenant_id, tool)).fetchone()


def validate(conn, tenant_id: str, steps, params) -> None:
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise LearnedError(f"cần từ 1 đến {MAX_STEPS} bước")
    if not isinstance(params, dict):
        raise LearnedError("params phải là object")
    for name, spec in params.items():
        if not PARAM_NAME.match(name) or not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES or set(spec) - {"type", "description"}:
            raise LearnedError(f"tham số {name!r} không hợp lệ")
    used = set()
    for i, step in enumerate(steps, 1):
        meta = _registry(conn, tenant_id, step.get("tool")) if isinstance(step, dict) and isinstance(step.get("tool"), str) else None
        if not isinstance(step, dict) or set(step) - {"tool", "args"} or meta is None or step["tool"].startswith(PREFIX):
            raise LearnedError(f"bước {i}: công cụ không có trong danh mục")
        args = step.get("args", {})
        if not isinstance(args, dict):
            raise LearnedError(f"bước {i}: args phải là object")
        for value in args.values():
            name = _placeholder(value)
            if name:
                if name not in params:
                    raise LearnedError(f"bước {i}: tham số {name} chưa được khai báo")
                used.add(name)
        missing = set(json.loads(meta["input_schema"]).get("required", [])) - set(args)
        if missing:
            raise LearnedError(f"bước {i}: thiếu tham số bắt buộc {sorted(missing)}")
    if set(params) - used:
        raise LearnedError(f"tham số không được dùng: {sorted(set(params) - used)}")


def _row(r) -> dict | None:
    return db.row(r, "params", "steps", "source_eps")


def get(conn, tenant_id: str, tool_id: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM learned_tool WHERE id=? AND tenant_id=?", (tool_id, tenant_id)).fetchone())


def risk(conn, tenant_id: str, steps: list[dict]) -> str:
    tiers = []
    for step in steps:
        r = conn.execute("SELECT risk_tier FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, step["tool"])).fetchone()
        tiers.append(r["risk_tier"] if r else "R4")
    return max_tier(*tiers)


def usable(conn, tenant_id: str, row: dict, env: str, roles: set[str], user_id: str | None = None) -> tuple[bool, str | None]:
    if row["status"] != "active":
        return False, "công cụ học được chưa bật"
    for step in row["steps"]:
        meta = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, step["tool"])).fetchone()
        if meta is None:
            return False, f"{step['tool']}: chưa đăng ký"
        ok, why = callable_by(meta, env, roles, row["scope"][5:] if row["scope"].startswith("user:") else user_id)
        if not ok:
            return False, f"{step['tool']}: {why}"
    return True, None


def flow(row: dict) -> str:
    return " → ".join(s["tool"] for s in row["steps"])


def spec(row: dict) -> dict:
    props = {k: {"type": v["type"], "description": v.get("description", "")} for k, v in row["params"].items()}
    return {"tool": row["name"], "description": f"{row['description']} (công cụ học được, chạy lần lượt: {flow(row)})",
            "input_schema": {"type": "object", "properties": props, "required": list(row["params"])}}


def visible_rows(conn, tenant_id: str, user_id: str) -> list[dict]:
    rs = conn.execute("SELECT * FROM learned_tool WHERE tenant_id=? AND (scope='tenant' OR scope=?) ORDER BY name", (tenant_id, f"user:{user_id}")).fetchall()
    return [_row(r) for r in rs]


def for_agent(conn, tenant_id: str, user_id: str, env: str, roles: set[str]) -> list[dict]:
    return [spec(r) for r in visible_rows(conn, tenant_id, user_id) if usable(conn, tenant_id, r, env, roles, user_id)[0]]


def find(conn, tenant_id: str, user_id: str, name: str) -> dict | None:
    """Công cụ cá nhân được ưu tiên hơn công cụ tenant trùng tên."""
    for scope in (f"user:{user_id}", "tenant"):
        r = conn.execute("SELECT * FROM learned_tool WHERE tenant_id=? AND scope=? AND name=?", (tenant_id, scope, name)).fetchone()
        if r:
            return _row(r)
    return None


def _unique_name(conn, tenant_id: str, scope: str, base: str) -> str:
    name, n = PREFIX + base, 2
    while conn.execute("SELECT 1 FROM learned_tool WHERE tenant_id=? AND scope=? AND name=?", (tenant_id, scope, name)).fetchone():
        name, n = f"{PREFIX}{base}_{n}", n + 1
    return name


def create(conn, tenant_id: str, *, name: str, description: str, steps: list, params: dict, scope: str, status: str,
           origin: str, source_eps: list[str], created_by: str) -> dict:
    validate(conn, tenant_id, steps, params)
    if conn.execute("SELECT 1 FROM learned_tool WHERE tenant_id=? AND scope=? AND name=?", (tenant_id, scope, name)).fetchone():
        raise LearnedError(f"đã có công cụ tên {name}", 409)
    tool_id, now = db.new_id(), db.iso()
    conn.execute(
        "INSERT INTO learned_tool(id,tenant_id,name,scope,description,params,steps,signature,status,origin,source_eps,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tool_id, tenant_id, name, scope, description, json.dumps(params, ensure_ascii=False), json.dumps(steps, ensure_ascii=False),
         json.dumps([s["tool"] for s in steps]), status, origin, json.dumps(source_eps), created_by, now, now))
    return get(conn, tenant_id, tool_id)


def _tainted(steps: list[dict]) -> bool:
    """Có bước nào chạy sau một kết quả untrusted: bước đó có thể bị nội dung ngoài lái.

    Các bước cùng nhóm song song được lập kế hoạch cùng lúc nên không ảnh hưởng nhau; chỉ nhóm sau mới bị tính.
    """
    groups: list[list[dict]] = []
    for step in steps:
        if groups and step.get("parallel") and groups[-1][0].get("parallel") == step["parallel"]:
            groups[-1].append(step)
        else:
            groups.append([step])
    seen = False
    for group in groups:
        if seen:
            return True
        seen = any(s.get("untrusted") for s in group)
    return False


def _scalar(value) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def from_episode(conn, tenant_id: str, user_id: str, episode_id: str, name: str | None, description: str | None, scope: str) -> dict:
    r = conn.execute("SELECT * FROM episode WHERE id=? AND tenant_id=?", (episode_id, tenant_id)).fetchone()
    if r is None:
        raise LearnedError("không tìm thấy lượt chat", 404)
    if r["user_id"] != user_id:
        raise LearnedError("chỉ người chạy lượt này mới lưu được thành công cụ", 403)
    ep = db.row(r, "input", "trace")
    steps = [s for s in ep["trace"] if s["type"] == "tool_call"]
    ok = [s for s in steps if s["status"] == "ok" and not s["tool"].startswith(PREFIX)]
    if not ok:
        raise LearnedError("lượt này không có bước công cụ nào chạy thành công")
    if _tainted(steps):
        raise LearnedError("lượt này có bước chạy sau nội dung chưa tin cậy nên không lưu thành công cụ được")
    words = set(tokenize(ep["input"]["task"]))
    params: dict = {}
    out = []
    for s in ok:
        args = {}
        for key, value in s["args"].items():
            # Giá trị người dùng gõ trong yêu cầu thường là thứ thay đổi giữa các lần dùng: biến thành tham số.
            if _scalar(value) and str(value).lower() in words:
                pname = key if key not in params else f"{key}_{len(params) + 1}"
                params[pname] = {"type": "integer" if isinstance(value, int) else "string", "description": f"ví dụ: {value}"}
                args[key] = "{{%s}}" % pname
            else:
                args[key] = value
        out.append({"tool": s["tool"], "args": args})
    scope_value = f"user:{user_id}" if scope == "user" else "tenant"
    base = slug(name) if name else slug("_".join(s["tool"].split(".")[-1] for s in out))
    return create(conn, tenant_id, name=_unique_name(conn, tenant_id, scope_value, base),
                  description=(description or ep["input"]["task"])[:300], steps=out, params=params, scope=scope_value,
                  status="active" if scope == "user" else "proposed", origin="manual", source_eps=[episode_id], created_by=user_id)


def propose(conn, tenant_id: str) -> int:
    """Đề xuất công cụ từ chuỗi bước giống nhau đã thành công ở từ MIN_EPISODES lượt."""
    rs = conn.execute("SELECT id, user_id, trace FROM episode WHERE tenant_id=? AND outcome='success' AND started_at>=? ORDER BY started_at",
                      (tenant_id, db.later(days=-30))).fetchall()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rs:
        steps = [s for s in json.loads(r["trace"]) if s["type"] == "tool_call"]
        if len(steps) < 2 or any(s["status"] != "ok" or s["tool"].startswith(PREFIX) for s in steps) or _tainted(steps):
            continue
        groups[tuple(s["tool"] for s in steps)].append((r["id"], r["user_id"], steps))
    made = 0
    for sig, items in groups.items():
        if len(items) < MIN_EPISODES:
            continue
        if conn.execute("SELECT 1 FROM learned_tool WHERE tenant_id=? AND signature=?", (tenant_id, json.dumps(list(sig)))).fetchone():
            continue  # đã có, kể cả bản bị từ chối: không đề xuất lại
        params: dict = {}
        steps: list = []
        generalizable = True
        for i, tool in enumerate(sig):
            keys = set.intersection(*(set(item[2][i]["args"]) for item in items))
            args = {}
            for key in sorted(keys):
                values = [item[2][i]["args"][key] for item in items]
                if all(v == values[0] for v in values):
                    args[key] = values[0]
                elif all(_scalar(v) for v in values):
                    pname = key if key not in params else f"{key}_{i + 1}"
                    examples = ", ".join(str(v) for v in list(dict.fromkeys(values))[:3])
                    params[pname] = {"type": "integer" if all(isinstance(v, int) for v in values) else "string", "description": f"ví dụ: {examples}"}
                    args[key] = "{{%s}}" % pname
                else:
                    generalizable = False
            steps.append({"tool": tool, "args": args})
        if not generalizable:
            continue
        users = {item[1] for item in items}
        scope = f"user:{users.pop()}" if len(users) == 1 else "tenant"
        base = slug("_".join(t.split(".")[-1] for t in sig))
        try:
            create(conn, tenant_id, name=_unique_name(conn, tenant_id, scope, base), description=f"Chuỗi {' → '.join(sig)} đã thành công {len(items)} lần",
                   steps=steps, params=params, scope=scope, status="proposed", origin="auto", source_eps=[i[0] for i in items], created_by="learning")
            made += 1
        except LearnedError:
            continue
    return made


def record_use(conn, row: dict, ok: bool) -> None:
    conn.execute("UPDATE learned_tool SET uses=uses+1, successes=successes+?, updated_at=? WHERE id=?", (int(ok), db.iso(), row["id"]))
    r = conn.execute("SELECT uses, successes FROM learned_tool WHERE id=?", (row["id"],)).fetchone()
    if r["uses"] >= 4 and r["successes"] / r["uses"] < 0.5:
        conn.execute("UPDATE learned_tool SET status='disabled', note=? WHERE id=?",
                     (f"tự tắt: chỉ thành công {r['successes']}/{r['uses']} lần", row["id"]))


def invoke_by_name(conn, ctx, name: str, args: dict) -> dict:
    base = {"type": "tool_call", "tool": name, "args": args, "env": ctx.env, "learned": True}
    row = find(conn, ctx.tenant_id, ctx.user_id, name)
    if row is None:
        return {**base, "decision": "blocked", "status": "blocked", "reasons": ["không có công cụ học được này"]}
    if ctx.roles is None:
        ctx.roles = gateway.user_roles(conn, ctx.tenant_id, ctx.user_id)
    tier = risk(conn, ctx.tenant_id, row["steps"])
    ok, why = usable(conn, ctx.tenant_id, row, ctx.env, ctx.roles, ctx.user_id)
    if not ok:
        return {**base, "risk_tier": tier, "decision": "blocked", "status": "blocked", "reasons": [why]}
    values = {}
    for pname, pspec in row["params"].items():
        value = args.get(pname)
        if pspec["type"] == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
        elif _scalar(value):
            value = str(value)
        else:
            value = None
        if value is None:
            return {**base, "risk_tier": tier, "decision": "blocked", "status": "invalid", "reasons": [f"thiếu hoặc sai tham số {pname}"]}
        values[pname] = value
    subs = []
    for step in row["steps"]:
        call_args = {k: values[_placeholder(v)] if _placeholder(v) else v for k, v in step["args"].items()}
        # Tham số của mọi bước đã cố định trước khi chạy, bước sau không đọc kết quả bước trước,
        # nên kết quả untrusted của một bước không thể lái bước sau. Các lượt sau vẫn bị đánh dấu qua step["untrusted"].
        sub = gateway.invoke(conn, ctx, step["tool"], call_args)
        subs.append(sub)
        if sub["status"] != "ok":
            break
    last = subs[-1]
    status = "ok" if len(subs) == len(row["steps"]) and all(s["status"] == "ok" for s in subs) else last["status"]
    if status in ("ok", "error"):
        record_use(conn, row, status == "ok")
    return {**base, "risk_tier": tier, "status": status, "decision": "auto" if status == "ok" else last.get("decision"),
            "steps": subs, "result": {"steps": [{"tool": s["tool"], "status": s["status"], "result": s.get("result")} for s in subs]},
            "untrusted": any(s.get("untrusted") for s in subs), "reasons": [] if status == "ok" else last.get("reasons", []),
            "approval_id": last.get("approval_id"), "required_approvals": last.get("required_approvals")}


def update(conn, tenant_id: str, tool_id: str, user_id: str, is_admin: bool, changes: dict) -> dict:
    row = get(conn, tenant_id, tool_id)
    personal = row is not None and row["scope"].startswith("user:")
    if row is None or (personal and row["scope"] != f"user:{user_id}"):
        raise LearnedError("không tìm thấy công cụ học được", 404)
    if not personal and not is_admin:
        raise LearnedError("cần quyền tenant_admin", 403)
    steps, params = changes.get("steps", row["steps"]), changes.get("params", row["params"])
    if "steps" in changes or "params" in changes:
        validate(conn, tenant_id, steps, params)
    status = changes.get("status", row["status"])
    if status not in STATUSES:
        raise LearnedError("trạng thái không hợp lệ")
    reset = status == "active" and row["status"] != "active"
    conn.execute(
        "UPDATE learned_tool SET description=?, steps=?, params=?, signature=?, status=?, approved_by=?, note=?, "
        "uses=CASE WHEN ? THEN 0 ELSE uses END, successes=CASE WHEN ? THEN 0 ELSE successes END, updated_at=? WHERE id=?",
        (changes.get("description", row["description"]), json.dumps(steps, ensure_ascii=False), json.dumps(params, ensure_ascii=False),
         json.dumps([s["tool"] for s in steps]), status, user_id if status == "active" else row["approved_by"],
         None if reset else row["note"], reset, reset, db.iso(), tool_id))
    return get(conn, tenant_id, tool_id)
