import json

import pytest

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
            active(conn, a.tenant, kind="procedure", title=f"P{i}", content=f"Bước số {i}", domain="k8s_ops", entities=["tool:k8s.kubectl_read"])
    run = a.post("/api/compile").json()
    assert run["needs_review"] == 1 and "skills/k8s_ops-workflow/SKILL.md" in run["changed"]
    a.post(f"/api/compile/{run['id']}/decide", json={"approve": True})
    skill = a.get("/api/knowledge/file?path=skills/k8s_ops-workflow/SKILL.md").json()["content"]
    assert "success_rate_30d: 1.0" in skill and "source_kus:" in skill
    with db.session() as conn:
        row = conn.execute("SELECT * FROM skill WHERE tenant_id=?", (a.tenant,)).fetchone()
        assert json.loads(row["tools"]) == ["k8s.kubectl_read"]
    step = next(s for s in alice.run("xem pod trong namespace default")["trace"] if s["type"] == "tool_call")
    assert step["confidence_parts"]["skill"] == 1.0


def test_file_read_blocks_traversal(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="lesson", title="C", content="N", domain="general")
    a.post("/api/compile")
    for bad in ("../kagent.db", "..\\kagent.db", ".git/config", "/etc/passwd"):
        assert a.get("/api/knowledge/file", params={"path": bad}).status_code == 404


# ---------- tách phần tự publish khỏi phần cần duyệt ----------

def _pending(a):
    return [r for r in a.get("/api/compile/runs").json() if r["status"] == "pending_review"]


def test_playbook_publishes_while_rules_wait_for_review(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="lesson", title="L", content="Bài học về index", domain="k8s_ops")
        rule = active(conn, a.tenant, kind="rule", title="R", content="Luôn nêu nguồn số liệu")
    r = a.post("/api/compile").json()
    assert r["status"] == "pending_review" and r["changed"] == ["core/rules.md"]
    assert "Bài học về index" in a.get("/api/knowledge/file?path=playbooks/k8s_ops.md").json()["content"]  # đã publish ngay
    assert a.get("/api/knowledge/file?path=core/rules.md").status_code == 404  # quy ước chưa vào cho tới khi duyệt
    p = _pending(a)[0]
    added = p["summary"]["rules_added"][0]
    assert {k: added[k] for k in ("ku", "title", "content", "source_trust", "episodes")} == {"ku": rule["id"], "title": "R", "content": "Luôn nêu nguồn số liệu", "source_trust": "user", "episodes": 0}
    assert added["why_review"].startswith("chưa đủ")  # do Curator ghi, không phải tenant_admin nói thẳng


def test_only_one_pending_proposal_and_no_duplicates(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="rule", title="R1", content="Quy ước một")
    first = a.post("/api/compile").json()
    assert a.post("/api/compile").json()["id"] == first["id"]  # không đổi gì: giữ đề xuất cũ, không tạo bản trùng
    with db.session() as conn:
        active(conn, a.tenant, kind="rule", title="R2", content="Quy ước hai")
    second = a.post("/api/compile").json()
    assert second["id"] != first["id"] and [p["id"] for p in _pending(a)] == [second["id"]]
    assert len(_pending(a)[0]["summary"]["rules_added"]) == 2


def test_approve_after_playbook_moved_main(users):
    a = users["admin"]
    with db.session() as conn:
        active(conn, a.tenant, kind="rule", title="R", content="Quy ước được duyệt")
    proposal = a.post("/api/compile").json()
    with db.session() as conn:  # playbook đổi sau khi tạo đề xuất: main đi tiếp
        active(conn, a.tenant, kind="lesson", title="L", content="Bài học mới", domain="docs")
        compiler.compile_tenant(conn, a.tenant, "scheduler")
    assert [p["id"] for p in _pending(a)] == [proposal["id"]]  # đề xuất quy ước không đổi nên vẫn là bản cũ
    done = a.post(f"/api/compile/{proposal['id']}/decide", json={"approve": True}).json()
    assert done["status"] == "published"
    assert "Quy ước được duyệt" in a.get("/api/knowledge/file?path=core/rules.md").json()["content"]
    assert "Bài học mới" in a.get("/api/knowledge/file?path=playbooks/docs.md").json()["content"]


def test_retire_rule_updates_proposal(users):
    a = users["admin"]
    with db.session() as conn:
        keep = active(conn, a.tenant, kind="rule", title="Giữ", content="Quy ước nên giữ")
        drop = active(conn, a.tenant, kind="rule", title="Bỏ", content="Quy ước về cơ chế duyệt của agent")
    a.post("/api/compile")
    r = a.post("/api/compile/retire-rule", json={"ku_id": drop["id"]}).json()
    assert r["status"] == "pending_review"
    added = _pending(a)[0]["summary"]["rules_added"]
    assert [x["ku"] for x in added] == [keep["id"]]
    assert a.get(f"/api/ku/{drop['id']}").json()["status"] == "deprecated"
    assert users["alice"].post("/api/compile/retire-rule", json={"ku_id": keep["id"]}).status_code == 403


