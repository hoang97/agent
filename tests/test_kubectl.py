"""Công cụ kubectl_* và việc kagent tạo ServiceAccount/RBAC theo 3 nhóm quyền, chạy trên API server giả."""
import yaml
import pytest

import fakes
from conftest import Session
from kagent import connections, db, gateway
from kagent.connectors import k8s, k8s_rbac, kubectl
from kagent.connectors.base import ConnectorError

ADMIN_P = {"_allow_private": False, "api_url": "https://k8s.test:6443", "token": fakes.K8S_TOKEN, "namespace": "default"}
CID, CNAME = "0123456789abcdef-conn", "k8s_prod"
BASE = k8s_rbac.base_name(CID, CNAME)


def rule(ns="default", resources=("pods",), verbs=("get", "list"), groups=("",)):
    return {"namespaces": [ns], "api_groups": list(groups), "resources": list(resources), "verbs": list(verbs)}


def config(read=(), write=(), admin=()):
    return {"sa_namespace": "kagent-system", "profiles": {"read": list(read), "write": list(write), "admin": list(admin)}}


@pytest.fixture()
def calls(monkeypatch):
    """Thay subprocess: ghi lại argv, env, stdin và kubeconfig tạm của từng lần gọi."""
    seen, replies = [], []

    def fake(argv, env, stdin, timeout):
        with open(env["KUBECONFIG"], encoding="utf-8") as f:
            seen.append({"argv": argv, "env": env, "stdin": stdin, "timeout": timeout, "config": yaml.safe_load(f)})
        return replies.pop(0) if replies else (0, "ok\n", "", False)

    monkeypatch.setattr(kubectl, "runner", fake)
    fake.seen, fake.replies = seen, replies
    return fake


# ---------- phân tích lệnh ----------

@pytest.mark.parametrize("command, profile", [
    ("get pods -n app -o wide", "read"), ("kubectl describe deploy/web -n app", "read"), ("logs web-0 -c app --tail=50 -p", "read"),
    ("get pods -o jsonpath={.items[*].spec.profile}", "read"), ("auth can-i list pods", "read"), ("rollout status deploy/web", "read"),
    ("get --raw /readyz", "read"), ("top pods -A", "read"),
    ("apply -f -", "write"), ("scale deploy/web --replicas=3 -n app", "write"), ("rollout restart deploy/web", "write"),
    ("patch deploy web -p '{\"spec\":{}}'", "write"), ("create rolebinding x --user=bob --role=view", "write"),
    ("delete pod web-0 -n app", "admin"), ("exec web-0 -n app -- sh -c 'cat /etc/hosts -f x --token y'", "admin"), ("drain node-1 --ignore-daemonsets", "admin"),
])
def test_parse_accepts(command, profile):
    kubectl.parse(command, profile)


@pytest.mark.parametrize("command, profile, message", [
    ("", "read", "thiếu tham số command"),
    ("-n app get pods", "read", "flag đặt phía sau"),
    ("proxy --port=8001", "admin", "không được hỗ trợ"),
    ("config view --raw", "admin", "không được hỗ trợ"),
    ("cp web-0:/etc/passwd ./x", "admin", "không được hỗ trợ"),
    ("delete pod web-0", "write", "hãy dùng công cụ kubectl_admin"),
    ("scale deploy/web --replicas=1", "read", "hãy dùng công cụ kubectl_write"),
    ("rollout restart deploy/web", "read", "kubectl_write"),
    ("rollout -n app restart deploy/web", "admin", "cần hành động ngay sau"),
    ("get pods --kubeconfig=/data/x", "read", "--kubeconfig"),
    ("get pods --server=https://evil.test", "read", "--server"),
    ("get pods --token abc", "read", "--token"),
    ("get pods --as=system:admin", "admin", "--as"),
    ("get pods -s https://evil.test", "read", "-s"),
    ("get pods -v=9", "read", "-v"),
    ("get pods --watch", "read", "--watch"),
    ("get pods -w", "read", "-w"),
    ("logs web-0 -f", "read", "theo dõi log"),
    ("apply -f /data/kagent.db", "write", "chỉ được -f -"),
    ("apply --filename=/etc/passwd", "write", "--filename=-"),
    ("apply -k ./dir", "write", "-k"),
    ("create configmap x --from-file=/data/secret.key", "write", "--from-file"),
    ("get pods -o go-template-file=/data/x", "read", "từ file"),
    ("get pods --output jsonpath-file=/data/x", "read", "từ file"),
    ("exec -it web-0 -- sh", "admin", "-it"),
    ("exec web-0 --stdin -- sh", "admin", "--stdin"),
    ("get pods -nfoo", "read", "viết tách"),
    ("cluster-info dump", "read", "dump"),
    ('get pods "chưa đóng', "read", "không hợp lệ"),
])
def test_parse_rejects(command, profile, message):
    with pytest.raises(ConnectorError, match=message):
        kubectl.parse(command, profile)


