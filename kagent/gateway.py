"""MCP Gateway: mọi lệnh gọi công cụ qua 3 cửa — chặn cứng → ma trận rủi ro × môi trường → auto mode (classifier, xem automode.py)."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import shlex
from dataclasses import dataclass, field

from . import automode, datafilter, db
from .config import settings
from .retrieval import tokenize
from . import net
from .connectors.base import ConnectorError
from .tools import callable_by

ENVS = ("dev", "staging", "prod")
TIERS = ("R0", "R1", "R2", "R3", "R4")
AUTO_RANK = {"on": 0, "read_only": 1, "off": 2}

BASELINE = {
    "environments": {e: {"auto_execute": "on"} for e in ENVS},
    "matrix": {
        "R0": {"dev": "auto", "staging": "auto", "prod": "auto"},
        "R1": {"dev": "auto", "staging": "auto", "prod": "judge"},
        "R2": {"dev": "judge", "staging": "judge", "prod": "ask"},
        "R3": {"dev": "judge", "staging": "ask", "prod": {"ask": 2}},
        "R4": {"dev": "ask", "staging": "block", "prod": "block"},
    },
    "deny": [],
}

DENIAL_STREAK, DENIAL_TOTAL = 3, 20  # auto mode chặn liên tiếp/tổng cộng trong một lượt đến mức này thì chuyển sang hỏi người


class PolicyError(ValueError):
    pass


def rank(cell) -> float:
    """Độ chặt của một ô ma trận; số càng lớn càng chặt."""
    if cell == "auto":
        return 0
    if cell == "judge":
        return 1
    if cell == "ask":
        return 3
    if cell == "block":
        return 5
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
            cell = _legacy(cell)
            if rank(cell) < rank(merged["matrix"][tier][env]):
                raise PolicyError(f"overlay không được nới lỏng {tier}/{env}")
            merged["matrix"][tier][env] = cell
    for env, thresholds in (overlay.get("thresholds") or {}).items():
        if env not in ENVS:
            raise PolicyError(f"môi trường không hợp lệ: {env}")
        for tier in thresholds:
            if tier not in TIERS:
                raise PolicyError(f"mức rủi ro không hợp lệ: {tier}")
            if rank("judge") >= rank(merged["matrix"][tier][env]):
                merged["matrix"][tier][env] = "judge"
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


def _legacy(cell):
    """Overlay lưu từ trước auto mode: ngưỡng điểm tin cậy {"ask_below": x} nay là ô judge."""
    return "judge" if isinstance(cell, dict) and "ask_below" in cell else cell


@dataclass
class Decision:
    outcome: str  # auto | judge | ask | block
    required: int = 0
    reasons: list[str] = field(default_factory=list)
    door: str = ""


def evaluate(policy: dict, tier: str, tool: str, env: str, forced: list[str], canonical: str | None = None) -> Decision:
    """canonical là "<template>.<công_cụ>" để luật chặn như "k8s.kubectl_admin" áp cho mọi kết nối Kubernetes."""
    for rule in policy["deny"]:
        names = [tool] + ([canonical] if canonical else [])
        if any(fnmatch.fnmatchcase(n, rule["tool"]) for n in names) and fnmatch.fnmatchcase(env, rule["env"]):
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
    if ask:
        return Decision("ask", required, reasons, "matrix" if not forced else "forced")
    return Decision("judge" if cell == "judge" else "auto", 0, reasons, "matrix")


@dataclass
class CallContext:
    tenant_id: str
    user_id: str
    env: str
    task: str
    episode_id: str | None = None
    # Ý của người dùng cho auto mode: tin nhắn của họ, lệnh họ đã duyệt, luật tự xử lý do admin đặt. Không chứa kết quả công cụ.
    intent: list = field(default_factory=list)
    calls: list = field(default_factory=list)  # lệnh agent đã gọi trong hội thoại; dùng chung giữa các bản replace() của ctx
    denials: dict = field(default_factory=lambda: {"streak": 0, "total": 0})  # dùng chung như calls
    escalate_on_block: bool = False  # không có agent để thử cách khác (luồng sự cố): auto mode chặn thì hỏi người luôn
    tokens: list = field(default_factory=list)
    roles: set | None = None  # vai trò của người yêu cầu trong tenant; None = tra cứu khi gọi
    reviewed: bool = False  # đang chạy bước của công cụ học được đã được người có quyền xem và bật
    forced: list = field(default_factory=list)  # lý do bên gọi buộc phải hỏi (vd. luồng cảnh báo: độ tin dưới ngưỡng cài đặt)


def user_roles(conn, tenant_id: str, user_id: str) -> set[str]:
    """org_admin được coi như tenant_admin ở mọi tenant."""
    roles = {r["role"] for r in conn.execute("SELECT role FROM membership WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)).fetchall()}
    if conn.execute("SELECT 1 FROM app_user WHERE id=? AND is_org_admin=1", (user_id,)).fetchone():
        roles.add("tenant_admin")
    return roles


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get_policy(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT policy FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    return merge_policy(json.loads(r["policy"]))


def _command_args(args: dict) -> list[str]:
    """argv của tham số command kiểu kubectl (bỏ chữ kubectl và phần sau --); lệnh không tách được thì rỗng."""
    command = args.get("command")
    if not isinstance(command, str) or "namespace" in args:
        return []
    try:
        argv = shlex.split(command)
    except ValueError:
        return []
    argv = argv[1:] if argv[:1] == ["kubectl"] else argv
    return argv[:argv.index("--")] if "--" in argv else argv


def namespace_of(args: dict) -> str | None:
    """Namespace của lệnh: tham số namespace, hoặc -n/--namespace trong command của kubectl_*."""
    if args.get("namespace"):
        return args["namespace"]
    argv = _command_args(args)
    for i, a in enumerate(argv):
        for flag in ("-n", "--namespace"):
            if a == flag and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(flag + "="):
                return a[len(flag) + 1:]
    return None


def _writes_today(conn, tenant_id: str) -> int:
    start = db.now().date().isoformat()
    return conn.execute(
        "SELECT count(*) c FROM tool_call_audit a JOIN tool_registry t ON t.tool=a.tool AND t.tenant_id=a.tenant_id "
        "WHERE a.tenant_id=? AND a.at>=? AND a.result_status='ok' AND t.action_class!='read'", (tenant_id, start)).fetchone()["c"]


def audit(conn, ctx: CallContext, tool, args, tier, decision, reasons, status) -> None:
    conn.execute(
        "INSERT INTO tool_call_audit(tenant_id,episode_id,user_id,tool,env,args_hash,args,risk_tier,confidence,decision,reasons,"
        "result_status,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ctx.tenant_id, ctx.episode_id, ctx.user_id, tool, ctx.env, args_hash(args), json.dumps(args, ensure_ascii=False, sort_keys=True),
         tier, None, decision, json.dumps(reasons, ensure_ascii=False), status, db.iso()))  # cột confidence chỉ còn cho dữ liệu cũ


def _run(conn, tenant_id: str, tool_name: str, args: dict, check: bool = False, user_id: str | None = None) -> dict:
    """Gọi connector thật của công cụ (hoặc hàm chạy thử). Không giữ giao dịch DB trong lúc gọi mạng.

    _filter (nếu có) không gửi xuống connector mà được áp lên kết quả ở đây, trước khi kết quả vào context của agent.
    """
    from . import connections
    _, _, spec, params = connections.resolve(conn, tenant_id, tool_name)
    fn = spec.check if check else spec.run
    if fn is None:
        return None
    params = {**params, "_tenant_id": tenant_id, "_user_id": user_id}
    result = fn(params, {k: v for k, v in args.items() if k != "_filter"})
    if args.get("_filter") and not check and result is not None:
        raw = result
        try:
            result = datafilter.apply(raw, args["_filter"])
        except datafilter.FilterError as exc:
            raise ConnectorError(f"_filter: {exc}") from exc
        # Bản gốc lớn được giữ lại để lọc theo cách khác bằng data.query mà không phải gọi lại hệ thống.
        from . import results
        if len(json.dumps(raw, ensure_ascii=False)) > results.INLINE_LIMIT:
            result["raw_result_id"] = results.save(conn, tenant_id, user_id, None, tool_name, raw, spec.untrusted(args))
    return result


def _execute(conn, tenant_id: str, env: str, tool_name: str, args: dict, user_id: str | None = None) -> tuple[str, dict]:
    try:
        return "ok", _run(conn, tenant_id, tool_name, args, user_id=user_id)
    except net.NetError as exc:
        return "error", {"error": f"kết nối bị chặn: {exc}"}
    except (ConnectorError, ValueError, TypeError) as exc:
        return "error", {"error": str(exc)}
    except Exception as exc:  # lỗi bất ngờ của driver: không lộ chi tiết nội bộ
        import logging
        logging.getLogger("kagent.gateway").exception("lỗi khi chạy %s", tool_name)
        return "error", {"error": f"lỗi không xác định khi gọi công cụ ({exc.__class__.__name__})"}


def _has_grant(conn, ctx: CallContext, tool: str, args: dict) -> bool:
    return conn.execute(
        "SELECT 1 FROM approval_grant WHERE tenant_id=? AND user_id=? AND tool=? AND env=? AND namespace IS ? AND expires_at>?",
        (ctx.tenant_id, ctx.user_id, tool, ctx.env, namespace_of(args), db.iso())).fetchone() is not None


def invoke(conn, ctx: CallContext, tool_name: str, args: dict) -> dict:
    """Điểm vào duy nhất để gọi công cụ. Trả về bước trace."""
    if tool_name.startswith("learned."):
        from . import learned  # công cụ học được: mỗi bước lại gọi invoke() này
        return learned.invoke_by_name(conn, ctx, tool_name, args)
    step: dict = {"type": "tool_call", "tool": tool_name, "args": args, "env": ctx.env}
    meta = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (ctx.tenant_id, tool_name)).fetchone()
    if meta is None:
        # Công cụ chưa phân loại mặc định bị coi là destructive: không chạy.
        audit(conn, ctx, tool_name, args, "R3", "blocked", ["công cụ chưa được đăng ký"], "blocked")
        return {**step, "decision": "blocked", "status": "blocked", "reasons": ["công cụ chưa được đăng ký"]}
    meta = dict(meta)
    tier = meta["risk_tier"]
    if ctx.roles is None:
        ctx.roles = user_roles(conn, ctx.tenant_id, ctx.user_id)
    allowed, why = callable_by(meta, ctx.env, ctx.roles, ctx.user_id)
    if not allowed:
        audit(conn, ctx, tool_name, args, tier, "blocked", [why], "blocked")
        return {**step, "decision": "blocked", "status": "blocked", "risk_tier": tier, "reasons": [why]}
    from .tools import spec_of
    tool = spec_of(meta)
    missing = [f for f in tool.required if f not in args]
    if missing or not isinstance(args, dict):
        audit(conn, ctx, tool_name, args, tier, "blocked", [f"thiếu tham số: {missing}"], "invalid")
        return {**step, "decision": "blocked", "status": "invalid", "risk_tier": tier, "reasons": [f"thiếu tham số: {missing}"]}
    if args.get("_filter") is not None:
        try:
            datafilter.check(args["_filter"])
        except datafilter.FilterError as exc:
            return {**step, "decision": "blocked", "status": "invalid", "risk_tier": tier, "reasons": [f"_filter: {exc}"]}

    dry_result = None
    if tool.check:
        try:
            dry_result = _run(conn, ctx.tenant_id, tool_name, args, check=True, user_id=ctx.user_id)
        except net.NetError as exc:
            dry_result = {"error": f"kết nối bị chặn: {exc}"}
        except (ConnectorError, ValueError, TypeError) as exc:
            dry_result = {"error": str(exc)}
        except Exception as exc:
            dry_result = {"error": f"chạy thử lỗi ({exc.__class__.__name__})"}

    tenant = conn.execute("SELECT change_freeze, write_quota_daily, self_approve FROM tenant WHERE id=?", (ctx.tenant_id,)).fetchone()
    forced: list[str] = list(ctx.forced)
    if isinstance(dry_result, dict) and dry_result.get("error"):
        forced.append("chạy thử phía server lỗi")
    if meta["action_class"] != "read":
        if tenant["change_freeze"]:
            forced.append("đang trong change freeze")
        if _writes_today(conn, ctx.tenant_id) >= tenant["write_quota_daily"]:
            forced.append("vượt quota ghi trong ngày")

    policy = get_policy(conn, ctx.tenant_id)
    # Công cụ local không chạm môi trường nào, nên dùng cột dev của ma trận (R4 vẫn luôn phải hỏi).
    decision = evaluate(policy, tier, tool_name, "dev" if tool.local else ctx.env, forced, meta.get("canonical"))
    step.update(risk_tier=tier, reasons=decision.reasons, dry_run=dry_result)

    if decision.outcome == "block":
        audit(conn, ctx, tool_name, args, tier, "blocked", decision.reasons, "blocked")
        return _called(ctx, {**step, "decision": "blocked", "status": "blocked"})
    if decision.outcome in ("ask", "judge") and tool.local and ctx.reviewed and not forced:
        # Code của data.script nằm cố định trong công cụ đã được duyệt; chỉ dữ liệu đầu vào thay đổi giữa các lần chạy.
        decision = Decision("auto", reasons=["bước của công cụ học được đã được duyệt"])
    elif decision.outcome == "judge" and tool.local:
        # Auto mode canh tác động ra ngoài; công cụ local chỉ xử lý dữ liệu đã lấy về (data.script là R4 nên vẫn luôn hỏi).
        decision = Decision("auto", reasons=["công cụ local không tác động ra ngoài"])
    # Khi tenant cho tự duyệt, một lần xác nhận của chính người yêu cầu là đủ, kể cả ô ma trận cần 2 người.
    required = 1 if tenant["self_approve"] else decision.required
    if decision.outcome in ("ask", "judge") and (decision.outcome == "judge" or required == 1) and not forced \
            and policy["environments"][ctx.env]["auto_execute"] != "off" and _has_grant(conn, ctx, tool_name, args):
        decision = Decision("auto", reasons=["dùng quyền đã duyệt cho phiên"])

    if decision.outcome != "auto":
        action = {"tool": tool_name, "args": args, "risk_tier": tier, "action_class": meta["action_class"], "env": ctx.env,
                  **({"dry_run": dry_result} if dry_result is not None else {})}
        verdict = automode.classify(conn, ctx.tenant_id, ctx.intent, ctx.calls, action)
        step["judge"] = verdict
        said = f"{verdict['rule']}: {verdict['reason']}" if verdict["rule"] else verdict["reason"]
        if decision.outcome == "ask":
            if verdict["allow"] is not None:  # ô luôn hỏi: classifier chỉ nhận xét cho người duyệt xem
                decision.reasons.append("nhận xét auto mode: " + ("khớp yêu cầu của người dùng" if verdict["allow"] else said))
        elif verdict["allow"]:
            ctx.denials["streak"] = 0
            decision = Decision("auto", reasons=["auto mode cho qua" + (f": {verdict['reason']}" if verdict["reason"] else "")])
        elif verdict["allow"] is None:
            decision = Decision("ask", 1, [said or "auto mode không đánh giá được"])
            required = 1
        else:
            ctx.denials["streak"] += 1
            ctx.denials["total"] += 1
            reason = f"auto mode chặn: {said}"
            if not (ctx.escalate_on_block or ctx.denials["streak"] >= DENIAL_STREAK or ctx.denials["total"] >= DENIAL_TOTAL):
                audit(conn, ctx, tool_name, args, tier, "blocked", [reason], "blocked")
                return _called(ctx, {**step, "decision": "blocked", "status": "blocked", "reasons": [reason]})
            more = [] if ctx.escalate_on_block else [f"auto mode đã chặn {ctx.denials['streak']} lần liên tiếp, {ctx.denials['total']} lần trong lượt"]
            decision = Decision("ask", 1, [reason, *more])
            required = 1
    step["reasons"] = decision.reasons

    if decision.outcome == "ask":
        approval_id = db.new_id()
        conn.execute(
            "INSERT INTO approval_request(id,tenant_id,episode_id,requester_id,original_request,tool,env,args,dry_run,risk_tier,confidence,"
            "reasons,rollback_hint,required_approvals,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (approval_id, ctx.tenant_id, ctx.episode_id, ctx.user_id, ctx.task, tool_name, ctx.env, json.dumps(args, ensure_ascii=False),
             json.dumps(dry_result, ensure_ascii=False), tier, 0, json.dumps(decision.reasons, ensure_ascii=False), tool.rollback_hint,
             required, db.iso(), db.later(minutes=settings.approval_ttl_minutes)))  # confidence = 0: cột chỉ còn cho dữ liệu cũ
        audit(conn, ctx, tool_name, args, tier, "pending", decision.reasons, "pending")
        return _called(ctx, {**step, "decision": "pending", "status": "pending", "approval_id": approval_id, "required_approvals": required})

    status, result = _execute(conn, ctx.tenant_id, ctx.env, tool_name, args, ctx.user_id)
    audit(conn, ctx, tool_name, args, tier, "auto", decision.reasons, status)
    step.update(decision="auto", status=status, result=result, untrusted=tool.untrusted(args))
    return _called(ctx, step)


def _called(ctx: CallContext, step: dict) -> dict:
    """Ghi lại lệnh agent vừa gọi cho các lần classify sau trong hội thoại; chỉ tên, tham số, trạng thái, không có kết quả."""
    ctx.calls.append({"tool": step["tool"], "args": step["args"], "status": step["status"]})
    return step


class ApprovalError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def can_approve(roles: set[str], env: str, is_org_admin: bool) -> bool:
    return is_org_admin or "tenant_admin" in roles or f"approver:{env}" in roles


def self_approve_enabled(conn, tenant_id: str) -> bool:
    r = conn.execute("SELECT self_approve FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    return bool(r and r["self_approve"])


def can_decide(req: dict, user_id: str, roles: set[str], is_org_admin: bool, self_approve: bool) -> bool:
    """Ai được quyết định yêu cầu này: chính người yêu cầu (nếu tenant cho tự duyệt) hoặc người có quyền duyệt."""
    if req["status"] != "pending" or user_id in req["decided_by"]:
        return False
    if user_id == req["requester_id"]:
        return self_approve
    return can_approve(roles, req["env"], is_org_admin)


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
    also: list[str] = []
    with db.tx(conn):
        req = db.row(conn.execute("SELECT * FROM approval_request WHERE id=? AND tenant_id=?", (approval_id, tenant_id)).fetchone(),
                     "args", "decided_by", "reasons")
        if req is None:
            raise ApprovalError("không tìm thấy yêu cầu", 404)
        if req["status"] != "pending":
            raise ApprovalError(f"yêu cầu đã ở trạng thái {req['status']}", 409)
        if user_id in req["decided_by"]:
            raise ApprovalError("bạn đã duyệt yêu cầu này", 409)
        if user_id == req["requester_id"]:
            if not self_approve_enabled(conn, tenant_id):
                raise ApprovalError("tenant này không cho người yêu cầu tự duyệt lệnh của mình", 403)
        elif not can_approve(roles, req["env"], is_org_admin):
            raise ApprovalError(f"không có quyền duyệt ở {req['env']}", 403)
        ctx = CallContext(tenant_id, req["requester_id"], req["env"], req["original_request"], req["episode_id"])
        meta = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, req["tool"])).fetchone()
        if action != "deny" and (meta is None or not callable_by(meta, req["env"], user_roles(conn, tenant_id, req["requester_id"]), req["requester_id"])[0]):
            raise ApprovalError("công cụ đã bị tắt, gỡ hoặc thu hẹp quyền sau khi yêu cầu được tạo; không thể thực thi", 409)

        if action == "deny":
            if not reason or not reason.strip():
                raise ApprovalError("phải nêu lý do từ chối")
            conn.execute("UPDATE approval_request SET status='denied', deny_reason=?, decided_by=?, decided_at=? WHERE id=?",
                         (reason.strip(), json.dumps(req["decided_by"] + [user_id]), db.iso(), approval_id))
            audit(conn, ctx, req["tool"], req["args"], req["risk_tier"], "denied", [reason], "denied")
        else:
            decided = req["decided_by"] + [user_id]
            if len(decided) < req["required_approvals"]:
                conn.execute("UPDATE approval_request SET decided_by=? WHERE id=?", (json.dumps(decided), approval_id))
            else:
                status, result = _execute(conn, tenant_id, req["env"], req["tool"], req["args"], req["requester_id"])
                result = _compact(conn, tenant_id, req, meta, result)
                conn.execute("UPDATE approval_request SET status='approved', decided_by=?, decided_at=?, result=? WHERE id=?",
                             (json.dumps(decided), db.iso(), json.dumps({"status": status, "result": result}, ensure_ascii=False), approval_id))
                audit(conn, ctx, req["tool"], req["args"], req["risk_tier"], "approved", decided, status)
                if action == "approve_session" and req["required_approvals"] == 1:
                    conn.execute("INSERT INTO approval_grant(id,tenant_id,user_id,tool,env,namespace,expires_at) VALUES (?,?,?,?,?,?,?)",
                                 (db.new_id(), tenant_id, req["requester_id"], req["tool"], req["env"], namespace_of(req["args"]),
                                  db.later(minutes=settings.grant_ttl_minutes)))
                    also = _run_covered_siblings(conn, tenant_id, req, meta, user_id)
    out = db.row(conn.execute("SELECT * FROM approval_request WHERE id=?", (approval_id,)).fetchone(),
                 "args", "decided_by", "reasons", "dry_run", "result")
    out["also_approved"] = also
    return out


def _compact(conn, tenant_id: str, req: dict, meta, result):
    """Kết quả lớn của lệnh chạy sau khi duyệt cũng được lưu thành result_id, như lệnh chạy ngay trong lượt."""
    from . import results
    from .tools import spec_of
    spec = spec_of(meta)
    small = results.compact(conn, tenant_id, req["requester_id"], req["episode_id"], req["tool"], result, bool(spec and spec.untrusted(req["args"])))
    return small or result


def _run_covered_siblings(conn, tenant_id: str, req: dict, meta, user_id: str) -> list[str]:
    """Grant vừa cấp phủ luôn các lệnh đọc cùng loại đang chờ của cùng người yêu cầu, để không phải bấm duyệt từng lệnh."""
    if meta["action_class"] != "read":
        return []
    done = []
    rows = conn.execute("SELECT * FROM approval_request WHERE tenant_id=? AND requester_id=? AND tool=? AND env=? AND status='pending' "
                        "AND required_approvals=1 AND id!=? ORDER BY created_at", (tenant_id, req["requester_id"], req["tool"], req["env"], req["id"])).fetchall()
    for r in rows:
        other = db.row(r, "args", "decided_by", "reasons")
        if other["args"].get("namespace") != req["args"].get("namespace"):
            continue
        status, result = _execute(conn, tenant_id, other["env"], other["tool"], other["args"], other["requester_id"])
        result = _compact(conn, tenant_id, other, meta, result)
        decided = other["decided_by"] + [user_id]
        conn.execute("UPDATE approval_request SET status='approved', decided_by=?, decided_at=?, result=? WHERE id=?",
                     (json.dumps(decided), db.iso(), json.dumps({"status": status, "result": result}, ensure_ascii=False), other["id"]))
        ctx = CallContext(tenant_id, other["requester_id"], other["env"], other["original_request"], other["episode_id"])
        audit(conn, ctx, other["tool"], other["args"], other["risk_tier"], "approved", ["dùng quyền đã duyệt cho phiên"], status)
        done.append(other["id"])
    return done


KUBECTL_WORDS = {"pod", "pods", "po", "deployment", "deployments", "deploy", "service", "services", "svc", "node", "nodes", "no", "namespace",
                 "namespaces", "ns", "configmap", "configmaps", "cm", "statefulset", "sts", "daemonset", "ds", "job", "jobs", "cronjob", "ingress",
                 "ing", "events", "all", "restart", "status", "history", "undo", "pause", "resume", "can-i", "whoami"}
_VALUE_FLAGS = {"-n", "--namespace", "-l", "--selector", "-c", "--container", "-o", "--output", "-p", "--patch"}


def _kubectl_targets(args: dict) -> list[str]:
    """Đối tượng của lệnh kubectl: tham số vị trí sau lệnh con (deploy/web → web) và namespace."""
    argv, out, skip = _command_args(args), [], False
    for a in argv[1:]:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in _VALUE_FLAGS
        elif a.lower() not in KUBECTL_WORDS:
            out.append(a.split("/")[-1].lower())
    ns = namespace_of(args) if "namespace" not in args else None
    return out + ([ns.lower()] if ns else [])


def plan_overlap(task: str, tool_name: str, args: dict) -> float:
    """Kiểm tra độc lập khi không có model: đối tượng của lệnh có nằm trong yêu cầu gốc không."""
    words = set(tokenize(task))
    targets = [str(v).lower() for k, v in args.items() if k in ("deployment", "namespace", "name", "index", "alert", "url", "query", "pod")]
    targets += _kubectl_targets(args)
    if not targets:
        return 0.5
    hits = sum(1 for t in targets if t in words or any(p in words for p in tokenize(t)))
    return round(hits / len(targets), 3)
