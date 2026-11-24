"""Gộp KU trùng ý theo cụm chủ đề, kể cả KU đang dùng.

Cụm = các KU cùng loại trên cùng một trang thực thể; thực thể hiếm xét trước nên cụm cụ thể nhất được nhận KU.
LLM chia cụm thành nhóm "cùng một điều":
- ứng viên trùng một KU khác → tự xử lý: loại ứng viên, KU giữ lại nhận thêm nguồn (nội dung agent thấy không đổi);
- nhóm chỉ toàn ứng viên cần viết bản gộp → tự gộp thành một ứng viên mới, cổng duyệt xét như mọi ứng viên;
- nhóm có KU đang dùng cần viết bản gộp, hoặc có từ 2 KU đang dùng → đề xuất, chờ admin duyệt, vì đổi điều agent sẽ thấy.
Cụm không đổi từ lần xét trước thì không gọi lại LLM.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from . import db, knowledge, llm
from .compiler import _rank

log = logging.getLogger("aiops.dedup")

MAX_CLUSTER = 20
CLUSTERS_PER_RUN = 60  # mỗi cụm một lần gọi model mạnh (~700 token input trên dữ liệu thật); cụm còn lại để lần chạy sau
_running: set[str] = set()
_running_lock = threading.Lock()


def _fingerprint(members: list[dict]) -> str:
    return hashlib.sha1(",".join(sorted(f"{m['id']}:{m['version']}" for m in members)).encode()).hexdigest()


def _pending_ids(conn, tenant_id: str) -> set[str]:
    return {i for r in conn.execute("SELECT ids FROM ku_merge_proposal WHERE tenant_id=? AND status='pending'", (tenant_id,))
            for i in json.loads(r["ids"])}


def clusters(conn, tenant_id: str) -> list[tuple[str, list[dict]]]:
    """(thực thể, KU) theo thứ tự xét: thực thể hiếm trước, mỗi KU chỉ ở một cụm, bỏ KU đang nằm trong đề xuất chờ duyệt."""
    rs = conn.execute("SELECT * FROM knowledge_unit WHERE tenant_id=? AND status IN ('active','candidate') AND scope='tenant'", (tenant_id,)).fetchall()
    kus = [knowledge.get(conn, tenant_id, r["id"]) for r in rs]
    busy = _pending_ids(conn, tenant_id)
    by_entity: dict[tuple[str, str], list[dict]] = {}
    df: dict[str, int] = {}
    for ku in kus:
        for e in ku["entities"]:
            df[e] = df.get(e, 0) + 1
            by_entity.setdefault((e, ku["kind"]), []).append(ku)
    taken: set[str] = set(busy)
    out = []
    for (entity, _kind), group in sorted(by_entity.items(), key=lambda x: (df[x[0][0]], x[0])):
        members = sorted((k for k in group if k["id"] not in taken), key=_rank)[:MAX_CLUSTER]
        if len(members) < 2:
            continue
        taken.update(m["id"] for m in members)
        out.append((entity, members))
    return out


def confirm(conn, tenant_id: str, keep: dict, dup: dict, actor: str = "dedup") -> None:
    """Ứng viên nói đúng điều KU keep đã nói: loại ứng viên; nguồn của nó thành một lần xác nhận cho keep.

    Nguồn kém tin hơn keep thì không cộng: một đoạn log bị cài không được đẩy KU tin cậy tới ngưỡng tự publish quy ước core.
    """
    if knowledge.TRUST_ORDER.index(dup["source_trust"]) >= knowledge.TRUST_ORDER.index(keep["source_trust"]):
        sources = sorted(set(keep["source_eps"]) | set(dup["source_eps"]))
        if sources != sorted(keep["source_eps"]):
            knowledge.update(conn, tenant_id, keep["id"], {"source_eps": sources}, "UPDATE", actor, f"xác nhận bởi KU trùng ý {dup['id'][:8]}")
    knowledge.update(conn, tenant_id, dup["id"], {"status": "rejected", "superseded_by": keep["id"]}, "CONFIRM", actor,
                     f"trùng ý với \"{keep['title'][:80]}\"")


def _propose(conn, tenant_id: str, entity: str, members: list[dict], title: str, content: str, applies_when: str, reason: str) -> bool:
    ids = sorted(m["id"] for m in members)
    if conn.execute("SELECT 1 FROM ku_merge_proposal WHERE tenant_id=? AND status='rejected' AND ids=?", (tenant_id, json.dumps(ids))).fetchone():
        return False  # admin đã bỏ qua đúng nhóm này
    conn.execute("INSERT INTO ku_merge_proposal(id,tenant_id,ids,kind,title,content,applies_when,reason,entity,status,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
                 (db.new_id(), tenant_id, json.dumps(ids), members[0]["kind"], title, content, applies_when or None, reason, entity, db.iso()))
    return True


def apply(conn, tenant_id: str, entity: str, members: list[dict], groups: list[dict]) -> dict:
    by_id = {m["id"]: m for m in members}
    done = {"confirmed": 0, "merged": 0, "proposed": 0}
    for g in groups:
        group = [by_id[i] for i in g["ids"]]
        actives = [m for m in group if m["status"] == "active"]
        if g["same"]:
            keep = by_id[g["keep"]]
            if keep["status"] != "active" and actives:
                keep = min(actives, key=_rank)
            for m in group:
                if m is not keep and m["status"] == "candidate":
                    confirm(conn, tenant_id, keep, m)
                    done["confirmed"] += 1
            if len(actives) >= 2:
                done["proposed"] += _propose(conn, tenant_id, entity, actives, keep["title"], keep["content"], keep.get("applies_when") or "",
                                             g["reason"] or "các KU đang dùng nói cùng một điều")
        elif not actives:
            try:
                knowledge.merge(conn, tenant_id, [m["id"] for m in group], g, "dedup")
                done["merged"] += len(group)
            except ValueError:  # LLM trả bản gộp rỗng: để admin viết
                done["proposed"] += _propose(conn, tenant_id, entity, group, g["title"], g["content"], g["applies_when"], g["reason"])
        else:
            done["proposed"] += _propose(conn, tenant_id, entity, group, g["title"], g["content"], g["applies_when"], g["reason"])
    return done


def run(conn, tenant_id: str, provider=None, limit: int = CLUSTERS_PER_RUN) -> dict:
    provider = provider or llm.get_provider()
    summary = {"at": db.iso(), "clusters": 0, "skipped": 0, "confirmed": 0, "merged": 0, "proposed": 0, "remaining": 0, "errors": []}
    with llm.collect_usage(tenant_id) as usage:
        try:
            for entity, members in clusters(conn, tenant_id):
                fp = _fingerprint(members)
                if conn.execute("SELECT 1 FROM ku_dedup_seen WHERE tenant_id=? AND fingerprint=?", (tenant_id, fp)).fetchone():
                    summary["skipped"] += 1
                    continue
                if summary["clusters"] >= limit:
                    summary["remaining"] += 1
                    continue
                summary["clusters"] += 1
                try:
                    groups = provider.dedup(members)
                except llm.LLMError as exc:
                    summary["errors"].append(f"{entity}: {exc}")
                    continue
                for k, v in apply(conn, tenant_id, entity, members, groups).items():
                    summary[k] += v
                conn.execute("INSERT OR IGNORE INTO ku_dedup_seen(tenant_id, fingerprint, at) VALUES (?,?,?)", (tenant_id, fp, db.iso()))
        finally:
            if usage:
                llm.save_usage(conn, tenant_id, None, usage)
    return summary


def proposals(conn, tenant_id: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM ku_merge_proposal WHERE tenant_id=? AND status='pending' ORDER BY created_at DESC, rowid DESC", (tenant_id,)).fetchall():
        p = db.row(r, "ids")
        p["members"] = [{k: ku[k] for k in ("id", "status", "kind", "title", "content", "applies_when", "source_trust", "helpful_cnt", "harmful_cnt")}
                        for ku in (knowledge.get(conn, tenant_id, i) for i in p["ids"]) if ku]
        out.append(p)
    return out


def decide(conn, tenant_id: str, proposal_id: str, approve: bool, actor: str, edits: dict | None = None) -> dict:
    r = conn.execute("SELECT * FROM ku_merge_proposal WHERE id=? AND tenant_id=?", (proposal_id, tenant_id)).fetchone()
    if r is None:
        raise KeyError(proposal_id)
    p = db.row(r, "ids")
    if p["status"] != "pending":
        raise ValueError(f"đề xuất đã ở trạng thái {p['status']}")
    merged_into = None
    if approve:
        kus = [knowledge.get(conn, tenant_id, i) for i in p["ids"]]
        if any(k is None or k["status"] not in ("active", "candidate") for k in kus):
            conn.execute("UPDATE ku_merge_proposal SET status='stale', decided_at=? WHERE id=?", (db.iso(), proposal_id))
            raise ValueError("có KU trong đề xuất đã đổi trạng thái; đề xuất bị bỏ, lần tìm sau sẽ xét lại")
        body = {"kind": p["kind"], "title": p["title"], "content": p["content"], "applies_when": p["applies_when"]} | (edits or {})
        merged_into = knowledge.merge(conn, tenant_id, p["ids"], body, actor, activate=True, allow_active=True)["id"]
    conn.execute("UPDATE ku_merge_proposal SET status=?, decided_by=?, decided_at=?, merged_into=? WHERE id=?",
                 ("accepted" if approve else "rejected", actor, db.iso(), merged_into, proposal_id))
    return {"id": proposal_id, "status": "accepted" if approve else "rejected", "merged_into": merged_into}


def _background(tenant_id: str) -> None:
    try:
        with db.session() as conn:
            summary = run(conn, tenant_id)
            from . import lint
            lint.attach(conn, tenant_id, "dedup", summary)
    except Exception:
        log.exception("không tìm được KU trùng cho tenant %s", tenant_id)
    finally:
        with _running_lock:
            _running.discard(tenant_id)


def start(tenant_id: str) -> bool:
    with _running_lock:
        if tenant_id in _running:
            return False
        _running.add(tenant_id)
    threading.Thread(target=_background, args=(tenant_id,), name=f"dedup-{tenant_id[:8]}", daemon=True).start()
    return True


def running(tenant_id: str) -> bool:
    return tenant_id in _running