# ---------- tự publish quy ước core ----------

def _core(a):
    r = a.get("/api/knowledge/file?path=core/rules.md")
    return r.json()["content"] if r.status_code == 200 else ""


def _proven(conn, tenant, content, days=8, helpful=3, eps=("e1", "e2", "e3"), trust="verified_tool"):
    ku = knowledge.add(conn, tenant, kind="rule", title=content[:30], content=content, source_eps=list(eps), source_trust=trust, status="active")
    conn.execute("UPDATE knowledge_unit SET helpful_cnt=?, recorded_at=? WHERE id=?", (helpful, db.later(days=-days), ku["id"]))
    return ku


def test_rule_said_by_tenant_admin_autopublishes(users):
    a = users["admin"]
    ku = a.post("/api/ku", json={"kind": "rule", "title": "Báo cáo", "content": "Báo cáo sự cố luôn mở đầu bằng tóm tắt ba dòng"}).json()
    a.post(f"/api/ku/{ku['id']}/review", json={"approve": True})
    r = a.post("/api/compile").json()
    assert r["status"] == "published" and "tóm tắt ba dòng" in _core(a) and _pending(a) == []
    assert r["summary"]["rules_auto"][0]["ku"] == ku["id"]


def test_rule_proven_by_use_autopublishes_after_waiting_period(users):
    a = users["admin"]
    with db.session() as conn:
        ready = _proven(conn, a.tenant, "Đếm lỗi bằng aggregation trên toàn bộ bản ghi")
        young = _proven(conn, a.tenant, "Luôn ghi rõ khung thời gian khi báo số liệu", days=2)
    a.post("/api/compile")
    assert "aggregation" in _core(a) and "khung thời gian" not in _core(a)
    waiting = _pending(a)[0]["summary"]["rules_added"]
    assert [x["ku"] for x in waiting] == [young["id"]] and "chưa đủ 7 ngày" in waiting[0]["why_review"]
    assert ready["id"]


@pytest.mark.parametrize("content, trust, reason", [
    ("Index log theo quy ước k8s-env-service-ngày", "external_untrusted", "nội dung ngoài"),
    ("Khi lệnh bị giữ chờ duyệt thì liệt kê approval_id", "verified_tool", "cơ chế của agent"),
])
def test_rules_that_never_autopublish(users, content, trust, reason):
    a = users["admin"]
    with db.session() as conn:
        _proven(conn, a.tenant, content, trust=trust)
    a.post("/api/compile")
    assert _core(a) == "" and reason in _pending(a)[0]["summary"]["rules_added"][0]["why_review"]


def test_counter_change_alone_needs_no_review(users):
    a = users["admin"]
    with db.session() as conn:
        ku = _proven(conn, a.tenant, "Đếm lỗi bằng aggregation trên toàn bộ bản ghi")
    a.post("/api/compile")
    with db.session() as conn:
        conn.execute("UPDATE knowledge_unit SET helpful_cnt=9 WHERE id=?", (ku["id"],))
    r = a.post("/api/compile").json()
    assert r["status"] == "published" and "✓9" in _core(a) and _pending(a) == []


def test_harmful_autopublished_rule_is_withdrawn_to_review(users):
    a = users["admin"]
    with db.session() as conn:
        ku = _proven(conn, a.tenant, "Đếm lỗi bằng aggregation trên toàn bộ bản ghi")
    a.post("/api/compile")
    with db.session() as conn:
        conn.execute("UPDATE knowledge_unit SET harmful_cnt=1 WHERE id=?", (ku["id"],))
    a.post("/api/compile")
    assert "aggregation" not in _core(a)
    assert "bị tính có hại" in _pending(a)[0]["summary"]["rules_added"][0]["why_review"]


def test_mode_off_and_policy_validation(users):
    a = users["admin"]
    assert a.get("/api/policy").json()["rule_autopublish"] == {"mode": "ab", "min_episodes": 3, "min_helpful": 3, "min_days": 7}
    assert a.patch("/api/tenant", json={"rule_autopublish": {"mode": "x"}}).status_code == 400
    assert a.patch("/api/tenant", json={"rule_autopublish": {"min_days": -1}}).status_code == 400
    assert users["alice"].patch("/api/tenant", json={"rule_autopublish": {"mode": "off"}}).status_code == 403
    a.patch("/api/tenant", json={"rule_autopublish": {"mode": "off"}})
    with db.session() as conn:
        _proven(conn, a.tenant, "Đếm lỗi bằng aggregation trên toàn bộ bản ghi")
    a.post("/api/compile")
    assert _core(a) == "" and "tắt tự publish" in _pending(a)[0]["summary"]["rules_added"][0]["why_review"]
