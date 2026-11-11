"""Connector thật, lớp mạng an toàn, template và quản lý kết nối."""
import json
import textwrap

import httpx
import pytest

import fakes
from conftest import Session, create_connections, tool_steps
from kagent import connections, db, net, tools
from kagent.connectors import base, elasticsearch, grafana, k8s, sql, web
from kagent.connectors.base import ConnectorError


def params(**kw):
    return {"_allow_private": False, **kw}


K8S = params(api_url="https://k8s.test:6443", token=fakes.K8S_TOKEN, namespace="default")


# ---------- lớp mạng ----------

@pytest.mark.parametrize("host,allow,ok", [
    ("example.test", False, True), ("internal.test", False, False), ("internal.test", True, True),
    ("metadata.test", True, False), ("loop.test", True, False), ("127.0.0.1", True, False), ("169.254.169.254", True, False),
    ("::1", True, False), ("::ffff:127.0.0.1", True, False), ("khong-co.test", True, False),
])
def test_check_host(host, allow, ok):
    if ok:
        assert net.check_host(host, 443, allow)
    else:
        with pytest.raises(net.NetError):
            net.check_host(host, 443, allow)


@pytest.mark.parametrize("url", ["ftp://example.test", "https://user:pw@example.test", "example.test", "https:///x"])
def test_check_url_rejects_bad_urls(url):
    with pytest.raises(net.NetError):
        net.check_url(url, True)


def test_pinned_transport_connects_to_checked_ip_with_original_host(monkeypatch):
    seen = {}

    def fake_handle(self, request):
        seen.update(url=str(request.url), host=request.headers["host"], sni=request.extensions.get("sni_hostname"))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle)
    monkeypatch.setattr(net, "transport_override", None)
    with net.client("https://example.test:8443", allow_private=False) as c:
        c.get("/x")
    assert seen == {"url": f"https://{fakes.PUBLIC_IP}:8443/x", "host": "example.test:8443", "sni": "example.test"}
    with net.client("https://internal.test", allow_private=False) as c, pytest.raises(net.NetError):
        c.get("/")


def test_response_size_capped(infra, monkeypatch):
    monkeypatch.setattr(net, "transport_override", lambda: httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * (base.MAX_BYTES + 10))))
    with net.client("https://example.test", allow_private=False) as c, pytest.raises(ConnectorError, match="quá lớn"):
        base.call(c, "GET", "/")


# ---------- connector ----------

def test_k8s_reads_and_writes(infra):
    assert k8s.ping(K8S)["version"] == "v1.31.0"
    assert [p["name"] for p in k8s.list_pods(K8S, {})["pods"]] == ["web-0", "web-1", "web-2", "api-0", "api-1"]
    assert k8s.list_deployments(K8S, {"namespace": "payments"})["deployments"][0] == {"name": "checkout", "replicas": 4, "ready": 4, "images": ["checkout:1"]}
    assert "ERROR db timeout" in k8s.pod_logs(K8S, {"pod": "web-0"})["log"]
    assert k8s.scale_check(K8S, {"deployment": "web", "replicas": 5})["would_change"] == "default/web: 3 → 5"
    assert k8s.scale(K8S, {"deployment": "web", "replicas": 5}) == {"namespace": "default", "deployment": "web", "from": 3, "to": 5}
    assert infra.deployments["default"]["web"] == 5
    assert k8s.rollout_restart(K8S, {"deployment": "api"})["restarted"] == "default/api"
    assert k8s.delete_pod(K8S, {"pod": "api-1"})["deleted"] == "default/api-1"


@pytest.mark.parametrize("args,message", [
    ({"deployment": "web", "replicas": 99}, "khoảng"), ({"deployment": "khong-co", "replicas": 1}, "HTTP 404"),
    ({"deployment": "Web!", "replicas": 1}, "không hợp lệ"), ({"deployment": "web", "replicas": 1, "namespace": "../x"}, "không hợp lệ"),
])
def test_k8s_validation(infra, args, message):
    with pytest.raises(ConnectorError, match=message):
        k8s.scale(K8S, args)


