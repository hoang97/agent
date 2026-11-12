"""MCP Gateway: mọi lệnh gọi công cụ qua 3 cửa — chặn cứng → ma trận rủi ro × môi trường → điểm tin cậy."""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import shlex
from dataclasses import dataclass, field

from . import datafilter, db
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


def evaluate(policy: dict, tier: str, tool: str, env: str, confidence: float, forced: list[str], canonical: str | None = None) -> Decision:
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


def _unseen_object(conn, ctx: CallContext, args: dict) -> str | None:
    target = namespace_of(args) or args.get("index")
    if not target:
        return None
    key_args = conn.execute(
        "SELECT args FROM tool_call_audit WHERE tenant_id=? AND env=? AND result_status='ok' ORDER BY id DESC LIMIT 500",
        (ctx.tenant_id, ctx.env)).fetchall()
    for r in key_args:
        past = json.loads(r["args"])
        if namespace_of(past) == target or past.get("index") == target:
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
        audit(conn, ctx, tool_name, args, "R3", 0.0, "blocked", ["công cụ chưa được đăng ký"], "blocked")
        return {**step, "decision": "blocked", "status": "blocked", "reasons": ["công cụ chưa được đăng ký"]}
    meta = dict(meta)
    tier = meta["risk_tier"]
    if ctx.roles is None:
        ctx.roles = user_roles(conn, ctx.tenant_id, ctx.user_id)
    allowed, why = callable_by(meta, ctx.env, ctx.roles, ctx.user_id)
    if not allowed:
        audit(conn, ctx, tool_name, args, tier, 0.0, "blocked", [why], "blocked")
        return {**step, "decision": "blocked", "status": "blocked", "risk_tier": tier, "reasons": [why]}
    from .tools import spec_of
    tool = spec_of(meta)
    missing = [f for f in tool.required if f not in args]
    if missing or not isinstance(args, dict):
        audit(conn, ctx, tool_name, args, tier, 0.0, "blocked", [f"thiếu tham số: {missing}"], "invalid")
        return {**step, "decision": "blocked", "status": "invalid", "risk_tier": tier, "reasons": [f"thiếu tham số: {missing}"]}
    if args.get("_filter") is not None:
        try:
            datafilter.check(args["_filter"])
        except datafilter.FilterError as exc:
            return {**step, "decision": "blocked", "status": "invalid", "risk_tier": tier, "reasons": [f"_filter: {exc}"]}

    dry_result = None
    dry_ok = 0.0
    if tool.check:
        try:
            dry_result = _run(conn, ctx.tenant_id, tool_name, args, check=True, user_id=ctx.user_id)
            dry_ok = 1.0
        except net.NetError as exc:
            dry_result = {"error": f"kết nối bị chặn: {exc}"}
        except (ConnectorError, ValueError, TypeError) as exc:
            dry_result = {"error": str(exc)}
        except Exception as exc:
            dry_result = {"error": f"chạy thử lỗi ({exc.__class__.__name__})"}

    tenant = conn.execute("SELECT change_freeze, write_quota_daily, self_approve FROM tenant WHERE id=?", (ctx.tenant_id,)).fetchone()
    forced: list[str] = list(ctx.forced)
    # Công cụ local chỉ xử lý dữ liệu đã lấy về; nội dung untrusted không lái được nó tác động ra ngoài.
    if ctx.tainted and not tool.local and not _auto_read_despite_taint(grant_policy(conn, ctx.tenant_id), ctx.env, meta["action_class"], tier):
        forced.append(UNTRUSTED_REASON)
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
    # Công cụ local không chạm môi trường nào, nên dùng cột dev của ma trận (R4 vẫn luôn phải hỏi).
    decision = evaluate(policy, tier, tool_name, "dev" if tool.local else ctx.env, conf, forced, meta.get("canonical"))
    step.update(risk_tier=tier, confidence=conf, confidence_parts=parts, reasons=decision.reasons, dry_run=dry_result)

    if decision.outcome == "block":
        audit(conn, ctx, tool_name, args, tier, conf, "blocked", decision.reasons, "blocked")
        return {**step, "decision": "blocked", "status": "blocked"}
    if decision.outcome == "ask" and tool.local and ctx.reviewed and not forced:
        # Code của data.script nằm cố định trong công cụ đã được duyệt; chỉ dữ liệu đầu vào thay đổi giữa các lần chạy.
        decision = Decision("auto", reasons=["bước của công cụ học được đã được duyệt"])
        step["reasons"] = decision.reasons

    # Khi tenant cho tự duyệt, một lần xác nhận của chính người yêu cầu là đủ, kể cả ô ma trận cần 2 người.
    required = 1 if tenant["self_approve"] else decision.required
    if decision.outcome == "ask" and required == 1 and grant_covers(grant_policy(conn, ctx.tenant_id), ctx.env, meta["action_class"], conf, forced) \
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
             required, db.iso(), db.later(minutes=settings.approval_ttl_minutes)))
        audit(conn, ctx, tool_name, args, tier, conf, "pending", decision.reasons, "pending")
        return {**step, "decision": "pending", "status": "pending", "approval_id": approval_id, "required_approvals": required}

    status, result = _execute(conn, ctx.tenant_id, ctx.env, tool_name, args, ctx.user_id)
    audit(conn, ctx, tool_name, args, tier, conf, "auto", decision.reasons, status)
    step.update(decision="auto", status=status, result=result, untrusted=tool.untrusted(args))
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


