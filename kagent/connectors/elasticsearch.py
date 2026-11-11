"""Elasticsearch (nơi Kibana đọc log) qua REST API, xác thực bằng API key hoặc tài khoản.

URL trỏ vào Kibana cũng dùng được: lệnh đi qua Console proxy của Kibana tới Elasticsearch phía sau.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx

from .base import ConnectorError, call, clip, http, integer, need, parse

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


def _is_kibana(status: int, headers) -> bool:
    # Kibana chuyển mọi đường dẫn lạ (kể cả API của Elasticsearch) sang trang đăng nhập.
    return "kbn-name" in headers or (300 <= status < 400 and "/login" in headers.get("location", ""))


def _es(c, method: str, path: str, *, params=None, body=None, ok=(200, 201, 202)):
    status, headers, text = call(c, method, path, params=params, json_body=body, with_status=True)
    if not _is_kibana(status, headers):
        return parse(status, headers, text, ok=ok)
    target = path + (f"?{urlencode(params)}" if params else "")
    return call(c, "POST", "/api/console/proxy", params={"path": target, "method": method}, json_body=body,
                headers={"kbn-xsrf": "true"}, ok=ok)


def ping(p, args=None):
    with _client(p) as c:
        info = _es(c, "GET", "/")
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
        data = _es(c, "POST", f"/{index}/_search", body=body)
    hits = data.get("hits", {})
    total = hits.get("total", {})
    return {"index": index, "total": total.get("value") if isinstance(total, dict) else total,
            "hits": [{"index": h.get("_index"), "timestamp": (h.get("_source") or {}).get("@timestamp"),
                      "source": _clip_values(h.get("_source"))} for h in hits.get("hits", [])]}


def _clip_values(value, limit: int = 2000, depth: int = 0):
    """Giữ _source dạng object để lọc, nhóm được theo trường lồng nhau; chỉ cắt từng chuỗi quá dài.

    Trước đây cả _source bị dồn thành một chuỗi JSON rồi cắt, nên bản ghi dài không parse lại được.
    """
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, dict) and depth < 6:
        return {k: _clip_values(v, limit, depth + 1) for k, v in value.items()}
    if isinstance(value, list) and depth < 6:
        return [_clip_values(v, limit, depth + 1) for v in value[:100]]
    return value if isinstance(value, (int, float, bool)) or value is None else clip(json.dumps(value, ensure_ascii=False), limit)


def list_indices(p, args):
    with _client(p) as c:
        rows = _es(c, "GET", "/_cat/indices", params={"format": "json", "h": "index,health,docs.count,store.size", "s": "index"})
    return {"indices": [r for r in rows if not str(r.get("index", "")).startswith(".")][:200]}


def delete_index_check(p, args):
    index = need(args, "index", INDEX_NAME)
    with _client(p) as c:
        rows = _es(c, "GET", f"/_cat/indices/{index}", params={"format": "json", "h": "index,docs.count,store.size"})
    if not rows:
        raise ConnectorError(f"không có index {index}")
    return {"would_delete": rows[0], "objects": 1}


def delete_index(p, args):
    index = need(args, "index", INDEX_NAME)
    with _client(p) as c:
        result = _es(c, "DELETE", f"/{index}")
    if not result.get("acknowledged"):
        raise ConnectorError("Elasticsearch không xác nhận việc xóa")
    return {"deleted_index": index}
