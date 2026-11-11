"""Connector Prometheus và Alertmanager trên hạ tầng giả."""
import pytest

import fakes
from kagent import net
from kagent.connectors import alertmanager, prometheus
from kagent.connectors.base import ConnectorError

PROM = {"_allow_private": False, "url": "https://prom.test", "token": fakes.PROM_TOKEN}
AM = {"_allow_private": False, "url": "https://am.test", "username": "am", "password": "pw"}


def test_prometheus_reads_and_compacts():
    assert prometheus.ping(PROM) == {"ok": True, "version": "2.53.0"}
    q = prometheus.query(PROM, {"expr": "up", "limit": 2})
    assert q["total_series"] == 3 and len(q["series"]) == 2 and q["series"][0] == {"metric": {"job": "api", "instance": "i0"}, "value": "0"}
    r = prometheus.query_range(PROM, {"expr": "rate(x[5m])", "minutes": 30, "max_points": 10})
    s = r["series"][0]
    assert (s["points"], s["min"], s["max"], s["last"], s["avg"]) == (100, 0, 99, 99, 49.5)
    assert len(s["values"]) == 10 and s["values"][-1] == [1000 + 15 * 99, "99"]
    assert "values" not in prometheus.query_range(PROM, {"expr": "x", "max_points": 0})["series"][0]
    with pytest.raises(ConnectorError, match="parse error"):
        prometheus.query(PROM, {"expr": "bad("})


def test_prometheus_alerts_rules_targets_labels():
    assert [a["alertname"] for a in prometheus.list_alerts(PROM, {"state": "firing"})["alerts"]] == ["HighCPU"]
    rules = prometheus.list_rules(PROM, {"only_firing": True})["rules"]
    assert [(r["name"], r["expr"], r["active"]) for r in rules] == [("HighCPU", "cpu > 0.9", 1)]
    t = prometheus.list_targets(PROM, {})
    assert t["by_job"] == {"api": {"up": 1, "down": 1}, "node": {"up": 1, "down": 0}} and t["down"][0]["error"] == "connection refused"
    assert prometheus.label_values(PROM, {"label": "__name__", "contains": "total"})["values"] == ["http_requests_total", "node_cpu_seconds_total"]
    assert prometheus.metric_metadata(PROM, {"metric": "up"})["metadata"][0]["type"] == "gauge"
    with pytest.raises(ConnectorError):
        prometheus.label_values(PROM, {"label": "a/../b"})
    with pytest.raises(ConnectorError, match="401"):
        prometheus.query({**PROM, "token": "sai"}, {"expr": "up"})


def test_alertmanager_lists_and_groups(infra):
    assert alertmanager.ping(AM)["version"] == "0.27.0"
    r = alertmanager.list_alerts(AM, {"matchers": "severity=warning,namespace=~pay.*"})
    assert r["total"] == 3 and r["by_alert"][0] == {"alertname": "HighCPU", "severity": "warning", "count": 2}
    assert infra.am_filters[-1] == ['severity="warning"', 'namespace=~"pay.*"']
    assert [s["id"] for s in alertmanager.list_silences(AM, {})["silences"]] == ["am-live"]
    assert alertmanager.list_silences(AM, {"state": "all"})["total"] == 2


def test_alertmanager_silence_lifecycle(infra):
    check = alertmanager.create_silence_check(AM, {"matchers": "alertname=HighCPU,namespace!=web"})
    assert check["would_silence_alerts"] == 1 and check["alertnames"] == ["HighCPU"]
    out = alertmanager.create_silence(AM, {"matchers": "alertname=HighCPU", "duration_minutes": 1})
    assert out["silence_id"] == "am1" and out["minutes"] == 5
    assert infra.silences[-1]["matchers"] == [{"name": "alertname", "value": "HighCPU", "isRegex": False, "isEqual": True}]
    assert alertmanager.expire_silence_check(AM, {"silence_id": "am-live"})["silence"]["matchers"] == ['namespace=~"pay.*"']
    assert alertmanager.expire_silence(AM, {"silence_id": "am-live"})["expired"] == "am-live" and infra.expired == ["am-live"]
    with pytest.raises(ConnectorError, match="đã hết hạn"):
        alertmanager.expire_silence_check(AM, {"silence_id": "am-old"})
    for bad in ("khongdung", "a=~(", "", "a="):
        with pytest.raises(ConnectorError):
            alertmanager.parse_matchers(bad)
    with pytest.raises(ConnectorError):
        alertmanager.expire_silence(AM, {"silence_id": "../x"})


def test_private_address_blocked():
    with pytest.raises(net.NetError):
        prometheus.query({**PROM, "url": "https://internal.test"}, {"expr": "up"})
