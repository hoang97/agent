"""Kubernetes qua REST API của API server, xác thực bằng token ServiceAccount."""
from __future__ import annotations

import json
import shlex
import socket
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from .. import net
from .base import NAME, ConnectorError, as_list, call, clip, http, integer, need


def _client(p):
    return http(p, "api_url", headers={"Authorization": f"Bearer {p['token']}", "Accept": "application/json"})


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
    return {"ok": True, "version": version.get("gitVersion")}


def list_pods(p, args):
    ns = _ns(p, args)
    with _client(p) as c:
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
    with _client(p) as c:
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
    with _client(p) as c:
        text = call(c, "GET", f"/api/v1/namespaces/{ns}/pods/{pod}/log", params=query, as_text=True)
    return {"namespace": ns, "pod": pod, "log": clip(text)}


def _deployment(c, ns: str, name: str) -> dict:
    return call(c, "GET", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}")


def _scale_args(p, args):
    ns, name = _ns(p, args), need(args, "deployment", NAME)
    return ns, name, integer(args, "replicas", -1, 0, int(p.get("max_replicas") or 50), clamp=False)


def scale_check(p, args):
    ns, name, replicas = _scale_args(p, args)
    with _client(p) as c:
        current = _deployment(c, ns, name).get("spec", {}).get("replicas")
    return {"would_change": f"{ns}/{name}: {current} → {replicas}", "objects": 1}


def scale(p, args):
    ns, name, replicas = _scale_args(p, args)
    with _client(p) as c:
        before = _deployment(c, ns, name).get("spec", {}).get("replicas")
        call(c, "PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}/scale", json_body={"spec": {"replicas": replicas}},
             headers={"Content-Type": "application/merge-patch+json"})
    return {"namespace": ns, "deployment": name, "from": before, "to": replicas}


def restart_check(p, args):
    ns, name = _ns(p, args), need(args, "deployment", NAME)
    with _client(p) as c:
        dep = _deployment(c, ns, name)
    return {"would_restart": f"{ns}/{name}", "pods": dep.get("spec", {}).get("replicas"), "objects": 1}


def rollout_restart(p, args):
    ns, name = _ns(p, args), need(args, "deployment", NAME)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}}
    with _client(p) as c:
        call(c, "PATCH", f"/apis/apps/v1/namespaces/{ns}/deployments/{name}", json_body=patch,
             headers={"Content-Type": "application/strategic-merge-patch+json"})
    return {"restarted": f"{ns}/{name}", "at": stamp}


def delete_pod_check(p, args):
    ns, pod = _ns(p, args), need(args, "pod", NAME)
    with _client(p) as c:
        item = call(c, "GET", f"/api/v1/namespaces/{ns}/pods/{pod}")
    owner = (item.get("metadata", {}).get("ownerReferences") or [{}])[0].get("kind")
    return {"would_delete": f"{ns}/{pod}", "owner": owner, "objects": 1}


def delete_pod(p, args):
    ns, pod = _ns(p, args), need(args, "pod", NAME)
    with _client(p) as c:
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

    base = net.check_url(p["api_url"], p.get("_allow_private", False))
    ip = net.check_host(base.host, base.port or net.default_port(base.scheme), p.get("_allow_private", False))
    query = [("command", a) for a in argv] + [("stdout", "true"), ("stderr", "true")] + ([("container", container)] if container else [])
    scheme = "wss" if base.scheme == "https" else "ws"
    path = f"/api/v1/namespaces/{quote(ns)}/pods/{quote(pod)}/exec?{urlencode(query)}"
    sock = socket.create_connection((ip, base.port or net.default_port(base.scheme)), timeout=10)
    kwargs = {"ssl": net.tls_context(p.get("verify_tls", True), p.get("ca_cert") or None), "server_hostname": base.host} if scheme == "wss" else {}
    out, err, status = [], [], None
    deadline = time.monotonic() + timeout
    try:
        with connect(f"{scheme}://{base.netloc.decode()}{path}", sock=sock, subprotocols=["v4.channel.k8s.io"], open_timeout=10, close_timeout=2,
                     max_size=2 ** 20, additional_headers={"Authorization": f"Bearer {p['token']}"}, **kwargs) as ws:
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
