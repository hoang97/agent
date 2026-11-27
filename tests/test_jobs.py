"""Việc chạy lâu bấm từ giao diện: trạng thái sống qua F5, chạy một lượt một lúc, lỗi của lượt gần nhất còn đọc lại được."""
import threading

import pytest
from fastapi import HTTPException

from aiops import db, jobs


def test_slow_job_returns_202_and_is_listed_until_done(client):
    gate = threading.Event()
    status, job = jobs.run("t1", "rewrite-cause", "incident:i1", None, lambda conn: gate.wait(5) and {"ok": 1}, wait=0.05)
    assert status == 202 and job["status"] == "running"
    with db.session() as conn:
        assert jobs.latest(conn, "t1", "incident:i1")["rewrite-cause"]["id"] == job["id"]
    again = jobs.run("t1", "rewrite-cause", "incident:i1", None, lambda conn: {"other": 1}, wait=0.05)
    assert again[1]["id"] == job["id"]  # bấm lại khi đang chạy: nhận lượt đang chạy
    gate.set()
    assert jobs.run("t1", "summary", "incident:i1", None, lambda conn: {"n": 2}) == (200, {"n": 2})
    with db.session() as conn:
        for _ in range(100):
            if jobs.get(conn, "t1", job["id"])["status"] != "running":
                break
            threading.Event().wait(0.05)
        done = jobs.get(conn, "t1", job["id"])
        assert done["status"] == "ok" and done["result"] == {"ok": 1}
        assert set(jobs.latest(conn, "t1", "incident:i1")) == {"rewrite-cause", "summary"} and not jobs.latest(conn, "t2", "incident:i1")


def test_error_keeps_status_and_detail_and_old_runs_are_pruned(client):
    def fail(conn):
        raise HTTPException(409, {"message": "đã có thay đổi sau", "later": 1})
    for _ in range(jobs.KEEP + 2):
        with pytest.raises(HTTPException) as exc:
            jobs.run("t1", "undo:c1", "changes", None, fail)
        assert exc.value.status_code == 409 and exc.value.detail["later"] == 1
    with db.session() as conn:
        assert conn.execute("SELECT count(*) n FROM job WHERE kind='undo:c1'").fetchone()["n"] == jobs.KEEP
        last = jobs.latest(conn, "t1", "changes")["undo:c1"]
    assert last["status"] == "error" and last["error"] == {"status": 409, "detail": {"message": "đã có thay đổi sau", "later": 1}}
    with pytest.raises(HTTPException) as exc:
        jobs.run("t1", "boom", "changes", None, lambda conn: 1 / 0)
    assert exc.value.status_code == 500 and "log máy chủ" in exc.value.detail


def test_running_job_left_by_restart_is_marked_error(client):
    with db.session() as conn, db.tx(conn):
        conn.execute("INSERT INTO job(id,tenant_id,kind,target,status,started_at) VALUES ('j1','t1','lint','knowledge','running',?)", (db.iso(),))
    with db.session() as conn:
        jobs.recover(conn)
        job = jobs.get(conn, "t1", "j1")
    assert job["status"] == "error" and "khởi động lại" in job["error"]["detail"]
    assert jobs.run("t1", "lint", "knowledge", None, lambda conn: {"ok": True}) == (200, {"ok": True})


def test_api_returns_202_with_job_then_job_endpoints_show_it(users, monkeypatch):
    admin = users["admin"]
    gate = threading.Event()
    monkeypatch.setattr(jobs.run, "__defaults__", (0.05,))
    from aiops import lint
    monkeypatch.setattr(lint, "run", lambda conn, tenant_id, user: gate.wait(5) and {"issues": []})
    r = admin.post("/api/knowledge/lint")
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["kind"] == "lint" and job["started_by_name"] == "admin"
    assert admin.get("/api/jobs", params={"target": "knowledge"}).json()["lint"]["status"] == "running"
    gate.set()
    for _ in range(100):
        if admin.get(f"/api/jobs/{job['id']}").json()["status"] != "running":
            break
        threading.Event().wait(0.05)
    assert admin.get(f"/api/jobs/{job['id']}").json()["result"] == {"issues": []}
    assert users["alice"].get(f"/api/jobs/{job['id']}").status_code == 200  # cùng tenant thấy việc người khác bấm
