"""Chạy kubectl thật bằng token ServiceAccount của từng nhóm quyền (xem k8s_rbac).

RBAC của cụm quyết định lệnh nào được phép; ở đây chỉ chặn những gì RBAC không chặn được:
- đọc hay ghi file trên máy chủ aiops (-f đường dẫn, --from-file, -o *-file, --kubeconfig, --log-file...),
- đổi danh tính hoặc đích đến (--as, --token, --server, --context...), lộ token (config view, -v),
- phiên tương tác hoặc kéo dài (-i, -t, --watch, --follow, proxy, port-forward, attach, cp, edit).
Kubeconfig tạm trỏ thẳng vào IP đã kiểm tra bởi net (chống DNS rebinding), TLS vẫn xác thực theo tên gốc.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

import httpx
import yaml

from .. import net
from . import k8s
from .base import ConnectorError, clip, integer
from .k8s_rbac import LABEL, PROFILES, TOKEN_KEY, TOOL

KUBECTL = os.environ.get("AIOPS_KUBECTL") or "kubectl"
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
        _fail("không đọc template từ file trên máy chủ aiops; dùng -o jsonpath=... hoặc -o go-template=... viết thẳng trong lệnh")


SHELL_OPS = {";", "&", "&&", "||", ">", ">>", "<", "<<", "2>", "2>&1", "&>", "$(", "`"}
FILTERS_HELP = "sau | chỉ hỗ trợ grep [-i -v -E -F -w -c] [-e] mẫu, head -n N, tail -n N, wc -l (aiops tự lọc, không chạy shell)"


def _split_pipeline(argv: list[str]) -> tuple[list[str], list[list[str]]]:
    """Model hay viết `... | grep -i x` như trong shell. Không có shell nên tự lọc stdout; toán tử shell khác thì báo rõ thay vì để kubectl hiểu nhầm là tên tài nguyên."""
    cut = argv.index("--") if "--" in argv else len(argv)  # sau -- là lệnh chạy trong pod
    for a in argv[:cut]:
        if a in SHELL_OPS or a.startswith((">", "<", "2>", "&>", "$(", "`")):
            _fail(f"không hỗ trợ toán tử shell {a}; mỗi lần gọi chạy một lệnh kubectl, lọc bằng -l, --field-selector, -o jsonpath=... hoặc | grep")
    if "|" not in argv[:cut]:
        return argv, []
    parts, cur = [], []
    for a in argv[:cut]:
        if a == "|":
            parts.append(cur)
            cur = []
        else:
            cur.append(a)
    parts.append(cur)
    if cut < len(argv):
        _fail("không lọc | được khi lệnh có -- (exec, run)")
    return parts[0], parts[1:]


def _count(value: str) -> int:
    if not value.isdigit():
        _fail(f"số dòng không hợp lệ: {value}; {FILTERS_HELP}")
    return int(value)


def _bre(pattern: str) -> str:
    """grep không -E dùng regex cơ bản: \\| \\( \\+ ... mới là toán tử, | ( + đứng trần là ký tự thường."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and pattern[i + 1] in "|(){}+?":
            out.append(pattern[i + 1])
            i += 2
            continue
        out.append("\\" + c if c in "|(){}+?" else c)
        if c == "\\" and i + 1 < len(pattern):
            out.append(pattern[i + 1])
            i += 1
        i += 1
    return "".join(out)


def _filter(stage: list[str]) -> tuple:
    if not stage:
        _fail(f"thiếu lệnh sau |; {FILTERS_HELP}")
    name, args = stage[0], stage[1:]
    if name in ("head", "tail"):
        if not args:
            return (name, 10)
        if len(args) == 2 and args[0] == "-n":
            return (name, _count(args[1]))
        if len(args) == 1 and args[0].startswith("-n"):
            return (name, _count(args[0][2:]))
        if len(args) == 1 and args[0].startswith("-"):
            return (name, _count(args[0][1:]))
        _fail(FILTERS_HELP)
    if name == "wc":
        if args != ["-l"]:
            _fail(FILTERS_HELP)
        return ("wc", 0)
    if name not in ("grep", "egrep"):
        _fail(f"không chạy được {name}; {FILTERS_HELP}")
    flags, patterns, i = set("E" if name == "egrep" else ""), [], 0
    while i < len(args):
        a = args[i]
        if a == "-e":
            if i + 1 >= len(args):
                _fail("grep -e thiếu mẫu")
            patterns.append(args[i + 1])
            i += 2
            continue
        if a.startswith("-") and len(a) > 1 and not patterns:
            bad = set(a[1:]) - set("ivEFwc")
            if bad:
                _fail(f"grep -{''.join(sorted(bad))} không hỗ trợ; {FILTERS_HELP}")
            flags |= set(a[1:])
        else:
            patterns.append(a)
        i += 1
    if not patterns:
        _fail(f"grep thiếu mẫu; {FILTERS_HELP}")
    parts = [re.escape(p) if "F" in flags else p if "E" in flags else _bre(p) for p in patterns]
    if "w" in flags:
        parts = [rf"(?<!\w)(?:{p})(?!\w)" for p in parts]
    try:
        rx = re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE if "i" in flags else 0)
    except re.error as exc:
        _fail(f"mẫu grep không hợp lệ: {exc}")
    return ("grep", rx, "v" in flags, "c" in flags)


def _apply_filters(text: str, filters: list[tuple]) -> str:
    lines = text.splitlines()
    for f in filters:
        if f[0] == "grep":
            lines = [line for line in lines if bool(f[1].search(line)) != f[2]]
            if f[3]:
                lines = [str(len(lines))]
        elif f[0] == "head":
            lines = lines[:f[1]]
        elif f[0] == "tail":
            lines = lines[-f[1]:] if f[1] else []
        else:
            lines = [str(len(lines))]
    return "".join(line + "\n" for line in lines)


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
    argv, stages = _split_pipeline(argv)
    filters = [_filter(stage) for stage in stages]
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
                _fail(f"flag --{name} không được dùng qua aiops")
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
    if filters and not read_only:
        _fail("chỉ lệnh đọc mới được lọc qua | grep/head/tail")
    return argv, {"sub": sub, "action": action, "key": key, "stdin": stdin, "read_only": read_only, "filters": filters,
                  "pipe": " | ".join(shlex.join(stage) for stage in stages),
                  "dry_run": key in DRY_RUN or sub in DRY_RUN and sub not in ACTIONS}


def kubeconfig(p: dict, token: str) -> str:
    from .. import proxy
    c = k8s._conn(p)
    u = net.check_url(c["api_url"], c["allow_private"])
    port = u.port or net.default_port(u.scheme)
    ip = net.check_host(u.host, port, c["allow_private"])
    try:
        route = proxy.routes(c["api_url"], p.get("proxy"))[0]  # kubectl chỉ nhận một proxy, không thử đường dự phòng
    except proxy.ProxyError as exc:
        raise net.NetError(str(exc)) from exc
    if route == "DIRECT":
        cluster = {"server": f"{u.scheme}://{f'[{ip}]' if ':' in ip else ip}:{port}{u.path.rstrip('/')}"}
    else:
        if p.get("proxy") not in (None, "", "auto"):
            pu = httpx.URL(route)
            net.check_host(pu.host, pu.port or net.default_port(pu.scheme), c["allow_private"])
        # Qua proxy thì proxy kết nối theo tên máy, không ghim IP được; đích vẫn đã qua kiểm tra IP ở trên.
        cluster = {"server": f"{u.scheme}://{u.host}:{port}{u.path.rstrip('/')}", "proxy-url": route}
    if not c["verify_tls"]:
        cluster["insecure-skip-tls-verify"] = True
    else:
        if route == "DIRECT" and ip != u.host:
            cluster["tls-server-name"] = u.host
        if c["ca_cert"]:
            cluster["certificate-authority-data"] = base64.b64encode(c["ca_cert"].encode()).decode()
    ctx = {"cluster": "aiops", "user": "aiops", "namespace": p.get("namespace") or "default"}
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "current-context": "aiops", "clusters": [{"name": "aiops", "cluster": cluster}],
                           "users": [{"name": "aiops", "user": {"token": token}}], "contexts": [{"name": "aiops", "context": ctx}]})


