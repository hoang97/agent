"""Nguyên nhân gốc theo 5 Whys: chuỗi tại sao có căn cứ, dừng ở điểm yếu sửa được của hệ thống, và mức đã xử lý tới đâu.

Mức tính lúc đọc từ dữ liệu sẵn có, không lưu, để đổi luật không cần chạy lại:
1 mitigated: cảnh báo đã hết, nguyên nhân có thể chưa sửa;
2 rõ nguyên nhân kỹ thuật: có mắt xích có căn cứ và một lệnh sửa (không phải lệnh giảm nhẹ) kiểm tra đạt, hoặc người xác nhận chuỗi;
3 rõ nguyên nhân gốc: người xác nhận, chuỗi dừng ở điểm yếu sửa được, và có việc phòng ngừa.
Sự cố sau trong RELATED_DAYS ngày có cùng nguyên nhân thì ca này không còn được coi là đã xử lý quá mức 1.
"""
from __future__ import annotations

import json
import logging
import re
import shlex

from .. import db
from .common import IncidentError, _event

log = logging.getLogger("kagent.incidents")

LEVELS = {0: "chưa rõ nguyên nhân", 1: "đã dập triệu chứng", 2: "rõ nguyên nhân kỹ thuật", 3: "rõ nguyên nhân gốc"}
FACTOR_LABEL = {"worse": "làm nặng hơn", "detect": "phát hiện chậm", "recover": "phục hồi lâu"}
_K8S_MITIGATE = re.compile(r"^(rollout restart|delete (pod|pods|po)\b|scale\b|uncordon\b|cordon\b|drain\b)")
_OS_MITIGATE = {"reboot", "os-start", "os-stop", "pause", "unpause", "suspend", "resume"}


def is_mitigation(tool: str, args) -> bool:
    """Lệnh chỉ làm hết triệu chứng. Luật cứng thắng nhãn kind của báo cáo: model hay gọi restart là "sửa"."""
    args = args if isinstance(args, dict) else {}
    command = args.get("command")
    if isinstance(command, str):
        try:
            words = [w for w in shlex.split(command) if w != "kubectl"]
        except ValueError:
            words = command.split()
        return bool(_K8S_MITIGATE.match(" ".join(words)))
    body = args.get("body")
    return isinstance(body, dict) and bool(set(body) & _OS_MITIGATE)


def action_kind(tool: str, args, proposed: str | None = None) -> str:
    return "mitigate" if is_mitigation(tool, args) else proposed or "fix"


def chain(inc) -> dict:
    """Chuỗi đã lưu; sự cố điều tra trước khi có 5 Whys thì rỗng."""
    raw = inc["whys"] if "whys" in inc.keys() else None
    data = json.loads(raw) if raw else {}
    return {"whys": data.get("whys") or [], "stop": data.get("stop") or "no_evidence", "next_question": data.get("next_question") or "",
            "factors": data.get("factors") or []}


def level(conn, inc) -> dict:
    c = chain(inc)
    grounded = any(w.get("steps") or "CB" in (w.get("refs") or []) for w in c["whys"])
    fixed = conn.execute("SELECT 1 FROM incident_action WHERE incident_id=? AND status='ok' AND verified=1 AND coalesce(kind,'fix')='fix' LIMIT 1",
                         (inc["id"],)).fetchone() is not None
    confirmed = bool(inc["rc_confirmed_at"])
    prevent = conn.execute("SELECT 1 FROM incident_task WHERE incident_id=? AND source='prevent' AND removed_at IS NULL LIMIT 1",
                           (inc["id"],)).fetchone() is not None
    weakness = bool(c["whys"]) and bool(c["whys"][-1].get("weakness"))
    n, notes = (1 if inc["status"] != "open" else 0), []
    if (grounded and fixed) or confirmed:
        n = 2
    if confirmed and weakness and prevent:
        n = 3
    if n == 2 and not confirmed:
        notes.append("chưa có người xác nhận chuỗi")
    if confirmed and not weakness:
        notes.append("chuỗi chưa dừng ở điểm yếu sửa được")
    if confirmed and weakness and not prevent:
        notes.append("chưa có việc phòng ngừa")
    recurred = None
    if inc["rc_recurred_by"]:
        r = conn.execute("SELECT id, title FROM incident WHERE id=?", (inc["rc_recurred_by"],)).fetchone()
        recurred = dict(r) if r else {"id": inc["rc_recurred_by"], "title": "(sự cố không còn)"}
        if n > 1:
            notes.append("nguyên nhân lặp lại ở sự cố sau, chưa xử lý triệt để")
            n = 1
    return {"level": n, "label": LEVELS[n], "notes": notes, "grounded": grounded, "fixed": fixed, "confirmed": confirmed,
            "weakness": weakness, "prevent": prevent, "recurred": recurred}


