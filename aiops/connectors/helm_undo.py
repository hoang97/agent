"""Kế hoạch hoàn tác cho helm_write/helm_admin (xem undo.py), dựa trên lịch sử revision helm tự lưu.

- upgrade, rollback: ghi revision đang chạy; hoàn tác = helm rollback về đúng revision đó.
- install (hoặc upgrade --install khi chưa có release): hoàn tác = uninstall đúng release vừa cài. Namespace do --create-namespace tạo không bị xoá.
- Không hoàn tác được: uninstall (history bị xoá), test (chạy pod), install --generate-name (không biết trước tên).
Trước khi hoàn tác so revision mới nhất với revision ngay sau lệnh: khác nghĩa là đã có người upgrade/rollback tiếp, không tự đè.
"""
from __future__ import annotations

import json
import shlex

from . import helm
from .base import ConnectorError

PROFILE = {"helm_write": "write", "helm_admin": "admin"}
UNSUPPORTED = {"uninstall": "gỡ release xoá luôn lịch sử revision, không cài lại được bằng một lệnh",
               "test": "helm test chạy pod kiểm tra, không có gì để hoàn tác"}


def _call(p: dict, argv: list[str]) -> str:
    argv, _ = helm.parse(shlex.join(argv), "admin", p.get("_allow_private", False))
    code, out, err, timed_out = helm._exec(p, argv, None, 120, interruptible=False)
    if timed_out:
        raise ConnectorError("helm hết giờ")
    if code != 0:
        raise ConnectorError(helm.kubectl._error_text(err or out)[:600] or f"helm lỗi (mã {code})")
    return out


def latest(p: dict, release: str, ns: str) -> dict | None:
    """Revision mới nhất của release, None nếu chưa có release."""
    try:
        out = _call(p, ["history", release, "-n", ns, "--max", "1", "-o", "json"])
    except ConnectorError as exc:
        if "not found" in str(exc).lower():
            return None
        raise
    rows = json.loads(out or "[]")
    return rows[-1] if rows else None


def _target(release: str, ns: str) -> dict:
    return {"key": f"helm/{ns}/{release}", "label": f"release {release} ({ns})", "name": release, "namespace": ns}


def plan_undo(p: dict, tool: str, args: dict) -> dict:
    profile = PROFILE.get(tool)
    if profile is None:
        return {"reversible": False, "why": "công cụ chỉ đọc"}
    argv, info = helm.parse(args.get("command"), profile, p.get("_allow_private", False))
    sub = info["sub"]
    if info["read_only"]:
        return {"reversible": True, "noop": True, "recipe": "helm_read", "summary": "lệnh không đổi gì", "targets": []}
    if sub in UNSUPPORTED:
        return {"reversible": False, "why": UNSUPPORTED[sub], "recipe": f"helm_{sub}"}
    if "generate-name" in info["flags"] or not info["positionals"]:
        return {"reversible": False, "why": "không biết trước tên release", "recipe": f"helm_{sub}"}
    release, ns = info["positionals"][0], helm.namespace(argv, p)
    t = _target(release, ns)
    cur = latest(p, release, ns)
    base = {"reversible": True, "targets": [t], "scoped": True, "scope": t["label"], "namespaces": [ns]}
    if cur is None:
        if sub == "rollback" or (sub == "upgrade" and "install" not in info["flags"]):
            raise ConnectorError(f"release {release} không có trong namespace {ns}")
        return {**base, "recipe": "helm_install", "summary": f"gỡ đúng release {release} vừa cài", "snapshot": {"release": release, "namespace": ns},
                "undo": [{"op": "helm", "argv": ["uninstall", release, "-n", ns]}]}
    if sub == "install":
        raise ConnectorError(f"release {release} đã có trong namespace {ns}; dùng upgrade")
    out = {**base, "recipe": f"helm_{sub}", "summary": f"helm rollback {release} về revision {cur['revision']} ({cur.get('chart')})",
           "snapshot": {"release": release, "namespace": ns, "revision": cur["revision"], "chart": cur.get("chart"), "app_version": cur.get("app_version")},
           "undo": [{"op": "helm", "argv": ["rollback", release, str(cur["revision"]), "-n", ns]}]}
    if cur.get("status") != "deployed":
        out["preflight"] = [f"release {release} đang ở trạng thái {cur.get('status')}, không phải deployed"]
    return out


def run_op(p: dict, op: dict) -> dict:
    if op.get("op") != "helm":
        raise ConnectorError(f"bước không hợp lệ: {op.get('op')}")
    out = _call(p, op["argv"])
    return {"command": "helm " + shlex.join(op["argv"]), "stdout": out.strip()[:500]}


def after_change(p: dict, plan: dict, result) -> dict:
    snap = plan["snapshot"]
    cur = latest(p, snap["release"], snap["namespace"])
    return {**plan, "after": {"revision": cur["revision"] if cur else None}}


def drift(p: dict, plan: dict) -> list[str]:
    snap, after = plan["snapshot"], (plan.get("after") or {}).get("revision")
    cur = latest(p, snap["release"], snap["namespace"])
    if cur is None:
        return [f"release {snap['release']} đã bị gỡ sau lệnh"]
    if after is not None and cur["revision"] != after:
        return [f"release {snap['release']} đã sang revision {cur['revision']} sau lệnh (lệnh tạo revision {after})"]
    return []


def check_change(p: dict, plan: dict) -> dict:
    snap = plan["snapshot"]
    cur = latest(p, snap["release"], snap["namespace"])
    if cur is None:
        return {"ready": False, "detail": f"release {snap['release']} không còn"}
    return {"ready": cur.get("status") == "deployed", "detail": f"release {snap['release']} revision {cur['revision']}: {cur.get('status')}"}


def same_as_before(p: dict, plan: dict) -> bool:
    snap = plan["snapshot"]
    cur = latest(p, snap["release"], snap["namespace"])
    if "revision" not in snap:
        return cur is None
    return cur is not None and cur.get("chart") == snap.get("chart") and cur.get("app_version") == snap.get("app_version")