def _exec(argv: list[str], config: str, stdin: str | None, timeout: int, interruptible: bool = False) -> tuple[int, str, str, bool]:
    """interruptible: lệnh chỉ đọc, bấm dừng thì giết ngay. Lệnh ghi luôn chạy tới hết để biết chắc nó đã làm gì."""
    exe = shutil.which(KUBECTL)
    if exe is None and runner is None:
        _fail("máy chủ aiops chưa cài kubectl")
    with tempfile.TemporaryDirectory(prefix="aiops-kubectl-") as d:
        path = os.path.join(d, "config")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            f.write(config)
        # Env sạch: không để lộ biến của aiops (khóa chủ...) cho kubectl hay chương trình diff nó gọi.
        env = {"PATH": f"{os.path.dirname(exe or '/usr/local/bin/kubectl')}:/usr/local/bin:/usr/bin:/bin", "HOME": d, "KUBECONFIG": path,
               "LANG": "C.UTF-8"}
        full = [exe or KUBECTL, f"--request-timeout={timeout}s", f"--cache-dir={os.path.join(d, 'cache')}", *argv]
        if runner is not None:
            return runner(full, env, stdin, timeout)
        return spawn(full, env, d, stdin, timeout + 5, interruptible)


def spawn(full: list[str], env: dict, cwd: str, stdin: str | None, deadline_s: float, interruptible: bool) -> tuple[int, str, str, bool]:
    from .. import live
    # Nhóm tiến trình riêng để giết được cả tiến trình con (plugin, credential helper) khi dừng hoặc hết giờ.
    proc = subprocess.Popen(full, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=True)
    deadline = time.monotonic() + deadline_s
    data = (stdin or "").encode()
    while True:
        try:
            out, err = proc.communicate(input=data, timeout=0.25)
            return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), False
        except subprocess.TimeoutExpired:
            data = None  # đã gửi stdin ở lần gọi đầu
            stopped = interruptible and live.stopping()
            if stopped or time.monotonic() > deadline:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                out, err = proc.communicate()
                if stopped:
                    _fail("đã dừng theo yêu cầu")
                return -1, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), True


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
    code, out, err, timed_out = _exec(argv, kubeconfig(p, _token(p, profile)), manifest, timeout, interruptible=info["read_only"] or dry)
    command = "kubectl " + shlex.join(argv)
    if info["filters"]:
        command += " | " + info["pipe"]
        out = _apply_filters(out, info["filters"])
    result = {"command": command, "exit_code": code, "stdout": clip(out), "stderr": clip(err, 5000)}
    if timed_out:
        result["timed_out"] = True
    elif code != 0 and not (info["key"] in LENIENT and code == 1):
        text = _error_text(err or out)
        if "across all namespaces" in text:
            text += "\nGợi ý: bỏ -A và dùng -n <namespace>; chưa biết namespace thì get <loại> -A --field-selector=metadata.name=<tên>"
        _fail(f"kubectl lỗi (mã {code}): {clip(text, 1500)}")
    if info["filters"] and not out:
        result["note"] = "không có dòng nào khớp bộ lọc"
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


