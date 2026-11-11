"""Lọc kết quả ở server: _filter, kết quả lớn lưu thành result_id, data.query và data.script."""
import json

import pytest

from conftest import tool_steps
from kagent import datafilter, db, gateway, results

LOGS = {"total": 5, "hits": [
    {"timestamp": "t1", "source": json.dumps({"level": "ERROR", "request_id": "a1", "msg": "409 Conflict"})},
    {"timestamp": "t2", "source": json.dumps({"level": "INFO", "request_id": "a1", "msg": "ok"})},
    {"timestamp": "t3", "source": json.dumps({"level": "ERROR", "request_id": "b2", "msg": "timeout"})},
    {"timestamp": "t4", "source": json.dumps({"level": "ERROR", "request_id": "c3", "msg": "409 Conflict"})},
    {"timestamp": "t5", "source": {"level": "WARN", "request_id": "d4", "msg": "slow"}},
]}


# ---------- bộ lọc khai báo ----------

def test_filter_where_fields_and_json_strings():
    out = datafilter.apply(LOGS, {"where": [{"field": "source.level", "op": "eq", "value": "ERROR"}], "fields": ["timestamp", "source.request_id"]})
    assert out["path"] == "hits" and out["total"] == 5 and out["matched"] == 3
    assert out["items"] == [{"timestamp": "t1", "source.request_id": "a1"}, {"timestamp": "t3", "source.request_id": "b2"},
                            {"timestamp": "t4", "source.request_id": "c3"}]


def test_filter_group_by_sort_limit_and_ops():
    groups = datafilter.apply(LOGS, {"group_by": "source.msg"})["groups"]
    assert groups[0] == {"key": "409 Conflict", "count": 2}
    assert datafilter.apply(LOGS, {"where": [{"field": "source.msg", "op": "regex", "value": "^40\\d"}]})["matched"] == 2
    assert datafilter.apply(LOGS, {"where": [{"field": "source.level", "op": "in", "value": ["WARN", "INFO"]}]})["matched"] == 2
    assert datafilter.apply(LOGS, {"where": [{"field": "nope", "op": "exists", "value": False}]})["matched"] == 5
    assert [i["timestamp"] for i in datafilter.apply(LOGS, {"sort": "-timestamp", "limit": 2})["items"]] == ["t5", "t4"]
    assert datafilter.apply({"rows": [{"n": 5}, {"n": 12}]}, {"where": [{"field": "n", "op": "gt", "value": 9}]})["matched"] == 1


@pytest.mark.parametrize("spec", [{"bogus": 1}, {"where": [{"field": "a", "op": "like"}]}, {"limit": 0}, {"fields": "a"}])
def test_filter_rejects_bad_spec(spec):
    with pytest.raises(datafilter.FilterError):
        datafilter.check(spec)


def test_preview_describes_shape():
    p = datafilter.preview(LOGS)
    assert p["path"] == "hits" and p["count"] == 5 and "source.request_id" in p["fields"] and len(p["sample"]) == 3


# ---------- qua gateway ----------

def _ctx(users, name="alice", tainted=False):
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username=?", (name,)).fetchone()["id"]
    return gateway.CallContext(users[name].tenant, uid, "dev", "tìm log lỗi", tainted=tainted)


def test_inline_filter_is_applied_before_result_reaches_agent(users):
    with db.session() as conn:
        step = gateway.invoke(conn, _ctx(users), "k8s.list_pods", {"_filter": {"fields": ["name"], "limit": 1}})
    assert step["status"] == "ok" and step["result"]["items"] == [{"name": step["result"]["items"][0]["name"]}] and step["result"]["matched"] >= 1


def test_bad_inline_filter_is_invalid(users):
    with db.session() as conn:
        step = gateway.invoke(conn, _ctx(users), "k8s.list_pods", {"_filter": {"where": "x"}})
    assert step["status"] == "invalid" and "_filter" in step["reasons"][0]


def test_large_result_is_stored_and_queried(users, monkeypatch):
    big = {"hits": [{"i": i, "level": "ERROR" if i % 3 == 0 else "INFO", "msg": "x" * 200} for i in range(300)]}
    real = gateway._run
    monkeypatch.setattr(gateway, "_run", lambda conn, t, name, args, check=False, user_id=None: big if name == "logs.search_logs" and not check
                        else real(conn, t, name, args, check, user_id))
    ep = users["alice"].run("tìm log lỗi")
    step = tool_steps(ep)[0]
    assert step["result_chars"] > results.INLINE_LIMIT and step["result"]["result_id"] == step["result_id"]
    assert step["result"]["preview"]["count"] == 300 and len(json.dumps(step["result"])) < results.INLINE_LIMIT
    with db.session() as conn:
        q = gateway.invoke(conn, _ctx(users, tainted=True), "data.query", {"result_id": step["result_id"], "group_by": "level"})
        assert q["status"] == "ok" and q["result"]["groups"] == [{"key": "INFO", "count": 200}, {"key": "ERROR", "count": 100}]
        assert q["untrusted"] is True and gateway.UNTRUSTED_REASON not in (q.get("reasons") or [])  # local: không bị bắt duyệt vì untrusted
        other = gateway.invoke(conn, _ctx(users, "bob"), "data.query", {"result_id": step["result_id"]})
    assert other["status"] == "error" and "không thuộc về bạn" in other["result"]["error"]


