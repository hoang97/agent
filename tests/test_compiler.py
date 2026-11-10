import json

from kagent import compiler, db, knowledge


def active(conn, tenant, **kw):
    return knowledge.add(conn, tenant, source_eps=[], source_trust="user", status="active", **kw)


def test_playbook_lines_carry_id_and_counters(users):
    a = users["admin"]
    with db.session() as conn:
        ku = active(conn, a.tenant, kind="lesson", title="API X", content="API X trả 200 cả khi lỗi", domain="k8s_ops")
    assert a.post("/api/compile").json()["status"] == "published"
    text = a.get("/api/knowledge/file?path=playbooks/k8s_ops.md").json()["content"]
    assert f"[ku:{ku['id'][:8]} ✓0 ✗0] API X trả 200 cả khi lỗi" in text
    assert "## Cạm bẫy" in text


def test_second_compile_is_noop(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="lesson", title="A", content="Nội dung", domain="k8s_ops")
    a.post("/api/compile")
    assert a.post("/api/compile").json()["status"] == "noop"


def test_golden_drop_blocks_publish(users):
    a = users["admin"]
    with db.session() as conn:
        ku = active(conn, a.tenant, kind="lesson", title="API Y", content="Dịch vụ omega cần token mới", domain="k8s_ops")
    assert a.post("/api/compile").json()["status"] == "published"
    a.post("/api/golden", json={"query": "kubectl pod dịch vụ", "expect_domain": "k8s_ops", "expect_terms": ["omega"]})
    with db.session() as conn:
        knowledge.invalidate(conn, a.tenant, ku["id"], None, "t", "thử")
    run = a.post("/api/compile").json()
    assert run["status"] == "blocked" and run["score_after"] < run["score_before"]
    assert "omega" in a.get("/api/knowledge/file?path=playbooks/k8s_ops.md").json()["content"]


def test_rollback_with_git_revert(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="lesson", title="B", content="Bản đầu", domain="general")
    a.post("/api/compile")
    assert "playbooks/general.md" in a.get("/api/knowledge/files").json()
    r = a.post("/api/compile/rollback")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reverted"
    assert "playbooks/general.md" not in a.get("/api/knowledge/files").json()


def test_rejected_proposal_not_published(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="rule", title="R", content="Luôn báo cáo", domain="general")
    run = a.post("/api/compile").json()
    assert run["status"] == "pending_review"
    assert "core/rules.md" not in a.get("/api/knowledge/files").json()
    assert a.post(f"/api/compile/{run['id']}/decide", json={"approve": False}).json()["status"] == "rejected"


def test_skill_requires_three_procedures_and_review(users):
    a, alice = users["admin"], users["alice"]
    for _ in range(5):
        alice.run("xem pod trong namespace default")
    with db.session() as conn:
        for i in range(3):
            active(conn, a.tenant, kind="procedure", title=f"P{i}", content=f"Bước số {i}", domain="k8s_ops", entities=["tool:k8s.list_pods"])
    run = a.post("/api/compile").json()
    assert run["needs_review"] == 1 and "skills/k8s_ops-workflow/SKILL.md" in run["changed"]
    a.post(f"/api/compile/{run['id']}/decide", json={"approve": True})
    skill = a.get("/api/knowledge/file?path=skills/k8s_ops-workflow/SKILL.md").json()["content"]
    assert "success_rate_30d: 1.0" in skill and "source_kus:" in skill
    with db.session() as conn:
        row = conn.execute("SELECT * FROM skill WHERE tenant_id=?", (a.tenant,)).fetchone()
        assert json.loads(row["tools"]) == ["k8s.list_pods"]
    step = next(s for s in alice.run("xem pod trong namespace default")["trace"] if s["type"] == "tool_call")
    assert step["confidence_parts"]["skill"] == 1.0


def test_file_read_blocks_traversal(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="lesson", title="C", content="N", domain="general")
    a.post("/api/compile")
    for bad in ("../kagent.db", "..\\kagent.db", ".git/config", "/etc/passwd"):
        assert a.get("/api/knowledge/file", params={"path": bad}).status_code == 404
