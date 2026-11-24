"""Kiểm tra định kỳ kho tri thức (lint.py)."""
from aiops import db, knowledge, lint


def _add(conn, tenant, title, *, days_old=0, eps=("e1",), status="candidate", scope="tenant", entities=None, kind="lesson"):
    ku = knowledge.add(conn, tenant, kind=kind, title=title, content=f"nội dung {title}", source_eps=list(eps), source_trust="verified_tool",
                       scope=scope, entities=entities, status=status)
    if days_old:
        conn.execute("UPDATE knowledge_unit SET recorded_at=? WHERE id=?", (db.later(days=-days_old), ku["id"]))
    return ku["id"]


def test_expire_only_old_unconfirmed_shared_candidates(users):
    tenant = users["admin"].tenant
    with db.session() as conn:
        old = _add(conn, tenant, "cũ", days_old=10)
        fresh = _add(conn, tenant, "mới", days_old=2)
        confirmed = _add(conn, tenant, "đã xác nhận", days_old=10, eps=("e1", "e2"))
        personal = _add(conn, tenant, "cá nhân", days_old=10, scope="user:u1")
        active = _add(conn, tenant, "đang dùng", days_old=10, status="active")
        report = lint.run(conn, tenant)
        status = {i: knowledge.get(conn, tenant, i)["status"] for i in (old, fresh, confirmed, personal, active)}
        events = conn.execute("SELECT id, op, actor FROM ku_event WHERE ku_id=? ORDER BY id DESC", (old,)).fetchall()
    assert [x["id"] for x in report["expired"]] == [old]
    assert status == {old: "rejected", fresh: "candidate", confirmed: "candidate", personal: "candidate", active: "active"}
    assert (events[0]["op"], events[0]["actor"]) == ("EXPIRE", "lint")
    assert report["backlog"]["candidates"] == 2  # ứng viên cá nhân không tính vào hàng chờ duyệt chung
    with db.session() as conn:
        knowledge.rollback(conn, tenant, events[0]["id"], "admin")
        assert knowledge.get(conn, tenant, old)["status"] == "candidate"


def test_entity_suggestions_group_separator_and_plural_variants(users):
    tenant = users["admin"].tenant
    with db.session() as conn:
        for _ in range(3):
            _add(conn, tenant, "a", entities=["k8s_staging"])
        _add(conn, tenant, "b", entities=["k8s-staging", "logs"])
        _add(conn, tenant, "c", entities=["log", "node-1"])
        _add(conn, tenant, "d", entities=["node-2"])  # node-1/node-2 là hai máy khác nhau, không được gợi ý gộp
        suggestions = lint.entity_suggestions(conn, tenant)
    assert {(s["target"], tuple(s["sources"])) for s in suggestions} == {("k8s_staging", ("k8s-staging",)), ("log", ("logs",))}


def test_merged_entity_suggestion_does_not_come_back_after_reload(users):
    admin = users["admin"]
    with db.session() as conn:
        _add(conn, admin.tenant, "a", entities=["k8s_staging"])
        _add(conn, admin.tenant, "b", entities=["k8s-staging", "logs"])
        _add(conn, admin.tenant, "c", entities=["log"])
        lint.run(conn, admin.tenant)
    assert admin.post("/api/knowledge/entities/merge", json={"sources": ["k8s-staging"], "target": "k8s_staging"}).status_code == 200
    assert [m["target"] for m in admin.get("/api/knowledge/lint").json()["entity_merges"]] == ["log"]


def test_unused_and_contradictions(users):
    tenant = users["admin"].tenant
    with db.session() as conn:
        stale = _add(conn, tenant, "lâu không dùng", days_old=40, status="active")
        _add(conn, tenant, "mới dùng", days_old=40, status="active")
        conn.execute("UPDATE knowledge_unit SET last_used_at=? WHERE title='mới dùng'", (db.iso(),))
        new = _add(conn, tenant, "bản mới")
        knowledge.add_edge(conn, tenant, new, stale, "contradicts")
        report = lint.run(conn, tenant)
    assert [x["id"] for x in report["unused"]] == [stale]
    assert report["contradictions"] == [{"new_id": new, "new_title": "bản mới", "old_id": stale, "old_title": "lâu không dùng"}]


def test_due_and_api(users):
    admin, alice = users["admin"], users["alice"]
    with db.session() as conn:
        assert lint.due(conn, admin.tenant)
    assert admin.get("/api/knowledge/lint").json() is None
    r = admin.post("/api/knowledge/lint")
    assert r.status_code == 200 and r.json()["backlog"]["candidates"] >= 0
    assert admin.get("/api/knowledge/lint").json()["at"] == r.json()["at"]
    with db.session() as conn:
        assert not lint.due(conn, admin.tenant)
    assert alice.post("/api/knowledge/lint").status_code == 403
