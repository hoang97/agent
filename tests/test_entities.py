"""Chuẩn hoá tên thực thể và gộp tên do admin chọn."""
from kagent import db, entities, knowledge

V = entities.Vocab(["kibana_devtest.search_logs", "kibana_devtest.count_logs", "k8s_devtest.list_pods", "db.list_pods"], {})


def n(*names, v=V):
    return entities.normalize(None, "t", list(names), v)


def test_case_and_spacing_collapse():
    assert n("Kibana", "kibana", "  Elastic ") == ["elasticsearch", "kibana"]
    assert n("Kubernetes") == ["k8s"]


def test_field_and_index_suffixes_dropped():
    assert n("kubernetes.pod_name.keyword", "module.keyword") == ["kubernetes.pod_name", "module"]
    assert n("k8s-devtest-cmp-backend-*", "k8s-devtest-*") == ["k8s-devtest", "k8s-devtest-cmp-backend"]


def test_http_codes_share_one_name():
    assert n("lỗi 500", "500 errors", "status:500", "status_code 500", "HTTP 500", "Internal Server Error") == ["http 500"]
    assert n("5xx errors", "Bad Gateway", "502") == ["http 502", "http 5xx"]


def test_tools_resolve_to_full_name_and_connection():
    assert n("search_logs") == n("kibana_devtest.search_logs") == n("tool:kibana_devtest.search_logs") == ["kibana_devtest", "tool:kibana_devtest.search_logs"]
    assert n("list_pods") == ["list_pods"]  # hai kết nối cùng có lệnh này: không đoán
    assert n("tool:k8s.scale") == ["tool:k8s.scale"]


def test_aliases_apply():
    v = entities.Vocab([], {"kibana": "kibana_devtest"})
    assert n("Kibana", v=v) == ["kibana_devtest"]


def test_add_normalizes_and_merge_rewrites_kus_and_future(users):
    admin = users["admin"]
    with db.session() as conn:
        a = knowledge.add(conn, admin.tenant, kind="fact", title="a", content="a", entities=["Kibana", "lỗi 500"], source_eps=[], source_trust="user")
        assert a["entities"] == ["http 500", "kibana"]
    r = admin.post("/api/knowledge/entities/merge", json={"sources": ["kibana", "Kibana UI"], "target": "Kibana_Devtest"})
    assert r.status_code == 200, r.text
    assert r.json() == {"target": "kibana_devtest", "merged": ["kibana", "kibana ui"], "kus": 1}
    with db.session() as conn:
        assert knowledge.get(conn, admin.tenant, a["id"])["entities"] == ["http 500", "kibana_devtest"]
        b = knowledge.add(conn, admin.tenant, kind="fact", title="b", content="b", entities=["KIBANA"], source_eps=[], source_trust="user")
        assert b["entities"] == ["kibana_devtest"]
    assert users["alice"].post("/api/knowledge/entities/merge", json={"sources": ["x"], "target": "y"}).status_code == 403
    assert admin.post("/api/knowledge/entities/merge", json={"sources": ["tool:x.y"], "target": "y"}).status_code == 400


def test_renormalize_runs_once_per_version(users):
    admin = users["admin"]
    with db.session() as conn:
        ku = knowledge.add(conn, admin.tenant, kind="fact", title="a", content="a", entities=[], source_eps=[], source_trust="user")
        conn.execute("UPDATE knowledge_unit SET entities='[\"Kibana\", \"kibana\"]' WHERE id=?", (ku["id"],))
        conn.execute("DELETE FROM app_setting WHERE key='entity_norm'")
        assert entities.renormalize(conn) >= 1
        assert knowledge.get(conn, admin.tenant, ku["id"])["entities"] == ["kibana"]
        assert entities.renormalize(conn) == 0
