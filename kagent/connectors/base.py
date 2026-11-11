"""Tiện ích chung cho connector."""
from __future__ import annotations

import json
import re

import httpx

from .. import net

MAX_BYTES = 2_000_000
NAME = r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?"


class ConnectorError(Exception):
    pass


_K8S_SUFFIX = re.compile(r"\.(svc|svc\.cluster\.local|cluster\.local)$")


def host_hint(host: str) -> str:
    """Gợi ý khi tên máy có dạng dịch vụ Kubernetes (service.namespace), vốn chỉ phân giải được trong cluster."""
    host = (host or "").lower().rstrip(".")
    labels = host.split(".")
    # Tên miền cấp cao nhất thật không chứa '-' hay số; 'x.y-z' gần như chắc là service.namespace.
    k8s_like = bool(_K8S_SUFFIX.search(host)) or (len(labels) == 2 and not labels[-1].isalpha())
    if not k8s_like:
        return ""
    return (f"; '{host}' có vẻ là tên dịch vụ trong Kubernetes, chỉ phân giải được từ bên trong cluster. "
            "Dùng địa chỉ mà máy chủ kagent truy cập được (IP, NodePort, LoadBalancer, Ingress)")


def as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def clip(text: str, limit: int = 20000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…(đã cắt {len(text) - limit} ký tự)"


def need(args: dict, key: str, pattern: str | None = None) -> str:
    value = args.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ConnectorError(f"thiếu tham số {key}")
    value = value.strip()
    if pattern and not re.fullmatch(pattern, value):
        raise ConnectorError(f"tham số {key} không hợp lệ: {value[:60]}")
    return value


def integer(args: dict, key: str, default: int, low: int, high: int, clamp: bool = True) -> int:
    raw = args.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"{key} phải là số nguyên") from exc
    if low <= value <= high:
        return value
    if clamp:
        return max(low, min(high, value))
    raise ConnectorError(f"{key} phải nằm trong khoảng {low}-{high}")


def http(params: dict, base_field: str, headers: dict | None = None, auth=None) -> httpx.Client:
    return net.client(params[base_field], allow_private=params.get("_allow_private", False), verify=params.get("verify_tls", True),
                      ca_pem=params.get("ca_cert") or None, headers=headers, auth=auth)


def call(client: httpx.Client, method: str, path: str, *, params=None, json_body=None, headers=None, ok=(200, 201, 202), as_text: bool = False,
         with_status: bool = False):
    """Gọi HTTP, giới hạn kích thước phản hồi, đổi lỗi thành ConnectorError dễ hiểu."""
    try:
        with client.stream(method, path, params=params, json=json_body, headers=headers) as response:
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ConnectorError("phản hồi quá lớn (trên 2 MB)")
            status, resp_headers = response.status_code, response.headers
    except net.NetError:
        raise
    except httpx.TimeoutException as exc:
        raise ConnectorError("hết thời gian chờ phản hồi" + host_hint(client.base_url.host)) from exc
    except httpx.HTTPError as exc:
        raise ConnectorError(f"lỗi kết nối ({exc.__class__.__name__})" + host_hint(client.base_url.host)) from exc
    text = data.decode("utf-8", "replace")
    if with_status:
        return status, resp_headers, text
    return parse(status, resp_headers, text, ok=ok, as_text=as_text)


def parse(status: int, headers, text: str, *, ok=(200, 201, 202), as_text: bool = False):
    """Đổi phản hồi đã đọc thành dữ liệu, hoặc ConnectorError dễ hiểu."""
    if status not in ok:
        if 300 <= status < 400:
            # Không tự theo chuyển hướng (chống SSRF), nên nói rõ bị chuyển đi đâu.
            target = headers.get("location", "")
            raise ConnectorError(f"HTTP {status}: bị chuyển hướng tới {clip(target, 200) or '(không rõ)'}; "
                                 "kiểm tra lại URL, hoặc địa chỉ này cần đăng nhập qua trình duyệt")
        hint = {401: "thông tin xác thực sai hoặc hết hạn", 403: "tài khoản của kết nối không có quyền", 404: "không tìm thấy"}.get(status, "")
        raise ConnectorError(f"HTTP {status} {hint}: {clip(text, 300)}".replace("  ", " "))
    if as_text:
        return text
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ConnectorError("phản hồi không phải JSON") from exc


def monitoring_client(params: dict) -> httpx.Client:
    """Prometheus, Alertmanager và các bản tương thích (Mimir, Thanos, VictoriaMetrics): Bearer token hoặc basic auth, X-Scope-OrgID nếu đa tenant."""
    headers = {"Accept": "application/json"}
    if params.get("token"):
        headers["Authorization"] = f"Bearer {params['token']}"
    if params.get("tenant_header"):
        headers["X-Scope-OrgID"] = str(params["tenant_header"])
    auth = (params["username"], params.get("password") or "") if params.get("username") and not params.get("token") else None
    return http(params, "url", headers=headers, auth=auth)
