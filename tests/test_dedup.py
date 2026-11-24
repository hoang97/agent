"""Gộp KU trùng theo cụm chủ đề (dedup.py) và KU cùng chủ đề cho Curator."""
import time

from aiops import db, dedup, knowledge, learning, llm

TEXT = "Wikijs crash vì role wikijs không là thành viên role wiki; chạy GRANT wiki TO wikijs rồi xóa pod"


def _add(conn, t, title, content=TEXT, *, status="active", trust="verified_tool", entities=("wikijs",), kind="lesson", eps=None, helpful=0):
    ku = knowledge.add(conn, t, kind=kind, title=title, content=content, entities=list(entities), source_eps=eps or [f"ep-{title}"],
                       source_trust=trust, status=status)
    if helpful:
        ku = knowledge.update(conn, t, ku["id"], {"helpful_cnt": helpful}, "UPDATE", "test")
    return ku


def test_clusters_take_rare_entity_first_and_one_cluster_per_ku(users):
    t = users["admin"].tenant
    with db.session() as conn:
        a = _add(conn, t, "a", entities=["wikijs", "staging"])
        b = _add(conn, t, "b", entities=["wikijs", "staging"])
        c = _add(conn, t, "c", "khác", entities=["staging"])
        _add(conn, t, "d", "khác nữa", entities=["staging"])
        _add(conn, t, "e", "loại khác", entities=["wikijs"], kind="fact")
        got = dedup.clusters(conn, t)
    assert [(e, sorted(m["id"] for m in ms)) for e, ms in got][0] == ("wikijs", sorted([a["id"], b["id"]]))
    assert all(c["id"] not in [m["id"] for m in ms] or e == "staging" for e, ms in got)
    assert sum(len(ms) for _, ms in got) == len({m["id"] for _, ms in got for m in ms})


def test_candidate_saying_same_thing_is_folded_into_active_without_review(users):
    t = users["admin"].tenant
    with db.session() as conn:
        keep = _add(conn, t, "giữ")
        dup = _add(conn, t, "ứng viên", status="candidate", trust="verified_tool")
        tainted = _add(conn, t, "từ log", status="candidate", trust="external_untrusted")
        summary = dedup.run(conn, t, llm.HeuristicProvider())
        after = knowledge.get(conn, t, keep["id"])
        d, x = knowledge.get(conn, t, dup["id"]), knowledge.get(conn, t, tainted["id"])
        assert dedup.run(conn, t, llm.HeuristicProvider())["skipped"] >= 0
    assert summary["confirmed"] == 2 and summary["proposed"] == 0
    assert after["status"] == "active" and after["content"] == TEXT
    assert (d["status"], d["superseded_by"]) == ("rejected", keep["id"]) and x["status"] == "rejected"
    # Nguồn kém tin hơn không được tính là xác nhận cho KU tin cậy.
    assert after["source_eps"] == sorted(["ep-giữ", "ep-ứng viên"])


def test_unchanged_cluster_is_not_sent_to_llm_again(users):
    t = users["admin"].tenant
    with db.session() as conn:
        _add(conn, t, "a")
        _add(conn, t, "b", "Wikijs migration lỗi do role thiếu quyền wiki")
        first = dedup.run(conn, t, llm.HeuristicProvider())
        second = dedup.run(conn, t, llm.HeuristicProvider())
    assert first["clusters"] == 1 and second == {**second, "clusters": 0, "skipped": 1}


def test_active_duplicates_need_review_then_merge_keeps_counters(users):
    admin, alice = users["admin"], users["alice"]
    t = admin.tenant
    with db.session() as conn:
        a = _add(conn, t, "bản 1", helpful=3)
        b = _add(conn, t, "bản 2", helpful=2)
        assert dedup.run(conn, t, llm.HeuristicProvider())["proposed"] == 1
    assert alice.get("/api/knowledge/merge-proposals").status_code == 403
    props = admin.get("/api/knowledge/merge-proposals").json()["proposals"]
    assert len(props) == 1 and {m["id"] for m in props[0]["members"]} == {a["id"], b["id"]}
    with db.session() as conn:  # đang nằm trong đề xuất chờ duyệt thì không bị xét lại
        assert all(a["id"] not in [m["id"] for m in ms] for _, ms in dedup.clusters(conn, t))
    r = admin.post(f"/api/knowledge/merge-proposals/{props[0]['id']}/decide", json={"approve": True, "title": "Wikijs thiếu quyền role"})
    assert r.status_code == 200, r.text
    merged = admin.get(f"/api/ku/{r.json()['merged_into']}").json()
    assert merged["status"] == "active" and merged["title"] == "Wikijs thiếu quyền role" and merged["helpful_cnt"] == 5
    for old in (a, b):
        o = admin.get(f"/api/ku/{old['id']}").json()
        assert o["status"] == "deprecated" and o["superseded_by"] == merged["id"]
    assert admin.post(f"/api/knowledge/merge-proposals/{props[0]['id']}/decide", json={"approve": True}).status_code == 409