def test_builtin_data_connection_exists_for_every_tenant(users):
    with db.session() as conn:
        for t in conn.execute("SELECT id FROM tenant").fetchall():
            tools = {r["tool"]: (r["enabled"], r["risk_tier"]) for r in conn.execute("SELECT tool, enabled, risk_tier FROM tool_registry WHERE tenant_id=? AND template='data'", (t["id"],))}
            assert tools == {"data.query": (1, "R0"), "data.script": (1, "R4")}


def test_script_needs_approval_then_runs_in_sandbox(users):
    alice = users["alice"]
    with db.session() as conn:
        rid = results.save(conn, alice.tenant, _ctx(users).user_id, None, "logs.search_logs", LOGS, True)
        step = gateway.invoke(conn, _ctx(users), "data.script", {"result_ids": [rid], "code": (
            "ids = [json.loads(h['source'])['request_id'] if isinstance(h['source'], str) else h['source']['request_id'] for h in inputs[%r]['hits']]\n"
            "result = sorted(set(ids))" % rid)})
    assert step["status"] == "pending" and step["risk_tier"] == "R4"
    r = alice.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).json()
    assert r["status"] == "approved" and r["result"] == {"status": "ok", "result": {"result": ["a1", "b2", "c3", "d4"], "stdout": ""}}


@pytest.mark.parametrize("code, message", [
    ("result = open('/etc/passwd').read()", "chặn open"),
    ("import socket", "không được import socket"),
    ("import os; os.system('id')", "chặn os.system"),
    ("result = eval('1')", "chặn compile"),
    ("while True: pass", "vượt giới hạn CPU"),
])
def test_script_sandbox_blocks_escape(users, code, message, monkeypatch):
    from kagent.connectors import data
    monkeypatch.setattr(data, "SCRIPT_TIMEOUT", 2)
    with db.session() as conn:
        rid = results.save(conn, users["alice"].tenant, _ctx(users).user_id, None, "x", {"a": 1}, False)
    with pytest.raises(data.ConnectorError, match=message):
        data.script({"_tenant_id": users["alice"].tenant, "_user_id": _ctx(users).user_id}, {"result_ids": [rid], "code": code})


def test_learned_tool_keeps_inline_filter_but_skips_data_steps(users):
    alice = users["alice"]
    with db.session() as conn:
        ctx = _ctx(users)
        rid = results.save(conn, alice.tenant, ctx.user_id, None, "x", LOGS, False)
    ep = alice.run("xem pod trong namespace default")
    with db.session() as conn:
        trace = json.loads(conn.execute("SELECT trace FROM episode WHERE id=?", (ep["id"],)).fetchone()["trace"])
    step = next(s for s in trace if s["type"] == "tool_call")
    step["args"]["_filter"] = {"fields": ["name"]}
    trace.append({"type": "tool_call", "tool": "data.query", "args": {"result_id": rid}, "status": "ok", "result": {}})
    from kagent import learned
    with db.session() as conn:
        conn.execute("DROP TRIGGER episode_append_only")
        conn.execute("UPDATE episode SET trace=? WHERE id=?", (json.dumps(trace), ep["id"]))
        tool = learned.from_episode(conn, alice.tenant, ctx.user_id, ep["id"], "pod_names", None, "user")
    assert [s["tool"] for s in tool["steps"]] == ["k8s.list_pods"] and tool["steps"][0]["args"]["_filter"] == {"fields": ["name"]}


def test_purge_removes_old_results(users):
    with db.session() as conn:
        rid = results.save(conn, users["alice"].tenant, None, None, "x", {"a": 1}, False)
        conn.execute("UPDATE tool_result SET created_at=? WHERE id=?", (db.later(days=-8), rid))
        assert results.purge(conn) == 1


def test_filtered_call_keeps_large_raw_result_for_requery(users, monkeypatch):
    big = {"hits": [{"i": i, "source": {"level": "ERROR" if i % 2 else "INFO", "msg": "y" * 100}} for i in range(200)]}
    real = gateway._run
    from kagent import connections
    monkeypatch.setattr(connections, "resolve", lambda conn, t, name: (None, None, type("S", (), {"check": None, "run": staticmethod(lambda p, a: big),
                                                                                                    "untrusted_output": True})(), {}))
    with db.session() as conn:
        out = real(conn, users["alice"].tenant, "logs.search_logs", {"_filter": {"group_by": "source.level"}}, user_id=_ctx(users).user_id)
        assert out["groups"] == [{"key": "ERROR", "count": 100}, {"key": "INFO", "count": 100}]
        raw = results.load(conn, users["alice"].tenant, _ctx(users).user_id, out["raw_result_id"])
    assert raw["untrusted"] == 1 and len(raw["data"]["hits"]) == 200


def test_elasticsearch_source_stays_an_object_so_fields_can_be_grouped(infra):
    from kagent.connectors import elasticsearch
    import fakes
    hits = elasticsearch.search_logs({"_allow_private": False, "url": "https://es.test:9200", "api_key": fakes.ES_KEY}, {"query": "error"})["hits"]
    assert isinstance(hits[0]["source"], dict)
    assert datafilter.apply({"hits": hits}, {"group_by": "source.level"})["groups"] == [{"key": "error", "count": 1}, {"key": "warn", "count": 1}]