def test_k8s_allowed_namespaces_and_bad_token(infra):
    with pytest.raises(ConnectorError, match="không nằm trong"):
        k8s.list_pods({**K8S, "allowed_namespaces": ["payments"]}, {"namespace": "default"})
    with pytest.raises(ConnectorError, match="401"):
        k8s.list_pods({**K8S, "token": "sai"}, {})


def test_k8s_exec_parses_command(infra):
    out = k8s.exec_command(K8S, {"pod": "api-0", "command": 'psql -c "SELECT 1"'})
    assert out["command"] == ["psql", "-c", "SELECT 1"]
    with pytest.raises(ConnectorError, match="lệnh không hợp lệ"):
        k8s.exec_command(K8S, {"pod": "api-0", "command": 'psql -c "chưa đóng'})


def test_grafana(infra):
    p = params(url="https://grafana.test", token=fakes.GRAFANA_TOKEN, default_datasource_uid="prom")
    assert grafana.ping(p)["version"] == "11.2.0"
    assert grafana.search_dashboards(p, {"query": "slo"})["dashboards"][0]["uid"] == "slo"
    assert grafana.prometheus_query(p, {"expr": "up"})["series"][0]["metric"] == {"job": "api"}
    assert grafana.list_alerts(p, {})["alerts"][0]["labels"]["alertname"] == "HighCPU"
    assert grafana.create_silence_check(p, {"matchers": "alertname=HighCPU"})["would_silence_alerts"] == 1
    assert grafana.create_silence(p, {"matchers": "alertname=HighCPU", "duration_minutes": 30})["silence_id"] == "s1"
    assert infra.silences[0]["matchers"] == [{"name": "alertname", "value": "HighCPU", "isRegex": False, "isEqual": True}]
    with pytest.raises(ConnectorError, match="matchers"):
        grafana.create_silence(p, {"matchers": "khong-dung"})


def test_elasticsearch(infra):
    p = params(url="https://es.test:9200", api_key=fakes.ES_KEY)
    assert elasticsearch.search_logs(p, {"query": "error"})["total"] == 2
    assert [i["index"] for i in elasticsearch.list_indices(p, {})["indices"]] == ["logs-app", "logs-old"]
    assert elasticsearch.delete_index_check(p, {"index": "logs-old"})["would_delete"]["docs.count"] == "40"
    assert elasticsearch.delete_index(p, {"index": "logs-old"}) == {"deleted_index": "logs-old"}
    with pytest.raises(ConnectorError, match="không hợp lệ"):
        elasticsearch.delete_index(p, {"index": "logs-*"})


def test_elasticsearch_through_kibana_console_proxy(infra):
    p = params(url="https://kibana.test", api_key=fakes.ES_KEY)
    assert elasticsearch.ping(p)["version"] == "8.15.0"
    assert elasticsearch.search_logs(p, {"query": "error"})["total"] == 2
    assert [i["index"] for i in elasticsearch.list_indices(p, {})["indices"]] == ["logs-app", "logs-old"]
    assert elasticsearch.delete_index(p, {"index": "logs-old"}) == {"deleted_index": "logs-old"}
    proxied = [r for r in infra.requests if r.url.path == "/api/console/proxy"]
    assert {r.url.params["method"] for r in proxied} == {"GET", "POST", "DELETE"}
    assert any(r.url.params["path"].startswith("/_cat/indices?format=json") for r in proxied)


def test_elasticsearch_direct_sends_each_request_once(infra):
    p = params(url="https://es.test:9200", api_key=fakes.ES_KEY)
    elasticsearch.delete_index(p, {"index": "logs-old"})
    assert [r.method for r in infra.requests if r.url.path == "/logs-old"] == ["DELETE"]


def test_redirect_error_says_where(infra):
    p = params(url="https://example.test", api_key=fakes.ES_KEY)
    with pytest.raises(ConnectorError, match="chuyển hướng tới http://internal.test/admin"):
        with base.http(p, "url") as c:
            base.call(c, "GET", "/moved")


@pytest.mark.parametrize("host, hinted", [("postgres-cluster-rw.postgres-cluster", True), ("db.payments.svc", True),
                                          ("pg.default.svc.cluster.local", True), ("db.example.com", False), ("10.0.0.5", False), ("db.test", False)])
