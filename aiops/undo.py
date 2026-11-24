"""Hoàn tác cho lệnh ghi: chỉ cho tự chạy khi rollback được, và rollback đó đáng tin.

Mỗi connector khai `undo: <module>` trong template; module đó lập kế hoạch cho từng lần gọi từ trạng thái đọc được ngay trước khi chạy,
không ai phải viết playbook cho từng hành động. Ô judge của ma trận chỉ tự chạy khi kế hoạch đạt:
  (a) hoàn tác do code dựng từ snapshot, không do model viết;
  (b) chỉ chạm đúng đối tượng lệnh gốc đổi, đưa về đúng trạng thái đã chụp;
  (c) có bằng chứng chạy được: chạy thử lệnh hoàn tác qua, đã từng hoàn tác thành công (thật hoặc diễn tập), hoặc admin đánh dấu tin cậy;
  và preflight không thấy trạng thái bất thường (đang rollout dở, đối tượng đang có thay đổi khác chờ kiểm tra).
Sau khi chạy: chờ một khoảng rồi kiểm tra đối tượng sẵn sàng và không có cảnh báo mới; không đạt thì tự hoàn tác lệnh đã tự chạy.

Module connector cung cấp:
  plan_undo(p, tool, args) -> dict          kế hoạch (xem PLAN_KEYS), bắt buộc
  run_op(p, op) -> dict                     chạy một bước do code dựng (lệnh hoàn tác, xoá mềm, sao lưu), bắt buộc
  after_change(p, plan, result) -> dict     bổ sung kế hoạch sau khi chạy (id vừa tạo, phiên bản sau khi đổi)
  check_change(p, plan) -> {"ready", "detail"}  đối tượng đã sẵn sàng chưa (None: không đọc được)
  drift(p, plan) -> [str]                   đối tượng đã bị sửa sau lệnh: hoàn tác lúc này sẽ đè thay đổi của người khác
  same_as_before(p, plan) -> bool | None    sau khi hoàn tác, trạng thái có đúng bản đã chụp (dùng khi diễn tập)
"""
from __future__ import annotations

import importlib
import json
import logging

from . import db

log = logging.getLogger("aiops.undo")

DEFAULTS = {"verify_minutes": 5, "auto_revert": True, "trash_days": 7, "max_rows": 100_000,
            "drill": {"dev": True, "staging": False, "prod": False}, "trusted": [], "paused": False}
MAX_TARGETS = 5
FINAL = ("reverted", "purged")
OPEN = ("verifying", "ok", "check_failed", "applied", "conflict", "revert_failed")
PLAN_KEYS = {"recipe", "summary", "reversible", "why", "scoped", "scope", "targets", "snapshot", "undo", "instead", "prepare", "purge",
             "undo_dry", "preflight", "forced", "noop", "soft", "after", "namespaces", "backup", "after_ops"}


class UndoError(Exception):
    def __init__(self, message: str, status: int = 400, data: dict | None = None):
        super().__init__(message)
        self.status = status
        self.data = data or {}


# ---------- cài đặt của tenant ----------

def check_settings(value) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError(f"chỉ gồm {sorted(DEFAULTS)}")
    out = settings_merge(value)
    for key, lo, hi in (("verify_minutes", 1, 120), ("trash_days", 1, 90), ("max_rows", 0, 10_000_000)):
        if not isinstance(out[key], int) or isinstance(out[key], bool) or not lo <= out[key] <= hi:
            raise ValueError(f"{key} phải là số nguyên từ {lo} đến {hi}")
    for key in ("auto_revert", "paused"):
        if not isinstance(out[key], bool):
            raise ValueError(f"{key} phải là true/false")
    if not isinstance(out["drill"], dict) or set(out["drill"]) - {"dev", "staging", "prod"} or not all(isinstance(v, bool) for v in out["drill"].values()):
        raise ValueError("drill gồm dev, staging, prod với giá trị true/false")
    if out["drill"].get("prod"):
        raise ValueError("không diễn tập hoàn tác ở prod")
    if not isinstance(out["trusted"], list) or not all(isinstance(r, str) and 0 < len(r) <= 80 for r in out["trusted"]):
        raise ValueError("trusted là danh sách tên loại hoàn tác")
    return out


