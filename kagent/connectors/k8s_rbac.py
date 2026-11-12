"""Tạo ServiceAccount và RBAC trong cụm theo 3 nhóm quyền cố định của một kết nối Kubernetes.

- Thông tin xác thực của kết nối (kubeconfig/token) là tài khoản quản trị, chỉ dùng để tạo, sửa, gỡ các object dưới đây.
- Mỗi nhóm có quy tắc riêng thì có một ServiceAccount riêng; nhóm cao hơn được thừa hưởng quy tắc của nhóm thấp hơn
  (RoleBinding của nhóm đọc gắn cả SA ghi và SA quản trị).
- Namespace "*" thành ClusterRole + ClusterRoleBinding, namespace cụ thể thành Role + RoleBinding trong namespace đó.
- Mọi object mang nhãn kagent.io/connection=<id kết nối>, nên tìm lại và gỡ được kể cả khi cấu hình đã đổi.
"""
from __future__ import annotations

import base64
import re
import time

from . import k8s
from .base import ConnectorError, call

PROFILES = ("read", "write", "admin")
LABEL = {"read": "đọc", "write": "ghi", "admin": "quản trị"}
TOOL = {"read": "kubectl_read", "write": "kubectl_write", "admin": "kubectl_admin"}
TOKEN_KEY = {p: f"_sa_{p}" for p in PROFILES}
VERBS = {"read": ("get", "list", "watch"), "write": ("get", "list", "watch", "create", "update", "patch"), "admin": None}
# Đọc được những thứ này là lấy được bí mật hoặc chạy lệnh trong pod, nên chỉ nhóm quản trị (R4) được cấp.
SENSITIVE = ("secrets", "pods/exec", "pods/attach", "pods/portforward", "pods/proxy", "services/proxy", "nodes/proxy", "serviceaccounts/token",
             "certificatesigningrequests/approval")
CLUSTER_SCOPED = {"nodes", "namespaces", "persistentvolumes", "storageclasses", "clusterroles", "clusterrolebindings", "customresourcedefinitions",
                  "priorityclasses", "ingressclasses", "csidrivers", "csinodes", "volumeattachments", "mutatingwebhookconfigurations",
                  "validatingwebhookconfigurations", "apiservices", "certificatesigningrequests", "runtimeclasses"}
WORKLOADS = {"pods", "deployments", "statefulsets", "daemonsets", "replicasets", "replicationcontrollers", "jobs", "cronjobs"}
MAX_RULES = 50
DEFAULT_SA_NAMESPACE = "kagent-system"
LABEL_MANAGED = "app.kubernetes.io/managed-by"
LABEL_CONN = "kagent.io/connection"
LABEL_PROFILE = "kagent.io/profile"
RBAC = "/apis/rbac.authorization.k8s.io/v1"

_NS = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_GROUP = re.compile(r"^(|\*|[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?)$")
_RESOURCE = re.compile(r"^(\*|[a-z0-9]([-a-z0-9.]{0,61}[a-z0-9])?)(/(\*|[a-z0-9-]{1,63}))?$")
_VERB = re.compile(r"^(\*|[a-z]{1,30})$")


def default_config(namespace: str = "default") -> dict:
    return {"sa_namespace": DEFAULT_SA_NAMESPACE, "profiles": {
        "read": [{"namespaces": [namespace or "default"], "api_groups": ["", "apps", "batch"],
                  "resources": ["pods", "pods/log", "services", "endpoints", "configmaps", "events", "deployments", "replicasets",
                                "statefulsets", "daemonsets", "jobs", "cronjobs"], "verbs": ["get", "list", "watch"]}],
        "write": [], "admin": []}}


def _strings(value, what: str, pattern: re.Pattern, where: str) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise ConnectorError(f"{where}: {what} phải là danh sách")
    out = sorted({str(v).strip() for v in value if isinstance(v, str) and (v.strip() or what == "api_groups")})
    bad = [v for v in out if not pattern.match(v)]
    if bad:
        raise ConnectorError(f"{where}: {what} không hợp lệ: {', '.join(bad[:5])}")
    return out