def test_host_hint_for_kubernetes_service_names(host, hinted):
    assert bool(base.host_hint(host)) is hinted


def test_sql_connect_error_names_host_and_hints(monkeypatch):
    def refuse(*a, **k):
        raise TimeoutError("connection timeout expired")

    monkeypatch.setattr(sql, "_open", refuse)
    with pytest.raises(ConnectorError) as err:
        sql.pg_query(params(host="postgres-cluster-rw.postgres-cluster", database="backend", user="u"), {"sql": "select 1"})
    msg = str(err.value)
    assert "postgres-cluster-rw.postgres-cluster:5432" in msg and "connection timeout expired" in msg and "Kubernetes" in msg


def test_web_fetch_strips_scripts_and_blocks_redirect_to_internal(infra):
    page = web.fetch_page(params(), {"url": "https://example.test/"})
    assert page["title"] == "Trang thử" and "Nội dung trang." in page["text"] and "x()" not in page["text"]
    with pytest.raises(net.NetError):
        web.fetch_page(params(), {"url": "https://example.test/moved"})
    with pytest.raises(ConnectorError, match="không nằm trong"):
        web.fetch_page(params(allowed_domains=["other.test"]), {"url": "https://example.test/"})


@pytest.mark.parametrize("path", ["x", "//evil.test/a", "/a/../b", "https://evil.test/"])
def test_http_api_path_must_be_relative(infra, path):
    with pytest.raises(ConnectorError):
        web.api_get(params(base_url="https://example.test"), {"path": path})


def test_sql_read_only_and_write(infra):
    p = params(host="db.test", database="shop", user="u")
    assert sql.pg_query(p, {"sql": "SELECT count(*) FROM orders"})["rows"] == [[6]]
    with pytest.raises(ConnectorError, match="CSDL báo lỗi"):
        sql.pg_query(p, {"sql": "DELETE FROM orders"})
    assert sql.pg_execute_check(p, {"sql": "UPDATE orders SET status='paid' WHERE status='failed'"})["would_affect_rows"] == 2
    assert sql.pg_query(p, {"sql": "SELECT count(*) FROM orders WHERE status='failed'"})["rows"] == [[2]]  # chạy thử đã rollback
    assert sql.pg_execute(p, {"sql": "UPDATE orders SET status='paid' WHERE status='failed'"})["affected_rows"] == 2
    with pytest.raises(net.NetError):
        sql.pg_query(params(host="internal.test", database="x", user="u"), {"sql": "SELECT 1"})


# ---------- template ----------

def test_shipped_templates_valid():
    templates = tools.load_templates()
    assert set(templates) == {"k8s", "grafana", "elasticsearch", "web", "http_api", "postgres", "mysql", "data", "prometheus", "alertmanager"}
    for t in templates.values():
        for s in t.tools.values():
            assert tools.TIERS.index(s.risk_tier) >= tools.TIERS.index(tools.minimum_tier(s.action_class, s.reversible, s.blast_radius, s.data_sensitivity))
            assert s.dry_run == "none" or s.check is not None


GOOD = """
template: thu
title: Thử
ping: web:ping
fields:
  - {name: base_url, type: url, required: true}
tools:
  - name: doc
    description: đọc
    handler: web:fetch_page
    action_class: read
    input_schema:
      properties: {url: {type: string}}
"""


@pytest.mark.parametrize("change,message", [
    (("action_class: read", "action_class: write\n    reversible: \"yes\"\n    risk_tier: R0"), "thấp hơn mức tối thiểu"),
    (("handler: web:fetch_page", "handler: os:system"), "kagent.connectors.os"),
    (("type: url", "type: password"), "type phải thuộc"),
    (("ping: web:ping", "ping: web:khong_co"), "khong_co"),
    (("    description: đọc\n", ""), "thiếu description"),
])
def test_invalid_template_rejected(tmp_path, change, message):
    (tmp_path / "t.yaml").write_text(textwrap.dedent(GOOD.replace(*change)), encoding="utf-8")
    with pytest.raises(tools.CatalogError, match=message):
        tools.load_templates(tmp_path)


