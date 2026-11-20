"""Tạo một sự cố mẫu đủ dữ liệu để xem panel sự cố mà không cần cluster hay LLM.

Dùng (cùng thư mục dữ liệu với app đang chạy, mặc định data/):
    KAGENT_DATA_DIR=data .venv/bin/python scripts/seed_panel_demo.py [--tenant team-platform] [--user admin]

Kịch bản: Online Boutique trong namespace boutique. Rollout currencyservice v1.8.3 đổi tên port container thành "grpc" nhưng Service
vẫn trỏ targetPort "grpc-api", nên frontend bị từ chối kết nối khi gọi currencyservice. Lượt 1 đoán nhầm DNS, lượt 2 ra đúng nguyên nhân.
Mỗi lần chạy tạo một sự cố mới; xoá bằng cách đóng sự cố trên giao diện.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kagent import db  # noqa: E402

NS = "boutique"
CUR_POD = "currencyservice-6c9f7d8b54-q7x2m"
FE_POD = "frontend-7d4b9c6f8d-k2p9s"
CO_POD = "checkoutservice-5f8b6d7c9b-h4n3t"
FE_POD2 = "frontend-7d4b9c6f8d-m8r2v"


def series(now, minutes, fn, step=60):
    start = now - timedelta(minutes=minutes)
    pts = []
    for i in range(minutes * 60 // step + 1):
        t = start + timedelta(seconds=i * step)
        pts.append([int(t.timestamp()), round(fn(i * step / 60.0), 4)])
    return pts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="team-platform")
    ap.add_argument("--user", default="admin")
    args = ap.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    tenant = conn.execute("SELECT id FROM tenant WHERE name=?", (args.tenant,)).fetchone() or conn.execute("SELECT id FROM tenant LIMIT 1").fetchone()
    user = conn.execute("SELECT id FROM app_user WHERE username=?", (args.user,)).fetchone()
    if not tenant or not user:
        sys.exit("Chưa có tenant hoặc user: chạy app một lần để khởi tạo dữ liệu mẫu trước.")
    tid, uid = tenant["id"], user["id"]
    rnd = random.Random(7)
    now = db.now()
    iso = lambda m: db.iso(now - timedelta(minutes=m))  # noqa: E731
    onset, rollout = 34, 36  # phút trước hiện tại
    inc_id, conv_id = db.new_id(), db.new_id()
    ep1, ep2 = db.new_id(), db.new_id()

    with db.tx(conn):
        conn.execute("INSERT INTO conversation(id,tenant_id,user_id,title,created_at,updated_at,incident_id) VALUES (?,?,?,?,?,?,?)",
                     (conv_id, tid, uid, "Sự cố: frontend lỗi gọi currencyservice", iso(33), iso(7), inc_id))
        conn.execute("INSERT INTO incident(id,tenant_id,env,title,severity,status,stage,root_cause,confidence,evidence,episode_id,created_at,updated_at,"
                     "last_alert_at,conversation_id,cause,cause_at,cause_episode,investigations,summary,summary_at,summary_basis) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (inc_id, tid, "dev", "frontend lỗi gọi currencyservice", "critical", "open", "awaiting_approval",
                      "frontend lỗi DNS khi gọi productcatalogservice", 0.4, "[]", ep2, iso(33), iso(7), iso(2), conv_id,
                      "Service currencyservice trỏ targetPort 'grpc-api' nhưng sau rollout v1.8.3 container chỉ còn port tên 'grpc', "
                      "nên endpoint rỗng và frontend bị từ chối kết nối tới currencyservice:7000.", iso(7), ep2, 2,
                      "Từ khoảng 34 phút trước, frontend ở môi trường dev không gọi được currencyservice nên trang thanh toán lỗi. "
                      "Nguyên nhân là Service currencyservice trỏ sai tên port sau lần rollout v1.8.3. Đang chờ duyệt lệnh sửa targetPort.",
                      iso(6), iso(7)))
        alerts = [("KubeServiceNoEndpoints", "critical", {"namespace": NS, "service": "currencyservice"}, 33),
                  ("HighErrorRate", "warning", {"namespace": NS, "deployment": "frontend"}, 32),
                  ("HighLatencyP95", "warning", {"namespace": NS, "deployment": "checkoutservice"}, 30)]
        for name, sev, labels, ago_m in alerts:
            conn.execute("INSERT INTO alert_event(id,tenant_id,source,fingerprint,starts_at,alertname,severity,labels,summary,env,status,first_seen,last_seen,"
                         "incident_id,join_method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (db.new_id(), tid, "demo", db.new_id()[:16], iso(ago_m), name, sev, json.dumps({"alertname": name, **labels}), None, "dev",
                          "firing", iso(ago_m), iso(1), inc_id, "root" if ago_m == 33 else "rule"))
        for at, kind, text in [(33, "opened", "mở sự cố từ cảnh báo KubeServiceNoEndpoints"),
                               (25, "investigated", "9 lệnh đọc → frontend lỗi DNS khi gọi productcatalogservice (độ tin 0.40)"),
                               (7, "investigated", "14 lệnh đọc → Service currencyservice trỏ sai targetPort (độ tin 0.85)")]:
            conn.execute("INSERT INTO incident_event(tenant_id,incident_id,at,kind,text,data,actor) VALUES (?,?,?,?,?,?,?)",
                         (tid, inc_id, iso(at), kind, text, "{}", "kagent" if kind == "investigated" else "alert"))

        def step(tool, a, result):
            return {"type": "tool_call", "tool": tool, "args": a, "status": "ok", "result": result, "ms": rnd.randint(80, 900)}

        trace2 = [step("k8s.kubectl_read", {"command": f"get svc currencyservice -n {NS} -o yaml"}, "targetPort: grpc-api"),
                  step("k8s.kubectl_read", {"command": f"get endpoints currencyservice -n {NS}"}, "currencyservice   <none>"),
                  step("k8s.kubectl_read", {"command": f"describe pod {CUR_POD} -n {NS}"}, "Ports: 7000/TCP (grpc)"),
                  step("k8s.kubectl_read", {"command": f"rollout history deploy/currencyservice -n {NS}"}, "3  image currency:v1.8.3"),
                  step("k8s.kubectl_read", {"command": f"logs {FE_POD} -n {NS} --tail 200 | grep -c refused"}, "130")]
        for ep, at, task, out, trace in [
            (ep1, 26, "Điều tra sự cố từ các cảnh báo KubeServiceNoEndpoints, HighErrorRate.", "Lượt 1: nghi frontend lỗi DNS.", [
                step("k8s.kubectl_read", {"command": f"logs {FE_POD} -n {NS} --tail 200"}, "dial tcp: lookup productcatalogservice ... no such host")]),
            (ep2, 9, "Điều tra lại sự cố.", "**Nguyên nhân:** Service `currencyservice` trỏ `targetPort: grpc-api` nhưng container chỉ còn port `grpc` "
             "sau rollout v1.8.3, nên endpoint rỗng.\n\n**Đề xuất:** sửa targetPort của Service thành `grpc` (đảo ngược được).", trace2)]:
            conn.execute("INSERT INTO episode(id,tenant_id,user_id,agent_id,scope,task_type,env,started_at,input,trace,output,conversation_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ep, tid, uid, "alert-agent", "tenant", "k8s_ops", "dev", iso(at), json.dumps({"task": task}, ensure_ascii=False),
                          json.dumps(trace, ensure_ascii=False), out, conv_id))

        appr, act = db.new_id(), db.new_id()
        patch = {"command": f"patch svc currencyservice -n {NS} --type merge -p '{{\"spec\":{{\"ports\":[{{\"port\":7000,\"targetPort\":\"grpc\"}}]}}}}'"}
        conn.execute("INSERT INTO approval_request(id,tenant_id,episode_id,requester_id,original_request,tool,env,args,risk_tier,confidence,reasons,status,"
                     "created_at,expires_at,why) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (appr, tid, ep2, uid, "Điều tra lại sự cố.", "k8s.kubectl_write", "dev", json.dumps(patch), "R2", 0.85,
                      json.dumps(["lệnh ghi cần duyệt"]), "pending", iso(7), db.iso(now + timedelta(hours=6)),
                      "Sửa targetPort của Service currencyservice về 'grpc' để có lại endpoint"))
        conn.execute("INSERT INTO incident_action(id,tenant_id,incident_id,episode_id,approval_id,origin,requested_by,tool,args,why,risk_tier,reasons,status,"
                     "proposed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (act, tid, inc_id, ep2, appr, "investigation", uid, "k8s.kubectl_write", json.dumps(patch),
                      "Sửa targetPort của Service currencyservice về 'grpc' để có lại endpoint", "R2", "[]", "pending", iso(7)))
        conn.execute("INSERT INTO incident_task(id,tenant_id,incident_id,text,source,created_at) VALUES (?,?,?,?,?,?)",
                     (db.new_id(), tid, inc_id, "Báo team currency sửa Helm chart để tên port khớp Service trước bản phát hành sau", "summary", iso(6)))

        cur_svc, cur_dep, cur_pod = f"{NS}/service/currencyservice", f"{NS}/deployment/currencyservice", f"{NS}/pod/{CUR_POD}"
        fe_dep, fe_pod, fe_svc = f"{NS}/deployment/frontend", f"{NS}/pod/{FE_POD}", f"{NS}/service/frontend"
        fe_pod2 = f"{NS}/pod/{FE_POD2}"
        co_dep, co_pod = f"{NS}/deployment/checkoutservice", f"{NS}/pod/{CO_POD}"
        cart_dep, redis, ingress, payment = f"{NS}/deployment/cartservice", f"{NS}/cache/redis-cart", f"{NS}/ingress/shop", "-/external/payment-api"
        w1, w2 = "-/node/worker-1", "-/node/worker-2"
        links = lambda q: {"logs": f"http://127.0.0.1:5601/app/discover#/?q={q}", "grafana": f"http://127.0.0.1:3000/explore?q={q}"}  # noqa: E731
        errs = lambda base, peak: series(now, 60, lambda m: round(peak * (0.9 + rnd.random() * 0.2)) if m >= 60 - onset else rnd.randint(0, base))  # noqa: E731
        graph = {"nodes": [
            {"id": ingress, "kind": "ingress", "name": "shop", "status": "ok", "attrs": {"ready": "1 rule"}},
            {"id": fe_svc, "kind": "service", "name": "frontend", "status": "ok", "attrs": {"ready": "2 endpoint"}},
            {"id": fe_dep, "kind": "deployment", "name": "frontend", "status": "bad",
             "attrs": {"ready": "2/2", "restarts": 0, "image": "frontend:v0.10.2", "links": links("frontend"),
                       "logs": {"errors": 12400, "baseline_per_min": 3, "series": errs(5, 380), "link": links("frontend")["logs"]},
                       "log_patterns": [
                           {"template": "connection refused to <*>:7000", "count": 12400, "status": "new", "first_seen": iso(onset),
                            "sample": "rpc error: code = Unavailable desc = dial tcp 10.42.3.17:7000: connect: connection refused"},
                           {"template": "request <*> completed in <*>ms", "count": 40210, "status": "normal", "sample": "request GET /product/OLJCESPC7Z completed in 38ms"},
                           {"template": "failed to retrieve ads", "count": 210, "status": "normal", "sample": "failed to retrieve ads: context canceled"}]}},
            {"id": fe_pod, "kind": "pod", "name": FE_POD, "status": "bad", "attrs": {"ready": "1/1", "restarts": 0, "host": "worker-2"}},
            {"id": fe_pod2, "kind": "pod", "name": FE_POD2, "status": "bad", "attrs": {"ready": "1/1", "restarts": 0, "host": "worker-1"}},
            {"id": cur_svc, "kind": "service", "name": "currencyservice", "status": "bad", "suspect": True,
             "attrs": {"ready": "0 endpoint (trước đó 1)", "events": [], "links": links("currencyservice")}},
            {"id": cur_dep, "kind": "deployment", "name": "currencyservice", "status": "ok",
             "attrs": {"ready": "1/1", "restarts": 0, "image": "currency:v1.8.3", "links": links("currencyservice"),
                       "changes": [{"at": iso(rollout), "type": "rollout", "detail": "v1.8.2 → v1.8.3 (port container đổi tên grpc-api → grpc)"}],
                       "events": [{"reason": "ScalingReplicaSet", "count": 2}],
                       "logs": {"errors": 0, "baseline_per_min": 0, "series": series(now, 60, lambda m: 0), "link": links("currencyservice")["logs"]},
                       "log_patterns": [{"template": "conversion request successful", "count": 18, "status": "normal",
                                         "sample": "conversion request successful (traffic gần như bằng 0 từ 14:04)"}]}},
            {"id": cur_pod, "kind": "pod", "name": CUR_POD, "status": "ok", "attrs": {"ready": "1/1", "restarts": 0, "image": "currency:v1.8.3", "host": "worker-2"}},
            {"id": co_dep, "kind": "deployment", "name": "checkoutservice", "status": "warn",
             "attrs": {"ready": "1/1", "restarts": 0, "image": "checkout:v0.10.2",
                       "logs": {"errors": 3100, "baseline_per_min": 2, "series": errs(3, 95), "link": links("checkoutservice")["logs"]},
                       "log_patterns": [{"template": "deadline exceeded calling <*>", "count": 3100, "status": "up", "factor": 40, "first_seen": iso(onset - 1),
                                         "sample": "rpc error: code = DeadlineExceeded desc = context deadline exceeded (currencyservice.Convert)"}]}},
            {"id": co_pod, "kind": "pod", "name": CO_POD, "status": "warn", "attrs": {"ready": "1/1", "host": "worker-1"}},
            {"id": cart_dep, "kind": "deployment", "name": "cartservice", "status": "ok", "attrs": {"ready": "1/1", "image": "cart:v0.10.2"}},
            {"id": redis, "kind": "cache", "name": "redis-cart", "status": "ok", "attrs": {"ready": "1/1"}},
            {"id": payment, "kind": "external", "name": "payment-api", "status": "ok", "attrs": {}},
            {"id": w1, "kind": "node", "name": "worker-1", "status": "ok", "attrs": {"ready": "Ready"}},
            {"id": w2, "kind": "node", "name": "worker-2", "status": "ok", "attrs": {"ready": "Ready"}},
        ], "edges": [
            # contains: gom nhóm; depends_on: A cần B, lỗi lan ngược chiều cạnh.
            {"from": fe_dep, "to": fe_pod, "rel": "contains"}, {"from": fe_dep, "to": fe_pod2, "rel": "contains"},
            {"from": fe_svc, "to": fe_pod, "rel": "contains"}, {"from": fe_svc, "to": fe_pod2, "rel": "contains"},
            {"from": cur_dep, "to": cur_pod, "rel": "contains"}, {"from": cur_svc, "to": cur_pod, "rel": "contains"},
            {"from": co_dep, "to": co_pod, "rel": "contains"},
            {"from": ingress, "to": fe_svc, "rel": "depends_on", "sub": "routes_to"},
            {"from": fe_pod, "to": cur_svc, "rel": "depends_on", "sub": "calls", "attrs": {"rps": 62, "error_rate": 0.12}},
            {"from": fe_pod2, "to": cur_svc, "rel": "depends_on", "sub": "calls", "attrs": {"rps": 58, "error_rate": 0.12}},
            {"from": fe_dep, "to": co_dep, "rel": "depends_on", "sub": "calls", "attrs": {"rps": 30, "error_rate": 0.08}},
            {"from": fe_dep, "to": cart_dep, "rel": "depends_on", "sub": "calls", "attrs": {"rps": 80, "error_rate": 0.0}},
            {"from": co_pod, "to": cur_svc, "rel": "depends_on", "sub": "calls", "attrs": {"rps": 30, "error_rate": 0.1, "p95_ms": 820}},
            {"from": co_dep, "to": payment, "rel": "depends_on", "sub": "calls", "attrs": {"rps": 3, "error_rate": 0.0}},
            {"from": cart_dep, "to": redis, "rel": "depends_on", "sub": "reads", "attrs": {"rps": 160}},
            {"from": fe_pod, "to": w2, "rel": "depends_on", "sub": "runs_on"}, {"from": fe_pod2, "to": w1, "rel": "depends_on", "sub": "runs_on"},
            {"from": cur_pod, "to": w2, "rel": "depends_on", "sub": "runs_on"}, {"from": co_pod, "to": w1, "rel": "depends_on", "sub": "runs_on"},
        ]}
        noise = lambda a: rnd.uniform(-a, a)  # noqa: E731
        after = lambda m: m >= 60 - onset  # noqa: E731
        signals = [
            {"entity": fe_dep, "name": "5xx rate", "status": "bad", "score": 9.4, "now": 0.124, "baseline": 0.002,
             "series": series(now, 60, lambda m: (0.12 + noise(0.01)) if after(m) else (0.002 + abs(noise(0.001)))), "link": links("frontend")["grafana"]},
            {"entity": cur_svc, "name": "endpoints ready", "status": "bad", "score": 10, "rule": "0 endpoint từ 34 phút trước (trước đó 1)", "now": 0, "baseline": 1,
             "series": series(now, 60, lambda m: 0 if after(m) else 1), "link": links("currencyservice")["grafana"]},
            {"entity": co_dep, "name": "p95 latency (ms)", "status": "warn", "score": 4.1, "now": 820, "baseline": 140,
             "series": series(now, 60, lambda m: (780 + noise(80)) if after(m) else (140 + noise(15))), "link": links("checkoutservice")["grafana"]},
            {"entity": cur_dep, "name": "memory / limit", "status": "ok", "score": 0.6, "now": 0.41, "baseline": 0.40,
             "series": series(now, 60, lambda m: 0.40 + 0.01 * math.sin(m / 5) + noise(0.005)), "link": links("currencyservice")["grafana"]},
        ]
        ents2 = [{"id": cur_svc, "kind": "service", "name": "currencyservice", "namespace": NS, "from": ["alert", "trace"], "suspect": True},
                 {"id": cur_dep, "kind": "deployment", "name": "currencyservice", "namespace": NS, "from": ["trace"], "suspect": False},
                 {"id": fe_dep, "kind": "deployment", "name": "frontend", "namespace": NS, "from": ["alert", "trace"], "suspect": False},
                 {"id": co_dep, "kind": "deployment", "name": "checkoutservice", "namespace": NS, "from": ["alert"], "suspect": False},
                 {"id": cur_pod, "kind": "pod", "name": CUR_POD, "namespace": NS, "from": ["trace"], "suspect": False},
                 {"id": fe_pod, "kind": "pod", "name": FE_POD, "namespace": NS, "from": ["alert", "trace"], "suspect": False}]
        rounds = [
            (1, ep1, 25, 9, {"symptom": "frontend lỗi gọi currencyservice", "root_cause": "frontend lỗi DNS khi gọi productcatalogservice", "confidence": 0.4,
                            "evidence": ["frontend log: lookup productcatalogservice: no such host (37 lần)"]},
             [e for e in ents2 if e["id"] in (fe_dep, fe_pod)], [], {}),
            (2, ep2, 7, 14, {"symptom": "frontend lỗi gọi currencyservice",
                            "root_cause": "Service currencyservice trỏ targetPort 'grpc-api' nhưng sau rollout v1.8.3 container chỉ còn port tên 'grpc', "
                                          "nên endpoint rỗng và frontend bị từ chối kết nối tới currencyservice:7000.",
                            "confidence": 0.85,
                            "evidence": ["Service currencyservice: targetPort grpc-api; container port name: grpc",
                                         "Endpoints currencyservice rỗng từ 34 phút trước",
                                         "rollout currencyservice v1.8.2 → v1.8.3 lúc 2 phút trước khi cảnh báo bắn",
                                         "frontend log: 130 lỗi connection refused tới currencyservice:7000",
                                         "checkoutservice p95 tăng từ 140 ms lên 820 ms vì chờ currencyservice"]},
             ents2, signals, graph),
        ]
        for n, ep, at, nsteps, report, ents, sigs, g in rounds:
            conn.execute("INSERT INTO incident_round(id,tenant_id,incident_id,round,episode_id,created_at,steps,report,entities,signals,graph) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (db.new_id(), tid, inc_id, n, ep, iso(at), nsteps, json.dumps(report, ensure_ascii=False), json.dumps(ents, ensure_ascii=False),
                          json.dumps(sigs, ensure_ascii=False), json.dumps(g, ensure_ascii=False)))
    print(f"Đã tạo sự cố {inc_id}\nHội thoại điều tra: /#/t/{tid}/chat/{conv_id}")


if __name__ == "__main__":
    main()
