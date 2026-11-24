"""Bản nháp cho repo ghi incident và postmortem của tổ chức: issue (tiêu đề, Phần A, comment nhật ký) và các mục postmortem aiops có dữ liệu.

Chỉ soạn để người sao chép; không tự tạo issue. Điều aiops không biết (ảnh hưởng khách hàng, SLA, service:: của tổ chức, người phụ trách)
để TODO: lint của repo chặn merge khi còn TODO, nên người viết buộc phải điền thay vì nhận số liệu đoán.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import impact, rootcause, tasks
from .common import IncidentError, _label
from .present import headline

TZ = timezone(timedelta(hours=7))  # template postmortem ghi giờ UTC+7
FACTOR_ITEM = {"worse": "giảm thiểu", "detect": "phát hiện", "recover": "giảm thiểu"}
LOCATION_LABELS = ("cluster", "region", "namespace", "node")


def _t(iso: str | None) -> str:
    if not iso:
        return "TODO"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _minutes(a: str | None, b: str | None) -> str:
    if not (a and b):
        return "TODO"
    return f"{max(0, round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60))} phút"


def _same_cause(conn, inc) -> list[dict]:
    """Sự cố trước có cùng nguyên nhân (rootcause.check_recurrence ghi hai chiều)."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM incident WHERE rc_recurred_by=?", (inc["id"],))]
    if inc["rc_recurred_by"]:
        ids.append(inc["rc_recurred_by"])
    rows = [conn.execute("SELECT id, title, created_at FROM incident WHERE id=?", (i,)).fetchone() for i in ids]
    return [dict(r) for r in rows if r]


def _severity(im: dict) -> tuple[str, str]:
    """Mức người đã đánh giá; chưa đánh giá thì gợi ý ở dev/staging, còn lại để TODO vì aiops không biết ảnh hưởng khách hàng."""
    if im["source"] == "human" or im["sev"] == "S4":
        return im["sev"], im["reason"]
    return "TODO", f"chưa đánh giá ảnh hưởng (gợi ý {im['sev']}: {im['reason']}); đánh giá ở trang sự cố hoặc theo README mục 3"


def _customers(im: dict) -> str:
    a = im["answers"]
    if not a:
        return "Chưa xác định"
    parts = [impact.CUSTOMERS[a["customers"]]] + [t for k, t in (("data_loss", "mất dữ liệu"), ("control_plane", "control plane không gọi được"),
                                                                  ("redundancy_lost", "mất dự phòng")) if a.get(k)]
    return ", ".join(parts) + (f" ({a['note']})" if a.get("note") else "")


