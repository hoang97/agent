"""Cách hiện một lệnh và nguyên nhân trên trang sự cố, tính lúc đọc từ incident_action/incident nên áp cho cả dữ liệu cũ."""
from __future__ import annotations

import json
import re

from .common import describe, first_sentence

HIDDEN_TOOLS = {"data.script"}  # tính trên kết quả đã có trong sandbox, không đụng hệ thống nào


def _args(row: dict) -> dict:
    a = row.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return {}
    return a if isinstance(a, dict) else {}


def _short(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _first_line(s) -> str:
    return next((x.strip() for x in str(s or "").splitlines() if x.strip()), "")


def _command(tool: str, command: str) -> str:
    """Bỏ phần payload dài (-p '[…]', JSON, heredoc) để dòng lệnh vừa một dòng; bản đủ nằm trong phần chi tiết."""
    cut = re.search(r"\s(?:-p|--patch|--overrides|--set-json)\s|\s['\"]?[\[{]", command)
    head = command[:cut.start()] + " …" if cut else command
    return f"{tool.split('.')[-1]} {_short(head, 160)}"


def _gitlab(args: dict) -> tuple[str | None, str]:
    method, path = str(args.get("method") or "GET").upper(), str(args.get("path") or "")
    body = args.get("body") if isinstance(args.get("body"), dict) else {}
    cmd = f"{method} {path}"
    if path.endswith("/repository/commits") and method == "POST":
        files = [a.get("file_path") for a in body.get("actions") or [] if isinstance(a, dict) and a.get("file_path")]
        what = f"Commit «{_short(_first_line(body.get('commit_message')), 90)}» lên nhánh {body.get('branch', '?')}"
        return what + (f" ({', '.join(files[:3])}{' …' if len(files) > 3 else ''})" if files else ""), cmd
    if path.endswith("/merge_requests") and method == "POST":
        return f"Mở MR «{_short(body.get('title') or '', 90)}» ({body.get('source_branch', '?')} → {body.get('target_branch', '?')})", cmd
    if path.endswith("/notes") and method == "POST":
        return "Bình luận: " + _short(body.get("body") or "", 100), cmd
    return None, cmd


def _gitlab_result(raw: str) -> str | None:
    """Kết quả GitLab bị cắt 2000 ký tự nên đọc bằng regex thay vì json.loads."""
    status = re.match(r'\{"status": (\d+)', raw)
    if not status:
        return None
    parts = [f"HTTP {status.group(1)}"]
    if m := re.search(r'"iid": (\d+)', raw):
        parts.append(f"!{m.group(1)}")
    elif m := re.search(r'"short_id": "(\w+)"', raw):
        parts.append(f"commit {m.group(1)}")
    if m := re.search(r'"message": "([^"]{1,160})"', raw):
        parts.append(m.group(1))
    return " · ".join(parts)


def action_view(row: dict) -> dict:
    """title: việc đã làm bằng lời; cmd: lệnh gọn một dòng; result: kết quả ngắn; detail: tham số đầy đủ cho phần gập."""
    tool, args = row["tool"], _args(row)
    title = None
    if isinstance(args.get("command"), str):
        cmd = _command(tool, args["command"])
        title = describe(args)
    elif "method" in args and "path" in args:
        title, cmd = _gitlab(args)
    else:
        cmd = tool.split(".")[-1] + " " + " ".join(f"{k}={_short(v, 40)}" for k, v in args.items()
                                                    if not k.startswith("_") and isinstance(v, (str, int, float, bool)))
    why = first_sentence(row.get("why"), 180)
    title = _cap(title or why or "") or cmd
    raw = row.get("result") or ""
    result = _gitlab_result(raw) if raw.startswith('{"status"') else None
    line = row.get("result_line") or ""
    if result is None and line and not line.lstrip().startswith(("{", "[")):
        result = _short(line, 200)
    if row.get("status") == "error":
        result = "lỗi" + (f": {result or _short(line, 200)}" if (result or line) else "")
    detail = json.dumps(args, ensure_ascii=False, indent=2)
    return {"title": title, "why": why if why and why != title else None, "cmd": cmd.strip(), "result": result,
            "detail": detail if len(detail) <= 4000 else detail[:4000] + "\n…"}


SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[\"'`(\[]?[A-ZÀ-Ỹ0-9])")
GAP = re.compile(r"^(chưa|không có bằng chứng|không đọc được|không truy cập|cần xác nhận)", re.I)


def cause_parts(text: str | None) -> dict | None:
    """Nguyên nhân dạng đoạn liền thành: câu kết luận, các ý bằng chứng, các điểm chưa xác nhận.

    Câu mở đầu ngắn kiểu "Chưa chắc chắn." chỉ là rào đón: tách thành hedge để câu sau làm kết luận.
    """
    if not text or not text.strip():
        return None
    lines = [x.strip(" -•*") for x in text.strip().splitlines() if x.strip()]
    sentences = [s.strip() for x in lines for s in SENTENCE.split(x) if s.strip()]
    hedge = None
    if len(sentences) > 1 and len(sentences[0]) < 40 and GAP.match(sentences[0]):
        hedge = sentences.pop(0).rstrip(".")
    lead = _cap(re.sub(r"^Bằng chứng cho thấy\s+", "", sentences[0]))
    points, gaps = [], []
    for s in sentences[1:]:
        s = _cap(re.sub(r"^Bằng chứng:\s*", "", s))
        (gaps if GAP.match(s) else points).append(s)
    return {"hedge": hedge, "lead": lead, "points": points, "gaps": gaps}


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def headline(text: str | None, limit: int = 160) -> str:
    """Câu kết luận cho dòng sự kiện "điều tra": bỏ câu rào đón để không chỉ còn "Chưa chắc chắn"."""
    parts = cause_parts(text)
    return first_sentence(parts["lead"], limit) if parts else ""
