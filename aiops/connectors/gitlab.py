"""GitLab qua REST API v4, xác thực bằng access token (header PRIVATE-TOKEN).

Quyền thật là scope và role của token (read_api cho đọc; api + Developer/Maintainer cho ghi). Ở đây chỉ chia công cụ theo việc
gọi làm gì để mức rủi ro khớp: đọc chỉ GET và không đọc chỗ chứa bí mật; ghi là các việc pipeline, job, comment; còn lại là quản trị.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .base import ConnectorError, call, clip, http, integer, need, parse

PROJECT = r"[0-9]{1,12}|[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){1,20}"
ID = r"[0-9]{1,12}"
REF = r"(?!.*\.\.)[A-Za-z0-9_./@+-]{1,255}"
FILE_PATH = r"(?!.*\.\.)[^\x00-\x1f]{1,1000}"
NOUNS = {"merge_request": "merge_requests", "issue": "issues"}
# Nơi GitLab trả token, khóa hay giá trị biến CI: chỉ api_admin đọc được.
SECRET_PATHS = re.compile(r"/(variables|access_tokens|personal_access_tokens|deploy_tokens|deploy_keys|keys|hooks|integrations|services|"
                          r"secure_files|runners|impersonation_tokens|ssh_keys|gpg_keys|registration_token|feature_flags_user_lists)(/|$|\?)", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}


def _client(p):
    base = need(p, "url").rstrip("/")
    return http({**p, "url": base if base.endswith("/api/v4") else base + "/api/v4"}, "url",
                headers={"PRIVATE-TOKEN": p["token"], "Accept": "application/json"})


def _project(p, args) -> str:
    value = args.get("project") or p.get("default_project")
    return quote(need({"project": value}, "project", PROJECT).strip("/"), safe="")


def _limit(args, default=20, high=100) -> int:
    return integer(args, "limit", default, 1, high)


def _query(**kw) -> dict:
    return {k: v for k, v in kw.items() if v not in (None, "")}


def _opt(args, key, pattern=None):
    return need(args, key, pattern) if args.get(key) not in (None, "") else None


def ping(p, args=None):
    with _client(p) as c:
        version = call(c, "GET", "/version")
        user = call(c, "GET", "/user")
    return {"ok": True, "version": version.get("version"), "user": user.get("username")}


def list_projects(p, args):
    q = _query(search=str(args.get("search") or "")[:100], membership="true", simple="true", order_by="last_activity_at", per_page=_limit(args))
    with _client(p) as c:
        items = call(c, "GET", "/projects", params=q)
    return {"projects": [{"id": i.get("id"), "path": i.get("path_with_namespace"), "default_branch": i.get("default_branch"),
                          "last_activity": i.get("last_activity_at"), "url": i.get("web_url")} for i in items]}


def list_pipelines(p, args):
    q = _query(ref=_opt(args, "ref", REF), status=_opt(args, "status", r"[a-z_]{3,30}"), source=_opt(args, "source", r"[a-z_]{3,40}"),
               order_by="id", sort="desc", per_page=_limit(args))
    with _client(p) as c:
        items = call(c, "GET", f"/projects/{_project(p, args)}/pipelines", params=q)
    return {"pipelines": [{"id": i.get("id"), "ref": i.get("ref"), "status": i.get("status"), "source": i.get("source"),
                           "sha": (i.get("sha") or "")[:8], "created_at": i.get("created_at"), "url": i.get("web_url")} for i in items]}


def _job(j: dict) -> dict:
    return {"id": j.get("id"), "name": j.get("name"), "stage": j.get("stage"), "status": j.get("status"), "when": j.get("when"),
            "duration": j.get("duration"), "failure_reason": j.get("failure_reason"), "allow_failure": j.get("allow_failure"),
            "environment": (j.get("environment") or {}).get("name") if isinstance(j.get("environment"), dict) else None}


def get_pipeline(p, args):
    pid = need(args, "pipeline_id", ID)
    project = _project(p, args)
    with _client(p) as c:
        pipe = call(c, "GET", f"/projects/{project}/pipelines/{pid}")
        jobs = call(c, "GET", f"/projects/{project}/pipelines/{pid}/jobs", params={"per_page": 100})
    return {"id": pipe.get("id"), "ref": pipe.get("ref"), "sha": (pipe.get("sha") or "")[:8], "status": pipe.get("status"),
            "source": pipe.get("source"), "created_at": pipe.get("created_at"), "finished_at": pipe.get("finished_at"),
            "duration": pipe.get("duration"), "user": (pipe.get("user") or {}).get("username"), "url": pipe.get("web_url"),
            "jobs": [_job(j) for j in jobs]}


def job_log(p, args):
    jid = need(args, "job_id", ID)
    tail = integer(args, "tail_lines", 200, 10, 2000)
    with _client(p) as c:
        text = call(c, "GET", f"/projects/{_project(p, args)}/jobs/{jid}/trace", as_text=True)
    lines = ANSI.sub("", text).splitlines()
    grep = str(args.get("contains") or "")
    if grep:
        lines = [line for line in lines if grep.lower() in line.lower()]
    return {"job_id": int(jid), "total_lines": len(lines), "log": clip("\n".join(lines[-tail:]))}


def list_merge_requests(p, args):
    q = _query(state=_opt(args, "state", r"opened|closed|merged|locked|all") or "opened", target_branch=_opt(args, "target_branch", REF),
               search=str(args.get("search") or "")[:100], order_by="updated_at", sort="desc", per_page=_limit(args))
    with _client(p) as c:
        items = call(c, "GET", f"/projects/{_project(p, args)}/merge_requests", params=q)
    return {"merge_requests": [{"iid": i.get("iid"), "title": i.get("title"), "state": i.get("state"), "author": (i.get("author") or {}).get("username"),
                                "source": i.get("source_branch"), "target": i.get("target_branch"), "merged_at": i.get("merged_at"),
                                "updated_at": i.get("updated_at"), "url": i.get("web_url")} for i in items]}


def get_merge_request(p, args):
    iid = need(args, "mr_iid", ID)
    project = _project(p, args)
    with _client(p) as c:
        mr = call(c, "GET", f"/projects/{project}/merge_requests/{iid}")
        diffs = call(c, "GET", f"/projects/{project}/merge_requests/{iid}/diffs", params={"per_page": 50}) if args.get("include_diff") else None
    out = {"iid": mr.get("iid"), "title": mr.get("title"), "state": mr.get("state"), "author": (mr.get("author") or {}).get("username"),
           "source": mr.get("source_branch"), "target": mr.get("target_branch"), "merge_status": mr.get("detailed_merge_status") or mr.get("merge_status"),
           "pipeline": {k: (mr.get("head_pipeline") or {}).get(k) for k in ("id", "status")}, "merged_at": mr.get("merged_at"),
           "merge_commit": (mr.get("merge_commit_sha") or "")[:8] or None, "description": clip(mr.get("description") or "", 3000), "url": mr.get("web_url")}
    if diffs is not None:
        budget, files = 30000, []
        for d in diffs:
            diff = d.get("diff") or ""
            files.append({"path": d.get("new_path"), "new": d.get("new_file"), "deleted": d.get("deleted_file"), "diff": clip(diff, max(0, min(budget, 6000)))})
            budget -= len(diff)
        out["files"] = files
    return out


def list_commits(p, args):
    q = _query(ref_name=_opt(args, "ref", REF), since=_opt(args, "since", r"[0-9T:.+Z-]{10,35}"), path=_opt(args, "path", FILE_PATH), per_page=_limit(args))
    with _client(p) as c:
        items = call(c, "GET", f"/projects/{_project(p, args)}/repository/commits", params=q)
    return {"commits": [{"sha": i.get("short_id"), "title": i.get("title"), "author": i.get("author_name"), "created_at": i.get("created_at")}
                        for i in items]}


def list_deployments(p, args):
    q = _query(environment=_opt(args, "environment", r"[A-Za-z0-9_./ -]{1,255}"), status=_opt(args, "status", r"[a-z_]{3,20}"),
               order_by="created_at", sort="desc", per_page=_limit(args))
    with _client(p) as c:
        items = call(c, "GET", f"/projects/{_project(p, args)}/deployments", params=q)
    return {"deployments": [{"id": i.get("id"), "environment": (i.get("environment") or {}).get("name"), "status": i.get("status"), "ref": i.get("ref"),
                             "sha": (i.get("sha") or "")[:8], "created_at": i.get("created_at"), "user": (i.get("user") or {}).get("username"),
                             "job_id": (i.get("deployable") or {}).get("id")} for i in items]}


def list_issues(p, args):
    q = _query(state=_opt(args, "state", r"opened|closed|all") or "opened", labels=_opt(args, "labels", r"[^\x00-\x1f]{1,300}"),
               search=str(args.get("search") or "")[:100], order_by="updated_at", sort="desc", per_page=_limit(args))
    with _client(p) as c:
        items = call(c, "GET", f"/projects/{_project(p, args)}/issues", params=q)
    return {"issues": [{"iid": i.get("iid"), "title": i.get("title"), "state": i.get("state"), "labels": i.get("labels"),
                        "author": (i.get("author") or {}).get("username"), "updated_at": i.get("updated_at"), "url": i.get("web_url")} for i in items]}


def get_file(p, args):
    path = need(args, "path", FILE_PATH).lstrip("/")
    ref = _opt(args, "ref", REF) or "HEAD"
    with _client(p) as c:
        text = call(c, "GET", f"/projects/{_project(p, args)}/repository/files/{quote(path, safe='')}/raw", params={"ref": ref}, as_text=True)
    return {"path": path, "ref": ref, "content": clip(text, 40000)}


def _api_path(args) -> str:
    path = need(args, "path")
    if not path.startswith("/") or ".." in path or "://" in path or any(ch in path for ch in "\r\n\x00#"):
        raise ConnectorError("path phải bắt đầu bằng / và tính từ /api/v4, ví dụ /projects/123/environments")
    return path.removeprefix("/api/v4")


def _api(p, method: str, args) -> dict:
    query = args.get("query") if isinstance(args.get("query"), dict) else None
    body = args.get("body") if isinstance(args.get("body"), (dict, list)) else None
    with _client(p) as c:
        status, headers, text = call(c, method, _api_path(args), params=query, json_body=body, with_status=True)
    data = parse(status, headers, text, ok=(200, 201, 202, 204), as_text="json" not in headers.get("content-type", ""))
    return {"status": status, "json": data} if not isinstance(data, str) else {"status": status, "text": clip(data)}


def api_read(p, args):
    path = _api_path(args)
    if SECRET_PATHS.search(path):
        raise ConnectorError("đường dẫn này trả token, khóa hoặc biến CI; dùng api_admin nếu thật sự cần")
    return _api(p, "GET", args)


def api_admin(p, args):
    method = need(args, "method").upper()
    if method not in METHODS:
        raise ConnectorError(f"method phải là một trong {sorted(METHODS)}")
    return _api(p, method, args)


# ---------- ghi ----------

def _pipeline_action(p, args, action: str):
    pid = need(args, "pipeline_id", ID)
    with _client(p) as c:
        r = call(c, "POST", f"/projects/{_project(p, args)}/pipelines/{pid}/{action}")
    return {"pipeline_id": r.get("id"), "status": r.get("status"), "url": r.get("web_url")}


def retry_pipeline(p, args):
    return _pipeline_action(p, args, "retry")


def cancel_pipeline(p, args):
    return _pipeline_action(p, args, "cancel")


def _job_action(p, args, action: str):
    jid = need(args, "job_id", ID)
    with _client(p) as c:
        r = call(c, "POST", f"/projects/{_project(p, args)}/jobs/{jid}/{action}")
    return {"job_id": r.get("id"), "name": r.get("name"), "status": r.get("status"), "url": r.get("web_url")}


def retry_job(p, args):
    return _job_action(p, args, "retry")


def play_job(p, args):
    return _job_action(p, args, "play")


def note_target(p, args) -> tuple[str, str, str]:
    noun = NOUNS.get(str(args.get("target") or ""))
    if not noun:
        raise ConnectorError("target phải là merge_request hoặc issue")
    return _project(p, args), noun, need(args, "iid", ID)


def create_note(p, args):
    project, noun, iid = note_target(p, args)
    body = need(args, "body")[:20000]
    with _client(p) as c:
        r = call(c, "POST", f"/projects/{project}/{noun}/{iid}/notes", json_body={"body": body})
    return {"note_id": r.get("id"), "target": f"{noun[:-1]} !{iid}" if noun == "merge_requests" else f"issue #{iid}"}
