"""Link mở lại bước truy vấn trên công cụ gốc (links.py) và cách trang sự cố gắn link vào bước, mắt xích 5 Whys."""
import json
from urllib.parse import parse_qs, unquote, urlsplit

from aiops import connections, db, links

AT = "2026-11-12T08:00:00+00:00"


def _tenant(users):
    return users["admin"].tenant


def _add(users, name, template, params, secrets):
    with db.session() as conn, db.tx(conn):
        admin = conn.execute("SELECT id FROM app_user WHERE username='admin'").fetchone()["id"]
        connections.create(conn, _tenant(users), admin, is_admin=True, is_org_admin=True, template=template, name=name, scope="tenant",
                           envs=["dev"], roles=["member"], params=params, secrets=secrets)


def _link(users, tool, args):
    with db.session() as conn:
        return links.source_link(conn, _tenant(users), tool, args, AT)


def test_grafana_explore_link_keeps_query_datasource_and_range(users):
    out = _link(users, "grafana.prometheus_query", {"expr": 'sum(rate(http_requests_total{code="500"}[5m]))', "range_minutes": 30})
    u = urlsplit(out["url"])
    assert out["label"] == "Mở trong Grafana Explore" and u.netloc == "grafana.test" and u.path == "/explore"
    left = json.loads(parse_qs(u.query)["left"][0])
    assert left["datasource"] == "prom" and left["queries"][0]["expr"] == 'sum(rate(http_requests_total{code="500"}[5m]))'
    assert int(left["range"]["to"]) - int(left["range"]["from"]) == 40 * 60 * 1000  # 30 phút trước + 10 phút sau mốc
    assert _link(users, "grafana.list_alerts", {}) is None


def test_prometheus_link_uses_ui_url_and_end_time(users):
    _add(users, "prom", "prometheus", {"url": "https://prom.test", "ui_url": "https://example.test/prom"}, {})
    out = _link(users, "prom.query_range", {"expr": "up == 0", "minutes": 120})
    q = parse_qs(urlsplit(out["url"]).query)
    assert out["url"].startswith("https://example.test/prom/graph?")
    assert q["g0.expr"] == ["up == 0"] and q["g0.range_input"] == ["130m"] and q["g0.end_input"] == ["2026-11-12 08:10:00"]
    assert parse_qs(urlsplit(_link(users, "prom.query", {"expr": "up", "time": "2026-11-12T07:00:00Z"})["url"]).query)["g0.end_input"] == ["2026-11-12 07:00:00"]
    assert _link(users, "prom.query", {}) is None and _link(users, "prom.list_targets", {}) is None


def test_kibana_link_only_for_kibana_address_and_escapes_query(users):
    assert _link(users, "logs.search_logs", {"query": "level:ERROR"}) is None  # url là Elasticsearch :9200, không có Discover
    _add(users, "kb", "elasticsearch", {"url": "https://kibana.test:5601"}, {"api_key": "k"})
    url = _link(users, "kb.search_logs", {"query": "msg:\"it's down!\"", "minutes": 15})["url"]
    assert url.startswith("https://kibana.test:5601/app/discover#/?_g=")
    text = unquote(url)
    assert "from:'2026-11-12T07:45:00.000Z',to:'2026-11-12T08:10:00.000Z'" in text
    assert "query:(language:lucene,query:'msg:\"it!'s down!!\"')" in text


def test_no_link_for_unsafe_scheme_or_unknown_tool(users):
    with db.session() as conn, db.tx(conn):
        conn.execute("UPDATE connection SET params=json_set(params,'$.ui_url','javascript:alert(1)') WHERE tenant_id=? AND name='grafana'", (_tenant(users),))
    assert _link(users, "grafana.prometheus_query", {"expr": "up"}) is None
    assert _link(users, "k8s.kubectl_read", {"command": "get pods"}) is None and _link(users, "nope", {}) is None

