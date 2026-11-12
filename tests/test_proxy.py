"""Proxy theo file PAC: tải và giữ 5 phút, chạy PAC trong tiến trình con, chọn đường cho từng request và cho kubectl."""
import httpx
import pytest
import yaml

import fakes
from kagent import db, net, proxy
from kagent.connectors import kubectl

PAC = """
function FindProxyForURL(url, host) {
    if (dnsDomainIs(host, ".devtest.vcloud.internal")) return "PROXY 10.221.38.10:8028; PROXY 10.221.38.11:8028";
    if (dnsDomainIs(host, ".socks.test")) return "SOCKS 10.0.0.9:1080";
    if (dnsDomainIs(host, ".loop.test")) { while (true) {} }
    if (host == "k8s.test") return "PROXY 10.221.38.10:8028";
    return "DIRECT";
}
"""


@pytest.fixture()
def pac(client, monkeypatch):
    """Khai URL file PAC trong Cài đặt; fetcher giả đếm số lần tải."""
    calls = []

    def fetch(url):
        calls.append(url)
        if isinstance(fetch.body, Exception):
            raise fetch.body
        return fetch.body

    fetch.body = PAC
    monkeypatch.setattr(proxy, "fetcher", fetch)
    for host, ip in {"am.devtest.vcloud.internal": fakes.PUBLIC_IP, "pac.test": fakes.PUBLIC_IP, "web.test": fakes.PUBLIC_IP,
                     "evil.devtest.vcloud.internal": "169.254.169.254"}.items():
        monkeypatch.setitem(fakes.HOSTS, host, ip)
    with db.session() as conn:
        proxy.set_url(conn, "https://pac.test/proxy.pac")
    fetch.calls = calls
    return fetch


def test_parse_pac_results():
    assert proxy.parse("PROXY a:1; HTTPS b:2 ;DIRECT") == ["http://a:1", "https://b:2", "DIRECT"]
    assert proxy.parse("SOCKS5 s:1") == [] and proxy.parse("") == []


@pytest.mark.parametrize("value, ok", [(None, "auto"), ("auto", "auto"), ("none", "none"), ("http://10.1.1.1:3128/", "http://10.1.1.1:3128"),
                                       ("ftp://x:1", None), ("http://u:p@x:1", None), ("http://x:1/path", None), ("rác", None)])
def test_check_mode(value, ok):
    if ok is None:
        with pytest.raises(proxy.ProxyError):
            proxy.check_mode(value)
    else:
        assert proxy.check_mode(value) == ok


def test_routes_follow_pac_and_cache_five_minutes(pac, monkeypatch):
    assert proxy.routes("https://am.devtest.vcloud.internal/api/v2/alerts") == ["http://10.221.38.10:8028", "http://10.221.38.11:8028"]
    assert proxy.routes("http://10.210.117.100:8601/") == ["DIRECT"]
    assert proxy.routes("https://am.devtest.vcloud.internal", "none") == ["DIRECT"]
    assert proxy.routes("https://x.test", "http://10.9.9.9:3128") == ["http://10.9.9.9:3128"]
    assert len(pac.calls) == 1  # trong 5 phút không tải lại
    later = proxy.time.time() + proxy.TTL + 1
    monkeypatch.setattr(proxy.time, "time", lambda: later)
    pac.body = OSError("vdrive không phản hồi")
    assert proxy.routes("https://am.devtest.vcloud.internal") == ["http://10.221.38.10:8028", "http://10.221.38.11:8028"]  # giữ bản cũ
    assert len(pac.calls) == 2 and "vdrive" in proxy.status()["error"] and proxy.status()["loaded"]


def test_pac_errors(pac, monkeypatch):
    with pytest.raises(proxy.ProxyError, match="SOCKS"):
        proxy.routes("https://a.socks.test")
    monkeypatch.setattr(proxy, "EVAL_TIMEOUT", 1)
    with pytest.raises(proxy.ProxyError, match="chạy quá"):
        proxy.routes("https://a.loop.test")
    proxy.reset()
    pac.body = "<html>đăng nhập</html>"
    assert proxy.routes("https://a.devtest.vcloud.internal") == ["DIRECT"]  # chưa nạp được PAC lần nào: đi thẳng như trình duyệt
    assert proxy.routes("https://a.devtest.vcloud.internal") == ["DIRECT"]
    assert len(pac.calls) == 2 and "FindProxyForURL" in proxy.status()["error"]  # lỗi được nhớ 5 phút, không tải lại ở mọi request


def test_no_pac_configured_means_direct(client):
    assert proxy.routes("https://anything.test") == ["DIRECT"]


# ---------- chọn đường cho từng request ----------

