"""L1: mọi thay đổi KU đều là phép nguyên tử và ghi ku_event để rollback được."""
from __future__ import annotations

import json
import re
import sqlite3

from . import db
from .retrieval import embed

KU_JSON = ("entities", "source_eps", "embedding")

_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[PRIVATE_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[API_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[API_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[API_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer [TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|mật khẩu|secret|token|api[_-]?key)\b(\s*[:=]\s*|\s+là\s+)\S+"), r"\1=[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\b\d{9,19}\b"), "[NUMBER]"),
]


def scrub(text: str | None) -> str:
    """Lọc bí mật và dữ liệu cá nhân trước khi ghi L1 hoặc gửi cho Reflector."""
    if not text:
        return text or ""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _snapshot(ku: dict | None) -> str | None:
    if ku is None:
        return None
    return json.dumps({k: v for k, v in ku.items() if k != "embedding"}, ensure_ascii=False)


def event(conn, tenant_id, ku_id, op, before, after, actor, reason=None) -> None:
    conn.execute(
        "INSERT INTO ku_event(tenant_id,ku_id,op,before,after,actor,reason,at) VALUES (?,?,?,?,?,?,?,?)",
        (tenant_id, ku_id, op, _snapshot(before), _snapshot(after), actor, reason, db.iso()),
    )


def get(conn, tenant_id: str, ku_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM knowledge_unit WHERE id=? AND tenant_id=?", (ku_id, tenant_id)).fetchone()
    return db.row(r, *KU_JSON)


def _index(conn, ku: dict) -> None:
    conn.execute("DELETE FROM ku_fts WHERE ku_id=?", (ku["id"],))
    conn.execute(
        "INSERT INTO ku_fts(ku_id,tenant_id,title,content,applies_when,entities) VALUES (?,?,?,?,?,?)",
        (ku["id"], ku["tenant_id"], ku["title"], ku["content"], ku.get("applies_when") or "", " ".join(ku["entities"])),
    )


def add(conn: sqlite3.Connection, tenant_id: str, *, kind: str, title: str, content: str, source_eps: list[str],
        source_trust: str, scope: str = "tenant", domain: str = "general", applies_when: str | None = None,
        entities: list[str] | None = None, actor: str = "curator", status: str = "candidate",
        reason: str | None = None) -> dict:
    title, content, applies_when = scrub(title), scrub(content), scrub(applies_when)
    entities = sorted(set(entities or []))
    ku_id = db.new_id()
    vec = embed(f"{title} {content} {applies_when or ''}")
    now = db.iso()
    with db.tx(conn):
        conn.execute(
            "INSERT INTO knowledge_unit(id,tenant_id,kind,scope,domain,title,content,applies_when,entities,embedding,"
            "status,valid_from,recorded_at,source_eps,source_trust) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ku_id, tenant_id, kind, scope, domain, title, content, applies_when, json.dumps(entities, ensure_ascii=False),
             json.dumps(vec), status, now, now, json.dumps(sorted(set(source_eps))), source_trust),
        )
        ku = get(conn, tenant_id, ku_id)
        _index(conn, ku)
        event(conn, tenant_id, ku_id, "ADD", None, ku, actor, reason)
    return ku


def update(conn, tenant_id: str, ku_id: str, changes: dict, op: str, actor: str, reason: str | None = None) -> dict:
    allowed = {"title", "content", "applies_when", "entities", "confidence", "helpful_cnt", "harmful_cnt", "status",
               "valid_to", "superseded_by", "source_eps", "version", "last_used_at", "scope", "kind"}
    if set(changes) - allowed:
        raise ValueError(f"cột không được phép: {set(changes) - allowed}")
    with db.tx(conn):
        before = get(conn, tenant_id, ku_id)
        if before is None:
            raise KeyError(ku_id)
        values = dict(changes)
        for f in ("title", "content", "applies_when"):
            if f in values:
                values[f] = scrub(values[f])
        if {"title", "content", "applies_when"} & set(values):
            merged = {**before, **values}
            values["embedding"] = json.dumps(embed(f"{merged['title']} {merged['content']} {merged.get('applies_when') or ''}"))
            values.setdefault("version", before["version"] + 1)
        for f in ("entities", "source_eps"):
            if f in values:
                values[f] = json.dumps(values[f], ensure_ascii=False)
        cols = ", ".join(f"{c}=?" for c in values)
        conn.execute(f"UPDATE knowledge_unit SET {cols} WHERE id=? AND tenant_id=?", (*values.values(), ku_id, tenant_id))
        after = get(conn, tenant_id, ku_id)
        _index(conn, after)
        event(conn, tenant_id, ku_id, op, before, after, actor, reason)
    return after


def add_edge(conn, tenant_id: str, src: str, dst: str, rel: str) -> None:
    conn.execute("INSERT OR IGNORE INTO ku_edge(tenant_id,src,dst,rel) VALUES (?,?,?,?)", (tenant_id, src, dst, rel))


def invalidate(conn, tenant_id: str, ku_id: str, superseded_by: str | None, actor: str, reason: str) -> dict:
    """Vô hiệu = đặt valid_to và superseded_by, không xóa."""
    return update(conn, tenant_id, ku_id, {"status": "deprecated", "valid_to": db.iso(), "superseded_by": superseded_by},
                  "INVALIDATE", actor, reason)


def promote(conn, tenant_id: str, ku_id: str, actor: str, reason: str) -> dict:
    """Thăng hạng và vô hiệu các KU active mà KU này mâu thuẫn."""
    with db.tx(conn):
        ku = update(conn, tenant_id, ku_id, {"status": "active"}, "PROMOTE", actor, reason)
        olds = conn.execute(
            "SELECT e.dst FROM ku_edge e JOIN knowledge_unit k ON k.id=e.dst "
            "WHERE e.src=? AND e.tenant_id=? AND e.rel='contradicts' AND k.status='active'", (ku_id, tenant_id)).fetchall()
        for old in olds:
            invalidate(conn, tenant_id, old["dst"], ku_id, actor, f"bị thay thế bởi {ku_id}")
    return ku


def rollback(conn, tenant_id: str, event_id: int, actor: str) -> dict:
    """Trả KU về trạng thái trước một ku_event."""
    ev = conn.execute("SELECT * FROM ku_event WHERE id=? AND tenant_id=?", (event_id, tenant_id)).fetchone()
    if ev is None:
        raise KeyError(event_id)
    if ev["before"] is None:
        return update(conn, tenant_id, ev["ku_id"], {"status": "rejected"}, "ROLLBACK", actor, f"hoàn tác ADD #{event_id}")
    before = json.loads(ev["before"])
    fields = ("title", "content", "applies_when", "entities", "confidence", "helpful_cnt", "harmful_cnt", "status",
              "valid_to", "superseded_by", "source_eps", "kind", "scope", "version")
    return update(conn, tenant_id, ev["ku_id"], {f: before[f] for f in fields}, "ROLLBACK", actor, f"hoàn tác sự kiện #{event_id}")


def recompute_confidence(ku: dict) -> float:
    """Làm trơn Laplace trên bộ đếm, kéo theo độ tin cậy của nguồn."""
    prior = {"user": 0.8, "verified_tool": 0.65, "internal_doc": 0.6, "external_untrusted": 0.2}[ku["source_trust"]]
    return round((ku["helpful_cnt"] + prior * 2) / (ku["helpful_cnt"] + ku["harmful_cnt"] + 2), 3)


KINDS = ("rule", "fact", "procedure", "lesson", "insight")
# Thứ tự tin cậy nguồn, thấp nhất trước: bản gộp mang mức thấp nhất trong nhóm, để nội dung untrusted không được "rửa" qua việc gộp.
TRUST_ORDER = ("external_untrusted", "internal_doc", "verified_tool", "user")


def merge(conn, tenant_id: str, ids: list[str], merged: dict, actor: str, activate: bool = False) -> dict:
    """Gộp các KU ứng viên trùng ý thành một KU mới; bản gốc chuyển rejected kèm superseded_by, vẫn truy vết được."""
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise ValueError("cần ít nhất 2 KU để gộp")
    kus = [get(conn, tenant_id, i) for i in ids]
    if any(k is None for k in kus):
        raise KeyError("không tìm thấy KU")
    if any(k["status"] != "candidate" for k in kus):
        raise ValueError("chỉ gộp được KU đang là ứng viên")
    scopes = {k["scope"] for k in kus}
    if len(scopes) != 1:
        raise ValueError("chỉ gộp được KU cùng phạm vi")
    kind = merged.get("kind") or kus[0]["kind"]
    if kind not in KINDS:
        raise ValueError(f"loại KU phải thuộc {list(KINDS)}")
    title, content = str(merged.get("title") or "").strip(), str(merged.get("content") or "").strip()
    if not title or not content:
        raise ValueError("bản gộp cần tiêu đề và nội dung")
    domains = [k["domain"] for k in kus]
    with db.tx(conn):
        new = add(conn, tenant_id, kind=kind, title=title[:200], content=content[:2000], applies_when=(merged.get("applies_when") or None),
                  entities=sorted({e for k in kus for e in k["entities"]}), source_eps=[e for k in kus for e in k["source_eps"]],
                  source_trust=min((k["source_trust"] for k in kus), key=TRUST_ORDER.index), scope=scopes.pop(),
                  domain=max(set(domains), key=domains.count), actor=actor, reason=f"gộp {len(kus)} KU trùng ý")
        for k in kus:
            update(conn, tenant_id, k["id"], {"status": "rejected", "superseded_by": new["id"]}, "MERGE", actor, f"gộp vào {new['id']}")
            add_edge(conn, tenant_id, new["id"], k["id"], "refines")
        if activate:
            new = promote(conn, tenant_id, new["id"], actor, "người duyệt chấp nhận bản gộp")
    return new
