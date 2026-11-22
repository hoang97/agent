"""Chấm điểm hai cảnh báo có chung một sự cố không, bằng cách cộng bằng chứng.

Bằng chứng cấu trúc lấy điểm cao nhất (cùng cảnh báo bắn lại, cùng workload, phụ thuộc trực tiếp, cùng node, cùng namespace),
rồi cộng thêm bằng chứng phụ (cùng đợt bắn, lịch sử gộp/tách). Nhãn phân vùng khác nhau hay cặp người vừa tách thì điểm 0.
Xem docs/GROUPING-PLAN.md.

Module này chỉ xử lý dữ liệu, không đọc DB: grouping.py nạp topology và thống kê rồi gọi vào đây, scripts/grouping_replay.py
chạy lại y nguyên trên dữ liệu cũ.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, NamedTuple

SAME_ALERT, SAME_WORKLOAD, DEPENDENCY, SAME_NODE, SAME_NAMESPACE = 1.0, 0.9, 0.8, 0.7, 0.4
BURST_BONUS, HISTORY_BONUS = 0.2, 0.3
HISTORY_MIN = 3  # số lần quan sát tối thiểu của một cặp trước khi tin thống kê
STRUCTURAL = 0.7  # từ mức này trở lên là bằng chứng topology (cùng workload, phụ thuộc, cùng node)


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


def refs(labels: dict) -> list[str]:
    """Đối tượng k8s mà cảnh báo nói tới, dạng "namespace/loại/tên"; loại là workload (deploy/sts/ds, pod quy về workload) hoặc svc."""
    ns = str(labels.get("namespace") or "")
    out = []
    for key in ("deployment", "statefulset", "daemonset"):
        if labels.get(key):
            out.append(f"{ns}/workload/{labels[key]}")
    owner = _owner(labels.get("pod"))
    if owner:
        out.append(f"{ns}/workload/{owner}")
    if labels.get("service"):
        out.append(f"{ns}/svc/{labels['service']}")
    return list(dict.fromkeys(out))


def ref_name(ref: str) -> str:
    ns, kind, name = ref.split("/", 2)
    return {"svc": "Service ", "ingress": "ingress "}.get(kind, "") + name


_TS = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")


def parse_ts(value: str | None) -> datetime | None:
    """Giờ của Alertmanager/Prometheus (có thể có phần lẻ tới nano giây và hậu tố Z) hoặc giờ ISO của kagent."""
    m = _TS.match(value or "")
    if not m:
        return None
    frac = (m.group(2) or "")[:6].ljust(6, "0")
    zone = m.group(3) or "+00:00"
    try:
        return datetime.fromisoformat(f"{m.group(1)}.{frac}{'+00:00' if zone == 'Z' else zone}").astimezone(timezone.utc)
    except ValueError:
        return None


def view(row: dict) -> dict:
    """Cảnh báo đã tính sẵn những gì cần để chấm điểm. row: id, alertname, labels (dict), starts_at, fingerprint, severity."""
    labels = row["labels"]
    target = target_labels(labels)
    rs = refs(target)
    kind = ("workload" if any("/workload/" in r for r in rs) else "svc" if rs else "node" if target.get("node")
            else "ns" if target.get("namespace") else "-")
    return {**row, "target": target, "refs": rs, "key": f"{row.get('alertname') or '?'}/{kind}", "ts": parse_ts(row.get("starts_at"))}


class Evidence(NamedTuple):
    score: float
    method: str | None  # rule (cùng cảnh báo) | topology (bằng chứng cấu trúc) | score (cộng các bằng chứng yếu) | ai | None
    reason: str


class Topology:
    """Đồ thị phụ thuộc k8s: Service và workload nó chọn gộp thành một đơn vị; cạnh gọi/trỏ tới nối các đơn vị.

    edges: (nguồn, đích, loại) với loại selects (Service → workload), routes (ingress → Service), calls (workload → Service).
    """

    VERB = {"calls": "gọi", "routes": "trỏ tới"}

    def __init__(self, edges=()):
        self.edges = [tuple(e) for e in edges]
        self._parent: dict[str, str] = {}
        for src, dst, kind in self.edges:
            if kind == "selects":
                a, b = self.unit(src), self.unit(dst)
                if a != b:
                    lo, hi = sorted((a, b))
                    self._parent[hi] = lo
        self.callees: dict[str, dict[str, str]] = {}
        for src, dst, kind in self.edges:
            if kind in self.VERB:
                a, b = self.unit(src), self.unit(dst)
                if a != b:
                    self.callees.setdefault(a, {}).setdefault(b, kind)

    def __bool__(self) -> bool:
        return bool(self.edges)

    def unit(self, ref: str) -> str:
        root = ref
        while self._parent.get(root, root) != root:
            root = self._parent[root]
        while self._parent.get(ref, ref) != root:  # nén đường đi
            self._parent[ref], ref = root, self._parent[ref]
        return root

    def units(self, rs: list[str]) -> set[str]:
        return {self.unit(r) for r in rs}

    def same_unit(self, ra: list[str], rb: list[str]) -> tuple[str, str] | None:
        return next(((x, y) for x in ra for y in rb if x != y and self.unit(x) == self.unit(y)), None)

    def link(self, ra: list[str], rb: list[str]) -> tuple[str, str, str] | None:
        """Cạnh trực tiếp giữa hai cảnh báo: (bên gọi, bên bị gọi, loại)."""
        for x in ra:
            for y in rb:
                ux, uy = self.unit(x), self.unit(y)
                if uy in self.callees.get(ux, {}):
                    return x, y, self.callees[ux][uy]
                if ux in self.callees.get(uy, {}):
                    return y, x, self.callees[uy][ux]
        return None

    def reaches(self, src: set[str], dst: set[str]) -> bool:
        """Từ đơn vị src đi theo chiều gọi có tới được đơn vị dst không (dst là phụ thuộc của src). Cùng đơn vị không tính."""
        if not src or not dst or src & dst:
            return False
        seen, todo = set(src), deque(src)
        while todo:
            for nxt in self.callees.get(todo.popleft(), {}):
                if nxt in dst:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return False


class PairStats:
    """Số lần hai loại cảnh báo (khóa alertname/loại đối tượng) nằm chung sự cố đã xong, và số lần người tách chúng ra."""

    def __init__(self, counts: dict | None = None):
        self.counts: dict[frozenset, list[int]] = counts or {}

    @classmethod
    def build(cls, groups: list[list[str]], splits: list[tuple[str, list[str]]], max_keys: int = 30) -> "PairStats":
        counts: dict[frozenset, list[int]] = {}
        for keys in groups:
            uniq = sorted(set(keys))
            if len(uniq) > max_keys:  # bão cảnh báo không cho biết cặp nào thật sự đi cùng nhau
                continue
            for i, a in enumerate(uniq):
                if keys.count(a) > 1:
                    counts.setdefault(frozenset((a,)), [0, 0])[0] += 1
                for b in uniq[i + 1:]:
                    counts.setdefault(frozenset((a, b)), [0, 0])[0] += 1
        for key, others in splits:
            for other in set(others):
                counts.setdefault(frozenset((key, other)), [0, 0])[1] += 1
        return cls(counts)

    def bonus(self, a: str, b: str) -> tuple[float, str]:
        together, split = self.counts.get(frozenset((a, b)), (0, 0))
        n = together + split
        if n < HISTORY_MIN or together / n <= 0.5:
            return 0.0, ""
        return round(HISTORY_BONUS * (together / n - 0.5) / 0.5, 2), f"thường đi cùng nhau ({together}/{n} lần)"


@dataclass
class Context:
    topology: Topology | None = None
    partition: list[str] = field(default_factory=list)  # khác nhau ở nhãn này thì không bao giờ chung sự cố
    burst_seconds: int = 0  # 0 = không cộng điểm cùng đợt
    stats: PairStats | None = None
    blocked: set = field(default_factory=set)  # cặp khóa người đã tách gần đây


def _same_object(ra: list[str], rb: list[str]) -> str | None:
    """Cùng workload hoặc cùng Service; thiếu namespace ở một bên thì chỉ so tên (như luật cũ)."""
    for x in ra:
        nx, kx, namex = x.split("/", 2)
        for y in rb:
            ny, ky, namey = y.split("/", 2)
            if kx == ky and namex == namey and (nx == ny or not nx or not ny):
                return x
    return None


def pair_score(a: dict, b: dict, ctx: Context) -> Evidence:
    """a, b là kết quả của view()."""
    la, lb = a["target"], b["target"]
    for key in ctx.partition:
        if la.get(key) and lb.get(key) and str(la[key]) != str(lb[key]):
            return Evidence(0.0, None, f"khác {key}")
    if frozenset((a["key"], b["key"])) in ctx.blocked:
        return Evidence(0.0, None, "người đã tách cặp cảnh báo này gần đây")
    if a.get("fingerprint") and a.get("fingerprint") == b.get("fingerprint"):
        return Evidence(SAME_ALERT, "rule", "cùng cảnh báo bắn lại")
    base, why = 0.0, ""
    same = _same_object(a["refs"], b["refs"])
    if same:
        base, why = SAME_WORKLOAD, f"cùng {'Service' if '/svc/' in same else 'workload'} {same.split('/', 2)[2]}"
    topo = ctx.topology
    if topo and base < SAME_WORKLOAD:
        pair = topo.same_unit(a["refs"], b["refs"])
        if pair:
            svc, other = sorted(pair, key=lambda r: "/svc/" not in r)
            base, why = SAME_WORKLOAD, f"{ref_name(svc)} trỏ tới {ref_name(other)}"
    if topo and base < DEPENDENCY:
        edge = topo.link(a["refs"], b["refs"])
        if edge:
            base, why = DEPENDENCY, f"{ref_name(edge[0])} {Topology.VERB[edge[2]]} {ref_name(edge[1])}"
    if base < SAME_NODE and la.get("node") and la.get("node") == lb.get("node"):
        base, why = SAME_NODE, f"cùng node {la['node']}"
    if base < SAME_NAMESPACE and la.get("namespace") and la.get("namespace") == lb.get("namespace"):
        base, why = SAME_NAMESPACE, f"cùng namespace {la['namespace']}"
    parts = [why] if why else []
    bonus = 0.0
    if ctx.burst_seconds and a.get("ts") and b.get("ts"):
        gap = abs((a["ts"] - b["ts"]).total_seconds())
        if gap <= ctx.burst_seconds:
            bonus += BURST_BONUS
            parts.append(f"bắn cách nhau {int(gap)} giây")
    if ctx.stats:
        extra, said = ctx.stats.bonus(a["key"], b["key"])
        if extra:
            bonus += extra
            parts.append(said)
    score = round(min(1.0, base + bonus), 2)
    if score <= 0:
        return Evidence(0.0, None, "")
    return Evidence(score, "topology" if base >= STRUCTURAL else "score", f"điểm {score:.2f}: " + "; ".join(parts))


def best(xs: list[dict], ys: list[dict], score: Callable[[dict, dict], Evidence | None]) -> Evidence | None:
    found = None
    for x in xs:
        for y in ys:
            ev = score(x, y)
            if ev and ev.score > 0 and (found is None or ev.score > found.score):
                found = ev
    return found


def cluster(items: list[dict], score: Callable[[dict, dict], Evidence | None], threshold: float) -> list[list[dict]]:
    """Cảnh báo mới trong cùng một vòng poll thành các cụm (thành phần liên thông theo điểm ≥ threshold), giữ thứ tự thời gian."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            ev = score(items[i], items[j])
            if ev and ev.score >= threshold:
                parent[find(j)] = find(i)
    groups: dict[int, list[dict]] = {}
    for i, item in enumerate(items):
        groups.setdefault(find(i), []).append(item)
    return list(groups.values())