def settings_merge(value: dict | None) -> dict:
    out = {**DEFAULTS, **(value or {})}
    out["drill"] = {**DEFAULTS["drill"], **((value or {}).get("drill") or {})}
    return out


def settings_of(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT undo_settings FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    try:
        return settings_merge(json.loads(r["undo_settings"]) if r and r["undo_settings"] else {})
    except ValueError:
        return dict(DEFAULTS)


def save_settings(conn, tenant_id: str, value: dict) -> dict:
    merged = check_settings({**settings_of(conn, tenant_id), **value})
    conn.execute("UPDATE tenant SET undo_settings=? WHERE id=?", (json.dumps(merged, ensure_ascii=False), tenant_id))
    return merged


def paused(conn, tenant_id: str) -> bool:
    return bool(settings_of(conn, tenant_id)["paused"])


# ---------- lập kế hoạch ----------

def provider(spec):
    return importlib.import_module(f"aiops.connectors.{spec.undo}") if spec is not None and spec.undo else None


def _connection(conn, tenant_id: str, tool_name: str, user_id: str | None = None):
    """(ToolSpec, params cho module hoàn tác). _template để module dùng chung cho nhiều template (sql, silence) biết mình đang ở đâu."""
    from . import connections
    _, _, spec, params = connections.resolve(conn, tenant_id, tool_name)
    cfg = settings_of(conn, tenant_id)
    return spec, {**params, "_tenant_id": tenant_id, "_user_id": user_id, "_template": spec.template,
                  "_undo_max_rows": cfg["max_rows"], "_undo_trash_days": cfg["trash_days"]}


def _not(why: str, **extra) -> dict:
    return {"reversible": False, "why": why, "recipe": extra.pop("recipe", ""), **extra}


def plan(conn, tenant_id: str, tool_name: str, args: dict, env: str) -> dict:
    """Kế hoạch hoàn tác của một lần gọi. Không bao giờ ném lỗi: lập không được thì là không hoàn tác được."""
    try:
        spec, params = _connection(conn, tenant_id, tool_name)
    except Exception as exc:
        return _not(f"không đọc được kết nối ({exc})")
    mod = provider(spec)
    if mod is None:
        return _not("công cụ không có cách hoàn tác" + (f": {spec.rollback_hint}" if spec and spec.rollback_hint else ""))
    try:
        out = mod.plan_undo(params, spec.name, {k: v for k, v in args.items() if k not in ("_filter", "_why")})
    except Exception as exc:  # lỗi của connector chỉ làm lệnh mất quyền tự chạy
        log.info("không lập được kế hoạch hoàn tác cho %s: %s", tool_name, exc)
        return _not(f"không lập được kế hoạch hoàn tác: {str(exc)[:300]}")
    out = {k: v for k, v in (out or {}).items() if k in PLAN_KEYS}
    out.setdefault("reversible", False)
    out.setdefault("recipe", "")
    out.setdefault("targets", [])
    out["recipe"] = f"{spec.template}.{out['recipe']}" if out["recipe"] and "." not in out["recipe"] else out["recipe"]
    if out["reversible"] and len(out["targets"]) > MAX_TARGETS:
        out["scoped"], out["scope"] = False, f"chạm {len(out['targets'])} đối tượng, quá {MAX_TARGETS}"
    busy = _busy(conn, tenant_id, out["targets"])
    if busy:
        out.setdefault("preflight", []).append(f"{busy} đang có thay đổi khác chờ kiểm tra")
    return out


def _busy(conn, tenant_id: str, targets: list[dict]) -> str | None:
    keys = {t["key"] for t in targets if t.get("key")}
    if not keys:
        return None
    for r in conn.execute("SELECT plan FROM change_record WHERE tenant_id=? AND status='verifying'", (tenant_id,)).fetchall():
        for t in json.loads(r["plan"]).get("targets", []):
            if t.get("key") in keys:
                return t.get("label") or t["key"]
    return None


def evidence(conn, tenant_id: str, plan_: dict) -> list[str]:
    recipe = plan_.get("recipe")
    out = []
    if plan_.get("noop"):
        out.append("không có trạng thái cần hoàn tác")
    if plan_.get("undo_dry"):
        out.append("chạy thử lệnh hoàn tác qua")
    if recipe:
        n = conn.execute("SELECT count(*) c FROM undo_evidence WHERE tenant_id=? AND recipe=? AND source='revert'", (tenant_id, recipe)).fetchone()["c"]
        d = conn.execute("SELECT count(*) c FROM undo_evidence WHERE tenant_id=? AND recipe=? AND source='drill'", (tenant_id, recipe)).fetchone()["c"]
        if n:
            out.append(f"đã hoàn tác thành công {n} lần")
        if d:
            out.append(f"đã diễn tập {d} lần")
        if recipe in settings_of(conn, tenant_id)["trusted"]:
            out.append("admin đánh dấu tin cậy")
    return out


def checks(conn, tenant_id: str, plan_: dict) -> list[dict]:
    """Bốn điều kiện để lệnh ghi được tự chạy, theo thứ tự hiện trên giao diện."""
    if not plan_.get("reversible"):
        return [{"ok": False, "key": "a", "text": "Không hoàn tác được: " + (plan_.get("why") or "chưa rõ")}]
    out = [{"ok": True, "key": "a", "text": "Hoàn tác được: " + (plan_.get("summary") or plan_.get("recipe") or "")}]
    out.append({"ok": plan_.get("scoped", True), "key": "b",
                "text": ("Chỉ chạm đúng " if plan_.get("scoped", True) else "Phạm vi quá rộng: ") + (plan_.get("scope") or _targets_text(plan_))})
    proof = evidence(conn, tenant_id, plan_)
    out.append({"ok": bool(proof), "key": "c",
                "text": ("Bằng chứng: " + " · ".join(proof)) if proof else f"Chưa có bằng chứng: loại hoàn tác {plan_.get('recipe')} chưa từng chạy ở tenant"})
    for problem in plan_.get("preflight") or []:
        out.append({"ok": False, "key": "d", "text": "Trạng thái hiện tại: " + problem})
    return out


def _targets_text(plan_: dict) -> str:
    labels = [t.get("label") or t.get("key") for t in plan_.get("targets") or []]
    return ", ".join(labels[:MAX_TARGETS]) or "đối tượng của lệnh"


def eligible(checks_: list[dict]) -> bool:
    return bool(checks_) and all(c["ok"] for c in checks_)


def public(plan_: dict | None, checks_: list[dict] | None = None) -> dict | None:
    """Phần hiện cho người dùng và agent; không có snapshot (có thể chứa dữ liệu nghiệp vụ)."""
    if plan_ is None:
        return None
    return {"reversible": bool(plan_.get("reversible")), "summary": plan_.get("summary") if plan_.get("reversible") else plan_.get("why"),
            "recipe": plan_.get("recipe"), "soft": bool(plan_.get("soft")), "checks": checks_ or [],
            "targets": [t.get("label") or t.get("key") for t in plan_.get("targets") or []]}


def for_classifier(plan_: dict | None) -> dict | None:
    if plan_ is None:
        return None
    return {"reversible": bool(plan_.get("reversible")), "how": (plan_.get("summary") if plan_.get("reversible") else plan_.get("why")) or ""}


# ---------- chạy lệnh kèm kế hoạch ----------

def _ops(mod, p, ops: list[dict]) -> list:
    return [mod.run_op(p, op) for op in ops or []]


def execute(conn, tenant_id: str, env: str, tool_name: str, args: dict, user_id: str | None, plan_: dict | None, run_tool,
            *, auto: bool, episode_id: str | None = None, approval_id: str | None = None, incident_id: str | None = None) -> tuple[str, dict, str | None]:
    """Chạy lệnh qua kế hoạch: bước chuẩn bị (sao lưu), lệnh gốc hoặc bản xoá mềm thay thế, ghi change_record.

    run_tool() chạy lệnh gốc qua gateway và trả (status, result). Kế hoạch không hoàn tác được thì chạy thẳng, không ghi gì.
    """
    if not plan_ or not plan_.get("reversible"):
        status, result = run_tool()
        return status, result, None
    try:
        spec, p = _connection(conn, tenant_id, tool_name, user_id)
        mod = provider(spec)
    except Exception as exc:
        return "error", {"error": f"không mở được kết nối để chạy kèm hoàn tác: {exc}"}, None
    try:
        _ops(mod, p, plan_.get("prepare"))
    except Exception as exc:  # sao lưu lỗi thì không chạy lệnh: chạy rồi sẽ không hoàn tác được
        return "error", {"error": f"bước chuẩn bị hoàn tác lỗi, lệnh chưa chạy: {str(exc)[:500]}"}, None
    if plan_.get("instead"):
        try:
            results = _ops(mod, p, plan_["instead"])
            status, result = "ok", {**(results[-1] if results and isinstance(results[-1], dict) else {}), "soft_deleted": True}
        except Exception as exc:
            status, result = "error", {"error": str(exc)[:1000]}
    else:
        status, result = run_tool()
    if status != "ok":
        return status, result, None
    if hasattr(mod, "after_change"):
        try:
            plan_ = mod.after_change(p, plan_, result) or plan_
        except Exception as exc:
            log.warning("after_change %s lỗi: %s", tool_name, exc)
            plan_ = {**plan_, "undo": None, "after_error": str(exc)[:300]}
    cfg = settings_of(conn, tenant_id)
    change_id = db.new_id()
    can_check = hasattr(mod, "check_change") or bool(plan_.get("namespaces"))
    status_ = "verifying" if can_check and not plan_.get("soft") else "applied"
    purge_at = db.later(days=cfg["trash_days"]) if plan_.get("soft") else None
    conn.execute(
        "INSERT INTO change_record(id,tenant_id,episode_id,approval_id,incident_id,user_id,env,tool,args,recipe,plan,auto,status,verify_until,created_at,purge_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (change_id, tenant_id, episode_id, approval_id, incident_id, user_id or "", env, tool_name, json.dumps(args, ensure_ascii=False),
         plan_.get("recipe") or "", json.dumps(plan_, ensure_ascii=False, default=str), int(auto), status_,
         db.later(minutes=cfg["verify_minutes"]) if status_ == "verifying" else None, db.iso(), purge_at))
    if plan_.get("soft") and isinstance(result, dict):
        result = {**result, "purge_at": purge_at, "note": f"xoá mềm: khôi phục được tới {purge_at[:10]}, sau đó người có quyền xoá thật ở thùng rác"}
    if cfg["drill"].get(env) and plan_.get("undo") and not evidence(conn, tenant_id, {"recipe": plan_.get("recipe")}):
        _drill(conn, tenant_id, change_id, mod, p, plan_, run_tool)
    return status, result, change_id


