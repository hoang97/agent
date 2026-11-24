"""Hoàn tác silence của Alertmanager và Grafana: tạo thì gỡ đúng silence vừa tạo; gỡ thì tạo lại cùng matchers tới hạn cũ."""
from __future__ import annotations

from datetime import datetime, timezone

from .alertmanager import SILENCE_ID, parse_matchers
from .base import ConnectorError, call, monitoring_client, need

BASE = {"alertmanager": "/api/v2", "grafana": "/api/alertmanager/grafana/api/v2"}


def _kind(p: dict) -> str:
    return "grafana" if p.get("_template") == "grafana" else "alertmanager"


def _client(p: dict):
    if _kind(p) == "grafana":
        from .grafana import _client as grafana_client
        return grafana_client(p)
    return monitoring_client(p)


def plan_undo(p: dict, tool: str, args: dict) -> dict:
    if tool == "create_silence":
        matchers = parse_matchers(need(args, "matchers"))
        label = ",".join(f"{m['name']}={m['value']}" for m in matchers)
        return {"reversible": True, "recipe": "create_silence", "summary": "gỡ đúng silence vừa tạo", "scoped": True, "scope": f"silence {label}",
                "targets": [{"key": f"silence-new/{label}", "label": f"silence {label}"}], "undo": None}
    if tool == "expire_silence":
        sid = need(args, "silence_id", SILENCE_ID)
        with _client(p) as c:
            s = call(c, "GET", f"{BASE[_kind(p)]}/silence/{sid}")
        if (s.get("status") or {}).get("state") == "expired":
            return {"reversible": True, "noop": True, "recipe": "expire_silence", "summary": "silence đã hết hạn sẵn", "targets": []}
        snap = {"matchers": s.get("matchers") or [], "endsAt": s.get("endsAt"), "comment": s.get("comment") or ""}
        return {"reversible": True, "recipe": "expire_silence", "summary": f"tạo lại silence cùng matchers tới {str(snap['endsAt'])[:16]}",
                "scoped": True, "scope": f"silence {sid}", "targets": [{"key": f"silence/{sid}", "label": f"silence {sid}"}], "snapshot": snap,
                "undo": [{"op": "create", **snap}]}
    return {"reversible": False, "why": "công cụ không đổi gì"}


def after_change(p: dict, plan: dict, result) -> dict:
    if plan.get("recipe", "").endswith("create_silence"):
        sid = (result or {}).get("silence_id")
        if not sid:
            raise ConnectorError("không biết id silence vừa tạo")
        return {**plan, "undo": [{"op": "expire", "silence_id": sid}], "targets": [{"key": f"silence/{sid}", "label": f"silence {sid}"}]}
    return plan


def run_op(p: dict, op: dict) -> dict:
    base = BASE[_kind(p)]
    with _client(p) as c:
        if op["op"] == "expire":
            sid = need(op, "silence_id", SILENCE_ID)
            call(c, "DELETE", f"{base}/silence/{sid}")
            return {"expired": sid}
        if op["op"] == "create":
            ends = op.get("endsAt")
            if ends and ends <= datetime.now(timezone.utc).isoformat():
                return {"skipped": "silence cũ đã quá hạn, không cần tạo lại"}
            body = {"matchers": op["matchers"], "startsAt": datetime.now(timezone.utc).isoformat(), "endsAt": ends, "createdBy": "aiops",
                    "comment": (op.get("comment") or "") + " (aiops hoàn tác)"}
            result = call(c, "POST", f"{base}/silences", json_body=body)
            return {"silence_id": result.get("silenceID") or result.get("id")}
    raise ConnectorError(f"bước không hợp lệ: {op['op']}")


def drift(p: dict, plan: dict) -> list[str]:
    for op in plan.get("undo") or []:
        if op["op"] == "expire":
            with _client(p) as c:
                s = call(c, "GET", f"{BASE[_kind(p)]}/silence/{op['silence_id']}")
            if (s.get("status") or {}).get("state") == "expired":
                return [f"silence {op['silence_id']} đã được gỡ hoặc hết hạn"]
    return []
