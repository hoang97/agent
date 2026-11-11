"""Kết quả công cụ lớn được lưu ở server; context của agent chỉ nhận result_id và bản xem trước."""
from __future__ import annotations

import json
from datetime import timedelta

from . import datafilter, db

INLINE_LIMIT = 6000  # ký tự; lớn hơn thì lưu và chỉ đưa bản xem trước vào context
KEEP_DAYS = 7


def save(conn, tenant_id: str, user_id: str | None, episode_id: str | None, tool: str, data, untrusted: bool) -> str:
    text = json.dumps(data, ensure_ascii=False)
    result_id = "r_" + db.new_id().replace("-", "")[:12]
    conn.execute("INSERT INTO tool_result(id,tenant_id,user_id,episode_id,tool,untrusted,chars,data,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (result_id, tenant_id, user_id, episode_id, tool, int(untrusted), len(text), text, db.iso()))
    return result_id


def load(conn, tenant_id: str, user_id: str | None, result_id: str) -> dict:
    """Chỉ người tạo đọc được: kết quả có thể chứa dữ liệu mà người khác trong tenant không được xem."""
    r = conn.execute("SELECT * FROM tool_result WHERE id=? AND tenant_id=? AND user_id IS ?", (result_id, tenant_id, user_id)).fetchone()
    if r is None:
        raise datafilter.FilterError(f"không có kết quả {result_id} (đã hết hạn {KEEP_DAYS} ngày hoặc không thuộc về bạn)")
    return {**dict(r), "data": json.loads(r["data"])}


def compact(conn, tenant_id: str, user_id: str | None, episode_id: str | None, tool: str, result, untrusted: bool) -> dict | None:
    """Nếu kết quả quá lớn: lưu bản đầy đủ, trả bản thay thế gồm result_id, cấu trúc và vài mẫu."""
    if result is None:
        return None
    size = len(json.dumps(result, ensure_ascii=False))
    if size <= INLINE_LIMIT:
        return None
    result_id = save(conn, tenant_id, user_id, episode_id, tool, result, untrusted)
    return {"result_id": result_id, "full_chars": size, "preview": datafilter.preview(result),
            "note": "Kết quả lớn đã được lưu ở server, chỉ có bản xem trước ở đây. Dùng data.query với result_id này để lọc, đếm, nhóm "
                    "(where/fields/group_by/sort/limit), hoặc gọi lại công cụ với _filter để chỉ lấy phần cần thiết."}


def purge(conn) -> int:
    cut = db.iso(db.now() - timedelta(days=KEEP_DAYS))
    return conn.execute("DELETE FROM tool_result WHERE created_at<?", (cut,)).rowcount
