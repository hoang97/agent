"""Công cụ helm_* (kèm plugin helm-diff, helm-drift) và kế hoạch hoàn tác helm_undo, chạy với helm giả."""
import json

import pytest
import yaml

import fakes
from aiops import net
from aiops.connectors import helm, helm_undo
from aiops.connectors.base import ConnectorError

P = {"_allow_private": False, "api_url": "https://k8s.test:6443", "token": fakes.K8S_TOKEN, "namespace": "default", "_sa_admin": "admin-tok"}


@pytest.fixture()
def calls(monkeypatch):
    """Thay subprocess: ghi lại argv, env, kubeconfig và file values tạm của từng lần gọi."""
    seen, replies = [], []

    def fake(argv, env, stdin, timeout):
        with open(env["KUBECONFIG"], encoding="utf-8") as f:
            call = {"argv": argv, "env": env, "timeout": timeout, "config": yaml.safe_load(f)}
        vals = [a for a in argv if a.endswith("values.yaml")]
        if vals:
            with open(vals[0].split("=", 1)[-1], encoding="utf-8") as f:
                call["values"] = f.read()
        seen.append(call)
        reply = replies.pop(0) if replies else (0, "ok\n", "", False)
        return reply(argv) if callable(reply) else reply

    monkeypatch.setattr(helm, "runner", fake)
    fake.seen, fake.replies = seen, replies
    return fake


# ---------- phân tích lệnh ----------

@pytest.mark.parametrize("command, profile", [
    ("list -A", "read"), ("helm ls -n app -o json", "read"), ("status web -n app", "read"), ("history web --max 5", "read"),
    ("show values web --repo https://example.test/charts", "read"), ("show chart oci://example.test/charts/web --version 1.2.3", "read"),
    ("get notes web", "read"), ("diff upgrade web oci://example.test/charts/web -n app -f -", "read"), ("diff rollback web 3", "read"),
    ("diff revision web 2 3", "read"), ("drift run web --from-release -n app", "read"), ("drift all -n app", "read"),
    ("template web https://example.test/web-1.0.0.tgz --set image.tag=v2 --kube-version 1.29", "read"), ("version --template={{.Version}}", "read"),
    ("upgrade --install web oci://example.test/charts/web -n app -f - --wait", "write"), ("rollback web 3 -n app", "write"),
    ("install web web --repo https://example.test/charts", "write"),
    ("uninstall web -n app", "admin"), ("get values web", "admin"), ("get manifest web --revision 2", "admin"),
    ("diff upgrade web oci://example.test/charts/web --show-secrets", "admin"),
])
def test_parse_accepts(command, profile):
    helm.parse(command, profile)


@pytest.mark.parametrize("command, profile, message", [
    ("", "read", "thiếu tham số command"),
    ("repo add x https://example.test", "admin", "không được hỗ trợ"),
    ("plugin install https://example.test/x", "admin", "không được hỗ trợ"),
    ("registry login example.test", "admin", "không được hỗ trợ"),
    ("upgrade web ./chart", "write", "không phải tên hợp lệ"),
    ("upgrade web /data/chart.tgz", "write", "không phải tên hợp lệ"),
    ("upgrade web bitnami/nginx", "write", "không phải tên hợp lệ"),
    ("upgrade web file:///etc/passwd", "write", "không hỗ trợ chart"),
    ("upgrade web oci://example.test/web -f /data/values.yaml", "write", "chỉ được -f -"),
    ("upgrade web oci://example.test/web --set-file x=/data/aiops.db", "write", "--set-file"),
    ("upgrade web oci://example.test/web --post-renderer /bin/sh", "write", "--post-renderer"),
    ("list --kubeconfig /data/x", "read", "--kubeconfig"),
    ("list --kube-token abc", "read", "--kube-token"),
    ("list --kube-apiserver=https://evil.test", "read", "--kube-apiserver"),
    ("list --kube-as-user admin", "read", "--kube-as-user"),
    ("drift run web --custom-diff 'sh -c id'", "read", "--custom-diff"),
    ("drift run web --temp-path /data", "read", "--temp-path"),
    ("show values web --repo https://example.test --ca-file /data/ca", "read", "--ca-file"),
    ("upgrade web oci://example.test/web --dry-run", "write", "--dry-run"),
    ("get values web", "read", "hãy dùng công cụ helm_admin"),
    ("diff upgrade web oci://example.test/web --show-secrets", "read", "helm_admin"),
    ("upgrade web oci://example.test/web", "read", "hãy dùng công cụ helm_write"),
    ("uninstall web", "write", "hãy dùng công cụ helm_admin"),
    ("get web", "read", "cần hành động ngay sau"),
    ("list -x", "read", "không được dùng"),
    ("list | sh", "read", "không chạy được sh"),
    ("upgrade web oci://example.test/web | grep x", "write", "chỉ lệnh đọc"),
    ("list ; rm -rf /", "read", "toán tử shell"),
])
def test_parse_rejects(command, profile, message):
    with pytest.raises(ConnectorError, match=message.replace("|", r"\|")):
        helm.parse(command, profile)