def test_rejected_group_is_not_proposed_again_and_stale_proposal_refused(users):
    admin = users["admin"]
    t = admin.tenant
    with db.session() as conn:
        a, b = _add(conn, t, "x1"), _add(conn, t, "x2")
        dedup.run(conn, t, llm.HeuristicProvider())
        pid = dedup.proposals(conn, t)[0]["id"]
        dedup.decide(conn, t, pid, False, "admin")
        assert dedup.apply(conn, t, "wikijs", [knowledge.get(conn, t, a["id"]), knowledge.get(conn, t, b["id"])],
                           [{"ids": [a["id"], b["id"]], "same": True, "keep": a["id"], "title": "", "content": "", "applies_when": "", "reason": ""}]) \
            == {"confirmed": 0, "merged": 0, "proposed": 0}
        c, d = _add(conn, t, "y1", entities=["etcd"]), _add(conn, t, "y2", entities=["etcd"])
        dedup.run(conn, t, llm.HeuristicProvider())
        pid = next(p["id"] for p in dedup.proposals(conn, t) if c["id"] in p["ids"])
        knowledge.invalidate(conn, t, c["id"], None, "admin", "test")
    assert admin.post(f"/api/knowledge/merge-proposals/{pid}/decide", json={"approve": True}).status_code == 409


def test_dedup_api_runs_in_background(users):
    admin = users["admin"]
    with db.session() as conn:
        _add(conn, admin.tenant, "a")
        _add(conn, admin.tenant, "b", status="candidate")
    assert users["alice"].post("/api/knowledge/dedup").status_code == 403
    assert admin.post("/api/knowledge/dedup").json()["running"] is True
    for _ in range(100):
        if not admin.get("/api/knowledge/merge-proposals").json()["running"]:
            break
        time.sleep(0.05)
    assert admin.get("/api/knowledge/lint").json()["dedup"]["confirmed"] == 1


def test_curator_sees_same_topic_kus_with_different_words(users):
    t = users["admin"].tenant
    with db.session() as conn:
        for i in range(30):
            _add(conn, t, f"đệm {i}", f"nội dung {i}", entities=[f"svc-{i}"])
        old = _add(conn, t, "Wikijs thiếu quyền", entities=["cmp-wikijs", "wiki-role"])
        item = {"kind": "lesson", "title": "Lỗi migrations already exists", "content": "pod khởi tạo DB thất bại, cần GRANT",
                "applies_when": "", "entities": ["cmp-wikijs", "wiki-role"]}
        peers = learning.topic_neighbours(conn, t, item, "tenant", set())
        assert [p["id"] for p in peers] == [old["id"]] and peers[0]["via"] == "entity"
        assert llm.HeuristicProvider().curate(item, peers)["op"] == "ADD"  # heuristic không đoán trùng chỉ vì chung thực thể


def test_candidate_only_group_is_merged_without_review(users):
    t = users["admin"].tenant
    with db.session() as conn:
        a = _add(conn, t, "c1", status="candidate", eps=["ep-1"])
        b = _add(conn, t, "c2", "Wikijs crash vì role wikijs không là thành viên role wiki; GRANT wiki TO wikijs", status="candidate", eps=["ep-2"])
        summary = dedup.run(conn, t, llm.HeuristicProvider())
        olds = [knowledge.get(conn, t, i) for i in (a["id"], b["id"])]
        new = knowledge.get(conn, t, olds[0]["superseded_by"])
        assert dedup.proposals(conn, t) == []
    assert summary["merged"] == 2 and summary["proposed"] == 0
    assert {o["status"] for o in olds} == {"rejected"} and olds[1]["superseded_by"] == new["id"]
    assert new["status"] == "candidate" and new["source_eps"] == ["ep-1", "ep-2"]