def check_config(config) -> tuple[dict, list[str]]:
    """Chuẩn hoá cấu hình và kiểm tra giới hạn của từng nhóm. Trả về (cấu hình, cảnh báo); lỗi báo bằng ConnectorError."""
    if not isinstance(config, dict):
        raise ConnectorError("cấu hình quyền phải là object")
    sa_ns = str(config.get("sa_namespace") or DEFAULT_SA_NAMESPACE).strip()
    if not _NS.match(sa_ns):
        raise ConnectorError(f"sa_namespace không hợp lệ: {sa_ns}")
    raw = config.get("profiles") or {}
    if not isinstance(raw, dict) or set(raw) - set(PROFILES):
        raise ConnectorError(f"profiles chỉ gồm {', '.join(PROFILES)}")
    profiles, warnings = {}, []
    for profile in PROFILES:
        rules = raw.get(profile) or []
        if not isinstance(rules, list) or len(rules) > MAX_RULES:
            raise ConnectorError(f"nhóm {LABEL[profile]}: tối đa {MAX_RULES} quy tắc")
        out = []
        for i, rule in enumerate(rules, 1):
            where = f"nhóm {LABEL[profile]}, quy tắc {i}"
            if not isinstance(rule, dict):
                raise ConnectorError(f"{where}: phải là object")
            namespaces = _strings(rule.get("namespaces"), "namespaces", re.compile(r"^(\*|[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$"), where)
            groups = _strings(rule.get("api_groups", [""]), "api_groups", _GROUP, where)
            resources = _strings(rule.get("resources"), "resources", _RESOURCE, where)
            verbs = _strings(rule.get("verbs"), "verbs", _VERB, where)
            if not namespaces or not resources or not verbs:
                raise ConnectorError(f"{where}: cần ít nhất một namespace, một resource và một verb")
            if not groups:
                groups = [""]
            if "*" in namespaces and len(namespaces) > 1:
                raise ConnectorError(f"{where}: namespace * đã bao gồm mọi namespace, không ghi kèm namespace khác")
            allowed = VERBS[profile]
            if allowed is not None:
                extra = [v for v in verbs if v not in allowed]
                if extra:
                    raise ConnectorError(f"{where}: nhóm {LABEL[profile]} chỉ được các verb {', '.join(allowed)}; "
                                         f"{', '.join(extra)} thuộc nhóm cao hơn")
                wild = [r for r in resources if "*" in r]
                if wild:
                    raise ConnectorError(f"{where}: resource * gồm cả secrets và pods/exec, chỉ nhóm quản trị được dùng; hãy liệt kê resource cụ thể")
                secret = [r for r in resources if r in SENSITIVE]
                if secret:
                    raise ConnectorError(f"{where}: {', '.join(secret)} lộ bí mật hoặc chạy lệnh trong pod, chỉ nhóm quản trị được cấp")
            scoped = [r for r in resources if r.split("/")[0] in CLUSTER_SCOPED]
            if scoped and "*" not in namespaces:
                warnings.append(f"{where}: {', '.join(scoped)} là tài nguyên cấp cụm, chỉ có hiệu lực khi namespace là *")
            if profile == "write" and set(verbs) & {"create", "update", "patch"} and {r.split("/")[0] for r in resources} & WORKLOADS:
                warnings.append(f"{where}: tạo hoặc sửa workload cho phép chạy pod với ServiceAccount bất kỳ trong namespace đó, "
                                "tức gần ngang quyền của các ServiceAccount ấy")
            if ("rbac.authorization.k8s.io" in groups or "*" in groups) and profile != "read":
                warnings.append(f"{where}: có thể chạm tới Role/RoleBinding; Kubernetes vẫn chặn cấp quyền vượt quá quyền đang có")
            if profile == "admin" and ("*" in resources or "*" in verbs):
                warnings.append(f"{where}: nhóm quản trị có toàn quyền trên {', '.join(namespaces)}")
            out.append({"namespaces": namespaces, "api_groups": groups, "resources": resources, "verbs": verbs})
        profiles[profile] = out
    return {"sa_namespace": sa_ns, "profiles": profiles}, warnings


def base_name(connection_id: str, connection_name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", connection_name.lower()).strip("-")[:30] or "conn"
    return f"kagent-{slug}-{connection_id[:8]}"


def _meta(name: str, connection_id: str, profile: str | None = None, namespace: str | None = None, annotations: dict | None = None) -> dict:
    labels = {LABEL_MANAGED: "kagent", LABEL_CONN: connection_id, **({LABEL_PROFILE: profile} if profile else {})}
    return {"name": name, **({"namespace": namespace} if namespace else {}), "labels": labels, **({"annotations": annotations} if annotations else {})}


def _k8s_rule(rule: dict) -> dict:
    return {"apiGroups": rule["api_groups"], "resources": rule["resources"], "verbs": rule["verbs"]}


def desired(connection_id: str, connection_name: str, cfg: dict) -> list[dict]:
    """Các object cần có trong cụm, theo thứ tự tạo: SA, Secret token, Role/ClusterRole, Binding."""
    base, sa_ns = base_name(connection_id, connection_name), cfg["sa_namespace"]
    active = [p for p in PROFILES if cfg["profiles"][p]]
    accounts, roles, bindings = [], [], []
    for p in active:
        sa = f"{base}-{p}"
        accounts.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(sa, connection_id, p, sa_ns)})
        accounts.append({"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/service-account-token",
                         "metadata": _meta(f"{sa}-token", connection_id, p, sa_ns, {"kubernetes.io/service-account.name": sa})})
    for i, p in enumerate(PROFILES):
        if not cfg["profiles"][p]:
            continue
        subjects = [{"kind": "ServiceAccount", "name": f"{base}-{q}", "namespace": sa_ns} for q in PROFILES[i:] if q in active]
        by_ns: dict[str, list[dict]] = {}
        for rule in cfg["profiles"][p]:
            for ns in rule["namespaces"]:
                by_ns.setdefault(ns, []).append(_k8s_rule(rule))
        name = f"{base}-{p}"
        for ns, rules in sorted(by_ns.items()):
            cluster = ns == "*"
            kind = "ClusterRole" if cluster else "Role"
            roles.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": kind, "metadata": _meta(name, connection_id, p, None if cluster else ns),
                          "rules": rules})
            bindings.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": kind + "Binding",
                             "metadata": _meta(name, connection_id, p, None if cluster else ns),
                             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": name}, "subjects": subjects})
    return accounts + roles + bindings


