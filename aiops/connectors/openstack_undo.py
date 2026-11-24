"""Hoàn tác cho api_write của OpenStack: action có cặp ngược rõ ràng, và PUT/PATCH đổi vài trường (đọc giá trị cũ trước rồi ghi lại).

POST tạo tài nguyên mới, DELETE và các action khác: không hoàn tác được, lệnh phải có người duyệt.
"""
from __future__ import annotations

import re

from . import openstack
from .base import ConnectorError

INVERSE = {"os-stop": "os-start", "os-start": "os-stop", "pause": "unpause", "unpause": "pause", "suspend": "resume", "resume": "suspend",
           "lock": "unlock", "unlock": "lock", "shelve": "unshelve", "unshelve": "shelve", "resize": "revertResize", "rescue": "unrescue",
           "unrescue": "rescue"}
NOOP = {"reboot": "khởi động lại máy ảo, không có trạng thái cần hoàn tác"}
RESOURCE = re.compile(r"^/(servers|volumes|ports|networks|subnets|routers|security-groups|v2\.0/\w+)/([A-Za-z0-9-]{1,64})(/action)?$")


def _get(p: dict, service: str, path: str) -> dict:
    return openstack._call(p, {"service": service, "method": "GET", "path": path}, "read").get("json") or {}


def plan_undo(p: dict, tool: str, args: dict) -> dict:
    if tool != "api_write":
        return {"reversible": False, "why": "chỉ hoàn tác được api_write"}
    service, method, path, body = args.get("service"), str(args.get("method") or "GET").upper(), str(args.get("path") or ""), args.get("body")
    m = RESOURCE.match(path.split("?", 1)[0].rstrip("/"))
    if not m:
        return {"reversible": False, "why": "chỉ hoàn tác được thao tác trên một tài nguyên đặt id (/servers/<id>...)", "recipe": f"{method.lower()}"}
    kind, rid, action = m.group(1).split("/")[-1], m.group(2), m.group(3)
    t = {"key": f"{service}/{kind}/{rid}", "label": f"{kind.rstrip('s')} {rid}", "name": rid}
    base = path.split("?", 1)[0].rstrip("/").removesuffix("/action")
    if method == "POST" and action and isinstance(body, dict) and len(body) == 1:
        name = next(iter(body))
        if name in NOOP:
            return {"reversible": True, "noop": True, "recipe": f"action_{name}", "summary": NOOP[name], "targets": [t], "scoped": True, "scope": t["label"]}
        if name in INVERSE:
            inverse = INVERSE[name]
            return {"reversible": True, "recipe": f"action_{name}", "summary": f"gọi {inverse} cho {t['label']}", "targets": [t], "scoped": True,
                    "scope": t["label"], "undo": [{"op": "call", "service": service, "method": "POST", "path": path, "body": {inverse: None}}]}
        return {"reversible": False, "why": f"action {name} chưa có cách hoàn tác", "recipe": f"action_{name}"}
    if method in ("PUT", "PATCH") and isinstance(body, dict) and len(body) == 1 and isinstance(next(iter(body.values())), dict):
        wrapper, fields = next(iter(body.items()))
        current = _get(p, service, base).get(wrapper)
        if not isinstance(current, dict):
            raise ConnectorError(f"không đọc được {t['label']} trước khi sửa")
        old = {k: current.get(k) for k in fields}
        return {"reversible": True, "recipe": f"{method.lower()}_{kind}", "summary": f"ghi lại giá trị cũ của {', '.join(sorted(fields))} cho {t['label']}",
                "targets": [t], "scoped": True, "scope": t["label"], "snapshot": {wrapper: old},
                "undo": [{"op": "call", "service": service, "method": method, "path": base, "body": {wrapper: old}}]}
    return {"reversible": False, "why": f"{method} {path} không hoàn tác được tự động (tạo mới hoặc thao tác chưa hỗ trợ)", "recipe": method.lower()}


def run_op(p: dict, op: dict) -> dict:
    if op.get("op") != "call":
        raise ConnectorError(f"bước không hợp lệ: {op.get('op')}")
    return openstack._call(p, {k: op[k] for k in ("service", "method", "path", "body") if k in op}, "write")


def drift(p: dict, plan: dict) -> list[str]:
    snap = plan.get("snapshot") or {}
    after = plan.get("after") or {}
    if not snap or not after:
        return []
    op = plan["undo"][0]
    wrapper = next(iter(snap))
    now = _get(p, op["service"], op["path"]).get(wrapper) or {}
    changed = [k for k, v in after.items() if now.get(k) != v]
    return [f"{plan['targets'][0]['label']} đã bị sửa sau lệnh: {', '.join(changed)}"] if changed else []


def after_change(p: dict, plan: dict, result) -> dict:
    snap = plan.get("snapshot")
    if not snap:
        return plan
    op = plan["undo"][0]
    wrapper = next(iter(snap))
    now = _get(p, op["service"], op["path"]).get(wrapper) or {}
    return {**plan, "after": {k: now.get(k) for k in snap[wrapper]}}
