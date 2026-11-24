"""Topology nhiều tầng (docs/TOPOLOGY-PLAN.md, giai đoạn A): thực thể và quan hệ có thời hạn, mức tin theo từng cạnh tại thời điểm hỏi.

Tên kiểu thực thể và khoá là tạm, đổi theo data model chung khi có. Quét bằng công cụ đọc của kết nối dùng chung (tài khoản bot, có audit),
gọi connector trực tiếp như lúc lấy cảnh báo: quét nền chỉ đọc nên không qua classifier của auto mode. Tầng cần quyền admin OpenStack là
best effort: không thấy trường của admin thì ghi tầng đó bị bỏ qua, các tầng khác vẫn dựng.

Topology đã lưu không phải fact: cạnh chỉ tin cậy tại t khi khoảng hiệu lực bao trùm t và lần thấy gần nhất đủ gần t theo loại cạnh.
Cạnh chưa đủ tin thì kèm lệnh đọc trực tiếp để agent kiểm chứng qua gateway (bước S<n> mới là căn cứ).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import shlex
import threading
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger("kagent.topology")

FULL_HOURS = 6  # không có sự cố
INCIDENT_MINUTES = 15  # có sự cố mở sau lần quét gần nhất mà lần đó đã cách quá chừng này thì quét lại một lần
TEMPLATES = ("k8s", "openstack")
SAFE = re.compile(r"^[\w.:/@+\-]{1,128}$")  # tên do người đặt (VM, volume) có thể chứa chữ tự do: không khớp thì không đưa cho model
# Cạnh tin được bao lâu sau lần thấy gần nhất (giờ), theo tốc độ thay đổi; đề xuất, chờ người vận hành từng tầng xác nhận.
TRUST_HOURS = {("k8s.node", "member_of"): 24 * 7, ("k8s.pod", "runs_on"): 0.25, ("k8s.pod", "uses"): 0.25, ("k8s.node", "runs_on"): 24, ("k8s.pvc", "uses"): 24, ("k8s.pv", "uses"): 24,
               ("os.server", "member_of"): 24 * 7, ("os.server", "uses"): FULL_HOURS + 1, ("os.server", "runs_on"): FULL_HOURS + 1,
               ("os.volume", "runs_on"): FULL_HOURS + 1}
DOWN = ("runs_on", "uses")  # đi xuống tầng dưới
NODE_COLS = ("NAME:.metadata.name,PROVIDER:.spec.providerID,IP:.status.addresses[?(@.type==\"InternalIP\")].address,"
             "READY:.status.conditions[?(@.type==\"Ready\")].status")
POD_COLS = "NAME:.metadata.name,NODE:.spec.nodeName,PVC:.spec.volumes[*].persistentVolumeClaim.claimName,PHASE:.status.phase"
MAX_POD_NAMESPACES = 10
PV_COLS = ("NAME:.metadata.name,DRIVER:.spec.csi.driver,HANDLE:.spec.csi.volumeHandle,CINDER:.spec.cinder.volumeID,"
           "NS:.spec.claimRef.namespace,CLAIM:.spec.claimRef.name,SC:.spec.storageClassName,STATUS:.status.phase")
UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|[0-9a-z]{1,64}", re.I)

_running: set[str] = set()
_lock = threading.Lock()


class TopologyError(Exception):
    pass


def _safe(v) -> str | None:
    v = "" if v is None else str(v)
    return v if SAFE.match(v) else None


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------- quét ----------

class _Scan:
    """Kết quả một lần quét của một nguồn: thực thể, cạnh, loại thực thể đã liệt kê đủ (để đánh dấu cái biến mất)."""

    def __init__(self, source: str):
        self.source, self.entities, self.edges, self.complete, self.layers = source, {}, set(), set(), {}
        self.complete_prefix: list[tuple[str, str]] = []  # (type, tiền tố id): liệt kê đủ trong phạm vi, vd pod của một namespace

    def entity(self, eid: str, type_: str, name: str | None, **attrs) -> str:
        self.entities[eid] = (type_, _safe(name) or eid.split(":", 1)[1][-64:], {k: v for k, v in attrs.items() if v not in (None, "")})
        return eid

    def edge(self, src: str, rel: str, dst: str, how: str = "api") -> None:
        self.edges.add((src, rel, dst, how))


def _call(conn, tenant_id: str, row: dict, tool: str, args: dict) -> dict:
    from . import gateway
    from .incidents.config import bot_user
    name = f"{row['name']}.{tool}"
    reg = conn.execute("SELECT enabled, status FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, name)).fetchone()
    if reg is None or not reg["enabled"] or reg["status"] != "active":
        raise TopologyError(f"công cụ {name} đang tắt")
    user = bot_user(conn, tenant_id)
    ctx = gateway.CallContext(tenant_id, user, (row["envs"] or ["dev"])[0], "quét topology", intent=["quét topology (chỉ đọc)"])
    status, result = gateway._execute(conn, tenant_id, ctx.env, name, args, user_id=user)
    with db.tx(conn):
        gateway.audit(conn, ctx, name, args, None, "system", ["quét topology nền, chỉ đọc"], status)
    if status != "ok":
        raise TopologyError(f"{name}: {(result or {}).get('error', 'lỗi')}"[:300])
    return result


def _columns(text: str) -> list[dict]:
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return []
    head = lines[0].split()
    return [dict(zip(head, [None if v == "<none>" else v for v in l.split()])) for l in lines[1:]]


PAGE, MAX_PAGES = 1000, 20  # Nova và Cinder trả tối đa 1000 bản ghi mỗi trang


def _list(conn, tenant_id: str, row: dict, service: str, path: str, key: str) -> tuple[list[dict], bool]:
    """Đọc hết các trang bằng marker; (bản ghi, đủ hay không). Thiếu thì không được đánh dấu thực thể biến mất."""
    items, marker = [], None
    for _ in range(MAX_PAGES):
        query = {"all_tenants": 1, "limit": PAGE, **({"marker": marker} if marker else {})}
        page = (_call(conn, tenant_id, row, "api_read", {"service": service, "path": path, "query": query}).get("json") or {}).get(key) or []
        items += page
        if len(page) < PAGE:
            return items, True
        marker = page[-1]["id"]
    return items, False


def _scan_openstack(conn, tenant_id: str, row: dict) -> _Scan:
    s = _Scan(row["name"])
    servers, full = _list(conn, tenant_id, row, "compute", "/servers/detail", "servers")
    if full:
        s.complete.add("os.server")
    admin_host = False
    for v in servers:
        sid = s.entity(f"os.server:{v['id']}", "os.server", v.get("name"), status=v.get("status"),
                       ips=sorted({a.get("addr") for net in (v.get("addresses") or {}).values() for a in net if a.get("addr")}))
        if v.get("tenant_id"):
            s.edge(sid, "member_of", s.entity(f"os.project:{v['tenant_id']}", "os.project", v["tenant_id"]))
        host = v.get("OS-EXT-SRV-ATTR:hypervisor_hostname") or v.get("OS-EXT-SRV-ATTR:host")
        if host:
            admin_host = True
            s.edge(sid, "runs_on", s.entity(f"os.hypervisor:{host}", "os.hypervisor", host))
        for att in v.get("os-extended-volumes:volumes_attached") or []:
            if att.get("id"):
                s.edge(sid, "uses", f"os.volume:{att['id']}")
    s.layers["hypervisor"] = "ok" if admin_host or not servers else "bỏ qua: tài khoản OpenStack không có quyền admin (không thấy host của máy ảo)"
    try:
        vols, vfull = _list(conn, tenant_id, row, "block-storage", "/volumes/detail", "volumes")
    except TopologyError as exc:
        s.layers["volume"] = f"lỗi: {exc}"
        return s
    if vfull:
        s.complete.add("os.volume")
    backend = False
    for v in vols:
        vid = s.entity(f"os.volume:{v['id']}", "os.volume", v.get("name"), status=v.get("status"), volume_type=_safe(v.get("volume_type")),
                       size=v.get("size"))
        for att in v.get("attachments") or []:
            if att.get("server_id"):
                s.edge(f"os.server:{att['server_id']}", "uses", vid)
        host = v.get("os-vol-host-attr:host")
        if host:
            backend = True
            name = host.split("#", 1)[0]
            s.edge(vid, "runs_on", s.entity(f"os.backend:{name}", "os.backend", name))
    s.layers["volume"] = "ok" if vfull else f"một phần: quá {PAGE * MAX_PAGES} volume"
    if not full:
        s.layers["server"] = f"một phần: quá {PAGE * MAX_PAGES} máy ảo"
    s.layers["storage_backend"] = "ok" if backend or not vols else "bỏ qua: tài khoản OpenStack không có quyền admin (không thấy backend của volume)"
    return s


def _server_by(conn, tenant_id: str, uuid: str | None, ip: str | None) -> tuple[str | None, str]:
    if uuid and conn.execute("SELECT 1 FROM topo_entity WHERE tenant_id=? AND id=?", (tenant_id, f"os.server:{uuid}")).fetchone():
        return f"os.server:{uuid}", "api"  # providerID trỏ thẳng tới máy ảo
    if ip:
        for r in conn.execute("SELECT id, attrs FROM topo_entity WHERE tenant_id=? AND type='os.server' AND gone_at IS NULL", (tenant_id,)):
            if ip in (json.loads(r["attrs"]).get("ips") or []):
                return r["id"], "match"  # trùng IP nội bộ: chỉ là gợi ý
    return None, ""


def _scan_k8s(conn, tenant_id: str, row: dict) -> _Scan:
    s, c = _Scan(row["name"]), row["name"]
    cluster = s.entity(f"k8s.cluster:{c}", "k8s.cluster", c)
    out = _call(conn, tenant_id, row, "kubectl_read", {"command": "get nodes -o " + shlex.quote(f"custom-columns={NODE_COLS}")})
    partial = "đã cắt" in (out.get("stdout") or "")
    if not partial:
        s.complete.add("k8s.node")
    for n in _columns(out.get("stdout")):
        nid = s.entity(f"k8s.node:{c}/{n['NAME']}", "k8s.node", n["NAME"], ip=n.get("IP"), ready=n.get("READY"))
        s.edge(nid, "member_of", cluster)
        provider = (n.get("PROVIDER") or "").rstrip("/").rsplit("/", 1)[-1] if (n.get("PROVIDER") or "").startswith("openstack://") else None
        sid, how = _server_by(conn, tenant_id, provider, n.get("IP"))
        if sid:
            s.edge(nid, "runs_on", sid, how)
    out = _call(conn, tenant_id, row, "kubectl_read", {"command": "get pv -o " + shlex.quote(f"custom-columns={PV_COLS}")})
    if "đã cắt" not in (out.get("stdout") or ""):
        s.complete.update({"k8s.pv", "k8s.pvc"})
    for p in _columns(out.get("stdout")):
        pid = s.entity(f"k8s.pv:{c}/{p['NAME']}", "k8s.pv", p["NAME"], storage_class=p.get("SC"), status=p.get("STATUS"))
        handle = p.get("CINDER") or (p.get("HANDLE") if "cinder" in (p.get("DRIVER") or "") else None)
        if handle:
            s.edge(pid, "uses", f"os.volume:{handle}")
        if p.get("NS") and p.get("CLAIM"):
            s.edge(s.entity(f"k8s.pvc:{c}/{p['NS']}/{p['CLAIM']}", "k8s.pvc", p["CLAIM"], namespace=p["NS"]), "uses", pid)
    s.layers["k8s"] = "ok" if not partial else "một phần: danh sách node bị cắt"
    for ns in _incident_namespaces(conn, tenant_id):
        try:
            out = _call(conn, tenant_id, row, "kubectl_read", {"command": f"get pods -n {ns} -o " + shlex.quote(f"custom-columns={POD_COLS}")})
        except TopologyError as exc:
            s.layers[f"pod:{ns}"] = f"lỗi: {exc}"
            continue
        if "đã cắt" not in (out.get("stdout") or ""):
            s.complete_prefix.append(("k8s.pod", f"k8s.pod:{c}/{ns}/"))
        for p in _columns(out.get("stdout")):
            pid = s.entity(f"k8s.pod:{c}/{ns}/{p['NAME']}", "k8s.pod", p["NAME"], namespace=ns, phase=p.get("PHASE"))
            if p.get("NODE"):
                s.edge(pid, "runs_on", f"k8s.node:{c}/{p['NODE']}")
            for claim in (p.get("PVC") or "").split(","):
                if claim:
                    s.edge(pid, "uses", f"k8s.pvc:{c}/{ns}/{claim}")
    return s


def _incident_namespaces(conn, tenant_id: str) -> list[str]:
    """Namespace của cảnh báo đang bắn thuộc sự cố đang mở: pod chỉ quét ở đây (đổi nhanh, quét cả cụm thì tốn)."""
    out = []
    for r in conn.execute("SELECT a.labels FROM alert_event a JOIN incident i ON i.id=a.incident_id WHERE a.tenant_id=? AND i.status='open' "
                          "AND a.status='firing' ORDER BY a.last_seen DESC LIMIT 200", (tenant_id,)):
        ns = _safe(json.loads(r["labels"]).get("namespace"))
        if ns and ns not in out and re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", ns):
            out.append(ns)
    return out[:MAX_POD_NAMESPACES]


def _store(conn, tenant_id: str, scan: _Scan, now: str) -> dict:
    """Ghi thực thể và cạnh; cạnh đổi thì đóng cạnh cũ, mở cạnh mới, không ghi đè."""
    stats = {"entities": len(scan.entities), "opened": 0, "closed": 0, "gone": 0}
    for eid, (type_, name, attrs) in scan.entities.items():
        conn.execute("INSERT INTO topo_entity(tenant_id,id,type,name,attrs,source,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(tenant_id,id) DO UPDATE SET name=excluded.name, attrs=excluded.attrs, source=excluded.source, "
                     "last_seen=excluded.last_seen, gone_at=NULL", (tenant_id, eid, type_, name, json.dumps(attrs, ensure_ascii=False), scan.source, now, now))
    scopes = [(t, "") for t in scan.complete] + scan.complete_prefix
    for type_, prefix in scopes:
        for r in conn.execute("SELECT id FROM topo_entity WHERE tenant_id=? AND type=? AND source=? AND gone_at IS NULL AND id LIKE ?",
                              (tenant_id, type_, scan.source, prefix.replace("%", "") + "%")).fetchall():
            if r["id"] in scan.entities:  # so theo tập đã thấy, không theo mốc giờ: hai lần quét có thể cùng một giây
                continue
            conn.execute("UPDATE topo_entity SET gone_at=? WHERE tenant_id=? AND id=?", (now, tenant_id, r["id"]))
            stats["closed"] += conn.execute("UPDATE topo_edge SET valid_to=? WHERE tenant_id=? AND valid_to IS NULL AND (src=? OR dst=?)",
                                            (now, tenant_id, r["id"], r["id"])).rowcount
            stats["gone"] += 1
    new = {(a, rel, b): how for a, rel, b, how in scan.edges}
    srcs = {a for a, _, _ in new} | {eid for eid in scan.entities}
    for src in srcs:
        for e in conn.execute("SELECT id, rel, dst, how FROM topo_edge WHERE tenant_id=? AND src=? AND source=? AND valid_to IS NULL",
                              (tenant_id, src, scan.source)).fetchall():
            key = (src, e["rel"], e["dst"])
            if key in new and new[key] == e["how"]:
                conn.execute("UPDATE topo_edge SET last_seen=? WHERE id=?", (now, e["id"]))
                del new[key]
            else:
                conn.execute("UPDATE topo_edge SET valid_to=? WHERE id=?", (now, e["id"]))
                stats["closed"] += 1
    for (src, rel, dst), how in new.items():
        conn.execute("INSERT INTO topo_edge(tenant_id,src,rel,dst,how,source,valid_from,last_seen) VALUES (?,?,?,?,?,?,?,?)",
                     (tenant_id, src, rel, dst, how, scan.source, now, now))
        stats["opened"] += 1
    return stats


def sources(conn, tenant_id: str) -> list[dict]:
    from .connections import _row
    rows = conn.execute("SELECT * FROM connection WHERE tenant_id=? AND status='active' AND scope='tenant' AND template IN ('k8s','openstack') "
                        "ORDER BY template DESC, name", (tenant_id,)).fetchall()  # openstack trước: cạnh node → máy ảo cần máy ảo đã có
    return [_row(r) for r in rows]


def scan(conn, tenant_id: str, reason: str = "manual") -> dict:
    started = db.iso()
    result = {}
    for row in sources(conn, tenant_id):
        try:
            sc = (_scan_openstack if row["template"] == "openstack" else _scan_k8s)(conn, tenant_id, row)
        except TopologyError as exc:
            result[row["name"]] = {"status": "error", "error": str(exc)}
            continue
        with db.tx(conn):
            stats = _store(conn, tenant_id, sc, db.iso())
        result[row["name"]] = {"status": "ok", "layers": sc.layers, **stats}
    with db.tx(conn):
        conn.execute("INSERT INTO topo_scan(tenant_id,started_at,finished_at,reason,result) VALUES (?,?,?,?,?)",
                     (tenant_id, started, db.iso(), reason, json.dumps(result, ensure_ascii=False)))
    return {"started_at": started, "reason": reason, "sources": result}


def last_scan(conn, tenant_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM topo_scan WHERE tenant_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1", (tenant_id,)).fetchone()
    return {"started_at": r["started_at"], "finished_at": r["finished_at"], "reason": r["reason"], "sources": json.loads(r["result"])} if r else None


def due(conn, tenant_id: str) -> str | None:
    if not sources(conn, tenant_id):
        return None
    last = last_scan(conn, tenant_id)
    if last is None:
        return "first"
    age = db.now() - _ts(last["started_at"])
    if age >= timedelta(hours=FULL_HOURS):
        return "periodic"
    if age >= timedelta(minutes=INCIDENT_MINUTES) and conn.execute(
            "SELECT 1 FROM incident WHERE tenant_id=? AND status='open' AND created_at>=? LIMIT 1", (tenant_id, last["started_at"])).fetchone():
        return "incident"
    return None


def start(tenant_id: str, reason: str) -> bool:
    with _lock:
        if tenant_id in _running:
            return False
        _running.add(tenant_id)

    def work():
        try:
            with db.session() as conn:
                scan(conn, tenant_id, reason)
        except Exception:
            log.exception("tenant %s: quét topology lỗi", tenant_id)
        finally:
            with _lock:
                _running.discard(tenant_id)

    threading.Thread(target=work, name=f"topology-{tenant_id[:8]}", daemon=True).start()
    return True


def tick(conn) -> None:
    for t in conn.execute("SELECT id FROM tenant").fetchall():
        reason = due(conn, t["id"])
        if reason:
            start(t["id"], reason)


def running(tenant_id: str) -> bool:
    with _lock:
        return tenant_id in _running


# ---------- đọc ----------

def trust(edge, type_: str, at: datetime) -> tuple[bool, str]:
    """(tin cậy tại at, lý do)."""
    vf, vt, seen = _ts(edge["valid_from"]), _ts(edge["valid_to"]), _ts(edge["last_seen"])
    if edge["how"] == "match":
        return False, "suy từ IP trùng, chưa có lần đọc xác nhận"
    if vf > at:
        return False, f"cạnh chỉ thấy từ {edge['valid_from']}, sau thời điểm hỏi"
    if vt and vt <= at:
        return False, f"cạnh đã đổi lúc {edge['valid_to']}"
    window = timedelta(hours=TRUST_HOURS.get((type_, edge["rel"]), 1))
    if seen >= at or at - seen <= window:
        return True, f"thấy lúc {edge['last_seen']}"
    return False, f"lần thấy gần nhất {edge['last_seen']} đã cũ hơn {int(window.total_seconds() // 3600)} giờ"


def _verify(eid: str, source: str | None) -> dict | None:
    """Lệnh đọc trực tiếp để kiểm chứng một thực thể, bằng kết nối đã thấy nó."""
    kind, _, key = eid.partition(":")
    if kind.startswith("k8s."):
        conn_name, _, rest = key.partition("/")
        if kind == "k8s.pvc":
            ns, _, pvc = rest.partition("/")
            return {"tool": f"{conn_name}.kubectl_read", "args": {"command": f"get pvc {pvc} -n {ns} -o wide"}}
        if kind in ("k8s.node", "k8s.pv"):
            return {"tool": f"{conn_name}.kubectl_read", "args": {"command": f"get {kind[4:]} {rest} -o wide"}}
        return None
    if not source:
        return None
    if kind == "os.server":
        return {"tool": f"{source}.api_read", "args": {"service": "compute", "path": f"/servers/{key}"}}
    if kind == "os.volume":
        return {"tool": f"{source}.api_read", "args": {"service": "block-storage", "path": f"/volumes/{key}"}}
    return None


def start_points(conn, tenant_id: str, labels: dict) -> tuple[list[str], list[dict]]:
    """Thực thể ứng với nhãn cảnh báo; pod không lưu nên trả lệnh đọc trực tiếp để tìm node của pod."""
    from .incidents.grouping import target_labels
    labels = target_labels(labels)  # nhãn pod/instance của exporter (kube-state-metrics) là của exporter, không phải đối tượng lỗi
    found, reads = [], []
    node = _safe(labels.get("node"))
    inst = str(labels.get("instance") or "")
    host = inst.rsplit(":", 1)[0] if inst.count(":") == 1 else inst
    if node:
        found += [r["id"] for r in conn.execute("SELECT id FROM topo_entity WHERE tenant_id=? AND type='k8s.node' AND name=?", (tenant_id, node))]
    if host and not found:
        for r in conn.execute("SELECT id, name, attrs FROM topo_entity WHERE tenant_id=? AND type='k8s.node'", (tenant_id,)):
            if r["name"] == host or json.loads(r["attrs"]).get("ip") == host:
                found.append(r["id"])
        try:
            ipaddress.ip_address(host)
            if not found:
                found += [r["id"] for r in conn.execute("SELECT id, attrs FROM topo_entity WHERE tenant_id=? AND type='os.server'", (tenant_id,))
                          if host in (json.loads(r["attrs"]).get("ips") or [])]
        except ValueError:
            pass
    pvc, ns = _safe(labels.get("persistentvolumeclaim")), _safe(labels.get("namespace"))
    if pvc and ns:
        found += [r["id"] for r in conn.execute("SELECT id FROM topo_entity WHERE tenant_id=? AND type='k8s.pvc' AND id LIKE ?",
                                                (tenant_id, f"k8s.pvc:%/{ns}/{pvc}"))]
    pod = _safe(labels.get("pod"))
    if pod and ns and not found:
        found += [r["id"] for r in conn.execute("SELECT id FROM topo_entity WHERE tenant_id=? AND type='k8s.pod' AND gone_at IS NULL AND id LIKE ?",
                                                (tenant_id, f"k8s.pod:%/{ns}/{pod}"))]
    if pod and ns and not node and not found:
        for row in sources(conn, tenant_id):
            if row["template"] == "k8s":
                reads.append({"tool": f"{row['name']}.kubectl_read", "args": {"command": f"get pod {pod} -n {ns} -o wide"},
                              "why": "pod không lưu trong topology; cột NODE cho biết node, rồi gọi lại với labels.node"})
    return list(dict.fromkeys(found)), reads


def _edges_at(conn, tenant_id: str, src: str, t: datetime, rels=DOWN) -> list:
    rel_in = f"rel IN ({','.join('?' * len(rels))})"
    edges = conn.execute(f"SELECT * FROM topo_edge WHERE tenant_id=? AND src=? AND {rel_in} AND valid_from<=? "
                         "AND (valid_to IS NULL OR valid_to>?) ORDER BY id", (tenant_id, src, *rels, db.iso(t), db.iso(t))).fetchall()
    if not edges:  # chưa quét tới thời điểm hỏi: dùng cạnh đang mở, trust() đánh dấu gợi ý
        edges = conn.execute(f"SELECT * FROM topo_edge WHERE tenant_id=? AND src=? AND {rel_in} AND valid_to IS NULL ORDER BY id",
                             (tenant_id, src, *rels)).fetchall()
    return edges


def _walk(conn, tenant_id: str, starts: list[str], t: datetime, depth: int, ents: dict) -> tuple[list[dict], set, list[dict]]:
    """Đi xuống tầng dưới từ starts tại t: (cạnh kèm mức tin, thực thể đã qua, lệnh đọc lại cho cạnh gợi ý)."""
    chain, seen, frontier, reads = [], set(starts), list(starts), []
    for _ in range(max(1, min(depth, 6))):
        nxt = []
        for src in frontier:
            for e in _edges_at(conn, tenant_id, src, t):
                ok, why = trust(e, ents[src]["type"] if src in ents else src.split(":", 1)[0], t)
                chain.append({"from": src, "rel": e["rel"], "to": e["dst"], "how": e["how"], "trust": "tin cậy" if ok else "gợi ý", "why": why})
                if not ok:
                    v = _verify(src, ents[src]["source"] if src in ents else None) or _verify(e["dst"], ents[e["dst"]]["source"] if e["dst"] in ents else None)
                    if v and all(v["args"] != r["args"] for r in reads):
                        reads.append({**v, "why": f"kiểm chứng {src} {e['rel']} {e['dst']} ({why})"})
                if e["dst"] not in seen:
                    seen.add(e["dst"])
                    nxt.append(e["dst"])
        frontier = nxt
    return chain, seen, reads


def neighbors(conn, tenant_id: str, entity: str | None = None, labels: dict | None = None, at: str | None = None, depth: int = 4) -> dict:
    t = _ts(at) if at else db.now()
    starts, reads = ([entity] if entity else []), []
    if labels:
        more, reads = start_points(conn, tenant_id, labels)
        starts += more
    ents = {r["id"]: r for r in conn.execute("SELECT * FROM topo_entity WHERE tenant_id=?", (tenant_id,))} if starts else {}
    starts = [s for s in starts if s in ents]
    chain, seen, more = _walk(conn, tenant_id, starts, t, depth, ents)
    reads += [r for r in more if all(r["args"] != x["args"] for x in reads)]
    # Thực thể khác cùng nằm trên host, backend hay volume đã đi qua: lỗi ở tầng dưới thường ảnh hưởng tất cả.
    shared = []
    for eid in seen - set(starts):
        if eid.split(":", 1)[0] not in ("os.hypervisor", "os.backend", "os.server"):
            continue
        others = [r["src"] for r in conn.execute("SELECT src FROM topo_edge WHERE tenant_id=? AND dst=? AND rel IN ('runs_on','uses') AND valid_to IS NULL",
                                                 (tenant_id, eid)) if r["src"] not in seen][:20]
        if others:
            shared.append({"via": eid, "others": others})
    last = last_scan(conn, tenant_id)
    skipped = {f"{src}:{layer}": text for src, r in (last or {}).get("sources", {}).items() for layer, text in (r.get("layers") or {}).items()
               if text != "ok"} | {src: r["error"] for src, r in (last or {}).get("sources", {}).items() if r.get("status") == "error"}
    show = seen | {o for s in shared for o in s["others"]}
    return {"at": db.iso(t), "start": starts, "chain": chain, "shared": shared, "verify": reads[:10],
            "entities": {eid: {"type": ents[eid]["type"], "name": ents[eid]["name"], **json.loads(ents[eid]["attrs"]),
                               **({"gone_at": ents[eid]["gone_at"]} if ents[eid]["gone_at"] else {})} for eid in show if eid in ents},
            "last_scan": last["started_at"] if last else None, "skipped_layers": skipped,
            "note": ("Topology đã lưu không phải fact: cạnh 'gợi ý' phải đọc lại trực tiếp (mục verify) trước khi dùng làm căn cứ."
                     if starts else "Không tìm thấy thực thể ứng với tham số; xem verify hoặc dùng kubectl/OpenStack trực tiếp.")}


def changes(conn, tenant_id: str, start: str, end: str, limit: int = 30) -> list[dict]:
    """Cạnh đổi trong khoảng thời gian (không tính lần đầu thấy thực thể): pod/node chuyển máy ảo, VM migrate, volume gắn lại."""
    out = []
    for e in conn.execute("SELECT e.*, s.first_seen FROM topo_edge e JOIN topo_entity s ON s.tenant_id=e.tenant_id AND s.id=e.src "
                          "WHERE e.tenant_id=? AND ((e.valid_from>=? AND e.valid_from<=? AND e.valid_from>s.first_seen) OR (e.valid_to>=? AND e.valid_to<=?)) "
                          "ORDER BY coalesce(e.valid_to, e.valid_from) DESC LIMIT ?", (tenant_id, start, end, start, end, limit)):
        if e["valid_to"] and start <= e["valid_to"] <= end:
            out.append({"at": e["valid_to"], "change": f"{e['src']} không còn {e['rel']} {e['dst']}"})
        if start <= e["valid_from"] <= end and e["valid_from"] > e["first_seen"]:
            out.append({"at": e["valid_from"], "change": f"{e['src']} {e['rel']} {e['dst']}"})
    return sorted(out, key=lambda x: x["at"], reverse=True)[:limit]


# ---------- sự cố: đồ thị phần bị ảnh hưởng, nghi gây lỗi, khoanh vùng ----------

# Tầng vẽ từ trên xuống; nhánh lưu trữ (PVC → PV → volume → backend) song song với nhánh tính toán.
LAYER = {"k8s.cluster": 0, "k8s.pod": 1, "k8s.node": 2, "k8s.pvc": 2, "os.server": 3, "k8s.pv": 3, "os.volume": 4, "os.hypervisor": 5, "os.backend": 5}
LAYER_NAMES = ["Cluster", "Pod", "Node, PVC", "Máy ảo, PV", "Volume", "Hạ tầng: host, backend lưu trữ"]
TYPE_LABEL = {"k8s.cluster": "cluster", "k8s.pod": "pod", "k8s.node": "node", "k8s.pvc": "PVC", "k8s.pv": "PV", "os.server": "máy ảo",
              "os.volume": "volume", "os.hypervisor": "host", "os.backend": "backend lưu trữ", "os.project": "project"}
SPECIFIC_SHARE = 0.8  # cùng hit >= 2; chưa kiểm trên sự cố thật
_UP = ("WITH RECURSIVE up(id, d) AS (SELECT ?, 0 UNION SELECT e.src, up.d + 1 FROM topo_edge e JOIN up ON e.dst = up.id "
       "WHERE e.tenant_id = ? AND e.rel IN ('runs_on','uses') AND e.valid_from <= ? AND (e.valid_to IS NULL OR e.valid_to > ?) AND up.d < 5) "
       "SELECT DISTINCT t.id, t.type FROM up JOIN topo_entity t ON t.tenant_id = ? AND t.id = up.id WHERE up.d > 0")


def _get(conn, tenant_id: str, ids) -> dict:
    ids = list(dict.fromkeys(ids))
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out.update({r["id"]: r for r in conn.execute(f"SELECT * FROM topo_entity WHERE tenant_id=? AND id IN ({','.join('?' * len(part))})",
                                                     (tenant_id, *part))})
    return out


def above(conn, tenant_id: str, eid: str, t: datetime) -> list[tuple[str, str]]:
    """Mọi thực thể phụ thuộc (trực tiếp hay gián tiếp, tới 5 tầng) vào eid tại t: (id, type)."""
    return [(r["id"], r["type"]) for r in conn.execute(_UP, (eid, tenant_id, db.iso(t), db.iso(t), tenant_id))]


def specific(conn, tenant_id: str, eid: str, alerted: set[str], alerting: set[str], t: datetime) -> tuple[bool, str, int, int]:
    """Tầng dưới chung có đáng nghi không: chính nó đang có cảnh báo, hoặc phần lớn thực thể cùng loại với thực thể bị cảnh báo
    mà phụ thuộc vào nó đang bị cảnh báo (ở sự cố này hay sự cố khác đang mở, vì cảnh báo của cùng một lỗi có thể chưa được gộp).
    Dùng chung là bình thường với backend hay host lớn, nên không đủ để nghi."""
    if eid in alerting:
        return True, "chính nó đang có cảnh báo", 0, 0
    kinds = {a.split(":", 1)[0] for a in alerted}
    peers = {i for i, ty in above(conn, tenant_id, eid, t) if ty in kinds}
    hits = peers & (alerted | alerting)
    hit, label = len(hits), "/".join(TYPE_LABEL.get(k, k) for k in sorted(kinds))
    elsewhere = len(hits - alerted)
    extra = f", gồm {elsewhere} ở sự cố khác" if elsewhere else ""
    if hit >= 2 and hit / len(peers) >= SPECIFIC_SHARE:
        return True, f"{hit}/{len(peers)} {label} dùng nó đang bị cảnh báo{extra}", hit, len(peers)
    return False, f"dùng chung bởi {len(peers)} {label}, {hit} đang bị cảnh báo{extra}", hit, len(peers)


_KIND = {"node": "k8s.node", "nodes": "k8s.node", "no": "k8s.node", "pv": "k8s.pv", "persistentvolume": "k8s.pv", "pvc": "k8s.pvc",
         "persistentvolumeclaim": "k8s.pvc", "pod": "k8s.pod", "pods": "k8s.pod", "po": "k8s.pod"}


def step_entities(tool: str, args) -> list[str]:
    """Thực thể mà một bước điều tra đọc tới (để nối mắt xích 5 Whys có căn cứ với topology)."""
    if not isinstance(args, dict) or "." not in tool:
        return []
    conn_name = tool.split(".", 1)[0]
    path = str(args.get("path") or "")
    m = re.match(r"^/(servers|volumes)/([A-Za-z0-9-]{1,64})(/|$)", path)
    if m and m.group(2) != "detail":  # id không có trong topology thì bị bỏ khi tra
        return [f"os.{'server' if m.group(1) == 'servers' else 'volume'}:{m.group(2)}"]
    try:
        words = [w for w in shlex.split(str(args.get("command") or "")) if w != "kubectl"]
    except ValueError:
        return []
    if len(words) < 2 or words[0] not in ("get", "describe", "top"):
        return []
    ns = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in ("-n", "--namespace")), None)
    rest = [w for i, w in enumerate(words[1:], 1) if not w.startswith("-") and words[i - 1] not in ("-n", "--namespace", "-o", "-l", "-c")]
    kind, names = (rest[0], rest[1:]) if rest and "/" not in rest[0] else (None, rest)
    out = []
    for n in names:
        k, _, name = n.partition("/") if "/" in n else (kind, "", n)
        typ = _KIND.get((k or "").split(".")[0])
        if not typ or not _safe(name):
            continue
        if typ in ("k8s.node", "k8s.pv"):
            out.append(f"{typ}:{conn_name}/{name}")
        elif ns:
            out.append(f"{typ}:{conn_name}/{ns}/{name}")
    return out


def _alert_entities(conn, tenant_id: str, rows) -> tuple[dict, list]:
    """{thực thể: [tên cảnh báo]}, và cảnh báo không gắn được (kèm lệnh đọc trực tiếp)."""
    mapped, unmapped = {}, []
    for a in rows:
        labels = json.loads(a["labels"]) if isinstance(a["labels"], str) else a["labels"]
        found, reads = start_points(conn, tenant_id, labels)
        for f in found:
            mapped.setdefault(f, []).append(a["alertname"] or "?")
        if not found:
            unmapped.append({"alertname": a["alertname"], "verify": reads[:1]})
    return mapped, unmapped


def suspects(conn, tenant_id: str, inc, starts: list[str], walked: dict[str, set], chains: dict[str, list], alerting: set[str], t: datetime) -> dict:
    """Nghi gây lỗi: (1) thực thể mắt xích 5 Whys có căn cứ đọc tới, sâu nhất trước; (2) tầng dưới đặc hiệu của thực thể bị cảnh báo,
    gần nhất trước. Luôn là "nghi", kèm lý do; đường nối có cạnh gợi ý thì lý do ghi rõ. Không có căn cứ nào thì rỗng."""
    from .incidents import rootcause
    whys = rootcause.chain(inc)["whys"]
    for n, w in reversed(list(enumerate(whys, 1))):
        ids = [e for st in w.get("steps") or [] for e in step_entities(st["tool"], st.get("args"))]
        ids = list(_get(conn, tenant_id, ids))
        if ids:
            return {"ids": ids, "basis": "5whys", "reason": f"mắt xích 5 Whys số {n} có căn cứ đọc tới: {w['answer'][:160]}", "trusted": True}
    reach: dict[str, list] = {}
    for s in starts:
        for e in walked.get(s, set()) - {s}:
            reach.setdefault(e, []).append(s)
    alerted, checked = set(starts), []
    # Xét cả tầng dưới chỉ một thực thể của sự cố này đi tới: thực thể cùng loại đang bị cảnh báo ở sự cố khác vẫn được đếm.
    for e in sorted(reach, key=lambda e: (LAYER.get(e.split(":", 1)[0], 9), e)):
        ok, why, hit, _ = specific(conn, tenant_id, e, alerted, alerting, t)
        if len(reach[e]) < 2 and hit < 2 and not ok:
            continue
        checked.append({"id": e, "specific": ok, "why": why})
        if ok:
            trusted = all(_path_trusted(chains.get(s, []), s, e) for s in reach[e])
            reason = f"tầng dưới của {len(reach[e])} thực thể bị cảnh báo ở sự cố này; {why}"
            return {"ids": [e], "basis": "common", "reason": reason + ("" if trusted else "; có cạnh gợi ý, cần kiểm chứng"),
                    "trusted": trusted, "common": checked}
    return {"ids": [], "basis": None, "reason": "", "trusted": None, "common": checked}


def blast(conn, tenant_id: str, eid: str, t: datetime) -> dict:
    """Thứ phụ thuộc vào eid tại t, đếm theo loại, kèm project của máy ảo: gợi ý khoanh vùng ảnh hưởng cho người đánh giá S1–S4."""
    up = above(conn, tenant_id, eid, t)
    counts: dict[str, int] = {}
    for _, ty in up:
        counts[ty] = counts.get(ty, 0) + 1
    servers = [i for i, ty in up if ty == "os.server"] + ([eid] if eid.startswith("os.server:") else [])
    projects = {r["dst"] for i in range(0, len(servers), 500) for r in conn.execute(
        f"SELECT DISTINCT dst FROM topo_edge WHERE tenant_id=? AND rel='member_of' AND valid_to IS NULL AND src IN ({','.join('?' * len(servers[i:i + 500]))})",
        (tenant_id, *servers[i:i + 500]))}
    return {"entity": eid, "counts": {TYPE_LABEL.get(k, k): v for k, v in sorted(counts.items(), key=lambda x: LAYER.get(x[0], 9))},
            "projects": len(projects)}


def incident_view(conn, tenant_id: str, incident_id: str) -> dict:
    """Đồ thị theo tầng của phần bị ảnh hưởng trong bức tranh chung: thực thể của cảnh báo và tầng dưới của chúng, phần còn lại gộp
    thành nhóm có đếm, thực thể khác đang có cảnh báo vẽ riêng. Tính lúc đọc tại lúc cảnh báo bắt đầu."""
    inc = conn.execute("SELECT * FROM incident WHERE id=? AND tenant_id=?", (incident_id, tenant_id)).fetchone()
    if inc is None:
        raise TopologyError("không tìm thấy sự cố")
    last = last_scan(conn, tenant_id)
    if last is None:
        return {"available": False, "reason": "chưa quét topology (cần kết nối k8s hoặc OpenStack dùng chung)"}
    rows = conn.execute("SELECT * FROM alert_event WHERE incident_id=? ORDER BY status='firing' DESC, starts_at", (incident_id,)).fetchall()
    t = _ts(min((r["starts_at"] for r in rows if r["starts_at"]), default=None) or inc["created_at"])
    mine, unmapped = _alert_entities(conn, tenant_id, rows)
    others, other_inc = {}, {}  # thực thể đang có cảnh báo ở sự cố khác đang mở
    for r in conn.execute("SELECT a.labels, a.alertname, i.id, i.title FROM alert_event a JOIN incident i ON i.id=a.incident_id WHERE a.tenant_id=? "
                          "AND a.status='firing' AND i.status='open' AND i.id!=?", (tenant_id, incident_id)):
        for e in start_points(conn, tenant_id, json.loads(r["labels"]))[0]:
            others.setdefault(e, []).append(r["alertname"] or "?")
            other_inc.setdefault(e, {"id": r["id"], "title": r["title"]})
    starts = list(mine)
    ents = _get(conn, tenant_id, starts)
    chain, walked, chains, reads = [], {}, {}, []
    for s in starts:
        c, seen, rd = _walk(conn, tenant_id, [s], t, 5, ents)
        chain += c
        walked[s], chains[s] = seen, c
        reads += [x for x in rd if all(x["args"] != y["args"] for y in reads)]
    shown = set(starts).union(*walked.values()) if walked else set()
    sus = suspects(conn, tenant_id, inc, starts, walked, chains, set(mine) | set(others), t)
    shown |= set(sus["ids"])
    edges = {}
    for e in chain:
        edges[(e["from"], e["to"])] = {"from": e["from"], "to": e["to"], "rel": e["rel"], "trust": e["trust"], "why": e["why"]}
    # Bức tranh chung: cluster của node, và những thứ khác cùng dùng các thực thể đang vẽ (gộp thành nhóm, trừ cái đang có cảnh báo).
    aggregates, other_nodes = [], set()
    for n in [x for x in list(shown) if x.startswith("k8s.node:")]:
        cl = f"k8s.cluster:{n.split(':', 1)[1].split('/', 1)[0]}"
        shown.add(cl)
        edges[(n, cl)] = {"from": n, "to": cl, "rel": "member_of", "trust": "tin cậy", "why": ""}
    for cl in [x for x in shown if x.startswith("k8s.cluster:")]:
        rest = [r["src"] for r in conn.execute("SELECT src FROM topo_edge WHERE tenant_id=? AND dst=? AND rel='member_of' AND valid_to IS NULL", (tenant_id, cl))
                if r["src"] not in shown]
        if rest:
            aggregates.append({"id": f"agg:{cl}:k8s.node", "under": cl, "type": "k8s.node", "count": len(rest)})
    # Thực thể đang có cảnh báo ở sự cố khác mà tầng dưới gặp phần đang vẽ: vẽ cả đoạn nối, để thấy sự cố lan tới đâu.
    base = set(shown)
    for e in list(others)[:20]:
        if e in base:
            continue
        c, seen, _ = _walk(conn, tenant_id, [e], t, 5, _get(conn, tenant_id, [e]))
        reach = seen & base
        if not reach:
            continue
        changed = True
        while changed:
            changed = False
            for x in c:
                if x["to"] in reach and x["from"] not in reach:
                    reach.add(x["from"])
                    changed = True
        other_nodes.add(e)
        shown |= reach
        for x in c:
            if x["from"] in reach and x["to"] in reach:
                edges.setdefault((x["from"], x["to"]), {"from": x["from"], "to": x["to"], "rel": x["rel"], "trust": x["trust"], "why": x["why"]})
    for low in [x for x in list(shown) if x.split(":", 1)[0] in ("k8s.node", "os.server", "os.volume", "os.hypervisor", "os.backend")]:
        deps = conn.execute("SELECT src FROM topo_edge WHERE tenant_id=? AND dst=? AND rel IN ('runs_on','uses') AND valid_from<=? "
                            "AND (valid_to IS NULL OR valid_to>?)", (tenant_id, low, db.iso(t), db.iso(t))).fetchall()
        by_type: dict[str, int] = {}
        for d in deps:
            if d["src"] in shown:
                continue
            ty = d["src"].split(":", 1)[0]
            by_type[ty] = by_type.get(ty, 0) + 1
        for ty, n in by_type.items():
            aggregates.append({"id": f"agg:{low}:{ty}", "under": low, "type": ty, "count": n})
    info = _get(conn, tenant_id, shown)
    common = {c["id"]: c for c in sus.get("common") or []}
    nodes = []
    for eid in shown:
        ty = eid.split(":", 1)[0]
        r = info.get(eid)
        role = ("suspect" if eid in sus["ids"] else "alert" if eid in mine else "other_alert" if eid in other_nodes
                else "common" if eid in common else "path")
        nodes.append({"id": eid, "type": ty, "type_label": TYPE_LABEL.get(ty, ty), "layer": LAYER.get(ty, 3), "role": role,
                      "label": r["name"] if r else eid.split(":", 1)[1].rsplit("/", 1)[-1][:40], "alerts": sorted(set(mine.get(eid, []) + others.get(eid, []))),
                      "incident": other_inc.get(eid) if role == "other_alert" else None, "gone": bool(r and r["gone_at"]),
                      "note": common[eid]["why"] if eid in common and role == "common" else ""})
    for a in aggregates:
        nodes.append({"id": a["id"], "type": a["type"], "type_label": TYPE_LABEL.get(a["type"], a["type"]), "layer": LAYER.get(a["type"], 3),
                      "role": "aggregate", "label": f"+{a['count']} {TYPE_LABEL.get(a['type'], a['type'])} khác", "count": a["count"], "alerts": []})
        edges[(a["id"], a["under"])] = {"from": a["id"], "to": a["under"], "rel": "runs_on", "trust": "nhóm", "why": ""}
    return {"available": True, "at": db.iso(t), "layers": LAYER_NAMES, "nodes": sorted(nodes, key=lambda n: (n["layer"], n["id"])),
            "edges": list(edges.values()), "suspect": {k: sus[k] for k in ("ids", "basis", "reason", "trusted")},
            "blast": blast(conn, tenant_id, sus["ids"][0], t) if sus["ids"] else None,
            "unmapped": unmapped, "verify": reads[:8], "last_scan": last["started_at"],
            "skipped_layers": {f"{src}:{layer}": text for src, r in last["sources"].items() for layer, text in (r.get("layers") or {}).items() if text != "ok"}}


# ---------- gom cảnh báo theo tầng dưới chung (giai đoạn B3) ----------

def _alert_reach(conn, tenant_id: str, labels: dict, at: str | None, cache: dict) -> list[tuple[str, list, set]]:
    key = (json.dumps(labels, sort_keys=True), at)
    if key not in cache:
        t = _ts(at) or db.now()
        out = []
        for s in start_points(conn, tenant_id, labels)[0]:
            chain, seen, _ = _walk(conn, tenant_id, [s], t, 5, _get(conn, tenant_id, [s]))
            out.append((s, chain, seen))
        cache[key] = out
    return cache[key]


def _path_trusted(chain: list[dict], start: str, target: str) -> bool:
    """Mọi cạnh trên các đường từ start xuống target đều tin cậy."""
    reach, changed = {target}, True
    while changed:
        changed = False
        for e in chain:
            if e["to"] in reach and e["from"] not in reach:
                reach.add(e["from"])
                changed = True
    return start in reach and all(e["trust"] == "tin cậy" for e in chain if e["from"] in reach and e["to"] in reach)


def _name(conn, tenant_id: str, eid: str) -> str:
    r = _get(conn, tenant_id, [eid]).get(eid)
    return f"{TYPE_LABEL.get(eid.split(':', 1)[0], '')} {r['name'] if r else eid.split(':', 1)[1]}".strip()


def infra_match(conn, tenant_id: str, a: tuple[dict, str | None], others: list[tuple[dict, str | None]], cache: dict | None = None) -> tuple[bool, str] | None:
    """Cảnh báo a và một trong các cảnh báo others có cùng thực thể, hoặc cùng tầng dưới đặc hiệu: (mọi cạnh tin cậy, lý do).
    Tầng dưới chung không đặc hiệu (backend, host dùng chung bởi nhiều thực thể cùng loại) không tính, kẻo gộp cả cụm."""
    cache = {} if cache is None else cache
    if "scanned" not in cache:
        cache["scanned"] = last_scan(conn, tenant_id) is not None
    if not cache["scanned"]:
        return None
    ra = _alert_reach(conn, tenant_id, a[0], a[1], cache)
    if not ra:
        return None
    rbs = [_alert_reach(conn, tenant_id, b[0], b[1], cache) for b in others]
    alerted = {s for s, _, _ in ra} | {s for rb in rbs for s, _, _ in rb}
    t = _ts(a[1]) or db.now()
    hint = None
    for sa, ca, seen_a in ra:
        for rb in rbs:
            for sb, cb, seen_b in rb:
                if sa == sb:
                    return True, f"cùng {_name(conn, tenant_id, sa)}"
                for low in sorted((seen_a & seen_b) - {sa, sb}, key=lambda e: LAYER.get(e.split(":", 1)[0], 9)):
                    ok, why, _, _ = specific(conn, tenant_id, low, alerted, set(), t)
                    if not ok:
                        continue
                    trusted = _path_trusted(ca, sa, low) and _path_trusted(cb, sb, low)
                    reason = f"{_name(conn, tenant_id, sa)} → {_name(conn, tenant_id, low)} ← {_name(conn, tenant_id, sb)} ({why})"
                    if trusted:
                        return True, reason
                    hint = hint or (False, reason + "; có cạnh gợi ý, cần kiểm chứng")
                    break
    return hint
