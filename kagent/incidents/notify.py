"""Báo ra ngoài qua webhook: sự cố mới, lệnh của sự cố chờ duyệt, sự cố cần người xử lý.

Quét trạng thái ở vòng nền thay vì móc vào từng chỗ đổi trạng thái, để không sót ca nào (kể cả sau khi khởi động lại). Mỗi tin có
khóa riêng trong incident_notice nên không gửi trùng; chỉ xét việc xảy ra sau mốc `since` (lúc bật), để bật lần đầu không xả cả lịch sử.
Thân JSON có "text" nên Slack, Mattermost và webhook vào của các ứng dụng chat nhận thẳng; bot trung gian đọc thêm các trường khác.
Cài đặt mã hóa cả khối vì URL webhook thường chứa token.
"""
from __future__ import annotations

import json
import logging
import threading
from urllib.parse import urlsplit

from .. import db, net
from .common import IncidentError

log = logging.getLogger("kagent.incidents")

EVENTS = {"new": "sự cố mới", "approval": "lệnh chờ duyệt", "needs_human": "cần người xử lý"}
DEFAULTS = {"enabled": False, "url": "", "events": list(EVENTS), "allow_private": False, "public_url": "", "since": None}
MAX_PER_TICK = 10  # một tenant không gửi quá chừng này mỗi vòng; phần còn lại vòng sau
_sending: set[str] = set()
_lock = threading.Lock()


