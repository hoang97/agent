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
from dataclasses import replace

from . import db, gateway, results
from .retrieval import tokenize
from .tools import callable_by, max_tier

PREFIX = "learned."
EPHEMERAL = "data."  # data.query/data.script làm việc trên result_id của một lượt cụ thể nên không dùng lại được
PLACEHOLDER = re.compile(r"^\{\{([a-z_][a-z0-9_]{0,30})\}\}$")
# {{step:N}}: kết quả đầy đủ của bước N, chỉ dùng làm dữ liệu đầu vào cho công cụ local (data.*), không bao giờ cho lệnh gọi ra ngoài.
STEP_REF = re.compile(r"^\{\{step:([1-9])\}\}$")
PARAM_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,30}$")
# {{x}} nằm giữa chuỗi (vd. command của kubectl_*): giá trị thay vào chỉ là một từ, không dấu cách, không mở đầu bằng "-",
# nên không chèn thêm được flag hay lệnh.
INLINE = re.compile(r"\{\{([a-z_][a-z0-9_]{0,30})\}\}")
INLINE_VALUE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:/=@+,-]{0,252}$")
MAX_INLINE_PARAMS = 6
COMMAND_ARG = "command"  # chỉ chuỗi lệnh dạng kubectl mới tự tách tham số khi học; SQL, URL... giữ nguyên
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


def _step_ref(value) -> int | None:
    m = STEP_REF.match(value) if isinstance(value, str) else None
    return int(m.group(1)) if m else None


def _inline(value) -> list[str]:
    """Tham số {{x}} nằm giữa chuỗi (không tính chuỗi chỉ gồm đúng một {{x}})."""
    return INLINE.findall(value) if isinstance(value, str) and not _placeholder(value) else []


def _values(value) -> list:
    return value if isinstance(value, list) else [value]


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
        for value in (v for raw in args.values() for v in _values(raw)):
            if isinstance(value, str) and "{{step:" in value and _step_ref(value) is None:
                raise LearnedError(f"bước {i}: {{{{step:N}}}} phải đứng riêng một giá trị")
            for name in ([_placeholder(value)] if _placeholder(value) else _inline(value)):
                if name not in params:
                    raise LearnedError(f"bước {i}: tham số {name} chưa được khai báo")
                used.add(name)
            ref = _step_ref(value)
            if ref is not None:
                if meta["template"] != EPHEMERAL.rstrip("."):
                    raise LearnedError(f"bước {i}: chỉ công cụ data.* được dùng kết quả bước trước ({{{{step:N}}}})")
                if ref >= i:
                    raise LearnedError(f"bước {i}: {{{{step:{ref}}}}} phải trỏ tới bước đứng trước")
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


def _pname(base: str, params: dict) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", base.lower()).strip("_")[:24] or "arg"
    base = base if PARAM_NAME.match(base) else f"p_{base}"
    name, n = base, 2
    while name in params:
        name, n = f"{base}_{n}", n + 1
    return name


def _pieces(token: str, prev: str | None) -> tuple[str, str, str]:
    """(tiền tố cố định, phần có thể thành tham số, tên gợi ý): --replicas=5 → ("--replicas=", "5", "replicas"), deploy/web → ("deploy/", "web", "deploy")."""
    if token.startswith("-"):
        head, eq, tail = token.partition("=")
        return (head + eq, tail, head.lstrip("-")) if eq else (token, "", "")
    head, slash, tail = token.rpartition("/")
    if slash:
        return head + slash, tail, head.split("/")[-1]
    hint = {"-n": "namespace", "--namespace": "namespace", "-l": "selector", "-c": "container"}.get(prev or "", "arg")
    return "", token, hint


def _templatize(texts: list[str], params: dict, keep=None) -> str | None:
    """Gộp các chuỗi lệnh cùng khung thành một mẫu có {{x}} ở chỗ khác nhau. keep(phần) quyết định chỗ nào thành tham số khi chỉ có một chuỗi."""
    rows = [t.split(" ") for t in texts]
    if len({len(r) for r in rows}) != 1:
        return None
    out, added = [], 0
    for i, column in enumerate(zip(*rows)):
        prev = rows[0][i - 1] if i else None
        split = [_pieces(t, prev) for t in column]
        heads, tails = {x[0] for x in split}, [x[1] for x in split]
        varies = len(set(column)) > 1
        if i == 0 or not tails[0] or len(heads) != 1 or not (varies or (keep and keep(tails[0]))):
            if varies:
                return None
            out.append(column[0])
            continue
        if not all(INLINE_VALUE.match(t) for t in tails):
            return None
        name = _pname(split[0][2], params)
        examples = ", ".join(list(dict.fromkeys(tails))[:3])
        params[name] = {"type": "integer" if all(t.isdigit() for t in tails) else "string", "description": f"ví dụ: {examples}"}
        out.append(split[0][0] + "{{%s}}" % name)
        added += 1
    return " ".join(out) if added <= MAX_INLINE_PARAMS else None


