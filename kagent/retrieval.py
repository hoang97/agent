"""L3: truy xuất lai = lọc phạm vi và hiệu lực → vector + BM25 → mở rộng đồ thị → xếp hạng theo độ hữu ích."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta

from . import db

DIM = 256
_TOKEN = re.compile(r"[^\W_]+(?:[.:_-][^\W_]+)*", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _slot(token: str) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
    return int.from_bytes(digest[:4], "big") % DIM, 1.0 if digest[4] & 1 else -1.0


def embed(text: str) -> list[float]:
    """Embedding băm từ và cặp từ: xác định, không cần dịch vụ ngoài."""
    tokens = tokenize(text)
    vec = [0.0] * DIM
    for tok in tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]:
        i, sign = _slot(tok)
        vec[i] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 5) for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _fts_query(text: str) -> str:
    tokens = list(dict.fromkeys(re.sub(r"[.:_-]", " ", t) for t in tokenize(text)))[:40]
    parts = [f'"{p}"' for t in tokens for p in t.split()]
    return " OR ".join(parts)


def scope_filter(project: str | None, user_id: str | None) -> set[str]:
    scopes = {"org", "tenant"}
    if project:
        scopes.add(f"project:{project}")
    if user_id:
        scopes.add(f"user:{user_id}")
    return scopes


def utility(ku: dict) -> float:
    return (ku["helpful_cnt"] + 1) / (ku["helpful_cnt"] + ku["harmful_cnt"] + 2)


def _bm25(conn, tenant_id: str, query: str) -> dict[str, float]:
    match = _fts_query(query)
    if not match:
        return {}
    rs = conn.execute(
        "SELECT ku_id, bm25(ku_fts) AS s FROM ku_fts WHERE ku_fts MATCH ? AND tenant_id=? ORDER BY s LIMIT 100",
        (match, tenant_id)).fetchall()
    if not rs:
        return {}
    best = max(-r["s"] for r in rs) or 1.0
    return {r["ku_id"]: max(0.0, -r["s"] / best) for r in rs}


def retrieve(conn: sqlite3.Connection, tenant_id: str, query: str, *, project: str | None = None,
             user_id: str | None = None, entities: list[str] | None = None, k: int = 15) -> list[dict]:
    scopes = scope_filter(project, user_id)
    now = db.iso()
    rs = conn.execute(
        "SELECT * FROM knowledge_unit WHERE status='active' AND (valid_to IS NULL OR valid_to > ?) "
        "AND (valid_from IS NULL OR valid_from <= ?) AND (tenant_id=? OR scope='org')",
        (now, now, tenant_id)).fetchall()
    kus = {r["id"]: db.row(r, "entities", "source_eps", "embedding") for r in rs}
    kus = {i: ku for i, ku in kus.items() if ku["scope"] in scopes}
    if not kus:
        return []

    qvec = embed(query)
    bm25 = _bm25(conn, tenant_id, query)
    wanted = set(entities or [])
    cutoff = db.now() - timedelta(days=90)
    scored: dict[str, float] = {}
    for ku_id, ku in kus.items():
        vector = max(0.0, cosine(qvec, ku["embedding"]))
        keyword = bm25.get(ku_id, 0.0)
        if vector < 0.05 and keyword == 0 and not (wanted & set(ku["entities"])):
            continue
        score = 0.45 * vector + 0.35 * keyword + 0.2 * utility(ku)
        if wanted & set(ku["entities"]):
            score += 0.1
        last = ku.get("last_used_at") or ku["recorded_at"]
        if datetime.fromisoformat(last) < cutoff:
            score *= 0.7
        scored[ku_id] = score

    top = sorted(scored, key=lambda i: (-scored[i], i))[:k]
    # Mở rộng một bước trên đồ thị, không theo cạnh mâu thuẫn.
    if top:
        marks = ",".join("?" * len(top))
        linked = conn.execute(
            f"SELECT src, dst FROM ku_edge WHERE tenant_id=? AND rel!='contradicts' AND (src IN ({marks}) OR dst IN ({marks}))",
            (tenant_id, *top, *top)).fetchall()
        for edge in linked:
            for other, origin in ((edge["dst"], edge["src"]), (edge["src"], edge["dst"])):
                if other in kus and other not in scored and origin in top:
                    scored[other] = scored[origin] * 0.5

    final = sorted(scored, key=lambda i: (-scored[i], i))[:k]
    out = []
    for ku_id in final:
        ku = {x: y for x, y in kus[ku_id].items() if x != "embedding"}
        ku["score"] = round(scored[ku_id], 4)
        out.append(ku)
    return out


def near(conn, tenant_id: str, text: str, kind: str | None = None, threshold: float = 0.55, limit: int = 5) -> list[dict]:
    """KU gần nghĩa cho Curator, gồm cả candidate."""
    sql = "SELECT * FROM knowledge_unit WHERE tenant_id=? AND status IN ('candidate','active')"
    args: list = [tenant_id]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    qvec = embed(text)
    found = []
    for r in conn.execute(sql, args).fetchall():
        sim = cosine(qvec, json.loads(r["embedding"]))
        if sim >= threshold:
            ku = db.row(r, "entities", "source_eps")
            ku.pop("embedding", None)
            ku["similarity"] = round(sim, 4)
            found.append(ku)
    found.sort(key=lambda x: -x["similarity"])
    return found[:limit]
