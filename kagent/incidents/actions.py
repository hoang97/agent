"""Lệnh khắc phục của sự cố (bảng incident_action): đưa đề xuất qua gateway, ghi lệnh chạy trong chat, theo dõi duyệt và xác minh."""
from __future__ import annotations

import json
from datetime import timedelta

from .. import db
from .common import BOT, IncidentError, _event, _label, _later
from .rootcause import action_kind
from .config import settings
from .ingest import poll

DONE = ("ok", "error")


def strip_dry_run(args: dict) -> dict:
    """Gateway đã chạy thử trước khi hỏi; --dry-run trong lệnh được duyệt làm lệnh thật không bao giờ chạy."""
    import shlex
    command = args.get("command")
    if not isinstance(command, str) or "--dry-run" not in command:
        return args
    try:
        argv = shlex.split(command)
    except ValueError:
        return args
    return {**args, "command": shlex.join(a for a in argv if a != "--dry-run" and not a.startswith("--dry-run="))}


def result_line(result) -> str | None:
    """Một dòng kết quả cho dòng sự kiện, không gọi model: lỗi, không có thì dòng đầu stdout/stderr."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            pass
    if result is None:
        return None
    text = ""
    if isinstance(result, dict):
        text = next((str(result[k]) for k in ("error", "stdout", "stderr", "message") if result.get(k)), "")
    text = text or (result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
    line = next((x.strip() for x in text.splitlines() if x.strip()), "")
    return line[:200] or None


def _dump(value) -> str | None:
    return json.dumps(value, ensure_ascii=False)[:2000] if value is not None else None


def _actor(conn, user_id: str | None) -> str | None:
    if not user_id:
        return None
    r = conn.execute("SELECT username FROM app_user WHERE id=?", (user_id,)).fetchone()
    return "kagent" if r and r["username"] == BOT else user_id


def _proposed_text(row: dict) -> str:
    first = "pending" if row["approval_id"] else row["status"]  # trạng thái lúc đề xuất; row có thể đã được quyết sau đó
    outcome = {"pending": "cần duyệt", "blocked": "bị chặn", "invalid": "sai tham số"}.get(first, first)
    reasons = json.loads(row["reasons"] or "[]")
    return f"{_label(row)} → {outcome}" + (f": {'; '.join(reasons)}" if reasons else "")


def _data(row: dict) -> dict:
    """Dữ liệu để tính "ai làm" lúc đọc (common.who)."""
    return {"tool": row["tool"], "status": row["status"], "origin": row["origin"], "auto": bool(row["auto"])}


def _outcome_event(conn, row: dict) -> None:
    """Sự kiện kết quả của lệnh: chạy xong, lỗi, bị từ chối, hết hạn. Người duyệt là actor của chính dòng này."""
    label, status = _label(row), row["status"]
    if status == "ok":
        kind, actor, text = "ran", None, f"{label} → {row['result_line'] or 'đã chạy'}"
    elif status == "error":
        kind, actor, text = "failed", None, f"{label} → lỗi" + (f": {row['result_line']}" if row["result_line"] else "")
    elif status == "denied":
        kind, actor, text = "denied", row["decided_by"], f"{label} → từ chối" + (f": {row['deny_reason']}" if row["deny_reason"] else "")
    elif status == "expired":
        kind, actor, text = "expired", "system", f"{label} → hết hạn duyệt, chưa chạy"
    elif status == "skipped":
        kind, actor, text = "skipped", "system", f"{label} → bỏ qua vì hội thoại có tin nhắn mới, chưa chạy"
    else:
        return
    if actor is None:
        actor = _actor(conn, row["requested_by"]) if row["auto"] else row["decided_by"]
    _event(conn, row["tenant_id"], row["incident_id"], kind, text, _data(row), actor=actor, action_id=row["id"], at=row["ran_at"] or row["decided_at"])


def _emit(conn, row: dict) -> None:
    """Sự kiện của một lệnh: đề xuất (khi không tự chạy ngay), rồi kết quả nếu đã có."""
    if not row["auto"]:
        _event(conn, row["tenant_id"], row["incident_id"], "proposed", _proposed_text(row), _data(row),
               actor=_actor(conn, row["requested_by"]), action_id=row["id"], at=row["proposed_at"])
    _outcome_event(conn, row)


def _insert(conn, tenant_id: str, incident_id: str, origin: str, requested_by: str, step: dict, episode_id: str | None, why: str | None,
            at: str | None = None) -> dict:
    status = step["status"]
    now = at or db.iso()
    row = {"id": db.new_id(), "tenant_id": tenant_id, "incident_id": incident_id, "episode_id": episode_id, "approval_id": step.get("approval_id"),
           "origin": origin, "requested_by": requested_by, "tool": step["tool"], "args": json.dumps(step.get("args"), ensure_ascii=False),
           "why": why, "risk_tier": step.get("risk_tier"), "reasons": json.dumps(step.get("reasons") or [], ensure_ascii=False),
           "dry_run": _dump(step.get("dry_run")), "status": status, "auto": int(status in DONE), "proposed_at": now,
           "decided_by": None, "decided_at": None, "deny_reason": None, "ran_at": now if status in DONE else None,
           "result": _dump(step.get("result")), "result_line": result_line(step.get("result")) if status in DONE else None,
           "verified": None, "verified_at": None, "superseded_by": None, "kind": action_kind(step["tool"], step.get("args"), step.get("kind"))}
    conn.execute(f"INSERT INTO incident_action({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
    _emit(conn, row)
    return row


ACTION_EVENTS = ("proposed", "ran", "failed", "denied", "expired", "skipped")


def rebuild_events(conn) -> None:
    """Viết lại sự kiện của mọi lệnh từ incident_action theo giờ gốc (khi đổi dạng câu của sự kiện)."""
    with db.tx(conn):
        conn.execute("DELETE FROM incident_event WHERE action_id IS NOT NULL AND kind IN (%s)" % ",".join("?" * len(ACTION_EVENTS)), ACTION_EVENTS)
        for row in conn.execute("SELECT * FROM incident_action ORDER BY proposed_at, rowid").fetchall():
            _emit(conn, dict(row))


def _settle(conn, row: dict, req) -> dict | None:
    """Trạng thái mới của lệnh chờ duyệt theo approval_request; None khi chưa có quyết định."""
    if req is None or req["status"] == "pending":
        return None
    result = json.loads(req["result"]) if req["result"] else {}
    decided = json.loads(req["decided_by"] or "[]")
    status = result.get("status", "ok") if req["status"] == "approved" else req["status"]
    return {**row, "status": status, "decided_by": decided[-1] if decided and status != "skipped" else None, "decided_at": req["decided_at"],
            "deny_reason": req["deny_reason"], "ran_at": req["decided_at"] if status in DONE else None,
            "result": _dump(result.get("result")) if result.get("result") is not None else row["result"],
            "result_line": result_line(result.get("result")) if status in DONE else None}


def _save(conn, row: dict) -> None:
    conn.execute("UPDATE incident_action SET status=?, decided_by=?, decided_at=?, deny_reason=?, ran_at=?, result=?, result_line=? WHERE id=?",
                 (row["status"], row["decided_by"], row["decided_at"], row["deny_reason"], row["ran_at"], row["result"], row["result_line"], row["id"]))


def rows(conn, incident_id: str, origin: str | None = None) -> list[dict]:
    sql = "SELECT * FROM incident_action WHERE incident_id=?" + (" AND origin=?" if origin else "") + " ORDER BY proposed_at, rowid"
    return [dict(r) for r in conn.execute(sql, (incident_id, origin) if origin else (incident_id,)).fetchall()]


def act(conn, tenant_id: str, incident_id: str, report: dict, user: str, episode_id: str | None, alerts: list[dict] | None = None,
        steps: list[dict] | None = None) -> dict:
    """Đưa hành động đề xuất qua gateway. Tự xử lý đang tắt hay đã quá số lần thì ép hỏi người duyệt.

    Auto mode xét hành động theo luật tự xử lý của admin cùng tên và nhãn cảnh báo, không theo báo cáo hay kết quả điều tra
    vì cả hai được viết từ log và dữ liệu người ngoài ghi được.
    """
    from .. import gateway
    cfg = settings(conn, tenant_id)
    inc = conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone()
    env = inc["env"]
    if not report["actions"]:
        with db.tx(conn):
            conn.execute("UPDATE incident SET stage='reported', updated_at=? WHERE id=?", (db.iso(), incident_id))
            _event(conn, tenant_id, incident_id, "no_action", "không có hành động an toàn để đề xuất; cần người xem", actor="kagent")
        return report
    rule = cfg["auto"][env]
    reasons = []
    if not rule["enabled"]:
        reasons.append(f"tự xử lý đang tắt ở {env}")
    hour_ago = (db.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    auto_count = conn.execute("SELECT count(*) n FROM incident_action WHERE incident_id=? AND origin='investigation' AND auto=1 AND proposed_at>=?",
                              (incident_id, hour_ago)).fetchone()["n"]
    if auto_count >= cfg["max_auto_actions_per_hour"]:
        reasons.append(f"đã tự xử lý {cfg['max_auto_actions_per_hour']} lần trong 1 giờ cho sự cố này")
    if conn.execute("SELECT 1 FROM incident_action WHERE incident_id=? AND origin='investigation' AND verified=0", (incident_id,)).fetchone():
        reasons.append("lần tự xử lý trước không làm hết cảnh báo")
    intent = [f"Tự xử lý sự cố ở môi trường {env}, chỉ trong phạm vi các luật sau của admin:\n" + "\n".join(f"- {r}" for r in cfg["auto_rules"]),
              "Cảnh báo của sự cố (tên và nhãn): " + json.dumps([{"alertname": a.get("alertname"), "labels": a.get("labels")} for a in alerts or []],
                                                               ensure_ascii=False)[:3000]]
    calls = [{"tool": s["tool"], "args": s.get("args"), "status": s.get("status")} for s in steps or []]
    ctx = gateway.CallContext(tenant_id, user, env, f"Xử lý sự cố: {inc['title']}", episode_id, intent=intent, calls=calls, escalate_on_block=True,
                              roles=gateway.user_roles(conn, tenant_id, user), forced=reasons)
    done = []
    for a in report["actions"]:
        a = {**a, "args": strip_dry_run(a["args"])}
        step = gateway.invoke(conn, ctx, a["tool"], a["args"])  # như agent: không bọc giao dịch quanh lệnh gọi mạng
        with db.tx(conn):
            done.append(_insert(conn, tenant_id, incident_id, "investigation", user, {**step, "kind": a.get("kind")}, episode_id, a["why"]))
        if step["status"] != "ok":
            break  # bước sau dựa trên bước trước: chưa chạy được thì dừng
    stage = "verifying" if any(d["status"] == "ok" for d in done) else "awaiting_approval" if any(d["status"] == "pending" for d in done) else "needs_human"
    with db.tx(conn):
        conn.execute("UPDATE incident SET stage=?, verify_due=?, updated_at=? WHERE id=?",
                     (stage, _later(cfg["verify_minutes"] * 60) if stage == "verifying" else None, db.iso(), incident_id))
    return report


def is_dry_run(args) -> bool:
    command = args.get("command") if isinstance(args, dict) else None
    return isinstance(command, str) and "--dry-run" in command and "--dry-run=none" not in command


def record_chat_action(conn, ctx, step: dict) -> None:
    """Gateway gọi khi lệnh ghi trong hội thoại của sự cố đã chạy hoặc thành yêu cầu duyệt. Lệnh chạy thử không đổi gì: bỏ qua."""
    if is_dry_run(step.get("args")):
        return
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM incident WHERE id=? AND tenant_id=?", (ctx.incident_id, ctx.tenant_id)).fetchone():
            _insert(conn, ctx.tenant_id, ctx.incident_id, "chat", ctx.user_id, step, ctx.episode_id, step.get("why"))
            if step["status"] == "ok":
                _verify_after_fix(conn, ctx.tenant_id, ctx.incident_id)


def _verify_after_fix(conn, tenant_id: str, incident_id: str) -> None:
    """Người chạy lệnh khắc phục trong chat: chờ verify_minutes rồi xem cảnh báo đã hết chưa, như lệnh do điều tra đề xuất."""
    conn.execute("UPDATE incident SET stage='verifying', verify_due=?, updated_at=? WHERE id=? AND status='open' "
                 "AND stage IN ('new','reported','needs_human','error','handled')",
                 (_later(settings(conn, tenant_id)["verify_minutes"] * 60), db.iso(), incident_id))


def _sync(conn, tenant_id: str, incident_id: str) -> None:
    """Lệnh chờ duyệt của sự cố đã được quyết: cập nhật trạng thái; lệnh do điều tra đề xuất đã chạy thì chuyển sang xác minh."""
    cfg = settings(conn, tenant_id)
    changed, executed, open_ = False, False, False
    for row in conn.execute("SELECT * FROM incident_action WHERE incident_id=? AND status='pending' AND approval_id IS NOT NULL",
                            (incident_id,)).fetchall():
        row = dict(row)
        req = conn.execute("SELECT * FROM approval_request WHERE id=?", (row["approval_id"],)).fetchone()
        new = _settle(conn, row, req)
        if new is None:
            open_ = open_ or row["origin"] == "investigation"
            continue
        with db.tx(conn):
            _save(conn, new)
            _outcome_event(conn, new)
        if row["origin"] == "investigation":
            changed = True
            executed = executed or new["status"] == "ok"
        elif new["status"] == "ok":
            with db.tx(conn):
                _verify_after_fix(conn, tenant_id, incident_id)
    inc = conn.execute("SELECT stage FROM incident WHERE id=?", (incident_id,)).fetchone()
    if changed and inc["stage"] == "awaiting_approval":
        stage = "verifying" if executed else "awaiting_approval" if open_ else "needs_human"
        with db.tx(conn):
            conn.execute("UPDATE incident SET stage=?, verify_due=?, updated_at=? WHERE id=?",
                         (stage, _later(cfg["verify_minutes"] * 60) if executed else None, db.iso(), incident_id))


def on_approval(conn, tenant_id: str, approval_id: str) -> None:
    """Gọi ngay sau khi có người duyệt/từ chối, để sự cố không phải đợi vòng nền."""
    for r in conn.execute("SELECT DISTINCT incident_id FROM incident_action WHERE approval_id=? AND tenant_id=?", (approval_id, tenant_id)).fetchall():
        _sync(conn, tenant_id, r["incident_id"])


RETRYABLE = {"expired": "đã hết hạn duyệt", "denied": "đã bị từ chối", "error": "đã chạy lỗi"}


def _retryable(conn, tenant_id: str, incident_id: str, action_id: str) -> dict:
    row = conn.execute("SELECT * FROM incident_action WHERE id=? AND incident_id=? AND tenant_id=?", (action_id, incident_id, tenant_id)).fetchone()
    if row is None:
        raise IncidentError("không tìm thấy lệnh của sự cố", 404)
    if row["status"] not in RETRYABLE or row["superseded_by"]:
        raise IncidentError("lệnh này không còn chờ xử lý", 409)
    return dict(row)


def retry(conn, tenant_id: str, incident_id: str, action_id: str, user: str) -> dict:
    """Đưa lại lệnh đã hết hạn, bị từ chối hay chạy lỗi qua gateway dưới tên người bấm. Luôn tạo thẻ duyệt: người bấm nhờ đề xuất, chưa đồng ý chạy."""
    from .. import gateway
    old = _retryable(conn, tenant_id, incident_id, action_id)
    inc = conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone()
    label = _label(old)
    ctx = gateway.CallContext(tenant_id, user, inc["env"], f"Đề xuất lại lệnh cho sự cố: {inc['title']}", inc["episode_id"],
                              intent=[f"Đề xuất lại lệnh {label} cho sự cố {inc['title']}"], escalate_on_block=True,
                              roles=gateway.user_roles(conn, tenant_id, user), forced=[f"người dùng nhờ đề xuất lại lệnh {RETRYABLE[old['status']]}"])
    step = gateway.invoke(conn, ctx, old["tool"], json.loads(old["args"]))
    with db.tx(conn):
        new = _insert(conn, tenant_id, incident_id, old["origin"], user, {**step, "kind": old["kind"]}, inc["episode_id"], old["why"])
        if new["status"] in ("pending", "ok"):
            _supersede(conn, old, new["id"])
        if inc["status"] == "open" and inc["stage"] not in ("investigating", "verifying") and new["status"] in ("pending", "ok"):
            stage = "verifying" if new["status"] == "ok" else "awaiting_approval"
            conn.execute("UPDATE incident SET stage=?, verify_due=?, updated_at=? WHERE id=?",
                         (stage, _later(settings(conn, tenant_id)["verify_minutes"] * 60) if stage == "verifying" else None, db.iso(), incident_id))
    return new


def dismiss(conn, tenant_id: str, incident_id: str, action_id: str) -> None:
    """Người xem quyết định không cần lệnh này nữa: rời khỏi "Đang chờ". superseded_by='dismissed' thay cho id lệnh thay thế."""
    _supersede(conn, _retryable(conn, tenant_id, incident_id, action_id), "dismissed")


def _supersede(conn, old: dict, by: str) -> None:
    """Đánh dấu cả các lần trước của cùng lệnh (cùng nhãn), để chúng không hiện lại ở "Đang chờ"."""
    label = _label(old)
    for r in conn.execute("SELECT * FROM incident_action WHERE incident_id=? AND superseded_by IS NULL AND status IN (%s)" % ",".join("?" * len(RETRYABLE)),
                          (old["incident_id"], *RETRYABLE)).fetchall():
        if _label(dict(r)) == label:
            conn.execute("UPDATE incident_action SET superseded_by=? WHERE id=?", (by, r["id"]))


def has_pending(conn, tenant_id: str) -> bool:
    return conn.execute("SELECT 1 FROM incident_action WHERE tenant_id=? AND status='pending' LIMIT 1", (tenant_id,)).fetchone() is not None


def follow_up(conn, tenant_id: str) -> None:
    """Theo dõi duyệt và xác minh: lệnh được duyệt thì chờ xác minh; tới hạn thì xem cảnh báo đã hết chưa."""
    cfg = settings(conn, tenant_id)
    for r in conn.execute("SELECT DISTINCT incident_id FROM incident_action WHERE tenant_id=? AND status='pending'", (tenant_id,)).fetchall():
        _sync(conn, tenant_id, r["incident_id"])
    now = db.iso()
    due = conn.execute("SELECT * FROM incident WHERE tenant_id=? AND stage='verifying' AND verify_due<=? AND status!='closed'", (tenant_id, now)).fetchall()
    if due and not cfg["enabled"] and cfg["sources"]:
        poll(conn, tenant_id)  # luồng đang tắt thì không ai poll; cần số liệu mới để xác minh
    for inc in due:
        left = conn.execute("SELECT count(*) n FROM alert_event WHERE incident_id=? AND status='firing'", (inc["id"],)).fetchone()["n"]
        with db.tx(conn):
            # Cả lệnh người chạy trong hội thoại: lệnh sửa của người cũng là căn cứ cho mức nguyên nhân (rootcause.level).
            conn.execute("UPDATE incident_action SET verified=?, verified_at=? WHERE incident_id=? AND status='ok' "
                         "AND verified IS NULL", (int(left == 0), now, inc["id"]))
            conn.execute("UPDATE incident SET stage=?, verify_due=NULL, updated_at=? WHERE id=?", ("handled" if left == 0 else "needs_human", now, inc["id"]))
            _event(conn, tenant_id, inc["id"], "verified", "đã xác minh: cảnh báo đã hết" if left == 0 else
                   f"sau {cfg['verify_minutes']} phút vẫn còn {left} cảnh báo; chuyển cho người xử lý", actor="kagent")


def migrate(conn) -> None:
    """Chép incident.actions (JSON) và lệnh ghi người dùng đã chạy trong hội thoại của sự cố sang incident_action, kèm sự kiện nhật ký.

    Chạy một lần khi bảng vừa được tạo. Giờ sự kiện lấy theo lúc lệnh được đề xuất/quyết, không theo lúc chép.
    """
    with db.tx(conn):
        for inc in conn.execute("SELECT id, tenant_id, actions, conversation_id FROM incident").fetchall():
            bot = conn.execute("SELECT id FROM app_user WHERE username=?", (BOT,)).fetchone()
            bot_id = bot["id"] if bot else BOT
            seen: set[str] = set()
            for a in json.loads(inc["actions"] or "[]"):
                result = a.get("result")
                try:
                    result = json.loads(result) if isinstance(result, str) else result
                except ValueError:
                    pass
                # Có approval_id thì trạng thái lấy lại từ approval_request: JSON cũ có thể còn "pending" khi yêu cầu đã hết hạn.
                step = {**a, "status": "pending" if a.get("approval_id") else a["status"], "result": result}
                row = _insert(conn, inc["tenant_id"], inc["id"], "investigation", bot_id, step, a.get("episode_id"), a.get("why"), at=a.get("at"))
                if a.get("approval_id"):
                    seen.add(a["approval_id"])
                    new = _settle(conn, row, conn.execute("SELECT * FROM approval_request WHERE id=?", (a["approval_id"],)).fetchone())
                    if new:
                        _save(conn, new)
                        _outcome_event(conn, new)
                if a.get("verified") is not None:
                    conn.execute("UPDATE incident_action SET verified=? WHERE id=?", (int(a["verified"]), row["id"]))
            if inc["conversation_id"]:
                _migrate_chat(conn, inc, seen)
            conn.execute("UPDATE incident_event SET actor='kagent' WHERE incident_id=? AND kind IN ('investigated','no_action','verified') AND actor IS NULL",
                         (inc["id"],))


def _migrate_chat(conn, inc, seen: set[str]) -> None:
    for ep in conn.execute("SELECT id, user_id, started_at, trace FROM episode WHERE conversation_id=? ORDER BY started_at",
                           (inc["conversation_id"],)).fetchall():
        for s in json.loads(ep["trace"]):
            if s.get("type") != "tool_call" or s.get("action_class") in (None, "read") or s.get("status") not in ("ok", "error", "pending"):
                continue
            if s.get("approval_id") in seen:
                continue
            row = _insert(conn, inc["tenant_id"], inc["id"], "chat", ep["user_id"], s, ep["id"], s.get("why"), at=ep["started_at"])
            if s["status"] == "pending" and s.get("approval_id"):
                new = _settle(conn, row, conn.execute("SELECT * FROM approval_request WHERE id=?", (s["approval_id"],)).fetchone())
                if new:
                    _save(conn, new)
                    _outcome_event(conn, new)
