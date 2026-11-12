"""Chạy kubectl thật bằng token ServiceAccount của từng nhóm quyền (xem k8s_rbac).

RBAC của cụm quyết định lệnh nào được phép; ở đây chỉ chặn những gì RBAC không chặn được:
- đọc hay ghi file trên máy chủ kagent (-f đường dẫn, --from-file, -o *-file, --kubeconfig, --log-file...),
- đổi danh tính hoặc đích đến (--as, --token, --server, --context...), lộ token (config view, -v),
- phiên tương tác hoặc kéo dài (-i, -t, --watch, --follow, proxy, port-forward, attach, cp, edit).
Kubeconfig tạm trỏ thẳng vào IP đã kiểm tra bởi net (chống DNS rebinding), TLS vẫn xác thực theo tên gốc.
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import tempfile

import yaml

from .. import net
from . import k8s
from .base import ConnectorError, clip, integer
from .k8s_rbac import LABEL, PROFILES, TOKEN_KEY, TOOL

KUBECTL = os.environ.get("KAGENT_KUBECTL") or "kubectl"
# Test thay bằng callable(argv, env, stdin, timeout) -> (mã thoát, stdout, stderr, hết giờ); kubeconfig tạm còn tồn tại trong lúc gọi.
runner = None

READ = {"get", "describe", "logs", "top", "explain", "api-resources", "api-versions", "version", "events", "auth", "rollout", "cluster-info"}
WRITE = READ | {"apply", "create", "patch", "replace", "label", "annotate", "scale", "autoscale", "set", "expose", "diff", "cordon", "uncordon", "taint"}
ADMIN = WRITE | {"delete", "drain", "exec", "run", "certificate"}
ALLOWED = {"read": READ, "write": WRITE, "admin": ADMIN}
# Lệnh hai cấp: hành động phải đứng ngay sau lệnh con. Nhóm cao hơn được cả hành động của nhóm thấp hơn.
ACTIONS = {"auth": {"read": {"can-i", "whoami"}}, "rollout": {"read": {"status", "history"}, "write": {"restart", "undo", "pause", "resume"}},
           "certificate": {"admin": {"approve", "deny"}}}
DRY_RUN = {"apply", "create", "patch", "replace", "label", "annotate", "scale", "autoscale", "set", "expose", "cordon", "uncordon", "taint",
           "delete", "drain", "run", "rollout restart", "rollout undo"}
LENIENT = {"auth can-i", "diff"}  # mã thoát 1 là câu trả lời ("no", "có khác biệt"), không phải lỗi
BLOCKED = {"kubeconfig", "server", "token", "as", "as-group", "as-uid", "context", "cluster", "username", "password",
           "certificate-authority", "client-certificate", "client-key", "insecure-skip-tls-verify", "tls-server-name", "cache-dir",
           "profile", "profile-output", "log-file", "log-dir", "log-backtrace-at", "log-flush-frequency", "v", "vmodule", "kustomize",
           "from-file", "from-env-file", "env-file", "output-directory", "stdin", "tty", "attach", "follow", "watch", "watch-only",
           "disable-compression", "match-server-version"}
SHORT = set("nlcoLpAhf")  # còn lại (-i, -t, -w, -k, -s, -v, -e...) bị chặn hoặc phải viết dạng dài
MAX_COMMAND, MAX_MANIFEST = 4000, 500_000


def _fail(message: str):
    raise ConnectorError(message)


FILE_OUTPUTS = {"go-template-file", "templatefile", "jsonpath-file", "custom-columns-file"}


def _check_output(value: str) -> None:
    if value.split("=", 1)[0] in FILE_OUTPUTS:
        _fail("không đọc template từ file trên máy chủ kagent; dùng -o jsonpath=... hoặc -o go-template=... viết thẳng trong lệnh")


def _lowest(sub: str, action: str | None) -> str:
    """Nhóm thấp nhất được chạy lệnh này."""
    if action is not None and sub in ACTIONS:
        return next(p for p in PROFILES if action in ACTIONS[sub].get(p, set()))
    return next(p for p in PROFILES if sub in ALLOWED[p])


def parse(command, profile: str) -> tuple[list[str], dict]:
    if not isinstance(command, str) or not command.strip():
        _fail("thiếu tham số command, ví dụ: get pods -n app")
    if len(command) > MAX_COMMAND:
        _fail(f"lệnh dài quá {MAX_COMMAND} ký tự")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        _fail(f"lệnh không hợp lệ: {exc}")
    if argv and argv[0] == "kubectl":
        argv = argv[1:]
    if not argv or argv[0].startswith("-"):
        _fail("lệnh phải bắt đầu bằng lệnh con của kubectl, flag đặt phía sau, ví dụ: get pods -n app")
    sub = argv[0]
    if sub not in ADMIN:
        _fail(f"kubectl {sub} không được hỗ trợ (chỉ hỗ trợ: {', '.join(sorted(ADMIN))})")
    action = None
    if sub in ACTIONS:
        known = set().union(*ACTIONS[sub].values())
        action = argv[1] if len(argv) > 1 else None
        if action not in known:
            _fail(f"kubectl {sub} cần hành động ngay sau, một trong: {', '.join(sorted(known))}")
    need = _lowest(sub, action)
    if PROFILES.index(need) > PROFILES.index(profile):
        _fail(f"kubectl {sub}{' ' + action if action else ''} thuộc nhóm {LABEL[need]}, hãy dùng công cụ {TOOL[need]}")
    if sub == "cluster-info" and "dump" in argv:
        _fail("kubectl cluster-info dump không được hỗ trợ")
    stdin = False
    i = 1
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if a == "--":
            break  # phần sau là lệnh chạy trong pod (exec, run)
        if a.startswith("--"):
            name, eq, value = a[2:].partition("=")
            if name in BLOCKED:
                _fail(f"flag --{name} không được dùng qua kagent")
            if name == "filename":
                if (value if eq else nxt) != "-":
                    _fail("chỉ được --filename=- ; truyền manifest qua tham số manifest")
                stdin = True
            if name == "output":
                _check_output(value if eq else nxt)
        elif a.startswith("-") and len(a) > 1 and not a[1:].isdigit():
            letter, rest = a[1], a[2:]
            if letter not in SHORT or (rest and not rest.startswith("=")):
                _fail(f"flag {a} không được dùng; viết tách flag ngắn (ví dụ -n default), flag khác dùng dạng dài --tên")
            value = rest[1:] if rest else nxt
            if letter == "f":
                if sub == "logs":
                    _fail("không hỗ trợ theo dõi log liên tục (-f); dùng --tail hoặc --since")
                if value != "-":
                    _fail("chỉ được -f - ; truyền manifest qua tham số manifest")
                stdin = True
            if letter == "o":
                _check_output(value)
        i += 1
    key = f"{sub} {action}" if action else sub
    read_only = need == "read" or sub == "diff"
    return argv, {"sub": sub, "action": action, "key": key, "stdin": stdin, "read_only": read_only, "dry_run": key in DRY_RUN or sub in DRY_RUN and sub not in ACTIONS}


def kubeconfig(p: dict, token: str) -> str:
    c = k8s._conn(p)
    u = net.check_url(c["api_url"], c["allow_private"])
    port = u.port or net.default_port(u.scheme)
    ip = net.check_host(u.host, port, c["allow_private"])
    cluster = {"server": f"{u.scheme}://{f'[{ip}]' if ':' in ip else ip}:{port}{u.path.rstrip('/')}"}
    if not c["verify_tls"]:
        cluster["insecure-skip-tls-verify"] = True
    else:
        if ip != u.host:
            cluster["tls-server-name"] = u.host
        if c["ca_cert"]:
            cluster["certificate-authority-data"] = base64.b64encode(c["ca_cert"].encode()).decode()
    ctx = {"cluster": "kagent", "user": "kagent", "namespace": p.get("namespace") or "default"}
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "current-context": "kagent", "clusters": [{"name": "kagent", "cluster": cluster}],
                           "users": [{"name": "kagent", "user": {"token": token}}], "contexts": [{"name": "kagent", "context": ctx}]})


def _exec(argv: list[str], config: str, stdin: str | None, timeout: int) -> tuple[int, str, str, bool]:
    exe = shutil.which(KUBECTL)
    if exe is None and runner is None:
        _fail("máy chủ kagent chưa cài kubectl")
    with tempfile.TemporaryDirectory(prefix="kagent-kubectl-") as d:
        path = os.path.join(d, "config")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            f.write(config)
        # Env sạch: không để lộ biến của kagent (khóa chủ...) cho kubectl hay chương trình diff nó gọi.
        env = {"PATH": f"{os.path.dirname(exe or '/usr/local/bin/kubectl')}:/usr/local/bin:/usr/bin:/bin", "HOME": d, "KUBECONFIG": path,
               "LANG": "C.UTF-8"}
        full = [exe or KUBECTL, f"--request-timeout={timeout}s", f"--cache-dir={os.path.join(d, 'cache')}", *argv]
        if runner is not None:
            return runner(full, env, stdin, timeout)
        try:
            proc = subprocess.run(full, input=(stdin or "").encode(), capture_output=True, timeout=timeout + 5, env=env, cwd=d)
        except subprocess.TimeoutExpired as exc:
            return -1, (exc.stdout or b"").decode("utf-8", "replace"), (exc.stderr or b"").decode("utf-8", "replace"), True
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"), False


_KLOG = re.compile(r"^[EWI]\d{4} \d{2}:\d{2}:\d{2}\.\d+\s+\d+ \S+:\d+\] ")


def _error_text(text: str) -> str:
    """Bỏ dòng log nội bộ của kubectl (E1112 06:09:51.88 19 memcache.go:265] ...) nếu còn dòng lỗi thật."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    plain = [line for line in lines if not _KLOG.match(line)]
    return "\n".join(plain or [_KLOG.sub("", lines[-1])] if lines else [])


