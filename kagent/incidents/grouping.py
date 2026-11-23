"""Gom cảnh báo thành sự cố (luật nhãn, quan hệ k8s, rồi mới tới LLM), mở lại, gộp, tách, đóng."""
from __future__ import annotations

import json
import re
from datetime import timedelta

from .. import db, live
from .common import IncidentError, _event, _later, _method_label, _running, _worse
from .config import bot_user, settings


def _owner(pod: str | None) -> str | None:
    """Tên workload từ tên pod: checkout-7d9f8c6b5-x2k4q → checkout, web-0 → web."""
    if not pod:
        return None
    parts = pod.split("-")
    if len(parts) >= 3 and re.fullmatch(r"[a-z0-9]{5}", parts[-1]) and re.fullmatch(r"[a-z0-9]{6,10}", parts[-2]):
        return "-".join(parts[:-2])
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[:-1])
    return None


# Cảnh báo từ các job này mang nhãn pod/service/instance của chính exporter (vd. pod monitor-kube-state-metrics),
# không phải của đối tượng bị lỗi; giữ lại thì cảnh báo ở các namespace khác nhau trông như cùng một pod.
EXPORTER_JOBS = ("kube-state-metrics",)
EXPORTER_LABELS = ("pod", "service", "instance", "endpoint", "job", "container")


def target_labels(labels: dict) -> dict:
    job = str(labels.get("job") or "")
    if not any(job == j or job.endswith("-" + j) for j in EXPORTER_JOBS):
        return labels
    out = dict(labels)
    for key in EXPORTER_LABELS:
        value = str(out.get(key) or "")
        if key in ("job", "service", "instance", "endpoint") or any(j in value for j in EXPORTER_JOBS):
            out.pop(key, None)
    return out


def match(a: dict, b: dict, keys: list[str]) -> tuple[str, float, str] | None:
    """(cách gộp, điểm, lý do) nếu hai cảnh báo nên chung một sự cố theo luật nhãn hoặc quan hệ k8s."""
    a, b = target_labels(a), target_labels(b)
    common = [k for k in keys if a.get(k) and b.get(k)]
    if common and all(a[k] == b[k] for k in common):
        return "rule", 1.0, "cùng " + ", ".join(f"{k}={a[k]}" for k in common)
    if a.get("node") and a.get("node") == b.get("node"):
        return "topology", 0.9, f"cùng node {a['node']}"
    owners = [(x.get("deployment") or x.get("statefulset") or x.get("daemonset") or _owner(x.get("pod"))) for x in (a, b)]
    if owners[0] and owners[0] == owners[1] and (not a.get("namespace") or not b.get("namespace") or a["namespace"] == b["namespace"]):
        return "topology", 0.9, f"cùng workload {owners[0]}"
    return None


def _brief(row) -> dict:
    labels = json.loads(row["labels"]) if isinstance(row["labels"], str) else row["labels"]
    return {"alertname": row["alertname"], "severity": row["severity"], "summary": (row["summary"] or "")[:300], "since": row["starts_at"],
            "labels": {k: str(v)[:100] for k, v in labels.items() if k not in ("alertname", "severity")}}


def _feedback(conn, tenant_id: str) -> list[str]:
    rs = conn.execute("SELECT text FROM incident_event WHERE tenant_id=? AND kind IN ('merge','split') ORDER BY id DESC LIMIT 10", (tenant_id,)).fetchall()
    return [r["text"] for r in rs]


