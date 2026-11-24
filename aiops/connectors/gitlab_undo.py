"""Hoàn tác comment GitLab: xoá đúng comment vừa tạo. Email thông báo đã gửi thì không thu hồi được."""
from __future__ import annotations

from .base import ConnectorError, call, need, parse
from .gitlab import ID, _client, note_target


def _label(noun: str, iid: str) -> str:
    return f"MR !{iid}" if noun == "merge_requests" else f"issue #{iid}"


def plan_undo(p: dict, tool: str, args: dict) -> dict:
    if tool != "create_note":
        return {"reversible": False, "why": "công cụ không có cách hoàn tác"}
    project, noun, iid = note_target(p, args)
    label = _label(noun, iid)
    return {"reversible": True, "recipe": "create_note", "summary": f"xoá đúng comment vừa tạo trên {label}", "scoped": True,
            "scope": f"comment mới trên {label}", "targets": [{"key": f"gitlab-note-new/{project}/{noun}/{iid}", "label": f"comment trên {label}"}],
            "snapshot": {"project": project, "noun": noun, "iid": iid}, "undo": None}


def _note_path(op: dict) -> str:
    return f"/projects/{op['project']}/{op['noun']}/{need(op, 'iid', ID)}/notes/{need(op, 'note_id', ID)}"


def after_change(p: dict, plan: dict, result) -> dict:
    nid = (result or {}).get("note_id")
    if not nid:
        raise ConnectorError("không biết id comment vừa tạo")
    where = {k: plan["snapshot"][k] for k in ("project", "noun", "iid")}  # after_change chạy lại sau diễn tập, nên không đọc từ targets
    project, noun, iid = where["project"], where["noun"], where["iid"]
    op = {"op": "delete_note", **where, "note_id": str(nid)}
    with _client(p) as c:
        note = call(c, "GET", _note_path(op))
    label = f"comment {nid} trên {_label(noun, iid)}"
    return {**plan, "undo": [op], "snapshot": {**where, "updated_at": note.get("updated_at")},
            "targets": [{"key": f"gitlab-note/{project}/{noun}/{iid}/{nid}", "label": label}]}


def run_op(p: dict, op: dict) -> dict:
    if op.get("op") != "delete_note":
        raise ConnectorError(f"bước không hợp lệ: {op.get('op')}")
    with _client(p) as c:
        call(c, "DELETE", _note_path(op), ok=(200, 202, 204))
    return {"deleted_note": op["note_id"]}


def _current(p: dict, op: dict) -> dict | None:
    with _client(p) as c:
        status, headers, text = call(c, "GET", _note_path(op), with_status=True)
    if status == 404:
        return None
    return parse(status, headers, text)


def drift(p: dict, plan: dict) -> list[str]:
    out = []
    for op in plan.get("undo") or []:
        note = _current(p, op)
        if note is None:
            out.append(f"comment {op['note_id']} đã bị xoá")
        elif note.get("updated_at") != (plan.get("snapshot") or {}).get("updated_at"):
            out.append(f"comment {op['note_id']} đã được sửa sau khi tạo")
    return out


def same_as_before(p: dict, plan: dict) -> bool:
    return all(_current(p, op) is None for op in plan.get("undo") or [])