def _token(p: dict, profile: str) -> str:
    token = p.get(TOKEN_KEY[profile])
    if not token:
        _fail(f"nhóm quyền {LABEL[profile]} chưa được áp dụng vào cụm; vào Công cụ → Kết nối, mục Quyền Kubernetes của kết nối này để cấu hình rồi bấm Áp dụng")
    return token


def _with_dry_run(argv: list[str]) -> list[str]:
    argv = [a for a in argv if a != "--dry-run" and not a.startswith("--dry-run=")]
    cut = argv.index("--") if "--" in argv else len(argv)
    return [*argv[:cut], "--dry-run=server", *argv[cut:]]


def _run(p: dict, args: dict, profile: str, dry: bool = False) -> dict:
    argv, info = parse(args.get("command"), profile)
    manifest = args.get("manifest")
    if manifest is not None and (not isinstance(manifest, str) or len(manifest) > MAX_MANIFEST):
        _fail(f"manifest phải là chuỗi YAML/JSON tối đa {MAX_MANIFEST} ký tự")
    if info["stdin"] and not manifest:
        _fail("lệnh có -f - nhưng thiếu tham số manifest")
    if manifest and not info["stdin"]:
        _fail("có manifest thì lệnh phải có -f -")
    if dry:
        if info["read_only"]:
            return {"read_only": True, "note": "lệnh chỉ đọc, không cần chạy thử"}
        if not info["dry_run"]:
            _fail(f"kubectl {info['key']} không có chế độ chạy thử phía server")
        argv = _with_dry_run(argv)
    timeout = integer(args, "timeout_seconds", 30, 1, 120)
    code, out, err, timed_out = _exec(argv, kubeconfig(p, _token(p, profile)), manifest, timeout)
    result = {"command": "kubectl " + shlex.join(argv), "exit_code": code, "stdout": clip(out), "stderr": clip(err, 5000)}
    if timed_out:
        result["timed_out"] = True
    elif code != 0 and not (info["key"] in LENIENT and code == 1):
        _fail(f"kubectl lỗi (mã {code}): {clip(_error_text(err or out), 1500)}")
    return result


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