def assign(conn, tenant_id: str, alert_id: str, cfg: dict | None = None) -> str:
    """Gom một cảnh báo mới vào sự cố đang mở phù hợp nhất, hoặc mở sự cố mới. Trả về id sự cố."""
    from .. import llm
    cfg = cfg or settings(conn, tenant_id)
    a = conn.execute("SELECT * FROM alert_event WHERE id=?", (alert_id,)).fetchone()
    labels = json.loads(a["labels"])
    since = (db.now() - timedelta(minutes=cfg["window_minutes"])).isoformat(timespec="seconds")
    # Khung window_minutes chỉ áp cho sự cố không còn cảnh báo đang bắn: sự cố kéo dài nhiều ngày vẫn nhận cảnh báo bắn lại.
    candidates = conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' AND env=? AND (last_alert_at>=? OR EXISTS "
                              "(SELECT 1 FROM alert_event e WHERE e.incident_id=incident.id AND e.status='firing')) ORDER BY last_alert_at DESC",
                              (tenant_id, a["env"], since)).fetchall()
    found = None
    for inc in candidates:
        for other in conn.execute("SELECT labels FROM alert_event WHERE incident_id=?", (inc["id"],)).fetchall():
            m = match(labels, json.loads(other["labels"]), cfg["group_labels"])
            if m and (found is None or m[1] > found[1][1]):
                found = (inc["id"], m)
        if found and found[1][0] == "rule":
            break
    if found is None and candidates:
        found = _infra_match(conn, tenant_id, labels, a["starts_at"], candidates)
    if found is None and cfg["ai_grouping"] and candidates:
        incidents = [{"id": i["id"], "title": i["title"], "alerts": [_brief(r) for r in conn.execute(
            "SELECT * FROM alert_event WHERE incident_id=? ORDER BY starts_at LIMIT 8", (i["id"],)).fetchall()]} for i in candidates[:5]]
        try:
            verdict = llm.get_provider().correlate(_brief(a), incidents, _feedback(conn, tenant_id))
        except llm.LLMError as exc:
            verdict = {"incident_id": None, "score": 0.0, "reason": f"lỗi LLM: {exc}"}
        if verdict["incident_id"] and verdict["score"] >= cfg["ai_min_score"]:
            found = (verdict["incident_id"], ("ai", verdict["score"], verdict["reason"] or "LLM đánh giá liên quan"))
    reopen = False
    if found is None and cfg["reopen_hours"]:
        found = _rule_match(conn, labels, cfg, conn.execute(
            "SELECT * FROM incident WHERE tenant_id=? AND status='resolved' AND env=? AND resolved_at>=? ORDER BY resolved_at DESC",
            (tenant_id, a["env"], db.iso(db.now() - timedelta(hours=cfg["reopen_hours"])))).fetchall())
        reopen = found is not None
    with db.tx(conn):
        return _attach(conn, tenant_id, a, labels, found, cfg, reopen)


def _infra_match(conn, tenant_id: str, labels: dict, at: str | None, incidents_, cache: dict | None = None) -> tuple | None:
    """Sự cố đầu tiên có cảnh báo cùng tầng dưới đặc hiệu theo topology, chỉ khi mọi cạnh trên đường nối tin cậy."""
    from .. import topology
    cache = {} if cache is None else cache
    for inc in incidents_:
        others = [(json.loads(r["labels"]), r["starts_at"]) for r in conn.execute("SELECT labels, starts_at FROM alert_event WHERE incident_id=?", (inc["id"],))]
        m = topology.infra_match(conn, tenant_id, (labels, at), others, cache)
        if m and m[0]:
            return inc["id"], ("infra", 0.85, m[1])
    return None


def _rule_match(conn, labels: dict, cfg: dict, incidents_) -> tuple | None:
    """Sự cố đầu tiên có cảnh báo khớp theo luật nhãn hoặc quan hệ k8s; không dùng AI vì mở lại hay gộp là quyết định không có người xem."""
    for inc in incidents_:
        for other in conn.execute("SELECT labels FROM alert_event WHERE incident_id=?", (inc["id"],)).fetchall():
            m = match(labels, json.loads(other["labels"]), cfg["group_labels"])
            if m:
                return inc["id"], m
    return None


def alert_title(alertname: str | None, labels: dict) -> str:
    """Tiêu đề trước khi điều tra: tên cảnh báo và nơi bắn."""
    where = labels.get("namespace") or labels.get("service") or labels.get("job") or labels.get("instance")
    return f"{alertname or 'Cảnh báo'}{f' · {where}' if where else ''}"[:200]