def test_chart_urls_must_pass_network_check():
    with pytest.raises(net.NetError):
        helm.parse("show values web --repo https://internal.test/charts", "read")
    with pytest.raises(net.NetError):
        helm.parse("show chart oci://internal.test/charts/web", "read")
    helm.parse("show chart oci://internal.test/charts/web", "read", allow_private=True)


# ---------- chạy ----------

def test_run_uses_admin_token_clean_env_and_temp_helm_home(calls, monkeypatch):
    monkeypatch.setenv("AIOPS_SECRET_KEY", "super-secret")
    out = helm.run_read(P, {"command": "helm list -n app"})
    call = calls.seen[0]
    assert call["argv"][1:] == ["list", "-n", "app"] and call["timeout"] == 60
    assert call["config"]["users"][0]["user"]["token"] == "admin-tok"
    assert "super-secret" not in str(call["env"]) and call["env"]["HELM_NAMESPACE"] == "app"
    home = call["env"]["HOME"]
    assert all(call["env"][k].startswith(home) for k in ("HELM_CACHE_HOME", "HELM_CONFIG_HOME", "HELM_DATA_HOME", "KUBECONFIG"))
    assert out["command"] == "helm list -n app"


def test_run_needs_admin_profile(calls):
    with pytest.raises(ConnectorError, match="quản trị chưa được áp dụng"):
        helm.run_read({**P, "_sa_admin": None, "_sa_read": "r"}, {"command": "list"})
    assert not calls.seen


def test_values_go_to_temp_file(calls):
    helm.run_write(P, {"command": "upgrade web oci://example.test/charts/web -n app -f -", "values": "image:\n  tag: v2\n"})
    call = calls.seen[0]
    i = call["argv"].index("-f")
    assert call["argv"][i + 1].endswith("values.yaml") and call["values"] == "image:\n  tag: v2\n"
    assert call["argv"][-1] == "--timeout=300s" and call["timeout"] == 300
    with pytest.raises(ConnectorError, match="thiếu tham số values"):
        helm.run_write(P, {"command": "upgrade web oci://example.test/charts/web -f -"})
    with pytest.raises(ConnectorError, match="phải có -f -"):
        helm.run_write(P, {"command": "upgrade web oci://example.test/charts/web", "values": "a: 1"})


def test_user_timeout_flag_is_kept(calls):
    helm.run_write(P, {"command": "rollback web 3 --timeout 10m", "timeout_seconds": 600})
    assert calls.seen[0]["argv"][1:] == ["rollback", "web", "3", "--timeout", "10m"]


def test_drift_reports_drift_as_result_not_error(calls):
    helm.run_read(P, {"command": "drift run web --from-release -n app"})
    assert calls.seen[0]["argv"][1:] == ["drift", "run", "web", "--from-release", "-n", "app", "--disable-error-on-drift", "--no-color"]


def test_diff_detailed_exitcode_2_is_not_error(calls):
    calls.replies.append((2, "changed\n", "", False))
    out = helm.run_read(P, {"command": "diff upgrade web oci://example.test/web --detailed-exitcode"})
    assert out["exit_code"] == 2
    calls.replies.append((1, "", "Error: release: not found", False))
    with pytest.raises(ConnectorError, match="release: not found"):
        helm.run_read(P, {"command": "status web"})


def test_missing_plugin_is_reported(calls, monkeypatch, tmp_path):
    monkeypatch.setattr(helm, "runner", None)
    monkeypatch.setattr(helm, "PLUGINS", str(tmp_path))
    with pytest.raises(ConnectorError, match="chưa cài plugin helm-drift"):
        helm.run_read(P, {"command": "drift all"})
    with pytest.raises(ConnectorError, match="chưa cài plugin helm-diff"):
        helm.run_read(P, {"command": "diff rollback web 2"})


def test_read_output_filters(calls):
    calls.replies.append((0, "NAME\tNAMESPACE\nweb\tapp\napi\tapp\n", "", False))
    out = helm.run_read(P, {"command": "list -A | grep web"})
    assert out["stdout"] == "web\tapp\n" and calls.seen[0]["argv"][1:] == ["list", "-A"]


def test_chart_proxy_skips_api_server(calls, monkeypatch):
    from aiops import proxy
    monkeypatch.setattr(proxy, "routes", lambda url, mode=None: ["http://proxy.test:3128"] if "example.test" in url else ["DIRECT"])
    helm.run_read(P, {"command": "show chart oci://example.test/charts/web"})
    env = calls.seen[0]["env"]
    assert env["HTTPS_PROXY"] == "http://proxy.test:3128" and "k8s.test" in env["NO_PROXY"]
    helm.run_read(P, {"command": "list"})
    assert "HTTPS_PROXY" not in calls.seen[1]["env"]


# ---------- chạy thử ----------

