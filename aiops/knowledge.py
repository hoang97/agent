"""L1: mọi thay đổi KU đều là phép nguyên tử và ghi ku_event để rollback được."""
from __future__ import annotations

import json
import math
import re
import sqlite3

from . import db
from .entities import normalize as normalize_entities
from .retrieval import cosine, embed

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


_SECRET_KEY = re.compile(r"(?i)^(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|authorization)$")


def scrub_data(value):
    """scrub cho dữ liệu đã parse: chỉ lọc chuỗi nên số và cấu trúc JSON giữ nguyên.

    Lọc trên chuỗi JSON thì mẫu số dài ăn cả timestamp/số thực thành [NUMBER] trần, JSON hỏng.
    """
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_data(v) for v in value]
    if isinstance(value, dict):
        return {k: "[REDACTED]" if isinstance(k, str) and _SECRET_KEY.match(k) and isinstance(v, (str, int, float)) and v != "" else scrub_data(v)
                for k, v in value.items()}
    return value


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
    entities = normalize_entities(conn, tenant_id, entities)
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
        if "entities" in values:
            values["entities"] = normalize_entities(conn, tenant_id, values["entities"])
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


RELATED_MIN = 6.0  # thử trên dữ liệu thật: ngưỡng 5 đã kéo vào cặp chỉ chung một chữ chung chung như "token", "log"
RELATED_PER_KU = 3


def _shares_scope(a: str, b: str) -> bool:
    shared = ("tenant", "org")
    return a == b or (a in shared and b in shared) or (a.startswith(("user:", "project:")) and b in shared) or (b.startswith(("user:", "project:")) and a in shared)


def topic_peers(conn, tenant_id: str, entities: list[str], vec: list[float], scope: str, *, exclude: set[str] = frozenset(),
                kind: str | None = None, minimum: float = RELATED_MIN) -> list[tuple[float, str]]:
    """KU cùng chủ đề, điểm cao trước: điểm = tổng IDF của thực thể chung + 2×cosine.

    IDF để thực thể có mặt khắp nơi (tên cụm, môi trường) gần như không tính. Chỉ xét KU nhìn thấy nhau trong phạm vi,
    để ký ức cá nhân không kéo KU của người khác vào.
    """
    mine = set(entities)
    if not mine:
        return []
    rs = conn.execute("SELECT id, kind, scope, entities, embedding FROM knowledge_unit WHERE tenant_id=? AND status IN ('active','candidate')", (tenant_id,)).fetchall()
    df: dict[str, int] = {}
    pool = []
    for r in rs:
        ents = set(json.loads(r["entities"]))
        for e in ents:
            df[e] = df.get(e, 0) + 1
        if r["id"] not in exclude and _shares_scope(scope, r["scope"]) and (kind is None or r["kind"] == kind) and mine & ents:
            pool.append((r["id"], ents, r["embedding"]))
    n = len(rs) + 1  # +1: KU đang xét có thể chưa nằm trong kho (Curator gọi trước khi thêm)
    scored = []
    for other, ents, emb in pool:
        score = sum(math.log(n / df[e]) for e in mine & ents) + 2 * max(0.0, cosine(vec, json.loads(emb)))
        if score >= minimum:
            scored.append((score, other))
    return sorted(scored, reverse=True)


def link_related(conn, tenant_id: str, ku_id: str) -> int:
    """Nối KU với tối đa 3 KU cùng chủ đề (topic_peers). Cạnh lưu src<dst vì truy xuất đi cả hai chiều."""
    ku = get(conn, tenant_id, ku_id)
    if ku is None or not ku["entities"] or ku["status"] not in ("active", "candidate"):
        return 0
    linked = {x for e in conn.execute("SELECT src, dst FROM ku_edge WHERE tenant_id=? AND (src=? OR dst=?)", (tenant_id, ku_id, ku_id)) for x in (e["src"], e["dst"])}
    room = max(0, RELATED_PER_KU - len(linked - {ku_id}))
    best = topic_peers(conn, tenant_id, ku["entities"], ku["embedding"], ku["scope"], exclude=linked | {ku_id})[:room]
    for _, other in best:
        add_edge(conn, tenant_id, min(ku_id, other), max(ku_id, other), "related")
    return len(best)


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


def source_count(source_eps: list[str]) -> int:
    """Số nguồn độc lập: các đoạn "doc:<id>#<n>" của cùng một tài liệu chỉ tính là một nguồn."""
    return len({e.split("#", 1)[0] for e in source_eps})


def recompute_confidence(ku: dict) -> float:
    """Làm trơn Laplace trên bộ đếm, kéo theo độ tin cậy của nguồn."""
    prior = {"user": 0.8, "verified_tool": 0.65, "internal_doc": 0.6, "external_untrusted": 0.2}[ku["source_trust"]]
    return round((ku["helpful_cnt"] + prior * 2) / (ku["helpful_cnt"] + ku["harmful_cnt"] + 2), 3)


KINDS = ("rule", "fact", "procedure", "lesson", "insight")
# Thứ tự tin cậy nguồn, thấp nhất trước: bản gộp mang mức thấp nhất trong nhóm, để nội dung untrusted không được "rửa" qua việc gộp.
TRUST_ORDER = ("external_untrusted", "internal_doc", "verified_tool", "user")


def merge(conn, tenant_id: str, ids: list[str], merged: dict, actor: str, activate: bool = False, allow_active: bool = False) -> dict:
    """Gộp các KU trùng ý thành một KU mới; bản gốc vẫn truy vết được qua superseded_by.

    allow_active (đề xuất gộp đã được người duyệt): gộp cả KU đang dùng; bản gốc đang dùng chuyển deprecated, bản gộp
    có hiệu lực và mang tổng bộ đếm hữu ích/có hại của các bản gốc để không mất lịch sử sử dụng.
    """
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise ValueError("cần ít nhất 2 KU để gộp")
    kus = [get(conn, tenant_id, i) for i in ids]
    if any(k is None for k in kus):
        raise KeyError("không tìm thấy KU")
    allowed = ("candidate", "active") if allow_active else ("candidate",)
    if any(k["status"] not in allowed for k in kus):
        raise ValueError("chỉ gộp được KU đang là ứng viên" if not allow_active else "có KU đã hết hiệu lực hoặc bị loại")
    activate = activate or any(k["status"] == "active" for k in kus)
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
        helpful, harmful = sum(k["helpful_cnt"] for k in kus), sum(k["harmful_cnt"] for k in kus)
        if helpful or harmful:
            new = update(conn, tenant_id, new["id"], {"helpful_cnt": helpful, "harmful_cnt": harmful,
                                                      "confidence": recompute_confidence({**new, "helpful_cnt": helpful, "harmful_cnt": harmful})},
                         "UPDATE", actor, "cộng bộ đếm của các bản gốc")
        for k in kus:
            gone = {"status": "deprecated", "valid_to": db.iso()} if k["status"] == "active" else {"status": "rejected"}
            update(conn, tenant_id, k["id"], {**gone, "superseded_by": new["id"]}, "MERGE", actor, f"gộp vào {new['id']}")
            add_edge(conn, tenant_id, new["id"], k["id"], "refines")
        if activate:
            new = promote(conn, tenant_id, new["id"], actor, "người duyệt chấp nhận bản gộp")
        link_related(conn, tenant_id, new["id"])
    return new
