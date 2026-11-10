"""Grafana qua HTTP API, xác thực bằng token của service account."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .base import ConnectorError, call, http, integer, need

UID = r"[A-Za-z0-9_-]{1,64}"


def _client(p):
    headers = {"Authorization": f"Bearer {p['token']}", "Accept": "application/json"}
    if p.get("org_id"):
        headers["X-Grafana-Org-Id"] = str(p["org_id"])
    return http(p, "url", headers=headers)


def ping(p, args=None):
    with _client(p) as c:
        call(c, "GET", "/api/search", params={"limit": 1})
        health = call(c, "GET", "/api/health")
    return {"ok": True, "version": health.get("version")}


def search_dashboards(p, args):
    with _client(p) as c:
        items = call(c, "GET", "/api/search", params={"query": str(args.get("query", ""))[:100], "type": "dash-db", "limit": 30})
    return {"dashboards": [{"title": i.get("title"), "uid": i.get("uid"), "folder": i.get("folderTitle"), "url": i.get("url")} for i in items]}


def list_datasources(p, args):
    with _client(p) as c:
        items = call(c, "GET", "/api/datasources")
    return {"datasources": [{"name": i.get("name"), "uid": i.get("uid"), "type": i.get("type"), "default": i.get("isDefault")} for i in items]}


def prometheus_query(p, args):
    expr = need(args, "expr")[:2000]
    uid = need({"uid": args.get("datasource_uid") or p.get("default_datasource_uid")}, "uid", UID)
    minutes = integer(args, "range_minutes", 0, 0, 7 * 24 * 60)
    base = f"/api/datasources/proxy/uid/{uid}/api/v1"
    with _client(p) as c:
        if minutes:
            end = int(time.time())
            query = {"query": expr, "start": end - minutes * 60, "end": end, "step": max(15, minutes * 60 // 120)}
            data = call(c, "GET", f"{base}/query_range", params=query)
        else:
            data = call(c, "GET", f"{base}/query", params={"query": expr})
    if data.get("status") != "success":
        raise ConnectorError(f"Prometheus báo lỗi: {data.get('error', 'không rõ')}")
    series = data.get("data", {}).get("result", [])[:20]
    for s in series:
        if "values" in s:
            s["values"] = s["values"][-120:]
    return {"expr": expr, "type": data.get("data", {}).get("resultType"), "series": series}


def list_alerts(p, args):
    with _client(p) as c:
        items = call(c, "GET", "/api/alertmanager/grafana/api/v2/alerts", params={"active": "true", "silenced": "false"})
    return {"alerts": [{"labels": a.get("labels"), "state": a.get("status", {}).get("state"), "since": a.get("startsAt"),
                        "summary": (a.get("annotations") or {}).get("summary")} for a in items[:50]]}


def _matchers(args) -> list[dict]:
    out = []
    for part in need(args, "matchers").split(","):
        if "=" not in part:
            raise ConnectorError("matchers phải có dạng nhan=gia_tri, cách nhau dấu phẩy")
        name, value = (x.strip() for x in part.split("=", 1))
        if not name or not value:
            raise ConnectorError("matchers phải có dạng nhan=gia_tri")
        out.append({"name": name, "value": value, "isRegex": False, "isEqual": True})
    return out


def _matching(c, matchers: list[dict]) -> int:
    alerts = call(c, "GET", "/api/alertmanager/grafana/api/v2/alerts", params={"active": "true"})
    return sum(1 for a in alerts if all((a.get("labels") or {}).get(m["name"]) == m["value"] for m in matchers))


def create_silence_check(p, args):
    matchers = _matchers(args)
    with _client(p) as c:
        count = _matching(c, matchers)
    return {"would_silence_alerts": count, "objects": 1}


def create_silence(p, args):
    matchers = _matchers(args)
    minutes = integer(args, "duration_minutes", 60, 5, 7 * 24 * 60)
    now = datetime.now(timezone.utc)
    body = {"matchers": matchers, "startsAt": now.isoformat(), "endsAt": (now + timedelta(minutes=minutes)).isoformat(),
            "createdBy": "kagent", "comment": str(args.get("comment") or "tạo bởi Knowledge Agent")[:500]}
    with _client(p) as c:
        result = call(c, "POST", "/api/alertmanager/grafana/api/v2/silences", json_body=body)
    return {"silence_id": result.get("silenceID") or result.get("id"), "minutes": minutes}
