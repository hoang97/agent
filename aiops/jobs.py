"""Việc chạy lâu bấm từ giao diện (viết lại nguyên nhân, biên dịch, hoàn tác...): chạy ở luồng nền, trạng thái lưu trong bảng job.

Request chờ tối đa WAIT giây rồi trả kết quả như lời gọi đồng bộ; quá thì trả job để trang theo dõi. Tải lại trang hay người khác mở
cùng trang vẫn thấy việc đang chạy và lỗi của lượt gần nhất. Mỗi (tenant, kind, target) chạy một lượt một lúc: bấm lại khi đang chạy
thì nhận lượt đang chạy.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading

from fastapi import HTTPException

from . import db

log = logging.getLogger("aiops.jobs")

WAIT = 20.0  # dưới timeout thường gặp của proxy/trình duyệt
KEEP = 3  # lượt đã xong giữ lại cho mỗi (kind, target)

_lock = threading.Lock()
_done: dict[str, threading.Event] = {}


def view(conn, row) -> dict:
    d = db.row(row, "result", "error")
    user = conn.execute("SELECT username FROM app_user WHERE id=?", (d["started_by"],)).fetchone() if d["started_by"] else None
    d["started_by_name"] = user["username"] if user else None
    return d


def get(conn, tenant_id: str, job_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM job WHERE id=? AND tenant_id=?", (job_id, tenant_id)).fetchone()
    return view(conn, r) if r else None


def latest(conn, tenant_id: str, target: str) -> dict[str, dict]:
    """Lượt gần nhất của mỗi kind trên target; kết quả bỏ đi vì trang đã đọc trạng thái mới từ API của chính nó."""
    rows = conn.execute("SELECT * FROM job WHERE tenant_id=? AND target=? ORDER BY started_at, rowid", (tenant_id, target)).fetchall()
    return {r["kind"]: {**view(conn, r), "result": None} for r in rows}


def run(tenant_id: str, kind: str, target: str, user_id: str | None, fn, wait: float = WAIT):
    """fn(conn) chạy ở luồng nền với kết nối riêng; HTTPException của fn thành lỗi của job (giữ mã và detail).
    Xong trong wait giây: trả kết quả hoặc ném lại lỗi như gọi thẳng. Chưa xong: (202, job)."""
    with _lock, db.session() as conn:
        cur = conn.execute("SELECT id FROM job WHERE tenant_id=? AND kind=? AND target=? AND status='running'", (tenant_id, kind, target)).fetchone()
        if cur and cur["id"] in _done:
            job_id = cur["id"]
        else:
            job_id = db.new_id()
            with db.tx(conn):
                conn.execute("DELETE FROM job WHERE id IN (SELECT id FROM job WHERE tenant_id=? AND kind=? AND target=? AND status!='running' "
                             "ORDER BY started_at DESC LIMIT -1 OFFSET ?)", (tenant_id, kind, target, KEEP - 1))
                conn.execute("INSERT INTO job(id,tenant_id,kind,target,status,started_by,started_at) VALUES (?,?,?,?, 'running', ?,?)",
                             (job_id, tenant_id, kind, target, user_id, db.iso()))
            _done[job_id] = threading.Event()
            threading.Thread(target=contextvars.copy_context().run, args=(_work, job_id, fn), name=f"job-{kind[:12]}-{job_id[:8]}", daemon=True).start()
        event = _done[job_id]
    event.wait(wait)
    with db.session() as conn:
        job = get(conn, tenant_id, job_id)
    if job["status"] == "running":
        return 202, job
    if job["status"] == "error":
        raise HTTPException(job["error"]["status"], job["error"]["detail"])
    return 200, job["result"]


def _work(job_id: str, fn) -> None:
    result, error = None, None
    try:
        with db.session() as conn:
            result = fn(conn)
    except HTTPException as exc:
        error = {"status": exc.status_code, "detail": exc.detail}
    except Exception:
        log.exception("job %s lỗi", job_id)
        error = {"status": 500, "detail": "lỗi không xác định, xem log máy chủ"}
    try:
        with db.session() as conn, db.tx(conn):
            conn.execute("UPDATE job SET status=?, result=?, error=?, finished_at=? WHERE id=?",
                         ("error" if error else "ok", None if error else json.dumps(result, ensure_ascii=False, default=str),
                          json.dumps(error, ensure_ascii=False) if error else None, db.iso(), job_id))
    finally:
        with _lock:
            _done.pop(job_id).set()


def recover(conn) -> None:
    """Lượt đang chạy khi máy chủ dừng thì không ai kết thúc nữa."""
    err = json.dumps({"status": 500, "detail": "bị ngắt giữa chừng vì máy chủ khởi động lại; bấm lại để chạy lại"}, ensure_ascii=False)
    with _lock, db.tx(conn):
        conn.execute("UPDATE job SET status='error', error=?, finished_at=? WHERE status='running' AND id NOT IN (%s)"
                     % ",".join("?" * len(_done)), (err, db.iso(), *_done))