# trusted_view: phần có cấu trúc của describe và get events để học tri thức từ sự cố. Message của event, annotation, label, env, command/args,
# message của điều kiện là chữ do ứng dụng hay người triển khai viết, nên chỉ giữ theo danh sách cho phép; ô không đúng dạng thì lược.
DESCRIBE_KEYS = {
    "name", "namespace", "priority", "priority class name", "service account", "node", "start time", "status", "reason", "ip", "controlled by",
    "qos class", "container id", "image", "image id", "port", "ports", "host port", "host ports", "state", "last state", "ready", "restart count",
    "exit code", "signal", "started", "finished", "cpu", "memory", "ephemeral-storage", "pods", "replicas", "strategytype", "minreadyseconds",
    "desired number of nodes scheduled", "current number of nodes scheduled", "number of nodes misscheduled", "pods status", "type", "roles",
    "unschedulable", "internalip", "externalip", "hostname", "kernel version", "os image", "operating system", "architecture",
    "container runtime version", "kubelet version", "kube-proxy version", "podcidr", "providerid", "capacity", "storageclass", "access modes",
    "volumemode", "volume", "used by", "claimname", "readonly", "optional", "completions", "parallelism", "succeeded", "failed", "active",
    "schedule", "last schedule time", "min replicas", "max replicas", "current replicas", "desired replicas", "clusterip", "clusterips",
    "targetport", "endpoints", "session affinity", "loadbalancer ingress", "nodeport", "ip family policy", "ip families", "created",
}
DESCRIBE_SECTIONS = {"containers", "init containers", "conditions", "events", "limits", "requests", "capacity", "allocatable", "addresses",
                     "volumes", "system info", "allocated resources", "replica set status", "pod template", "port", "mounts"}
DROP_COLUMNS = {"message", "note", "lastheartbeattime", "lasttransitiontime"}
_CELL = re.compile(r"^[\w.:/@+\-<>,%=]{0,128}$")
_AGE = re.compile(r"^\d+[smhdy]\w*( \(x\d+ over \d+[smhdy]\w*\))?$")
_DATE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d [+-]\d{4}$")
_KEYLINE = re.compile(r"^(\s*)([A-Za-z][\w .\-/]{0,40}?):(?:\s+(.*))?$")
LACKS = "…"
EVENT_PATHS = {".lastTimestamp", ".firstTimestamp", ".eventTime", ".reason", ".type", ".count", ".involvedObject.kind", ".involvedObject.name",
               ".involvedObject.namespace", ".metadata.namespace", ".source.component", ".source.host", ".reportingController"}


