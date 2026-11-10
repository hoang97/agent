"""MCP Gateway: mọi lệnh gọi công cụ qua 3 cửa — chặn cứng → ma trận rủi ro × môi trường → điểm tin cậy."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from dataclasses import dataclass, field

from . import db
from .config import settings
from .retrieval import tokenize
from .tools import TOOLS, ToolError, load_state, save_state

ENVS = ("dev", "staging", "prod")
TIERS = ("R0", "R1", "R2", "R3", "R4")
AUTO_RANK = {"on": 0, "read_only": 1, "off": 2}

BASELINE = {
    "environments": {e: {"auto_execute": "on"} for e in ENVS},
    "matrix": {
        "R0": {"dev": "auto", "staging": "auto", "prod": "auto"},
        "R1": {"dev": "auto", "staging": "auto", "prod": {"ask_below": 0.8}},
        "R2": {"dev": "auto", "staging": {"ask_below": 0.7}, "prod": "ask"},
        "R3": {"dev": {"ask_below": 0.8}, "staging": "ask", "prod": {"ask": 2}},
        "R4": {"dev": "ask", "staging": "block", "prod": "block"},
    },
    "deny": [],
}

WEIGHTS = {"skill": 0.35, "precedent": 0.25, "dry_run": 0.25, "independent": 0.15}


class PolicyError(ValueError):
    pass


def rank(cell) -> float:
    """Độ chặt của một ô ma trận; số càng lớn càng chặt."""
    if cell == "auto":
        return 0
    if cell == "ask":
        return 3
    if cell == "block":
        return 5
    if isinstance(cell, dict) and "ask_below" in cell:
        value = cell["ask_below"]
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise PolicyError(f"ngưỡng không hợp lệ: {value}")
        return 1 + value
    if isinstance(cell, dict) and "ask" in cell:
        if not isinstance(cell["ask"], int) or cell["ask"] < 1:
            raise PolicyError(f"số người duyệt không hợp lệ: {cell['ask']}")
        return 2.5 + cell["ask"] * 0.5
    raise PolicyError(f"ô ma trận không hợp lệ: {cell}")


def merge_policy(overlay: dict | None) -> dict:
    """Overlay của tenant chỉ được siết; nới lỏng thì báo lỗi."""
    overlay = overlay or {}
    unknown = set(overlay) - {"environments", "matrix", "deny", "thresholds", "min_approvers"}
    if unknown:
        raise PolicyError(f"khóa không hợp lệ: {sorted(unknown)}")
    merged = copy.deepcopy(BASELINE)
    for env, conf in (overlay.get("environments") or {}).items():
        if env not in ENVS:
            raise PolicyError(f"môi trường không hợp lệ: {env}")
        wanted = conf.get("auto_execute", "on")
        if wanted not in AUTO_RANK:
            raise PolicyError(f"auto_execute không hợp lệ: {wanted}")
        merged["environments"][env]["auto_execute"] = wanted
    for tier, cells in (overlay.get("matrix") or {}).items():
        if tier not in TIERS:
            raise PolicyError(f"mức rủi ro không hợp lệ: {tier}")
        for env, cell in cells.items():
            if env not in ENVS:
                raise PolicyError(f"môi trường không hợp lệ: {env}")
            if rank(cell) < rank(merged["matrix"][tier][env]):
                raise PolicyError(f"overlay không được nới lỏng {tier}/{env}")
            merged["matrix"][tier][env] = cell
    for env, thresholds in (overlay.get("thresholds") or {}).items():
        if env not in ENVS:
            raise PolicyError(f"môi trường không hợp lệ: {env}")
        for tier, value in thresholds.items():
            if tier not in TIERS:
                raise PolicyError(f"mức rủi ro không hợp lệ: {tier}")
            cell = {"ask_below": value}
            current = merged["matrix"][tier][env]
            if rank(cell) < rank(current):
                raise PolicyError(f"ngưỡng overlay không được thấp hơn baseline ở {tier}/{env}")
            merged["matrix"][tier][env] = cell
    for env, approvers in (overlay.get("min_approvers") or {}).items():
        for tier, n in approvers.items():
            cell = {"ask": n}
            current = merged["matrix"][tier][env]
            if rank(cell) < rank(current):
                raise PolicyError(f"số người duyệt overlay thấp hơn baseline ở {tier}/{env}")
            merged["matrix"][tier][env] = cell
    for rule in overlay.get("deny") or []:
        if not isinstance(rule, dict) or "tool" not in rule:
            raise PolicyError(f"luật deny không hợp lệ: {rule}")
        merged["deny"].append({"tool": rule["tool"], "env": rule.get("env", "*")})
    return merged


@dataclass
class Decision:
    outcome: str  # auto | ask | block
    required: int = 0
    reasons: list[str] = field(default_factory=list)
    door: str = ""


def evaluate(policy: dict, tier: str, tool: str, env: str, confidence: float, forced: list[str]) -> Decision:
    for rule in policy["deny"]:
        if fnmatch.fnmatchcase(tool, rule["tool"]) and fnmatch.fnmatchcase(env, rule["env"]):
            return Decision("block", reasons=[f"luật chặn: {rule['tool']} @ {rule['env']}"], door="deny")
    cell = policy["matrix"][tier][env]
    if cell == "block":
        return Decision("block", reasons=[f"ma trận chặn {tier} ở {env}"], door="matrix")

    reasons = list(forced)
    required = 1
    ask = bool(forced)
    switch = policy["environments"][env]["auto_execute"]
    if switch == "off":
        ask = True
        reasons.append(f"auto_execute=off ở {env}")
    elif switch == "read_only" and tier != "R0":
        ask = True
        reasons.append(f"auto_execute=read_only ở {env}, {tier} phải hỏi")

    if cell == "ask":
        ask = True
        reasons.append(f"ma trận luôn hỏi {tier} ở {env}")
    elif isinstance(cell, dict) and "ask" in cell:
        ask = True
        required = cell["ask"]
        reasons.append(f"ma trận luôn hỏi {tier} ở {env}, cần {required} người duyệt")
    elif isinstance(cell, dict) and confidence < cell["ask_below"]:
        ask = True
        reasons.append(f"tin cậy {confidence:.2f} < ngưỡng {cell['ask_below']}")
    return Decision("ask" if ask else "auto", required if ask else 0, reasons, "matrix" if not forced else "forced")


@dataclass
class CallContext:
    tenant_id: str
    user_id: str
    env: str
    task: str
    episode_id: str | None = None
    tainted: bool = False  # kế hoạch đã chịu ảnh hưởng từ nội dung untrusted
    independent: float = 0.5
    tokens: list = field(default_factory=list)


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get_policy(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT policy FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    return merge_policy(json.loads(r["policy"]))


def _precedent(conn, ctx: CallContext, tool: str, args: dict) -> float:
    rs = conn.execute(
        "SELECT args FROM tool_call_audit WHERE tenant_id=? AND tool=? AND env=? AND decision IN ('auto','approved') "
        "AND result_status='ok' ORDER BY id DESC LIMIT 50", (ctx.tenant_id, tool, ctx.env)).fetchall()
    if not rs:
        return 0.0
    current = {(k, json.dumps(v, sort_keys=True)) for k, v in args.items()}
    best = 0.0
    for r in rs:
        past = {(k, json.dumps(v, sort_keys=True)) for k, v in json.loads(r["args"]).items()}
        union = current | past
        best = max(best, len(current & past) / len(union) if union else 1.0)
    return round(min(1.0, 0.5 * best + 0.5 * min(1.0, len(rs) / 5)), 3)


def _skill_score(conn, ctx: CallContext, tool: str) -> float:
    best = 0.0
    for r in conn.execute("SELECT tools, success_rate_30d FROM skill WHERE tenant_id=?", (ctx.tenant_id,)).fetchall():
        if tool in json.loads(r["tools"]):
            best = max(best, r["success_rate_30d"])
    return best


def confidence(conn, ctx: CallContext, tool_meta: dict, args: dict, dry_ok: float) -> tuple[float, dict]:
    parts = {
        "skill": _skill_score(conn, ctx, tool_meta["tool"]),
        "precedent": _precedent(conn, ctx, tool_meta["tool"], args),
        "dry_run": dry_ok,
        "independent": ctx.independent,
    }
    return round(sum(WEIGHTS[k] * v for k, v in parts.items()), 3), parts


def _unseen_object(conn, ctx: CallContext, args: dict) -> str | None:
    target = args.get("namespace") or args.get("index")
    if not target:
        return None
    key_args = conn.execute(
        "SELECT args FROM tool_call_audit WHERE tenant_id=? AND env=? AND result_status='ok' ORDER BY id DESC LIMIT 500",
        (ctx.tenant_id, ctx.env)).fetchall()
    for r in key_args:
        past = json.loads(r["args"])
        if past.get("namespace") == target or past.get("index") == target:
            return None
    return f"đối tượng chưa từng thấy: {target}"


def _writes_today(conn, tenant_id: str) -> int:
    start = db.now().date().isoformat()
    return conn.execute(
        "SELECT count(*) c FROM tool_call_audit a JOIN tool_registry t ON t.tool=a.tool AND t.tenant_id=a.tenant_id "
        "WHERE a.tenant_id=? AND a.at>=? AND a.result_status='ok' AND t.action_class!='read'", (tenant_id, start)).fetchone()["c"]


def audit(conn, ctx: CallContext, tool, args, tier, conf, decision, reasons, status) -> None:
    conn.execute(
        "INSERT INTO tool_call_audit(tenant_id,episode_id,user_id,tool,env,args_hash,args,risk_tier,confidence,decision,reasons,"
        "result_status,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ctx.tenant_id, ctx.episode_id, ctx.user_id, tool, ctx.env, args_hash(args), json.dumps(args, ensure_ascii=False, sort_keys=True),
         tier, conf, decision, json.dumps(reasons, ensure_ascii=False), status, db.iso()))


def _execute(conn, tenant_id: str, env: str, tool_name: str, args: dict) -> tuple[str, dict]:
    tool = TOOLS[tool_name]
    with db.tx(conn):
        state = load_state(conn, tenant_id, env)
        try:
            result = tool.run(state, args)
        except (ToolError, ValueError, TypeError) as exc:
            return "error", {"error": str(exc)}
        save_state(conn, tenant_id, env, state)
    return "ok", result


def _has_grant(conn, ctx: CallContext, tool: str, args: dict) -> bool:
    return conn.execute(
        "SELECT 1 FROM approval_grant WHERE tenant_id=? AND user_id=? AND tool=? AND env=? AND namespace IS ? AND expires_at>?",
        (ctx.tenant_id, ctx.user_id, tool, ctx.env, args.get("namespace"), db.iso())).fetchone() is not None


def invoke(conn, ctx: CallContext, tool_name: str, args: dict) -> dict:
    """Điểm vào duy nhất để gọi công cụ. Trả về bước trace."""
    step: dict = {"type": "tool_call", "tool": tool_name, "args": args, "env": ctx.env}
    meta = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (ctx.tenant_id, tool_name)).fetchone()
    tool = TOOLS.get(tool_name)
    if meta is None or tool is None:
        # Công cụ chưa phân loại mặc định bị coi là destructive: không chạy.
        audit(conn, ctx, tool_name, args, "R3", 0.0, "blocked", ["công cụ chưa được đăng ký"], "blocked")
        return {**step, "decision": "blocked", "status": "blocked", "reasons": ["công cụ chưa được đăng ký"]}
    meta = dict(meta)
    tier = meta["risk_tier"]
    if not meta["enabled"]:
        audit(conn, ctx, tool_name, args, tier, 0.0, "blocked", ["công cụ chưa bật cho tenant"], "blocked")
        return {**step, "decision": "blocked", "status": "blocked", "risk_tier": tier, "reasons": ["công cụ chưa bật cho tenant"]}
    missing = [f for f in tool.required if f not in args]
    if missing or not isinstance(args, dict):
        audit(conn, ctx, tool_name, args, tier, 0.0, "blocked", [f"thiếu tham số: {missing}"], "invalid")
        return {**step, "decision": "blocked", "status": "invalid", "risk_tier": tier, "reasons": [f"thiếu tham số: {missing}"]}

    dry_result = None
    dry_ok = 0.0
    if tool.check:
        try:
            dry_result = tool.check(load_state(conn, ctx.tenant_id, ctx.env), args)
            dry_ok = 1.0
        except (ToolError, ValueError, TypeError) as exc:
            dry_result = {"error": str(exc)}

    tenant = conn.execute("SELECT change_freeze, write_quota_daily FROM tenant WHERE id=?", (ctx.tenant_id,)).fetchone()
    forced: list[str] = []
    if ctx.tainted:
        forced.append("kế hoạch chịu ảnh hưởng từ nội dung untrusted")
    if meta["action_class"] != "read":
        unseen = _unseen_object(conn, ctx, args)
        if unseen:
            forced.append(unseen)
        if tenant["change_freeze"]:
            forced.append("đang trong change freeze")
        if _writes_today(conn, ctx.tenant_id) >= tenant["write_quota_daily"]:
            forced.append("vượt quota ghi trong ngày")

    policy = get_policy(conn, ctx.tenant_id)
    conf, parts = confidence(conn, ctx, meta, args, dry_ok)
    decision = evaluate(policy, tier, tool_name, ctx.env, conf, forced)
    step.update(risk_tier=tier, confidence=conf, confidence_parts=parts, reasons=decision.reasons, dry_run=dry_result)

    if decision.outcome == "block":
        audit(conn, ctx, tool_name, args, tier, conf, "blocked", decision.reasons, "blocked")
        return {**step, "decision": "blocked", "status": "blocked"}

    if decision.outcome == "ask" and decision.required == 1 and not forced \
            and policy["environments"][ctx.env]["auto_execute"] != "off" and _has_grant(conn, ctx, tool_name, args):
        decision = Decision("auto", reasons=["dùng quyền đã duyệt cho phiên"])
        step["reasons"] = decision.reasons

    if decision.outcome == "ask":
        approval_id = db.new_id()
        conn.execute(
            "INSERT INTO approval_request(id,tenant_id,episode_id,requester_id,original_request,tool,env,args,dry_run,risk_tier,confidence,"
            "reasons,rollback_hint,required_approvals,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, ctx.tenant_id, ctx.episode_id, ctx.user_id, ctx.task, tool_name, ctx.env, json.dumps(args, ensure_ascii=False),
             json.dumps(dry_result, ensure_ascii=False), tier, conf, json.dumps(decision.reasons, ensure_ascii=False), tool.rollback_hint,
             decision.required, db.iso(), db.later(minutes=settings.approval_ttl_minutes)))
        audit(conn, ctx, tool_name, args, tier, conf, "pending", decision.reasons, "pending")
        return {**step, "decision": "pending", "status": "pending", "approval_id": approval_id, "required_approvals": decision.required}

    status, result = _execute(conn, ctx.tenant_id, ctx.env, tool_name, args)
    audit(conn, ctx, tool_name, args, tier, conf, "auto", decision.reasons, status)
    step.update(decision="auto", status=status, result=result, untrusted=bool(meta["untrusted_output"]))
    return step


class ApprovalError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def can_approve(roles: set[str], env: str, is_org_admin: bool) -> bool:
    return is_org_admin or "tenant_admin" in roles or f"approver:{env}" in roles


def expire_approvals(conn, tenant_id: str | None = None) -> int:
    sql = "UPDATE approval_request SET status='expired', decided_at=? WHERE status='pending' AND expires_at<=?"
    now = db.iso()
    args: list = [now, now]
    if tenant_id:
        sql += " AND tenant_id=?"
        args.append(tenant_id)
    return conn.execute(sql, args).rowcount


def decide(conn, tenant_id: str, approval_id: str, user_id: str, roles: set[str], is_org_admin: bool,
           action: str, reason: str | None = None) -> dict:
    """action: approve_once | approve_session | deny. Hết 30 phút không trả lời coi như từ chối."""
    if action not in ("approve_once", "approve_session", "deny"):
        raise ApprovalError("hành động không hợp lệ")
    expire_approvals(conn, tenant_id)
    with db.tx(conn):
        req = db.row(conn.execute("SELECT * FROM approval_request WHERE id=? AND tenant_id=?", (approval_id, tenant_id)).fetchone(),
                     "args", "decided_by", "reasons")
        if req is None:
            raise ApprovalError("không tìm thấy yêu cầu", 404)
        if req["status"] != "pending":
            raise ApprovalError(f"yêu cầu đã ở trạng thái {req['status']}", 409)
        if user_id == req["requester_id"]:
            raise ApprovalError("người yêu cầu không được tự duyệt lệnh của mình", 403)
        if not can_approve(roles, req["env"], is_org_admin):
            raise ApprovalError(f"không có quyền duyệt ở {req['env']}", 403)
        if user_id in req["decided_by"]:
            raise ApprovalError("bạn đã duyệt yêu cầu này", 409)
        ctx = CallContext(tenant_id, req["requester_id"], req["env"], req["original_request"], req["episode_id"])

        if action == "deny":
            if not reason or not reason.strip():
                raise ApprovalError("phải nêu lý do từ chối")
            conn.execute("UPDATE approval_request SET status='denied', deny_reason=?, decided_by=?, decided_at=? WHERE id=?",
                         (reason.strip(), json.dumps(req["decided_by"] + [user_id]), db.iso(), approval_id))
            audit(conn, ctx, req["tool"], req["args"], req["risk_tier"], req["confidence"], "denied", [reason], "denied")
        else:
            decided = req["decided_by"] + [user_id]
            if len(decided) < req["required_approvals"]:
                conn.execute("UPDATE approval_request SET decided_by=? WHERE id=?", (json.dumps(decided), approval_id))
            else:
                status, result = _execute(conn, tenant_id, req["env"], req["tool"], req["args"])
                conn.execute("UPDATE approval_request SET status='approved', decided_by=?, decided_at=?, result=? WHERE id=?",
                             (json.dumps(decided), db.iso(), json.dumps({"status": status, "result": result}, ensure_ascii=False), approval_id))
                audit(conn, ctx, req["tool"], req["args"], req["risk_tier"], req["confidence"], "approved", decided, status)
                if action == "approve_session" and req["required_approvals"] == 1:
                    conn.execute("INSERT INTO approval_grant(id,tenant_id,user_id,tool,env,namespace,expires_at) VALUES (?,?,?,?,?,?,?)",
                                 (db.new_id(), tenant_id, req["requester_id"], req["tool"], req["env"], req["args"].get("namespace"),
                                  db.later(minutes=settings.grant_ttl_minutes)))
    return db.row(conn.execute("SELECT * FROM approval_request WHERE id=?", (approval_id,)).fetchone(),
                  "args", "decided_by", "reasons", "dry_run", "result")


def plan_overlap(task: str, tool_name: str, args: dict) -> float:
    """Kiểm tra độc lập khi không có model: đối tượng của lệnh có nằm trong yêu cầu gốc không."""
    words = set(tokenize(task))
    targets = [str(v).lower() for k, v in args.items() if k in ("deployment", "namespace", "name", "index", "alert", "url", "query", "pod")]
    if not targets:
        return 0.5
    hits = sum(1 for t in targets if t in words or any(p in words for p in tokenize(t)))
    return round(hits / len(targets), 3)