def roles(items: list[dict], topo: Topology | None) -> dict[str, tuple[str, str]]:
    """Vai trò theo topology: cảnh báo ở đối tượng mà cảnh báo khác phụ thuộc vào là gốc, cảnh báo phụ thuộc vào nó là triệu chứng.

    Chỉ trả về khi topology nối được ít nhất hai cảnh báo; không có thì {} (không đoán theo giờ bắn).
    """
    if not topo:
        return {}
    units = {a["id"]: topo.units(a["refs"]) for a in items}
    deps = {a["id"]: [b for b in items if b["id"] != a["id"] and topo.reaches(units[a["id"]], units[b["id"]])] for a in items}
    upstream = {b["id"] for a in items for b in deps[a["id"]]}
    out: dict[str, tuple[str, str]] = {}
    for a in items:
        if deps[a["id"]]:
            names = list(dict.fromkeys(ref_name(r) for b in deps[a["id"]] for r in b["refs"][:1]))
            out[a["id"]] = ("symptom", "phụ thuộc " + ", ".join(names[:3]) + " đang cảnh báo")
        elif a["id"] in upstream:
            below = list(dict.fromkeys(x["alertname"] or "?" for x in items if a in deps[x["id"]]))
            out[a["id"]] = ("root", "các cảnh báo phụ thuộc vào đây: " + ", ".join(below[:5]))
    return out