# ---------- chạy kubectl ----------

def test_kubeconfig_pins_checked_ip_and_keeps_tls_name():
    cfg = yaml.safe_load(kubectl.kubeconfig({**ADMIN_P, "api_url": "https://k8s.test:6443/k8s/clusters/c-1", "ca_cert": "PEM"}, "sa-tok"))
    cluster = cfg["clusters"][0]["cluster"]
    assert cluster["server"] == f"https://{fakes.PUBLIC_IP}:6443/k8s/clusters/c-1" and cluster["tls-server-name"] == "k8s.test"
    assert cluster["certificate-authority-data"] and cfg["users"][0]["user"] == {"token": "sa-tok"}
    assert cfg["contexts"][0]["context"]["namespace"] == "default"
    insecure = yaml.safe_load(kubectl.kubeconfig({**ADMIN_P, "verify_tls": False}, "t"))["clusters"][0]["cluster"]
    assert insecure["insecure-skip-tls-verify"] is True and "tls-server-name" not in insecure


def test_kubeconfig_refuses_internal_address_without_permission():
    with pytest.raises(Exception, match="mạng nội bộ"):
        kubectl.kubeconfig({**ADMIN_P, "api_url": "https://internal.test:6443"}, "t")


def test_run_uses_profile_token_and_clean_env(calls, monkeypatch):
    monkeypatch.setenv("KAGENT_SECRET_KEY", "super-secret")
    out = kubectl.run_read({**ADMIN_P, "_sa_read": "read-tok"}, {"command": "kubectl get pods -n default", "timeout_seconds": 500})
    call = calls.seen[0]
    assert call["argv"][1:] == ["--request-timeout=120s", call["argv"][2], "get", "pods", "-n", "default"] and call["timeout"] == 120
    assert call["argv"][2].startswith("--cache-dir=")
    assert call["config"]["users"][0]["user"]["token"] == "read-tok"
    assert "super-secret" not in str(call["env"]) and set(call["env"]) == {"PATH", "HOME", "KUBECONFIG", "LANG"}
    assert out["stdout"] == "ok\n" and out["command"] == "kubectl get pods -n default"


def test_run_needs_applied_profile(calls):
    with pytest.raises(ConnectorError, match="chưa được áp dụng"):
        kubectl.run_write({**ADMIN_P, "_sa_read": "read-tok"}, {"command": "scale deploy/web --replicas=2"})
    assert not calls.seen


def test_run_errors_and_lenient_exit_codes(calls):
    p = {**ADMIN_P, "_sa_read": "r"}
    calls.replies.append((1, "", 'Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:x" cannot list', False))
    with pytest.raises(ConnectorError, match="Forbidden"):
        kubectl.run_read(p, {"command": "get pods -n kube-system"})
    calls.replies.append((1, "no\n", "", False))
    assert kubectl.run_read(p, {"command": "auth can-i delete pods"})["stdout"] == "no\n"
    calls.replies.append((-1, "partial", "", True))
    assert kubectl.run_read(p, {"command": "get pods"})["timed_out"] is True


def test_manifest_goes_to_stdin(calls):
    p = {**ADMIN_P, "_sa_write": "w"}
    kubectl.run_write(p, {"command": "apply -f -", "manifest": "kind: ConfigMap"})
    assert calls.seen[0]["stdin"] == "kind: ConfigMap"
    with pytest.raises(ConnectorError, match="thiếu tham số manifest"):
        kubectl.run_write(p, {"command": "apply -f -"})
    with pytest.raises(ConnectorError, match="phải có -f -"):
        kubectl.run_write(p, {"command": "get pods", "manifest": "x"})


