"""Ảnh chụp từng lượt điều tra của sự cố cho panel: kết luận, entity liên quan, tín hiệu, topology con.

Entity lấy bằng code từ nhãn cảnh báo và tham số các lệnh đọc trong trace, không nhờ model: tên luôn đúng định dạng và có thật trong
dữ liệu đã đọc. Dạng chuẩn `namespace/kind/name` (node: `-/node/<tên>`). Tín hiệu và topology chưa có bộ sinh nên ghi rỗng; panel
ẩn các khối đó khi rỗng.
"""
from __future__ import annotations

import json
import re
import shlex

KINDS = {
    "po": "pod", "pod": "pod", "pods": "pod",
    "deploy": "deployment", "deployment": "deployment", "deployments": "deployment",
    "sts": "statefulset", "statefulset": "statefulset", "statefulsets": "statefulset",
    "ds": "daemonset", "daemonset": "daemonset", "daemonsets": "daemonset",
    "rs": "replicaset", "replicaset": "replicaset", "replicasets": "replicaset",
    "svc": "service", "service": "service", "services": "service",
    "no": "node", "node": "node", "nodes": "node",
    "pvc": "pvc", "persistentvolumeclaim": "pvc", "persistentvolumeclaims": "pvc",
    "job": "job", "jobs": "job", "cronjob": "cronjob", "cronjobs": "cronjob", "cj": "cronjob",
    "ing": "ingress", "ingress": "ingress", "ingresses": "ingress",
    "cm": "configmap", "configmap": "configmap", "configmaps": "configmap",
}
WORKLOADS = ("deployment", "statefulset", "daemonset")
# Thứ tự ưu tiên khi nhiều entity cùng tên: chọn cái nói về workload trước.
RANK = {"deployment": 0, "statefulset": 0, "daemonset": 0, "service": 1, "pod": 2, "node": 3}

# Nhãn cảnh báo và tham số công cụ (kể cả bộ công cụ benchmark) trỏ tới đối tượng nào.
LABEL_KINDS = {"pod": "pod", "pod_name": "pod", "deployment": "deployment", "statefulset": "statefulset", "daemonset": "daemonset",
               "service": "service", "service_name": "service", "app_name": "deployment", "node": "node", "node_name": "node",
               "instance_node": "node", "persistentvolumeclaim": "pvc", "job_name": "job"}
NS_KEYS = ("namespace", "exported_namespace", "kubernetes_namespace", "kubernetes.namespace_name", "kubernetes.namespace")
RESOURCE_TYPE_KEYS = ("resource_type", "kind")

_POD_HASH = re.compile(r"^(?P<base>.+?)-(?P<rs>[a-z0-9]{6,10})-(?P<pod>[a-z0-9]{5})$")
_STS_POD = re.compile(r"^(?P<base>.+)-\d+$")
_MATCHER = re.compile(r'([a-zA-Z_][a-zA-Z0-9_.]*)\s*=~?\s*"([^"]*)"')
_ES_TERM = re.compile(r'(kubernetes\.(?:namespace_name|pod_name|container_name|labels\.app)|namespace|pod|service)\s*:\s*"?([a-z0-9][a-z0-9.\-]*)"?')
_NAME = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$")


def eid(ns: str | None, kind: str, name: str) -> str:
    return f"{'-' if kind == 'node' else (ns or '?')}/{kind}/{name}"


def _owner_guess(pod: str) -> tuple[str, str] | None:
    """Đoán workload sở hữu pod theo tên (deployment: <tên>-<hash rs>-<5 ký tự>; statefulset: <tên>-<số>). Chưa đọc ownerReferences."""
    m = _POD_HASH.match(pod)
    if m:
        return "deployment", m["base"]
    m = _STS_POD.match(pod)
    if m:
        return "statefulset", m["base"]
    return None


