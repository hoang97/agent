"""Luồng tự xử lý cảnh báo của một tenant.

poll Alertmanager/Prometheus → khử trùng lặp theo fingerprint → gom thành sự cố (luật nhãn, quan hệ k8s, rồi mới tới LLM)
→ điều tra bằng agent chỉ đọc → báo cáo nguyên nhân + đề xuất hành động → hành động đi qua gateway như mọi lệnh khác
(độ tin dưới ngưỡng cài đặt của môi trường thì bị ép hỏi) → chờ vài phút rồi xác minh cảnh báo đã hết chưa.

Lệnh do tài khoản bot "kagent-alerts" của tenant gọi, nên audit, chính sách, luật chặn và duyệt vẫn áp dụng; bot chỉ có vai trò member.
"""
from __future__ import annotations

import logging

from .. import db
from .actions import act, dismiss, follow_up, has_pending, migrate, rebuild_events, retry, on_approval, record_chat_action, result_line, strip_dry_run
from .common import (BOT, first_sentence, who, SEVERITY, SOURCE_TEMPLATES, STAGE_LABEL, STAGES, WAITING_ON_HUMAN, IncidentError, MANIFEST_VERBS, describe, _cmd, _event, _is_write, _later,
                     _lock, _method_label, _running, _shown_stage, _slots, _state, _summarizing, _summary_failed, _waiting, _worse)
from .config import DEFAULTS, bot_user, check_settings, save_settings, settings
from .grouping import (RELATED_DAYS, related, suggestions, EXPORTER_JOBS, EXPORTER_LABELS, _attach, _brief, _feedback, _maybe_resolved, _owner, _rule_match, assign, auto_merge,
                       close, match, merge, merged_from, split, target_labels)
from .ingest import _env_of, _fingerprint, fetch, poll, source_status
from .investigate import (INVESTIGATE_TASK, _investigate_run, _recover_orphans, _write_tools, ensure_conversation, investigate, queued,
                          reinvestigate, start_investigation)
from .summary import ACTION_STATUS, MAX_SUMMARIES_PER_DAY, _summary_input, due_summaries, last_activity, start_summary, summarize, summary_view
from . import export, impact, notify, quality, rootcause, tasks
from .views import SHOWN_EVENTS, context_block, actions_by_episode, get, list_incidents, timeline, view

log = logging.getLogger("kagent.incidents")



# Gọi qua tên của package để test thay được start_investigation.
def tick(conn) -> None:
    """Gọi từ vòng nền: poll khi tới hạn, theo dõi duyệt/xác minh, khởi động điều tra tới hạn, viết lại tóm tắt khi sự cố đã yên."""
    _recover_orphans(conn)
    for t in conn.execute("SELECT id FROM tenant").fetchall():
        for incident_id, basis in due_summaries(conn, t["id"]):
            start_summary(t["id"], incident_id, basis)
        try:
            notify.dispatch(conn, t["id"])  # cả khi luồng cảnh báo tắt: lệnh đã xin duyệt vẫn cần người xem
        except Exception:
            log.exception("tenant %s: lỗi khi gửi thông báo", t["id"])
        cfg = settings(conn, t["id"])
        if not cfg["enabled"]:
            if has_pending(conn, t["id"]) or conn.execute("SELECT 1 FROM incident WHERE tenant_id=? AND stage='verifying' LIMIT 1", (t["id"],)).fetchone():
                follow_up(conn, t["id"])  # lệnh đã xin duyệt trước khi tắt luồng vẫn được theo dõi
            continue
        state = _state.get(t["id"]) or {}
        if db.now().timestamp() - state.get("last_poll", 0) >= cfg["poll_seconds"] - 1:
            poll(conn, t["id"])
        if cfg["auto_merge"]:
            auto_merge(conn, t["id"], cfg)
        follow_up(conn, t["id"])
        if cfg["investigate"]:
            for inc in conn.execute("SELECT id FROM incident WHERE tenant_id=? AND status='open' AND investigate_after IS NOT NULL AND investigate_after<=?",
                                    (t["id"], db.iso())).fetchall():
                start_investigation(t["id"], inc["id"])
