"""Kết nối Kubernetes: đọc kubeconfig/token, kiểm tra kết nối. Lệnh chạy bằng kubectl (kubectl.py), quyền do k8s_rbac.py tạo."""
from __future__ import annotations

import base64
import binascii

import yaml

from .. import net
from .base import NAME, ConnectorError, call, need

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


def _client(p, token: str | None = None):
    """Mặc định dùng thông tin xác thực của kết nối (tài khoản quản trị); token là để thử ServiceAccount của một nhóm quyền."""
    c = _conn(p)
    token, client_cert = (token, None) if token else (c["token"], c["client_cert"])
    headers = {"Accept": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    return net.client(c["api_url"], allow_private=c["allow_private"], verify=c["verify_tls"], ca_pem=c["ca_cert"] or None,
                      client_cert=client_cert, headers=headers)


def ping(p, args=None):
    ns = need({"namespace": p.get("namespace") or "default"}, "namespace", NAME)
    with _client(p) as c:
        version = call(c, "GET", "/version")
        call(c, "GET", f"/api/v1/namespaces/{ns}/pods", params={"limit": 1})
    out = {"ok": True, "version": version.get("gitVersion")}
    identities = {}
    for r in IDENTITIES:
        if not p.get(f"_sa_{r}"):
            continue
        try:
            with _client(p, p[f"_sa_{r}"]) as c:
                review = call(c, "POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews", ok=(200, 201),
                              json_body={"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
            identities[r] = review.get("status", {}).get("userInfo", {}).get("username") or "ok"
        except ConnectorError as exc:
            identities[r] = f"lỗi: {exc}"
    if identities:
        out["identities"] = identities
    return out