def _path(kind: str, name: str, namespace: str | None = None) -> str:
    return {
        "ServiceAccount": f"/api/v1/namespaces/{namespace}/serviceaccounts/{name}",
        "Secret": f"/api/v1/namespaces/{namespace}/secrets/{name}",
        "Role": f"{RBAC}/namespaces/{namespace}/roles/{name}",
        "RoleBinding": f"{RBAC}/namespaces/{namespace}/rolebindings/{name}",
        "ClusterRole": f"{RBAC}/clusterroles/{name}",
        "ClusterRoleBinding": f"{RBAC}/clusterrolebindings/{name}",
    }[kind]


LISTS = (("ServiceAccount", "/api/v1/serviceaccounts"), ("Secret", "/api/v1/secrets"), ("Role", f"{RBAC}/roles"),
         ("RoleBinding", f"{RBAC}/rolebindings"), ("ClusterRole", f"{RBAC}/clusterroles"), ("ClusterRoleBinding", f"{RBAC}/clusterrolebindings"))


def _key(obj: dict) -> tuple:
    return obj["kind"], obj["metadata"].get("namespace"), obj["metadata"]["name"]


def existing(c, connection_id: str) -> dict[tuple, dict]:
    out = {}
    for kind, path in LISTS:
        data = call(c, "GET", path, params={"labelSelector": f"{LABEL_CONN}={connection_id}", "limit": 500})
        for item in data.get("items", []):
            item = {**item, "kind": kind}
            out[_key(item)] = item
    return out


def _norm_rules(rules) -> list:
    return sorted((tuple(sorted(r.get("apiGroups") or [])), tuple(sorted(r.get("resources") or [])), tuple(sorted(r.get("verbs") or [])),
                   tuple(sorted(r.get("resourceNames") or []))) for r in rules or [])


def _differs(want: dict, have: dict) -> bool:
    labels = have.get("metadata", {}).get("labels") or {}
    if any(labels.get(k) != v for k, v in want["metadata"]["labels"].items()):
        return True
    kind = want["kind"]
    if kind in ("Role", "ClusterRole"):
        return _norm_rules(want["rules"]) != _norm_rules(have.get("rules"))
    if kind.endswith("Binding"):
        subjects = lambda s: sorted((x.get("kind"), x.get("name"), x.get("namespace")) for x in s or [])
        return want["roleRef"] != have.get("roleRef") or subjects(want["subjects"]) != subjects(have.get("subjects"))
    if kind == "Secret":
        return (have.get("metadata", {}).get("annotations") or {}).get("kubernetes.io/service-account.name") != \
            want["metadata"]["annotations"]["kubernetes.io/service-account.name"]
    return False


def _summary(obj: dict) -> str:
    kind = obj["kind"]
    if kind in ("Role", "ClusterRole"):
        return "; ".join(f"{','.join(r['verbs'])} {','.join(r['resources'])}" + (f" ({','.join(g or 'core' for g in r['apiGroups'])})")
                         for r in obj.get("rules") or [])
    if kind.endswith("Binding"):
        return f"{obj['roleRef']['name']} → " + ", ".join(s["name"] for s in obj.get("subjects") or [])
    return ""


def _apply(c, obj: dict, dry_run: bool = False) -> None:
    meta = obj["metadata"]
    params = {"fieldManager": "kagent", "force": "true", **({"dryRun": "All"} if dry_run else {})}
    call(c, "PATCH", _path(obj["kind"], meta["name"], meta.get("namespace")), params=params, json_body=obj,
         headers={"Content-Type": "application/apply-patch+yaml"})


def _delete(c, kind: str, namespace: str | None, name: str) -> None:
    call(c, "DELETE", _path(kind, name, namespace), ok=(200, 202, 404))


