"""Xử lý kết quả công cụ đã lưu ở server: bộ lọc khai báo và script Python trong sandbox."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile

from .. import datafilter, db, results
from .base import ConnectorError, as_list, need

MAX_INPUTS = 5
SCRIPT_TIMEOUT = 15
MAX_OUTPUT = 200_000
MAX_CODE = 8000

# Chạy trong tiến trình con. Các lớp chặn chồng lên nhau vì không lớp nào đủ chắc khi đứng một mình:
# rlimit (CPU, RAM, ghi file, fork, số fd) + audit hook (open, socket, subprocess, os.*, import ngoài danh sách).
_BOOT = r'''
import sys, json, resource, io
import re, math, statistics, collections, itertools, functools, datetime, string, heapq, bisect, operator, decimal, fractions
import textwrap, unicodedata, ipaddress, urllib.parse, base64, hashlib
payload = json.loads(sys.stdin.read())
code = compile(payload["code"], "<script>", "exec")
env = {"inputs": payload["inputs"], "result": None, "__builtins__": __builtins__}
for name in ("re", "math", "statistics", "collections", "itertools", "functools", "datetime", "string", "heapq", "bisect",
             "operator", "decimal", "fractions", "textwrap", "unicodedata", "ipaddress", "base64", "hashlib", "json"):
    env[name] = sys.modules[name]
env["urllib"] = sys.modules["urllib"]
out = io.StringIO()
real_stdout = sys.stdout
resource.setrlimit(resource.RLIMIT_NOFILE, (3, 3))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
try:
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
except (ValueError, OSError):
    pass
BLOCK = ("open", "socket.", "subprocess.", "os.", "shutil.", "ctypes.", "sys.addaudithook", "pty.", "fcntl.", "mmap.", "winreg.",
         "_posixsubprocess", "signal.", "resource.", "code.__new__", "marshal.load")
allowed_exec = {id(code)}
def hook(event, args):
    if event == "exec":
        if id(args[0]) not in allowed_exec:
            raise RuntimeError("sandbox: không được exec/eval code khác")
        return
    if event == "import":
        raise RuntimeError(f"sandbox: không được import {args[0]}")
    if event == "compile" or event.startswith(BLOCK):
        raise RuntimeError(f"sandbox: chặn {event}")
sys.addaudithook(hook)
sys.stdout = out
try:
    exec(code, env)
    error = None
except BaseException as exc:
    error = f"{exc.__class__.__name__}: {exc}"
sys.stdout = real_stdout
try:
    text = json.dumps({"result": env.get("result"), "stdout": out.getvalue()[-20000:], "error": error}, ensure_ascii=False, default=str)
except Exception as exc:
    text = json.dumps({"result": None, "stdout": "", "error": f"result không chuyển được sang JSON: {exc}"})
real_stdout.write(text)
'''


def _limits() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (SCRIPT_TIMEOUT, SCRIPT_TIMEOUT + 1))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 2**20, 768 * 2**20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _load(p, result_id: str):
    with db.session() as conn:
        try:
            return results.load(conn, p["_tenant_id"], p.get("_user_id"), result_id)
        except datafilter.FilterError as exc:
            raise ConnectorError(str(exc)) from exc


def ping(p, args=None):
    return {"ok": True}


def query(p, args):
    row = _load(p, need(args, "result_id", r"r_[0-9a-f]{12}"))
    spec = {k: v for k, v in args.items() if k in datafilter.SPEC_KEYS}
    try:
        return {"result_id": row["id"], "source_tool": row["tool"], **datafilter.apply(row["data"], spec)}
    except datafilter.FilterError as exc:
        raise ConnectorError(str(exc)) from exc


def script(p, args):
    ids = as_list(args.get("result_ids"))
    if not 1 <= len(ids) <= MAX_INPUTS:
        raise ConnectorError(f"result_ids phải có 1-{MAX_INPUTS} phần tử")
    code = need(args, "code")
    if len(code) > MAX_CODE:
        raise ConnectorError(f"script dài quá {MAX_CODE} ký tự")
    inputs = {rid: _load(p, need({"id": rid}, "id", r"r_[0-9a-f]{12}"))["data"] for rid in ids}
    with tempfile.TemporaryDirectory(prefix="kagent-script-") as cwd:
        try:
            proc = subprocess.run([sys.executable, "-I", "-S", "-c", _BOOT], input=json.dumps({"inputs": inputs, "code": code}, ensure_ascii=False),
                                  capture_output=True, text=True, timeout=SCRIPT_TIMEOUT + 5, cwd=cwd, env={}, preexec_fn=_limits)
        except subprocess.TimeoutExpired as exc:
            raise ConnectorError(f"script chạy quá {SCRIPT_TIMEOUT} giây") from exc
    if proc.returncode and proc.returncode < 0:
        raise ConnectorError(f"script bị dừng vì vượt giới hạn CPU ({SCRIPT_TIMEOUT} giây) hoặc RAM")
    if len(proc.stdout) > MAX_OUTPUT:
        raise ConnectorError("đầu ra của script quá lớn; hãy tổng hợp trước khi gán vào result")
    try:
        out = json.loads(proc.stdout)
    except ValueError as exc:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["không có đầu ra"]
        raise ConnectorError(f"script lỗi: {tail[0][:300]}") from exc
    if out.get("error"):
        raise ConnectorError(f"script lỗi: {out['error'][:500]}")
    return {"result": out["result"], "stdout": out["stdout"]}


def propose_tool(p, args):
    from .. import learned
    with db.session() as conn:
        try:
            row = learned.propose_tool(conn, p["_tenant_id"], p.get("_user_id"), name=need(args, "name"), description=str(args.get("description") or ""),
                                       steps=args.get("steps"), params=args.get("params") or {}, scope=args.get("scope") or "user")
        except learned.LearnedError as exc:
            raise ConnectorError(str(exc)) from exc
    return {"name": row["name"], "status": row["status"], "scope": row["scope"], "steps": len(row["steps"]),
            "note": "Đã tạo công cụ ở trạng thái đề xuất; người có quyền xem các bước ở trang Công cụ & Chính sách rồi bật."}
