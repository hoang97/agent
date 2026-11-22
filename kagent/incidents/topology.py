"""Topology k8s thường trực để gom cảnh báo: Service chọn workload nào, ingress trỏ tới Service nào, workload gọi Service nào.

Như lấy cảnh báo (ingest.fetch), kagent tự đọc bằng quyền nhóm đọc của kết nối Kubernetes, không đi qua gateway: đây là việc nền
định kỳ, không có người dùng để auto mode xét ý định, và qua gateway thì ô R1 ở prod sẽ gọi classifier hoặc tạo thẻ duyệt mỗi lần.
Mỗi lần đọc vẫn ghi audit. Chỉ đọc các namespace từng có cảnh báo trong 7 ngày, mỗi namespace một lệnh.

Cạnh "gọi" suy ra từ biến môi trường của container (vd. CURRENCY_SERVICE_ADDR=currencyservice:7000). Giá trị biến chỉ được đọc
trong bộ nhớ để tìm tên Service; DB chỉ lưu cạnh.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import timedelta

from .. import db
from .scoring import Topology

log = logging.getLogger("kagent.incidents")

KINDS = "svc,deploy,sts,ds,ingress"
JSONPATH = ('{range .items[*]}{.kind}{"\\t"}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}{.spec.selector}{"\\t"}'
            '{.spec.template.metadata.labels}{"\\t"}{.spec.defaultBackend.service.name} {.spec.rules[*].http.paths[*].backend.service.name}'
            '{"\\t"}{.spec.template.spec.containers[*].env[*].value}{"\\n"}{end}')
RECORD_KINDS = {"Service": "svc", "Deployment": "workload", "StatefulSet": "workload", "DaemonSet": "workload", "Ingress": "ingress"}
MAX_NAMESPACES = 30
SCOPE_DAYS = 7
_running: set[str] = set()
_lock = threading.Lock()
_status: dict[str, dict] = {}  # tenant → {"at": giây lần chạy cuối, env: trạng thái lần đọc cuối}


def command(namespace: str) -> str:
    return f"get {KINDS} -n {namespace} -o 'jsonpath={JSONPATH}'"


def _labels(text: str) -> dict:
    """Map nhãn do jsonpath in ra: JSON ({"app":"x"}) ở kubectl mới, map[app:x] ở bản cũ."""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    m = re.fullmatch(r"map\[(.*)\]", text)
    return dict(p.split(":", 1) for p in m.group(1).split() if ":" in p) if m else {}


def parse(text: str) -> list[dict]:
    """Mỗi đối tượng một dòng 7 cột phân cách bằng tab; giá trị biến môi trường có thể xuống dòng nên dòng lạ nối vào cột cuối."""
    items: list[dict] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 7 and cols[0] in RECORD_KINDS:
            kind, ns, name, selector, labels, backends = cols[:6]
            items.append({"kind": RECORD_KINDS[kind], "ns": ns, "name": name, "selector": _labels(selector), "labels": _labels(labels),
                          "backends": backends.split(), "env": "\t".join(cols[6:])})
        elif items:
            items[-1]["env"] += "\n" + line
    return items


_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")


def edges(items: list[dict], namespaces: set[str]) -> list[tuple[str, str, str]]:
    """namespaces: các namespace đã đọc. Tên "svc.ns" chỉ nhận khi ns nằm trong đó hoặc có hậu tố .svc (tránh nhầm tên miền ngoài)."""
    services = {(i["ns"], i["name"]): i for i in items if i["kind"] == "svc"}
    out: set[tuple[str, str, str]] = set()
    for svc in services.values():
        if not svc["selector"]:
            continue
        for w in items:
            if w["kind"] == "workload" and w["ns"] == svc["ns"] and w["labels"] and all(w["labels"].get(k) == v for k, v in svc["selector"].items()):
                out.add((f"{svc['ns']}/svc/{svc['name']}", f"{w['ns']}/workload/{w['name']}", "selects"))
    for ing in items:
        if ing["kind"] == "ingress":
            for name in set(ing["backends"]):
                if (ing["ns"], name) in services:
                    out.add((f"{ing['ns']}/ingress/{ing['name']}", f"{ing['ns']}/svc/{name}", "routes"))
    for w in items:
        if w["kind"] != "workload" or not w["env"]:
            continue
        for host in set(_HOST.findall(w["env"].lower())):
            parts = host.split(".")
            target = None
            if len(parts) == 1 and (w["ns"], host) in services:
                target = (w["ns"], host)
            elif len(parts) >= 2 and (parts[1] in namespaces or parts[2:3] == ["svc"]):
                if parts[2:] in ([], ["svc"], ["svc", "cluster", "local"]):
                    target = (parts[1], parts[0])
            if target and target != (w["ns"], w["name"]):
                out.add((f"{w['ns']}/workload/{w['name']}", f"{target[0]}/svc/{target[1]}", "calls"))
    return sorted(out)


def scope(conn, tenant_id: str, env: str) -> list[str]:
    since = db.iso(db.now() - timedelta(days=SCOPE_DAYS))
    rows = conn.execute("SELECT json_extract(labels, '$.namespace') ns, max(first_seen) last FROM alert_event WHERE tenant_id=? AND env=? AND first_seen>=? "
                        "AND json_extract(labels, '$.namespace') IS NOT NULL GROUP BY ns ORDER BY last DESC", (tenant_id, env, since)).fetchall()
    return [r["ns"] for r in rows if re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", r["ns"] or "")][:MAX_NAMESPACES]


def connections_for(conn, tenant_id: str, env: str) -> list[dict]:
    from .. import connections
    rows = conn.execute("SELECT * FROM connection WHERE tenant_id=? AND template='k8s' AND status='active' AND scope='tenant' ORDER BY name",
                        (tenant_id,)).fetchall()
    return [r for r in (connections._row(r) for r in rows) if env in r["envs"]]


def refresh(conn, tenant_id: str, env: str) -> dict:
    """Đọc lại topology của một môi trường và thay toàn bộ cạnh đã lưu. Lỗi ở một namespace không chặn namespace khác."""
    from .. import connections, gateway, net
    from ..connectors import kubectl
    from ..connectors.base import ConnectorError
    from .config import bot_user
    status = {"at": db.iso(), "edges": 0, "namespaces": [], "errors": [], "truncated": []}
    conns = connections_for(conn, tenant_id, env)
    namespaces = scope(conn, tenant_id, env)
    if not conns:
        status["errors"].append(f"không có kết nối Kubernetes dùng chung ở môi trường {env}")
    elif not namespaces:
        status["errors"].append("chưa có cảnh báo nào có nhãn namespace trong 7 ngày")
    else:
        with db.tx(conn):
            user = bot_user(conn, tenant_id)
        items: list[dict] = []
        for row in conns:
            params = connections.params_of(row)
            ctx = gateway.CallContext(tenant_id, user, env, "Cập nhật topology để gom cảnh báo")
            tool = f"{row['name']}.kubectl_read"
            for ns in namespaces:
                args = {"command": command(ns)}
                try:
                    out = kubectl.run_read(params, args)["stdout"]
                    gateway.audit(conn, ctx, tool, args, "R1", "auto", ["kagent đọc topology để gom cảnh báo"], "ok")
                except (ConnectorError, net.NetError) as exc:
                    gateway.audit(conn, ctx, tool, args, "R1", "auto", ["kagent đọc topology để gom cảnh báo"], "error")
                    status["errors"].append(f"{row['name']} · {ns}: {str(exc)[:200]}")
                    continue
                if "…(đã cắt" in out[-200:]:
                    status["truncated"].append(ns)
                items += parse(out)
        found = edges(items, set(namespaces))
        with db.tx(conn):
            conn.execute("DELETE FROM topology_edge WHERE tenant_id=? AND env=?", (tenant_id, env))
            conn.executemany("INSERT OR IGNORE INTO topology_edge(tenant_id,env,src,dst,kind,seen_at) VALUES (?,?,?,?,?,?)",
                             [(tenant_id, env, s, d, k, status["at"]) for s, d, k in found])
        status.update(edges=len(found), namespaces=namespaces)
        from .grouping import reapply_roles
        reapply_roles(conn, tenant_id, env)
    _status.setdefault(tenant_id, {})[env] = status
    return status


def load(conn, tenant_id: str, env: str) -> Topology:
    return Topology((r["src"], r["dst"], r["kind"]) for r in conn.execute(
        "SELECT src, dst, kind FROM topology_edge WHERE tenant_id=? AND env=?", (tenant_id, env)).fetchall())


def envs(conn, tenant_id: str) -> list[str]:
    since = db.iso(db.now() - timedelta(days=SCOPE_DAYS))
    return [r["env"] for r in conn.execute("SELECT DISTINCT env FROM alert_event WHERE tenant_id=? AND first_seen>=? ORDER BY env",
                                           (tenant_id, since)).fetchall()]


def due(tenant_id: str, minutes: int) -> bool:
    if minutes <= 0:
        return False
    last = (_status.get(tenant_id) or {}).get("at", 0.0)
    return db.now().timestamp() - last >= minutes * 60


def start_refresh(tenant_id: str) -> bool:
    """Cập nhật topology mọi môi trường có cảnh báo, ở luồng nền để vòng poll không phải chờ kubectl."""
    with _lock:
        if tenant_id in _running:
            return False
        _running.add(tenant_id)
    _status.setdefault(tenant_id, {})["at"] = db.now().timestamp()

    def work():
        try:
            with db.session() as conn:
                for env in envs(conn, tenant_id):
                    refresh(conn, tenant_id, env)
        except Exception:
            log.exception("cập nhật topology của tenant %s lỗi", tenant_id)
        finally:
            with _lock:
                _running.discard(tenant_id)

    threading.Thread(target=work, name=f"topology-{tenant_id[:8]}", daemon=True).start()
    return True


def status(conn, tenant_id: str) -> dict:
    counts = {r["env"]: r["n"] for r in conn.execute("SELECT env, count(*) n FROM topology_edge WHERE tenant_id=? GROUP BY env", (tenant_id,)).fetchall()}
    last = {k: v for k, v in (_status.get(tenant_id) or {}).items() if k != "at"}
    return {"running": tenant_id in _running, "envs": {env: {**(last.get(env) or {}), "edges": counts.get(env, 0)} for env in sorted(set(counts) | set(last))}}
