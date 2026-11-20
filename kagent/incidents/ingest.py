"""Lấy cảnh báo từ Alertmanager/Prometheus, khử trùng lặp theo fingerprint, đánh dấu hết."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta

from .. import db
from ..tools import ENVS
from .common import SOURCE_TEMPLATES, _event, _state
from .config import settings
from .grouping import _maybe_resolved, group_new

log = logging.getLogger("kagent.incidents")


def _fingerprint(labels: dict) -> str:
    return hashlib.sha1(json.dumps(labels, sort_keys=True).encode()).hexdigest()[:16]


def _env_of(labels: dict, default: str) -> str:
    for key in ("env", "environment"):
        if labels.get(key) in ENVS:
            return labels[key]
    return default


def fetch(conn, tenant_id: str, name: str) -> list[dict]:
    """Các cảnh báo đang bắn (không tính cái đã silence) của một kết nối nguồn, đã chuẩn hoá."""
    from .. import connections
    from ..connectors import alertmanager, prometheus
    from ..connectors.base import ConnectorError, monitoring_client
    row = conn.execute("SELECT * FROM connection WHERE tenant_id=? AND name=? AND status='active'", (tenant_id, name)).fetchone()
    if row is None or row["template"] not in SOURCE_TEMPLATES:
        raise ConnectorError("kết nối nguồn không còn hoạt động")
    row = connections._row(row)
    params = connections.params_of(row)
    default_env = row["envs"][0] if row["envs"] else "dev"
    out = []
    if row["template"] == "alertmanager":
        with monitoring_client(params) as c:
            raw = alertmanager._alerts(c)
        for a in raw if isinstance(raw, list) else []:
            labels = a.get("labels") or {}
            if (a.get("status") or {}).get("state", "active") != "active":
                continue
            out.append({"fingerprint": a.get("fingerprint") or _fingerprint(labels), "starts_at": a.get("startsAt") or "", "labels": labels,
                        "summary": (a.get("annotations") or {}).get("summary") or (a.get("annotations") or {}).get("description") or ""})
    else:
        for a in prometheus._get(params, "alerts").get("alerts", []):
            if a.get("state") != "firing":
                continue
            labels = a.get("labels") or {}
            out.append({"fingerprint": _fingerprint(labels), "starts_at": a.get("activeAt") or "", "labels": labels,
                        "summary": (a.get("annotations") or {}).get("summary") or (a.get("annotations") or {}).get("description") or ""})
    for a in out:
        a["alertname"] = a["labels"].get("alertname")
        a["severity"] = str(a["labels"].get("severity") or "").lower() or None
        a["env"] = _env_of(a["labels"], default_env)
        a["summary"] = str(a["summary"])[:1000]
    return out


def poll(conn, tenant_id: str) -> dict:
    """Một vòng lấy cảnh báo: thêm mới, đánh dấu hết, gom cảnh báo mới vào sự cố. Lỗi của nguồn này không chặn nguồn khác."""
    from .. import net
    from ..connectors.base import ConnectorError
    cfg = settings(conn, tenant_id)
    state = _state.setdefault(tenant_id, {"last_poll": 0.0, "sources": {}})
    fetched: dict[str, list[dict]] = {}
    for name in cfg["sources"]:
        try:
            fetched[name] = fetch(conn, tenant_id, name)
            state["sources"][name] = {"at": db.iso(), "count": len(fetched[name]), "error": None}
        except (ConnectorError, net.NetError) as exc:
            state["sources"][name] = {"at": db.iso(), "count": None, "error": str(exc)[:300]}
            log.warning("tenant %s: không lấy được cảnh báo từ %s: %s", tenant_id, name, exc)
    state["last_poll"] = db.now().timestamp()
    now = db.iso()
    new, resolved = [], 0
    with db.tx(conn):
        for name, alerts in fetched.items():
            seen = []
            for a in alerts:
                r = conn.execute("SELECT id FROM alert_event WHERE tenant_id=? AND source=? AND fingerprint=? AND starts_at=?",
                                 (tenant_id, name, a["fingerprint"], a["starts_at"])).fetchone()
                if r:
                    conn.execute("UPDATE alert_event SET last_seen=?, summary=?, status='firing', resolved_at=NULL WHERE id=?", (now, a["summary"], r["id"]))
                    seen.append(r["id"])
                    continue
                aid = insert(conn, tenant_id, name, a, now, cfg)
                seen.append(aid)
                new.append(aid)
            marks = ",".join("?" * len(seen)) or "''"
            gone = conn.execute(f"SELECT id, incident_id, alertname FROM alert_event WHERE tenant_id=? AND source=? AND status='firing' AND id NOT IN ({marks})",
                                (tenant_id, name, *seen)).fetchall()
            for g in gone:
                resolve(conn, tenant_id, g, now)
                resolved += 1
    # Gom sau khi đã ghi xong: gom có thể gọi LLM, không được giữ giao dịch trong lúc đó.
    group_new(conn, tenant_id, new, cfg)
    return {"new": len(new), "resolved": resolved, "sources": state["sources"]}


FLAP_WINDOW = timedelta(hours=1)


def resolve(conn, tenant_id: str, g, now: str) -> None:
    """Cảnh báo không còn trong nguồn: đánh dấu hết, sự cố hết cảnh báo đang bắn thì thành đã hết."""
    conn.execute("UPDATE alert_event SET status='resolved', resolved_at=? WHERE id=?", (now, g["id"]))
    if g["incident_id"]:
        _event(conn, tenant_id, g["incident_id"], "alert_resolved", f"cảnh báo {g['alertname']} đã hết")
        _maybe_resolved(conn, tenant_id, g["incident_id"])


def insert(conn, tenant_id: str, source: str, a: dict, now: str, cfg: dict, alert_id: str | None = None) -> str:
    """Ghi một lần bắn mới. Cùng fingerprint đã bắn từ flap_count lần trong 1 giờ (tính cả lần này) thì đánh dấu chập chờn."""
    flapping = 0
    limit = cfg["grouping"]["flap_count"]
    if limit:
        before = conn.execute("SELECT count(*) n FROM alert_event WHERE tenant_id=? AND source=? AND fingerprint=? AND first_seen>=?",
                              (tenant_id, source, a["fingerprint"], db.iso(db.now() - FLAP_WINDOW))).fetchone()["n"]
        flapping = int(before + 1 >= limit)
    aid = alert_id or db.new_id()
    conn.execute("INSERT INTO alert_event(id,tenant_id,source,fingerprint,starts_at,alertname,severity,labels,summary,env,first_seen,last_seen,flapping) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (aid, tenant_id, source, a["fingerprint"], a["starts_at"], a["alertname"], a["severity"],
                  json.dumps(a["labels"], ensure_ascii=False), a["summary"], a["env"], now, now, flapping))
    return aid


def source_status(tenant_id: str) -> dict:
    state = _state.get(tenant_id) or {}
    return {"last_poll": state.get("last_poll") or None, "sources": state.get("sources", {})}