def draft(conn, tenant_id: str, incident_id: str) -> dict:
    from .views import timeline
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    alerts = [dict(a) | {"labels": json.loads(a["labels"] or "{}")} for a in
              conn.execute("SELECT * FROM alert_event WHERE incident_id=? ORDER BY starts_at", (incident_id,)).fetchall()]
    names = list(dict.fromkeys(a["alertname"] or "?" for a in alerts))
    places = list(dict.fromkeys(f"{k} {a['labels'][k]}" for a in alerts for k in LOCATION_LABELS if a["labels"].get(k)))[:4]
    where = f"{inc['env']}" + (f" ({', '.join(places)})" if places else "")
    first = min((a["starts_at"] or a["first_seen"] for a in alerts), default=inc["created_at"])
    c = rootcause.chain(inc)
    lv = rootcause.level(conn, inc)
    same = _same_cause(conn, inc)
    im = impact.assess(conn, inc)
    sev, sev_reason = _severity(im)
    events = timeline(conn, dict(inc), alerts)
    acted = [r for r in conn.execute("SELECT * FROM incident_action WHERE incident_id=? AND status IN ('ok','error') AND superseded_by IS NULL "
                                     "ORDER BY coalesce(ran_at, proposed_at)", (incident_id,)).fetchall()]
    task_rows = tasks.current(conn, incident_id)
    summary = headline(inc["summary"], 200) if inc["summary"] else inc["title"]

    # Issue: Phần A và comment nhật ký "<hành động> → <kết quả>" (bot của repo dựng nhật ký postmortem từ comment).
    title = f"[<SERVICE>] {inc['title']} tại {where}"
    pm_reasons = list(im["postmortem"]["reasons"]) if im["source"] == "human" else []
    if same and not any(r.startswith("S3 lặp lại") for r in pm_reasons):
        pm_reasons.append("có sự cố cùng nguyên nhân trong 30 ngày")
    need_pm = "; ".join(pm_reasons) or None
    body = "\n".join([
        "## Phần A: Thông tin incident", "",
        "| Mục | Nội dung |", "| --- | --- |",
        f"| Tóm tắt | {_cell(summary)} |",
        "| Service gây sự cố | TODO: trùng label service:: |",
        "| Service khác bị ảnh hưởng | |",
        f"| Vị trí | {_cell(where)} |",
        f"| Ảnh hưởng khách hàng | {_cell(_customers(im))} |",
        f"| Cách phát hiện | alert: {_cell(', '.join(names)[:200])} |",
        "| Người xử lý chính | @TODO |",
        "| Link liên quan | TODO |", "",
        "### Đánh giá postmortem", "",
        f"- [{'x' if need_pm else ' '}] Cần postmortem. Lý do: {need_pm or ''}",
        "- [ ] Không cần postmortem. Lý do:", "",
        '/label ~"issuetype::Incident" ~"incident::' + ("active" if inc["status"] == "open" else "mitigated") + '"',
    ])
    comments = []
    for e in events:
        if e["kind"] == "investigated" and e.get("title"):
            line = f"điều tra bằng {e.get('result') or 'lệnh đọc'} → {e['title']}"
        elif e.get("title") and e.get("result"):
            cmd = f" ({e['cmd']})" if e.get("cmd") and e["cmd"] != e["title"] else ""
            line = f"{e['title']}{cmd} → {e['result']}"
        else:
            line = e["text"]
        comments.append(f"{_t(e['at'])[5:]} {line}")

    # Postmortem: chỉ các mục aiops có dữ liệu; mục 1, 2, 7, 10, 12 cần người viết.
    whys = []
    for i, w in enumerate(c["whys"], 1):
        mark = "" if w.get("by_user") or w.get("steps") or "CB" in (w.get("refs") or []) else " (giả thuyết, cần xác nhận)"
        whys.append(f"{i}. {w['question']} {w['answer']}{mark}" + (" — điểm yếu sửa được" if w.get("weakness") else ""))
    if c["stop"] == "no_evidence":
        whys.append(f"{len(whys) + 1}. {c['next_question'] or 'Tại sao'} TODO")
    factors = [f"- {rootcause.FACTOR_LABEL[f['type']]}: {f['text']}" for f in c["factors"]]
    detect_gaps = [f["text"] for f in c["factors"] if f["type"] == "detect"]
    mitigations = [a for a in acted if a["status"] == "ok" and a["kind"] == "mitigate"]
    items = [(t["text"], "phòng ngừa") for t in task_rows if t["source"] == "prevent"] + [(f["text"], FACTOR_ITEM[f["type"]]) for f in c["factors"]]
    pm = [
        f"<!-- aiops soạn từ sự cố \"{inc['title']}\". Mức nguyên nhân: {lv['label']}"
        + (f" ({'; '.join(lv['notes'])})" if lv["notes"] else "") + ". Các mục 1, 2, 7, 10, 12 cần người viết. -->", "",
        "## 3. Các mốc thời gian", "",
        "| Mốc | Thời gian (YYYY-MM-DD HH:MM, UTC+7) |", "| --- | --- |",
        f"| Sự cố bắt đầu | {_t(first)} |",
        f"| Phát hiện | {_t(inc['created_at'])} |",
        f"| Dịch vụ phục hồi | {_t(inc['resolved_at'])} |",
        f"| **Thời gian phát hiện** (phát hiện − bắt đầu) | {_minutes(first, inc['created_at'])} |",
        f"| **Thời gian phục hồi** (phục hồi − phát hiện) | {_minutes(inc['created_at'], inc['resolved_at'])} |", "",
        "## 4. Nhật ký xử lý", "", *[f"- {x}" for x in comments], "",
        "## 5. Nguyên nhân và yếu tố góp phần", "",
        "**Tác nhân kích hoạt:** TODO" + (f" <!-- gợi ý từ điều tra: {c['whys'][0]['answer']} -->" if c["whys"] else ""), "",
        "**Chuỗi nguyên nhân chính (5 Whys):**" + ("" if lv["confirmed"] else " <!-- chưa có người xác nhận trong aiops -->"), "",
        *(whys or ["1. Tại sao TODO?"]), "",
        "**Yếu tố góp phần khác:**" + ("" if factors else " TODO"), *factors, "",
        "## 6. Phát hiện và monitoring", "",
        "| Mục | Nội dung |", "| --- | --- |",
        f"| Có alert không? | Có: {_cell(', '.join(names)[:200])} |",
        "| Alert có đúng mức và đúng người? | TODO |",
        f"| Lỗ hổng monitoring | {_cell('; '.join(detect_gaps)) if detect_gaps else 'TODO'} |", "",
        "## 8. Xử lý ngắn hạn", "",
        *([f"- {_label(dict(a))} → {a['result_line'] or ('đã chạy' if a['status'] == 'ok' else 'lỗi')}" for a in acted] or ["TODO"]), "",
        "**Biện pháp tạm thời còn giữ:** " + ("TODO: còn giữ " + ", ".join(_label(dict(a)) for a in mitigations) + "?" if mitigations else "TODO"), "",
        "## 9. Xử lý dài hạn (action items)", "",
        "| Hành động | Loại | Người phụ trách | Deadline | Issue |", "| --- | --- | --- | --- | --- |",
        *([f"| {_cell(text)} | {kind} | @TODO | YYYY-MM-DD | TODO |" for text, kind in items] or ["| TODO | TODO | @TODO | YYYY-MM-DD | TODO |"]), "",
        "## 11. Sự cố liên quan", "",
        *([f"- {r['title']} ({_t(r['created_at'])[:10]}): cùng nguyên nhân. TODO: link issue" for r in same] or ["TODO"]),
    ]
    return {"issue": {"title": title, "labels": ["service::<x>"], "severity": sev, "severity_reason": sev_reason, "body": body, "comments": comments},
            "postmortem": "\n".join(pm)}