class _Bag:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, ns: str | None, kind: str, name: str, source: str) -> None:
        name = str(name or "").strip().lower()
        if not name or not _NAME.match(name) or kind not in RANK and kind not in KINDS.values():
            return
        if kind != "node" and not ns:
            ns = None
        key = eid(ns, kind, name)
        e = self.items.setdefault(key, {"id": key, "kind": kind, "name": name, "namespace": None if kind == "node" else ns, "from": [], "suspect": False})
        if source not in e["from"]:
            e["from"].append(source)
        if kind == "pod":
            owner = _owner_guess(name)
            if owner:
                self.add(ns, owner[0], owner[1], source)

    def fill_namespaces(self) -> None:
        """Entity thiếu namespace: gán namespace duy nhất đã thấy, nếu chỉ có một; trùng với entity đã có namespace thì gộp."""
        seen = {e["namespace"] for e in self.items.values() if e["namespace"] and e["namespace"] != "?"}
        if len(seen) != 1:
            return
        ns = seen.pop()
        for key in [k for k, e in self.items.items() if e["kind"] != "node" and not e["namespace"]]:
            e = self.items.pop(key)
            fixed = eid(ns, e["kind"], e["name"])
            if fixed in self.items:
                self.items[fixed]["from"] = sorted(set(self.items[fixed]["from"]) | set(e["from"]))
            else:
                self.items[fixed] = {**e, "id": fixed, "namespace": ns}


def _from_mapping(bag: _Bag, d: dict, source: str) -> None:
    ns = next((str(d[k]) for k in NS_KEYS if d.get(k)), None)
    rtype = next((KINDS.get(str(d[k]).lower()) for k in RESOURCE_TYPE_KEYS if d.get(k)), None)
    for k, kind in LABEL_KINDS.items():
        if k == "service_name" and (d.get("node_name") or d.get("node")):
            continue  # dịch vụ hệ thống trên node (kubelet, containerd), không phải Service của K8s
        if d.get(k) and isinstance(d[k], str):
            bag.add(ns, kind, d[k], source)
    if rtype and isinstance(d.get("name"), str):
        bag.add(ns, rtype, d["name"], source)


def _from_kubectl(bag: _Bag, command: str, source: str) -> None:
    try:
        argv = shlex.split(command.split("|", 1)[0])
    except ValueError:
        return
    ns, pos, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("-n", "--namespace") and i + 1 < len(argv):
            ns, i = argv[i + 1], i + 2
            continue
        if a.startswith("--namespace="):
            ns = a.split("=", 1)[1]
        elif a.startswith("-"):
            if a in ("-l", "--selector", "-o", "--output", "-c", "--container", "--tail", "--since", "-f", "--field-selector") and i + 1 < len(argv):
                i += 1
        else:
            pos.append(a)
        i += 1
    if not pos:
        return
    verb, rest = pos[0], pos[1:]
    if verb == "logs" and rest:
        target = rest[0]
        kind, name = (target.split("/", 1) if "/" in target else ("pod", target))
        bag.add(ns, KINDS.get(kind.lower(), "pod"), name, source)
        return
    if verb in ("rollout",) and len(rest) >= 2:
        rest = rest[1:]
    if verb not in ("get", "describe", "rollout", "top", "events"):
        return
    if not rest:
        return
    first = rest[0]
    if "/" in first:
        for item in rest:
            if "/" in item:
                kind, name = item.split("/", 1)
                if KINDS.get(kind.lower()):
                    bag.add(ns, KINDS[kind.lower()], name, source)
        return
    kinds = [KINDS.get(k.lower()) for k in first.split(",")]
    if len(kinds) == 1 and kinds[0]:
        for name in rest[1:]:
            bag.add(ns, kinds[0], name, source)


def _from_text_query(bag: _Bag, q: str, source: str) -> None:
    labels = {k: v for k, v in _MATCHER.findall(q) if v and not any(c in v for c in "|*+[]()")}
    if labels:
        _from_mapping(bag, labels, source)
    terms: dict[str, str] = {}
    for k, v in _ES_TERM.findall(q):
        key = {"kubernetes.namespace_name": "namespace", "kubernetes.pod_name": "pod", "kubernetes.labels.app": "app_name",
               "kubernetes.container_name": None}.get(k, k)
        if key:
            terms.setdefault(key, v)
    if terms:
        _from_mapping(bag, terms, source)