def _cell(v: str) -> str:
    v = v.strip()
    return v if _CELL.match(v) or _AGE.match(v) or _DATE.match(v) else LACKS


def _columns(header: str, dashes: str | None) -> list[tuple[int, int, str]]:
    marks = [(m.start(), m.end()) for m in re.finditer(r"-+" if dashes else r"\S+(?: \S+)*", dashes or header)]
    return [(s, marks[i + 1][0] if i + 1 < len(marks) else 10**6, header[s:marks[i + 1][0] if i + 1 < len(marks) else None].strip())
            for i, (s, _) in enumerate(marks)]


def _table(lines: list[str], dashes: bool) -> list[str]:
    """Bảng căn cột: dòng tiêu đề (kèm dòng gạch nếu có) rồi các dòng dữ liệu; bỏ cột chữ tự do, kiểm từng ô."""
    cols = _columns(lines[0], lines[1] if dashes else None)
    keep = [c for c in cols if c[2].lower() not in DROP_COLUMNS]
    out = ["   ".join(c[2] for c in keep)]
    for row in lines[2 if dashes else 1:]:
        out.append("   ".join(_cell(row[s:e]) for s, e, _ in keep))
    return out


def _describe(text: str) -> str:
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() and set(nxt.strip()) <= {"-", " "} and line.strip():  # tiêu đề bảng có dòng gạch: Events, Conditions...
            indent = line[:len(line) - len(line.lstrip())]
            j = i + 2
            while j < len(lines) and lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) >= len(indent):
                j += 1
            out += [indent + r for r in _table([l[len(indent):] for l in lines[i:j]], dashes=True)]
            i = j
            continue
        m = _KEYLINE.match(line.rstrip())
        if m:
            indent, key, value = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            low = key.lower()
            if not value and (low in DESCRIBE_SECTIONS or _CELL.match(key)):
                out.append(f"{indent}{key}:")
            elif value and low in DESCRIBE_KEYS:
                out.append(f"{indent}{key}: {_cell(value)}")
        i += 1
    return "\n".join(out)


def trusted_view(result):
    """Phần có cấu trúc của kết quả kubectl_* (trusted_view): describe, get events dạng bảng hoặc JSON. Lệnh khác (logs, exec,
    -o yaml...) trả None: vẫn là chữ tự do."""
    if not isinstance(result, dict) or not isinstance(result.get("stdout"), str) or not isinstance(result.get("command"), str):
        return None
    try:
        argv, _ = _split_pipeline(shlex.split(result["command"])[1:])
    except (ValueError, ConnectorError):
        return None
    if not argv:
        return None
    text = result["stdout"]
    if argv[0] == "describe":
        return {"stdout": _describe(text)}
    kinds = (_positionals(argv)[:1] or [""])[0].split(",")
    if argv[0] != "get" or not all(k.split(".")[0].lower() in ("events", "event", "ev") for k in kinds):
        return None
    output = _output(argv)
    if output in (None, "wide"):
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines or not re.match(r"^\s*(NAMESPACE|LAST SEEN)\b", lines[0]):
            return None  # bộ lọc grep đã bỏ dòng tiêu đề: không biết cột nào là message
        return {"stdout": "\n".join(_table(lines, dashes=False))}
    if output and output.startswith("custom-columns="):
        paths = [c.split(":", 1)[1] if ":" in c else "" for c in output.split("=", 1)[1].split(",")]
        lines = [l for l in text.splitlines() if l.strip()]
        if not lines or not all(p in EVENT_PATHS for p in paths) or len(lines[0].split()) != len(paths):
            return None  # cột tùy ý (message...) hoặc grep đã bỏ dòng tiêu đề
        return {"stdout": "\n".join(_table(lines, dashes=False))}
    if output == "json":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        keep = lambda e: {"type": _cell(str(e.get("type", ""))), "reason": _cell(str(e.get("reason", ""))), "count": e.get("count"),
                          "object": {k: _cell(str((e.get("involvedObject") or e.get("regarding") or {}).get(k, ""))) for k in ("kind", "name", "namespace")},
                          "last": _cell(str(e.get("lastTimestamp") or e.get("eventTime") or "")),
                          "source": _cell(str((e.get("source") or {}).get("component") or e.get("reportingController") or ""))}
        items = data.get("items") if isinstance(data, dict) and "items" in data else [data]
        return {"items": [keep(e) for e in items if isinstance(e, dict)][:500]}
    return None