UNTRUSTED_REASON = "kế hoạch chịu ảnh hưởng từ nội dung untrusted"
GRANT_MODES = ("off", "read", "all")
# Grant "cho phép trong 1 giờ" có được vượt qua lý do untrusted không, theo môi trường. Tenant admin chỉnh được.
# untrusted_auto: mức cao nhất của lệnh đọc vẫn tự chạy khi kế hoạch chịu ảnh hưởng từ nội dung untrusted (none | R0 | R1).
AUTO_READ_TIERS = ("none", "R0", "R1")
DEFAULT_GRANT_POLICY = {"dev": {"untrusted": "read", "min_confidence": 0.0, "untrusted_auto": "R0"},
                        "staging": {"untrusted": "read", "min_confidence": 0.5, "untrusted_auto": "none"},
                        "prod": {"untrusted": "off", "min_confidence": 0.8, "untrusted_auto": "none"}}


def check_grant_policy(policy: dict) -> dict:
    """Gộp với mặc định và kiểm tra. 'all' ở prod bị cấm: log độc hại có thể lái agent gọi lệnh ghi mà không ai xác nhận."""
    if not isinstance(policy, dict) or set(policy) - set(ENVS):
        raise PolicyError(f"chính sách grant phải là object theo môi trường {list(ENVS)}")
    merged = copy.deepcopy(DEFAULT_GRANT_POLICY)
    for env, conf in policy.items():
        if not isinstance(conf, dict) or set(conf) - {"untrusted", "min_confidence", "untrusted_auto"}:
            raise PolicyError(f"{env}: chỉ gồm untrusted, min_confidence và untrusted_auto")
        mode = conf.get("untrusted", merged[env]["untrusted"])
        value = conf.get("min_confidence", merged[env]["min_confidence"])
        auto = conf.get("untrusted_auto", merged[env]["untrusted_auto"])
        if auto not in AUTO_READ_TIERS:
            raise PolicyError(f"{env}: untrusted_auto phải thuộc {list(AUTO_READ_TIERS)}")
        if env == "prod" and auto == "R1":
            raise PolicyError("prod chỉ cho lệnh đọc R0 tự chạy khi có nội dung untrusted")
        if mode not in GRANT_MODES:
            raise PolicyError(f"{env}: untrusted phải thuộc {list(GRANT_MODES)}")
        if env == "prod" and mode == "all":
            raise PolicyError("prod không được cho grant phủ lệnh ghi khi kế hoạch chịu ảnh hưởng từ nội dung untrusted")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise PolicyError(f"{env}: min_confidence phải nằm trong 0..1")
        merged[env] = {"untrusted": mode, "min_confidence": float(value), "untrusted_auto": auto}
    return merged


