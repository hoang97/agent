"""Việc cần người của sự cố: bản tóm tắt nêu ra hoặc người thêm tay; người đánh dấu đã xong."""
from __future__ import annotations

import re

from .. import db
from .common import IncidentError, _event


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def current(conn, incident_id: str) -> list[dict]:
    """Việc chưa bị bỏ: còn mở và đã xong, theo thứ tự thêm."""
    return [dict(r) for r in conn.execute("SELECT * FROM incident_task WHERE incident_id=? AND removed_at IS NULL ORDER BY created_at, rowid",
                                          (incident_id,)).fetchall()]


def sync_from_summary(conn, tenant_id: str, incident_id: str, texts: list[str]) -> None:
    """Thêm việc bản tóm tắt mới nêu; việc do tóm tắt nêu trước đó mà nay không còn, chưa xong, thì bỏ. Việc người thêm hay đã xong thì giữ."""
    now = db.iso()
    have = current(conn, incident_id)
    known = {_norm(t["text"]) for t in have}
    wanted = {_norm(t) for t in texts}
    for t in have:
        if t["source"] == "summary" and not t["done_at"] and _norm(t["text"]) not in wanted:
            conn.execute("UPDATE incident_task SET removed_at=? WHERE id=?", (now, t["id"]))
    for text in texts:
        if _norm(text) not in known:
            known.add(_norm(text))
            conn.execute("INSERT INTO incident_task(id,tenant_id,incident_id,text,source,created_at) VALUES (?,?,?,?, 'summary', ?)",
                         (db.new_id(), tenant_id, incident_id, text[:200], now))


def add(conn, tenant_id: str, incident_id: str, text: str, user: str) -> dict:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise IncidentError("nhập nội dung việc cần làm")
    if not conn.execute("SELECT 1 FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone():
        raise IncidentError("không tìm thấy sự cố", 404)
    tid = db.new_id()
    conn.execute("INSERT INTO incident_task(id,tenant_id,incident_id,text,source,created_at,created_by) VALUES (?,?,?,?, 'user', ?, ?)",
                 (tid, tenant_id, incident_id, text[:200], db.iso(), user))
    _event(conn, tenant_id, incident_id, "task_added", text[:200], actor=user)
    return dict(conn.execute("SELECT * FROM incident_task WHERE id=?", (tid,)).fetchone())


def set_done(conn, tenant_id: str, incident_id: str, task_id: str, user: str, done: bool = True) -> None:
    t = conn.execute("SELECT * FROM incident_task WHERE id=? AND incident_id=? AND tenant_id=? AND removed_at IS NULL",
                     (task_id, incident_id, tenant_id)).fetchone()
    if t is None:
        raise IncidentError("không tìm thấy việc của sự cố", 404)
    if bool(t["done_at"]) == done:
        return
    conn.execute("UPDATE incident_task SET done_by=?, done_at=? WHERE id=?", (user if done else None, db.iso() if done else None, task_id))
    _event(conn, tenant_id, incident_id, "task_done" if done else "task_reopened", t["text"], actor=user)
