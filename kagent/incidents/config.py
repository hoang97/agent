"""Cài đặt luồng cảnh báo của tenant và tài khoản bot chạy lệnh."""
from __future__ import annotations

import copy
import json
import re

from .. import db
from ..tools import ENVS
from .common import BOT, SOURCE_TEMPLATES, IncidentError, _state

DEFAULTS = {
    "enabled": False, "sources": [], "poll_seconds": 60, "window_minutes": 10,
    # Gom khi mọi nhãn trong danh sách mà hai cảnh báo cùng có đều bằng nhau. Thêm service/job sẽ tách nhỏ hơn
    # nhưng kết quả phụ thuộc thứ tự cảnh báo đến (cảnh báo không có service lại nối được cả hai nhóm).
    "group_labels": ["cluster", "namespace"],
    "ai_grouping": True, "ai_min_score": 0.8,
    "investigate": True, "debounce_seconds": 30,
    # Tự xử lý theo môi trường. Bật rồi thì hành động vẫn qua gateway: ma trận rủi ro, rồi auto mode xét theo auto_rules dưới đây.
    "auto": {"dev": {"enabled": True}, "staging": {"enabled": False}, "prod": {"enabled": False}},
    # Đứng ở chỗ lời người dùng khi auto mode xét hành động của sự cố: chỉ những việc này được tự làm.
    "auto_rules": [
        "Được khởi động lại (rollout restart) hoặc scale tăng số replica của workload đang bị cảnh báo, trong đúng namespace của cảnh báo.",
        "Được tạo silence ngắn (tối đa 2 giờ) cho đúng cảnh báo của sự cố.",
        "Không xóa tài nguyên, không sửa cấu hình (image, biến môi trường, resource, manifest), không đụng tới đối tượng không có trong cảnh báo.",
    ],
    "max_auto_actions_per_hour": 1, "verify_minutes": 5,
    # Cảnh báo cùng nhóm bắn lại trong reopen_hours sau khi sự cố hết thì mở lại sự cố đó thay vì mở sự cố mới (0 = tắt).
    "reopen_hours": 24,
    # Sự cố đang mở khớp nhau theo luật nhãn hoặc cùng node/workload thì tự gộp vào sự cố cũ nhất; khớp theo AI thì không.
    "auto_merge": True,
    # Như session recap của Claude Code: viết lại tóm tắt khi đã yên idle_minutes sau hoạt động mới.
    "summary": {"enabled": True, "idle_minutes": 5},
    # Gom nâng cao (docs/GROUPING-PLAN.md). Mặc định tắt hết: bật từng phần sau khi chạy lại trên dữ liệu cũ thấy tốt hơn.
    "grouping": {
        "batch": False,  # gom các cảnh báo mới của cùng một vòng poll thành cụm trước, rồi mới khớp với sự cố
        "scoring": False,  # chấm điểm bằng chứng thay cho luật nhãn group_labels; LLM chỉ quyết phần lưng chừng
        "partition_labels": ["cluster"],  # khi chấm điểm: khác nhau ở các nhãn này thì không bao giờ chung sự cố
        "join_threshold": 0.7, "suggest_threshold": 0.4, "burst_seconds": 120,
        "ai_max_calls": 5,  # số lần hỏi LLM tối đa mỗi vòng poll
        "topology_minutes": 0,  # đọc topology k8s mỗi N phút (0 = tắt)
        "flap_count": 0,  # bắn lại từ N lần trong 1 giờ là chập chờn: không điều tra lại mỗi lần (0 = tắt)
        "open_min_severity": "",  # dưới mức này thì không tự mở sự cố mới ("" = mức nào cũng mở)
    },
}
OPEN_SEVERITIES = ("", "info", "warning", "error", "critical")


