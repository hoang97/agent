"""Prometheus HTTP API (/api/v1), dùng được cho Mimir, Thanos, VictoriaMetrics.

Kết quả được rút gọn trước khi trả cho agent: chuỗi theo thời gian kèm min/max/avg/last và lấy mẫu thưa, để không phình ngữ cảnh.
"""
from __future__ import annotations

import time

from .base import ConnectorError, call, integer, monitoring_client, need

LABEL = r"[a-zA-Z_][a-zA-Z0-9_]*"


def _get(p, path, params=None):
    with monitoring_client(p) as c:
        data = call(c, "GET", f"/api/v1/{path}", params=params)
    if data.get("status") != "success":
        raise ConnectorError(f"Prometheus báo lỗi: {data.get('error', 'không rõ')}")
    return data.get("data")


def ping(p, args=None):
    try:
        info = _get(p, "status/buildinfo")
        return {"ok": True, "version": info.get("version")}
    except ConnectorError as exc:
        if "404" not in str(exc):  # Mimir/Thanos không có buildinfo: thử một truy vấn
            raise
    _get(p, "query", {"query": "1"})
    return {"ok": True, "version": None}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _stats(values: list) -> dict:
    nums = [x for x in (_num(v[1]) for v in values) if x is not None]
    if not nums:
        return {"points": len(values)}
    return {"points": len(values), "min": min(nums), "max": max(nums), "avg": round(sum(nums) / len(nums), 6), "last": nums[-1]}


def _thin(values: list, n: int) -> list:
    if len(values) <= n:
        return values
    step = len(values) / n
    return [values[int(i * step)] for i in range(n - 1)] + [values[-1]]


def query(p, args):
    expr = need(args, "expr")[:2000]
    limit = integer(args, "limit", 50, 1, 200)
    params = {"query": expr}
    if args.get("time"):
        params["time"] = str(args["time"])[:40]
    data = _get(p, "query", params)
    result = data.get("result", [])
    if data.get("resultType") in ("scalar", "string"):
        return {"expr": expr, "type": data["resultType"], "value": result}
    series = [{"metric": s.get("metric"), "value": (s.get("value") or [None, None])[1]} for s in result[:limit]]
    return {"expr": expr, "type": data.get("resultType"), "total_series": len(result), "series": series}


def query_range(p, args):
    expr = need(args, "expr")[:2000]
    minutes = integer(args, "minutes", 60, 1, 31 * 24 * 60)
    step = integer(args, "step_seconds", max(15, minutes * 60 // 240), 5, 86400)
    points = integer(args, "max_points", 30, 0, 300)
    limit = integer(args, "limit", 20, 1, 100)
    end = int(time.time())
    data = _get(p, "query_range", {"query": expr, "start": end - minutes * 60, "end": end, "step": step})
    result = data.get("result", [])
    series = [{"metric": s.get("metric"), **_stats(s.get("values") or []), **({"values": _thin(s.get("values") or [], points)} if points else {})}
              for s in result[:limit]]
    return {"expr": expr, "minutes": minutes, "step_seconds": step, "total_series": len(result), "series": series}


def list_alerts(p, args):
    state = str(args.get("state") or "").lower()
    alerts = _get(p, "alerts").get("alerts", [])
    alerts = [a for a in alerts if not state or a.get("state") == state]
    return {"total": len(alerts), "alerts": [{"alertname": (a.get("labels") or {}).get("alertname"), "state": a.get("state"), "labels": a.get("labels"),
                                               "since": a.get("activeAt"), "value": a.get("value"),
                                               "summary": (a.get("annotations") or {}).get("summary") or (a.get("annotations") or {}).get("description")}
                                              for a in alerts[:100]]}


def list_rules(p, args):
    name = str(args.get("name") or "").lower()
    only_firing = bool(args.get("only_firing"))
    out = []
    for g in _get(p, "rules", {"type": "alert"}).get("groups", []):
        for r in g.get("rules", []):
            if name and name not in str(r.get("name", "")).lower():
                continue
            if only_firing and r.get("state") != "firing":
                continue
            out.append({"group": g.get("name"), "name": r.get("name"), "state": r.get("state"), "expr": r.get("query"), "for_seconds": r.get("duration"),
                        "labels": r.get("labels"), "summary": (r.get("annotations") or {}).get("summary"), "active": len(r.get("alerts") or []),
                        "health": r.get("health")})
    return {"total": len(out), "rules": out[:100]}


def list_targets(p, args):
    job = str(args.get("job") or "")
    targets = [t for t in _get(p, "targets", {"state": "active"}).get("activeTargets", []) if not job or (t.get("labels") or {}).get("job") == job]
    down = [t for t in targets if t.get("health") != "up"]
    by_job: dict[str, dict] = {}
    for t in targets:
        j = by_job.setdefault((t.get("labels") or {}).get("job") or t.get("scrapePool") or "?", {"up": 0, "down": 0})
        j["up" if t.get("health") == "up" else "down"] += 1
    return {"total": len(targets), "by_job": by_job,
            "down": [{"job": (t.get("labels") or {}).get("job"), "instance": (t.get("labels") or {}).get("instance"), "health": t.get("health"),
                      "error": t.get("lastError"), "last_scrape": t.get("lastScrape")} for t in down[:50]]}


def label_values(p, args):
    label = need(args, "label", LABEL)
    contains = str(args.get("contains") or "").lower()
    params = {}
    if args.get("match"):
        params["match[]"] = str(args["match"])[:500]
    values = [v for v in _get(p, f"label/{label}/values", params) or [] if not contains or contains in str(v).lower()]
    limit = integer(args, "limit", 200, 1, 1000)
    return {"label": label, "total": len(values), "values": values[:limit]}


def metric_metadata(p, args):
    metric = need(args, "metric", r"[a-zA-Z_:][a-zA-Z0-9_:]*")
    data = _get(p, "metadata", {"metric": metric}) or {}
    return {"metric": metric, "metadata": data.get(metric, [])[:5]}