def test_check_runs_server_dry_run(calls):
    p = {**ADMIN_P, "_sa_write": "w", "_sa_admin": "a"}
    kubectl.check_write(p, {"command": "scale deploy/web --replicas=2 --dry-run=client"})
    assert calls.seen[-1]["argv"][-3:] == ["deploy/web", "--replicas=2", "--dry-run=server"]
    kubectl.check_admin(p, {"command": "run dbg --image=busybox --restart=Never -- sleep 10"})
    assert calls.seen[-1]["argv"][-4:] == ["--dry-run=server", "--", "sleep", "10"]
    assert kubectl.check_write(p, {"command": "get pods"})["read_only"] is True
    assert kubectl.check_write(p, {"command": "diff -f -", "manifest": "x"})["read_only"] is True
    with pytest.raises(ConnectorError, match="không có chế độ chạy thử"):
        kubectl.check_admin(p, {"command": "exec web-0 -- env"})


# ---------- cấu hình nhóm quyền ----------

@pytest.mark.parametrize("cfg, message", [
    (config(read=[rule(verbs=["get", "delete"])]), "delete thuộc nhóm cao hơn"),
    (config(write=[rule(verbs=["delete"])]), "delete thuộc nhóm cao hơn"),
    (config(read=[rule(resources=["secrets"])]), "secrets lộ bí mật"),
    (config(write=[rule(resources=["pods/exec"], verbs=["create"])]), "pods/exec"),
    (config(write=[rule(resources=["pods/eviction"], verbs=["create"])]), "pods/eviction"),
    (config(read=[rule(ns="Bad_NS")]), "namespaces không hợp lệ"),
    (config(read=[{**rule(), "namespaces": ["*", "app"]}]), "namespace \\*"),
    (config(read=[{**rule(), "resources": []}]), "ít nhất một"),
])
def test_config_limits(cfg, message):
    with pytest.raises(ConnectorError, match=message):
        k8s_rbac.check_config(cfg)


def test_config_warnings_and_admin_freedom():
    _, warnings = k8s_rbac.check_config(config(read=[rule(resources=["nodes"])], write=[rule(resources=["deployments"], verbs=["patch"], groups=["apps"])],
                                               admin=[rule(ns="*", resources=["*"], verbs=["*"], groups=["*"])]))
    text = " ".join(warnings)
    assert "nodes là tài nguyên cấp cụm" in text and "chạy pod với ServiceAccount" in text and "toàn quyền" in text


# ---------- tạo ServiceAccount và RBAC trong cụm ----------

def test_plan_apply_and_reapply(infra):
    cfg = config(read=[rule(), rule(ns="*", resources=["nodes"])], admin=[rule(resources=["pods"], verbs=["delete"])])
    plan = k8s_rbac.plan(ADMIN_P, CID, CNAME, cfg)
    assert not plan["errors"] and plan["profiles"] == {"read": True, "write": False, "admin": True}
    actions = {(i["kind"], i["namespace"], i["name"]): i["action"] for i in plan["items"]}
    assert actions[("Namespace", None, "kagent-system")] == "create"
    assert actions[("ClusterRole", None, f"{BASE}-read")] == "create" and actions[("Role", "default", f"{BASE}-admin")] == "create"
    assert not infra.k8s_objects  # xem trước chỉ chạy thử

    result, tokens = k8s_rbac.apply(ADMIN_P, CID, CNAME, cfg)
    assert tokens == {"read": f"sa-token-{BASE}-read-token", "write": None, "admin": f"sa-token-{BASE}-admin-token"}
    objs = infra.k8s_objects
    assert ("ServiceAccount", "kagent-system", f"{BASE}-read") in objs and ("ServiceAccount", "kagent-system", f"{BASE}-write") not in objs
    # nhóm cao hơn thừa hưởng quyền nhóm thấp hơn
    read_binding = objs[("RoleBinding", "default", f"{BASE}-read")]
    assert {s["name"] for s in read_binding["subjects"]} == {f"{BASE}-read", f"{BASE}-admin"}
    assert {s["name"] for s in objs[("RoleBinding", "default", f"{BASE}-admin")]["subjects"]} == {f"{BASE}-admin"}
    assert objs[("ClusterRole", None, f"{BASE}-read")]["rules"] == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}]
    assert all(o["metadata"]["labels"][k8s_rbac.LABEL_CONN] == CID for o in objs.values())

    again = k8s_rbac.plan(ADMIN_P, CID, CNAME, result["config"])
    assert again["changes"] == 0

    shrunk = k8s_rbac.plan(ADMIN_P, CID, CNAME, config(read=[rule(verbs=["get"])]))
    changed = {(i["kind"], i["name"]): i["action"] for i in shrunk["items"] if i["action"] != "unchanged"}
    assert changed[("Role", f"{BASE}-read")] == "update" and changed[("ServiceAccount", f"{BASE}-admin")] == "delete"
    assert changed[("ClusterRoleBinding", f"{BASE}-read")] == "delete"

    count = len(objs)
    assert k8s_rbac.remove(ADMIN_P, CID) == count and not infra.k8s_objects


