"""Chất lượng điều tra: người chấm nhận định của từng lượt điều tra, và số liệu theo khoảng ngày (tỉ lệ đúng, MTTR, mức xử lý).

Chấm gắn với episode của lượt điều tra, không với sự cố: lượt sau có thể đúng khi lượt trước sai. Người chấm tay thắng kết quả
suy từ lúc xác nhận chuỗi 5 Whys; suy ra không ghi đè chấm tay.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta

from .. import db
from .common import IncidentError, _event

VERDICTS = {"correct": "đúng", "partial": "đúng một phần", "wrong": "sai"}


def review(conn, tenant_id: str, incident_id: str, user: str, verdict: str, note: str = "") -> dict:
    if verdict not in VERDICTS:
        raise IncidentError("verdict phải là correct, partial hoặc wrong")
    inc = conn.execute("SELECT cause_episode FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    if not inc["cause_episode"]:
        raise IncidentError("sự cố chưa có lượt điều tra để chấm")
    note = re.sub(r"\s+", " ", note or "").strip()[:500]
    _save(conn, tenant_id, incident_id, inc["cause_episode"], verdict, note, "manual", user)
    _event(conn, tenant_id, incident_id, "reviewed", f"chấm nhận định của kagent: {VERDICTS[verdict]}" + (f" ({note})" if note else ""),
           {"verdict": verdict, "episode_id": inc["cause_episode"]}, actor=user)
    return current(conn, inc["cause_episode"])


def infer_from_confirm(conn, tenant_id: str, incident_id: str, user: str, agent_links: int, kept: int) -> None:
    """Xác nhận chuỗi cũng là một lần chấm: giữ nguyên mọi mắt xích của agent là đúng, giữ một phần là đúng một phần, bỏ hết là sai."""
    inc = conn.execute("SELECT cause_episode FROM incident WHERE id=?", (incident_id,)).fetchone()
    if not inc or not inc["cause_episode"] or not agent_links:
        return
    old = current(conn, inc["cause_episode"])
    if old and old["source"] == "manual":
        return
    verdict = "correct" if kept == agent_links else "partial" if kept else "wrong"
    _save(conn, tenant_id, incident_id, inc["cause_episode"], verdict, f"giữ {kept}/{agent_links} mắt xích của agent khi xác nhận", "confirm", user)


def _save(conn, tenant_id, incident_id, episode_id, verdict, note, source, user) -> None:
    conn.execute("INSERT INTO investigation_review(episode_id,tenant_id,incident_id,verdict,note,source,by_user,at) VALUES (?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(episode_id) DO UPDATE SET verdict=excluded.verdict, note=excluded.note, source=excluded.source, "
                 "by_user=excluded.by_user, at=excluded.at", (episode_id, tenant_id, incident_id, verdict, note, source, user, db.iso()))


def current(conn, episode_id: str | None) -> dict | None:
    r = conn.execute("SELECT r.*, u.username FROM investigation_review r LEFT JOIN app_user u ON u.id=r.by_user WHERE r.episode_id=?",
                     (episode_id,)).fetchone() if episode_id else None
    if r is None:
        return None
    return {"verdict": r["verdict"], "label": VERDICTS[r["verdict"]], "note": r["note"], "source": r["source"], "by": r["username"], "at": r["at"]}


def _minutes(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return max(0.0, (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 60)


def _summary(values: list[float]) -> dict:
    return {"n": len(values), "median": round(statistics.median(values), 1) if values else None,
            "mean": round(statistics.fmean(values), 1) if values else None}


def stats(conn, tenant_id: str, days: int = 30, env: str | None = None) -> dict:
    """Số liệu của sự cố mở trong `days` ngày gần nhất. MTTR tính từ cảnh báo đầu tiên tới khi mọi cảnh báo hết."""
    from .rootcause import level
    since = db.iso(db.now() - timedelta(days=days))
    sql, params = "SELECT * FROM incident WHERE tenant_id=? AND created_at>=?", [tenant_id, since]
    if env:
        sql += " AND env=?"
        params.append(env)
    incs = conn.execute(sql, params).fetchall()
    ids = [i["id"] for i in incs]
    first_alert = {r["incident_id"]: r["t"] for r in conn.execute(
        f"SELECT incident_id, min(starts_at) t FROM alert_event WHERE incident_id IN ({','.join('?' * len(ids))}) GROUP BY incident_id", ids)} if ids else {}
    first_insight = {r["incident_id"]: r["t"] for r in conn.execute(
        f"SELECT incident_id, min(at) t FROM incident_event WHERE kind='investigated' AND incident_id IN ({','.join('?' * len(ids))}) "
        "GROUP BY incident_id", ids)} if ids else {}
    ttr = [m for i in incs if (m := _minutes(first_alert.get(i["id"]) or i["created_at"], i["resolved_at"])) is not None]
    tti = [m for i in incs if (m := _minutes(i["created_at"], first_insight.get(i["id"]))) is not None]
    reviews = conn.execute(
        f"SELECT r.verdict, r.source, r.note, r.at, i.id, i.title FROM investigation_review r JOIN incident i ON i.id=r.incident_id "
        f"WHERE i.id IN ({','.join('?' * len(ids))}) ORDER BY r.at DESC", ids).fetchall() if ids else []
    counts = {v: sum(1 for r in reviews if r["verdict"] == v) for v in VERDICTS}
    graded = sum(counts.values())
    rounds = sum(i["investigations"] or 0 for i in incs)
    levels = {n: 0 for n in range(4)}
    for i in incs:
        levels[level(conn, i)["level"]] += 1
    return {"days": days, "since": since, "env": env, "incidents": len(incs), "investigated": sum(1 for i in incs if i["investigations"]),
            "rounds": rounds, "reviews": {**counts, "total": graded, "manual": sum(1 for r in reviews if r["source"] == "manual")},
            "accuracy": round(counts["correct"] / graded, 3) if graded else None,
            "accuracy_partial": round((counts["correct"] + counts["partial"]) / graded, 3) if graded else None,
            "review_coverage": round(graded / rounds, 3) if rounds else None,
            "mttr_minutes": _summary(ttr), "first_insight_minutes": _summary(tti), "levels": levels,
            "wrong": [{"id": r["id"], "title": r["title"], "note": r["note"], "at": r["at"]} for r in reviews if r["verdict"] == "wrong"][:10]}
