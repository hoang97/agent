"""Chạy helm thật (kèm plugin helm-diff, helm-drift) trên cụm của kết nối Kubernetes.

Helm lưu release trong Secret của namespace nên mọi lệnh helm, kể cả list/status/diff, cần token của nhóm quản trị (k8s_rbac).
Vì vậy RBAC không phân biệt được đọc/ghi như với kubectl; aiops tự phân lệnh theo nội dung:
- helm_read: list, status, history, show, template, get notes/metadata, diff (không --show-secrets), drift. Không lộ nội dung Secret.
- helm_write: install, upgrade, rollback. Chạy thử bằng helm diff (che giá trị Secret), hoàn tác bằng helm rollback.
- helm_admin: uninstall, test, get values/manifest/hooks/all, diff --show-secrets.
Không đọc file trên máy chủ aiops: chart lấy từ --repo URL, oci:// hoặc URL .tgz; values truyền qua tham số values với -f -.
Không có helm repo add, registry login, plugin install: mỗi lần chạy là HOME tạm, không giữ cấu hình.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile

import httpx
import yaml

from .. import net
from . import k8s, kubectl
from .base import ConnectorError, clip, integer

HELM = os.environ.get("AIOPS_HELM") or "helm"
PLUGINS = os.environ.get("AIOPS_HELM_PLUGINS") or "/opt/helm/plugins"
# Test thay bằng callable(argv, env, stdin, timeout) -> (mã thoát, stdout, stderr, hết giờ).
runner = None

TOOL = {"read": "helm_read", "write": "helm_write", "admin": "helm_admin"}
PROFILES = ("read", "write", "admin")
ALIASES = {"ls": "list", "hist": "history", "un": "uninstall", "del": "uninstall", "delete": "uninstall", "uninstal": "uninstall",
           "inspect": "show"}
SUBS = {"list": "read", "status": "read", "history": "read", "show": "read", "template": "read", "version": "read",
        "get": None, "diff": None, "drift": "read",
        "install": "write", "upgrade": "write", "rollback": "write", "uninstall": "admin", "test": "admin"}
ACTIONS = {"show": {"read": {"chart", "values", "readme", "crds", "all"}},
           "get": {"read": {"notes", "metadata"}, "admin": {"values", "manifest", "hooks", "all"}},
           "diff": {"read": {"upgrade", "rollback", "revision", "release", "version"}},
           "drift": {"read": {"run", "all", "version"}}}
WRITES = {"install", "upgrade", "rollback", "uninstall", "test"}
# Đọc file hoặc chạy chương trình trên máy chủ aiops, đổi danh tính/đích đến, hoặc ghi cấu hình ngoài thư mục tạm.
BLOCKED = {"kubeconfig", "kube-context", "kube-token", "kube-as-user", "kube-as-group", "kube-apiserver", "kube-ca-file",
           "kube-insecure-skip-tls-verify", "kube-tls-server-name", "registry-config", "repository-config", "repository-cache",
           "post-renderer", "post-renderer-args", "set-file", "ca-file", "cert-file", "key-file", "keyring", "verify", "username", "password",
           "pass-credentials", "output-dir", "temp-path", "custom-diff", "skip-cleaning", "report", "plugins", "dry-run", "post-render"}
# Flag nhận giá trị (dạng --x y); còn lại coi là flag bật/tắt. Chỉ để tìm đúng tham số vị trí (tên release, chart).
VALUED = {"namespace", "output", "values", "set", "set-string", "set-json", "set-literal", "repo", "version", "timeout", "description",
          "name-template", "labels", "max", "filter", "selector", "revision", "kind", "name", "skip", "ignore-hooks", "concurrency",
          "log-level", "regex", "suppress", "suppress-output-line-regex", "find-renames", "api-versions", "kube-version", "skip-release",
          "history-max", "context", "output-format", "time-format", "show-only", "release-name", "wait-for", "burst-limit", "qps", "template"}
SHORT = {"n": "namespace", "o": "output", "f": "values", "l": "selector", "a": "all", "A": "all-namespaces", "q": "short", "d": "disable-error-on-drift",
         "C": "context", "s": "show-only"}
# helm diff không có các flag chỉ dành cho lệnh ghi này; bỏ đi khi đổi install/upgrade thành diff để chạy thử.
DIFF_DROP = {"wait", "wait-for-jobs", "atomic", "rollback-on-failure", "timeout", "create-namespace", "description", "history-max", "cleanup-on-fail",
             "force", "force-replace", "force-conflicts", "dependency-update", "generate-name", "name-template", "install", "render-subchart-notes",
             "take-ownership", "labels", "skip-schema-validation", "replace", "debug", "hide-notes", "enable-dns", "server-side", "devel"}
NAME = re.compile(r"^[A-Za-z0-9][-A-Za-z0-9_.+]{0,252}$")
MAX_COMMAND, MAX_VALUES = 4000, 500_000


def _fail(message: str):
    raise ConnectorError(message)


def _flag(a: str) -> tuple[str | None, str | None]:
    """(tên flag dạng dài, giá trị viết liền) của một token; (None, None) nếu không phải flag."""
    if a.startswith("--") and len(a) > 2:
        name, eq, value = a[2:].partition("=")
        return name, (value if eq else None)
    if a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
        letter, rest = a[1], a[2:]
        if letter not in SHORT or (rest and not rest.startswith("=")):
            _fail(f"flag {a} không được dùng; viết tách flag ngắn (ví dụ -n default), flag khác dùng dạng dài --tên")
        return SHORT[letter], (rest[1:] if rest else None)
    return None, None


def _check_chart(ref: str, allow_private: bool | None) -> None:
    if "://" in ref:
        if allow_private is None:
            return
        scheme = ref.split("://", 1)[0].lower()
        if scheme == "oci":
            u = httpx.URL("https://" + ref.split("://", 1)[1])
            net.check_host(u.host, u.port or 443, allow_private)
            return
        if scheme in ("http", "https"):
            net.check_url(ref, allow_private)
            return
        _fail(f"không hỗ trợ chart {ref}; dùng oci://, https://.../chart.tgz hoặc --repo URL kèm tên chart")
    if not NAME.match(ref) or ".." in ref:
        _fail(f"'{ref}' không phải tên hợp lệ; aiops không đọc chart trên máy chủ và không có helm repo add, "
              "chart phải là oci://..., https://.../chart.tgz hoặc tên chart kèm --repo https://...")


def parse(command, profile: str, allow_private: bool | None = False) -> tuple[list[str], dict]:
    if not isinstance(command, str) or not command.strip():
        _fail("thiếu tham số command, ví dụ: list -n app")
    if len(command) > MAX_COMMAND:
        _fail(f"lệnh dài quá {MAX_COMMAND} ký tự")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _fail(f"lệnh không hợp lệ: {exc}")
    if argv and argv[0] == "helm":
        argv = argv[1:]
    argv, stages = kubectl._split_pipeline(argv)
    filters = [kubectl._filter(stage) for stage in stages]
    if not argv or argv[0].startswith("-"):
        _fail("lệnh phải bắt đầu bằng lệnh con của helm, flag đặt phía sau, ví dụ: status web -n app")
    sub = ALIASES.get(argv[0], argv[0])
    argv = [sub, *argv[1:]]
    if sub not in SUBS:
        _fail(f"helm {sub} không được hỗ trợ (chỉ hỗ trợ: {', '.join(sorted(SUBS))})")
    action, need = None, SUBS[sub]
    if sub in ACTIONS:
        known = set().union(*ACTIONS[sub].values())
        action = argv[1] if len(argv) > 1 else None
        if action not in known:
            _fail(f"helm {sub} cần hành động ngay sau, một trong: {', '.join(sorted(known))}")
        need = next(p for p in PROFILES if action in ACTIONS[sub].get(p, set()))
    stdin, positionals, flags = False, [], set()
    i = 2 if action else 1
    while i < len(argv):
        a = argv[i]
        name, value = _flag(a)
        if name is None:
            positionals.append(a)
            i += 1
            continue
        if name in BLOCKED:
            _fail(f"flag --{name} không được dùng qua aiops")
        if name.startswith("kube-") and name != "kube-version":
            _fail(f"flag --{name} không được dùng qua aiops; aiops tự cấp kết nối tới cụm")
        if name in VALUED and value is None:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        flags.add(name)
        if name == "values":
            if value != "-":
                _fail("chỉ được -f - ; truyền nội dung values qua tham số values")
            stdin = True
        if name == "repo" and allow_private is not None:
            net.check_url(value, allow_private)
        if name == "show-secrets" and need == "read":
            need = "admin"
        i += 1
    for ref in positionals:
        _check_chart(ref, allow_private)
    if PROFILES.index(need) > PROFILES.index(profile):
        _fail(f"helm {sub}{' ' + action if action else ''}{' --show-secrets' if 'show-secrets' in flags else ''} thuộc nhóm "
              f"{ {'read': 'đọc', 'write': 'ghi', 'admin': 'quản trị'}[need]}, hãy dùng công cụ {TOOL[need]}")
    read_only = sub not in WRITES
    if filters and not read_only:
        _fail("chỉ lệnh đọc mới được lọc qua | grep/head/tail")
    return argv, {"sub": sub, "action": action, "key": f"{sub} {action}" if action else sub, "stdin": stdin, "read_only": read_only,
                  "filters": filters, "pipe": " | ".join(shlex.join(s) for s in stages), "positionals": positionals, "flags": flags}


def namespace(argv: list[str], p: dict) -> str:
    for i, a in enumerate(argv):
        if a in ("-n", "--namespace") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(("-n=", "--namespace=")):
            return a.split("=", 1)[1]
    return p.get("namespace") or "default"


def _chart_route(argv: list[str], p: dict) -> str | None:
    """Proxy cho việc tải chart (helm chỉ nhận một HTTPS_PROXY): theo URL chart hoặc --repo đầu tiên."""
    from .. import proxy
    urls = [a for a in argv if "://" in a]
    for i, a in enumerate(argv):
        if a == "--repo" and i + 1 < len(argv):
            urls.insert(0, argv[i + 1])
        elif a.startswith("--repo="):
            urls.insert(0, a.split("=", 1)[1])
    if not urls:
        return None
    url = urls[0].replace("oci://", "https://", 1)
    try:
        route = proxy.routes(url, p.get("proxy"))[0]
    except proxy.ProxyError as exc:
        raise net.NetError(str(exc)) from exc
    return None if route == "DIRECT" else route


def _exec(p: dict, argv: list[str], values: str | None, timeout: int, interruptible: bool) -> tuple[int, str, str, bool]:
    exe = shutil.which(HELM)
    if exe is None and runner is None:
        _fail("máy chủ aiops chưa cài helm")
    c = k8s._conn(p)
    api = httpx.URL(c["api_url"])
    config = kubectl.kubeconfig(p, kubectl._token(p, "admin"))
    route = _chart_route(argv, p)
    with tempfile.TemporaryDirectory(prefix="aiops-helm-") as d:
        path = os.path.join(d, "config")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            f.write(config)
        if values is not None:
            # helm-drift không đọc values từ stdin, nên mọi lệnh đều nhận file tạm thay cho "-".
            vpath = os.path.join(d, "values.yaml")
            with open(vpath, "w", encoding="utf-8") as f:
                f.write(values)
            argv = [vpath if a == "-" and i and argv[i - 1] in ("-f", "--values") else a for i, a in enumerate(argv)]
            argv = [f"--values={vpath}" if a == "--values=-" else f"-f={vpath}" if a == "-f=-" else a for a in argv]
        tools = os.path.dirname(shutil.which(kubectl.KUBECTL) or "/usr/local/bin/kubectl")  # helm-drift gọi kubectl diff
        env = {"PATH": f"{os.path.dirname(exe or '/usr/local/bin/helm')}:{tools}:/usr/local/bin:/usr/bin:/bin", "HOME": d, "KUBECONFIG": path,
               "LANG": "C.UTF-8", "HELM_PLUGINS": PLUGINS, "HELM_CACHE_HOME": os.path.join(d, "cache"),
               "HELM_CONFIG_HOME": os.path.join(d, "config.d"), "HELM_DATA_HOME": os.path.join(d, "data"), "HELM_NAMESPACE": namespace(argv, p)}
        if route:
            # API server đã có proxy-url hoặc IP riêng trong kubeconfig; NO_PROXY giữ nó khỏi đi qua proxy của chart.
            server = httpx.URL(yaml.safe_load(config)["clusters"][0]["cluster"]["server"])
            env.update({"HTTPS_PROXY": route, "HTTP_PROXY": route, "NO_PROXY": f"{api.host},{server.host}"})
        full = [exe or HELM, *argv]
        if runner is not None:
            return runner(full, env, None, timeout)
        return kubectl.spawn(full, env, d, None, timeout + (5 if interruptible else 30), interruptible)


def _drift_installed() -> bool:
    return runner is not None or os.path.isdir(os.path.join(PLUGINS, "drift"))


def _diff_installed() -> bool:
    return runner is not None or os.path.isdir(os.path.join(PLUGINS, "diff"))


def _prepare(argv: list[str], info: dict, timeout: int) -> list[str]:
    sub = info["sub"]
    if sub == "diff" and not _diff_installed():
        _fail("máy chủ aiops chưa cài plugin helm-diff")
    if sub == "drift" and not _drift_installed():
        _fail("máy chủ aiops chưa cài plugin helm-drift")
    if sub == "drift" and info["action"] in ("run", "all") and "disable-error-on-drift" not in info["flags"]:
        argv = [*argv, "--disable-error-on-drift"]  # có drift là kết quả, không phải lỗi
    if sub == "drift" and "no-color" not in info["flags"]:
        argv = [*argv, "--no-color"]
    if sub == "diff" and "no-color" not in info["flags"]:
        argv = [*argv, "--no-color"]
    if sub in WRITES - {"test"} and "timeout" not in info["flags"]:
        argv = [*argv, f"--timeout={timeout}s"]  # helm tự dừng chờ trước khi aiops giết tiến trình, release không kẹt pending
    return argv


def to_diff(argv: list[str], info: dict) -> list[str] | None:
    """Lệnh helm diff tương ứng với install/upgrade/rollback, để chạy thử. None nếu không có."""
    sub, pos = info["sub"], info["positionals"]
    rest, i = [], 1
    while i < len(argv):
        a = argv[i]
        name, value = _flag(a)
        if name is None:
            i += 1
            continue
        take = name in VALUED and value is None
        if name not in DIFF_DROP:
            rest.append(a)
            if take and i + 1 < len(argv):
                rest.append(argv[i + 1])
        i += 2 if take else 1
    if sub == "rollback":
        if not pos:
            return None
        return ["diff", "rollback", pos[0], *pos[1:2], *rest]
    if sub == "install" and ("generate-name" in info["flags"] or len(pos) < 2):
        return None
    if sub in ("install", "upgrade") and len(pos) >= 2:
        return ["diff", "upgrade", pos[0], pos[1], "--allow-unreleased", *rest]
    return None


def _run(p: dict, args: dict, profile: str, dry: bool = False) -> dict:
    allow_private = p.get("_allow_private", False)
    argv, info = parse(args.get("command"), profile, allow_private)
    values = args.get("values")
    if values is not None and (not isinstance(values, str) or len(values) > MAX_VALUES):
        _fail(f"values phải là chuỗi YAML tối đa {MAX_VALUES} ký tự")
    if info["stdin"] and not values:
        _fail("lệnh có -f - nhưng thiếu tham số values")
    if values and not info["stdin"]:
        _fail("có values thì lệnh phải có -f -")
    timeout = integer(args, "timeout_seconds", 60 if info["read_only"] else 300, 1, 600)
    if dry:
        if info["read_only"]:
            return {"read_only": True, "note": "lệnh chỉ đọc, không cần chạy thử"}
        return _dry(p, argv, info, values, timeout)
    argv = _prepare(argv, info, timeout)
    code, out, err, timed_out = _exec(p, argv, values, timeout, interruptible=info["read_only"])
    command = "helm " + shlex.join(argv)
    if info["filters"]:
        command += " | " + info["pipe"]
        out = kubectl._apply_filters(out, info["filters"])
    result = {"command": command, "exit_code": code, "stdout": clip(out), "stderr": clip(err, 5000)}
    if timed_out:
        result["timed_out"] = True
    elif code != 0 and not (info["sub"] == "diff" and code == 2 and "detailed-exitcode" in info["flags"]):
        _fail(f"helm lỗi (mã {code}): {clip(kubectl._error_text(err or out), 1500)}")
    if info["filters"] and not out:
        result["note"] = "không có dòng nào khớp bộ lọc"
    return result


def _dry(p: dict, argv: list[str], info: dict, values: str | None, timeout: int) -> dict:
    """Chạy thử bằng helm diff (giá trị Secret bị che). helm --dry-run in cả Secret ra nên chỉ dùng khi thiếu plugin, và bỏ stdout."""
    if info["sub"] in ("uninstall", "test"):
        _fail(f"helm {info['sub']} không có chế độ chạy thử")
    diff = to_diff(argv, info)
    if diff is not None and _diff_installed():
        diff = [*diff, "--no-color"]
        code, out, err, timed_out = _exec(p, diff, values, timeout, interruptible=True)
        if timed_out or code != 0:
            _fail(f"helm diff lỗi (mã {code}): {clip(kubectl._error_text(err or out), 1500)}")
        return {"command": "helm " + shlex.join(diff), "diff": clip(out) or "không có khác biệt"}
    dry = [a for a in argv if not a.startswith("--dry-run")] + ["--dry-run=server"]
    code, out, err, timed_out = _exec(p, dry, values, timeout, interruptible=True)
    if timed_out or code != 0:
        _fail(f"helm chạy thử lỗi (mã {code}): {clip(kubectl._error_text(err or out), 1500)}")
    return {"command": "helm " + shlex.join(dry), "note": "chạy thử phía server qua; không hiện manifest vì có thể chứa Secret"
            + ("" if _diff_installed() else ", cài plugin helm-diff để xem khác biệt")}


def run_read(p, args):
    return _run(p, args, "read")


def run_write(p, args):
    return _run(p, args, "write")


def check_write(p, args):
    return _run(p, args, "write", dry=True)


def run_admin(p, args):
    return _run(p, args, "admin")


def check_admin(p, args):
    return _run(p, args, "admin", dry=True)


def trusted_output(args: dict) -> bool:
    """helm list và version chỉ trả tên, trạng thái, phiên bản; còn lại có NOTES, mô tả, manifest do người viết chart ghi."""
    try:
        argv, info = parse(args.get("command"), "admin", allow_private=None)
    except ConnectorError:
        return False
    return info["key"] in ("list", "version")