def _namespace_exists(c, ns: str) -> bool:
    status, _, _ = call(c, "GET", f"/api/v1/namespaces/{ns}", with_status=True)
    if status == 404:
        return False
    if status != 200:
        raise ConnectorError(f"không kiểm tra được namespace {ns} (HTTP {status}); tài khoản của kết nối cần quyền get namespaces")
    return True


def plan(p: dict, connection_id: str, connection_name: str, config, validate: bool = True) -> dict:
    """So sánh cấu hình với cụm. validate=True chạy thử từng thay đổi (dryRun=All) để bắt lỗi như cấp quyền vượt quyền đang có."""
    cfg, warnings = check_config(config)
    want = desired(connection_id, connection_name, cfg)
    items, errors = [], []
    with k8s._client(p) as c:
        have = existing(c, connection_id)
        namespaces = sorted({o["metadata"]["namespace"] for o in want if o["metadata"].get("namespace")})
        missing = {ns for ns in namespaces if not _namespace_exists(c, ns)}
        if cfg["sa_namespace"] in missing and want:
            items.append({"action": "create", "kind": "Namespace", "name": cfg["sa_namespace"], "namespace": None, "summary": "chứa ServiceAccount của kagent"})
        for obj in want:
            key = _key(obj)
            cur = have.pop(key, None)
            action = "create" if cur is None else "update" if _differs(obj, cur) else "unchanged"
            item = {"action": action, "kind": key[0], "namespace": key[1], "name": key[2], "summary": _summary(obj)}
            ns = key[1]
            if ns in missing and ns != cfg["sa_namespace"]:
                item["error"] = f"namespace {ns} không tồn tại"
            elif validate and action != "unchanged" and ns not in missing:
                try:
                    _apply(c, obj, dry_run=True)
                except ConnectorError as exc:
                    item["error"] = str(exc)
            items.append(item)
        for (kind, ns, name), obj in sorted(have.items(), key=lambda kv: str(kv[0])):
            items.append({"action": "delete", "kind": kind, "namespace": ns, "name": name, "summary": _summary(obj)})
    errors = [f"{i['kind']} {(i['namespace'] + '/') if i['namespace'] else ''}{i['name']}: {i['error']}" for i in items if i.get("error")]
    return {"config": cfg, "items": items, "warnings": warnings, "errors": errors,
            "changes": sum(1 for i in items if i["action"] != "unchanged"),
            "profiles": {q: bool(cfg["profiles"][q]) for q in PROFILES}}


def _token(c, namespace: str, name: str) -> str:
    """Token controller điền token vào Secret sau khi tạo, nên chờ một chút."""
    for _ in range(40):
        data = call(c, "GET", f"/api/v1/namespaces/{namespace}/secrets/{name}").get("data") or {}
        if data.get("token"):
            return base64.b64decode(data["token"]).decode()
        time.sleep(0.25)
    raise ConnectorError(f"Secret {namespace}/{name} chưa có token sau 10 giây; kiểm tra token controller của cụm")


def apply(p: dict, connection_id: str, connection_name: str, config) -> tuple[dict, dict]:
    """Áp cấu hình vào cụm. Trả về (kết quả, token theo nhóm); nhóm không còn quy tắc có token None."""
    result = plan(p, connection_id, connection_name, config, validate=False)
    if result["errors"]:
        raise ConnectorError("không áp dụng được: " + "; ".join(result["errors"]))
    cfg = result["config"]
    want = desired(connection_id, connection_name, cfg)
    tokens: dict[str, str | None] = {q: None for q in PROFILES}
    with k8s._client(p) as c:
        if any(i["kind"] == "Namespace" for i in result["items"]):
            call(c, "POST", "/api/v1/namespaces", json_body={"apiVersion": "v1", "kind": "Namespace",
                                                             "metadata": {"name": cfg["sa_namespace"], "labels": {LABEL_MANAGED: "kagent"}}},
                 ok=(200, 201, 409))
        for obj in want:
            _apply(c, obj)
        for i in result["items"]:
            if i["action"] == "delete":
                _delete(c, i["kind"], i["namespace"], i["name"])
        for obj in want:
            if obj["kind"] == "Secret":
                tokens[obj["metadata"]["labels"][LABEL_PROFILE]] = _token(c, cfg["sa_namespace"], obj["metadata"]["name"])
    return result, tokens


def remove(p: dict, connection_id: str) -> int:
    """Gỡ mọi object kagent đã tạo cho kết nối. Namespace chứa SA được giữ vì có thể dùng chung."""
    with k8s._client(p) as c:
        have = existing(c, connection_id)
        for kind, ns, name in sorted(have, key=lambda k: ("Binding" not in k[0], str(k))):
            _delete(c, kind, ns, name)
    return len(have)
