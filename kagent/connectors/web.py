"""Đọc trang web và gọi HTTP API thật. Nội dung trả về luôn được coi là chưa tin cậy."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser

from .. import net
from .base import ConnectorError, as_list, call, clip, http, need

MAX_REDIRECTS = 5


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(self.parts))).strip()


def _allowed(p, host: str) -> None:
    domains = [d.lower().lstrip(".") for d in as_list(p.get("allowed_domains"))]
    if domains and not any(host == d or host.endswith("." + d) for d in domains):
        raise ConnectorError(f"{host} không nằm trong danh sách tên miền được phép của kết nối")


def ping(p, args=None):
    return {"ok": True, "note": "kết nối web không cần thông tin xác thực"}


def fetch_page(p, args):
    url = need(args, "url")
    limit = int(p.get("max_chars") or 20000)
    for _ in range(MAX_REDIRECTS + 1):
        u = net.check_url(url, p.get("_allow_private", False))
        _allowed(p, u.host)
        with net.client(None, allow_private=p.get("_allow_private", False), headers={"User-Agent": p.get("user_agent") or "KnowledgeAgent/1.0"},
                        proxy=p.get("proxy")) as c:
            status, headers, text = call(c, "GET", str(u), with_status=True)
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            url = str(u.join(headers["location"]))
            continue
        if status >= 400:
            raise ConnectorError(f"HTTP {status} khi đọc {u}")
        ctype = headers.get("content-type", "")
        if "html" in ctype:
            parser = _Text()
            parser.feed(text)
            return {"url": str(u), "status": status, "title": parser.title.strip()[:300], "text": clip(parser.text(), limit)}
        return {"url": str(u), "status": status, "content_type": ctype, "text": clip(text, limit)}
    raise ConnectorError("quá nhiều lần chuyển hướng")


def _path(p, args) -> str:
    path = need(args, "path")
    if not path.startswith("/") or "//" in path or ".." in path or "://" in path:
        raise ConnectorError("path phải là đường dẫn tương đối bắt đầu bằng /")
    prefixes = as_list(p.get("allowed_paths"))
    if prefixes and not any(path.startswith(x) for x in prefixes):
        raise ConnectorError(f"path không nằm trong các tiền tố được phép: {', '.join(prefixes)}")
    return path


def _api(p):
    headers = {"Accept": "application/json"}
    if p.get("auth_value"):
        headers[p.get("auth_header") or "Authorization"] = p["auth_value"]
    return http(p, "base_url", headers=headers)


def api_ping(p, args=None):
    with _api(p) as c:
        status, _, _ = call(c, "GET", "/", with_status=True)
    if status in (401, 403):
        raise ConnectorError(f"HTTP {status}: thông tin xác thực bị từ chối")
    return {"ok": True, "status": status}


def api_get(p, args):
    query = args.get("query") if isinstance(args.get("query"), dict) else None
    with _api(p) as c:
        status, headers, text = call(c, "GET", _path(p, args), params=query, with_status=True)
    return _result(status, headers, text)


def api_post(p, args):
    body = args.get("body") if isinstance(args.get("body"), (dict, list)) else {}
    with _api(p) as c:
        status, headers, text = call(c, "POST", _path(p, args), json_body=body, with_status=True)
    if status >= 400:
        raise ConnectorError(f"HTTP {status}: {clip(text, 300)}")
    return _result(status, headers, text)


def _result(status, headers, text):
    if status >= 400:
        raise ConnectorError(f"HTTP {status}: {clip(text, 300)}")
    if "json" in headers.get("content-type", ""):
        try:
            return {"status": status, "json": json.loads(text)}
        except json.JSONDecodeError:
            pass
    return {"status": status, "text": clip(text)}
