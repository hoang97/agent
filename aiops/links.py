"""Link mở lại bước truy vấn của agent trên công cụ gốc (Prometheus, Grafana Explore, Kibana Discover), để người tự xem bằng chứng.

Link dựng từ tham số đã chạy và địa chỉ giao diện của kết nối (ui_url, mặc định là url). Không có mốc giờ của từng bước nên khoảng
thời gian tính quanh `at` (lúc bắt đầu lượt). Công cụ không có giao diện tương ứng thì không có link.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit


def _when(at: str | None) -> datetime:
    try:
        t = datetime.fromisoformat((at or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _minutes(args: dict, key: str, default: int) -> int:
    try:
        return max(1, min(int(args.get(key) or default), 30 * 24 * 60))
    except (TypeError, ValueError):
        return default


def _base(params: dict) -> str | None:
    url = str(params.get("ui_url") or params.get("url") or "").strip().rstrip("/")
    return url if urlsplit(url).scheme in ("http", "https") else None


def _rison(s: str) -> str:
    return "'" + s.replace("!", "!!").replace("'", "!'") + "'"


def _prometheus(base, name, args, at):
    if name not in ("query", "query_range") or not args.get("expr"):
        return None
    minutes = _minutes(args, "minutes", 60) if name == "query_range" else 60
    end = _when(args.get("time")) if name == "query" and args.get("time") else at + timedelta(minutes=10)
    q = {"g0.expr": str(args["expr"]), "g0.tab": "0", "g0.range_input": f"{minutes + 10}m", "g0.end_input": end.strftime("%Y-%m-%d %H:%M:%S")}
    return f"{base}/graph?{urlencode(q)}"


def _grafana(base, name, args, params, at):
    uid = args.get("datasource_uid") or params.get("default_datasource_uid")
    if name != "prometheus_query" or not args.get("expr") or not uid:
        return None
    minutes = _minutes(args, "range_minutes", 60)
    left = {"datasource": uid, "queries": [{"refId": "A", "expr": str(args["expr"]), "datasource": {"uid": uid}}],
            "range": {"from": str(int((at - timedelta(minutes=minutes)).timestamp() * 1000)), "to": str(int((at + timedelta(minutes=10)).timestamp() * 1000))}}
    q = {"left": json.dumps(left, separators=(",", ":"))}
    if params.get("org_id"):
        q["orgId"] = str(params["org_id"])
    return f"{base}/explore?{urlencode(q)}"


def _kibana(base, name, args, params, at):
    # Elasticsearch không có giao diện: chỉ dựng link khi địa chỉ là Kibana (ui_url, hoặc url cổng 5601 hay có chữ kibana).
    if name not in ("search_logs", "count_logs"):
        return None
    if not params.get("ui_url"):
        u = urlsplit(base)
        if u.port != 5601 and "kibana" not in (u.hostname or ""):
            return None
    minutes = _minutes(args, "minutes", 60)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    g = f"(time:(from:{_rison(iso(at - timedelta(minutes=minutes)))},to:{_rison(iso(at + timedelta(minutes=10)))}))"
    a = f"(query:(language:lucene,query:{_rison(str(args.get('query') or '*'))}))"
    return f"{base}/app/discover#/?_g={quote(g, safe='():,!*')}&_a={quote(a, safe='():,!*')}"


def source_link(conn, tenant_id: str, tool: str, args, at: str | None) -> dict | None:
    """{"url", "label"} mở bước `tool(args)` trên công cụ gốc, hoặc None."""
    from . import connections
    if not isinstance(args, dict) or "." not in tool:
        return None
    meta = conn.execute("SELECT connection_id, template FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, tool)).fetchone()
    if meta is None or meta["template"] not in ("prometheus", "grafana", "elasticsearch") or not meta["connection_id"]:
        return None
    row = connections.get(conn, tenant_id, meta["connection_id"])
    if row is None:
        return None
    params, base, name, t = row["params"], _base(row["params"]), tool.split(".", 1)[1], _when(at).astimezone(timezone.utc)
    if base is None:
        return None
    if meta["template"] == "prometheus":
        url, label = _prometheus(base, name, args, t), "Mở trên Prometheus"
    elif meta["template"] == "grafana":
        url, label = _grafana(base, name, args, params, t), "Mở trong Grafana Explore"
    else:
        url, label = _kibana(base, name, args, params, t), "Mở trong Kibana Discover"
    return {"url": url, "label": label} if url else None
