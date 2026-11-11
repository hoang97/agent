"""Phiên trượt và trạng thái giữ qua refresh: lượt đang chạy, đề xuất tổng hợp mở lại."""
import threading
import time
from datetime import datetime, timedelta

from kagent import api as api_mod, db, knowledge, llm
from test_chat import chat


def _session_row(user):
    with db.session() as conn:
        return conn.execute("SELECT * FROM session WHERE token_hash=?", (api_mod._token_hash(user.cookie),)).fetchone()


def test_active_session_is_extended_and_cookie_renewed(users):
    alice = users["alice"]
    with db.session() as conn:
        conn.execute("UPDATE session SET expires_at=? WHERE token_hash=?", (db.later(hours=1), api_mod._token_hash(alice.cookie)))
    r = alice.get("/api/me", tenant=False)
    assert r.status_code == 200 and "kagent_session" in r.headers.get("set-cookie", "")
    left = datetime.fromisoformat(_session_row(alice)["expires_at"]) - db.now()
    assert left > timedelta(hours=11)


def test_fresh_session_is_not_rewritten_on_every_request(users):
    alice = users["alice"]
    before = _session_row(alice)["expires_at"]
    r = alice.get("/api/me", tenant=False)
    assert "set-cookie" not in r.headers and _session_row(alice)["expires_at"] == before


def test_session_never_outlives_max_age(users):
    alice = users["alice"]
    with db.session() as conn:
        conn.execute("UPDATE session SET created_at=?, expires_at=? WHERE token_hash=?",
                     (db.later(days=-30), db.later(minutes=30), api_mod._token_hash(alice.cookie)))
    assert alice.get("/api/me", tenant=False).status_code == 200
    assert datetime.fromisoformat(_session_row(alice)["expires_at"]) <= db.now() + timedelta(minutes=31)  # không gia hạn quá 30 ngày


def test_expired_session_is_rejected(users):
    alice = users["alice"]
    with db.session() as conn:
        conn.execute("UPDATE session SET expires_at=? WHERE token_hash=?", (db.later(minutes=-1), api_mod._token_hash(alice.cookie)))
    assert alice.get("/api/me", tenant=False).status_code == 401


def test_conversation_reports_turn_still_running(users):
    alice = users["alice"]
    cid = chat(alice, "xem pod")[0]["conversation_id"]
    gate, started = threading.Event(), threading.Event()

    class Slow(llm.HeuristicProvider):
        def run_agent(self, *a, **k):
            started.set()
            gate.wait(5)
            return super().run_agent(*a, **k)

    llm.set_provider(Slow())
    worker = threading.Thread(target=lambda: chat(alice, "xem pod lần nữa", cid))
    worker.start()
    assert started.wait(5)
    assert alice.get(f"/api/conversations/{cid}").json()["running"] is True
    gate.set()
    worker.join(10)
    data = alice.get(f"/api/conversations/{cid}").json()
    assert data["running"] is False and len(data["messages"]) == 2


def test_reopened_proposal_skips_already_handled_kus(users):
    admin = users["admin"]
    ids = []
    with db.session() as conn:
        for i in range(3):
            ids.append(knowledge.add(conn, admin.tenant, kind="lesson", title=f"Kế hoạch dựa trên nội dung untrusted bị chặn {i}",
                                     content="gateway giữ lệnh chờ duyệt khi kế hoạch dựa trên nội dung untrusted", source_eps=[], source_trust="verified_tool")["id"])
    job = admin.post("/api/ku/consolidate", json={}).json()
    for _ in range(100):
        if job["status"] != "running":
            break
        time.sleep(0.05)
        job = admin.get(f"/api/ku/consolidate/{job['id']}").json()
    assert sorted(job["groups"][0]["ids"]) == sorted(ids)
    admin.post("/api/ku/review-batch", json={"ids": ids[:2], "approve": True})
    again = admin.get(f"/api/ku/consolidate/{job['id']}").json()
    assert again["groups"] == []  # chỉ còn 1 KU ứng viên trong nhóm: không còn gì để gộp
