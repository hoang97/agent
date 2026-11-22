"""Chất lượng gom cảnh báo, đo trên dữ liệu thật (trang Đo lường) và trong scripts/grouping_replay.py.

- Tỉ lệ nén: số cảnh báo đã vào sự cố / số sự cố còn lại sau khi gộp.
- Gom thừa: số lần người tách cảnh báo ra / số sự cố đã mở. Gom thiếu: số lần người gộp tay / số sự cố đã mở.
- Số lần hỏi LLM để gom trên mỗi cảnh báo, và thời gian từ cảnh báo đầu tiên bắn tới lúc mở sự cố (trung vị).
"""
from __future__ import annotations

import statistics
from datetime import timedelta

from .. import db
from .scoring import parse_ts


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def pairwise(predicted: dict[str, str], truth: dict[str, str]) -> dict:
    """Precision/recall theo cặp: hai cảnh báo cùng sự cố hay không. Đếm qua bảng chéo nên chạy được với hàng chục nghìn cảnh báo."""
    def pairs(counts) -> int:
        return sum(n * (n - 1) // 2 for n in counts)

    def sizes(mapping: dict[str, str]) -> dict:
        out: dict = {}
        for alert, group in mapping.items():
            out[group] = out.get(group, 0) + 1
        return out

    ids = [a for a in truth if a in predicted]
    both: dict[tuple, int] = {}
    for a in ids:
        key = (predicted[a], truth[a])
        both[key] = both.get(key, 0) + 1
    tp = pairs(both.values())
    pred = pairs(sizes({a: predicted[a] for a in ids}).values())
    real = pairs(sizes({a: truth[a] for a in ids}).values())
    p = tp / pred if pred else 1.0
    r = tp / real if real else 1.0
    return {"alerts": len(ids), "precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0,
            "predicted_groups": len(set(predicted[a] for a in ids)), "true_groups": len(set(truth[a] for a in ids))}


def grouping(conn, tenant_id: str, days: int = 30) -> dict:
    since = db.iso(db.now() - timedelta(days=days))
    alerts = conn.execute("SELECT count(*) n, coalesce(sum(incident_id IS NOT NULL),0) attached, coalesce(sum(flapping),0) flapping "
                          "FROM alert_event WHERE tenant_id=? AND first_seen>=?", (tenant_id, since)).fetchone()
    opened = conn.execute("SELECT count(*) n FROM incident WHERE tenant_id=? AND created_at>=?", (tenant_id, since)).fetchone()["n"]
    kept = conn.execute("SELECT count(*) n FROM incident i WHERE tenant_id=? AND created_at>=? AND NOT EXISTS "
                        "(SELECT 1 FROM incident_event e WHERE e.incident_id=i.id AND e.kind='merged')", (tenant_id, since)).fetchone()["n"]
    events = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, count(*) n FROM incident_event WHERE tenant_id=? AND at>=? "
                                                      "AND kind IN ('split','merge','auto_merge','reopened') GROUP BY kind", (tenant_id, since)).fetchall()}
    calls = conn.execute("SELECT count(*) n FROM llm_call WHERE tenant_id=? AND purpose='correlate' AND at>=?", (tenant_id, since)).fetchone()["n"]
    methods = {r["m"]: r["n"] for r in conn.execute("SELECT coalesce(join_method,'none') m, count(*) n FROM alert_event WHERE tenant_id=? AND first_seen>=? "
                                                    "GROUP BY m", (tenant_id, since)).fetchall()}
    waits = []
    for r in conn.execute("SELECT i.created_at, min(a.starts_at) first FROM incident i JOIN alert_event a ON a.incident_id=i.id "
                          "WHERE i.tenant_id=? AND i.created_at>=? GROUP BY i.id", (tenant_id, since)).fetchall():
        start, made = parse_ts(r["first"]), parse_ts(r["created_at"])
        if start and made and made >= start:
            waits.append((made - start).total_seconds())
    return {"days": days, "alerts": alerts["n"], "attached": alerts["attached"], "unattached": alerts["n"] - alerts["attached"],
            "flapping": alerts["flapping"], "incidents_opened": opened, "incidents": kept,
            "compression": round(alerts["attached"] / kept, 2) if kept else None,
            "splits": events.get("split", 0), "manual_merges": events.get("merge", 0), "auto_merges": events.get("auto_merge", 0),
            "reopened": events.get("reopened", 0),
            "split_pct": _pct(events.get("split", 0), opened), "merge_pct": _pct(events.get("merge", 0), opened),
            "llm_calls": calls, "llm_per_alert": round(calls / alerts["n"], 3) if alerts["n"] else None,
            "methods": methods, "open_delay_median_s": round(statistics.median(waits)) if waits else None}
