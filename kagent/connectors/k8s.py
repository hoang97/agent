"""Kubernetes qua REST API của API server, xác thực bằng token ServiceAccount hoặc kubeconfig."""
from __future__ import annotations

import base64
import binascii
import json
import shlex
import socket
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import yaml

from .. import net
from .base import NAME, ConnectorError, as_list, call, clip, integer, need

FLATTEN = "kubectl config view --raw --minify --flatten"


def _b64(value, what: str) -> str:
    try:
        return base64.b64decode(str(value), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ConnectorError(f"kubeconfig: {what} không phải base64 hợp lệ") from exc


def _named(doc: dict, kind: str, name) -> dict:
    for entry in doc.get(kind + "s") or []:
        if isinstance(entry, dict) and entry.get("name") == name and isinstance(entry.get(kind), dict):
            return entry[kind]
    raise ConnectorError(f"kubeconfig không có {kind} '{name}'")


def _kubeconfig(p) -> dict:
    """Rút server, CA và thông tin xác thực từ kubeconfig. Không chạy lệnh ngoài, không đọc file trên máy chủ."""
    raw = p.get("kubeconfig")
    if not raw:
        return {}
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConnectorError("kubeconfig không phải YAML hợp lệ") from exc
    if not isinstance(doc, dict):
        raise ConnectorError("kubeconfig không phải YAML hợp lệ")
    name = p.get("context") or doc.get("current-context")
    if not name:
        contexts = [c.get("name") for c in doc.get("contexts") or [] if isinstance(c, dict)]
        if len(contexts) != 1:
            raise ConnectorError("kubeconfig không có current-context; hãy điền tham số context")
        name = contexts[0]
    ctx = _named(doc, "context", name)
    cluster, user = _named(doc, "cluster", ctx.get("cluster")), _named(doc, "user", ctx.get("user"))
    for key in ("certificate-authority", "client-certificate", "client-key", "tokenFile"):
        if cluster.get(key) or user.get(key):
            raise ConnectorError(f"kubeconfig tham chiếu file trên máy ({key}); hãy xuất bản nhúng sẵn bằng: {FLATTEN}")
    if user.get("exec") or user.get("auth-provider"):
        raise ConnectorError("kubeconfig lấy token bằng lệnh ngoài (exec/auth-provider, ví dụ aws, gcloud, oidc), "
                             "máy chủ kagent không chạy lệnh đó; hãy dùng token ServiceAccount hoặc chứng chỉ client")
    out = {"api_url": cluster.get("server"), "verify_tls": not cluster.get("insecure-skip-tls-verify", False)}
    if cluster.get("certificate-authority-data"):
        out["ca_cert"] = _b64(cluster["certificate-authority-data"], "certificate-authority-data")
    if user.get("token"):
        out["token"] = str(user["token"])
    if user.get("client-certificate-data") and user.get("client-key-data"):
        out["client_cert"] = (_b64(user["client-certificate-data"], "client-certificate-data"), _b64(user["client-key-data"], "client-key-data"))
    if not out.get("token") and not out.get("client_cert"):
        raise ConnectorError(f"kubeconfig: user '{ctx.get('user')}' không có token hay chứng chỉ client nhúng sẵn")
    return out


def _conn(p) -> dict:
    """Tham số kết nối thực tế. api_url, token, ca_cert điền tay được ưu tiên hơn giá trị trong kubeconfig."""
    kc = _kubeconfig(p)
    api_url = p.get("api_url") or kc.get("api_url")
    if not api_url:
        raise ConnectorError("cần api_url hoặc kubeconfig")
    token = p.get("token") or kc.get("token")
    client_cert = None if p.get("token") else kc.get("client_cert")
    if not token and not client_cert:
        raise ConnectorError("cần token hoặc kubeconfig có thông tin xác thực")
    return {"api_url": api_url, "token": token, "client_cert": client_cert, "ca_cert": p.get("ca_cert") or kc.get("ca_cert"),
            "verify_tls": p.get("verify_tls", True) and kc.get("verify_tls", True), "allow_private": p.get("_allow_private", False)}


def validate_params(p) -> None:
    c = _conn(p)
    net.check_url(c["api_url"], c["allow_private"])


IDENTITIES = ("read", "write", "admin")


def _identity(p, role: str | None, c: dict) -> tuple[str | None, tuple | None]:
    """Đã áp dụng quyền Kubernetes thì công cụ chạy bằng SA của nhóm thấp nhất đủ cho nó; chưa áp dụng thì dùng thông tin của kết nối."""
    if role is None or not any(p.get(f"_sa_{r}") for r in IDENTITIES):
        return c["token"], c["client_cert"]
    for r in IDENTITIES[IDENTITIES.index(role):]:
        if p.get(f"_sa_{r}"):
            return p[f"_sa_{r}"], None
    raise ConnectorError(f"kết nối đã áp dụng quyền Kubernetes nhưng chưa có nhóm {role} trở lên cho công cụ này")


def _client(p, role: str | None = None):
    c = _conn(p)
    token, client_cert = _identity(p, role, c)
    headers = {"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    return net.client(c["api_url"], allow_private=c["allow_private"], verify=c["verify_tls"], ca_pem=c["ca_cert"] or None,
                      client_cert=client_cert, headers=headers)


def _ns(p, args) -> str:
    ns = args.get("namespace") or p.get("namespace") or "default"
    ns = need({"namespace": ns}, "namespace", NAME)
    allowed = as_list(p.get("allowed_namespaces"))
    if allowed and ns not in allowed:
        raise ConnectorError(f"namespace {ns} không nằm trong danh sách được phép của kết nối: {', '.join(allowed)}")
    return ns


def ping(p, args=None):
    with _client(p) as c:
        version = call(c, "GET", "/version")
        call(c, "GET", f"/api/v1/namespaces/{_ns(p, {})}/pods", params={"limit": 1})
    out = {"ok": True, "version": version.get("gitVersion")}
    identities = {}
    for r in IDENTITIES:
        if not p.get(f"_sa_{r}"):
            continue
        try:
            with _client({**p, **{f"_sa_{q}": None for q in IDENTITIES if q != r}}, r) as c:
                review = call(c, "POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews", ok=(200, 201),
                              json_body={"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
            identities[r] = review.get("status", {}).get("userInfo", {}).get("username") or "ok"
        except ConnectorError as exc:
            identities[r] = f"lỗi: {exc}"
    if identities:
        out["identities"] = identities
    return out


def list_pods(p, args):
    ns = _ns(p, args)
    with _client(p, "read") as c:
        data = call(c, "GET", f"/api/v1/namespaces/{ns}/pods", params={"limit": 200})
    pods = []
    for item in data.get("items", []):
        statuses = item.get("status", {}).get("containerStatuses") or []
        pods.append({"name": item["metadata"]["name"], "phase": item.get("status", {}).get("phase"),
                     "ready": f"{sum(1 for s in statuses if s.get('ready'))}/{len(statuses)}",
                     "restarts": sum(s.get("restartCount", 0) for s in statuses), "node": item.get("spec", {}).get("nodeName")})
    return {"namespace": ns, "pods": pods}


def list_deployments(p, args):
    ns = _ns(p, args)
    with _client(p, "read") as c:
        data = call(c, "GET", f"/apis/apps/v1/namespaces/{ns}/deployments", params={"limit": 200})
    out = []
    for item in data.get("items", []):
        spec, status = item.get("spec", {}), item.get("status", {})
        images = [ct.get("image") for ct in spec.get("template", {}).get("spec", {}).get("containers", [])]
        out.append({"name": item["metadata"]["name"], "replicas": spec.get("replicas"), "ready": status.get("readyReplicas", 0), "images": images})
    return {"namespace": ns, "deployments": out}


def pod_logs(p, args):
    ns, pod = _ns(p, args), need(args, "pod", NAME)
    query = {"tailLines": integer(args, "tail_lines", 200, 1, 2000)}
    if args.get("container"):
        query["container"] = need(args, "container", NAME)
    with _client(p, "read") as c:
        text = call(c, "GET", f"/api/v1/namespaces/{ns}/pods/{pod}/log", params=query, as_text=True)
    return {"namespace": ns, "pod": pod, "log": clip(text)}


def _deployment(c, ns: str, name: str) -> dict:
    return call(c, "GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")


def _scale_args(p, args):
    ns, name = _ns(p, args), need(args, "deployment", NAME)
    return ns, name, integer(args, "replicas", -1, 0, int(p.get("max_replicas") or 50), clamp=False)


def scale_check(p, args):
    ns, name, replicas = _scale_args(p, args)
    with _client(p, "write") as c:
        current = _deployment(c, ns, name).get("spec", {}).get("replicas")
    return {"would_change": f"{ns}/{name}: {current} → {replicas}", "objects": 1}


def scale(p, args):
    ns, name, replicas = _scale_args(p, args)
    with _client(p, "write") as c:
        before = _deployment(c, ns, name).get("spec", {}).get("replicas")
        call(c, "PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale", json_body={"spec": {"replicas": replicas}},
             headers={"Content-Type": "application/merge-patch+json"})
    return {"namespace": ns, "deployment": name, "from": before, "to": replicas}


def restart_check(p, args):
    ns, name = _ns(p, args), need(args, "deployment", NAME)
    with _client(p, "write") as c:
        dep = _deployment(c, ns, name)
    return {"would_restart": f"{ns}/{name}", "pods": dep.get("spec", {}).get("replicas"), "objects": 1}


def rollout_restart(p, args):
    ns, name = _ns(p, args), need(args, "deployment", NAME)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
    with _client(p, "write") as c:
        call(c, "PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", json_body=patch,
             headers={"Content-Type": "application/strategic-merge-patch+json"})
    return {"restarted": f"{ns}/{name}", "at": stamp}


def delete_pod_check(p, args):
    ns, pod = _ns(p, args), need(args, "pod", NAME)
    with _client(p, "admin") as c:
        item = call(c, "GET", f"/api/v1/namespaces/{ns}/pods/{pod}")
    owner = (item.get("metadata", {}).get("ownerReferences") or [{}])[0].get("kind")
    return {"would_delete": f"{ns}/{pod}", "owner": owner, "objects": 1}


def delete_pod(p, args):
    ns, pod = _ns(p, args), need(args, "pod", NAME)
    with _client(p, "admin") as c:
        call(c, "DELETE", f"/api/v1/namespaces/{ns}/pods/{pod}")
    return {"deleted": f"{ns}/{pod}"}


def exec_command(p, args):
    ns, pod = _ns(p, args), need(args, "pod", NAME)
    try:
        argv = shlex.split(need(args, "command"))
    except ValueError as exc:
        raise ConnectorError(f"lệnh không hợp lệ: {exc}") from exc
    container = need(args, "container", NAME) if args.get("container") else None
    return _exec_ws(p, ns, pod, container, argv, timeout=integer(args, "timeout_seconds", 30, 1, 120))


def _exec_ws(p, ns: str, pod: str, container: str | None, argv: list[str], timeout: int) -> dict:
    """Exec qua WebSocket (kênh v4.channel.k8s.io): byte đầu là số kênh, 1 stdout, 2 stderr, 3 trạng thái."""
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    c = _conn(p)
    c["token"], c["client_cert"] = _identity(p, "admin", c)
    base = net.check_url(c["api_url"], c["allow_private"])
    ip = net.check_host(base.host, base.port or net.default_port(base.scheme), c["allow_private"])
    query = [("command", a) for a in argv] + [("stdout", "true"), ("stderr", "true")] + ([("container", container)] if container else [])
    scheme = "wss" if base.scheme == "https" else "ws"
    path = f"/api/v1/namespaces/{quote(ns)}/pods/{quote(pod)}/exec?{urlencode(query)}"
    sock = socket.create_connection((ip, base.port or net.default_port(base.scheme)), timeout=10)
    kwargs = {"ssl": net.tls_context(c["verify_tls"], c["ca_cert"] or None, c["client_cert"]), "server_hostname": base.host} if scheme == "wss" else {}
    out, err, status = [], [], None
    deadline = time.monotonic() + timeout
    try:
        with connect(f"{scheme}://{base.netloc.decode()}{path}", sock=sock, subprotocols=["v4.channel.k8s.io"], open_timeout=10, close_timeout=2,
                     max_size=2 ** 20, additional_headers={"Authorization": f"Bearer {c['token']}"} if c["token"] else {}, **kwargs) as ws:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    message = ws.recv(timeout=remaining)
                except TimeoutError:
                    break
                except ConnectionClosed:
                    break
                data = message.encode() if isinstance(message, str) else message
                if not data:
                    continue
                channel, body = data[0], data[1:].decode("utf-8", "replace")
                (out if channel == 1 else err if channel == 2 else []).append(body)
                if channel == 3:
                    try:
                        status = json.loads(body)
                    except json.JSONDecodeError:
                        status = {"status": body}
    except OSError as exc:
        raise ConnectorError(f"không mở được phiên exec: {exc.__class__.__name__}") from exc
    return {"pod": f"{ns}/{pod}", "command": argv, "stdout": clip("".join(out)), "stderr": clip("".join(err), 5000),
            "status": (status or {}).get("status"), "timed_out": time.monotonic() >= deadline}
