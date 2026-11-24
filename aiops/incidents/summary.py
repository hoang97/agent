"""Tóm tắt cho lãnh đạo, viết lại khi sự cố đã yên sau hoạt động mới."""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from .. import db, live
from . import tasks
from .common import STAGE_LABEL, IncidentError, _cmd, _event, _lock, _running, _shown_stage, _summarizing, _summary_failed
from .config import bot_user, settings

log = logging.getLogger("aiops.incidents")


MAX_SUMMARIES_PER_DAY = 12  # sự cố kéo dài cả ngày, hoạt động lác đác, không gọi model liên tục


def last_activity(conn, inc) -> str | None:
    """Mốc hoạt động cuối: sự kiện của sự cố, lượt chat bắt đầu/xong, lệnh được quyết trong hội thoại của sự cố."""
    marks = [conn.execute("SELECT max(at) m FROM incident_event WHERE incident_id=? AND kind!='summarized'", (inc["id"],)).fetchone()["m"]]
    conv = inc["conversation_id"]
    if conv:
        marks.append(conn.execute("SELECT max(started_at) m FROM episode WHERE conversation_id=?", (conv,)).fetchone()["m"])
        marks.append(conn.execute("SELECT max(a.decided_at) m FROM approval_request a JOIN episode e ON e.id=a.episode_id WHERE e.conversation_id=?",
                                  (conv,)).fetchone()["m"])
        marks.append(live.finished_at(conv))
    marks = [m for m in marks if m]
    return max(marks) if marks else None


def summary_view(conn, inc) -> dict | None:
    if not inc["conversation_id"]:
        return None
    basis = last_activity(conn, inc)
    return {"text": inc["summary"], "at": inc["summary_at"], "stale": bool(inc["summary"] and basis and basis > (inc["summary_basis"] or "")),
            "running": inc["id"] in _summarizing}


def due_summaries(conn, tenant_id: str) -> list[tuple[str, str]]:
    """Sự cố cần viết lại tóm tắt: có hoạt động mới sau bản trước, đã yên idle_minutes, không có lượt nào đang chạy."""
    cfg = settings(conn, tenant_id)["summary"]
    if not cfg["enabled"]:
        return []
    now = db.now()
    quiet_since = db.iso(now - timedelta(minutes=cfg["idle_minutes"]))
    day_ago = db.iso(now - timedelta(days=1))
    out = []
    for inc in conn.execute("SELECT * FROM incident WHERE tenant_id=? AND conversation_id IS NOT NULL AND (status='open' OR updated_at>=?)",
                            (tenant_id, db.iso(now - timedelta(days=2)))).fetchall():
        if inc["id"] in _running or inc["id"] in _summarizing or live.current(inc["conversation_id"]):
            continue
        if not conn.execute("SELECT 1 FROM episode WHERE conversation_id=? LIMIT 1", (inc["conversation_id"],)).fetchone():
            continue  # chưa điều tra được lượt nào (hết quota, lỗi): chưa có gì để tóm tắt
        if inc["status"] == "closed" and conn.execute("SELECT 1 FROM incident_event WHERE incident_id=? AND kind='merged'", (inc["id"],)).fetchone():
            continue  # đã gộp vào sự cố khác: tóm tắt ở sự cố đích
        basis = last_activity(conn, inc)
        if not basis or basis > quiet_since or basis <= (inc["summary_basis"] or "") or _summary_failed.get(inc["id"]) == basis:
            continue
        if conn.execute("SELECT count(*) n FROM incident_event WHERE incident_id=? AND kind='summarized' AND at>=?",
                        (inc["id"], day_ago)).fetchone()["n"] >= MAX_SUMMARIES_PER_DAY:
            continue
        out.append((inc["id"], basis))
    return out


ACTION_STATUS = {"ok": "đã chạy", "pending": "đang chờ duyệt", "expired": "hết hạn duyệt, chưa chạy", "denied": "bị từ chối, chưa chạy", "skipped": "bị bỏ qua, chưa chạy",
                 "error": "chạy lỗi", "blocked": "bị chặn, chưa chạy", "invalid": "sai tham số, chưa chạy"}