def reset_long_titles(conn) -> int:
    """Tiêu đề cũ là câu kết luận dài của báo cáo: về lại tiêu đề theo cảnh báo gốc, tới khi lượt điều tra sau viết triệu chứng."""
    n = 0
    for inc in conn.execute("SELECT id, title FROM incident WHERE length(title) > 80").fetchall():
        root = conn.execute("SELECT alertname, labels FROM alert_event WHERE incident_id=? ORDER BY join_method!='root', starts_at LIMIT 1", (inc["id"],)).fetchone()
        if root:
            conn.execute("UPDATE incident SET title=? WHERE id=?", (alert_title(root["alertname"], json.loads(root["labels"])), inc["id"]))
            n += 1
    return n


def _attach(conn, tenant_id: str, a, labels: dict, found, cfg: dict, reopen: bool = False) -> str:
    alert_id = a["id"]
    now = db.iso()
    if found:
        incident_id, (method, score, reason) = found
        if reopen:
            conn.execute("UPDATE incident SET status='open', resolved_at=NULL, stage=CASE WHEN stage='investigating' THEN stage ELSE 'new' END, "
                         "verify_due=NULL, repeats=repeats+1 WHERE id=?", (incident_id,))
            _event(conn, tenant_id, incident_id, "reopened", f"mở lại vì {a['alertname']} bắn lại ({_method_label(method)}: {reason})",
                   {"alert_id": alert_id})
        inc = conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone()
        rerun = inc["stage"] not in ("investigating", "awaiting_approval", "verifying") and cfg["investigate"]
        conn.execute("UPDATE incident SET last_alert_at=?, updated_at=?, severity=?, investigate_after=CASE WHEN ? THEN ? ELSE investigate_after END WHERE id=?",
                     (now, now, _worse(inc["severity"], a["severity"]), int(rerun), _later(cfg["debounce_seconds"]), incident_id))
        conn.execute("UPDATE alert_event SET incident_id=?, join_method=?, join_score=?, join_reason=? WHERE id=?", (incident_id, method, score, reason, alert_id))
        _event(conn, tenant_id, incident_id, "alert_joined", f"gộp cảnh báo {a['alertname']} ({_method_label(method)}: {reason})",
               {"alert_id": alert_id, "method": method, "score": score})
        return incident_id
    incident_id = db.new_id()
    conn.execute("INSERT INTO incident(id,tenant_id,env,title,severity,status,stage,created_at,updated_at,last_alert_at,investigate_after) "
                 "VALUES (?,?,?,?,?,'open','new',?,?,?,?)",
                 (incident_id, tenant_id, a["env"], alert_title(a["alertname"], labels), a["severity"], now, now, now,
                  _later(cfg["debounce_seconds"]) if cfg["investigate"] else None))
    conn.execute("UPDATE alert_event SET incident_id=?, join_method='root', join_score=1.0, join_reason='cảnh báo mở sự cố' WHERE id=?", (incident_id, alert_id))
    _event(conn, tenant_id, incident_id, "opened", f"mở sự cố từ cảnh báo {a['alertname']}", {"alert_id": alert_id})
    return incident_id


def _maybe_resolved(conn, tenant_id: str, incident_id: str) -> None:
    left = conn.execute("SELECT count(*) n FROM alert_event WHERE incident_id=? AND status='firing'", (incident_id,)).fetchone()["n"]
    inc = conn.execute("SELECT status FROM incident WHERE id=?", (incident_id,)).fetchone()
    if left == 0 and inc and inc["status"] == "open":
        conn.execute("UPDATE incident SET status='resolved', resolved_at=?, updated_at=?, investigate_after=NULL WHERE id=?", (db.iso(), db.iso(), incident_id))
        _event(conn, tenant_id, incident_id, "resolved", "mọi cảnh báo đã hết")