def _drill(conn, tenant_id: str, change_id: str, mod, p: dict, plan_: dict, run_tool) -> None:
    """Diễn tập lần đầu của một loại hoàn tác (chỉ dev/staging): hoàn tác, so với bản chụp, rồi chạy lại lệnh gốc."""
    try:
        _ops(mod, p, plan_["undo"])
        same = mod.same_as_before(p, plan_) if hasattr(mod, "same_as_before") else True
    except Exception as exc:
        _note(conn, change_id, {"drill": f"diễn tập hoàn tác lỗi: {str(exc)[:300]}"})
        return
    redo = _ops(mod, p, plan_["instead"]) if plan_.get("instead") else None
    status, result = ("ok", redo) if redo is not None else run_tool()
    if status != "ok":
        conn.execute("UPDATE change_record SET status='reverted', reverted_at=?, reverted_by='drill', revert_result=? WHERE id=?",
                     (db.iso(), json.dumps({"drill": "đã hoàn tác khi diễn tập nhưng chạy lại lệnh gốc lỗi", "result": result}, ensure_ascii=False, default=str), change_id))
        return
    if hasattr(mod, "after_change"):
        try:
            fresh = mod.after_change(p, plan_, result if isinstance(result, dict) else {}) or plan_
            conn.execute("UPDATE change_record SET plan=? WHERE id=?", (json.dumps(fresh, ensure_ascii=False, default=str), change_id))
        except Exception:
            pass
    if same is False:
        _note(conn, change_id, {"drill": "diễn tập: trạng thái sau khi hoàn tác khác bản đã chụp"})
        return
    conn.execute("INSERT INTO undo_evidence(tenant_id,recipe,source,change_id,at) VALUES (?,?,?,?,?)", (tenant_id, plan_.get("recipe"), "drill", change_id, db.iso()))
    _note(conn, change_id, {"drill": "đã diễn tập hoàn tác và chạy lại lệnh gốc"})