def test_plan_reports_missing_namespace_and_escalation(infra):
    plan = k8s_rbac.plan(ADMIN_P, CID, CNAME, config(read=[rule(ns="ghost"), rule()]))
    assert any("namespace ghost không tồn tại" in e for e in plan["errors"])
    with pytest.raises(ConnectorError, match="không áp dụng được"):
        k8s_rbac.apply(ADMIN_P, CID, CNAME, config(read=[rule(ns="ghost")]))
    infra.k8s_namespaces.add("kagent-system")
    infra.k8s_forbid_grant = True
    plan = k8s_rbac.plan(ADMIN_P, CID, CNAME, config(read=[rule()]))
    assert any("not currently held" in e for e in plan["errors"])


def test_ping_reports_service_account_identities(infra):
    _, tokens = k8s_rbac.apply(ADMIN_P, CID, CNAME, config(read=[rule()]))
    out = k8s.ping({**ADMIN_P, "_sa_read": tokens["read"], "_sa_write": "revoked-token"})
    assert out["identities"]["read"] == f"system:serviceaccount:kagent-system:{BASE}-read"
    assert out["identities"]["write"].startswith("lỗi: HTTP 401")


# ---------- qua API và gateway ----------

def test_rbac_api_flow_and_gateway(users, calls, infra):
    alice, bob = users["alice"], users["bob"]
    c = alice.post("/api/connections", json={"template": "k8s", "name": "my_k8s", "scope": "user", "envs": ["dev"],
                                             "params": {"api_url": "https://k8s.test:6443"}, "secrets": {"token": fakes.K8S_TOKEN}}).json()
    url = f"/api/connections/{c['id']}/k8s-rbac"
    info = alice.get(url).json()
    assert not info["applied"] and info["config"]["profiles"]["read"][0]["namespaces"] == ["default"]
    assert bob.get(url).status_code == 404

    with db.session() as conn:
        ctx = gateway.CallContext(alice.tenant, conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"], "dev", "xem pod")
        before = gateway.invoke(conn, ctx, "my_k8s.kubectl_read", {"command": "get pods"})
    assert before["status"] == "error" and "chưa được áp dụng" in before["result"]["error"]

    cfg = config(read=[rule()], write=[rule(resources=["deployments"], verbs=["patch"], groups=["apps"])])
    plan = alice.post(f"{url}/plan", json={"config": cfg}).json()
    assert plan["changes"] > 0 and not infra.k8s_objects
    applied = alice.post(f"{url}/apply", json={"config": cfg})
    assert applied.status_code == 200, applied.text
    info = alice.get(url).json()
    assert info["applied"] and info["identities"] == {"read": True, "write": True, "admin": False}
    listed = next(x for x in alice.get("/api/connections").json() if x["id"] == c["id"])
    assert "_rbac" not in listed["params"] and "sa-token" not in str(listed)

    # sửa kết nối không làm mất token và cấu hình đã áp dụng
    assert alice.patch(f"/api/connections/{c['id']}", json={"title": "Cụm dev", "params": {"namespace": "default"}}).status_code == 200
    with db.session() as conn:
        row = connections.get(conn, alice.tenant, c["id"])
        assert connections.params_of(row)["_sa_read"] == f"sa-token-{k8s_rbac.base_name(c['id'], 'my_k8s')}-read-token"
        step = gateway.invoke(conn, ctx, "my_k8s.kubectl_read", {"command": "get pods -n default"})
    assert step["status"] == "ok" and step["risk_tier"] == "R1" and step["untrusted"] is False  # bảng pod: dữ liệu có cấu trúc
    assert calls.seen[-1]["config"]["users"][0]["user"]["token"].endswith("-read-token")

    assert alice._call("DELETE", f"/api/connections/{c['id']}").json() == {"ok": True}
    assert not infra.k8s_objects


def test_rbac_api_rejects_bad_config_and_non_k8s(users):
    admin = users["admin"]
    conns = admin.get("/api/connections").json()
    k8s_conn = next(x for x in conns if x["template"] == "k8s")
    other = next(x for x in conns if x["template"] != "k8s")
    assert admin.get(f"/api/connections/{other['id']}/k8s-rbac").status_code == 400
    r = admin.post(f"/api/connections/{k8s_conn['id']}/k8s-rbac/plan", json={"config": config(read=[rule(resources=["secrets"])])})
    assert r.status_code == 400 and "secrets" in r.json()["detail"]
    alice = users["alice"]
    assert alice.post(f"/api/connections/{k8s_conn['id']}/k8s-rbac/apply", json={"config": config(read=[rule()])}).status_code == 403


def test_error_text_drops_kubectl_internal_logs():
    noisy = ("E1112 06:09:51.887074      19 memcache.go:265] couldn't get current server API group list: the server has asked for credentials\n"
             "error: You must be logged in to the server (Unauthorized)\n")
    assert kubectl._error_text(noisy) == "error: You must be logged in to the server (Unauthorized)"
    only_klog = "E1112 06:09:51.887074      19 memcache.go:265] couldn't get current server API group list\n"
    assert kubectl._error_text(only_klog) == "couldn't get current server API group list"


def test_wildcard_expands_without_sensitive_resources(infra):
    cfg = config(read=[rule(resources=["*"], groups=["*"], verbs=["get", "list", "watch"])],
                 write=[rule(ns="*", resources=["*"], groups=["*"], verbs=["create", "update", "patch"])],
                 admin=[rule(resources=["*"], groups=["*"], verbs=["*"])])
    plan = k8s_rbac.plan(ADMIN_P, CID, CNAME, cfg)
    assert not plan["errors"] and any("metrics.k8s.io/v1beta1" in w for w in plan["warnings"])
    assert any("resource * được đổi thành mọi resource" in w for w in plan["warnings"])
    k8s_rbac.apply(ADMIN_P, CID, CNAME, cfg)
    objs = infra.k8s_objects
    read = {g: r["resources"] for r in objs[("Role", "default", f"{BASE}-read")]["rules"] for g in r["apiGroups"]}
    assert read == {"": ["configmaps", "pods", "pods/log"], "apps": ["deployments", "deployments/scale"]}  # không có nodes vì Role trong namespace
    write = {g: r["resources"] for r in objs[("ClusterRole", None, f"{BASE}-write")]["rules"] for g in r["apiGroups"]}
    assert write == {"": ["bindings", "configmaps", "nodes", "pods"], "apps": ["deployments", "deployments/scale"]}
    assert objs[("Role", "default", f"{BASE}-admin")]["rules"] == [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}]
    assert k8s_rbac.plan(ADMIN_P, CID, CNAME, cfg)["changes"] == 0
    # CRD mới cài vào cụm thì lần xem trước sau báo lệch
    infra.K8S_DISCOVERY = {**infra.K8S_DISCOVERY, "/apis/apps/v1": {"resources": [
        *infra.K8S_DISCOVERY["/apis/apps/v1"]["resources"], {"name": "statefulsets", "namespaced": True, "verbs": fakes.Infra.RW}]}}
    changed = [i for i in k8s_rbac.plan(ADMIN_P, CID, CNAME, cfg)["items"] if i["action"] == "update"]
    assert {i["name"] for i in changed} == {f"{BASE}-read", f"{BASE}-write"}


def test_subresource_wildcard():
    rules = k8s_rbac._k8s_rules({"api_groups": [""], "resources": ["pods/*"], "verbs": ["get"]}, "read", False,
                                {"": fakes.Infra.K8S_DISCOVERY["/api/v1"]["resources"]})
    assert rules == [{"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]


@pytest.mark.parametrize("command, trusted", [
    ("get pods -n app", True), ("get deploy,svc -o wide", True), ("get pods -o name", True), ("get deployment/web", True), ("top pods", True),
    ("auth can-i list pods", True), ("rollout status deploy/web", True), ("scale deploy/web --replicas=2", True), ("delete pod web-0", True),
    ("logs web-0", False), ("describe pod web-0", False), ("get pods -o yaml", False), ("get pods -o jsonpath={.items}", False),
    ("get events", False), ("get configmaps", False), ("get widgets.example.com", False), ("get --raw /metrics", False),
    ("rollout history deploy/web", False), ("explain pods", False), ("exec web-0 -- env", False), ("apply -f - -o yaml", False),
    ("get pods --kubeconfig=/x", False),
])
def test_trusted_output_only_for_structured_results(command, trusted):
    assert kubectl.trusted_output({"command": command}) is trusted
