"""Panel sự cố: ảnh chụp theo lượt điều tra (incident_round), entity trích bằng code, API /api/incidents/{id}/panel."""
import pytest

from kagent import db, incidents
from kagent.incidents.rounds import extract_entities, mark_suspect
from test_incidents import _web_incident, flow, tenant  # noqa: F401  (flow là fixture)


def ids(items):
    return [e["id"] for e in items]


# ---------- trích entity ----------

def test_entities_from_alert_labels_and_pod_owner():
    out = extract_entities([{"labels": {"alertname": "X", "namespace": "app", "pod": "currencyservice-7f9c8d6b5-x2k4p"}}], [])
    assert set(ids(out)) == {"app/pod/currencyservice-7f9c8d6b5-x2k4p", "app/deployment/currencyservice"}
    assert all(e["from"] == ["alert"] for e in out)


@pytest.mark.parametrize("command, expected", [
    ("describe svc currencyservice -n app", {"app/service/currencyservice"}),
    ("logs deploy/frontend -n app --tail 200 | grep -i refused", {"app/deployment/frontend"}),
    ("get pods -n app -o wide", set()),
    ("rollout history deploy/checkout -n shop", {"shop/deployment/checkout"}),
    ("get deploy web api -n shop", {"shop/deployment/web", "shop/deployment/api"}),
    ("get nodes worker-1", {"-/node/worker-1"}),
])
def test_entities_from_kubectl(command, expected):
    assert set(ids(extract_entities([], [{"tool": "k8s.kubectl_read", "args": {"command": command}}]))) == expected


def test_entities_from_promql_es_and_generic_args():
    steps = [{"tool": "prom.query_range", "args": {"query": 'rate(x{namespace="app",pod="web-6d8f7c9b4-abcde"}[5m])'}},
             {"tool": "prom.query", "args": {"query": 'x{namespace="app",pod=~"api-.*"}'}},  # regex thì bỏ
             {"tool": "es.search_logs", "args": {"query": 'kubernetes.namespace_name:"app" AND kubernetes.pod_name:"db-0"'}},
             {"tool": "bench.describe_resource", "args": {"resource_type": "pod", "name": "cart-79598c9984-fs47p", "namespace": "app"}},
             {"tool": "bench.check_node_service_status", "args": {"node_name": "worker-01", "service_name": "kubelet"}}]
    got = set(ids(extract_entities([], steps)))
    assert {"app/pod/web-6d8f7c9b4-abcde", "app/deployment/web", "app/pod/db-0", "app/statefulset/db", "app/deployment/cart", "-/node/worker-01"} <= got
    assert not any("api" in i or "kubelet" in i for i in got)


def test_missing_namespace_filled_when_only_one_seen():
    out = extract_entities([{"labels": {"namespace": "app", "deployment": "web"}}], [{"tool": "es.search_logs", "args": {"query": 'service:"web"'}}])
    assert set(ids(out)) == {"app/deployment/web", "app/service/web"}


def test_suspect_only_when_exactly_one_name_matches():
    items = extract_entities([{"labels": {"namespace": "s", "deployment": "cart"}}, {"labels": {"namespace": "s", "deployment": "redis-cart"}}], [],
                             "redis-cart hết bộ nhớ")
    assert [e["id"] for e in items if e["suspect"]] == ["s/deployment/redis-cart"]  # "cart" là chuỗi con của tên khớp dài hơn
    items = extract_entities([{"labels": {"namespace": "s", "deployment": "cart"}}, {"labels": {"namespace": "s", "deployment": "redis"}}], [],
                             "cart và redis đều lỗi")
    assert not any(e["suspect"] for e in items)
    items = [{"id": "s/service/web", "kind": "service", "name": "web", "suspect": False},
             {"id": "s/deployment/web", "kind": "deployment", "name": "web", "suspect": False}]
    mark_suspect(items, "web trả 500")
    assert [e["id"] for e in items if e["suspect"]] == ["s/deployment/web"]  # cùng tên: chọn workload


# ---------- lượt điều tra và API ----------

def test_each_investigation_records_a_round(flow, users):  # noqa: F811
    iid = _web_incident(users)
    for _ in range(2):
        with db.session() as conn:
            incidents.investigate(conn, tenant(users), iid)
    d = flow.get(f"/api/incidents/{iid}/panel").json()
    assert [r["round"] for r in d["rounds"]] == [1, 2]
    assert d["round"]["round"] == 2 and not d["round"].get("synthesized")
    assert "root_cause" in d["round"]["report"] and isinstance(d["round"]["entities"], list)
    assert any(e["namespace"] == "web" for e in d["round"]["entities"])
    assert flow.get(f"/api/incidents/{iid}/panel?round=1").json()["round"]["round"] == 1
    assert flow.get(f"/api/incidents/{iid}/panel?round=9").status_code == 404
    assert d["state"] and "waiting" in d  # dữ liệu như trang sự cố


def test_panel_synthesizes_round_for_incidents_investigated_before_rounds(flow, users):  # noqa: F811
    iid = _web_incident(users)
    with db.session() as conn:
        incidents.investigate(conn, tenant(users), iid)
        conn.execute("DELETE FROM incident_round WHERE incident_id=?", (iid,))
    d = flow.get(f"/api/incidents/{iid}/panel").json()
    assert d["rounds"] == [] and d["round"]["synthesized"] and d["round"]["round"] == 1


def test_panel_without_investigation_has_no_round(flow, users):  # noqa: F811
    iid = _web_incident(users)
    d = flow.get(f"/api/incidents/{iid}/panel").json()
    assert d["rounds"] == [] and d["round"] is None