def _summary_input(conn, inc) -> dict:
    from ..conversation import clean_answer
    from .actions import rows
    from .views import timeline  # views đọc summary_view: nhập muộn để tránh vòng
    alerts = [dict(r) for r in conn.execute("SELECT * FROM alert_event WHERE incident_id=? ORDER BY starts_at", (inc["id"],)).fetchall()]
    events = [{"at": e["at"], "kind": e["kind"], "text": f"{e['who']}: {e['text']}"} for e in timeline(conn, dict(inc), alerts)]
    bot = bot_user(conn, inc["tenant_id"])
    turns = []
    for ep in conn.execute("SELECT user_id, started_at, input, trace FROM episode WHERE conversation_id=? ORDER BY started_at", (inc["conversation_id"],)).fetchall():
        texts = [s["text"] for s in json.loads(ep["trace"]) if s.get("type") == "text"]
        turns.append({"at": ep["started_at"], "by": "aiops điều tra tự động" if ep["user_id"] == bot else "người vận hành",
                      "request": str(json.loads(ep["input"]).get("task") or "")[:400], "answer": clean_answer(texts[-1] if texts else "")[-1500:]})
    actions = []
    for a in rows(conn, inc["id"]):
        status = a["status"]
        if a["approval_id"] and status == "pending":
            req = conn.execute("SELECT status FROM approval_request WHERE id=?", (a["approval_id"],)).fetchone()
            status = req["status"] if req else status
        actions.append({"command": _cmd(a), "why": a["why"], "status": ACTION_STATUS.get(status, status),
                        "by": "aiops đề xuất" if a["origin"] == "investigation" else "người vận hành chạy trong chat", "auto": bool(a["auto"]),
                        "verified": None if a["verified"] is None else bool(a["verified"])})
    shown = _shown_stage(dict(inc))
    return {"now": db.iso(),
            "incident": {"title": inc["title"], "env": inc["env"], "severity": inc["severity"], "status": inc["status"],
                         "stage": STAGE_LABEL.get(shown, shown), "opened_at": inc["created_at"], "resolved_at": inc["resolved_at"],
                         "root_cause": inc["cause"] or inc["root_cause"]},
            "alerts": [{"alertname": a["alertname"], "severity": a["severity"], "status": a["status"], "starts_at": a["starts_at"], "resolved_at": a["resolved_at"],
                        "summary": (a["summary"] or "")[:200]} for a in alerts[:20]],
            "events": events[-40:], "actions": actions, "turns": turns[-8:],
            "human_tasks": [{"text": t["text"], "done": bool(t["done_at"])} for t in tasks.current(conn, inc["id"])],
            # Bản cũ dài quá giới hạn thì model bám theo và viết dài tiếp: bỏ, để viết lại từ đầu.
            "previous_summary": inc["summary"] if inc["summary"] and len(inc["summary"]) <= 450 else None}


def summarize(conn, tenant_id: str, incident_id: str, basis: str | None = None) -> dict:
    from .. import agent, llm
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    if not inc["conversation_id"]:
        raise IncidentError("sự cố chưa có hội thoại điều tra để tóm tắt")
    basis = basis or last_activity(conn, inc) or db.iso()
    quota = conn.execute("SELECT token_quota_daily FROM tenant WHERE id=?", (tenant_id,)).fetchone()["token_quota_daily"]
    if agent.tokens_today(conn, tenant_id) >= quota:
        _summary_failed[incident_id] = basis
        raise IncidentError("tenant đã hết quota token trong ngày", 429)
    data = _summary_input(conn, inc)
    with llm.collect_usage(tenant_id) as usage:
        try:
            out = llm.get_provider().incident_summary(data)
        except llm.LLMError as exc:
            _summary_failed[incident_id] = basis
            raise IncidentError(f"không viết được tóm tắt: {exc}", 502) from exc
        finally:
            if usage:
                llm.save_usage(conn, tenant_id, None, usage)
    _summary_failed.pop(incident_id, None)
    text = out["summary"]
    with db.tx(conn):
        conn.execute("UPDATE incident SET summary=?, summary_at=?, summary_basis=? WHERE id=?", (text, db.iso(), basis, incident_id))
        tasks.sync_from_summary(conn, tenant_id, incident_id, out.get("human_tasks") or [])
        _event(conn, tenant_id, incident_id, "summarized", text)  # giữ các bản cũ; không tính là hoạt động
    return summary_view(conn, conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone())


def start_summary(tenant_id: str, incident_id: str, basis: str) -> bool:
    with _lock:
        if incident_id in _summarizing:
            return False
        _summarizing.add(incident_id)

    def work():
        try:
            with db.session() as conn:
                summarize(conn, tenant_id, incident_id, basis)
        except IncidentError as exc:
            log.warning("tóm tắt sự cố %s: %s", incident_id, exc)
        except Exception:
            _summary_failed[incident_id] = basis
            log.exception("tóm tắt sự cố %s lỗi", incident_id)
        finally:
            with _lock:
                _summarizing.discard(incident_id)

    threading.Thread(target=work, name=f"summary-{incident_id[:8]}", daemon=True).start()
    return True
