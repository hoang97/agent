"""Gom cảnh báo thành sự cố (luật nhãn, quan hệ k8s, rồi mới tới LLM), mở lại, gộp, tách, đóng.

Hai cách quyết định hai cảnh báo có chung sự cố không, theo cài đặt grouping.scoring của tenant:
- luật cũ (match): khớp nhãn group_labels, hoặc cùng node/workload; còn lại hỏi LLM;
- chấm điểm (scoring.py): cộng bằng chứng, topology k8s và lịch sử gộp/tách; LLM chỉ quyết phần lưng chừng.
grouping.batch gom các cảnh báo mới của cùng một vòng poll thành cụm trước khi khớp với sự cố. Xem docs/GROUPING-PLAN.md.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

from .. import db, live
from . import scoring, topology
from .common import SEVERITY, IncidentError, _event, _later, _method_label, _running, _worse
from .config import bot_user, settings
from .scoring import EXPORTER_JOBS, EXPORTER_LABELS, Evidence, _owner, target_labels  # noqa: F401  (tên cũ vẫn dùng qua incidents.*)


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
    out = {"alertname": row["alertname"], "severity": row["severity"], "summary": (row["summary"] or "")[:300], "since": row["starts_at"],
           "labels": {k: str(v)[:100] for k, v in labels.items() if k not in ("alertname", "severity")}}
    role = row["role"] if "role" in row.keys() else None
    if role:  # gợi ý theo topology cho điều tra và cho LLM gom, không phải kết luận
        out["role"], out["role_reason"] = role, row["role_reason"]
    return out


def _feedback(conn, tenant_id: str) -> list[str]:
    rs = conn.execute("SELECT text FROM incident_event WHERE tenant_id=? AND kind IN ('merge','split') ORDER BY id DESC LIMIT 10", (tenant_id,)).fetchall()
    return [r["text"] for r in rs]


# ---------- dữ liệu cho việc chấm điểm ----------

def _views(conn, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM alert_event WHERE {where} ORDER BY starts_at, rowid", params).fetchall()
    return [scoring.view({**dict(r), "labels": json.loads(r["labels"])}) for r in rows]


HISTORY_DAYS, BLOCK_DAYS, STATS_TTL = 90, 7, 600
_stats_cache: dict[str, tuple[float, scoring.PairStats]] = {}


def _keys(conn, incident_id: str) -> list[str]:
    return [scoring.view({"alertname": r["alertname"], "labels": json.loads(r["labels"])})["key"]
            for r in conn.execute("SELECT alertname, labels FROM alert_event WHERE incident_id=?", (incident_id,)).fetchall()]


def _splits(conn, tenant_id: str, since: str) -> list[tuple[str, list[str]]]:
    """Mỗi lần người tách: (khóa cảnh báo bị tách, khóa các cảnh báo còn lại ở sự cố nguồn)."""
    out = []
    for e in conn.execute("SELECT incident_id, data FROM incident_event WHERE tenant_id=? AND kind='split' AND at>=?", (tenant_id, since)).fetchall():
        a = conn.execute("SELECT alertname, labels FROM alert_event WHERE id=?", (json.loads(e["data"] or "{}").get("alert_id"),)).fetchone()
        if a:
            out.append((scoring.view({"alertname": a["alertname"], "labels": json.loads(a["labels"])})["key"], _keys(conn, e["incident_id"])))
    return out


def pair_stats(conn, tenant_id: str) -> scoring.PairStats:
    """Thống kê cặp từ sự cố đã xong trong 90 ngày (sự cố không ai sửa coi là gom đúng) và các lần người tách; giữ 10 phút."""
    hit = _stats_cache.get(tenant_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    since = db.iso(db.now() - timedelta(days=HISTORY_DAYS))
    groups: dict[str, list[str]] = {}
    for r in conn.execute("SELECT a.incident_id, a.alertname, a.labels FROM alert_event a JOIN incident i ON i.id=a.incident_id "
                          "WHERE i.tenant_id=? AND i.status IN ('resolved','closed') AND i.created_at>=? "
                          "AND NOT EXISTS (SELECT 1 FROM incident_event e WHERE e.incident_id=i.id AND e.kind='merged')", (tenant_id, since)).fetchall():
        groups.setdefault(r["incident_id"], []).append(scoring.view({"alertname": r["alertname"], "labels": json.loads(r["labels"])})["key"])
    stats = scoring.PairStats.build([g for g in groups.values() if len(g) > 1], _splits(conn, tenant_id, since))
    _stats_cache[tenant_id] = (time.monotonic() + STATS_TTL, stats)
    return stats


def blocked_pairs(conn, tenant_id: str) -> set[frozenset]:
    since = db.iso(db.now() - timedelta(days=BLOCK_DAYS))
    return {frozenset((key, other)) for key, others in _splits(conn, tenant_id, since) for other in others}


class Matcher:
    """Hai cảnh báo có chung sự cố không, theo cài đặt của tenant. Dùng chung cho gom, mở lại, tự gộp, gợi ý gộp và sự cố liên quan.

    join: từ điểm này thì gom; suggest: từ điểm này thì hỏi LLM (khi chấm điểm) và gợi ý người gộp. Luật cũ cho 0.9 hoặc 1.0 khi khớp.
    """

    def __init__(self, conn, tenant_id: str, cfg: dict):
        self.conn, self.tenant_id, self.cfg = conn, tenant_id, cfg
        self.g = cfg["grouping"]
        self.scoring = self.g["scoring"]
        self.join = self.g["join_threshold"] if self.scoring else 0.9
        self.suggest = self.g["suggest_threshold"] if self.scoring else 0.9
        self._topo: dict[str, scoring.Topology] = {}
        self._ctx: dict[tuple, scoring.Context] = {}
        self._stats = self._blocked = None

    def topo(self, env: str) -> scoring.Topology:
        if env not in self._topo:
            self._topo[env] = topology.load(self.conn, self.tenant_id, env) if self.g["topology_minutes"] else scoring.Topology()
        return self._topo[env]

    def context(self, env: str, timing: bool = True) -> scoring.Context:
        if (env, timing) not in self._ctx:
            if self._stats is None:
                self._stats, self._blocked = pair_stats(self.conn, self.tenant_id), blocked_pairs(self.conn, self.tenant_id)
            self._ctx[(env, timing)] = scoring.Context(self.topo(env), self.g["partition_labels"], self.g["burst_seconds"] if timing else 0,
                                                       self._stats, self._blocked)
        return self._ctx[(env, timing)]

    def pair(self, a: dict, b: dict, env: str, timing: bool = True) -> Evidence | None:
        if not self.scoring:
            m = match(a["labels"], b["labels"], self.cfg["group_labels"])
            return Evidence(m[1], m[0], m[2]) if m else None
        ev = scoring.pair_score(a, b, self.context(env, timing))
        return ev if ev.score > 0 else None

    def best(self, xs: list[dict], ys: list[dict], env: str, timing: bool = True) -> Evidence | None:
        return scoring.best(xs, ys, lambda a, b: self.pair(a, b, env, timing))

    def alerts_of(self, incident_id: str) -> list[dict]:
        return _views(self.conn, "incident_id=?", (incident_id,))


def apply_roles(conn, tenant_id: str, incident_id: str, topo: scoring.Topology) -> None:
    """Ghi vai trò gốc/triệu chứng theo topology. Tiêu đề còn là tiêu đề tự đặt theo một cảnh báo thì đổi theo cảnh báo gốc;
    tiêu đề do điều tra viết thì giữ."""
    if not topo:
        return
    items = _views(conn, "incident_id=?", (incident_id,))
    found = scoring.roles(items, topo)
    for a in items:
        role, why = found.get(a["id"], (None, None))
        if (a.get("role"), a.get("role_reason")) != (role, why):
            conn.execute("UPDATE alert_event SET role=?, role_reason=? WHERE id=?", (role, why, a["id"]))
    roots = [a for a in items if found.get(a["id"], ("",))[0] == "root"]
    if not roots:
        return
    root = min(roots, key=lambda a: (a["starts_at"] or "", -SEVERITY.get(a["severity"] or "", 0)))
    title = conn.execute("SELECT title FROM incident WHERE id=?", (incident_id,)).fetchone()["title"]
    wanted = alert_title(root["alertname"], root["labels"])
    if title != wanted and title in {alert_title(a["alertname"], a["labels"]) for a in items}:
        conn.execute("UPDATE incident SET title=? WHERE id=?", (wanted, incident_id))


def _roles_after(conn, tenant_id: str, incident_id: str, cfg: dict | None = None) -> None:
    cfg = cfg or settings(conn, tenant_id)
    if cfg["grouping"]["topology_minutes"]:
        env = conn.execute("SELECT env FROM incident WHERE id=?", (incident_id,)).fetchone()["env"]
        apply_roles(conn, tenant_id, incident_id, topology.load(conn, tenant_id, env))


def reapply_roles(conn, tenant_id: str, env: str) -> None:
    """Sau khi đọc lại topology: tính lại vai trò cho các sự cố đang mở của môi trường đó."""
    topo = topology.load(conn, tenant_id, env)
    with db.tx(conn):
        for r in conn.execute("SELECT id FROM incident WHERE tenant_id=? AND env=? AND status='open'", (tenant_id, env)).fetchall():
            apply_roles(conn, tenant_id, r["id"], topo)


def _below_min(severity: str | None, cfg: dict) -> bool:
    floor = cfg["grouping"]["open_min_severity"]
    return bool(floor) and SEVERITY.get((severity or "").lower(), 0) < SEVERITY[floor]


def _correlate(conn, tenant_id: str, alert: dict, incidents_: list[dict]) -> dict:
    """Một lần hỏi LLM; token ghi vào llm_call (mục đích correlate) để đo số lần gọi trên mỗi cảnh báo."""
    from .. import llm
    with llm.collect_usage(tenant_id) as usage:
        try:
            verdict = llm.get_provider().correlate(alert, incidents_, _feedback(conn, tenant_id))
        except llm.LLMError as exc:
            verdict = {"incident_id": None, "score": 0.0, "reason": f"lỗi LLM: {exc}"}
    if usage:
        with db.tx(conn):
            llm.save_usage(conn, tenant_id, None, usage)
    return verdict


def _candidates(conn, tenant_id: str, env: str, cfg: dict) -> list:
    since = (db.now() - timedelta(minutes=cfg["window_minutes"])).isoformat(timespec="seconds")
    # Khung window_minutes chỉ áp cho sự cố không còn cảnh báo đang bắn: sự cố kéo dài nhiều ngày vẫn nhận cảnh báo bắn lại.
    return conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' AND env=? AND (last_alert_at>=? OR EXISTS "
                        "(SELECT 1 FROM alert_event e WHERE e.incident_id=incident.id AND e.status='firing')) ORDER BY last_alert_at DESC",
                        (tenant_id, env, since)).fetchall()


def _recently_resolved(conn, tenant_id: str, env: str, cfg: dict) -> list:
    return conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='resolved' AND env=? AND resolved_at>=? ORDER BY resolved_at DESC",
                        (tenant_id, env, db.iso(db.now() - timedelta(hours=cfg["reopen_hours"])))).fetchall()


def _ai_incidents(conn, pool: list, evidence: dict | None = None) -> list[dict]:
    out = []
    for i in pool[:5]:
        item = {"id": i["id"], "title": i["title"], "alerts": [_brief(r) for r in conn.execute(
            "SELECT * FROM alert_event WHERE incident_id=? ORDER BY starts_at LIMIT 8", (i["id"],)).fetchall()]}
        if evidence and evidence.get(i["id"]):
            item["evidence"] = evidence[i["id"]].reason  # bằng chứng kagent đã thấy, để LLM quyết phần còn thiếu
        out.append(item)
    return out


def assign(conn, tenant_id: str, alert_id: str, cfg: dict | None = None) -> str | None:
    """Gom một cảnh báo mới vào sự cố đang mở phù hợp nhất, hoặc mở sự cố mới (luật cũ). Trả về id sự cố,
    None khi cảnh báo dưới mức mở sự cố mà không khớp sự cố nào."""
    cfg = cfg or settings(conn, tenant_id)
    a = conn.execute("SELECT * FROM alert_event WHERE id=?", (alert_id,)).fetchone()
    labels = json.loads(a["labels"])
    candidates = _candidates(conn, tenant_id, a["env"], cfg)
    found = None
    for inc in candidates:
        for other in conn.execute("SELECT labels FROM alert_event WHERE incident_id=?", (inc["id"],)).fetchall():
            m = match(labels, json.loads(other["labels"]), cfg["group_labels"])
            if m and (found is None or m[1] > found[1][1]):
                found = (inc["id"], m)
        if found and found[1][0] == "rule":
            break
    if found is None and cfg["ai_grouping"] and candidates:
        verdict = _correlate(conn, tenant_id, _brief(a), _ai_incidents(conn, candidates))
        if verdict["incident_id"] and verdict["score"] >= cfg["ai_min_score"]:
            found = (verdict["incident_id"], ("ai", verdict["score"], verdict["reason"] or "LLM đánh giá liên quan"))
    reopen = False
    if found is None and cfg["reopen_hours"]:
        found = _rule_match(conn, labels, cfg, _recently_resolved(conn, tenant_id, a["env"], cfg))
        reopen = found is not None
    with db.tx(conn):
        incident_id = _attach(conn, tenant_id, a, labels, found, cfg, reopen)
        if incident_id:
            _roles_after(conn, tenant_id, incident_id, cfg)
    return incident_id


def group_new(conn, tenant_id: str, alert_ids: list[str], cfg: dict) -> None:
    """Gom các cảnh báo mới của một vòng poll. Không bật gom theo lô hay chấm điểm thì như cũ: từng cảnh báo theo giờ bắn."""
    if not alert_ids:
        return
    g = cfg["grouping"]
    marks = ",".join("?" * len(alert_ids))
    if not (g["batch"] or g["scoring"]):
        for r in conn.execute(f"SELECT id FROM alert_event WHERE id IN ({marks}) ORDER BY starts_at, rowid", tuple(alert_ids)).fetchall():
            assign(conn, tenant_id, r["id"], cfg)
        return
    m = Matcher(conn, tenant_id, cfg)
    items = _views(conn, f"id IN ({marks})", tuple(alert_ids))
    budget = [g["ai_max_calls"]]
    for env in dict.fromkeys(i["env"] for i in items):
        batch = [i for i in items if i["env"] == env]
        groups = scoring.cluster(batch, lambda x, y: m.pair(x, y, env), m.join) if g["batch"] else [[i] for i in batch]
        for group in groups:
            _place(conn, tenant_id, group, env, cfg, m, budget)


def _place(conn, tenant_id: str, group: list[dict], env: str, cfg: dict, m: Matcher, budget: list[int]) -> str | None:
    """Một cụm cảnh báo mới: vào sự cố đang mở khớp nhất; điểm lưng chừng thì hỏi LLM một lần cho cả cụm; rồi mới tới mở lại hay mở mới."""
    candidates = _candidates(conn, tenant_id, env, cfg)
    scored = [(inc, ev) for inc in candidates if (ev := m.best(group, m.alerts_of(inc["id"]), env))]
    scored.sort(key=lambda x: -x[1].score)  # sort ổn định: hòa điểm thì sự cố có cảnh báo gần nhất đứng trước
    found, reopen = None, False
    if scored and scored[0][1].score >= m.join:
        found = (scored[0][0]["id"], scored[0][1])
    elif cfg["ai_grouping"] and budget[0] > 0:
        # Luật cũ: luật không khớp thì hỏi LLM. Chấm điểm: chỉ hỏi khi có sự cố đạt ngưỡng gợi ý, LLM quyết phần còn thiếu.
        pool = [inc for inc, ev in scored if ev.score >= m.suggest] if m.scoring else list(candidates)
        if pool:
            budget[0] -= 1
            brief = _brief(group[0]) if len(group) == 1 else {"alerts": [_brief(x) for x in group[:10]], "count": len(group)}
            verdict = _correlate(conn, tenant_id, brief, _ai_incidents(conn, pool, {inc["id"]: ev for inc, ev in scored}))
            if verdict["incident_id"] and verdict["score"] >= cfg["ai_min_score"]:
                found = (verdict["incident_id"], Evidence(verdict["score"], "ai", verdict["reason"] or "LLM đánh giá liên quan"))
    if found is None and cfg["reopen_hours"]:
        for inc in _recently_resolved(conn, tenant_id, env, cfg):
            ev = m.best(group, m.alerts_of(inc["id"]), env)
            if ev and ev.score >= m.join:
                found, reopen = (inc["id"], ev), True
                break
    with db.tx(conn):
        return _attach_group(conn, tenant_id, group, found, cfg, reopen, m, env)


def _attach_group(conn, tenant_id: str, group: list[dict], found, cfg: dict, reopen: bool, m: Matcher, env: str) -> str | None:
    """Ghi cả cụm vào sự cố. Mở mới thì cảnh báo gốc (theo topology, không có thì cảnh báo bắn sớm nhất) mở sự cố.
    Mỗi cảnh báo ghi bằng chứng mạnh nhất của chính nó: với cảnh báo đã có trong sự cố, hoặc với cảnh báo cùng cụm."""
    placed: list[dict] = []
    if found is None:
        openable = [a for a in group if not _below_min(a["severity"], cfg)]
        if not openable:
            for a in group:
                _skip_low(conn, a, cfg)
            return None
        found_roles = scoring.roles(group, m.topo(env))
        root = next((a for a in openable if found_roles.get(a["id"], ("",))[0] == "root"), openable[0])
        incident_id = _attach(conn, tenant_id, root, root["labels"], None, cfg)
        placed.append(root)
    else:
        incident_id = found[0]
    pool = [x for x in m.alerts_of(incident_id) if x["id"] not in {a["id"] for a in group}] + placed
    first, done = True, {a["id"] for a in placed}
    for a in group:
        if a["id"] in done:
            continue
        if found and found[1].method == "ai":
            ev = found[1]
        else:
            ev = m.best([a], pool, env)
            mate = m.best([a], [x for x in group if x["id"] != a["id"]], env)
            if mate and (ev is None or mate.score > ev.score):
                ev = Evidence(mate.score, mate.method, "cùng cụm cảnh báo mới · " + mate.reason)
            if ev is None:
                ev = found[1] if found else Evidence(m.join, "score", "cùng cụm cảnh báo mới")
        _attach(conn, tenant_id, a, a["labels"], (incident_id, (ev.method, ev.score, ev.reason)), cfg, reopen and first)
        first = False
        pool.append(a)
    apply_roles(conn, tenant_id, incident_id, m.topo(env))
    return incident_id


def _skip_low(conn, a, cfg: dict) -> None:
    conn.execute("UPDATE alert_event SET join_method=NULL, join_reason=? WHERE id=?",
                 (f"mức {a['severity'] or 'không có'} dưới ngưỡng mở sự cố ({cfg['grouping']['open_min_severity']})", a["id"]))


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


def _attach(conn, tenant_id: str, a, labels: dict, found, cfg: dict, reopen: bool = False) -> str | None:
    """Ghi một cảnh báo vào sự cố found, hoặc mở sự cố mới. Không khớp mà dưới mức mở sự cố thì để ngoài, trả None.
    Cảnh báo chập chờn (bắn rồi hết liên tục) vẫn vào sự cố nhưng không làm điều tra chạy lại."""
    alert_id = a["id"]
    now = db.iso()
    flapping = bool(a["flapping"]) if "flapping" in a.keys() else False
    if found:
        incident_id, (method, score, reason) = found
        if reopen:
            conn.execute("UPDATE incident SET status='open', resolved_at=NULL, stage=CASE WHEN ? OR stage='investigating' THEN stage ELSE 'new' END, "
                         "verify_due=NULL, repeats=repeats+1 WHERE id=?", (int(flapping), incident_id))
            _event(conn, tenant_id, incident_id, "reopened", f"mở lại vì {a['alertname']} bắn lại ({_method_label(method)}: {reason})"
                   + (" · chập chờn, không điều tra lại" if flapping else ""), {"alert_id": alert_id})
        inc = conn.execute("SELECT * FROM incident WHERE id=?", (incident_id,)).fetchone()
        rerun = inc["stage"] not in ("investigating", "awaiting_approval", "verifying") and cfg["investigate"] and not flapping
        conn.execute("UPDATE incident SET last_alert_at=?, updated_at=?, severity=?, investigate_after=CASE WHEN ? THEN ? ELSE investigate_after END WHERE id=?",
                     (now, now, _worse(inc["severity"], a["severity"]), int(rerun), _later(cfg["debounce_seconds"]), incident_id))
        conn.execute("UPDATE alert_event SET incident_id=?, join_method=?, join_score=?, join_reason=? WHERE id=?", (incident_id, method, score, reason, alert_id))
        _event(conn, tenant_id, incident_id, "alert_joined", f"gộp cảnh báo {a['alertname']} ({_method_label(method)}: {reason})"
               + (" · chập chờn" if flapping else ""), {"alert_id": alert_id, "method": method, "score": score})
        return incident_id
    if _below_min(a["severity"], cfg):
        _skip_low(conn, a, cfg)
        return None
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
    _roles_after(conn, tenant_id, into, cfg)
    _stats_cache.pop(tenant_id, None)


def auto_merge(conn, tenant_id: str, cfg: dict | None = None) -> int:
    """Gộp các sự cố đang mở khớp nhau (luật nhãn hoặc quan hệ k8s; khi chấm điểm là đạt ngưỡng gom) vào sự cố cũ nhất.
    Trả về số sự cố đã gộp. Khớp theo AI thì không tự gộp.

    Bỏ qua sự cố đang có việc dở (điều tra, chờ duyệt, xác minh, lượt chat đang chạy) và sự cố người đã tách hay gộp tay,
    để không làm ngược quyết định của người.
    """
    cfg = cfg or settings(conn, tenant_id)
    m = Matcher(conn, tenant_id, cfg)
    rows = conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' ORDER BY created_at", (tenant_id,)).fetchall()
    alerts = {r["id"]: m.alerts_of(r["id"]) for r in rows}
    merged, user = set(), None
    for j, src in enumerate(rows):
        if (src["id"] in _running or src["stage"] in ("investigating", "awaiting_approval", "verifying")
                or (src["conversation_id"] and live.current(src["conversation_id"]))
                or any(a["join_method"] == "manual" for a in alerts[src["id"]])):
            continue
        for dst in rows[:j]:
            if dst["id"] in merged or dst["env"] != src["env"]:
                continue
            found = m.best(alerts[src["id"]], alerts[dst["id"]], src["env"])
            if found and found.score >= m.join:
                user = user or bot_user(conn, tenant_id)
                with db.tx(conn):
                    merge(conn, tenant_id, src["id"], dst["id"], user, auto_reason=found.reason)
                merged.add(src["id"])
                alerts[dst["id"]] = alerts[dst["id"]] + alerts.pop(src["id"])
                break
    return len(merged)


RELATED_DAYS = 30


def _absorbed(conn, incident_id: str) -> bool:
    return conn.execute("SELECT 1 FROM incident_event WHERE incident_id=? AND kind='merged'", (incident_id,)).fetchone() is not None


def related(conn, tenant_id: str, incident_id: str) -> dict:
    """Sự cố cùng nhóm trong RELATED_DAYS ngày: đã hết/đã đóng, và đang mở nên gộp vào đây. Cùng nhóm là khớp luật nhãn
    hoặc quan hệ k8s; khi chấm điểm là đạt ngưỡng gợi ý, không tính điểm cùng đợt.

    Bỏ sự cố đã gộp vào sự cố khác: cảnh báo của chúng đã nằm ở sự cố đích.
    """
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    m = Matcher(conn, tenant_id, settings(conn, tenant_id))
    mine = m.alerts_of(incident_id)
    since = db.iso(db.now() - timedelta(days=RELATED_DAYS))
    history, open_ = [], []
    for r in conn.execute("SELECT id, title, status, created_at, resolved_at, repeats FROM incident WHERE tenant_id=? AND env=? AND id!=? AND created_at>=? "
                          "ORDER BY created_at DESC", (tenant_id, inc["env"], incident_id, since)).fetchall():
        if r["status"] == "closed" and _absorbed(conn, r["id"]):
            continue
        found = m.best(mine, m.alerts_of(r["id"]), inc["env"], timing=False)
        if found and found.score >= m.suggest:
            (open_ if r["status"] == "open" else history).append({**dict(r), "reason": found.reason})
    groups: dict[str, dict] = {}
    for r in history:  # cảnh báo lặp lại tạo nhiều sự cố cùng tiêu đề: một dòng kèm số lần và khoảng ngày
        g = groups.setdefault(r["title"], {**r, "count": 0, "first": r["created_at"]})
        g["count"] += 1
        g["first"] = min(g["first"], r["created_at"])
    return {"days": RELATED_DAYS, "open": open_, "history": list(groups.values())[:10], "history_count": len(history)}


def suggestions(conn, tenant_id: str) -> list[dict]:
    """Cặp sự cố đang mở khớp nhau mà tự gộp đã bỏ qua (đang có việc dở, tự gộp tắt, hoặc khi chấm điểm là điểm lưng chừng):
    gợi ý người gộp vào sự cố cũ hơn.

    Bỏ sự cố có cảnh báo người đã tách hay gộp tay: người đã quyết định rồi.
    """
    m = Matcher(conn, tenant_id, settings(conn, tenant_id))
    rows = conn.execute("SELECT * FROM incident WHERE tenant_id=? AND status='open' ORDER BY created_at", (tenant_id,)).fetchall()
    alerts = {r["id"]: m.alerts_of(r["id"]) for r in rows}
    manual = {r["id"] for r in rows if any(a["join_method"] == "manual" for a in alerts[r["id"]])}
    out = []
    for j, src in enumerate(rows):
        if src["id"] in manual:
            continue
        for dst in rows[:j]:
            if dst["env"] != src["env"] or dst["id"] in manual:
                continue
            found = m.best(alerts[src["id"]], alerts[dst["id"]], src["env"])
            if found and found.score >= m.suggest:
                out.append({"from": {"id": src["id"], "title": src["title"], "created_at": src["created_at"]},
                            "into": {"id": dst["id"], "title": dst["title"], "created_at": dst["created_at"]}, "reason": found.reason})
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
    for iid in (incident_id, new_id):
        _roles_after(conn, tenant_id, iid)
    _stats_cache.pop(tenant_id, None)
    return new_id


def close(conn, tenant_id: str, incident_id: str, user: str, note: str = "") -> None:
    inc = conn.execute("SELECT status FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise IncidentError("không tìm thấy sự cố", 404)
    conn.execute("UPDATE incident SET status='closed', closed_by=?, investigate_after=NULL, updated_at=? WHERE id=?", (user, db.iso(), incident_id))
    _event(conn, tenant_id, incident_id, "closed", "đóng sự cố" + (f": {note[:300]}" if note else ""), {"by": user}, actor=user)