def save_report(conn, tenant_id: str, incident_id: str, report: dict, steps: list[dict]) -> None:
    """Lưu chuỗi của lượt điều tra mới: mã S<n> đổi thành (công cụ, tham số) của bước, để bản ghi học tri thức đánh lại mã theo bước.
    Lượt mới thay chuỗi cũ nên xác nhận cũ hết hiệu lực; việc "trả lời tiếp" cũ chưa xong thì bỏ."""
    def resolve(refs):
        out = []
        for r in refs:
            m = re.fullmatch(r"S(\d+)", r)
            if m and 0 < int(m.group(1)) <= len(steps):
                st = steps[int(m.group(1)) - 1]
                out.append({"tool": st["tool"], "args": st.get("args") or {}})
        return out
    whys = [{**w, "steps": resolve(w["refs"]), "refs": [r for r in w["refs"] if r == "CB" or re.fullmatch(r"S\d+", r)]}
            for w in report.get("whys") or []]
    factors = [{**f, "steps": resolve(f["refs"])} for f in report.get("factors") or []]
    data = {"whys": whys, "stop": report.get("stop") or "no_evidence", "next_question": report.get("next_question") or "", "factors": factors}
    now = db.iso()
    prev = conn.execute("SELECT rc_confirmed_at FROM incident WHERE id=?", (incident_id,)).fetchone()
    conn.execute("UPDATE incident SET whys=?, rc_confirmed_by=NULL, rc_confirmed_at=NULL WHERE id=?", (json.dumps(data, ensure_ascii=False), incident_id))
    if prev and prev["rc_confirmed_at"]:
        _event(conn, tenant_id, incident_id, "rc_unconfirmed", "lượt điều tra mới thay chuỗi 5 Whys đã xác nhận; cần xác nhận lại", actor="kagent")
    conn.execute("UPDATE incident_task SET removed_at=? WHERE incident_id=? AND source='why' AND done_at IS NULL AND removed_at IS NULL", (now, incident_id))
    if whys and data["stop"] == "no_evidence" and data["next_question"]:
        conn.execute("INSERT INTO incident_task(id,tenant_id,incident_id,text,source,created_at) VALUES (?,?,?,?, 'why', ?)",
                     (db.new_id(), tenant_id, incident_id, f"Trả lời tiếp 5 Whys: {data['next_question']}"[:200], now))


def confirm(conn, tenant_id: str, incident_id: str, user: str, whys: list[dict] | None, prevent: str) -> dict:
    """Người xác nhận (có thể đã sửa) chuỗi. Mắt xích người viết thêm hay sửa câu trả lời không còn căn cứ của agent: người đứng ra bảo đảm."""
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    c = chain(inc)
    agent_links = sum(1 for w in c["whys"] if not w.get("by_user"))
    kept = agent_links
    if whys is not None:
        old = {w["answer"]: w for w in c["whys"]}
        edited = []
        for w in whys[:7]:
            answer = re.sub(r"\s+", " ", str(w.get("answer") or "")).strip()[:600]
            if not answer:
                continue
            base = old.get(answer, {})
            edited.append({"question": re.sub(r"\s+", " ", str(w.get("question") or base.get("question") or "Tại sao?")).strip()[:300],
                           "answer": answer, "refs": base.get("refs", []), "steps": base.get("steps", []), "weakness": bool(w.get("weakness")),
                           "by_user": not base})
        for w in edited[:-1]:
            w["weakness"] = False  # chỉ mắt xích cuối là điểm dừng
        kept = sum(1 for w in edited if not w["by_user"] and not old[w["answer"]].get("by_user"))
        c["whys"] = edited
    if not c["whys"]:
        raise IncidentError("chuỗi 5 Whys trống: cần ít nhất một mắt xích")
    c["stop"] = "weakness" if c["whys"][-1]["weakness"] else "no_evidence"
    if c["stop"] == "weakness":
        c["next_question"] = ""
    now = db.iso()
    conn.execute("UPDATE incident SET whys=?, rc_confirmed_by=?, rc_confirmed_at=? WHERE id=?",
                 (json.dumps(c, ensure_ascii=False), user, now, incident_id))
    if c["stop"] == "weakness":
        conn.execute("UPDATE incident_task SET removed_at=? WHERE incident_id=? AND source='why' AND done_at IS NULL AND removed_at IS NULL", (now, incident_id))
    prevent = re.sub(r"\s+", " ", prevent or "").strip()
    if prevent and not conn.execute("SELECT 1 FROM incident_task WHERE incident_id=? AND source='prevent' AND text=? AND removed_at IS NULL",
                                    (incident_id, prevent[:200])).fetchone():
        conn.execute("INSERT INTO incident_task(id,tenant_id,incident_id,text,source,created_at,created_by) VALUES (?,?,?,?, 'prevent', ?, ?)",
                     (db.new_id(), tenant_id, incident_id, prevent[:200], now, user))
    from .quality import infer_from_confirm
    infer_from_confirm(conn, tenant_id, incident_id, user, agent_links, kept)
    last = c["whys"][-1]["answer"]
    _event(conn, tenant_id, incident_id, "rc_confirmed", f"xác nhận chuỗi 5 Whys ({len(c['whys'])} mắt xích): {last[:200]}", actor=user)
    return level(conn, conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone())