def _note(conn, change_id: str, data: dict) -> None:
    r = conn.execute("SELECT check_result FROM change_record WHERE id=?", (change_id,)).fetchone()
    cur = json.loads(r["check_result"]) if r and r["check_result"] else {}
    conn.execute("UPDATE change_record SET check_result=? WHERE id=?", (json.dumps({**cur, **data}, ensure_ascii=False), change_id))


# ---------- kiểm tra sau khi chạy ----------

def new_alerts(conn, tenant_id: str, since: str, plan_: dict) -> list[str]:
    """Cảnh báo bắt đầu sau lệnh, trên namespace hoặc đối tượng của lệnh (đọc từ luồng cảnh báo đã kết nối)."""
    spaces = set(plan_.get("namespaces") or [])
    names = {t.get("name") for t in plan_.get("targets") or [] if t.get("name")}
    if not spaces and not names:
        return []
    out = []
    for r in conn.execute("SELECT alertname, labels FROM alert_event WHERE tenant_id=? AND status='firing' AND first_seen>=?", (tenant_id, since)).fetchall():
        labels = json.loads(r["labels"] or "{}")
        values = {str(v) for v in labels.values()}
        if labels.get("namespace") in spaces or values & names:
            out.append(r["alertname"] or "?")
    return sorted(set(out))