def from_episode(conn, tenant_id: str, user_id: str, episode_id: str, name: str | None, description: str | None, scope: str) -> dict:
    r = conn.execute("SELECT * FROM episode WHERE id=? AND tenant_id=?", (episode_id, tenant_id)).fetchone()
    if r is None:
        raise LearnedError("không tìm thấy lượt chat", 404)
    if r["user_id"] != user_id:
        raise LearnedError("chỉ người chạy lượt này mới lưu được thành công cụ", 403)
    ep = db.row(r, "input", "trace")
    steps = [s for s in ep["trace"] if s["type"] == "tool_call"]
    ok = [s for s in steps if s["status"] == "ok" and not s["tool"].startswith((PREFIX, EPHEMERAL))]
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
            elif key == COMMAND_ARG and isinstance(value, str) and " " in value.strip():
                trial = dict(params)
                template = _templatize([value], trial, keep=lambda piece: piece.lower() in words)
                args[key] = template if template else value
                params.update(trial if template else {})
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
        steps = [s for s in json.loads(r["trace"]) if s["type"] == "tool_call" and not s["tool"].startswith(EPHEMERAL)]
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
                elif key == COMMAND_ARG and all(isinstance(v, str) and " " in v.strip() for v in values):
                    trial = dict(params)  # mẫu hỏng giữa chừng thì không để lại tham số thừa
                    template = _templatize(values, trial)
                    if template is None:
                        generalizable = False
                    else:
                        params.update(trial)
                        args[key] = template
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
    subs: list[dict] = []
    handles: dict[int, str] = {}
    consumed: set[int] = set()

    def resolve(value):
        ref = _step_ref(value)
        if ref is None:
            if _placeholder(value):
                return values[_placeholder(value)]
            return INLINE.sub(lambda m: str(values[m.group(1)]), value) if _inline(value) else value
        consumed.add(ref)
        if ref not in handles:
            prev = subs[ref - 1]
            handles[ref] = prev.get("result", {}).get("raw_result_id") if isinstance(prev.get("result"), dict) and "raw_result_id" in prev["result"] \
                else results.save(conn, ctx.tenant_id, ctx.user_id, ctx.episode_id, prev["tool"], prev.get("result"), bool(prev.get("untrusted")))
        return handles[ref]

    for step in row["steps"]:
        for name in (n for raw in step["args"].values() for v in _values(raw) for n in _inline(v)):
            if not INLINE_VALUE.match(str(values[name])):
                return {**base, "risk_tier": tier, "decision": "blocked", "status": "invalid",
                        "reasons": [f"tham số {name} nằm trong chuỗi lệnh nên chỉ được là một từ (chữ, số, ._:/=@+,-), không mở đầu bằng -"]}
    reviewed = _reviewed(conn, ctx.tenant_id, row)
    for step in row["steps"]:
        call_args = {k: [resolve(x) for x in v] if isinstance(v, list) else resolve(v) for k, v in step["args"].items()}
        # Tham số của lệnh gọi ra ngoài cố định trước khi chạy; kết quả bước trước chỉ đi vào công cụ local (data.*)
        # qua {{step:N}}, nên nội dung untrusted không lái được lệnh nào tác động ra ngoài.
        sub = gateway.invoke(conn, replace(ctx, reviewed=reviewed), step["tool"], call_args)
        subs.append(sub)
        if sub["status"] != "ok":
            break
    last = subs[-1]
    status = "ok" if len(subs) == len(row["steps"]) and all(s["status"] == "ok" for s in subs) else last["status"]
    if status in ("ok", "error"):
        record_use(conn, row, status == "ok")
    return {**base, "risk_tier": tier, "status": status, "decision": "auto" if status == "ok" else last.get("decision"),
            "steps": subs, "result": {"steps": [{"tool": s["tool"], "status": s["status"],
                                                 # Bước đã được bước sau xử lý thì chỉ trả result_id, không đổ dữ liệu thô vào context.
                                                 "result": {"result_id": handles[i]} if i in consumed and i in handles else s.get("result")}
                                                for i, s in enumerate(subs, 1)]},
            "untrusted": any(s.get("untrusted") for s in subs), "reasons": [] if status == "ok" else last.get("reasons", []),
            "approval_id": last.get("approval_id"), "required_approvals": last.get("required_approvals")}


def _reviewed(conn, tenant_id: str, row: dict) -> bool:
    """Các bước (kể cả code data.script) đã được người có quyền xem và bật: công cụ tenant do admin, công cụ cá nhân do chính chủ khi tenant cho tự duyệt."""
    if not row.get("approved_by"):
        return False
    return row["scope"] == "tenant" or gateway.self_approve_enabled(conn, tenant_id)


def propose_tool(conn, tenant_id: str, user_id: str, *, name: str, description: str, steps, params, scope: str) -> dict:
    """Agent đề xuất công cụ chuẩn hóa từ cách lấy và lọc dữ liệu vừa làm; luôn chờ người xem các bước rồi mới bật."""
    if scope not in ("user", "tenant"):
        raise LearnedError("scope phải là user hoặc tenant")
    scope_value = f"user:{user_id}" if scope == "user" else "tenant"
    return create(conn, tenant_id, name=_unique_name(conn, tenant_id, scope_value, slug(name)), description=str(description or name)[:300],
                  steps=steps, params=params or {}, scope=scope_value, status="proposed", origin="agent", source_eps=[], created_by=user_id)


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
