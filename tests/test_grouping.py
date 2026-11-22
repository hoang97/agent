"""Gom cảnh báo nâng cao (docs/GROUPING-PLAN.md): chấm điểm bằng chứng, gom theo lô, topology k8s, gốc/triệu chứng,
lịch sử gộp/tách, giảm nhiễu, đo chất lượng và chạy lại trên dữ liệu cũ."""
import importlib.util
from pathlib import Path

import pytest

from kagent import db, incidents, llm
from kagent.config import settings
from kagent.incidents import scoring, topology
from test_incidents import am, flow, poll, tenant  # noqa: F401  (flow là fixture)


def configure(admin, investigate=False, **extra):
    grouping = extra.pop("grouping", {})
    r = admin.put("/api/alert-settings", json={"enabled": True, "sources": ["am"], "investigate": investigate, "grouping": grouping, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def v(id, labels, alertname="X", starts="2026-11-12T08:00:00Z", fp=None):
    return scoring.view({"id": id, "alertname": alertname, "labels": labels, "starts_at": starts, "fingerprint": fp or id, "severity": "warning"})


def open_items(admin):
    return admin.get("/api/incidents").json()["items"]


TOPO = scoring.Topology([("shop/svc/frontend", "shop/workload/frontend", "selects"), ("pay/svc/currency", "pay/workload/currency", "selects"),
                         ("shop/workload/frontend", "pay/svc/currency", "calls"), ("shop/ingress/web", "shop/svc/frontend", "routes")])


# ---------- chấm điểm (thuần dữ liệu) ----------

@pytest.mark.parametrize("raw, iso", [
    ("2026-11-12T08:00:00Z", "2026-11-12T08:00:00+00:00"), ("2026-11-12T08:00:00.123456789Z", "2026-11-12T08:00:00.123456+00:00"),
    ("2026-11-12T15:00:00+07:00", "2026-11-12T08:00:00+00:00"), ("", None), ("hôm qua", None),
])
def test_parse_alert_times(raw, iso):
    got = scoring.parse_ts(raw)
    assert (got.isoformat() if got else None) == iso


@pytest.mark.parametrize("a, b, score, method, why", [
    ({"namespace": "web", "pod": "api-7d9f8c6b5-x2k4q"}, {"namespace": "web", "deployment": "api"}, 0.9, "topology", "cùng workload api"),
    ({"namespace": "pay", "service": "currency"}, {"namespace": "pay", "pod": "currency-7d9f8c6b5-x2k4q"}, 0.9, "topology",
     "Service currency trỏ tới currency"),
    ({"namespace": "shop", "deployment": "frontend"}, {"namespace": "pay", "service": "currency"}, 0.8, "topology", "frontend gọi Service currency"),
    ({"namespace": "a", "node": "n1"}, {"namespace": "b", "node": "n1"}, 0.7, "topology", "cùng node n1"),
    ({"namespace": "web", "deployment": "api"}, {"namespace": "web", "deployment": "db"}, 0.4, "score", "cùng namespace web"),
    ({"namespace": "a"}, {"namespace": "b"}, 0.0, None, ""),
    ({"cluster": "c1", "namespace": "web", "deployment": "api"}, {"cluster": "c2", "namespace": "web", "deployment": "api"}, 0.0, None, "khác cluster"),
])
def test_structural_evidence(a, b, score, method, why):
    ev = scoring.pair_score(v("a", a), v("b", b, starts="2026-11-12T09:00:00Z"), scoring.Context(TOPO, ["cluster"], burst_seconds=120))
    assert (ev.score, ev.method) == (score, method) and why in ev.reason


def test_burst_history_refire_and_block():
    a = v("a", {"namespace": "web", "deployment": "api"}, "Latency")
    b = v("b", {"namespace": "web", "deployment": "db"}, "DBDown", starts="2026-11-12T08:01:00Z")
    assert scoring.pair_score(a, b, scoring.Context(burst_seconds=120)).score == 0.6  # cùng namespace + cùng đợt: chưa đủ để gom
    stats = scoring.PairStats.build([["Latency/workload", "DBDown/workload"]] * 3 + [["Latency/workload"]], [])
    ev = scoring.pair_score(a, b, scoring.Context(burst_seconds=120, stats=stats))
    assert (ev.score, ev.method) == (0.9, "score") and "3/3 lần" in ev.reason and "cách nhau 60 giây" in ev.reason
    mixed = scoring.PairStats.build([["Latency/workload", "DBDown/workload"]] * 2, [("DBDown/workload", ["Latency/workload"])])
    assert mixed.bonus("Latency/workload", "DBDown/workload")[0] == 0.1
    assert scoring.PairStats.build([["Latency/workload", "DBDown/workload"]] * 2, []).bonus("Latency/workload", "DBDown/workload") == (0.0, "")
    blocked = scoring.Context(burst_seconds=120, stats=stats, blocked={frozenset(("Latency/workload", "DBDown/workload"))})
    assert scoring.pair_score(a, b, blocked).score == 0.0
    again = scoring.pair_score(v("c", {"namespace": "x"}, fp="F"), v("d", {"namespace": "y"}, fp="F"), scoring.Context())
    assert (again.score, again.method) == (1.0, "rule")


def test_roles_follow_dependencies():
    fe = v("fe", {"namespace": "shop", "deployment": "frontend"}, "HighErrorRate")
    probe = v("probe", {"namespace": "shop", "service": "frontend"}, "ProbeFailed")  # cùng đơn vị với frontend
    cur = v("cur", {"namespace": "pay", "service": "currency"}, "KubeServiceNoEndpoints")
    other = v("x", {"namespace": "ops"}, "DiskFull")
    roles = scoring.roles([fe, probe, cur, other], TOPO)
    assert {k: r[0] for k, r in roles.items()} == {"fe": "symptom", "probe": "symptom", "cur": "root"}
    assert "Service currency" in roles["fe"][1] and "HighErrorRate" in roles["cur"][1]
    assert scoring.roles([fe, cur], scoring.Topology()) == {}  # không có topology thì không đoán theo giờ bắn


def test_topology_parse_and_edges():
    text = "\n".join([
        'Service\tshop\tfrontend\t{"app":"frontend"}\t\t \t',
        'Deployment\tshop\tfrontend\t{"matchLabels":{"app":"frontend"}}\t{"app":"frontend","tier":"web"}\t \t'
        "currency.pay:7000 http://cart:7070 redis.cache.svc.cluster.local:6379 api.stripe.com",
        "dòng tiếp của biến nhiều dòng cart.other:1",
        "Service\tshop\tcart\tmap[app:cart]\t\t \t",
        "Deployment\tshop\tcart\t\tmap[app:cart]\t \t",
        "Ingress\tshop\tweb\t\t\t frontend\t",
    ])
    items = topology.parse(text)
    assert [i["kind"] for i in items] == ["svc", "workload", "svc", "workload", "ingress"] and "cart.other:1" in items[1]["env"]
    edges = topology.edges(items, {"shop", "pay"})
    assert {(s, d) for s, d, k in edges if k == "selects"} == {("shop/svc/frontend", "shop/workload/frontend"), ("shop/svc/cart", "shop/workload/cart")}
    assert [(s, d) for s, d, k in edges if k == "routes"] == [("shop/ingress/web", "shop/svc/frontend")]
    # tên miền ngoài (api.stripe.com) và "svc.ns" với ns chưa đọc (cart.other) không thành cạnh
    assert {(s, d) for s, d, k in edges if k == "calls"} == {("shop/workload/frontend", "pay/svc/currency"),
                                                             ("shop/workload/frontend", "shop/svc/cart"), ("shop/workload/frontend", "cache/svc/redis")}


@pytest.mark.parametrize("g, message", [
    ({"x": 1}, "grouping chỉ gồm"), ({"join_threshold": 0.3}, "join_threshold"), ({"suggest_threshold": 0.9, "join_threshold": 0.8}, "suggest_threshold"),
    ({"topology_minutes": 2}, "topology_minutes"), ({"flap_count": 1}, "flap_count"), ({"open_min_severity": "high"}, "open_min_severity"),
    ({"partition_labels": ["a b"]}, "partition_labels"),
])
def test_grouping_settings_validation(g, message):
    with pytest.raises(incidents.IncidentError, match=message):
        incidents.check_settings({"grouping": g})
    assert not any(incidents.check_settings({})["grouping"][k] for k in ("batch", "scoring", "topology_minutes", "flap_count", "open_min_severity"))


# ---------- gom theo lô ----------

A = ({"alertname": "Latency", "severity": "warning", "namespace": "web", "service": "api"}, "fa")
B = ({"alertname": "Errors", "severity": "warning", "namespace": "web", "service": "db"}, "fb")
C = ({"alertname": "PodsPending", "severity": "warning", "namespace": "web"}, "fc")


@pytest.mark.parametrize("batch, order, expected", [
    (False, [A, B, C], 2), (False, [C, A, B], 1),  # luật cũ: kết quả phụ thuộc cảnh báo nào được xét trước
    (True, [A, B, C], 1), (True, [C, A, B], 1),
])
def test_batch_grouping_does_not_depend_on_arrival_order(flow, users, infra, batch, order, expected):
    configure(flow, ai_grouping=False, group_labels=["cluster", "namespace", "service"], grouping={"batch": batch})
    infra.am_alerts[:] = [am(labels, fp) for labels, fp in order]
    poll(users)
    assert len(open_items(flow)) == expected


class Counting(llm.HeuristicProvider):
    def __init__(self, answer=None):
        self.calls, self.seen, self.answer = 0, [], answer

    def correlate(self, alert, incidents_, feedback):
        self.calls += 1
        self.seen.append((alert, incidents_))
        target = incidents_[0]["id"] if self.answer else None
        return {"incident_id": target, "score": self.answer or 0.0, "reason": "LLM thấy liên quan"}


def test_storm_on_one_workload_opens_one_incident_with_one_llm_call(flow, users, infra):
    configure(flow, grouping={"batch": True, "scoring": True})
    provider = Counting()
    llm.set_provider(provider)
    infra.am_alerts[:] = [am({"alertname": "DBSlow", "severity": "warning", "namespace": "pay", "deployment": "db"}, "db1")]
    poll(users)
    infra.am_alerts += [am({"alertname": "PodCrash", "severity": "critical", "namespace": "pay", "pod": f"checkout-7d9f8c6b5-{i:05d}"}, f"p{i}",
                           starts="2026-11-12T08:30:00Z") for i in range(50)]
    poll(users)
    items = sorted(open_items(flow), key=lambda i: i["alerts"])
    assert [i["alerts"] for i in items] == [1, 50]
    # Sự cố DBSlow chỉ cùng namespace (0.4): điểm lưng chừng nên hỏi LLM, một lần cho cả cụm, kèm bằng chứng đã thấy.
    assert provider.calls == 1
    alert, offered = provider.seen[0]
    assert alert["count"] == 50 and len(alert["alerts"]) == 10 and "cùng namespace pay" in offered[0]["evidence"]
    crash = flow.get(f"/api/incidents/{items[1]['id']}").json()["alerts"]
    assert {a["join_method"] for a in crash} == {"root", "topology"} and "cùng workload checkout" in crash[1]["join_reason"]


def test_scoring_asks_llm_only_in_grey_zone_and_shared_namespace_stays_apart(flow, users, infra):
    configure(flow, grouping={"scoring": True})
    provider = Counting(answer=0.9)
    llm.set_provider(provider)
    infra.am_alerts[:] = [am({"alertname": "Latency", "severity": "warning", "namespace": "shared", "deployment": "api"}, "s1"),
                          am({"alertname": "Disk", "severity": "warning", "namespace": "ops", "node": "n1"}, "s2")]
    poll(users)
    assert len(open_items(flow)) == 2 and provider.calls == 0  # khác namespace, không có bằng chứng: không hỏi
    infra.am_alerts.append(am({"alertname": "QueueBacklog", "severity": "warning", "namespace": "shared", "deployment": "worker"}, "s3"))
    poll(users)
    assert provider.calls == 1  # cùng namespace với Latency: hỏi LLM, LLM nói liên quan thì gom
    joined = next(a for i in open_items(flow) for a in flow.get(f"/api/incidents/{i['id']}").json()["alerts"] if a["alertname"] == "QueueBacklog")
    assert joined["join_method"] == "ai"


# ---------- topology, gốc và triệu chứng ----------

def test_topology_links_namespaces_and_marks_root(flow, users, infra):
    configure(flow, grouping={"batch": True, "scoring": True, "topology_minutes": 30})
    infra.k8s_topology = {
        "shop": [("Service", "frontend", {"app": "frontend"}, None, [], []),
                 ("Deployment", "frontend", None, {"app": "frontend"}, [], ["currencyservice.pay:7000"])],
        "pay": [("Service", "currencyservice", {"app": "currency"}, None, [], []),
                ("Deployment", "currencyservice", None, {"app": "currency"}, [], [])],
    }
    infra.am_alerts[:] = [am({"alertname": "HighErrorRate", "severity": "warning", "namespace": "shop", "deployment": "frontend"}, "e1",
                             starts="2026-11-12T08:01:00Z"),
                          am({"alertname": "KubeServiceNoEndpoints", "severity": "critical", "namespace": "pay", "service": "currencyservice"}, "e2")]
    poll(users)
    assert len(open_items(flow)) == 2  # chưa có topology: khác namespace, chỉ cùng đợt
    t = tenant(users)
    with db.session() as conn:
        status = topology.refresh(conn, t, "dev")
        assert status["edges"] == 3 and sorted(infra.topology_reads) == ["pay", "shop"] and not status["errors"]
        assert conn.execute("SELECT count(*) n FROM tool_call_audit WHERE tool='k8s.kubectl_read' AND tenant_id=?", (t,)).fetchone()["n"] == 2
        assert incidents.auto_merge(conn, t) == 1
        merged = conn.execute("SELECT text FROM incident_event WHERE kind='auto_merge'").fetchone()["text"]
    assert "frontend gọi Service currencyservice" in merged
    [item] = open_items(flow)
    d = flow.get(f"/api/incidents/{item['id']}").json()
    assert {a["alertname"]: a["role"] for a in d["alerts"]} == {"KubeServiceNoEndpoints": "root", "HighErrorRate": "symptom"}
    assert d["title"] == "KubeServiceNoEndpoints · pay"
    info = flow.get("/api/alert-settings").json()["topology"]
    assert info["envs"]["dev"]["edges"] == 3 and sorted(info["envs"]["dev"]["namespaces"]) == ["pay", "shop"]

    # Có topology rồi: cảnh báo mới ở frontend vào thẳng sự cố, và điều tra thấy cảnh báo gốc trước.
    infra.am_alerts.append(am({"alertname": "PodRestarts", "severity": "warning", "namespace": "shop", "pod": "frontend-7d4b9c6f8d-k2p9s"}, "e3"))
    poll(users)
    assert len(open_items(flow)) == 1
    with db.session() as conn:
        first = conn.execute("SELECT alertname FROM alert_event WHERE incident_id=? AND status='firing' ORDER BY role IS NOT 'root', starts_at",
                             (item["id"],)).fetchone()["alertname"]
        brief = incidents._brief(conn.execute("SELECT * FROM alert_event WHERE alertname='PodRestarts'").fetchone())
    assert first == "KubeServiceNoEndpoints" and brief["role"] == "symptom"


def test_topology_without_kubernetes_connection_reports_why(flow, users):
    configure(flow, grouping={"topology_minutes": 30})
    poll(users)
    with db.session() as conn:
        conn.execute("UPDATE connection SET status='disabled' WHERE template='k8s'")
        status = topology.refresh(conn, tenant(users), "dev")
    assert status["edges"] == 0 and "không có kết nối Kubernetes" in status["errors"][0]


# ---------- lịch sử gộp/tách ----------

def test_split_pair_is_not_regrouped(flow, users, infra):
    configure(flow, grouping={"batch": True, "scoring": True})
    infra.am_alerts[:] = [am({"alertname": "Latency", "severity": "warning", "namespace": "pay", "deployment": "checkout"}, "s1"),
                          am({"alertname": "OOMKilled", "severity": "warning", "namespace": "pay", "pod": "checkout-7d9f8c6b5-aaaaa"}, "s2")]
    poll(users)
    [item] = open_items(flow)
    oom = next(a for a in flow.get(f"/api/incidents/{item['id']}").json()["alerts"] if a["alertname"] == "OOMKilled")
    split = flow.post(f"/api/incidents/{item['id']}/split", json={"alert_id": oom["id"]}).json()["id"]
    infra.am_alerts.append(am({"alertname": "OOMKilled", "severity": "warning", "namespace": "pay", "pod": "checkout-7d9f8c6b5-bbbbb"}, "s3"))
    poll(users)
    t = tenant(users)
    with db.session() as conn:
        new = conn.execute("SELECT * FROM alert_event WHERE fingerprint='s3'").fetchone()
        m = incidents.Matcher(conn, t, incidents.settings(conn, t))
        mine = incidents.grouping._views(conn, "id=?", (new["id"],))
        assert m.best(mine, m.alerts_of(item["id"]), "dev") is None  # cặp Latency/OOMKilled người vừa tách
    assert new["incident_id"] == split and "cùng workload checkout" in new["join_reason"]


def test_pair_stats_count_finished_incidents_and_splits(flow, users, infra):
    poll(users)  # luật cũ gom HighCPU và Down cùng namespace web, rồi người tách ra
    items = {i["title"]: i["id"] for i in open_items(flow)}
    down = next(a for a in flow.get(f"/api/incidents/{items['HighCPU · web']}").json()["alerts"] if a["alertname"] == "Down")
    flow.post(f"/api/incidents/{items['HighCPU · web']}/split", json={"alert_id": down["id"]})
    infra.am_alerts[:] = []
    poll(users)  # mọi cảnh báo hết: sự cố thành đã hết
    with db.session() as conn:
        stats = incidents.pair_stats(conn, tenant(users))
    assert stats.counts[frozenset(("Down/ns", "HighCPU/ns"))] == [0, 1]


# ---------- giảm nhiễu ----------

@pytest.mark.parametrize("flap_count, reinvestigate", [(0, True), (2, False)])
def test_flapping_alert_reopens_without_reinvestigating(flow, users, infra, flap_count, reinvestigate):
    configure(flow, investigate=True, grouping={"flap_count": flap_count})
    infra.am_alerts[:] = [am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f1")]
    poll(users)
    [item] = open_items(flow)
    infra.am_alerts[:] = []
    poll(users)
    infra.am_alerts[:] = [am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "f1", starts="2026-11-12T08:05:00Z")]
    poll(users)
    with db.session() as conn:
        inc = conn.execute("SELECT status, investigate_after FROM incident WHERE id=?", (item["id"],)).fetchone()
        reopened = conn.execute("SELECT text FROM incident_event WHERE incident_id=? AND kind='reopened'", (item["id"],)).fetchone()["text"]
        flags = [r["flapping"] for r in conn.execute("SELECT flapping FROM alert_event ORDER BY rowid")]
    assert inc["status"] == "open" and (inc["investigate_after"] is not None) == reinvestigate
    assert flags == [0, int(not reinvestigate)] and ("chập chờn" in reopened) == (not reinvestigate)


def test_low_severity_alert_does_not_open_incident(flow, users, infra):
    configure(flow, grouping={"open_min_severity": "warning"})
    infra.am_alerts[:] = [am({"alertname": "Info", "severity": "info", "namespace": "ops"}, "i1")]
    poll(users)
    assert open_items(flow) == []
    infra.am_alerts += [am({"alertname": "Down", "severity": "critical", "namespace": "web"}, "d1"),
                        am({"alertname": "Hint", "severity": "info", "namespace": "web"}, "i2")]
    poll(users)
    [item] = open_items(flow)
    assert item["alerts"] == 2  # cảnh báo mức thấp vẫn vào sự cố đang có
    with db.session() as conn:
        orphan = conn.execute("SELECT incident_id, join_reason FROM alert_event WHERE fingerprint='i1'").fetchone()
    assert orphan["incident_id"] is None and "dưới ngưỡng mở sự cố (warning)" in orphan["join_reason"]
    assert flow.get("/api/metrics").json()["grouping"]["unattached"] == 1


# ---------- đo chất lượng và chạy lại ----------

def test_metrics_report_grouping_quality(flow, users):
    poll(users)
    items = {i["title"]: i["id"] for i in open_items(flow)}
    flow.post(f"/api/incidents/{items['HighCPU · payments']}/merge", json={"into": items["HighCPU · web"]})
    down = next(a for a in flow.get(f"/api/incidents/{items['HighCPU · web']}").json()["alerts"] if a["alertname"] == "Down")
    flow.post(f"/api/incidents/{items['HighCPU · web']}/split", json={"alert_id": down["id"]})
    g = flow.get("/api/metrics").json()["grouping"]
    assert (g["alerts"], g["incidents_opened"], g["incidents"], g["compression"]) == (3, 3, 2, 1.5)
    assert (g["splits"], g["manual_merges"], g["split_pct"]) == (1, 1, 33.3)


def test_pairwise_precision_recall():
    truth = {"a": "1", "b": "1", "c": "2", "d": "3"}
    assert incidents.quality.pairwise({"a": "x", "b": "x", "c": "y", "d": "z"}, truth)["f1"] == 1.0
    over = incidents.quality.pairwise({"a": "x", "b": "x", "c": "x", "d": "z"}, truth)
    assert (over["precision"], over["recall"]) == (0.333, 1.0)


def test_replay_compares_settings_on_recorded_alerts(flow, users):
    poll(users)  # luật cũ gom HighCPU và Down cùng namespace web
    items = {i["title"]: i["id"] for i in open_items(flow)}
    down = next(a for a in flow.get(f"/api/incidents/{items['HighCPU · web']}").json()["alerts"] if a["alertname"] == "Down")
    flow.post(f"/api/incidents/{items['HighCPU · web']}/split", json={"alert_id": down["id"]})  # người sửa: không liên quan
    spec = importlib.util.spec_from_file_location("grouping_replay", Path(__file__).resolve().parent.parent / "scripts" / "grouping_replay.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    out = replay.replay(settings.db_path, tenant(users), "dev", candidates={"chấm điểm": {"grouping": {"scoring": True, "batch": True}}})
    now, scored = out["hiện tại"], out["chấm điểm"]
    assert (now["alerts"], now["true_groups"], now["predicted_groups"], now["precision"]) == (3, 3, 2, 0.0)
    assert (scored["predicted_groups"], scored["precision"], scored["recall"]) == (3, 1.0, 1.0)
    with db.session() as conn:
        assert conn.execute("SELECT count(*) n FROM incident").fetchone()["n"] == 3  # DB gốc không bị đụng