def merge(conn, tenant_id: str, incident_id: str, into: str, user: str, auto_reason: str | None = None) -> None:
    """Gộp sự cố incident_id vào into. auto_reason có nghĩa kagent tự gộp: ghi kind riêng để không thành phản hồi của người cho AI gom."""
    src, dst = (conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (i, tenant_id)).fetchone() for i in (incident_id, into))
    if src is None or dst is None or incident_id == into:
        raise IncidentError("không tìm thấy sự cố", 404)
    names = lambda iid: {r["alertname"] or "?" for r in conn.execute("SELECT alertname FROM alert_event WHERE incident_id=?", (iid,))}
    src_names, dst_names = names(incident_id), names(into)
    conn.execute("UPDATE alert_event SET incident_id=?, join_method=?, join_score=1.0, join_reason=? WHERE incident_id=?",
                 (into, "rule" if auto_reason else "manual", f"kagent tự gộp từ sự cố {src['title']}: {auto_reason}" if auto_reason
                  else f"người gộp từ sự cố {src['title']}", incident_id))
    # Lệnh còn chờ duyệt đi theo sự cố đích: sự cố nguồn đã đóng thì không còn ai theo dõi để xác minh.
    moved = [r["id"] for r in conn.execute(
        "SELECT a.id FROM incident_action a JOIN approval_request q ON q.id=a.approval_id WHERE a.incident_id=? AND a.origin='investigation' "
        "AND a.status='pending' AND q.status='pending'", (incident_id,)).fetchall()]
    for action_id in moved:
        conn.execute("UPDATE incident_action SET incident_id=? WHERE id=?", (into, action_id))
        conn.execute("UPDATE incident_event SET incident_id=? WHERE action_id=?", (into, action_id))
    dst_stage = dst["stage"]
    if moved and dst_stage not in ("investigating", "verifying"):
        dst_stage = "awaiting_approval"
    cfg = settings(conn, tenant_id)
    rerun = bool(src_names - dst_names) and cfg["investigate"] and dst["status"] == "open" and into not in _running \
        and dst_stage not in ("investigating", "awaiting_approval", "verifying")
    conn.execute("UPDATE incident SET status='closed', closed_by=?, verify_due=NULL, updated_at=?, investigate_after=NULL WHERE id=?",
                 (user, db.iso(), incident_id))
    conn.execute("UPDATE incident SET last_alert_at=max(last_alert_at, ?), severity=?, stage=?, updated_at=?, repeats=repeats+1+?, "
                 "investigate_after=CASE WHEN ? THEN ? ELSE investigate_after END WHERE id=?",
                 (src["last_alert_at"], _worse(src["severity"], dst["severity"]), dst_stage, db.iso(), src["repeats"], int(rerun),
                  _later(cfg["debounce_seconds"]), into))
    listed = ", ".join(sorted(src_names))
    if auto_reason:
        _event(conn, tenant_id, into, "auto_merge", f"kagent tự gộp [{listed}] ({src['title']}) vào [{dst['title']}]: {auto_reason}",
               {"from": incident_id, "by": user, "moved_actions": len(moved)})
    else:
        _event(conn, tenant_id, into, "merge", f"người dùng gộp [{listed}] ({src['title']}) vào [{dst['title']}]: các cảnh báo này liên quan",
               {"from": incident_id, "by": user, "moved_actions": len(moved)})
    _event(conn, tenant_id, incident_id, "merged", f"đã gộp vào sự cố {dst['title']}", {"into": into, "by": user, "auto": bool(auto_reason)})
    _maybe_resolved(conn, tenant_id, into)


