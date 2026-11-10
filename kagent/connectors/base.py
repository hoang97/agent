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
        raise ConnectorError("hết thời gian chờ phản hồi") from exc
    except httpx.HTTPError as exc:
        raise ConnectorError(f"lỗi kết nối ({exc.__class__.__name__})") from exc
    text = data.decode("utf-8", "replace")
    if with_status:
        return status, resp_headers, text
    if status not in ok:
        hint = {401: "thông tin xác thực sai hoặc hết hạn", 403: "tài khoản của kết nối không có quyền", 404: "không tìm thấy"}.get(status, "")
        raise ConnectorError(f"HTTP {status} {hint}: {clip(text, 300)}".replace("  ", " "))
    if as_text:
        return text
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ConnectorError("phản hồi không phải JSON") from exc
