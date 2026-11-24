"""Tên thực thể của KU: cùng một thứ chỉ có một tên, để đồ thị L3 nối được các KU và truy xuất khớp được theo thực thể.

Dạng chuẩn: chữ thường, khoảng trắng đơn; công cụ là `tool:<kết nối>.<lệnh>` kèm tên kết nối; mã HTTP là `http 500`;
bỏ hậu tố `.keyword` của trường Elasticsearch và `*` cuối index pattern. Admin gộp tên thì tên cũ thành bí danh của tenant.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from . import db

VERSION = "2"  # tăng khi đổi luật để chuẩn hoá lại KU đã có lúc khởi động
MAX_LEN = 80
MAX_PER_KU = 8
_SYNONYM = {"kubernetes": "k8s", "elastic": "elasticsearch", "bad gateway": "http 502", "internal server error": "http 500"}
_HTTP = re.compile(r"^(?:http[ _-]?|status(?:_code)?[ :=]*|lỗi |errors? )?([1-5](?:\d\d|xx))(?: errors?| lỗi)?$")
_TRAIL = re.compile(r"(?:\.keyword|[-_.]?\*+)$")


class Vocab:
    def __init__(self, tools: list[str], aliases: dict[str, str]):
        self.tools = set(tools)
        self.connections = {t.split(".", 1)[0] for t in tools if "." in t}
        self.by_op: dict[str, set[str]] = defaultdict(set)
        for t in tools:
            if "." in t:
                self.by_op[t.split(".", 1)[1]].add(t)
        self.aliases = aliases


def vocab(conn, tenant_id: str) -> Vocab:
    tools = [r["tool"] for r in conn.execute("SELECT tool FROM tool_registry WHERE tenant_id=?", (tenant_id,))]
    aliases = {r["alias"]: r["canonical"] for r in conn.execute("SELECT alias, canonical FROM entity_alias WHERE tenant_id=?", (tenant_id,))}
    return Vocab(tools, aliases)


def clean(name: str) -> str:
    s = " ".join(str(name).split()).strip("`'\" ").lower()[:MAX_LEN]
    while (t := _TRAIL.sub("", s)) != s:
        s = t
    if m := _HTTP.match(s):
        return f"http {m.group(1)}"
    return _SYNONYM.get(s, s)


def normalize_one(name: str, v: Vocab) -> list[str]:
    s = clean(name)
    tool = s.startswith("tool:")
    s = s[5:] if tool else s
    if not s:
        return []
    s = v.aliases.get(s, s)
    if s in v.tools or ("." in s and s.split(".", 1)[0] in v.connections):
        return [f"tool:{s}", s.split(".", 1)[0]]
    if len(v.by_op.get(s, ())) == 1:
        full = next(iter(v.by_op[s]))
        return [f"tool:{full}", full.split(".", 1)[0]]
    return [f"tool:{s}"] if tool else [s]


def normalize(conn, tenant_id: str, names: list[str] | None, v: Vocab | None = None) -> list[str]:
    v = v or vocab(conn, tenant_id)
    out: list[str] = []
    for n in names or []:
        for e in normalize_one(n, v):
            e = v.aliases.get(e, e)
            if e not in out:
                out.append(e)
    return sorted(out[:MAX_PER_KU])


def top(conn, tenant_id: str, limit: int = 40) -> list[str]:
    """Tên đang dùng nhiều nhất, đưa cho Reflector để nó dùng lại thay vì đặt tên mới."""
    rs = conn.execute("SELECT e.value v, count(*) n FROM knowledge_unit k, json_each(k.entities) e WHERE k.tenant_id=? "
                      "AND k.status IN ('active','candidate') GROUP BY e.value ORDER BY n DESC LIMIT ?", (tenant_id, limit)).fetchall()
    return [r["v"] for r in rs]


def renormalize(conn) -> int:
    """Chuẩn hoá lại KU đã có khi luật đổi; chạy một lần cho mỗi VERSION."""
    from .knowledge import _index, get
    r = conn.execute("SELECT value FROM app_setting WHERE key='entity_norm'").fetchone()
    if r and r["value"] == VERSION:
        return 0
    changed = 0
    with db.tx(conn):
        for t in [r["id"] for r in conn.execute("SELECT id FROM tenant")]:
            v = vocab(conn, t)
            for r in conn.execute("SELECT id, entities FROM knowledge_unit WHERE tenant_id=?", (t,)).fetchall():
                old = db.row(r, "entities")["entities"]
                new = normalize(conn, t, old, v)
                if new != old:
                    conn.execute("UPDATE knowledge_unit SET entities=? WHERE id=?", (json.dumps(new, ensure_ascii=False), r["id"]))
                    _index(conn, get(conn, t, r["id"]))
                    changed += 1
        conn.execute("INSERT INTO app_setting(key,value) VALUES ('entity_norm',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (VERSION,))
    return changed


def merge(conn, tenant_id: str, sources: list[str], target: str, actor: str) -> dict:
    """Gộp các tên vào một tên: sửa mọi KU đang dùng và ghi bí danh để KU học sau này cũng dùng tên đích."""
    from .knowledge import update
    v = vocab(conn, tenant_id)
    dst = normalize(conn, tenant_id, [target], v)
    if len(dst) != 1:
        raise ValueError("tên đích phải là một thực thể, không phải công cụ")
    dst = dst[0]
    srcs = {clean(s) for s in sources} - {dst, ""}
    if not srcs:
        raise ValueError("không có tên nào để gộp")
    if dst.startswith("tool:") or any(s.startswith("tool:") for s in srcs):
        raise ValueError("tên công cụ do hệ thống đặt, không gộp được")
    kus = 0
    with db.tx(conn):
        for s in srcs:
            conn.execute("INSERT INTO entity_alias(tenant_id,alias,canonical,created_by,created_at) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(tenant_id,alias) DO UPDATE SET canonical=excluded.canonical", (tenant_id, s, dst, actor, db.iso()))
            conn.execute("UPDATE entity_alias SET canonical=? WHERE tenant_id=? AND canonical=?", (dst, tenant_id, s))
        conn.execute("DELETE FROM entity_alias WHERE tenant_id=? AND alias=?", (tenant_id, dst))
        marks = " OR ".join("e.value=?" for _ in srcs)
        rows = conn.execute(f"SELECT DISTINCT k.id, k.entities FROM knowledge_unit k, json_each(k.entities) e WHERE k.tenant_id=? AND ({marks})",
                            (tenant_id, *srcs)).fetchall()
        for r in rows:
            ents = sorted({dst if e in srcs else e for e in db.row(r, "entities")["entities"]})
            update(conn, tenant_id, r["id"], {"entities": ents}, "UPDATE", actor, f"gộp thực thể {', '.join(sorted(srcs))} → {dst}")
            kus += 1
    return {"target": dst, "merged": sorted(srcs), "kus": kus}

