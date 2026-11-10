"""Elasticsearch (nơi Kibana đọc log) qua REST API, xác thực bằng API key hoặc tài khoản."""
from __future__ import annotations

import json

import httpx

from .base import ConnectorError, call, clip, http, integer, need

INDEX_PATTERN = r"[a-zA-Z0-9.*_,:-]{1,200}"
INDEX_NAME = r"[a-z0-9][a-z0-9._-]{0,200}"


def _client(p):
    headers = {"Accept": "application/json"}
    auth = None
    if p.get("api_key"):
        headers["Authorization"] = f"ApiKey {p['api_key']}"
    elif p.get("username"):
        auth = httpx.BasicAuth(p["username"], p.get("password") or "")
    return http(p, "url", headers=headers, auth=auth)


def ping(p, args=None):
    with _client(p) as c:
        info = call(c, "GET", "/")
    return {"ok": True, "version": info.get("version", {}).get("number")}


def search_logs(p, args):
    index = need({"index": args.get("index") or p.get("index_pattern") or "logs-*"}, "index", INDEX_PATTERN)
    minutes = integer(args, "minutes", 60, 1, 30 * 24 * 60)
    body = {
        "size": integer(args, "size", 20, 1, 100),
        "sort": [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}],
        "query": {"bool": {"must": [{"query_string": {"query": str(args.get("query") or "*")[:1000]}}],
                           "filter": [{"range": {"@timestamp": {"gte": f"now-{minutes}m"}}}]}},
    }
    with _client(p) as c:
        data = call(c, "POST", f"/{index}/_search", json_body=body)
    hits = data.get("hits", {})
    total = hits.get("total", {})
    return {"index": index, "total": total.get("value") if isinstance(total, dict) else total,
            "hits": [{"index": h.get("_index"), "timestamp": (h.get("_source") or {}).get("@timestamp"),
                      "source": clip(json.dumps(h.get("_source"), ensure_ascii=False), 2000)} for h in hits.get("hits", [])]}


def list_indices(p, args):
    with _client(p) as c:
        rows = call(c, "GET", "/_cat/indices", params={"format": "json", "h": "index,health,docs.count,store.size", "s": "index"})
    return {"indices": [r for r in rows if not str(r.get("index", "")).startswith(".")][:200]}


def delete_index_check(p, args):
    index = need(args, "index", INDEX_NAME)
    with _client(p) as c:
        rows = call(c, "GET", f"/_cat/indices/{index}", params={"format": "json", "h": "index,docs.count,store.size"})
    if not rows:
        raise ConnectorError(f"không có index {index}")
    return {"would_delete": rows[0], "objects": 1}


def delete_index(p, args):
    index = need(args, "index", INDEX_NAME)
    with _client(p) as c:
        result = call(c, "DELETE", f"/{index}")
    if not result.get("acknowledged"):
        raise ConnectorError("Elasticsearch không xác nhận việc xóa")
    return {"deleted_index": index}
