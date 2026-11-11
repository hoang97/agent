"""Duyệt hàng loạt và tổng hợp KU ứng viên trùng ý."""
import json
import time

from kagent import api as api_mod, db, knowledge, llm


def _add(tenant, title, content, kind="lesson", trust="verified_tool", eps=("e1",)):
    with db.session() as conn:
        return knowledge.add(conn, tenant, kind=kind, title=title, content=content, source_eps=list(eps), source_trust=trust)["id"]


def _consolidate(user, **body):
    job = user.post("/api/ku/consolidate", json=body).json()
    for _ in range(200):
        if job["status"] != "running":
            return job
        time.sleep(0.05)
        job = user.get(f"/api/ku/consolidate/{job['id']}").json()
    raise AssertionError("job tổng hợp không xong")


def test_batch_review_accepts_and_skips(users):
    admin = users["admin"]
    a, b = _add(admin.tenant, "A", "nội dung a"), _add(admin.tenant, "B", "nội dung b")
    r = admin.post("/api/ku/review-batch", json={"ids": [a, b, "khong-co"], "approve": True}).json()
    assert r == {"done": 2, "skipped": 1}
    assert {admin.get(f"/api/ku/{i}").json()["status"] for i in (a, b)} == {"active"}
    assert admin.post("/api/ku/review-batch", json={"ids": [a], "approve": False}).json() == {"done": 0, "skipped": 1}  # không còn là ứng viên


def test_batch_review_requires_admin(users):
    a = _add(users["admin"].tenant, "A", "x")
    assert users["alice"].post("/api/ku/review-batch", json={"ids": [a], "approve": True}).status_code == 403


def test_merge_keeps_lowest_trust_and_links_originals(users):
    admin = users["admin"]
    a = _add(admin.tenant, "raise_for_status làm 500 giả", "HTTPError upstream không map", trust="verified_tool", eps=("e1",))
    b = _add(admin.tenant, "HTTP client biến 4xx thành 500", "client không map status", trust="external_untrusted", eps=("e2",))
    r = admin.post("/api/ku/merge", json={"ids": [a, b], "kind": "lesson", "title": "Upstream 4xx thành 500", "content": "raise_for_status để HTTPError lọt ra, Django trả 500",
                                          "activate": True})
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["status"] == "active" and merged["source_trust"] == "external_untrusted" and sorted(merged["source_eps"]) == ["e1", "e2"]
    for old in (a, b):
        o = admin.get(f"/api/ku/{old}").json()
        assert o["status"] == "rejected" and o["superseded_by"] == merged["id"] and "MERGE" in [ev["op"] for ev in o["events"]]


def test_merge_rejects_non_candidates_and_single(users):
    admin = users["admin"]
    a, b = _add(admin.tenant, "A", "x"), _add(admin.tenant, "B", "y")
    admin.post(f"/api/ku/{a}/review", json={"approve": True})
    assert admin.post("/api/ku/merge", json={"ids": [a, b], "kind": "lesson", "title": "t", "content": "c"}).status_code == 400
    assert admin.post("/api/ku/merge", json={"ids": [b], "kind": "lesson", "title": "t", "content": "c"}).status_code == 422


def test_consolidate_with_llm_validates_proposal(users):
    admin = users["admin"]
    a = _add(admin.tenant, "Index log k8s theo env-service-ngày", "k8s-devtest-cmp-backend-2026.11.11")
    b = _add(admin.tenant, "Quy ước đặt tên index Kibana", "index dạng k8s-<env>-<service>-<ngày>")
    c = _add(admin.tenant, "Duyệt trong chat không mở cổng approval", "gateway giữ lệnh")

    class Curator(llm.HeuristicProvider):
        def consolidate(self, items):
            return llm.check_consolidation({"groups": [{"ids": [a, b, "bia-dat"], "kind": "fact", "title": "Quy ước index log", "content": "k8s-<env>-<service>-<ngày>",
                                                         "applies_when": "khi tìm log", "reason": "cùng ý"},
                                                        {"ids": [a, c], "kind": "fact", "title": "trùng", "content": "x", "applies_when": "", "reason": ""}],
                                             "drop": [{"id": c, "reason": "nói về cơ chế duyệt của agent"}, {"id": a, "reason": "đã trong nhóm"}]},
                                            {it["id"] for it in items})

    llm.set_provider(Curator())
    p = _consolidate(admin)
    assert [sorted(g["ids"]) for g in p["groups"]] == [sorted([a, b])]  # id bịa bị bỏ, KU đã vào nhóm không vào nhóm thứ hai
    assert p["drop"] == [{"id": c, "reason": "nói về cơ chế duyệt của agent"}] and p["considered"] == 3 and set(p["items"]) == {a, b, c}
    with db.session() as conn:
        assert knowledge.get(conn, admin.tenant, a)["status"] == "candidate"  # chỉ đề xuất, chưa đổi gì


