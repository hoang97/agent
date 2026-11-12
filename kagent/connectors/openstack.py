"""OpenStack qua REST API: xác thực Keystone v3 từ clouds.yaml hoặc openrc, gọi API từng dịch vụ theo service catalog.

Quyền thật là role của tài khoản trong project (reader, member, admin). Ở đây chỉ chia công cụ theo việc gọi làm gì,
để mức rủi ro khớp: đọc chỉ GET; ghi thêm POST/PUT/PATCH, trừ action phá dữ liệu và thay đổi Keystone; quản trị gọi được tất cả.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import threading
import time
from datetime import datetime, timezone

import yaml

from .base import ConnectorError, call, clip, http, need, parse

# Tên hay gọi → service type trong catalog; Cinder, Manila mỗi bản cài khai một kiểu nên thử lần lượt.
ALIASES = {"nova": ["compute"], "neutron": ["network"], "glance": ["image"], "keystone": ["identity"], "heat": ["orchestration"],
           "octavia": ["load-balancer"], "swift": ["object-store"], "designate": ["dns"], "barbican": ["key-manager"], "ironic": ["baremetal"],
           "magnum": ["container-infra"], "placement": ["placement"],
           "cinder": ["block-storage", "volumev3", "volumev2", "volume"], "block-storage": ["block-storage", "volumev3", "volumev2", "volume"],
           "volume": ["block-storage", "volumev3", "volumev2", "volume"], "volumev3": ["volumev3", "block-storage"],
           "manila": ["shared-file-system", "sharev2", "share"], "shared-file-system": ["shared-file-system", "sharev2", "share"]}
SERVICE = r"[a-z][a-z0-9-]{1,40}"
MICROVERSION = r"(\d{1,2}\.\d{1,3}|latest)"
WRITE_METHODS = {"POST", "PUT", "PATCH"}
METHODS = WRITE_METHODS | {"GET", "DELETE", "HEAD"}
# Action làm mất dữ liệu hoặc vượt kiểm tra trạng thái: chỉ công cụ quản trị được gọi.
DESTRUCTIVE_ACTIONS = {"forceDelete", "rebuild", "evacuate", "os-resetState", "trigger_crash_dump", "os-reset_status", "os-force_delete",
                       "os-force_detach", "os-unmanage", "os-reset-status", "reset_status", "force_delete", "reset_task_state", "revert",
                       "os-migrate_volume", "unmanage"}
OVERWRITE_PATHS = re.compile(r"/backups/[^/]+/restore$")  # khôi phục backup Cinder đè lên volume đang có
READ_ACTIONS = {"os-getConsoleOutput"}  # POST nhưng chỉ đọc: log console giúp điều tra máy ảo không lên
# Đọc lộ bí mật (payload của Barbican, khóa EC2, mật khẩu máy ảo): chỉ công cụ quản trị.
SECRET_PATHS = re.compile(r"(/payload|/credentials|/OS-EC2|/os-server-password)(/|$)", re.I)
TTL_MARGIN = 120

_cache: dict[str, tuple[float, str, list, dict]] = {}
_lock = threading.Lock()


def _fail(message: str):
    raise ConnectorError(message)


# ---------- đọc cấu hình ----------

_OPENRC = re.compile(r"^\s*(?:export\s+)?(OS_[A-Z0-9_]+)=(.*)$")
_RC_KEYS = {"OS_AUTH_URL": "auth_url", "OS_USERNAME": "username", "OS_USER_ID": "user_id", "OS_PASSWORD": "password",
            "OS_PROJECT_NAME": "project_name", "OS_TENANT_NAME": "project_name", "OS_PROJECT_ID": "project_id", "OS_TENANT_ID": "project_id",
            "OS_USER_DOMAIN_NAME": "user_domain_name", "OS_USER_DOMAIN_ID": "user_domain_id", "OS_PROJECT_DOMAIN_NAME": "project_domain_name",
            "OS_PROJECT_DOMAIN_ID": "project_domain_id", "OS_DOMAIN_NAME": "domain_name", "OS_DOMAIN_ID": "domain_id",
            "OS_APPLICATION_CREDENTIAL_ID": "application_credential_id", "OS_APPLICATION_CREDENTIAL_NAME": "application_credential_name",
            "OS_APPLICATION_CREDENTIAL_SECRET": "application_credential_secret", "OS_TOKEN": "token"}


def _openrc(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        m = _OPENRC.match(line)
        if not m:
            continue
        try:
            words = shlex.split(m.group(2), comments=True)
        except ValueError:
            continue
        value = words[0] if words else ""
        if "$" not in value:  # OS_PASSWORD=$OS_PASSWORD_INPUT của openrc tải từ Horizon: mật khẩu điền ở ô password
            env[m.group(1)] = value
    auth = {v: env[k] for k, v in _RC_KEYS.items() if env.get(k)}
    return {"auth": auth, "auth_type": env.get("OS_AUTH_TYPE"), "region_name": env.get("OS_REGION_NAME"),
            "interface": env.get("OS_INTERFACE") or env.get("OS_ENDPOINT_TYPE")}


def _clouds(text: str, name: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _fail(f"config không phải clouds.yaml hay openrc hợp lệ: {str(exc)[:200]}")
    clouds = doc.get("clouds") if isinstance(doc, dict) else None
    if not isinstance(clouds, dict) or not clouds:
        _fail("config phải là clouds.yaml (có khóa clouds:) hoặc file openrc (các dòng export OS_...=...)")
    if not name:
        if len(clouds) > 1:
            _fail(f"clouds.yaml có nhiều cloud, điền ô cloud là một trong: {', '.join(map(str, clouds))}")
        name = next(iter(clouds))
    cloud = clouds.get(name)
    if not isinstance(cloud, dict):
        _fail(f"clouds.yaml không có cloud {name}; có: {', '.join(map(str, clouds))}")
    return {"auth": dict(cloud.get("auth") or {}), "auth_type": cloud.get("auth_type"), "region_name": cloud.get("region_name"),
            "interface": cloud.get("interface") or cloud.get("endpoint_type")}


def _auth_url(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if not url:
        _fail("config thiếu auth_url (OS_AUTH_URL)")
    if url.endswith("/v2.0"):
        url = url[:-5]
    return url if url.endswith("/v3") else url + "/v3"


def settings(p: dict) -> dict:
    text = str(p.get("config") or "")
    if not text.strip():
        _fail("thiếu config: dán clouds.yaml hoặc nội dung file openrc")
    c = _openrc(text) if any(_OPENRC.match(line) for line in text.splitlines()) else _clouds(text, str(p.get("cloud") or "").strip())
    auth = {k: str(v) for k, v in c["auth"].items() if v not in (None, "")}
    if p.get("password"):
        auth["password"] = p["password"]
    kind = str(c.get("auth_type") or "").lower().replace("v3", "").replace("_", "").replace("-", "")
    if kind in ("applicationcredential",) or (not kind and auth.get("application_credential_secret")):
        method = "application_credential"
        if not auth.get("application_credential_secret") or not (auth.get("application_credential_id") or auth.get("application_credential_name")):
            _fail("application credential cần application_credential_secret và application_credential_id (hoặc name kèm username)")
    elif kind == "token" or (not kind and auth.get("token") and not auth.get("password")):
        method = "token"
        if not auth.get("token"):
            _fail("auth_type token nhưng config thiếu token")
    elif kind in ("", "password"):
        method = "password"
        if not (auth.get("username") or auth.get("user_id")):
            _fail("config thiếu username (OS_USERNAME)")
        if not auth.get("password"):
            _fail("config chưa có mật khẩu: điền ô password của kết nối (openrc tải từ Horizon không chứa mật khẩu)")
    else:
        _fail(f"auth_type {c.get('auth_type')} chưa hỗ trợ; dùng password, v3applicationcredential hoặc token")
    interface = str(p.get("interface") or c.get("interface") or "public").lower().replace("url", "")
    if interface not in ("public", "internal", "admin"):
        _fail("interface phải là public, internal hoặc admin")
    return {"auth_url": _auth_url(auth.get("auth_url")), "method": method, "auth": auth, "interface": interface,
            "region": str(p.get("region") or c.get("region_name") or "").strip()}


def validate_params(p) -> None:
    from .. import net
    net.check_url(settings(p)["auth_url"], p.get("_allow_private", False))


def _ref(auth: dict, prefix: str, fallback: str = "") -> dict | None:
    """{"id": ...} hoặc {"name": ...} của user/project/domain, theo các khóa <prefix>_id, <prefix>_name."""
    for key in (f"{prefix}_id", f"{prefix}_name") + ((f"{fallback}_id", f"{fallback}_name") if fallback else ()):
        if auth.get(key):
            return {"id" if key.endswith("_id") else "name": auth[key]}
    return None


def _body(s: dict) -> dict:
    a = s["auth"]
    if s["method"] == "application_credential":
        cred = {"secret": a["application_credential_secret"]}
        if a.get("application_credential_id"):
            cred["id"] = a["application_credential_id"]
        else:
            cred.update(name=a["application_credential_name"], user=_user(a))
        return {"auth": {"identity": {"methods": ["application_credential"], "application_credential": cred}}}
    identity = ({"methods": ["token"], "token": {"id": a["token"]}} if s["method"] == "token"
                else {"methods": ["password"], "password": {"user": {**_user(a), "password": a["password"]}}})
    body = {"auth": {"identity": identity}}
    project = _ref(a, "project", "tenant")
    if project:
        if "name" in project:
            project["domain"] = _ref(a, "project_domain") or _ref(a, "user_domain") or _ref(a, "domain") or {"id": "default"}
        body["auth"]["scope"] = {"project": project}
    elif _ref(a, "domain"):
        body["auth"]["scope"] = {"domain": _ref(a, "domain")}
    else:
        _fail("config thiếu project (OS_PROJECT_NAME hoặc OS_PROJECT_ID): token không gắn project thì không có service catalog")
    return body


def _user(a: dict) -> dict:
    if a.get("user_id"):
        return {"id": a["user_id"]}
    return {"name": a.get("username", ""), "domain": _ref(a, "user_domain") or _ref(a, "domain") or {"id": "default"}}


# ---------- token và catalog ----------

def _http(p: dict, url: str, token: str | None = None, headers: dict | None = None):
    return http({**p, "_url": url}, "_url", headers={"Accept": "application/json", **({"X-Auth-Token": token} if token else {}), **(headers or {})})


def _key(p: dict, s: dict) -> str:
    raw = json.dumps([s, p.get("_allow_private"), p.get("verify_tls", True), p.get("ca_cert"), p.get("proxy")], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _login(p: dict, fresh: bool = False) -> tuple[str, list, dict, dict]:
    s = settings(p)
    key = _key(p, s)
    with _lock:
        hit = _cache.get(key)
    if hit and not fresh and hit[0] > time.monotonic():
        return hit[1], hit[2], hit[3], s
    with _http(p, s["auth_url"]) as c:
        status, headers, text = call(c, "POST", "/auth/tokens", json_body=_body(s), with_status=True)
    if status == 401:
        _fail("Keystone từ chối thông tin xác thực (HTTP 401): kiểm tra username/password, application credential hoặc project")
    data = parse(status, headers, text, ok=(200, 201))
    token = headers.get("x-subject-token")
    t = data.get("token") or {}
    if not token:
        _fail("Keystone không trả X-Subject-Token")
    info = {"user": (t.get("user") or {}).get("name"), "project": (t.get("project") or {}).get("name"),
            "project_id": (t.get("project") or {}).get("id"), "domain": (t.get("domain") or {}).get("name"),
            "roles": sorted(r.get("name") for r in t.get("roles") or [] if r.get("name")), "expires_at": t.get("expires_at")}
    left = 3600.0
    try:
        left = (datetime.fromisoformat(str(t["expires_at"]).replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
    except (KeyError, ValueError, TypeError):
        pass
    if left > TTL_MARGIN:
        with _lock:
            _cache[key] = (time.monotonic() + min(left - TTL_MARGIN, 3600), token, t.get("catalog") or [], info)
    return token, t.get("catalog") or [], info, s


def _endpoint(catalog: list, service: str, s: dict) -> tuple[str, str]:
    wanted = ALIASES.get(service, [service])
    found = [e for kind in wanted for e in catalog if e.get("type") == kind] or [e for e in catalog if e.get("name") == service]
    if not found:
        _fail(f"catalog không có dịch vụ {service}; có: {', '.join(sorted({str(e.get('type')) for e in catalog}))}")
    for entry in found:
        eps = [e for e in entry.get("endpoints") or [] if e.get("interface") == s["interface"]]
        if s["region"]:
            eps = [e for e in eps if s["region"] in (e.get("region"), e.get("region_id"))]
        if eps:
            return entry.get("type"), str(eps[0]["url"]).rstrip("/")
    where = f"interface {s['interface']}" + (f", region {s['region']}" if s["region"] else "")
    _fail(f"dịch vụ {service} không có endpoint ở {where}")


def ping(p, args=None):
    _, catalog, info, s = _login(p, fresh=True)
    return {"ok": True, **info, "services": len(catalog), "regions": sorted({e.get("region_id") or e.get("region") for c in catalog
                                                                            for e in c.get("endpoints") or [] if e.get("region_id") or e.get("region")})}


def list_services(p, args):
    _, catalog, info, s = _login(p)
    out = []
    for entry in catalog:
        eps = [e for e in entry.get("endpoints") or [] if e.get("interface") == s["interface"] and (not s["region"] or s["region"] in (e.get("region"), e.get("region_id")))]
        if eps:
            out.append({"type": entry.get("type"), "name": entry.get("name"), "url": eps[0].get("url"), "region": eps[0].get("region_id") or eps[0].get("region")})
    return {**{k: v for k, v in info.items() if k != "expires_at"}, "interface": s["interface"], "services": sorted(out, key=lambda e: str(e["type"]))}


# ---------- gọi API ----------

def _path(args: dict) -> str:
    path = need(args, "path")
    low = path.lower()
    if not path.startswith("/") or "//" in path or ".." in path or "://" in path or "#" in path or "%2e" in low or "%2f" in low \
            or any(ch.isspace() or ord(ch) < 32 for ch in path):
        _fail("path phải là đường dẫn tương đối bắt đầu bằng /, tính từ URL endpoint trong catalog, ví dụ /servers/detail")
    return path


def _actions(body) -> set[str]:
    return set(body) if isinstance(body, dict) else set()


def _classify(service_type: str, method: str, path: str, body) -> str:
    """Nhóm thấp nhất được gọi: read | write | admin."""
    if SECRET_PATHS.search(path.split("?", 1)[0]):
        return "admin"
    if method in ("GET", "HEAD"):
        return "read"
    action = path.split("?", 1)[0].rstrip("/").endswith("/action")
    if method == "POST" and action and _actions(body) and _actions(body) <= READ_ACTIONS:
        return "read"
    if method == "DELETE" or service_type == "identity" or (action and _actions(body) & DESTRUCTIVE_ACTIONS) \
            or OVERWRITE_PATHS.search(path.split("?", 1)[0].rstrip("/")):
        return "admin"
    return "write"


TOOL = {"read": "api_read", "write": "api_write", "admin": "api_admin"}
LEVELS = ("read", "write", "admin")


def _call(p: dict, args: dict, profile: str) -> dict:
    service = need(args, "service", SERVICE).lower()
    method = str(args.get("method") or "GET").upper()
    if method not in METHODS:
        _fail(f"method phải là một trong {', '.join(sorted(METHODS))}")
    path = _path(args)
    query = args.get("query") if isinstance(args.get("query"), dict) else None
    body = args.get("body")
    if body is not None and not isinstance(body, (dict, list)):
        _fail("body phải là object hoặc mảng JSON")
    if body is not None and method in ("GET", "HEAD", "DELETE"):
        _fail(f"{method} không gửi body")
    version = need(args, "microversion", MICROVERSION) if args.get("microversion") else None
    headers = {}
    for attempt in (0, 1):
        token, catalog, _, s = _login(p, fresh=attempt == 1)
        kind, url = _endpoint(catalog, service, s)
        lowest = _classify(kind, method, path, body)
        if LEVELS.index(lowest) > LEVELS.index(profile):
            _fail(f"{method} {path} trên {kind} thuộc nhóm {lowest}, hãy dùng công cụ {TOOL[lowest]}")
        if version:
            headers = {"OpenStack-API-Version": f"{kind} {version}", **({"X-OpenStack-Nova-API-Version": version} if kind == "compute" else {})}
        with _http(p, url, token, headers) as c:
            status, resp_headers, text = call(c, method, path, params=query, json_body=body, with_status=True)
        if status != 401 or attempt:
            break  # 401 lần đầu thường do token trong bộ nhớ đã bị thu hồi: đăng nhập lại một lần
    result = {"service": kind, "request": f"{method} {url}{path}", "status": status}
    if status >= 400:
        _fail(f"HTTP {status} từ {kind}: {clip(_error(text), 600)}")
    if resp_headers.get("location"):
        result["location"] = resp_headers["location"]
    if "json" in resp_headers.get("content-type", "") and text.strip():
        try:
            result["json"] = json.loads(text)
            return result
        except json.JSONDecodeError:
            pass
    if text.strip():
        result["text"] = clip(text)
    return result


def _error(text: str) -> str:
    """Lỗi của OpenStack thường lồng một tầng: {"itemNotFound": {"message": ...}} hoặc {"NeutronError": {...}}."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(data, dict):
        for v in [data, *data.values()]:
            if isinstance(v, dict) and (v.get("message") or v.get("faultstring")):
                return str(v.get("message") or v.get("faultstring"))
        if isinstance(data.get("error"), dict):
            return str(data["error"].get("message") or data["error"])
    return text


def api_read(p, args):
    return _call(p, args, "read")


def api_write(p, args):
    return _call(p, args, "write")


def api_admin(p, args):
    return _call(p, args, "admin")
