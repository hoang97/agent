"""Xoá index thành xoá mềm: đóng index (dữ liệu còn nguyên, không tốn RAM), mở lại là hoàn tác; người có quyền xoá thật ở thùng rác."""
from __future__ import annotations

from .base import ConnectorError, need
from .elasticsearch import INDEX_NAME, _client, _es


def _state(p: dict, index: str) -> str | None:
    with _client(p) as c:
        rows = _es(c, "GET", f"/_cat/indices/{index}", params={"format": "json", "h": "index,status,docs.count,store.size", "expand_wildcards": "all"})
    return rows[0].get("status", "open") if rows else None


def plan_undo(p: dict, tool: str, args: dict) -> dict:
    if tool != "delete_index":
        return {"reversible": False, "why": "công cụ không đổi dữ liệu"}
    index = need(args, "index", INDEX_NAME)
    state = _state(p, index)
    if state is None:
        raise ConnectorError(f"không có index {index}")
    t = {"key": f"index/{index}", "label": f"index {index}", "name": index}
    if state == "close":
        return {"reversible": False, "why": f"index {index} đang đóng; xoá index đóng là xoá thật", "recipe": "soft_delete_index", "targets": [t]}
    return {"reversible": True, "soft": True, "recipe": "soft_delete_index", "targets": [t], "scoped": True, "scope": t["label"],
            "summary": f"xoá mềm: đóng index {index} thay vì xoá, mở lại để khôi phục",
            "instead": [{"op": "close", "index": index}], "undo": [{"op": "open", "index": index}], "purge": [{"op": "delete", "index": index}]}


def run_op(p: dict, op: dict) -> dict:
    index = need(op, "index", INDEX_NAME)
    method, path = {"close": ("POST", f"/{index}/_close"), "open": ("POST", f"/{index}/_open"), "delete": ("DELETE", f"/{index}")}[op["op"]]
    with _client(p) as c:
        result = _es(c, method, path)
    if not result.get("acknowledged"):
        raise ConnectorError(f"Elasticsearch không xác nhận {op['op']} {index}")
    return {{"close": "closed_index", "open": "opened_index", "delete": "deleted_index"}[op["op"]]: index}


def drift(p: dict, plan: dict) -> list[str]:
    index = plan["targets"][0]["name"]
    state = _state(p, index)
    if state is None:
        return [f"index {index} không còn"]
    return [f"index {index} đã được mở lại"] if state != "close" else []


def same_as_before(p: dict, plan: dict) -> bool:
    return _state(p, plan["targets"][0]["name"]) == "open"