def auto_merge(conn, tenant_id: str, cfg: dict | None = None) -> int:
    """Gộp các sự cố đang mở khớp nhau theo luật nhãn hoặc quan hệ k8s vào sự cố cũ nhất. Trả về số sự cố đã gộp.

    Bỏ qua sự cố đang có việc dở (điều tra, chờ duyệt, xác minh, lượt chat đang chạy) và sự cố người đã tách hay gộp tay,
    để không làm ngược quyết định của người.
    """
    cfg = cfg or settings(conn, tenant_id)
    rows = conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' ORDER BY created_at", (tenant_id,)).fetchall()
    alerts = {r["id"]: conn.execute("SELECT labels, join_method, starts_at FROM alert_event WHERE incident_id=?", (r["id"],)).fetchall() for r in rows}
    cache: dict = {}
    merged, user = set(), None
    for j, src in enumerate(rows):
        if (src["id"] in _running or src["stage"] in ("investigating", "awaiting_approval", "verifying")
                or (src["conversation_id"] and live.current(src["conversation_id"]))
                or any(a["join_method"] == "manual" for a in alerts[src["id"]])):
            continue
        for dst in rows[:j]:
            if dst["id"] in merged or dst["env"] != src["env"]:
                continue
            found = next((m for a in alerts[src["id"]] for b in alerts[dst["id"]]
                          if (m := match(json.loads(a["labels"]), json.loads(b["labels"]), cfg["group_labels"]))), None)
            if found is None:
                from .. import topology
                others = [(json.loads(b["labels"]), b["starts_at"]) for b in alerts[dst["id"]]]
                found = next((("infra", 0.85, m[1]) for a in alerts[src["id"]]
                              if (m := topology.infra_match(conn, tenant_id, (json.loads(a["labels"]), a["starts_at"]), others, cache)) and m[0]), None)
            if found:
                user = user or bot_user(conn, tenant_id)
                with db.tx(conn):
                    merge(conn, tenant_id, src["id"], dst["id"], user, auto_reason=found[2])
                merged.add(src["id"])
                alerts[dst["id"]] = alerts[dst["id"]] + alerts.pop(src["id"])
                break
    return len(merged)


RELATED_DAYS = 30


def _labels(conn, incident_id: str) -> list[dict]:
    return [json.loads(r["labels"]) for r in conn.execute("SELECT labels FROM alert_event WHERE incident_id=?", (incident_id,)).fetchall()]


def _first_match(a: list[dict], b: list[dict], keys: list[str]) -> tuple | None:
    return next((m for x in a for y in b if (m := match(x, y, keys))), None)


def _absorbed(conn, incident_id: str) -> bool:
    return conn.execute("SELECT 1 FROM incident_event WHERE incident_id=? AND kind='merged'", (incident_id,)).fetchone() is not None


def related(conn, tenant_id: str, incident_id: str) -> dict:
    """Sự cố cùng nhóm (luật nhãn hoặc quan hệ k8s) trong RELATED_DAYS ngày: đã hết/đã đóng, và đang mở nên gộp vào đây.

    Bỏ sự cố đã gộp vào sự cố khác: cảnh báo của chúng đã nằm ở sự cố đích.
    """
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    keys = settings(conn, tenant_id)["group_labels"]
    mine = _labels(conn, incident_id)
    since = db.iso(db.now() - timedelta(days=RELATED_DAYS))
    history, open_ = [], []
    for r in conn.execute("SELECT id, title, status, created_at, resolved_at, repeats FROM incident WHERE tenant_id=? AND env=? AND id!=? AND created_at>=? "
                          "ORDER BY created_at DESC", (tenant_id, inc["env"], incident_id, since)).fetchall():
        if r["status"] == "closed" and _absorbed(conn, r["id"]):
            continue
        m = _first_match(mine, _labels(conn, r["id"]), keys)
        if m:
            (open_ if r["status"] == "open" else history).append({**dict(r), "reason": m[2]})
    groups: dict[str, dict] = {}
    for r in history:  # cảnh báo lặp lại tạo nhiều sự cố cùng tiêu đề: một dòng kèm số lần và khoảng ngày
        g = groups.setdefault(r["title"], {**r, "count": 0, "first": r["created_at"]})
        g["count"] += 1
        g["first"] = min(g["first"], r["created_at"])
    return {"days": RELATED_DAYS, "open": open_, "history": list(groups.values())[:10], "history_count": len(history)}


