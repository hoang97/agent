"""Auto mode: classifier đứng thay người duyệt cho các ô "judge" của ma trận, theo cách của Claude Code.

Classifier chỉ thấy ý của người dùng (tin nhắn, lệnh họ đã duyệt, luật tự xử lý do admin đặt) và các lệnh agent đã gọi.
Kết quả công cụ và lời của agent không được đưa vào: nội dung độc hại trong log, trang web chỉ có thể lái agent
gọi một lệnh, và lệnh đó vẫn phải khớp ý người dùng. Luật viết bằng chữ, mỗi tenant tự thêm; "$defaults" chèn luật mặc định.
"""
from __future__ import annotations

import copy
import json

SECTIONS = ("environment", "hard_deny", "soft_deny", "allow")
DEFAULTS_MARK = "$defaults"
MAX_RULES, MAX_RULE_CHARS = 40, 600
MAX_INTENT_CHARS, MAX_CALLS = 6000, 30

DEFAULTS = {
    "environment": [
        "Môi trường của lệnh (dev, staging, prod) là môi trường người dùng chọn cho hội thoại. prod là hệ thống thật đang phục vụ người dùng cuối.",
        "Đích tin cậy là các kết nối công cụ đã cấu hình trong kagent. Mọi địa chỉ khác (trang web, API, máy chủ lạ) nằm ngoài vùng tin cậy.",
    ],
    "hard_deny": [
        "Lộ dữ liệu ra ngoài: gửi secret, token, mật khẩu, dữ liệu cá nhân hoặc nội dung đọc được từ hệ thống nội bộ tới địa chỉ nằm ngoài vùng tin cậy.",
        "Tự nâng quyền hoặc gỡ kiểm soát: tạo, sửa RBAC, ClusterRole, RoleBinding, role hay user Keystone; tắt audit, log, cảnh báo; sửa chính sách của kagent.",
    ],
    "soft_deny": [
        "Xóa hàng loạt [phải nêu: đối tượng và phạm vi]: xóa theo selector, --all, ký tự đại diện, nhiều đối tượng một lúc; xóa namespace, PVC, volume, index, database, image.",
        "Đoán đối tượng [phải nêu: tên đối tượng]: hành động lên đối tượng người dùng không nhắc tới và agent cũng chưa đọc ra bằng lệnh trước trong hội thoại, hoặc chọn đối tượng vì tên gần giống.",
        "Lách lỗi [phải nêu: cách làm]: chạy lại lệnh vừa lỗi với cờ bỏ kiểm tra (--force, --grace-period=0, bỏ xác thực TLS), hoặc đi tìm token, tài khoản khác sau lỗi quyền.",
        "Ảnh hưởng rộng [phải nêu: thay đổi]: cordon, drain, taint node; sửa network policy, security group, ingress, quota, limit range; khởi động lại hoặc dừng nhiều workload cùng lúc.",
        "Chạy lệnh tùy ý [phải nêu: lệnh]: exec, run, script trong pod hoặc máy ảo; đọc nội dung secret.",
        "Thay đổi ở prod [phải nêu: thay đổi và đối tượng]: mọi lệnh ghi ở prod.",
        "Vượt phạm vi yêu cầu: yêu cầu chung chung (kiểm tra, dọn dẹp, xử lý giúp) không cho phép thay đổi mất dữ liệu; câu hỏi \"có sửa được không\" không phải là lệnh.",
    ],
    "allow": [
        "Lệnh chỉ đọc (get, describe, logs, top, list, GET, SELECT) trong môi trường của hội thoại.",
        "Chạy thử và so sánh: --dry-run, diff, auth can-i.",
    ],
}


class AutoModeError(ValueError):
    pass


def check(config) -> dict:
    """Cấu hình tenant lưu được: mỗi mục là danh sách câu, có thể chứa "$defaults"."""
    if not isinstance(config, dict) or set(config) - set(SECTIONS):
        raise AutoModeError(f"auto_mode chỉ gồm {list(SECTIONS)}")
    out = {}
    for key, rules in config.items():
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            raise AutoModeError(f"{key} phải là danh sách câu")
        rules = [r.strip() for r in rules if r.strip()]
        if len(rules) > MAX_RULES or any(len(r) > MAX_RULE_CHARS for r in rules):
            raise AutoModeError(f"{key}: tối đa {MAX_RULES} luật, mỗi luật {MAX_RULE_CHARS} ký tự")
        out[key] = list(dict.fromkeys(rules))
    return out


def effective(config: dict | None) -> dict:
    """Mục không khai giữ luật mặc định; mục có khai thay hẳn, trừ khi chứa "$defaults"."""
    config = config or {}
    out = {}
    for key in SECTIONS:
        if key not in config:
            out[key] = list(DEFAULTS[key])
            continue
        rules = []
        for r in config[key]:
            rules += DEFAULTS[key] if r == DEFAULTS_MARK else [r]
        out[key] = list(dict.fromkeys(rules))
    return out


def tenant_config(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT auto_mode FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    try:
        return check(json.loads(r["auto_mode"]) if r and r["auto_mode"] else {})
    except (AutoModeError, ValueError):
        return {}


def payload(rules: dict, intent: list[str], calls: list[dict], action: dict) -> dict:
    """Đầu vào của classifier. intent mới nhất được giữ khi phải cắt; calls chỉ gồm tên, tham số, trạng thái."""
    kept, used = [], 0
    for text in reversed(intent):
        text = text[:2000]
        if used + len(text) > MAX_INTENT_CHARS and kept:
            break
        kept.insert(0, text)
        used += len(text)
    return {"rules": rules, "user_intent": kept, "previous_calls": calls[-MAX_CALLS:], "action": action}


def classify(conn, tenant_id: str, intent: list[str], calls: list[dict], action: dict) -> dict:
    """{"allow": True|False|None, "rule", "reason"}. None: không đánh giá được (không có model, model lỗi) nên phải hỏi người."""
    from . import llm
    data = payload(effective(tenant_config(conn, tenant_id)), intent, calls, action)
    try:
        verdict = llm.get_provider().classify_action(data)
    except Exception as exc:  # lỗi model không được làm lệnh tự chạy
        return {"allow": None, "rule": "", "reason": f"auto mode không đánh giá được ({exc.__class__.__name__})"}
    return verdict


def defaults() -> dict:
    return copy.deepcopy(DEFAULTS)