@pytest.fixture()
def wire(monkeypatch):
    """Ghi lại request đi thẳng (PinnedTransport) và đi qua proxy (HTTPTransport có proxy)."""
    seen = []

    def direct(self, request):
        seen.append(("DIRECT", str(request.url)))
        return httpx.Response(200, text="ok")

    def via(self, request):
        route = self.proxy_url
        if route in wire.down:
            raise httpx.ConnectError("proxy down")
        seen.append((route, str(request.url)))
        return httpx.Response(200, text="ok")

    real_init = httpx.HTTPTransport.__init__

    def init(self, *a, proxy=None, **kw):
        real_init(self, *a, **kw)
        self.proxy_url = str(proxy) if proxy else None

    monkeypatch.setattr(net.PinnedTransport, "handle_request", direct)
    monkeypatch.setattr(httpx.HTTPTransport, "__init__", init)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", via)
    monkeypatch.setattr(net, "transport_override", None)
    wire.down = set()
    wire.seen = seen
    return wire


def test_transport_routes_and_falls_back(pac, wire):
    with net.client("https://am.devtest.vcloud.internal", allow_private=False) as c:
        assert c.get("/-/healthy").text == "ok"
        wire.down.add("http://10.221.38.10:8028")
        c.post("/api/v2/silences", json={"a": 1})
    with net.client("https://web.test", allow_private=False) as c:
        c.get("/")
    assert wire.seen == [("http://10.221.38.10:8028", "https://am.devtest.vcloud.internal/-/healthy"),
                         ("http://10.221.38.11:8028", "https://am.devtest.vcloud.internal/api/v2/silences"),
                         ("DIRECT", "https://web.test/")]


def test_proxied_target_still_checked(pac, wire):
    with net.client("https://evil.devtest.vcloud.internal", allow_private=True) as c, pytest.raises(net.NetError, match="bị chặn"):
        c.get("/")
    # proxy khai tay trên kết nối phải qua kiểm tra địa chỉ; proxy trong file PAC (org admin) thì không
    with net.client("https://web.test", allow_private=False, proxy="http://10.0.0.5:3128") as c, pytest.raises(net.NetError, match="mạng nội bộ"):
        c.get("/")
    assert not wire.seen


def test_kubectl_uses_proxy_url(pac):
    p = {"_allow_private": False, "api_url": "https://k8s.test:6443", "token": fakes.K8S_TOKEN, "namespace": "default"}
    cluster = yaml.safe_load(kubectl.kubeconfig(p, "t"))["clusters"][0]["cluster"]
    assert cluster == {"server": "https://k8s.test:6443", "proxy-url": "http://10.221.38.10:8028"}
    direct = yaml.safe_load(kubectl.kubeconfig({**p, "proxy": "none"}, "t"))["clusters"][0]["cluster"]
    assert direct["server"] == f"https://{fakes.PUBLIC_IP}:6443" and "proxy-url" not in direct


# ---------- API và kết nối ----------

def test_settings_api_is_org_admin_only(users, pac):
    admin, alice = users["admin"], users["alice"]
    assert alice.get("/api/settings/proxy", tenant=False).status_code == 403
    st = admin.put("/api/settings/proxy", json={"url": "https://pac.test/v2.pac"}, tenant=False).json()
    assert st["url"] == "https://pac.test/v2.pac" and st["loaded"] and st["bytes"] == len(PAC)
    r = admin.post("/api/settings/proxy/test", json={"url": "https://am.devtest.vcloud.internal"}, tenant=False).json()
    assert r["routes"][0] == "http://10.221.38.10:8028" and "qua proxy" in r["text"]
    assert admin.put("/api/settings/proxy", json={"url": "ftp://x"}, tenant=False).status_code == 400
    assert admin.put("/api/settings/proxy", json={"url": ""}, tenant=False).json()["url"] is None


def test_connection_proxy_field_and_test_shows_route(users, pac):
    alice = users["alice"]
    body = {"template": "alertmanager", "name": "am_dev", "scope": "user", "envs": ["dev"], "params": {"url": "https://am.devtest.vcloud.internal"}}
    assert alice.post("/api/connections", json={**body, "params": {**body["params"], "proxy": "socks5://x:1"}}).status_code == 400
    r = alice.post("/api/connections", json={**body, "params": {**body["params"], "proxy": "http://10.0.0.5:3128"}})
    assert r.status_code == 400 and "mạng nội bộ" in r.json()["detail"]
    c = alice.post("/api/connections", json=body).json()
    out = alice.post(f"/api/connections/{c['id']}/test").json()
    assert out["route"] == "qua proxy http://10.221.38.10:8028"


def test_download_keeps_https_on_same_host_redirect(monkeypatch):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if request.url.path == "/s/abc/download":
            return httpx.Response(303, headers={"location": "http://pac.test/dav/abc?accept=zip"})
        return httpx.Response(200, text=PAC)

    monkeypatch.setitem(fakes.HOSTS, "pac.test", fakes.PUBLIC_IP)
    monkeypatch.setattr(net, "transport_override", lambda: httpx.MockTransport(handle))
    monkeypatch.setattr(proxy, "fetcher", None)
    assert "FindProxyForURL" in proxy._download("https://pac.test/s/abc/download")
    assert seen == ["https://pac.test/s/abc/download", "https://pac.test/dav/abc?accept=zip"]