def tick(conn) -> None:
    for r in conn.execute("SELECT * FROM change_record WHERE status='verifying'").fetchall():
        try:
            _verify(conn, dict(r))
        except Exception:
            log.exception("lỗi khi kiểm tra thay đổi %s", r["id"])


def _verify(conn, row: dict) -> None:
    plan_ = json.loads(row["plan"])
    alerts = new_alerts(conn, row["tenant_id"], row["created_at"], plan_)
    due = row["verify_until"] and row["verify_until"] <= db.iso()
    if not alerts and not due:
        return
    ready, detail = None, ""
    if not alerts:
        try:
            spec, params = _connection(conn, row["tenant_id"], row["tool"])
            mod = provider(spec)
            if hasattr(mod, "check_change"):
                state = mod.check_change(params, plan_)
                ready, detail = state.get("ready"), state.get("detail") or ""
        except Exception as exc:
            detail = f"không đọc được trạng thái ({str(exc)[:200]})"
    failed = bool(alerts) or ready is False
    result = {"alerts": alerts, "ready": ready, "detail": detail, "at": db.iso()}
    if alerts:
        result["detail"] = f"{len(alerts)} cảnh báo mới: {', '.join(alerts)}"
    elif ready is False:
        result["detail"] = detail or "đối tượng chưa sẵn sàng"
    conn.execute("UPDATE change_record SET status=?, check_result=? WHERE id=?",
                 ("check_failed" if failed else "ok", json.dumps({**_old_note(row), **result}, ensure_ascii=False), row["id"]))
    _incident_event(conn, row, "change_checked" if not failed else "change_check_failed",
                    f"Kiểm tra sau khi chạy {_label(row)}: " + ("ổn" + (f", {detail}" if detail else "") if not failed else result["detail"]))
    if failed and row["auto"]:
        cfg = settings_of(conn, row["tenant_id"])
        if cfg["auto_revert"] and not cfg["paused"] and plan_.get("undo"):
            try:
                revert(conn, row["tenant_id"], row["id"], None, auto=True)
            except UndoError as exc:
                log.info("không tự hoàn tác được %s: %s", row["id"], exc)


