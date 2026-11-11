"""Alertmanager API v2: xem cảnh báo, silence; tạo và gỡ silence."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .base import ConnectorError, call, integer, monitoring_client, need

SILENCE_ID = r"[A-Za-z0-9-]{1,64}"
_MATCHER = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*\"?(.*?)\"?\s*$")


def parse_matchers(text: str) -> list[dict]:
    out = []
    for part in [x for x in str(text).split(",") if x.strip()]:
        m = _MATCHER.match(part)
        if not m or not m.group(3):
            raise ConnectorError("matchers có dạng nhan=gia_tri (hoặc !=, =~, !~), cách nhau dấu phẩy, ví dụ alertname=HighCPU,namespace=~pay.*")
        op = m.group(2)
        if "~" in op:
            try:
                re.compile(m.group(3))
            except re.error as exc:
                raise ConnectorError(f"biểu thức chính quy sai trong matcher {m.group(1)}: {exc}") from exc
        out.append({"name": m.group(1), "value": m.group(3), "isRegex": "~" in op, "isEqual": not op.startswith("!")})
    if not out:
        raise ConnectorError("cần ít nhất một matcher")
    return out


def _filter(matchers: list[dict]) -> list[str]:
    ops = {(True, False): "=", (True, True): "=~", (False, False): "!=", (False, True): "!~"}
    return [f'{m["name"]}{ops[(m.get("isEqual", True), m["isRegex"])]}"{m["value"]}"' for m in matchers]


def _matches(labels: dict, matchers: list[dict]) -> bool:
    for m in matchers:
        v = labels.get(m["name"], "")
        hit = bool(re.fullmatch(m["value"], v)) if m["isRegex"] else v == m["value"]
        if hit != m["isEqual"]:
            return False
    return True


def ping(p, args=None):
    with monitoring_client(p) as c:
        status = call(c, "GET", "/api/v2/status")
    return {"ok": True, "version": (status.get("versionInfo") or {}).get("version"), "cluster": (status.get("cluster") or {}).get("status")}


def _alerts(c, *, active=True, silenced=False, inhibited=False, matchers=None, receiver=None):
    params = {"active": str(active).lower(), "silenced": str(silenced).lower(), "inhibited": str(inhibited).lower()}
    if matchers:
        params["filter"] = _filter(matchers)
    if receiver:
        params["receiver"] = receiver
    return call(c, "GET", "/api/v2/alerts", params=params)


def list_alerts(p, args):
    matchers = parse_matchers(args["matchers"]) if args.get("matchers") else None
    with monitoring_client(p) as c:
        alerts = _alerts(c, silenced=bool(args.get("include_silenced")), inhibited=bool(args.get("include_silenced")), matchers=matchers,
                         receiver=str(args.get("receiver") or "")[:100] or None)
    limit = integer(args, "limit", 50, 1, 200)
    count = Counter(((a.get("labels") or {}).get("alertname"), (a.get("labels") or {}).get("severity")) for a in alerts)
    return {"total": len(alerts), "by_alert": [{"alertname": n, "severity": s, "count": k} for (n, s), k in count.most_common(30)],
            "alerts": [{"alertname": (a.get("labels") or {}).get("alertname"), "labels": a.get("labels"), "state": (a.get("status") or {}).get("state"),
                        "silenced_by": (a.get("status") or {}).get("silencedBy") or [], "since": a.get("startsAt"),
                        "summary": (a.get("annotations") or {}).get("summary") or (a.get("annotations") or {}).get("description"),
                        "fingerprint": a.get("fingerprint")} for a in alerts[:limit]]}


def list_silences(p, args):
    state = str(args.get("state") or "active")
    with monitoring_client(p) as c:
        items = call(c, "GET", "/api/v2/silences")
    items = [s for s in items if state == "all" or (s.get("status") or {}).get("state") == state]
    return {"total": len(items), "silences": [{"id": s.get("id"), "state": (s.get("status") or {}).get("state"), "matchers": _filter(s.get("matchers") or []),
                                               "starts": s.get("startsAt"), "ends": s.get("endsAt"), "created_by": s.get("createdBy"), "comment": s.get("comment")}
                                              for s in items[:100]]}


def create_silence_check(p, args):
    matchers = parse_matchers(need(args, "matchers"))
    with monitoring_client(p) as c:
        alerts = _alerts(c, matchers=matchers)
    hit = [a for a in alerts if _matches(a.get("labels") or {}, matchers)]
    return {"would_silence_alerts": len(hit), "alertnames": sorted({(a.get("labels") or {}).get("alertname") for a in hit} - {None})[:20], "objects": 1}


def create_silence(p, args):
    matchers = parse_matchers(need(args, "matchers"))
    minutes = integer(args, "duration_minutes", 60, 5, 7 * 24 * 60)
    now = datetime.now(timezone.utc)
    body = {"matchers": matchers, "startsAt": now.isoformat(), "endsAt": (now + timedelta(minutes=minutes)).isoformat(),
            "createdBy": "kagent", "comment": str(args.get("comment") or "tạo bởi Knowledge Agent")[:500]}
    with monitoring_client(p) as c:
        result = call(c, "POST", "/api/v2/silences", json_body=body)
    return {"silence_id": result.get("silenceID"), "minutes": minutes, "ends": body["endsAt"]}


def _silence(c, sid):
    s = call(c, "GET", f"/api/v2/silence/{sid}")
    return {"id": s.get("id"), "state": (s.get("status") or {}).get("state"), "matchers": _filter(s.get("matchers") or []), "comment": s.get("comment")}


def expire_silence_check(p, args):
    sid = need(args, "silence_id", SILENCE_ID)
    with monitoring_client(p) as c:
        s = _silence(c, sid)
    if s["state"] == "expired":
        raise ConnectorError("silence này đã hết hạn")
    return {"silence": s, "objects": 1}


def expire_silence(p, args):
    sid = need(args, "silence_id", SILENCE_ID)
    with monitoring_client(p) as c:
        before = _silence(c, sid)
        call(c, "DELETE", f"/api/v2/silence/{sid}")
    return {"expired": sid, "matchers": before["matchers"]}
