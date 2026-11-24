"""Kiểm tra định kỳ kho tri thức: loại ứng viên quá hạn, gợi ý gộp tên thực thể, báo KU lâu không dùng và mâu thuẫn còn treo.

Chỉ bước loại ứng viên quá hạn tự sửa dữ liệu (ghi ku_event, hoàn tác được); các mục còn lại là gợi ý cho admin.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from . import db, knowledge
from .config import settings

UNUSED_DAYS = 30
INTERVAL_HOURS = 24
LIST_LIMIT = 50
_SEPARATORS = re.compile(r"[-_. ]+")


def _entity_key(name: str) -> str:
    """Hai tên chỉ khác dấu nối hoặc đuôi số nhiều là một thứ: k8s-devtest/k8s_devtest, log/logs."""
    key = _SEPARATORS.sub("", name)
    return key[:-1] if len(key) > 3 and key.endswith("s") else key


def entity_suggestions(conn, tenant_id: str) -> list[dict]:
    counts = {r["v"]: r["n"] for r in conn.execute(
        "SELECT e.value v, count(*) n FROM knowledge_unit k, json_each(k.entities) e WHERE k.tenant_id=? "
        "AND k.status IN ('active','candidate') AND k.scope NOT LIKE 'user:%' GROUP BY e.value", (tenant_id,))}
    groups: dict[str, list[str]] = defaultdict(list)
    for name in counts:
        if not name.startswith("tool:"):  # tên công cụ do hệ thống đặt, entities.merge không cho gộp
            groups[_entity_key(name)].append(name)
    out = []
    for names in groups.values():
        if len(names) < 2:
            continue
        names.sort(key=lambda n: (-counts[n], n))
        out.append({"target": names[0], "sources": names[1:], "kus": sum(counts[n] for n in names)})
    return sorted(out, key=lambda s: -s["kus"])[:LIST_LIMIT]


def expire_candidates(conn, tenant_id: str, days: int, actor: str = "lint") -> list[dict]:
    """Ứng viên quá hạn mà chưa có nguồn thứ hai xác nhận thì loại.

    Không đụng tới ký ức cá nhân và KU từ tài liệu được tải lên: đó là thứ người dùng chủ động đưa vào, không phải nhiễu tự sinh.
    """
    cutoff = db.later(days=-days)
    rs = [r for r in conn.execute("SELECT id, title, source_eps FROM knowledge_unit WHERE tenant_id=? AND status='candidate' AND scope NOT LIKE 'user:%' "
                                  "AND source_trust!='internal_doc' AND recorded_at < ? ORDER BY recorded_at", (tenant_id, cutoff)).fetchall()
          if knowledge.source_count(json.loads(r["source_eps"])) < 2]
    for r in rs:
        knowledge.update(conn, tenant_id, r["id"], {"status": "rejected"}, "EXPIRE", actor,
                         f"ứng viên quá {days} ngày chưa được duyệt hay episode khác xác nhận")
    return [{"id": r["id"], "title": r["title"]} for r in rs]


def unused(conn, tenant_id: str, days: int = UNUSED_DAYS) -> list[dict]:
    cutoff = db.later(days=-days)
    rs = conn.execute("SELECT id, title, kind, coalesce(last_used_at, recorded_at) last FROM knowledge_unit WHERE tenant_id=? AND status='active' "
                      "AND scope NOT LIKE 'user:%' AND coalesce(last_used_at, recorded_at) < ? ORDER BY last LIMIT ?",
                      (tenant_id, cutoff, LIST_LIMIT)).fetchall()
    return [{"id": r["id"], "title": r["title"], "kind": r["kind"], "last_used": r["last"]} for r in rs]


def contradictions(conn, tenant_id: str) -> list[dict]:
    """Ứng viên mâu thuẫn với KU đang dùng: cổng tự thăng hạng bỏ qua chúng, chỉ người duyệt mới chọn được bên đúng."""
    rs = conn.execute("SELECT n.id new_id, n.title new_title, o.id old_id, o.title old_title FROM ku_edge e "
                      "JOIN knowledge_unit n ON n.id=e.src JOIN knowledge_unit o ON o.id=e.dst "
                      "WHERE e.tenant_id=? AND e.rel='contradicts' AND n.status='candidate' AND o.status='active' LIMIT ?",
                      (tenant_id, LIST_LIMIT)).fetchall()
    return [dict(r) for r in rs]


def backlog(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT count(*) n, min(recorded_at) oldest FROM knowledge_unit WHERE tenant_id=? AND status='candidate' AND scope NOT LIKE 'user:%'",
                     (tenant_id,)).fetchone()
    week = conn.execute("SELECT count(*) FROM knowledge_unit WHERE tenant_id=? AND scope NOT LIKE 'user:%' AND recorded_at >= ?",
                        (tenant_id, db.later(days=-7))).fetchone()[0]
    return {"candidates": r["n"], "oldest": r["oldest"], "new_per_day": round(week / 7, 1)}


def link_missing(conn, tenant_id: str) -> int:
    """KU chưa có cạnh liên quan nào (học trước khi có link_related, hoặc lúc thêm chưa có KU cùng chủ đề) thì thử nối lại."""
    rs = conn.execute("SELECT id FROM knowledge_unit k WHERE tenant_id=? AND status IN ('active','candidate') AND entities!='[]' "
                      "AND NOT EXISTS (SELECT 1 FROM ku_edge e WHERE e.tenant_id=k.tenant_id AND e.rel='related' AND (e.src=k.id OR e.dst=k.id))",
                      (tenant_id,)).fetchall()
    return sum(knowledge.link_related(conn, tenant_id, r["id"]) for r in rs)


def run(conn, tenant_id: str, actor: str = "lint") -> dict:
    report = {
        "at": db.iso(),
        "stale_days": settings.lint_stale_days,
        "expired": expire_candidates(conn, tenant_id, settings.lint_stale_days, actor),
        "linked": link_missing(conn, tenant_id),
        "entity_merges": entity_suggestions(conn, tenant_id),
        "unused": unused(conn, tenant_id),
        "unused_days": UNUSED_DAYS,
        "contradictions": contradictions(conn, tenant_id),
        "backlog": backlog(conn, tenant_id),
    }
    previous = latest(conn, tenant_id) or {}
    if "dedup" in previous:  # tìm KU trùng chạy riêng (gọi LLM), kết quả gần nhất giữ qua các lần kiểm tra nhanh
        report["dedup"] = previous["dedup"]
    conn.execute("INSERT INTO ku_lint(tenant_id, report, at) VALUES (?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET report=excluded.report, at=excluded.at",
                 (tenant_id, json.dumps(report, ensure_ascii=False), report["at"]))
    return report


def attach(conn, tenant_id: str, key: str, value) -> None:
    report = latest(conn, tenant_id) or {"at": db.iso()}
    report[key] = value
    conn.execute("INSERT INTO ku_lint(tenant_id, report, at) VALUES (?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET report=excluded.report",
                 (tenant_id, json.dumps(report, ensure_ascii=False), report["at"]))


def refresh_entity_merges(conn, tenant_id: str) -> None:
    """Sau khi admin gộp tên, báo cáo đã lưu không còn gợi ý nhóm vừa gộp (gợi ý tính lại rẻ, không cần chạy lại cả lint)."""
    if latest(conn, tenant_id) is not None:
        attach(conn, tenant_id, "entity_merges", entity_suggestions(conn, tenant_id))


def latest(conn, tenant_id: str) -> dict | None:
    r = conn.execute("SELECT report FROM ku_lint WHERE tenant_id=?", (tenant_id,)).fetchone()
    return json.loads(r["report"]) if r else None


def due(conn, tenant_id: str) -> bool:
    r = conn.execute("SELECT at FROM ku_lint WHERE tenant_id=?", (tenant_id,)).fetchone()
    return r is None or datetime.fromisoformat(r["at"]) <= db.now() - timedelta(hours=INTERVAL_HOURS)
