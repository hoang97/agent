"""L2: sinh playbook, core, hồ sơ thực thể và Skill từ KU active, quản lý bằng git.

LLM không viết lại playbook: Compiler dựng file một cách xác định từ các KU.
Nhánh proposal/* đóng vai trò pull request; golden task chặn publish nếu điểm giảm.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

from . import db
from .config import settings
from .retrieval import tokenize

_lock = threading.Lock()
_SLUG = re.compile(r"[^\w-]+", re.UNICODE)
HEADINGS = {"lesson": "Cạm bẫy", "insight": "Chiến lược", "procedure": "Thủ tục", "fact": "Sự thật", "rule": "Quy ước"}


class CompileError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def repo_path(tenant_id: str) -> Path:
    return settings.knowledge_dir / tenant_id


def _git_proc(tenant_id: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "user.name=kagent-compiler", "-c", "user.email=compiler@kagent.local", "-c", "core.autocrlf=false", *args],
                          cwd=repo_path(tenant_id), capture_output=True, text=True, encoding="utf-8")


def _git(tenant_id: str, *args: str, check: bool = True) -> str:
    proc = _git_proc(tenant_id, *args)
    if check and proc.returncode != 0:
        raise CompileError(f"git {args[0]} lỗi: {proc.stderr.strip()}", 500)
    return proc.stdout


def ensure_repo(tenant_id: str) -> None:
    path = repo_path(tenant_id)
    if (path / ".git").exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    _git(tenant_id, "init", "-q", "-b", "main")
    (path / "CHANGELOG.md").write_text("# Nhật ký thay đổi tri thức\n", encoding="utf-8")
    _git(tenant_id, "add", "-A")
    _git(tenant_id, "commit", "-q", "-m", "khởi tạo kho tri thức")


def read_published(tenant_id: str) -> dict[str, str]:
    path = repo_path(tenant_id)
    if not path.exists():
        return {}
    out = {}
    for file in path.rglob("*.md"):
        if ".git" not in file.parts:
            out[file.relative_to(path).as_posix()] = file.read_text(encoding="utf-8")
    return out


def read_file(tenant_id: str, rel: str) -> str:
    """Đọc một file trong kho, chặn thoát khỏi thư mục tenant."""
    root = repo_path(tenant_id).resolve()
    target = (root / rel).resolve()
    if root not in target.parents or ".git" in target.parts or not target.is_file():
        raise CompileError("không tìm thấy file", 404)
    return target.read_text(encoding="utf-8")


def _line(ku: dict) -> str:
    return f"- [ku:{ku['id'][:8]} ✓{ku['helpful_cnt']} ✗{ku['harmful_cnt']}] {ku['content']}"


def _rank(ku: dict) -> tuple:
    return (-(ku["helpful_cnt"] - ku["harmful_cnt"]), -ku["confidence"], ku["id"])


def _active(conn, tenant_id: str) -> list[dict]:
    # Ký ức cá nhân và tri thức riêng của dự án không được biên dịch vào playbook chung của tenant.
    rs = conn.execute("SELECT * FROM knowledge_unit WHERE tenant_id=? AND status='active' AND scope IN ('tenant','org') AND (valid_to IS NULL OR valid_to>?)",
                      (tenant_id, db.iso())).fetchall()
    return sorted(db.rows(rs, "entities", "source_eps"), key=_rank)


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:60] or "khac"


def skill_metadata(content: str) -> str:
    name = re.search(r"^name:\s*(.+)$", content, re.M)
    desc = re.search(r"^description:\s*(.+)$", content, re.M)
    return f"- {name.group(1) if name else '?'}: {desc.group(1) if desc else ''}"


def _success_stats(conn, tenant_id: str, domain: str) -> tuple[int, int]:
    r = conn.execute("SELECT count(*) n, coalesce(sum(outcome='success'),0) ok FROM episode WHERE tenant_id=? AND task_type=? AND started_at>=?",
                     (tenant_id, domain, db.later(days=-30))).fetchone()
    return r["n"], r["ok"]


def generate(conn, tenant_id: str) -> tuple[dict[str, str], list[dict]]:
    kus = _active(conn, tenant_id)
    files: dict[str, str] = {}
    rules = [k for k in kus if k["kind"] == "rule"]
    if rules:
        core, used = ["# Quy ước (luôn nạp)", ""], 0
        for ku in rules:
            line = _line(ku)
            if used + len(line) > settings.budget_core * 4:
                break
            core.append(line)
            used += len(line)
        files["core/rules.md"] = "\n".join(core) + "\n"

    for domain in sorted({k["domain"] for k in kus if k["kind"] in ("lesson", "insight", "procedure")}):
        body = [f"# Playbook: {domain}", ""]
        for kind in ("lesson", "insight", "procedure"):
            group = [k for k in kus if k["domain"] == domain and k["kind"] == kind]
            if group:
                body += [f"## {HEADINGS[kind]}", *[_line(k) for k in group], ""]
        files[f"playbooks/{slug(domain)}.md"] = "\n".join(body).rstrip() + "\n"

    by_entity: dict[str, list[dict]] = {}
    for ku in kus:
        if ku["kind"] == "fact":
            for entity in ku["entities"] or ["chung"]:
                by_entity.setdefault(entity, []).append(ku)
    for entity in sorted(by_entity):
        files[f"entities/{slug(entity)}.md"] = f"# {entity}\n\n" + "\n".join(_line(k) for k in by_entity[entity]) + "\n"

    skills = []
    for domain in sorted({k["domain"] for k in kus if k["kind"] == "procedure"}):
        group = [k for k in kus if k["domain"] == domain and k["kind"] == "procedure"]
        total, ok = _success_stats(conn, tenant_id, domain)
        if len(group) < 3 or ok < 5:
            continue
        name = f"{slug(domain)}-workflow"
        tools = sorted({e[5:] for k in group for e in k["entities"] if e.startswith("tool:")})
        rate = round(ok / total, 2) if total else 0.0
        kus_list = ", ".join(f"ku:{k['id'][:8]}" for k in group)
        steps = "\n".join(f"{i}. {k['content']}" for i, k in enumerate(group, 1))
        files[f"skills/{name}/SKILL.md"] = (
            f"---\nname: {name}\ndescription: Quy trình đã kiểm chứng cho nhiệm vụ loại {domain}; dùng khi nhiệm vụ thuộc loại này.\n"
            f"metadata:\n  source_kus: [{kus_list}]\n  success_rate_30d: {rate}\n  owner: {tenant_id}\n---\n\n# {name}\n\n{steps}\n")
        skills.append({"name": name, "domain": domain, "tools": tools, "rate": rate})
    return files, skills


def golden_score(conn, tenant_id: str, files: dict[str, str]) -> float | None:
    from . import agent, llm
    tasks = conn.execute("SELECT * FROM golden_task WHERE tenant_id=?", (tenant_id,)).fetchall()
    if not tasks:
        return None
    provider = llm.HeuristicProvider()  # đo trên router xác định để điểm không dao động theo model
    points = 0.0
    for t in tasks:
        terms = json.loads(t["expect_terms"])
        route = provider.route(t["query"])
        ctx = agent.assemble_context(conn, tenant_id, t["query"], route["task_type"], files=files)
        words = set(tokenize(ctx["text"]))
        hit = sum(1 for term in terms if all(p in words for p in tokenize(term))) / len(terms) if terms else 1.0
        points += 0.3 * (route["task_type"] == t["expect_domain"]) + 0.7 * hit
    return round(points / len(tasks), 4)


def _changed(current: dict[str, str], new: dict[str, str]) -> list[str]:
    keys = (set(current) | set(new)) - {"CHANGELOG.md"}
    return sorted(k for k in keys if current.get(k) != new.get(k))


def _record(conn, tenant_id, run_id, status, actor, **extra) -> dict:
    fields = dict(id=run_id, tenant_id=tenant_id, status=status, actor=actor, created_at=db.iso(), **extra)
    if "changed" in fields:
        fields["changed"] = json.dumps(fields["changed"], ensure_ascii=False)
    cols = ",".join(fields)
    conn.execute(f"INSERT INTO compile_run({cols}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values()))
    return db.row(conn.execute("SELECT * FROM compile_run WHERE id=?", (run_id,)).fetchone(), "changed")


def _publish_skills(conn, tenant_id: str, skills: list[dict]) -> None:
    for s in skills:
        conn.execute("INSERT INTO skill(tenant_id,name,domain,tools,success_rate_30d) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(tenant_id,name) DO UPDATE SET tools=excluded.tools, success_rate_30d=excluded.success_rate_30d",
                     (tenant_id, s["name"], s["domain"], json.dumps(s["tools"]), s["rate"]))


def _write(tenant_id: str, files: dict[str, str]) -> None:
    root = repo_path(tenant_id)
    for rel in read_published(tenant_id):
        if rel != "CHANGELOG.md" and rel not in files:
            (root / rel).unlink()
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


_RULE_LINE = re.compile(r"^- \[ku:([0-9a-f]{8})[^\]]*\] (.*)$", re.M)


def _needs_review(path: str) -> bool:
    """Chỉ khối core (luôn nạp vào mọi lượt) và Skill cần người duyệt; playbook và hồ sơ thực thể tự publish."""
    return path == "core/rules.md" or path.startswith("skills/")


def _summary(conn, tenant_id: str, current: dict[str, str], proposed: dict[str, str]) -> dict:
    """Thay đổi của phần cần duyệt dưới dạng danh sách quy ước thêm/bỏ và Skill, thay cho git diff có mã KU."""
    def rules(text: str) -> dict[str, str]:
        return {m.group(1): m.group(2) for m in _RULE_LINE.finditer(text or "")}
    old, new = rules(current.get("core/rules.md")), rules(proposed.get("core/rules.md"))

    def info(short: str, content: str) -> dict:
        r = conn.execute("SELECT id, title, source_trust, source_eps, recorded_at FROM knowledge_unit WHERE tenant_id=? AND id LIKE ?",
                         (tenant_id, short + "%")).fetchone()
        return {"ku": r["id"] if r else short, "title": r["title"] if r else "", "content": content,
                "source_trust": r["source_trust"] if r else None, "episodes": len(json.loads(r["source_eps"])) if r else 0}
    skills = sorted({p.split("/")[1] for p in set(current) | set(proposed) if p.startswith("skills/") and current.get(p) != proposed.get(p)})
    return {"rules_added": [info(k, v) for k, v in new.items() if k not in old],
            "rules_removed": [info(k, v) for k, v in old.items() if k not in new],
            "rules_kept": len(set(old) & set(new)), "skills": skills}


def _commit_main(tenant_id: str, files: dict[str, str], current: dict[str, str], changed: list[str], actor: str, message: str) -> str:
    _write(tenant_id, files)
    log = current.get("CHANGELOG.md", "# Nhật ký thay đổi tri thức\n")
    entry = f"\n## {db.iso()} — {actor}\n" + "\n".join(f"- {p}" for p in changed) + "\n"
    (repo_path(tenant_id) / "CHANGELOG.md").write_text(log + entry, encoding="utf-8", newline="\n")
    _git(tenant_id, "add", "-A")
    _git(tenant_id, "commit", "-q", "-m", message)
    return _git(tenant_id, "rev-parse", "HEAD").strip()


def _supersede_pending(conn, tenant_id: str, keep: str | None = None) -> None:
    """Mỗi tenant chỉ một đề xuất chờ duyệt: đề xuất cũ chuyển stale, xóa nhánh."""
    for r in conn.execute("SELECT id, branch FROM compile_run WHERE tenant_id=? AND status='pending_review' AND id IS NOT ?", (tenant_id, keep)).fetchall():
        conn.execute("UPDATE compile_run SET status='stale', decided_at=?, decided_by='compiler' WHERE id=?", (db.iso(), r["id"]))
        if r["branch"]:
            _git(tenant_id, "branch", "-q", "-D", r["branch"], check=False)


def compile_tenant(conn, tenant_id: str, actor: str = "compiler") -> dict:
    """Playbook và hồ sơ thực thể publish ngay; quy ước core và Skill gom thành một đề xuất chờ duyệt duy nhất."""
    with _lock:
        ensure_repo(tenant_id)
        run_id = db.new_id()
        current = read_published(tenant_id)
        new_files, skills = generate(conn, tenant_id)
        changed = _changed(current, new_files)
        if not changed:
            _supersede_pending(conn, tenant_id)  # tri thức đã quay về đúng bản đang publish: đề xuất cũ không còn ý nghĩa
            return _record(conn, tenant_id, run_id, "noop", actor, changed=[], reason="không có thay đổi")

        before = golden_score(conn, tenant_id, current)
        after = golden_score(conn, tenant_id, new_files)
        if before is not None and after is not None and after < before:
            return _record(conn, tenant_id, run_id, "blocked", actor, changed=changed, score_before=before, score_after=after,
                           reason="điểm golden task giảm")

        auto_changed = [p for p in changed if not _needs_review(p)]
        review_changed = [p for p in changed if _needs_review(p)]
        result = None
        if auto_changed:
            # Bản publish ngay: phần không cần duyệt theo bản mới, phần cần duyệt giữ nguyên bản đang publish.
            auto_files = {p: c for p, c in new_files.items() if not _needs_review(p)} | {p: c for p, c in current.items() if _needs_review(p)}
            auto_files.pop("CHANGELOG.md", None)
            sha = _commit_main(tenant_id, auto_files, current, auto_changed, actor, f"compile {run_id[:8]}: tự publish {len(auto_changed)} file")
            result = _record(conn, tenant_id, run_id, "published", actor, changed=auto_changed, commit_sha=sha, decided_at=db.iso(), decided_by="auto",
                             score_before=before, score_after=after)
            current = read_published(tenant_id)
        if not review_changed:
            _supersede_pending(conn, tenant_id)
            return result

        summary = _summary(conn, tenant_id, current, new_files)
        fingerprint = json.dumps({p: new_files.get(p) for p in review_changed}, sort_keys=True, ensure_ascii=False)
        pending = conn.execute("SELECT * FROM compile_run WHERE tenant_id=? AND status='pending_review' ORDER BY created_at DESC LIMIT 1", (tenant_id,)).fetchone()
        # Nội dung cần duyệt không đổi thì giữ đề xuất cũ (kể cả khi main đã đi tiếp: _merge áp riêng file cần duyệt).
        if pending is not None and (pending["summary"] or "").endswith(fingerprint):
            _supersede_pending(conn, tenant_id, keep=pending["id"])
            return result or db.row(pending, "changed")  # đề xuất đang chờ vẫn đúng: không tạo thêm bản trùng

        review_id = db.new_id() if result else run_id
        branch = f"proposal/{review_id[:8]}"
        _git(tenant_id, "checkout", "-q", "-b", branch, "main")
        try:
            files = {p: c for p, c in current.items() if p != "CHANGELOG.md"} | {p: new_files[p] for p in review_changed if p in new_files}
            for p in review_changed:
                if p not in new_files:
                    files.pop(p, None)
            _commit_main(tenant_id, files, current, review_changed, actor, f"compile {review_id[:8]}: đề xuất {len(review_changed)} file")
            diff = _git(tenant_id, "diff", "main", branch, "--", ".", ":(exclude)CHANGELOG.md")
        finally:
            _git(tenant_id, "checkout", "-q", "main")
        _supersede_pending(conn, tenant_id)
        run = _record(conn, tenant_id, review_id, "pending_review", actor, branch=branch, diff=diff, changed=review_changed, needs_review=1,
                      score_before=before, score_after=after, reason=json.dumps(skills, ensure_ascii=False),
                      summary=json.dumps(summary, ensure_ascii=False) + "\n" + fingerprint)
        return run


def retire_rule(conn, tenant_id: str, ku_id: str, actor: str) -> dict:
    """Người duyệt bỏ một quy ước khỏi đề xuất: KU chuyển hết hiệu lực, rồi biên dịch lại để đề xuất mới không còn nó."""
    from . import knowledge
    ku = knowledge.get(conn, tenant_id, ku_id)
    if ku is None or ku["kind"] != "rule":
        raise CompileError("không tìm thấy quy ước", 404)
    if ku["status"] == "active":
        knowledge.update(conn, tenant_id, ku_id, {"status": "deprecated", "valid_to": db.iso()}, "DEPRECATE", actor, "người duyệt bỏ khỏi khối core")
    return compile_tenant(conn, tenant_id, actor)


def _merge(conn, tenant_id: str, run: dict, decided_by: str, skills: list[dict]) -> dict:
    proc = _git_proc(tenant_id, "merge", "-q", "--ff-only", run["branch"])
    if proc.returncode != 0:
        # main đã đi tiếp do playbook tự publish; đề xuất chỉ chạm các file cần duyệt nên áp riêng chúng lên main hiện tại.
        for path in run["changed"]:
            if _git_proc(tenant_id, "cat-file", "-e", f"{run['branch']}:{path}").returncode == 0:
                _git(tenant_id, "checkout", run["branch"], "--", path)
            else:
                _git(tenant_id, "rm", "-q", "--ignore-unmatch", "--", path)
        _git(tenant_id, "add", "-A")
        if _git_proc(tenant_id, "diff", "--cached", "--quiet").returncode != 0:
            _git(tenant_id, "commit", "-q", "-m", f"publish đề xuất {run['id'][:8]} (duyệt bởi {decided_by})")
    if run.get("branch"):
        _git(tenant_id, "branch", "-q", "-D", run["branch"], check=False)
    sha = _git(tenant_id, "rev-parse", "HEAD").strip()
    _publish_skills(conn, tenant_id, skills)
    conn.execute("UPDATE compile_run SET status='published', commit_sha=?, decided_at=?, decided_by=? WHERE id=?", (sha, db.iso(), decided_by, run["id"]))
    return db.row(conn.execute("SELECT * FROM compile_run WHERE id=?", (run["id"],)).fetchone(), "changed")


def decide(conn, tenant_id: str, run_id: str, approve: bool, user_id: str) -> dict:
    with _lock:
        run = db.row(conn.execute("SELECT * FROM compile_run WHERE id=? AND tenant_id=?", (run_id, tenant_id)).fetchone(), "changed")
        if run is None:
            raise CompileError("không tìm thấy lần biên dịch", 404)
        if run["status"] != "pending_review":
            raise CompileError(f"lần biên dịch đang ở trạng thái {run['status']}", 409)
        if not approve:
            conn.execute("UPDATE compile_run SET status='rejected', decided_at=?, decided_by=? WHERE id=?", (db.iso(), user_id, run_id))
            return db.row(conn.execute("SELECT * FROM compile_run WHERE id=?", (run_id,)).fetchone(), "changed")
        skills = json.loads(run["reason"]) if run["reason"] else []
        return _merge(conn, tenant_id, run, user_id, skills)


def rollback(conn, tenant_id: str, actor: str) -> dict:
    """Hoàn tác lần publish gần nhất bằng git revert."""
    with _lock:
        ensure_repo(tenant_id)
        if int(_git(tenant_id, "rev-list", "--count", "HEAD").strip()) < 2:
            raise CompileError("chưa có lần publish nào để hoàn tác", 409)
        _git(tenant_id, "revert", "--no-edit", "HEAD")
        sha = _git(tenant_id, "rev-parse", "HEAD").strip()
        return _record(conn, tenant_id, db.new_id(), "reverted", actor, commit_sha=sha, changed=[], reason="git revert")
