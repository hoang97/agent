"""API tra cứu L3: đồ thị theo thực thể và theo câu hỏi."""
from aiops import db, knowledge


def _ku(tenant, title, content, entities, status="active", scope="tenant"):
    with db.session() as conn:
        return knowledge.add(conn, tenant, kind="fact", title=title, content=content, entities=entities, source_eps=[], source_trust="user", status=status, scope=scope)


def test_graph_with_entity_adds_its_kus_beyond_the_default_limit(users, monkeypatch):
    from aiops import api
    a = users["admin"]
    monkeypatch.setattr(api, "GRAPH_LIMIT", 2)
    for i in range(3):
        _ku(a.tenant, f"đệm {i}", f"nội dung {i}", [f"svc-{i}"])
    rare = _ku(a.tenant, "Về etcd", "etcd cần defrag", ["etcd"])
    with db.session() as conn:
        conn.execute("UPDATE knowledge_unit SET recorded_at='2000-01-01T00:00:00+00:00' WHERE id=?", (rare["id"],))
    assert rare["id"] not in {n["id"] for n in a.get("/api/knowledge/graph").json()["nodes"]}
    nodes = a.get("/api/knowledge/graph?entity=etcd").json()["nodes"]
    assert len(nodes) == 3 and rare["id"] in {n["id"] for n in nodes}


def test_graph_lookup_by_query_entity_and_candidates(users):
    a = users["admin"]
    x = _ku(a.tenant, "Index log svc-backend", "Log svc-backend nằm trong index k8s-staging-svc-backend", ["svc-backend", "kibana"])
    y = _ku(a.tenant, "Level trong request.level", "Log svc-backend để level trong request.level", ["svc-backend"])
    c = _ku(a.tenant, "Ứng viên về grafana", "Dashboard SLO nằm trong folder Ops", ["grafana"], status="candidate")
    with db.session() as conn:
        knowledge.add_edge(conn, a.tenant, x["id"], y["id"], "refines")
    g = a.get("/api/knowledge/graph").json()
    assert {n["id"] for n in g["nodes"]} == {x["id"], y["id"]} and g["edges"] == [{"src": x["id"], "dst": y["id"], "rel": "refines"}]
    assert {e["entity"]: e["n"] for e in g["entities"]}["svc-backend"] == 2
    assert c["id"] in {n["id"] for n in a.get("/api/knowledge/graph?include_candidates=true").json()["nodes"]}
    # Chọn thực thể không lọc mất phần còn lại của đồ thị (giao diện chỉ làm nổi phần liên quan).
    assert {n["id"] for n in a.get("/api/knowledge/graph?entity=kibana").json()["nodes"]} == {x["id"], y["id"]}
    q = a.get("/api/knowledge/graph?q=index log svc-backend").json()["nodes"]
    assert q[0]["id"] == x["id"] and "score" in q[0]


def test_graph_hides_other_users_personal_knowledge(users):
    a, alice = users["admin"], users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
    mine = _ku(alice.tenant, "Sở thích", "Tôi thích câu trả lời ngắn", [], scope=f"user:{uid}")
    assert mine["id"] in {n["id"] for n in alice.get("/api/knowledge/graph").json()["nodes"]}
    assert mine["id"] not in {n["id"] for n in a.get("/api/knowledge/graph").json()["nodes"]}


def _edges(tenant, rel="related"):
    with db.session() as conn:
        return {(r["src"], r["dst"]) for r in conn.execute("SELECT src, dst FROM ku_edge WHERE tenant_id=? AND rel=?", (tenant, rel))}


def test_related_links_rare_shared_entities_not_common_ones(users):
    t = users["admin"].tenant
    # "staging" có ở mọi KU nên IDF ≈ 0; "etcd-defrag" hiếm nên chung nó mới là cùng chủ đề.
    fillers = [_ku(t, f"Ghi chú {i}", f"nội dung khác {i}", ["staging", f"svc-{i}"]) for i in range(40)]
    a = _ku(t, "Defrag etcd", "Chạy defrag từng member khi db size lớn", ["staging", "etcd-defrag", "etcd"])
    b = _ku(t, "Cảnh báo db size etcd", "Ngưỡng cảnh báo db size trước khi defrag", ["staging", "etcd-defrag", "etcd"])
    with db.session() as conn:
        assert knowledge.link_related(conn, t, a["id"]) == 1
        assert knowledge.link_related(conn, t, a["id"]) == 0  # đã nối rồi thì không nối lại
        assert knowledge.link_related(conn, t, fillers[0]["id"]) == 0
    assert _edges(t) == {tuple(sorted((a["id"], b["id"])))}


def test_related_respects_personal_scope(users):
    t = users["admin"].tenant
    mine = _ku(t, "Của alice", "defrag etcd theo cách của tôi", ["etcd-defrag", "etcd", "etcd-member"], scope="user:u1")
    other = _ku(t, "Của bob", "defrag etcd theo cách của bob", ["etcd-defrag", "etcd", "etcd-member"], scope="user:u2")
    shared = _ku(t, "Chung", "defrag etcd từng member", ["etcd-defrag", "etcd", "etcd-member"])
    for i in range(40):
        _ku(t, f"Khác {i}", "x", [f"svc-{i}"])
    with db.session() as conn:
        knowledge.link_related(conn, t, mine["id"])
    assert _edges(t) == {tuple(sorted((mine["id"], shared["id"])))}
    assert other["id"] not in {x for e in _edges(t) for x in e}