# Kết quả coi là tin cậy khi chỉ gồm tên, trạng thái, số liệu do Kubernetes sinh ra, không có chuỗi tự do người khác ghi được
# (log, annotation, message của event, nội dung configmap, cột in của CRD, mô tả schema).
TABLE_KINDS = {"pods", "pod", "po", "deployments", "deployment", "deploy", "replicasets", "replicaset", "rs", "statefulsets", "statefulset", "sts",
               "daemonsets", "daemonset", "ds", "services", "service", "svc", "nodes", "node", "no", "namespaces", "namespace", "ns", "jobs", "job",
               "cronjobs", "cronjob", "cj", "persistentvolumeclaims", "persistentvolumeclaim", "pvc", "persistentvolumes", "persistentvolume", "pv",
               "endpoints", "ep", "ingresses", "ingress", "ing", "horizontalpodautoscalers", "horizontalpodautoscaler", "hpa", "serviceaccounts",
               "serviceaccount", "sa", "storageclasses", "storageclass", "sc", "poddisruptionbudgets", "poddisruptionbudget", "pdb"}
TRUSTED_KEYS = {"top", "version", "api-versions", "api-resources", "auth can-i", "auth whoami", "rollout status"}
MUTATIONS = DRY_RUN - {"run"}  # kết quả mặc định chỉ là "kind/tên đã làm gì"


def _positionals(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv[1:]:
        if a == "--":
            break
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = "=" not in a and a in ("-n", "--namespace", "-l", "--selector", "-c", "--container", "-o", "--output", "-L", "--label-columns",
                                          "--sort-by", "-p", "--patch", "--field-selector", "--replicas", "--type")
        else:
            out.append(a)
    return out


def _output(argv: list[str]) -> str | None:
    for i, a in enumerate(argv):
        if a in ("-o", "--output"):
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith(("-o=", "--output=")):
            return a.split("=", 1)[1]
    return None


def trusted_output(args: dict) -> bool:
    """Lệnh này chỉ trả dữ liệu có cấu trúc (bảng tên/trạng thái, xác nhận thay đổi), nên không làm 'nhiễm' hội thoại."""
    try:
        argv, info = parse(args.get("command"), "admin")
    except ConnectorError:
        return False
    if any(a == "--raw" or a.startswith(("--raw=", "--template", "--show-managed-fields")) for a in argv):
        return False
    output = _output(argv)
    if info["key"] in TRUSTED_KEYS:
        return output in (None, "wide", "name")
    if info["sub"] == "get":
        pos = _positionals(argv)
        kinds = [k.split("/")[0].split(".")[0].lower() for k in (pos[0].split(",") if pos else [""])]
        return output in (None, "wide", "name") and all(k in TABLE_KINDS for k in kinds)
    if info["key"] in MUTATIONS or info["sub"] in MUTATIONS:
        return output in (None, "name")
    return False