def _old_note(row: dict) -> dict:
    try:
        return json.loads(row.get("check_result") or "{}")
    except ValueError:
        return {}


def _label(row: dict) -> str:
    args = json.loads(row["args"]) if isinstance(row.get("args"), str) else row.get("args") or {}
    cmd = args.get("command") or args.get("sql") or args.get("index") or args.get("matchers") or args.get("silence_id") or args.get("path") or ""
    return f"{row['tool'].split('.', 1)[-1]} {str(cmd)[:120]}".strip()


def _incident_event(conn, row: dict, kind: str, text: str, actor: str | None = "system") -> None:
    if not row.get("incident_id"):
        return
    from .incidents.common import _event
    _event(conn, row["tenant_id"], row["incident_id"], kind, text, {"change_id": row["id"]}, actor=actor)


# ---------- hoàn tác ----------

def get(conn, tenant_id: str, change_id: str) -> dict:
    r = conn.execute("SELECT * FROM change_record WHERE id=? AND tenant_id=?", (change_id, tenant_id)).fetchone()
    if r is None:
        raise UndoError("không tìm thấy thay đổi", 404)
    return dict(r)


def can_revert(conn, row: dict, user_id: str, roles: set[str], is_org_admin: bool) -> bool:
    from .gateway import can_approve
    return user_id == row["user_id"] or can_approve(roles, row["env"], is_org_admin)


def revert(conn, tenant_id: str, change_id: str, user_id: str | None, *, auto: bool = False, force: bool = False) -> dict:
    """Chạy lệnh hoàn tác đã lưu. Đối tượng đã bị sửa sau lệnh thì dừng và trả danh sách khác biệt; người xem xong chọn force."""
    row = get(conn, tenant_id, change_id)
    if row["status"] in FINAL:
        raise UndoError(f"thay đổi đã ở trạng thái {row['status']}", 409)
    plan_ = json.loads(row["plan"])
    if not plan_.get("undo"):
        raise UndoError("thay đổi này không có lệnh hoàn tác", 409)
    spec, p = _connection(conn, tenant_id, row["tool"], user_id)
    mod = provider(spec)
    differences = mod.drift(p, plan_) if hasattr(mod, "drift") else []
    if differences and (auto or not force):
        if auto:
            conn.execute("UPDATE change_record SET status='conflict', revert_result=? WHERE id=?",
                         (json.dumps({"drift": differences}, ensure_ascii=False), change_id))
            _incident_event(conn, row, "change_check_failed", f"Không tự hoàn tác {_label(row)}: đối tượng đã bị sửa sau lệnh")
        raise UndoError("đối tượng đã bị sửa sau lệnh; hoàn tác lúc này sẽ đè thay đổi đó", 409, {"drift": differences})
    who = "auto" if auto else user_id
    try:
        results = _ops(mod, p, plan_["undo"])
    except Exception as exc:
        conn.execute("UPDATE change_record SET status='revert_failed', revert_result=?, reverted_by=? WHERE id=?",
                     (json.dumps({"error": str(exc)[:1000]}, ensure_ascii=False), who, change_id))
        _incident_event(conn, row, "revert_failed", f"Hoàn tác {_label(row)} lỗi: {str(exc)[:200]}", actor=None if auto else user_id)
        _audit(conn, row, who, "error")
        raise UndoError(f"hoàn tác lỗi: {str(exc)[:500]}", 502) from exc
    conn.execute("UPDATE change_record SET status='reverted', reverted_at=?, reverted_by=?, revert_result=? WHERE id=?",
                 (db.iso(), who, json.dumps({"results": results, **({"forced_over": differences} if differences else {})}, ensure_ascii=False, default=str), change_id))
    conn.execute("INSERT INTO undo_evidence(tenant_id,recipe,source,change_id,at) VALUES (?,?,?,?,?)", (tenant_id, row["recipe"], "revert", change_id, db.iso()))
    _audit(conn, row, who, "ok")
    reason = "tự hoàn tác vì kiểm tra sau khi chạy không đạt" if auto else "hoàn tác"
    _incident_event(conn, row, "reverted", f"{reason.capitalize()}: {_label(row)} → {plan_.get('summary') or 'đã đưa về trạng thái trước'}",
                    actor="system" if auto else user_id)
    return view(conn, get(conn, tenant_id, change_id))