# ---------- quản lý kết nối qua API ----------

def new_k8s(user, **overrides):
    body = {"template": "k8s", "name": "my_k8s", "scope": "user", "envs": ["dev"], "params": {"api_url": "https://k8s.test:6443"},
            "secrets": {"token": fakes.K8S_TOKEN}, **overrides}
    return user.post("/api/connections", json=body)


def test_templates_endpoint(users):
    names = {t["name"] for t in users["alice"].get("/api/templates").json()}
    assert "k8s" in names and "postgres" in names


def test_member_creates_personal_connection_private_to_them(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    r = new_k8s(alice)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["personal"] and c["secrets_set"] == {"kubeconfig": False, "token": True} and fakes.K8S_TOKEN not in r.text
    tools_ = {t["tool"]: t for t in c["tools"]}
    assert tools_["my_k8s.list_pods"]["enabled"] == 1 and tools_["my_k8s.scale"]["enabled"] == 0  # ghi mặc định tắt
    assert all(x["name"] != "my_k8s" for x in bob.get("/api/connections").json())
    assert all(x["name"] != "my_k8s" for x in admin.get("/api/connections").json())
    assert all(not t["tool"].startswith("my_k8s.") for t in bob.get("/api/tools").json())
    with db.session() as conn:
        stored = conn.execute("SELECT secrets FROM connection WHERE name='my_k8s'").fetchone()["secrets"]
    assert fakes.K8S_TOKEN not in stored


def test_personal_tool_usable_only_by_owner(users):
    from kagent import gateway
    alice, bob = users["alice"], users["bob"]
    new_k8s(alice)
    with db.session() as conn:
        ids = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM app_user")}
        mine = gateway.invoke(conn, gateway.CallContext(alice.tenant, ids["alice"], "dev", "xem pod"), "my_k8s.list_pods", {})
        theirs = gateway.invoke(conn, gateway.CallContext(alice.tenant, ids["bob"], "dev", "xem pod"), "my_k8s.list_pods", {})
        other_env = gateway.invoke(conn, gateway.CallContext(alice.tenant, ids["alice"], "prod", "xem pod"), "my_k8s.list_pods", {})
    assert mine["status"] == "ok" and theirs["status"] == "blocked" and "người khác" in theirs["reasons"][0]
    assert other_env["status"] == "blocked" and "môi trường prod" in other_env["reasons"][0]
    assert bob.patch("/api/tools/my_k8s.scale", json={"enabled": True}).status_code == 404
    assert alice.patch("/api/tools/my_k8s.scale", json={"enabled": True}).json()["enabled"] == 1


def test_shared_connection_requires_admin(users):
    assert new_k8s(users["alice"], scope="tenant", name="shared_k8s").status_code == 403
    assert new_k8s(users["admin"], scope="tenant", name="shared_k8s").status_code == 200
    c = next(x for x in users["alice"].get("/api/connections").json() if x["name"] == "shared_k8s")
    assert c["can_manage"] is False and c["secrets_set"] == {"kubeconfig": False, "token": True}
    assert users["alice"].patch(f"/api/connections/{c['id']}", json={"title": "x"}).status_code == 403
    assert users["alice"]._call("DELETE", f"/api/connections/{c['id']}").status_code == 403


def test_private_network_needs_org_admin(client, users):
    alice = users["alice"]
    r = new_k8s(alice, params={"api_url": "https://internal.test:6443"})
    assert r.status_code == 400 and "nội bộ" in r.text
    assert new_k8s(alice, params={"api_url": "https://internal.test:6443"}, allow_private=True).status_code == 403
    assert new_k8s(users["admin"], params={"api_url": "https://internal.test:6443"}, allow_private=True, name="noi_bo").status_code == 200
    assert new_k8s(users["admin"], params={"api_url": "https://metadata.test"}, allow_private=True, name="meta").status_code == 400


@pytest.mark.parametrize("body,message", [
    ({"name": "K8S"}, "tên kết nối"), ({"name": "k8s"}, "đã có"), ({"template": "khong"}, "template"),
    ({"secrets": {}}, "cần token"), ({"params": {}}, "cần api_url"), ({"envs": ["qa"]}, "môi trường"),
    ({"params": {"api_url": "https://k8s.test", "bogus": 1}}, "không có trong template"),
])
def test_connection_validation(users, body, message):
    r = new_k8s(users["alice"], **body)
    assert r.status_code in (400, 409) and message in r.text


def test_update_keeps_secret_when_blank_and_test_endpoint(users, infra):
    alice = users["alice"]
    c = new_k8s(alice).json()
    assert alice.post(f"/api/connections/{c['id']}/test").json()["ok"] is True
    alice.patch(f"/api/connections/{c['id']}", json={"secrets": {"token": ""}, "params": {"namespace": "payments"}})
    assert alice.post(f"/api/connections/{c['id']}/test").json()["ok"] is True
    alice.patch(f"/api/connections/{c['id']}", json={"secrets": {"token": "sai"}})
    result = alice.post(f"/api/connections/{c['id']}/test").json()
    assert result["ok"] is False and "401" in result["error"]


def test_env_binding_follows_connection(users):
    alice = users["alice"]
    c = new_k8s(alice, envs=["dev", "staging"]).json()
    alice.patch(f"/api/connections/{c['id']}", json={"envs": ["staging"]})
    tool = next(t for t in alice.get("/api/tools").json() if t["tool"] == "my_k8s.list_pods")
    assert tool["effective_envs"] == ["staging"] and tool["catalog_envs"] == ["staging"]
    assert alice.patch("/api/tools/my_k8s.list_pods", json={"envs": ["dev"]}).status_code == 400


def test_remove_connection_blocks_its_tools(users):
    admin, alice = users["admin"], users["alice"]
    k8s_conn = next(c for c in admin.get("/api/connections").json() if c["name"] == "k8s")
    assert admin._call("DELETE", f"/api/connections/{k8s_conn['id']}").status_code == 200
    assert all(not t["tool"].startswith("k8s.") for t in alice.get("/api/tools").json())
    assert tool_steps(alice.run("xem pod")) == []  # agent không còn thấy công cụ


def test_disabled_connection_blocks_execution_of_pending_approval(users):
    admin, alice = users["admin"], users["alice"]
    step = tool_steps(alice.run("scale deployment web lên 4 replica"))[0]
    k8s_conn = next(c for c in admin.get("/api/connections").json() if c["name"] == "k8s")
    admin.patch(f"/api/connections/{k8s_conn['id']}", json={"status": "disabled"})
    assert alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).status_code == 409


