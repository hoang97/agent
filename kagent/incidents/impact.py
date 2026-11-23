"""Mức S1–S4 theo ảnh hưởng tới khách hàng và có bắt buộc postmortem không, theo quy trình của repo incidents.

kagent không biết khách hàng nào bị ảnh hưởng (chưa có mapping khách hàng ↔ project), nên mức chính thức do người trả lời vài câu hỏi;
khi chưa có câu trả lời chỉ có mức gợi ý từ môi trường và mức cảnh báo, ghi rõ là gợi ý. Chưa chắc thì chọn mức cao hơn, hạ sau.
Mức và điều kiện postmortem tính lúc đọc từ câu trả lời đã lưu, để đổi luật không cần đánh giá lại.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .. import db
from .common import IncidentError, _event

CUSTOMERS = {"many": "nhiều khách hàng", "few": "vài khách hàng hoặc một tính năng", "none": "chưa ảnh hưởng khách hàng",
             "internal": "chỉ ảnh hưởng nội bộ", "unknown": "chưa biết"}
FLAGS = ("data_loss", "control_plane", "redundancy_lost", "missed_by_alert", "near_miss", "spread")
LABELS = {"S1": "Critical", "S2": "High", "S3": "Medium", "S4": "Low"}
POSTMORTEM_DAYS = 5  # ngày làm việc tính từ lúc dịch vụ phục hồi (incident::mitigated)
RECUR_DAYS = 30  # bằng grouping.RELATED_DAYS: lịch sử cùng nhóm lấy từ related()


def answers(inc) -> dict | None:
    raw = inc["impact"] if "impact" in inc.keys() else None
    return json.loads(raw) if raw else None


def grade(a: dict) -> tuple[str, str]:
    """(mức, lý do) từ câu trả lời của người."""
    if a.get("data_loss"):
        return "S1", "mất dữ liệu luôn là S1"
    customers = a.get("customers")
    if customers == "many":
        return "S1", "nhiều khách hàng bị ảnh hưởng"
    if customers == "few":
        return "S2", "vài khách hàng hoặc một tính năng bị ảnh hưởng"
    if a.get("control_plane"):
        return "S2", "lỗi control plane là ảnh hưởng khách hàng (khách hàng không gọi được API dù workload vẫn chạy)"
    if customers == "unknown":
        return "S2", "chưa chắc ảnh hưởng khách hàng: chọn mức cao hơn, hạ sau khi đánh giá xong"
    if a.get("redundancy_lost"):
        return "S3", "chưa ảnh hưởng khách hàng nhưng mất dự phòng"
    return "S4", "chỉ ảnh hưởng nội bộ" if customers == "internal" else "chưa ảnh hưởng khách hàng, không mất dự phòng"


def suggest(inc) -> tuple[str, str]:
    if inc["env"] in ("dev", "staging", "test"):
        return "S4", f"môi trường {inc['env']}, chỉ ảnh hưởng nội bộ"
    if (inc["severity"] or "") in ("critical",):
        return "S2", "cảnh báo critical ở môi trường thật, chưa biết ảnh hưởng khách hàng: chọn mức cao hơn"
    return "S3", "cảnh báo ở môi trường thật, chưa biết ảnh hưởng khách hàng"


def _add_workdays(start: datetime, days: int) -> datetime:
    d = start
    while days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def _recurring(conn, inc) -> int:
    """Số sự cố cùng nhóm trong RELATED_DAYS (30) ngày, tính cả ca này và các lần mở lại, gộp vào."""
    from .grouping import related
    return 1 + (inc["repeats"] or 0) + sum(r.get("count") or 1 for r in related(conn, inc["tenant_id"], inc["id"])["history"])


def assess(conn, inc) -> dict:
    a = answers(inc)
    sev, reason = grade(a) if a else suggest(inc)
    reasons = []
    if sev in ("S1", "S2"):
        reasons.append(f"mức {sev}")
    if a and a.get("missed_by_alert"):
        reasons.append("cảnh báo không phát hiện sự cố")
    if a and a.get("near_miss"):
        reasons.append("suýt thành sự cố lớn")
    if a and a.get("spread"):
        reasons.append("lan sang service khác hoặc khó xử lý")
    n = _recurring(conn, inc) if sev == "S3" else 0
    if n >= 2:
        reasons.append(f"S3 lặp lại {n} lần trong {RECUR_DAYS} ngày")
    due = None
    if reasons and inc["resolved_at"]:
        due = db.iso(_add_workdays(datetime.fromisoformat(inc["resolved_at"].replace("Z", "+00:00")), POSTMORTEM_DAYS))
    return {"sev": sev, "label": LABELS[sev], "reason": reason, "source": "human" if a else "suggested", "answers": a,
            "postmortem": {"required": bool(reasons), "reasons": reasons, "due": due, "suggested_only": not a and bool(reasons)}}


def save(conn, tenant_id: str, incident_id: str, user: str, data: dict) -> dict:
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    if data.get("customers") not in CUSTOMERS:
        raise IncidentError(f"customers phải là một trong {list(CUSTOMERS)}")
    a = {"customers": data["customers"], **{k: bool(data.get(k)) for k in FLAGS},
         "note": re.sub(r"\s+", " ", str(data.get("note") or "")).strip()[:300], "by": user, "at": db.iso()}
    old = assess(conn, inc)
    conn.execute("UPDATE incident SET impact=?, updated_at=? WHERE id=?", (json.dumps(a, ensure_ascii=False), db.iso(), incident_id))
    new = assess(conn, conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone())
    change = f"{old['sev']} → {new['sev']}" if old["source"] == "human" and old["sev"] != new["sev"] else new["sev"]
    _event(conn, tenant_id, incident_id, "impact_assessed", f"đánh giá ảnh hưởng: {change} ({new['reason']})" + (f"; {a['note']}" if a["note"] else ""),
           {"sev": new["sev"]}, actor=user)
    return new