def load(conn, tenant_id: str) -> dict:
    from ..connections import _decrypt
    r = conn.execute("SELECT notify FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    return {**DEFAULTS, **(_decrypt(r["notify"]) if r and r["notify"] else {})}


def view(cfg: dict) -> dict:
    """Không trả URL đầy đủ về trình duyệt: chỉ scheme và máy chủ."""
    u = urlsplit(cfg["url"]) if cfg["url"] else None
    return {**{k: cfg[k] for k in ("enabled", "events", "allow_private", "public_url", "since")},
            "url_set": bool(cfg["url"]), "url_hint": f"{u.scheme}://{u.hostname}/…" if u else ""}


def save(conn, tenant_id: str, data: dict, *, is_org_admin: bool, public_url: str) -> dict:
    from ..connections import _encrypt
    if not isinstance(data, dict) or set(data) - {"enabled", "url", "events", "allow_private", "public_url"}:
        raise IncidentError("chỉ gồm enabled, url, events, allow_private, public_url")
    cfg = load(conn, tenant_id)
    allow_private = bool(data.get("allow_private", cfg["allow_private"]))
    if allow_private and not cfg["allow_private"] and not is_org_admin:
        raise IncidentError("gửi vào mạng nội bộ cần org admin bật", 403)
    url = str(data["url"]).strip() if data.get("url") else cfg["url"]  # để trống là giữ URL đã lưu
    events = data.get("events", cfg["events"])
    if not isinstance(events, list) or set(events) - set(EVENTS):
        raise IncidentError(f"events là danh sách trong {list(EVENTS)}")
    base = str(data.get("public_url", cfg["public_url"]) or public_url).strip().rstrip("/")
    if base and urlsplit(base).scheme not in ("http", "https"):
        raise IncidentError("public_url phải là http hoặc https")
    enabled = bool(data.get("enabled", cfg["enabled"]))
    if enabled:
        if not url:
            raise IncidentError("cần URL webhook")
        try:
            net.check_url(url, allow_private)
        except net.NetError as exc:
            raise IncidentError(f"URL webhook: {exc}") from exc
    since = cfg["since"] if cfg["enabled"] and enabled else (db.iso() if enabled else None)
    turned_on = enabled and since != cfg["since"]
    cfg = {"enabled": enabled, "url": url, "events": list(dict.fromkeys(events)), "allow_private": allow_private, "public_url": base, "since": since}
    conn.execute("UPDATE tenant SET notify=? WHERE id=?", (_encrypt(cfg), tenant_id))
    if turned_on:  # mốc giờ chỉ tới giây: việc có sẵn lúc bật đánh dấu bỏ qua, không dựa vào so giờ
        for iid, key, _ in pending(conn, tenant_id, {**cfg, "events": list(EVENTS)}):
            conn.execute("INSERT OR IGNORE INTO incident_notice(tenant_id,incident_id,key,at,status) VALUES (?,?,?,?, 'skipped')", (tenant_id, iid, key, since))
    return view(cfg)


def _link(cfg: dict, inc) -> str:
    return f"{cfg['public_url']}/#/t/{inc['tenant_id']}/incidents/{inc['id']}" if cfg["public_url"] else ""


def _payload(event: str, inc, cfg: dict, extra: dict | None = None) -> dict:
    url = _link(cfg, inc)
    head = f"[kagent · {inc['env']}] {EVENTS[event].capitalize()}: {inc['title']}"
    lines = [head]
    from .impact import answers, grade
    sev = grade(answers(inc))[0] if answers(inc) else None
    if sev or inc["severity"]:
        lines.append(" · ".join(x for x in (f"Mức sự cố: {sev}" if sev else "", f"mức cảnh báo: {inc['severity']}" if inc["severity"] else "") if x))
    if extra and extra.get("tool"):
        lines.append(f"Lệnh: {extra['tool']} {extra.get('command', '')}".rstrip() + (f" (hết hạn {extra['expires_at']})" if extra.get("expires_at") else ""))
    if extra and extra.get("reason"):
        lines.append(extra["reason"])
    if url:
        lines.append(url)
    return {"text": "\n".join(lines), "event": event,
            "incident": {"id": inc["id"], "title": inc["title"], "env": inc["env"], "severity": inc["severity"], "stage": inc["stage"], "url": url},
            **({"action": extra} if extra else {})}


def pending(conn, tenant_id: str, cfg: dict) -> list[tuple[str, str, dict]]:
    """(incident_id, khóa, payload) chưa gửi, cũ trước."""
    if not cfg["enabled"] or not cfg["since"]:
        return []
    seen = {(r["incident_id"], r["key"]) for r in conn.execute("SELECT incident_id, key FROM incident_notice WHERE tenant_id=?", (tenant_id,))}
    out = []
    since = cfg["since"]
    incs = {r["id"]: r for r in conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' AND updated_at>=?", (tenant_id, since))}
    if "new" in cfg["events"]:
        for i in incs.values():
            if i["created_at"] >= since and (i["id"], "new") not in seen:
                out.append((i["created_at"], i["id"], "new", _payload("new", i, cfg)))
    if "approval" in cfg["events"]:
        for a in conn.execute("SELECT a.incident_id, a.approval_id, a.tool, a.args, r.expires_at, r.created_at FROM incident_action a "
                              "JOIN approval_request r ON r.id=a.approval_id WHERE a.tenant_id=? AND r.status='pending' AND r.created_at>=?",
                              (tenant_id, since)):
            key = f"approval:{a['approval_id']}"
            inc = incs.get(a["incident_id"]) or conn.execute("SELECT * FROM incident WHERE id=?", (a["incident_id"],)).fetchone()
            if inc is None or (a["incident_id"], key) in seen:
                continue
            args = json.loads(a["args"] or "{}")
            extra = {"tool": a["tool"], "command": str(args.get("command") or "")[:200], "expires_at": a["expires_at"]}
            out.append((a["created_at"], a["incident_id"], key, _payload("approval", inc, cfg, extra)))
    if "needs_human" in cfg["events"]:
        for i in incs.values():
            key = f"needs_human:{i['investigations'] or 0}:{i['repeats'] or 0}"  # lượt điều tra hay lần lặp mới thì báo lại
            if i["stage"] == "needs_human" and (i["id"], key) not in seen:
                last = conn.execute("SELECT text FROM incident_event WHERE incident_id=? ORDER BY id DESC LIMIT 1", (i["id"],)).fetchone()
                out.append((i["updated_at"], i["id"], key, _payload("needs_human", i, cfg, {"reason": last["text"][:300]} if last else None)))
    return [(iid, key, body) for _, iid, key, body in sorted(out, key=lambda x: x[0])]


def send(cfg: dict, body: dict) -> None:
    """Lỗi mạng hay mã HTTP lỗi thì ném NetError/IncidentError để người gọi ghi lại."""
    import httpx
    url = net.check_url(cfg["url"], cfg["allow_private"])
    with net.client(None, allow_private=cfg["allow_private"], timeout=10.0, proxy="auto") as c:
        try:
            r = c.post(str(url), json=body)
        except httpx.HTTPError as exc:
            raise IncidentError(f"không gửi được webhook ({exc.__class__.__name__})") from exc
    if r.status_code >= 300:
        raise IncidentError(f"webhook trả mã {r.status_code}")


def dispatch(conn, tenant_id: str, background: bool = True) -> int:
    """Ghi khóa trước rồi mới gửi ở luồng nền: hai vòng nền chồng nhau không gửi đôi; gửi lỗi thì ghi lỗi, không thử lại tự động."""
    cfg = load(conn, tenant_id)
    todo = pending(conn, tenant_id, cfg)[:MAX_PER_TICK]
    if not todo:
        return 0
    with _lock:
        if tenant_id in _sending:
            return 0
        _sending.add(tenant_id)
    with db.tx(conn):
        for iid, key, _ in todo:
            conn.execute("INSERT OR IGNORE INTO incident_notice(tenant_id,incident_id,key,at,status) VALUES (?,?,?,?, 'sending')", (tenant_id, iid, key, db.iso()))

    def work():
        try:
            _send_all()
        finally:
            with _lock:
                _sending.discard(tenant_id)

    def _send_all():
        for iid, key, body in todo:
            status, error = "sent", None
            try:
                send(cfg, body)
            except (IncidentError, net.NetError) as exc:
                status, error = "failed", str(exc)[:300]
                log.warning("tenant %s: gửi thông báo %s của sự cố %s lỗi: %s", tenant_id, key, iid, error)
            with db.session() as c2, db.tx(c2):
                c2.execute("UPDATE incident_notice SET status=?, error=?, at=? WHERE tenant_id=? AND incident_id=? AND key=?",
                           (status, error, db.iso(), tenant_id, iid, key))

    if background:
        threading.Thread(target=work, name=f"notify-{tenant_id[:8]}", daemon=True).start()
    else:
        work()
    return len(todo)


def test(conn, tenant_id: str) -> dict:
    """Gửi một tin thử đồng bộ để admin thấy ngay lỗi cấu hình."""
    cfg = load(conn, tenant_id)
    if not cfg["url"]:
        raise IncidentError("chưa có URL webhook")
    try:
        send(cfg, {"text": "[kagent] Tin thử: webhook thông báo sự cố đã nối được.", "event": "test"})
    except net.NetError as exc:
        raise IncidentError(f"không gửi được: {exc}") from exc
    return {"ok": True}


def recent(conn, tenant_id: str, limit: int = 10) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT n.incident_id, n.key, n.at, n.status, n.error, i.title FROM incident_notice n "
                                         "LEFT JOIN incident i ON i.id=n.incident_id WHERE n.tenant_id=? AND n.status!='skipped' ORDER BY n.at DESC, n.rowid DESC LIMIT ?", (tenant_id, limit))]