def _brief(inc) -> dict:
    c = chain(inc)
    return {"id": inc["id"], "title": inc["title"], "cause": (inc["cause"] or inc["root_cause"] or "")[:600],
            "whys": [w["answer"][:300] for w in c["whys"]]}


def check_recurrence(conn, tenant_id: str, incident_id: str, provider) -> list[str]:
    """Sau lượt điều tra có chuỗi: sự cố cùng nhóm trong RELATED_DAYS ngày có cùng nguyên nhân thì hạ mức của chúng và giảm điểm
    bài học rút từ chúng. Cùng cảnh báo khác nguyên nhân không tính. Trả về id các sự cố cũ bị đánh dấu."""
    from .grouping import related
    from .. import llm
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None or not (chain(inc)["whys"] or inc["cause"]):
        return []
    ids = [r["id"] for r in related(conn, tenant_id, incident_id)["history"]]
    olds = [conn.execute("SELECT * FROM incident WHERE id=?", (i,)).fetchone() for i in ids]
    olds = [o for o in olds if o is not None and o["created_at"] < inc["created_at"] and not o["rc_recurred_by"] and (o["cause"] or o["whys"])][:8]
    if not olds:
        return []
    try:
        same = provider.same_cause(_brief(inc), [_brief(o) for o in olds])
    except llm.LLMError as exc:
        log.warning("không so được nguyên nhân sự cố %s: %s", incident_id, exc)
        return []
    with db.tx(conn):
        for old_id in same:
            old = next(o for o in olds if o["id"] == old_id)
            conn.execute("UPDATE incident SET rc_recurred_by=? WHERE id=?", (incident_id, old_id))
            _event(conn, tenant_id, old_id, "rc_recurred", f"nguyên nhân lặp lại ở sự cố sau: {inc['title']}", {"incident_id": incident_id}, actor="kagent")
            _event(conn, tenant_id, incident_id, "rc_same_cause", f"cùng nguyên nhân với sự cố trước: {old['title']}", {"incident_id": old_id}, actor="kagent")
            _blame_lessons(conn, tenant_id, old_id)
    return same


def _blame_lessons(conn, tenant_id: str, incident_id: str) -> None:
    """Bài học (lesson, insight) rút từ ca nay lặp lại: cách sửa hay kết luận của nó chưa đủ, cộng một lần có hại."""
    from .. import knowledge
    reason = f"nguyên nhân của sự cố {incident_id} lặp lại"
    docs = [r["id"] for r in conn.execute("SELECT id FROM source_doc WHERE tenant_id=? AND kind='incident' AND ref LIKE ?", (tenant_id, f"{incident_id}@%"))]
    for doc_id in docs:
        for r in conn.execute("SELECT DISTINCT k.id FROM knowledge_unit k, json_each(k.source_eps) e WHERE k.tenant_id=? AND e.value LIKE ? "
                              "AND k.kind IN ('lesson','insight') AND k.status IN ('candidate','active')", (tenant_id, f"doc:{doc_id}#%")).fetchall():
            ku = knowledge.get(conn, tenant_id, r["id"])
            if conn.execute("SELECT 1 FROM ku_event WHERE tenant_id=? AND ku_id=? AND reason=? LIMIT 1", (tenant_id, ku["id"], reason)).fetchone():
                continue
            merged = {**ku, "harmful_cnt": ku["harmful_cnt"] + 1}
            knowledge.update(conn, tenant_id, ku["id"], {"harmful_cnt": merged["harmful_cnt"], "confidence": knowledge.recompute_confidence(merged)},
                             "UPDATE", "incident", reason)
            if merged["harmful_cnt"] > merged["helpful_cnt"] and ku["status"] == "active":
                knowledge.update(conn, tenant_id, ku["id"], {"status": "deprecated", "valid_to": db.iso()}, "DEPRECATE", "incident", reason)