def check_settings(data) -> dict:
    if not isinstance(data, dict):
        raise IncidentError("cài đặt phải là object")
    out = copy.deepcopy(DEFAULTS)
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise IncidentError(f"khóa không hợp lệ: {sorted(unknown)}")

    def num(key, low, high, kind=int):
        try:
            value = kind(data.get(key, out[key]))
        except (TypeError, ValueError) as exc:
            raise IncidentError(f"{key} phải là số") from exc
        if not low <= value <= high:
            raise IncidentError(f"{key} phải nằm trong {low}-{high}")
        return value

    out["enabled"] = bool(data.get("enabled", out["enabled"]))
    sources = data.get("sources", out["sources"])
    if not isinstance(sources, list) or not all(isinstance(s, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,30}", s) for s in sources):
        raise IncidentError("sources là danh sách tên kết nối")
    out["sources"] = list(dict.fromkeys(sources))[:10]
    out["poll_seconds"] = num("poll_seconds", 30, 3600)
    out["window_minutes"] = num("window_minutes", 1, 240)
    labels = data.get("group_labels", out["group_labels"])
    if not isinstance(labels, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", x) for x in labels):
        raise IncidentError("group_labels là danh sách tên nhãn")
    out["group_labels"] = list(dict.fromkeys(labels))[:10]
    out["ai_grouping"] = bool(data.get("ai_grouping", out["ai_grouping"]))
    out["ai_min_score"] = num("ai_min_score", 0.5, 1.0, float)
    out["investigate"] = bool(data.get("investigate", out["investigate"]))
    out["debounce_seconds"] = num("debounce_seconds", 0, 600)
    auto = data.get("auto", {})
    if not isinstance(auto, dict) or set(auto) - set(ENVS):
        raise IncidentError(f"auto theo môi trường {list(ENVS)}")
    for env, conf in auto.items():
        if not isinstance(conf, dict) or set(conf) - {"enabled", "min_confidence"}:  # min_confidence: cài đặt lưu từ trước auto mode, bỏ qua
            raise IncidentError(f"auto.{env} chỉ gồm enabled")
        out["auto"][env] = {"enabled": bool(conf.get("enabled", out["auto"][env]["enabled"]))}
    rules = data.get("auto_rules", out["auto_rules"])
    if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
        raise IncidentError("auto_rules là danh sách câu")
    rules = [r.strip() for r in rules if r.strip()]
    if len(rules) > 20 or any(len(r) > 600 for r in rules):
        raise IncidentError("auto_rules: tối đa 20 luật, mỗi luật 600 ký tự")
    out["auto_rules"] = rules
    out["max_auto_actions_per_hour"] = num("max_auto_actions_per_hour", 0, 10)
    out["verify_minutes"] = num("verify_minutes", 1, 60)
    out["reopen_hours"] = num("reopen_hours", 0, 168)
    out["auto_merge"] = bool(data.get("auto_merge", out["auto_merge"]))
    summary = data.get("summary", {})
    if not isinstance(summary, dict) or set(summary) - {"enabled", "idle_minutes"}:
        raise IncidentError("summary chỉ gồm enabled, idle_minutes")
    try:
        idle = int(summary.get("idle_minutes", out["summary"]["idle_minutes"]))
    except (TypeError, ValueError) as exc:
        raise IncidentError("summary.idle_minutes phải là số") from exc
    if not 1 <= idle <= 120:
        raise IncidentError("summary.idle_minutes phải nằm trong 1-120")
    out["summary"] = {"enabled": bool(summary.get("enabled", out["summary"]["enabled"])), "idle_minutes": idle}
    out["grouping"] = _check_grouping(data.get("grouping", {}))
    return out


def _check_grouping(data) -> dict:
    out = copy.deepcopy(DEFAULTS["grouping"])
    if not isinstance(data, dict) or set(data) - set(out):
        raise IncidentError(f"grouping chỉ gồm {', '.join(out)}")

    def num(key, low, high, kind=int):
        try:
            value = kind(data.get(key, out[key]))
        except (TypeError, ValueError) as exc:
            raise IncidentError(f"grouping.{key} phải là số") from exc
        if not low <= value <= high:
            raise IncidentError(f"grouping.{key} phải nằm trong {low}-{high}")
        return value

    out["batch"] = bool(data.get("batch", out["batch"]))
    out["scoring"] = bool(data.get("scoring", out["scoring"]))
    labels = data.get("partition_labels", out["partition_labels"])
    if not isinstance(labels, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", x) for x in labels):
        raise IncidentError("grouping.partition_labels là danh sách tên nhãn")
    out["partition_labels"] = list(dict.fromkeys(labels))[:10]
    out["join_threshold"] = num("join_threshold", 0.5, 1.0, float)
    out["suggest_threshold"] = num("suggest_threshold", 0.1, 1.0, float)
    if out["suggest_threshold"] > out["join_threshold"]:
        raise IncidentError("grouping.suggest_threshold không được lớn hơn join_threshold")
    out["burst_seconds"] = num("burst_seconds", 0, 1800)
    out["ai_max_calls"] = num("ai_max_calls", 0, 50)
    out["topology_minutes"] = num("topology_minutes", 0, 1440)
    if 0 < out["topology_minutes"] < 5:
        raise IncidentError("grouping.topology_minutes: 0 (tắt) hoặc từ 5 phút")
    out["flap_count"] = num("flap_count", 0, 20)
    if out["flap_count"] == 1:
        raise IncidentError("grouping.flap_count: 0 (tắt) hoặc từ 2")
    if data.get("open_min_severity", out["open_min_severity"]) not in OPEN_SEVERITIES:
        raise IncidentError(f"grouping.open_min_severity là một trong {', '.join(repr(x) for x in OPEN_SEVERITIES)}")
    out["open_min_severity"] = data.get("open_min_severity", out["open_min_severity"])
    return out


def settings(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT alert_settings FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    try:
        return check_settings(json.loads(r["alert_settings"]) if r and r["alert_settings"] else {})
    except (IncidentError, ValueError):
        return copy.deepcopy(DEFAULTS)


def save_settings(conn, tenant_id: str, data: dict) -> dict:
    cfg = check_settings(data)
    for name in cfg["sources"]:
        row = conn.execute("SELECT template FROM connection WHERE tenant_id=? AND name=? AND status='active'", (tenant_id, name)).fetchone()
        if row is None or row["template"] not in SOURCE_TEMPLATES:
            raise IncidentError(f"{name}: cần là kết nối {'/'.join(SOURCE_TEMPLATES)} đang hoạt động")
    conn.execute("UPDATE tenant SET alert_settings=? WHERE id=?", (json.dumps(cfg), tenant_id))
    _state.pop(tenant_id, None)
    return cfg


def bot_user(conn, tenant_id: str) -> str:
    """Tài khoản chạy lệnh của luồng cảnh báo: không đăng nhập được (hash rỗng), chỉ vai trò member trong tenant."""
    r = conn.execute("SELECT id FROM app_user WHERE username=?", (BOT,)).fetchone()
    uid = r["id"] if r else db.new_id()
    if r is None:
        conn.execute("INSERT INTO app_user(id,username,pw_hash,is_org_admin) VALUES (?,?,?,0)", (uid, BOT, "00$disabled"))
    if not conn.execute("SELECT 1 FROM membership WHERE tenant_id=? AND user_id=?", (tenant_id, uid)).fetchone():
        conn.execute("INSERT INTO membership(tenant_id,user_id,role) VALUES (?,?,'member')", (tenant_id, uid))
    return uid