def extract_entities(alerts: list[dict], steps: list[dict], root_cause: str = "") -> list[dict]:
    """Entity của lượt điều tra từ nhãn cảnh báo và tham số lệnh đọc; đánh dấu `suspect` khi nguyên nhân nhắc đúng một tên."""
    bag = _Bag()
    for a in alerts or []:
        labels = a.get("labels") or {}
        if isinstance(labels, str):
            try:
                labels = json.loads(labels)
            except ValueError:
                labels = {}
        _from_mapping(bag, labels, "alert")
    for s in steps or []:
        args = s.get("args") or {}
        if not isinstance(args, dict):
            continue
        tool = str(s.get("tool") or "").split(".")[-1]
        if isinstance(args.get("command"), str) and tool.startswith("kubectl"):
            _from_kubectl(bag, args["command"], "trace")
        for key in ("query", "expr", "q"):
            if isinstance(args.get(key), str):
                _from_text_query(bag, args[key], "trace")
        _from_mapping(bag, args, "trace")
    bag.fill_namespaces()
    items = sorted(bag.items.values(), key=lambda e: (RANK.get(e["kind"], 9), e["id"]))
    mark_suspect(items, root_cause)
    return items


def mark_suspect(items: list[dict], text: str) -> None:
    """Đánh dấu entity mà nguyên nhân nhắc tới. Chỉ khi các tên khớp quy về đúng một tên (bỏ tên là chuỗi con của tên khớp dài hơn);
    nhiều tên khác nhau thì không đánh dấu: không đánh dấu còn hơn đánh dấu sai."""
    text = (text or "").lower()
    if not text:
        return
    names = {e["name"] for e in items}
    hit = {n for n in names if re.search(rf"(?<![a-z0-9\-]){re.escape(n)}(?![a-z0-9\-])", text)}
    hit = {n for n in hit if not any(n != m and n in m for m in hit)}
    if len(hit) != 1:
        return
    name = hit.pop()
    same = sorted((e for e in items if e["name"] == name), key=lambda e: RANK.get(e["kind"], 9))
    best = RANK.get(same[0]["kind"], 9)
    for e in same:
        if RANK.get(e["kind"], 9) == best:
            e["suspect"] = True


def record(conn, tenant_id: str, incident_id: str, round_no: int, episode_id: str | None, report: dict, alerts: list[dict], steps: list[dict]) -> str:
    """Ghi ảnh chụp của một lượt; gọi trong transaction của lượt điều tra. Ghi lại cùng lượt thì thay (điều tra lại cùng số lượt không xảy ra,
    nhưng không để lỗi UNIQUE làm hỏng lượt)."""
    from .. import db
    entities = extract_entities(alerts, steps, report.get("root_cause") or "")
    rid = db.new_id()
    conn.execute("INSERT OR REPLACE INTO incident_round(id,tenant_id,incident_id,round,episode_id,created_at,steps,report,entities,signals,graph) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, tenant_id, incident_id, round_no, episode_id, db.iso(), len(steps or []),
                  json.dumps({k: report.get(k) for k in ("symptom", "root_cause", "confidence", "evidence", "hypotheses") if k in report}, ensure_ascii=False),
                  json.dumps(entities, ensure_ascii=False), "[]", "{}"))
    return rid


def _row(r) -> dict:
    d = dict(r)
    for k in ("report", "entities", "signals", "graph"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def list_rounds(conn, incident_id: str) -> list[dict]:
    return [{"round": r["round"], "created_at": r["created_at"], "steps": r["steps"]}
            for r in conn.execute("SELECT round, created_at, steps FROM incident_round WHERE incident_id=? ORDER BY round", (incident_id,))]


def get_round(conn, incident_id: str, round_no: int | None = None) -> dict | None:
    if round_no is None:
        r = conn.execute("SELECT * FROM incident_round WHERE incident_id=? ORDER BY round DESC LIMIT 1", (incident_id,)).fetchone()
    else:
        r = conn.execute("SELECT * FROM incident_round WHERE incident_id=? AND round=?", (incident_id, round_no)).fetchone()
    return _row(r) if r else None