def test_deny_rule_matches_template_name_for_any_connection(users):
    admin, alice = users["admin"], users["alice"]
    new_k8s(admin, scope="tenant", name="k8s_prod", envs=["prod"])
    with db.session() as conn:
        conn.execute("UPDATE tool_registry SET enabled=1 WHERE tool='k8s_prod.exec'")
    step = tool_steps(alice.run("exec vào pod web-0 chạy `ls`", env="prod"))
    assert all(s["decision"] == "blocked" and "k8s.exec" in s["reasons"][0] for s in step)


def test_old_simulated_tools_marked_removed_on_startup(client):
    with db.session() as conn:
        tid = conn.execute("SELECT id FROM tenant LIMIT 1").fetchone()["id"]
        conn.execute("INSERT INTO tool_registry(tenant_id,tool,server,action_class,risk_tier,reversible,blast_radius,data_sensitivity,dry_run,"
                     "description,input_schema,envs,enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                     (tid, "sim.old", "mcp-sim", "read", "R0", "n/a", "object", "internal", "none", "cũ", "{}", '["dev"]'))
        connections.sync_all(conn)
        assert conn.execute("SELECT status FROM tool_registry WHERE tool='sim.old'").fetchone()["status"] == "removed"


def test_other_tenant_cannot_see_connections(client, users):
    finance = Session(client, "admin", "team-finance")
    assert [c["name"] for c in finance.get("/api/connections").json()] == ["data"]  # chỉ kết nối nội bộ của chính tenant đó
    k8s_conn = next(c for c in users["admin"].get("/api/connections").json() if c["name"] == "k8s")
    assert finance.patch(f"/api/connections/{k8s_conn['id']}", json={"title": "x"}).status_code == 404