def _audit(conn, row: dict, who: str | None, status: str) -> None:
    conn.execute("INSERT INTO tool_call_audit(tenant_id,episode_id,user_id,tool,env,args_hash,args,risk_tier,confidence,decision,reasons,result_status,at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (row["tenant_id"], row["episode_id"], who if who and who != "auto" else row["user_id"], "undo:" + row["tool"], row["env"], row["id"],
                  json.dumps({"change_id": row["id"]}), "", None, "auto" if who == "auto" else "approved", "[]", status, db.iso()))


def purge(conn, tenant_id: str, change_id: str, user_id: str) -> dict:
    """Xoá thật đối tượng đang trong thùng rác. Người bấm là người duyệt."""
    row = get(conn, tenant_id, change_id)
    plan_ = json.loads(row["plan"])
    if not plan_.get("soft") or row["status"] in FINAL:
        raise UndoError("thay đổi này không có gì trong thùng rác", 409)
    spec, p = _connection(conn, tenant_id, row["tool"], user_id)
    mod = provider(spec)
    try:
        _ops(mod, p, plan_.get("purge"))
    except Exception as exc:
        raise UndoError(f"xoá thật lỗi: {str(exc)[:500]}", 502) from exc
    conn.execute("UPDATE change_record SET status='purged', purged_at=?, purged_by=? WHERE id=?", (db.iso(), user_id, change_id))
    _audit(conn, {**row, "tool": row["tool"]}, user_id, "ok")
    return view(conn, get(conn, tenant_id, change_id))


# ---------- hiển thị ----------

def view(conn, row: dict | None) -> dict | None:
    if row is None:
        return None
    plan_ = json.loads(row["plan"]) if isinstance(row["plan"], str) else row["plan"]
    who = row.get("reverted_by")
    if who and who not in ("auto", "drill"):
        u = conn.execute("SELECT username FROM app_user WHERE id=?", (who,)).fetchone()
        who = u["username"] if u else who
    return {"id": row["id"], "tool": row["tool"], "env": row["env"], "args": json.loads(row["args"]) if isinstance(row["args"], str) else row["args"],
            "recipe": row["recipe"], "status": row["status"], "auto": bool(row["auto"]), "created_at": row["created_at"], "verify_until": row["verify_until"],
            "summary": plan_.get("summary"), "soft": bool(plan_.get("soft")), "can_undo": bool(plan_.get("undo")) and row["status"] not in FINAL,
            "targets": [t.get("label") or t.get("key") for t in plan_.get("targets") or []],
            "check": json.loads(row["check_result"]) if row.get("check_result") else None,
            "revert": json.loads(row["revert_result"]) if row.get("revert_result") else None,
            "reverted_at": row.get("reverted_at"), "reverted_by": who, "purge_at": row.get("purge_at"), "purged_at": row.get("purged_at"),
            "incident_id": row.get("incident_id"), "approval_id": row.get("approval_id")}


def trash(conn, tenant_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM change_record WHERE tenant_id=? AND purge_at IS NOT NULL AND status NOT IN ('reverted','purged') "
                        "ORDER BY created_at DESC LIMIT 200", (tenant_id,)).fetchall()
    return [view(conn, dict(r)) for r in rows]


def recipes(conn, tenant_id: str) -> dict[str, dict]:
    """Số bằng chứng theo loại hoàn tác, cho trang Công cụ & Chính sách."""
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT recipe, source, count(*) c FROM undo_evidence WHERE tenant_id=? GROUP BY recipe, source", (tenant_id,)).fetchall():
        out.setdefault(r["recipe"], {"revert": 0, "drill": 0})[r["source"]] = r["c"]
    return out
