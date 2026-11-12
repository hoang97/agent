"""Gọi mạng ra ngoài một cách an toàn cho connector.

- Luôn chặn loopback, link-local (gồm địa chỉ metadata của cloud), multicast, địa chỉ dành riêng.
- Mạng nội bộ (RFC 1918, ULA...) chỉ được dùng khi kết nối có allow_private do org admin bật.
- Phân giải DNS một lần, kiểm tra mọi IP rồi ghim kết nối vào IP đã kiểm tra (chống DNS rebinding).
- Không tự theo redirect: connector nào cần thì tự theo và kiểm tra lại từng bước.
- Proxy theo file PAC hoặc theo cấu hình của kết nối (xem proxy.py và RoutingTransport).
"""
from __future__ import annotations

import ipaddress
import socket
import ssl

import httpx


class NetError(Exception):
    pass


# Có thể ghi đè trong test để không cần DNS hay mạng thật.
transport_override = None  # callable() -> httpx.BaseTransport


def resolve(host: str, port: int) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


def check_ip(ip: str, allow_private: bool) -> None:
    addr = ipaddress.ip_address(ip)
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        raise NetError(f"địa chỉ {ip} bị chặn")
    if not addr.is_global and not allow_private:
        raise NetError(f"{ip} là địa chỉ mạng nội bộ; chỉ dùng được khi org admin cho phép kết nối này vào mạng nội bộ")


def check_host(host: str, port: int, allow_private: bool) -> str:
    """Trả về IP đã kiểm tra để ghim kết nối. Mọi IP phân giải ra đều phải hợp lệ."""
    if not host:
        raise NetError("thiếu tên máy chủ")
    try:
        ips = resolve(host, port)
    except (socket.gaierror, UnicodeError) as exc:
        raise NetError(f"không phân giải được {host}") from exc
    if not ips:
        raise NetError(f"không phân giải được {host}")
    for ip in ips:
        check_ip(ip, allow_private)
    return ips[0]


def default_port(scheme: str) -> int:
    return {"https": 443, "http": 80, "wss": 443, "ws": 80}.get(scheme, 443)


def check_url(url: str, allow_private: bool, schemes=("https", "http")) -> httpx.URL:
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise NetError("URL không hợp lệ") from exc
    if u.scheme not in schemes or not u.host or u.userinfo:
        raise NetError(f"URL phải có dạng {'/'.join(schemes)}://tên-máy, không kèm tài khoản")
    check_host(u.host, u.port or default_port(u.scheme), allow_private)
    return u


class PinnedTransport(httpx.HTTPTransport):
    """Phân giải và kiểm tra ở từng yêu cầu, rồi kết nối thẳng tới IP đã kiểm tra; TLS vẫn xác thực theo tên gốc."""

    def __init__(self, allow_private: bool, **kwargs):
        super().__init__(**kwargs)
        self.allow_private = allow_private

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        ip = check_host(host, request.url.port or default_port(request.url.scheme), self.allow_private)
        request.extensions = {**request.extensions, "sni_hostname": host}
        request.url = request.url.copy_with(host=ip)  # header Host đã được đặt theo tên gốc khi tạo request
        return super().handle_request(request)


class RoutingTransport(httpx.BaseTransport):
    """Chọn đường cho từng request theo proxy.routes: DIRECT thì ghim IP như PinnedTransport, còn lại đi qua proxy (CONNECT với https).

    Đích đi qua proxy vẫn phải phân giải được và qua kiểm tra IP như khi đi thẳng; chỉ việc kết nối là do proxy làm.
    Proxy lấy từ file PAC do org admin khai được dùng luôn; proxy khai tay trên kết nối phải qua kiểm tra địa chỉ như mọi đích khác.
    Đường lỗi kết nối thì thử đường kế tiếp trong danh sách PAC trả về.
    """

    def __init__(self, allow_private: bool, mode: str | None, verify):
        self.allow_private, self.mode, self.verify = allow_private, mode, verify
        self.direct = PinnedTransport(allow_private=allow_private, verify=verify)
        self.via: dict[str, httpx.HTTPTransport] = {}
        self.last_route: str | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        from . import proxy
        try:
            paths = proxy.routes(str(request.url), self.mode)
        except proxy.ProxyError as exc:
            raise NetError(str(exc)) from exc
        request.read()
        last: Exception | None = None
        for route in paths:
            self.last_route = route
            attempt = httpx.Request(request.method, request.url, headers=request.headers, content=request.content, extensions=dict(request.extensions))
            try:
                if route == "DIRECT":
                    return self.direct.handle_request(attempt)
                check_host(request.url.host, request.url.port or default_port(request.url.scheme), self.allow_private)
                if self.mode not in (None, "", "auto"):
                    pu = httpx.URL(route)
                    check_host(pu.host, pu.port or default_port(pu.scheme), self.allow_private)
                if route not in self.via:
                    self.via[route] = httpx.HTTPTransport(proxy=route, verify=self.verify)
                return self.via[route].handle_request(attempt)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as exc:
                last = exc
        raise last  # type: ignore[misc]

    def close(self) -> None:
        self.direct.close()
        for t in self.via.values():
            t.close()


def describe_route(route: str | None) -> str:
    return "" if not route else "đi thẳng" if route == "DIRECT" else f"qua proxy {route}"


def tls_context(verify: bool = True, ca_pem: str | None = None, client_cert: tuple[str, str] | None = None) -> ssl.SSLContext:
    """Dùng bộ CA của certifi (như httpx mặc định) để chạy giống nhau trên mọi hệ điều hành; CA riêng được nạp thêm.

    client_cert là (PEM chứng chỉ, PEM khóa riêng) cho mTLS.
    """
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        if ca_pem:
            ctx.load_verify_locations(cadata=ca_pem)
        if client_cert:
            _load_client_cert(ctx, *client_cert)
    except (ssl.SSLError, ValueError) as exc:
        raise NetError(f"chứng chỉ TLS không hợp lệ ({exc.__class__.__name__})") from exc
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _load_client_cert(ctx: ssl.SSLContext, cert_pem: str, key_pem: str) -> None:
    import os
    import tempfile
    # ssl chỉ nạp chứng chỉ từ file: ghi tạm với quyền 600, nạp xong xóa ngay.
    with tempfile.TemporaryDirectory(prefix="kagent-tls-") as d:
        paths = []
        for name, data in (("cert.pem", cert_pem), ("key.pem", key_pem)):
            path = os.path.join(d, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            paths.append(path)
        ctx.load_cert_chain(*paths)


def client(base_url: str | None, *, allow_private: bool, verify: bool = True, ca_pem: str | None = None,
           headers: dict | None = None, timeout: float = 20.0, auth=None, client_cert: tuple[str, str] | None = None,
           proxy: str | None = None) -> httpx.Client:
    """proxy: chế độ proxy của kết nối, auto (theo file PAC trong Cài đặt) | none | http://host:port."""
    if transport_override is not None:
        def guard(request: httpx.Request) -> None:
            check_host(request.url.host, request.url.port or default_port(request.url.scheme), allow_private)
        return httpx.Client(base_url=base_url or "", headers=headers, timeout=timeout, transport=transport_override(),
                            follow_redirects=False, event_hooks={"request": [guard]}, auth=auth)
    transport = RoutingTransport(allow_private, proxy, tls_context(verify, ca_pem, client_cert))
    return httpx.Client(base_url=base_url or "", headers=headers, timeout=timeout, transport=transport, follow_redirects=False, auth=auth)
