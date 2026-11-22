"""Hằng số, trạng thái trong tiến trình và hàm nhỏ dùng chung của các module sự cố."""
from __future__ import annotations

import json
import re
import threading
from datetime import timedelta

import yaml

from .. import db

SOURCE_TEMPLATES = ("alertmanager", "prometheus")
STAGES = ("new", "investigating", "reported", "awaiting_approval", "verifying", "handled", "needs_human", "error")
SEVERITY = {"critical": 4, "error": 3, "high": 3, "warning": 2, "medium": 2, "info": 1, "low": 1}
BOT = "kagent-alerts"


class IncidentError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_state: dict[str, dict] = {}  # tenant → {"last_poll": giây, "sources": {tên: {"at", "count", "error"}}}
_running: set[str] = set()
_waiting: set[str] = set()  # trong _running nhưng chưa có chỗ trong _slots
_lock = threading.Lock()
_slots = threading.Semaphore(2)  # tối đa 2 lượt điều tra cùng lúc trong cả tiến trình
_summarizing: set[str] = set()
_summary_failed: dict[str, str] = {}  # sự cố → mốc hoạt động đã tóm tắt lỗi: không thử lại tới khi có hoạt động mới


def _method_label(method: str) -> str:
    return {"rule": "luật nhãn", "topology": "quan hệ k8s", "ai": "AI", "manual": "người gộp", "root": "gốc", "score": "điểm bằng chứng"}.get(method, method)


def _worse(a: str | None, b: str | None) -> str | None:
    return max((a, b), key=lambda s: SEVERITY.get(s or "", 0)) if a or b else None


def _later(seconds: int) -> str:
    return (db.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _event(conn, tenant_id: str, incident_id: str, kind: str, text: str, data: dict | None = None, actor: str | None = None,
           action_id: str | None = None, at: str | None = None) -> None:
    conn.execute("INSERT INTO incident_event(tenant_id,incident_id,at,kind,text,data,actor,action_id) VALUES (?,?,?,?,?,?,?,?)",
                 (tenant_id, incident_id, at or db.iso(), kind, text[:1000], json.dumps(data, ensure_ascii=False) if data else None, actor, action_id))


STAGE_LABEL = {"new": "mới", "investigating": "đang điều tra", "reported": "đã báo cáo, cần người xem", "awaiting_approval": "chờ duyệt",
               "verifying": "đang xác minh", "handled": "đã xử lý", "needs_human": "cần người xử lý", "error": "lỗi",
               "cleared": "cảnh báo đã hết", "closed": "đã đóng", "waiting_quota": "chờ quota"}
WAITING_ON_HUMAN = ("new", "reported", "needs_human", "error", "waiting_quota")


def _shown_stage(d: dict) -> str:
    """Sự cố không còn mở thì không còn chờ ai. Tính lúc đọc vì stage còn bị ghi sau khi hết cảnh báo (điều tra đang chạy, lệnh bị từ chối)."""
    if d["status"] != "open" and d["stage"] in WAITING_ON_HUMAN:
        return "cleared" if d["status"] == "resolved" else "closed"
    return d["stage"]


def _cmd(step: dict) -> str:
    args = step.get("args") or {}
    if isinstance(args, str):
        args = json.loads(args) or {}
    return f"{step['tool'].split('.')[-1]} {args['command']}" if isinstance(args.get("command"), str) else f"{step['tool']} {json.dumps(args, ensure_ascii=False)}"


def _is_write(conn, tenant_id: str, tool: str) -> bool:
    r = conn.execute("SELECT action_class FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, tool)).fetchone()
    return bool(r and r["action_class"] != "read")


MANIFEST_VERBS = {"apply": "tạo hoặc cập nhật", "create": "tạo", "replace": "thay", "delete": "xóa", "patch": "sửa"}


def describe(args) -> str | None:
    """Câu ngắn cho lệnh kubectl kèm manifest khi không có _why: dòng lệnh chỉ còn "apply -f -", không nói đụng vào đối tượng nào."""
    if not isinstance(args, dict) or not isinstance(args.get("manifest"), str):
        return None
    try:
        docs = [d for d in yaml.safe_load_all(args["manifest"]) if isinstance(d, dict)]
    except yaml.YAMLError:
        return None
    command = args.get("command") if isinstance(args.get("command"), str) else ""
    words = command.split()
    verb = MANIFEST_VERBS.get(next((w for w in words if w in MANIFEST_VERBS), ""), "áp manifest")
    flag_ns = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in ("-n", "--namespace")), None)
    names = []
    for d in docs[:5]:
        meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
        ns = meta.get("namespace") or flag_ns
        names.append(f"{d.get('kind', '?')} {meta.get('name', '?')}" + (f" (namespace {ns})" if ns else ""))
    if not names:
        return None
    return f"{verb} " + ", ".join(names) + (f" và {len(docs) - 5} đối tượng khác" if len(docs) > 5 else "")


def _label(row: dict) -> str:
    """Lệnh cho dòng sự kiện; lệnh kèm manifest thêm tên đối tượng vì dòng lệnh chỉ còn "apply -f -"."""
    args = json.loads(row["args"]) if isinstance(row.get("args"), str) else row.get("args")
    what = describe(args)
    return _cmd(row) + (f" ({what})" if what else "")


def first_sentence(text: str | None, limit: int = 160) -> str:
    text = " ".join((text or "").split())
    m = re.search(r"[.;!?](\s|$)", text)
    out = text[:m.start()] if m else text
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def who(e: dict, name) -> str:
    """Ai làm, cho một dòng của dòng sự kiện; name(actor) → tên hiện. Tính lúc đọc để câu trong sự kiện chỉ còn "việc → kết quả"."""
    kind, actor = e["kind"], e.get("actor")
    data = e.get("data") or {}
    if kind in ("alert", "alert_resolved", "resolved", "reopened"):
        return "cảnh báo"
    if kind == "investigated":
        return f"kagent · điều tra lần {data['round']}" if data.get("round") else "kagent · điều tra"
    if kind in ("ran", "failed"):
        if data.get("auto"):
            return "tự động" if data.get("origin") == "investigation" else f"{name(actor)} chạy"
        return f"{name(actor)} duyệt"
    if kind in ("change_checked", "change_check_failed"):
        return "kagent · kiểm tra sau khi chạy"
    if kind in ("reverted", "revert_failed"):
        return "tự hoàn tác" if actor in (None, "system") else f"{name(actor)} hoàn tác"
    if kind == "task_added":
        return f"{name(actor)} thêm việc cần người"
    if kind == "task_done":
        return f"{name(actor)} đánh dấu đã xong"
    if kind == "task_reopened":
        return f"{name(actor)} mở lại việc"
    if kind == "closed":
        return f"{name(actor)} đóng" if actor else "đóng"
    return "kagent" if actor in (None, "kagent", "system") else name(actor)