def suggestions(conn, tenant_id: str) -> list[dict]:
    """Cặp sự cố đang mở khớp nhau mà tự gộp đã bỏ qua (đang có việc dở, hoặc tự gộp tắt): gợi ý người gộp vào sự cố cũ hơn.

    Bỏ sự cố có cảnh báo người đã tách hay gộp tay: người đã quyết định rồi.
    """
    keys = settings(conn, tenant_id)["group_labels"]
    rows = conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' ORDER BY created_at", (tenant_id,)).fetchall()
    manual = {r["id"] for r in rows if conn.execute("SELECT 1 FROM alert_event WHERE incident_id=? AND join_method='manual'", (r["id"],)).fetchone()}
    labels = {r["id"]: _labels(conn, r["id"]) for r in rows}
    timed = {r["id"]: [(json.loads(a["labels"]), a["starts_at"]) for a in conn.execute("SELECT labels, starts_at FROM alert_event WHERE incident_id=?", (r["id"],))]
             for r in rows}
    cache: dict = {}
    out = []
    for j, src in enumerate(rows):
        if src["id"] in manual:
            continue
        for dst in rows[:j]:
            if dst["env"] != src["env"] or dst["id"] in manual:
                continue
            m = _first_match(labels[src["id"]], labels[dst["id"]], keys)
            if m is None:  # theo topology: cạnh gợi ý thì chỉ thành gợi ý ở đây, không tự gộp
                from .. import topology
                hit = next((x for a in timed[src["id"]] if (x := topology.infra_match(conn, tenant_id, a, timed[dst["id"]], cache))), None)
                m = ("infra", 0.85, hit[1]) if hit else None
            if m:
                out.append({"from": {"id": src["id"], "title": src["title"], "created_at": src["created_at"]},
                            "into": {"id": dst["id"], "title": dst["title"], "created_at": dst["created_at"]}, "reason": m[2]})
                break
    return out


def merged_from(conn, incident_id: str) -> list[dict]:
    out = []
    for e in conn.execute("SELECT at, kind, data FROM incident_event WHERE incident_id=? AND kind IN ('merge','auto_merge') ORDER BY id", (incident_id,)).fetchall():
        src = conn.execute("SELECT id, title, conversation_id, created_at FROM incident WHERE id=?", (json.loads(e["data"] or "{}").get("from"),)).fetchone()
        if src:
            out.append({**dict(src), "at": e["at"], "auto": e["kind"] == "auto_merge"})
    return out


def split(conn, tenant_id: str, incident_id: str, alert_id: str, user: str) -> str:
    a = conn.execute("SELECT * FROM alert_event WHERE id=? AND incident_id=? AND tenant_id=?", (alert_id, incident_id, tenant_id)).fetchone()
    inc = conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone()
    if a is None or inc is None:
        raise IncidentError("không tìm thấy cảnh báo trong sự cố", 404)
    if conn.execute("SELECT count(*) n FROM alert_event WHERE incident_id=?", (incident_id,)).fetchone()["n"] < 2:
        raise IncidentError("sự cố chỉ có một cảnh báo")
    new_id, now = db.new_id(), db.iso()
    conn.execute("INSERT INTO incident(id,tenant_id,env,title,severity,status,stage,created_at,updated_at,last_alert_at,investigate_after) "
                 "VALUES (?,?,?,?,?,?,'new',?,?,?,?)",
                 (new_id, tenant_id, a["env"], a["alertname"] or "Cảnh báo", a["severity"], "open" if a["status"] == "firing" else "resolved", now, now, now,
                  now if a["status"] == "firing" and settings(conn, tenant_id)["investigate"] else None))
    conn.execute("UPDATE alert_event SET incident_id=?, join_method='manual', join_score=1.0, join_reason='người tách ra' WHERE id=?", (new_id, alert_id))
    _event(conn, tenant_id, incident_id, "split", f"người dùng tách {a['alertname']} khỏi [{inc['title']}] ({a['join_method']}: {a['join_reason']}): không liên quan",
           {"alert_id": alert_id, "to": new_id, "by": user})
    _event(conn, tenant_id, new_id, "opened", f"tách từ sự cố {inc['title']}", {"from": incident_id, "by": user})
    _maybe_resolved(conn, tenant_id, incident_id)
    return new_id


def close(conn, tenant_id: str, incident_id: str, user: str, note: str = "") -> None:
    inc = conn.execute("SELECT status FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    conn.execute("UPDATE incident SET status='closed', closed_by=?, investigate_after=NULL, updated_at=? WHERE id=?", (user, db.iso(), incident_id))
    _event(conn, tenant_id, incident_id, "closed", "đóng sự cố" + (f": {note[:300]}" if note else ""), {"by": user}, actor=user)
