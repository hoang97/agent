"""Proxy cho kết nối ra ngoài, theo file PAC do org admin khai URL trong Cài đặt.

- File PAC tải về và giữ 5 phút; tải lỗi thì dùng bản tốt gần nhất, chưa có bản nào thì đi thẳng (như trình duyệt).
  Kết quả tra theo (scheme, host) cũng giữ 5 phút.
- Mã PAC chạy trong tiến trình con có hạn giờ: PAC treo (vòng lặp vô hạn) chỉ làm lỗi lần tra đó, không treo kagent.
- Mỗi kết nối chọn "auto" (theo PAC, mặc định), "none" (đi thẳng) hoặc một proxy cụ thể http://host:port.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import httpx

TTL = 300
FETCH_TIMEOUT = 15
EVAL_TIMEOUT = 5
MAX_PAC_BYTES = 1_000_000
KEY = "proxy_pac_url"
log = logging.getLogger("kagent.proxy")


class ProxyError(Exception):
    pass


_lock = threading.Lock()
_pac: dict = {"url": None, "text": None, "fetched_at": 0.0, "error": None, "checked_at": 0.0}
_routes: dict[tuple, tuple[float, list[str]]] = {}
# Test thay bằng callable(url) -> str để không cần mạng.
fetcher = None


def configured_url(conn=None) -> str | None:
    from . import db
    if conn is None:
        with db.session() as c:
            return configured_url(c)
    try:
        r = conn.execute("SELECT value FROM app_setting WHERE key=?", (KEY,)).fetchone()
    except sqlite3.OperationalError:  # CSDL chưa khởi tạo (công cụ dòng lệnh, test connector đơn lẻ): coi như chưa khai PAC
        return None
    return r["value"] if r and r["value"] else None


def set_url(conn, url: str | None) -> None:
    from . import net
    url = (url or "").strip() or None
    if url:
        try:
            net.check_url(url, allow_private=True)
        except net.NetError as exc:
            raise ProxyError(f"URL file PAC: {exc}") from exc
    conn.execute("INSERT INTO app_setting(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, url))
    reset()


_url_cache: list = [0.0, None]  # [lúc đọc, URL]: đọc DB tối đa 30 giây một lần thay vì ở mọi request


def _configured() -> str | None:
    now = time.time()
    if now - _url_cache[0] > 30:
        _url_cache[:] = [now, configured_url()]
    return _url_cache[1]


def reset() -> None:
    with _lock:
        _pac.update(url=None, text=None, fetched_at=0.0, error=None, checked_at=0.0)
        _routes.clear()
        _url_cache[:] = [0.0, None]


def _download(url: str) -> str:
    """Tải file PAC. Org admin khai URL này nên được vào mạng nội bộ; theo tối đa 5 lần chuyển hướng, kiểm tra lại từng bước."""
    if fetcher is not None:
        return fetcher(url)
    from . import net
    for _ in range(6):
        u = net.check_url(url, allow_private=True)
        with net.client(None, allow_private=True, proxy="none", timeout=FETCH_TIMEOUT) as c, c.stream("GET", str(u)) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                nxt = u.join(r.headers["location"])
                if u.scheme == "https" and nxt.scheme == "http" and nxt.host == u.host:
                    # Không hạ https xuống http (vdrive chuyển hướng sang cổng 80 vốn bị chặn; trình duyệt cũng tự giữ https).
                    nxt = nxt.copy_with(scheme="https", port=u.port)
                url = str(nxt)
                continue
            if r.status_code != 200:
                raise ProxyError(f"tải file PAC lỗi HTTP {r.status_code}")
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_PAC_BYTES:
                    raise ProxyError("file PAC lớn quá 1 MB")
            return data.decode("utf-8", "replace")
    raise ProxyError("file PAC chuyển hướng quá 5 lần")


def _current() -> str | None:
    """Nội dung PAC đang dùng; None nếu chưa khai URL. Hết 5 phút thì tải lại, tải lỗi thì giữ bản cũ."""
    url = _configured()
    now = time.time()
    with _lock:
        if url != _pac["url"]:
            _pac.update(url=url, text=None, fetched_at=0.0, error=None, checked_at=0.0)
            _routes.clear()
        if not url:
            return None
        if now - _pac["checked_at"] < TTL:
            return _pac["text"]  # None khi chưa nạp được lần nào: đi thẳng như trình duyệt, lỗi hiện ở Cài đặt → Proxy
    try:
        text = _download(url)
        if "FindProxyForURL" not in text:
            raise ProxyError("file tải về không có hàm FindProxyForURL, có thể không phải file PAC")
        with _lock:
            if text != _pac["text"]:
                _routes.clear()
            _pac.update(text=text, fetched_at=now, checked_at=now, error=None)
    except Exception as exc:  # mọi lỗi tải đều giữ bản cũ
        message = str(exc) or exc.__class__.__name__
        with _lock:
            _pac.update(error=message, checked_at=now)  # đợi 5 phút mới thử lại, không tải lại ở mọi lệnh gọi
        log.warning("không tải được file PAC (%s); %s", message, "dùng bản cũ" if _pac["text"] else "đi thẳng cho tới khi tải được")
    return _pac["text"]


_EVAL = r"""
import json, sys
from pypac.parser import PACFile
data = json.load(sys.stdin)
pac = PACFile(data["pac"])
print(json.dumps(pac.find_proxy_for_url(data["url"], data["host"])))
"""


def evaluate(pac: str, url: str, host: str) -> str:
    """Chạy FindProxyForURL trong tiến trình con có hạn giờ."""
    with tempfile.TemporaryDirectory(prefix="kagent-pac-") as d:
        try:
            proc = subprocess.run([sys.executable, "-c", _EVAL], input=json.dumps({"pac": pac, "url": url, "host": host}), capture_output=True,
                                  text=True, timeout=EVAL_TIMEOUT, cwd=d, env={k: v for k, v in os.environ.items() if k in ("PATH", "PYTHONPATH", "LANG")})
        except subprocess.TimeoutExpired as exc:
            raise ProxyError(f"file PAC chạy quá {EVAL_TIMEOUT} giây") from exc
    if proc.returncode != 0:
        raise ProxyError("file PAC lỗi: " + (proc.stderr.strip().splitlines() or ["không rõ"])[-1][:300])
    return json.loads(proc.stdout)


def parse(result: str) -> list[str]:
    """'PROXY a:1; HTTPS b:2; DIRECT' → ['http://a:1', 'https://b:2', 'DIRECT']. SOCKS chưa hỗ trợ nên bị bỏ qua."""
    out = []
    for part in (result or "").split(";"):
        words = part.split()
        if not words:
            continue
        kind = words[0].upper()
        if kind == "DIRECT":
            out.append("DIRECT")
        elif kind in ("PROXY", "HTTP", "HTTPS") and len(words) > 1:
            out.append(f"{'https' if kind == 'HTTPS' else 'http'}://{words[1]}")
    return out


def routes(url: str, mode: str | None = None) -> list[str]:
    """Danh sách đường thử lần lượt cho URL: 'DIRECT' hoặc URL proxy."""
    mode = (mode or "auto").strip()
    if mode == "none":
        return ["DIRECT"]
    if mode != "auto":
        return [mode]
    u = httpx.URL(url)
    pac = _current()
    if pac is None:
        return ["DIRECT"]
    key = (u.scheme, u.host, hashlib.sha256(pac.encode()).hexdigest())
    now = time.time()
    with _lock:
        hit = _routes.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    found = parse(evaluate(pac, f"{u.scheme}://{u.host}/", u.host))
    if not found:
        raise ProxyError(f"file PAC không trả về đường đi dùng được cho {u.host} (SOCKS chưa hỗ trợ)")
    with _lock:
        _routes[key] = (now, found)
    return found


def status() -> dict:
    url = _configured()
    with _lock:
        text = _pac["text"] if _pac["url"] == url else None
        return {"url": url, "loaded": text is not None, "bytes": len(text or ""), "sha256": hashlib.sha256(text.encode()).hexdigest()[:16] if text else None,
                "fetched_at": _pac["fetched_at"] or None, "error": _pac["error"] if _pac["url"] == url else None, "ttl_seconds": TTL}


def check_mode(value) -> str:
    """Giá trị ô proxy của kết nối: auto | none | http(s)://host:port (không kèm đường dẫn)."""
    value = (value or "auto").strip() if isinstance(value, str) or value is None else None
    if value in ("auto", "none"):
        return value
    try:
        u = httpx.URL(value or "")
    except (httpx.InvalidURL, TypeError) as exc:
        raise ProxyError("proxy phải là auto, none hoặc http://host:port") from exc
    if u.scheme not in ("http", "https") or not u.host or u.path not in ("", "/") or u.query or u.userinfo:
        raise ProxyError("proxy phải là auto, none hoặc http://host:port")
    return value.rstrip("/")