def grant_policy(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT grant_policy FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    try:
        return check_grant_policy(json.loads(r["grant_policy"]) if r and r["grant_policy"] else {})
    except (PolicyError, ValueError):
        return copy.deepcopy(DEFAULT_GRANT_POLICY)


def _auto_read_despite_taint(policy: dict, env: str, action_class: str, tier: str) -> bool:
    limit = (policy.get(env) or {}).get("untrusted_auto", "none")
    return action_class == "read" and limit != "none" and TIERS.index(tier) <= TIERS.index(limit)


def grant_covers(policy: dict, env: str, action_class: str, confidence: float, forced: list[str]) -> bool:
    """Grant phiên có thay được việc hỏi lại không. Chỉ lý do untrusted mới có thể được grant vượt qua."""
    if not forced:
        return True
    if any(reason != UNTRUSTED_REASON for reason in forced):
        return False
    rule = policy.get(env) or {"untrusted": "off", "min_confidence": 1.0}
    allowed = rule["untrusted"] == "all" or (rule["untrusted"] == "read" and action_class == "read")
    return allowed and confidence >= rule["min_confidence"]


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
            audit(conn, ctx, req["tool"], req["args"], req["risk_tier"], req["confidence"], "denied", [reason], "denied")
        else:
            decided = req["decided_by"] + [user_id]
            if len(decided) < req["required_approvals"]:
                conn.execute("UPDATE approval_request SET decided_by=? WHERE id=?", (json.dumps(decided), approval_id))
            else:
                status, result = _execute(conn, tenant_id, req["env"], req["tool"], req["args"], req["requester_id"])
                result = _compact(conn, tenant_id, req, meta, result)
                conn.execute("UPDATE approval_request SET status='approved', decided_by=?, decided_at=?, result=? WHERE id=?",
                             (json.dumps(decided), db.iso(), json.dumps({"status": status, "result": result}, ensure_ascii=False), approval_id))
                audit(conn, ctx, req["tool"], req["args"], req["risk_tier"], req["confidence"], "approved", decided, status)
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
    policy = grant_policy(conn, tenant_id)
    done = []
    rows = conn.execute("SELECT * FROM approval_request WHERE tenant_id=? AND requester_id=? AND tool=? AND env=? AND status='pending' "
                        "AND required_approvals=1 AND id!=? ORDER BY created_at", (tenant_id, req["requester_id"], req["tool"], req["env"], req["id"])).fetchall()
    for r in rows:
        other = db.row(r, "args", "decided_by", "reasons")
        if other["args"].get("namespace") != req["args"].get("namespace"):
            continue
        forced = [x for x in other["reasons"] if x == UNTRUSTED_REASON]
        if not grant_covers(policy, other["env"], meta["action_class"], other["confidence"], forced):
            continue
        status, result = _execute(conn, tenant_id, other["env"], other["tool"], other["args"], other["requester_id"])
        result = _compact(conn, tenant_id, other, meta, result)
        decided = other["decided_by"] + [user_id]
        conn.execute("UPDATE approval_request SET status='approved', decided_by=?, decided_at=?, result=? WHERE id=?",
                     (json.dumps(decided), db.iso(), json.dumps({"status": status, "result": result}, ensure_ascii=False), other["id"]))
        ctx = CallContext(tenant_id, other["requester_id"], other["env"], other["original_request"], other["episode_id"])
        audit(conn, ctx, other["tool"], other["args"], other["risk_tier"], other["confidence"], "approved", ["dùng quyền đã duyệt cho phiên"], status)
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