def test_lexical_consolidation_without_llm(users):
    admin = users["admin"]
    a = _add(admin.tenant, "Kế hoạch dựa trên nội dung untrusted bị chặn chờ duyệt", "gateway giữ lệnh chờ duyệt khi kế hoạch dựa trên nội dung untrusted")
    b = _add(admin.tenant, "Kế hoạch dựa trên kết quả untrusted bị chặn chờ duyệt", "lệnh dựa trên nội dung untrusted bị gateway giữ chờ duyệt")
    _add(admin.tenant, "Log cmp-backend để level trong request.level", "trường level nằm trong request.level")
    groups = _consolidate(admin)["groups"]
    assert [sorted(g["ids"]) for g in groups] == [sorted([a, b])]


def test_reflector_keeps_at_most_three_items():
    from test_llm import msg, provider, text
    import json
    item = {"kind": "lesson", "title": "t", "content": "c", "applies_when": "a", "entities": [], "evidence": "e", "source": "tool", "blame_ku": None, "scope": "tenant"}
    p, _ = provider([msg([text(json.dumps({"items": [item] * 5}))])])
    assert len(p.reflect({"input": {"task": "x"}})) == 3


def test_batches_split_by_kind_and_size():
    items = [{"id": f"l{i}", "kind": "lesson"} for i in range(85)] + [{"id": "f1", "kind": "fact"}, {"id": "f2", "kind": "fact"}, {"id": "r1", "kind": "rule"}]
    sizes = sorted(len(b) for b in api_mod._batches(items))
    assert sizes == [2, 5, 40, 40]  # rule chỉ có 1 KU nên không có gì để gộp


def test_failed_batch_is_reported_without_losing_others(users):
    admin = users["admin"]
    for i in range(3):
        _add(admin.tenant, f"Bài học {i}", "nội dung giống nhau về index log", kind="lesson")
        _add(admin.tenant, f"Sự thật {i}", "nội dung giống nhau về index log", kind="fact")

    class Flaky(llm.HeuristicProvider):
        def consolidate(self, items):
            if items[0]["kind"] == "fact":
                raise llm.LLMError("đầu ra bị cắt do chạm max_tokens")
            return super().consolidate(items)

    llm.set_provider(Flaky())
    job = _consolidate(admin)
    assert job["status"] == "done" and job["done"] == job["total"] == 2
    assert job["errors"] == ["đầu ra bị cắt do chạm max_tokens"] and len(job["groups"]) == 1


def test_consolidation_job_is_per_tenant(client, users):
    from conftest import Session
    job = users["admin"].post("/api/ku/consolidate", json={}).json()
    finance = Session(client, "admin", "team-finance")
    assert finance.get(f"/api/ku/consolidate/{job['id']}").status_code == 404


def test_job_survives_server_restart_and_resumes_only_unfinished_batches(users):
    """Server dừng giữa chừng: khởi động lại thì chạy tiếp lô còn dở, không chạy lại lô đã xong."""
    admin = users["admin"]
    for i in range(2):
        _add(admin.tenant, f"Bài học trùng {i}", "gateway giữ lệnh chờ duyệt khi kế hoạch dựa trên nội dung untrusted", kind="lesson")
        _add(admin.tenant, f"Sự thật trùng {i}", "index log dạng k8s env service ngày theo quy ước đặt tên", kind="fact")
    seen = []

    class Recording(llm.HeuristicProvider):
        def consolidate(self, items):
            seen.append(items[0]["kind"])
            return super().consolidate(items)

    llm.set_provider(Recording())
    job = _consolidate(admin)
    assert job["status"] == "done" and sorted(seen) == ["fact", "lesson"] and len(job["groups"]) == 2
    # Giả lập server bị dừng khi mới xong một lô: job còn "running", một lô chưa ghi kết quả.
    with db.session() as conn:
        row = api_mod._job(conn, job["id"])
        lesson_batch = next(i for i, ids in enumerate(row["batches"]) if row["items"][ids[0]]["kind"] == "lesson")
        keep = [g for g in row["groups"] if row["items"][g["ids"][0]]["kind"] != "lesson"]
        conn.execute("UPDATE ku_consolidation SET status='running', done_batches=?, groups=? WHERE id=?",
                     (json.dumps([i for i in row["done_batches"] if i != lesson_batch]), json.dumps(keep), job["id"]))
    seen.clear()
    api_mod.resume_consolidations()
    for _ in range(100):
        again = admin.get(f"/api/ku/consolidate/{job['id']}").json()
        if again["status"] != "running":
            break
        time.sleep(0.05)
    assert again["status"] == "done" and seen == ["lesson"] and len(again["groups"]) == 2


def test_stale_running_job_is_marked_error_on_startup(users):
    admin = users["admin"]
    _add(admin.tenant, "A trùng", "x y z"); _add(admin.tenant, "B trùng", "x y z")
    job = _consolidate(admin)
    with db.session() as conn:
        conn.execute("UPDATE ku_consolidation SET status='running', done_batches='[]', created_at=? WHERE id=?", (db.later(days=-2), job["id"]))
    api_mod.resume_consolidations()
    again = admin.get(f"/api/ku/consolidate/{job['id']}").json()
    assert again["status"] == "error" and "quá hạn" in again["errors"][-1]
