"""API tra cứu L3: đồ thị theo thực thể và theo câu hỏi."""
from kagent import db, knowledge


def _ku(tenant, title, content, entities, status="active", scope="tenant"):
    with db.session() as conn:
        return knowledge.add(conn, tenant, kind="fact", title=title, content=content, entities=entities, source_eps=[], source_trust="user", status=status, scope=scope)


def test_graph_lookup_by_query_entity_and_candidates(users):
    a = users["admin"]
    x = _ku(a.tenant, "Index log cmp-backend", "Log cmp-backend nằm trong index k8s-devtest-cmp-backend", ["cmp-backend", "kibana"])
    y = _ku(a.tenant, "Level trong request.level", "Log cmp-backend để level trong request.level", ["cmp-backend"])
    c = _ku(a.tenant, "Ứng viên về grafana", "Dashboard SLO nằm trong folder Ops", ["grafana"], status="candidate")
    with db.session() as conn:
        knowledge.add_edge(conn, a.tenant, x["id"], y["id"], "refines")
    g = a.get("/api/knowledge/graph").json()
    assert {n["id"] for n in g["nodes"]} == {x["id"], y["id"]} and g["edges"] == [{"src": x["id"], "dst": y["id"], "rel": "refines"}]
    assert {e["entity"]: e["n"] for e in g["entities"]}["cmp-backend"] == 2
    assert c["id"] in {n["id"] for n in a.get("/api/knowledge/graph?include_candidates=true").json()["nodes"]}
    assert {n["id"] for n in a.get("/api/knowledge/graph?entity=kibana").json()["nodes"]} == {x["id"]}
    q = a.get("/api/knowledge/graph?q=index log cmp-backend").json()["nodes"]
    assert q[0]["id"] == x["id"] and "score" in q[0]


def test_graph_hides_other_users_personal_knowledge(users):
    a, alice = users["admin"], users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
    mine = _ku(alice.tenant, "Sở thích", "Tôi thích câu trả lời ngắn", [], scope=f"user:{uid}")
    assert mine["id"] in {n["id"] for n in alice.get("/api/knowledge/graph").json()["nodes"]}
    assert mine["id"] not in {n["id"] for n in a.get("/api/knowledge/graph").json()["nodes"]}