def test_check_write_uses_helm_diff(calls):
    out = helm.check_write(P, {"command": "upgrade --install web oci://example.test/charts/web -n app -f - --wait --atomic --timeout 5m --create-namespace",
                               "values": "a: 1"})
    argv = calls.seen[0]["argv"][1:]
    assert argv[:5] == ["diff", "upgrade", "web", "oci://example.test/charts/web", "--allow-unreleased"]
    assert "--wait" not in argv and "--atomic" not in argv and "5m" not in argv and "--create-namespace" not in argv and "--install" not in argv
    assert "-n" in argv and "--no-color" in argv and calls.seen[0]["values"] == "a: 1"
    assert out["diff"] == "ok\n"
    helm.check_write(P, {"command": "rollback web 3 -n app"})
    assert calls.seen[1]["argv"][1:5] == ["diff", "rollback", "web", "3"]


def test_check_without_diff_plugin_hides_manifest(calls, monkeypatch):
    monkeypatch.setattr(helm, "_diff_installed", lambda: False)
    calls.replies.append((0, "kind: Secret\ndata:\n  password: c2VjcmV0\n", "", False))
    out = helm.check_write(P, {"command": "upgrade web oci://example.test/charts/web"})
    assert calls.seen[0]["argv"][-1] == "--dry-run=server" and "c2VjcmV0" not in json.dumps(out) and "helm-diff" in out["note"]


def test_trusted_output_only_list_and_version():
    assert helm.trusted_output({"command": "list -A"}) and helm.trusted_output({"command": "version"})
    assert not helm.trusted_output({"command": "status web"}) and not helm.trusted_output({"command": "get values web"})
    assert not helm.trusted_output({"command": "show chart oci://internal.test/x"})


# ---------- hoàn tác ----------

def history(revision, status="deployed", chart="web-1.0.0", app="1.0"):
    return json.dumps([{"revision": revision, "status": status, "chart": chart, "app_version": app, "description": "Upgrade complete"}])


def test_upgrade_plan_rolls_back_to_current_revision(calls):
    calls.replies.append((0, history(4), "", False))
    plan = helm_undo.plan_undo(P, "helm_write", {"command": "upgrade web oci://example.test/charts/web -n app"})
    assert calls.seen[0]["argv"][1:] == ["history", "web", "-n", "app", "--max", "1", "-o", "json"]
    assert plan["reversible"] and plan["recipe"] == "helm_upgrade" and plan["targets"][0]["key"] == "helm/app/web"
    assert plan["undo"] == [{"op": "helm", "argv": ["rollback", "web", "4", "-n", "app"]}] and plan["namespaces"] == ["app"]
    assert "preflight" not in plan
    calls.replies.append((0, history(5, chart="web-1.1.0"), "", False))
    plan = helm_undo.after_change(P, plan, {})
    assert plan["after"] == {"revision": 5}
    calls.replies.append((0, history(5, chart="web-1.1.0"), "", False))
    assert helm_undo.drift(P, plan) == []
    calls.replies.append((0, history(6, chart="web-1.2.0"), "", False))
    assert "revision 6" in helm_undo.drift(P, plan)[0]
    calls.replies += [(0, "Rollback was a success!\n", "", False), (0, history(7), "", False)]
    assert helm_undo.run_op(P, plan["undo"][0])["command"] == "helm rollback web 4 -n app"
    assert helm_undo.same_as_before(P, plan)


def test_install_plan_uninstalls_new_release(calls):
    calls.replies.append((1, "", "Error: release: not found", False))
    plan = helm_undo.plan_undo(P, "helm_write", {"command": "upgrade --install web oci://example.test/charts/web -n app"})
    assert plan["recipe"] == "helm_install" and plan["undo"][0]["argv"] == ["uninstall", "web", "-n", "app"]
    calls.replies.append((1, "", "Error: release: not found", False))
    assert helm_undo.same_as_before(P, plan)
    calls.replies.append((1, "", "Error: release: not found", False))
    with pytest.raises(ConnectorError, match="không có trong namespace"):
        helm_undo.plan_undo(P, "helm_write", {"command": "upgrade web oci://example.test/charts/web -n app"})


def test_not_deployed_release_is_flagged_and_checked(calls):
    calls.replies.append((0, history(3, status="pending-upgrade"), "", False))
    plan = helm_undo.plan_undo(P, "helm_write", {"command": "rollback web 2"})
    assert "pending-upgrade" in plan["preflight"][0]
    calls.replies.append((0, history(4, status="failed"), "", False))
    assert helm_undo.check_change(P, plan)["ready"] is False


@pytest.mark.parametrize("tool, command, why", [
    ("helm_admin", "uninstall web", "lịch sử revision"),
    ("helm_admin", "test web", "chạy pod"),
    ("helm_write", "install oci://example.test/charts/web --generate-name", "tên release"),
    ("helm_read", "list", "chỉ đọc"),
])
def test_not_reversible(calls, tool, command, why):
    plan = helm_undo.plan_undo(P, tool, {"command": command})
    assert plan["reversible"] is False and why in plan["why"] and not calls.seen


def test_read_command_through_write_tool_is_noop(calls):
    plan = helm_undo.plan_undo(P, "helm_write", {"command": "status web"})
    assert plan["noop"] and not calls.seen
