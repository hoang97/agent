"""L2: sinh playbook, core, hồ sơ thực thể và Skill từ KU active, quản lý bằng git.

LLM không viết lại playbook: Compiler dựng file một cách xác định từ các KU.
Nhánh proposal/* đóng vai trò pull request; golden task chặn publish nếu điểm giảm.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import timedelta
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


_RULE_LINE = re.compile(r"^- \[ku:([0-9a-f]{8})[^\]]*\] (.*)$", re.M)  # nhóm 2 là nội dung


def _needs_review(path: str) -> bool:
    """Chỉ khối core (luôn nạp vào mọi lượt) và Skill cần người duyệt; playbook và hồ sơ thực thể tự publish."""
    return path == "core/rules.md" or path.startswith("skills/")


def _summary(conn, tenant_id: str, current: dict[str, str], proposed: dict[str, str]) -> dict:
    """Thay đổi của phần cần duyệt dưới dạng danh sách quy ước thêm/bỏ và Skill, thay cho git diff có mã KU."""
    def rules(text: str) -> dict[str, str]:
        return {m.group(1): m.group(2) for m in _RULE_LINE.finditer(text or "")}
    old, new = rules(current.get("core/rules.md")), rules(proposed.get("core/rules.md"))

    policy = autopublish_policy(conn, tenant_id)

    def info(short: str, content: str, pending: bool = False) -> dict:
        r = conn.execute("SELECT * FROM knowledge_unit WHERE tenant_id=? AND id LIKE ?", (tenant_id, short + "%")).fetchone()
        ku = db.row(r, "entities", "source_eps") if r else None
        out = {"ku": ku["id"] if ku else short, "title": ku["title"] if ku else "", "content": content,
               "source_trust": ku["source_trust"] if ku else None, "episodes": len(ku["source_eps"]) if ku else 0}
        if pending and ku:
            out["why_review"] = rule_eligible(conn, tenant_id, ku, policy)[1]  # vì sao không tự publish
        return out
    skills = sorted({p.split("/")[1] for p in set(current) | set(proposed) if p.startswith("skills/") and current.get(p) != proposed.get(p)})
    return {"rules_added": [info(k, v, True) for k, v in new.items() if k not in old],
            "rules_changed": [info(k, v, True) for k, v in new.items() if k in old and old[k] != v],
            "rules_removed": [info(k, v) for k, v in old.items() if k not in new],
            "rules_kept": sum(1 for k in new if k in old and old[k] == new[k]), "skills": skills}


# ---------- tự publish quy ước core ----------

AUTOPUBLISH_MODES = ("off", "a", "ab")
DEFAULT_AUTOPUBLISH = {"mode": "ab", "min_episodes": 3, "min_helpful": 3, "min_days": 7}
WATCH_DAYS = 14  # quy ước tự publish bị tính có hại trong khoảng này thì rút khỏi core, quay về chờ duyệt
# Quy ước nói về cơ chế của chính agent/hệ thống không bao giờ tự vào core: hệ thống đã biết, và dễ lệch với chỉ dẫn gốc.
_INTERNAL = re.compile(r"gateway|approval|phê duyệt|duyệt lệnh|chờ duyệt|pending|data\.(query|script|propose_tool)|propose_tool|_filter|result_id|"
                       r"công cụ học được|learned\.|tool call|tool_call|system prompt|reflector|curator", re.I)


def check_autopublish(policy) -> dict:
    if not isinstance(policy, dict) or set(policy) - set(DEFAULT_AUTOPUBLISH):
        raise ValueError(f"chỉ gồm {sorted(DEFAULT_AUTOPUBLISH)}")
    merged = {**DEFAULT_AUTOPUBLISH, **policy}
    if merged["mode"] not in AUTOPUBLISH_MODES:
        raise ValueError(f"mode phải thuộc {list(AUTOPUBLISH_MODES)}")
    for k, low, high in (("min_episodes", 1, 100), ("min_helpful", 0, 1000), ("min_days", 0, 365)):
        if isinstance(merged[k], bool) or not isinstance(merged[k], int) or not low <= merged[k] <= high:
            raise ValueError(f"{k} phải là số nguyên {low}-{high}")
    return merged


def autopublish_policy(conn, tenant_id: str) -> dict:
    r = conn.execute("SELECT rule_autopublish FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    try:
        return check_autopublish(json.loads(r["rule_autopublish"]) if r and r["rule_autopublish"] else {})
    except (ValueError, TypeError):
        return dict(DEFAULT_AUTOPUBLISH)


def _is_tenant_admin(conn, tenant_id: str, user_id: str | None = None, username: str | None = None) -> bool:
    if username:
        u = conn.execute("SELECT id FROM app_user WHERE username=?", (username,)).fetchone()
        user_id = u["id"] if u else None
    if not user_id:
        return False
    if conn.execute("SELECT 1 FROM app_user WHERE id=? AND is_org_admin=1", (user_id,)).fetchone():
        return True
    return conn.execute("SELECT 1 FROM membership WHERE tenant_id=? AND user_id=? AND role='tenant_admin'", (tenant_id, user_id)).fetchone() is not None


def _events(conn, tenant_id: str, ku_id: str, op: str) -> list:
    return conn.execute("SELECT actor, at FROM ku_event WHERE tenant_id=? AND ku_id=? AND op=? ORDER BY id", (tenant_id, ku_id, op)).fetchall()


def withdrawn(conn, tenant_id: str, ku: dict) -> bool:
    """Tự publish gần đây (trong WATCH_DAYS) rồi bị tính có hại: rút khỏi core, chờ người duyệt."""
    pub = _events(conn, tenant_id, ku["id"], "AUTOPUBLISH")
    return bool(pub) and ku["harmful_cnt"] > 0 and pub[-1]["at"] >= db.iso(db.now() - timedelta(days=WATCH_DAYS))


def rule_eligible(conn, tenant_id: str, ku: dict, policy: dict) -> tuple[bool, str]:
    """Quy ước có được tự vào core không, kèm lý do (hiện cho người duyệt)."""
    if policy["mode"] == "off":
        return False, "tenant tắt tự publish quy ước"
    if ku["source_trust"] == "external_untrusted":
        return False, "nguồn là nội dung ngoài"
    if withdrawn(conn, tenant_id, ku):
        return False, "đã tự publish nhưng bị tính có hại"
    if _INTERNAL.search(f"{ku['title']} {ku['content']}"):
        return False, "nói về cơ chế của agent/hệ thống"
    if conn.execute("SELECT 1 FROM ku_edge e JOIN knowledge_unit k ON k.id=e.dst WHERE e.tenant_id=? AND e.src=? AND e.rel='contradicts' AND k.status='active'",
                    (tenant_id, ku["id"])).fetchone():
        return False, "mâu thuẫn với tri thức đang hiệu lực"
    from .retrieval import near
    dup = [n for n in near(conn, tenant_id, f"{ku['title']} {ku['content']}", "rule", settings.confirm_threshold, scope=ku["scope"])
           if n["id"] != ku["id"] and n["status"] == "active"]
    if dup:
        return False, "trùng ý với quy ước khác đang hiệu lực"
    # Đường A: người có quyền tenant_admin nói thẳng (trong chat, hoặc thêm thủ công).
    if ku["source_trust"] == "user":
        speakers = [r["user_id"] for r in conn.execute(
            f"SELECT user_id FROM episode WHERE tenant_id=? AND id IN ({','.join('?' * len(ku['source_eps']))})", (tenant_id, *ku["source_eps"])).fetchall()] \
            if ku["source_eps"] else []
        added_by = [r["actor"] for r in _events(conn, tenant_id, ku["id"], "ADD")]
        if any(_is_tenant_admin(conn, tenant_id, user_id=u) for u in speakers) or any(_is_tenant_admin(conn, tenant_id, username=a) for a in added_by):
            return True, "tenant_admin nói thẳng"
        if policy["mode"] == "a":
            return False, "người nói không phải tenant_admin"
    if policy["mode"] != "ab":
        return False, "chỉ bật đường người có quyền nói thẳng"
    # Đường B: đã kiểm chứng qua sử dụng.
    promoted = _events(conn, tenant_id, ku["id"], "PROMOTE")
    since = promoted[-1]["at"] if promoted else ku["recorded_at"]
    missing = []
    if len(set(ku["source_eps"])) < policy["min_episodes"]:
        missing.append(f"{len(set(ku['source_eps']))}/{policy['min_episodes']} lượt xác nhận")
    if ku["helpful_cnt"] < policy["min_helpful"]:
        missing.append(f"{ku['helpful_cnt']}/{policy['min_helpful']} lần hữu ích")
    if ku["harmful_cnt"] > 0:
        missing.append(f"{ku['harmful_cnt']} lần có hại")
    if since > db.iso(db.now() - timedelta(days=policy["min_days"])):
        missing.append(f"hiệu lực chưa đủ {policy['min_days']} ngày")
    return (False, "chưa đủ: " + ", ".join(missing)) if missing else (True, "đã kiểm chứng qua sử dụng")


def _auto_core(conn, tenant_id: str, published: str | None, proposed: str | None) -> tuple[str | None, list[dict]]:
    """Khối core sẽ tự publish: bỏ quy ước không còn hiệu lực, thêm quy ước đủ điều kiện; phần còn lại chờ duyệt.

    Trả về (nội dung core, các KU vừa được tự thêm). Quy ước đang có mà nội dung bị sửa thì giữ bản cũ cho tới khi đủ điều kiện hoặc được duyệt.
    """
    policy = autopublish_policy(conn, tenant_id)
    old = {m.group(1): m.group(0) for m in _RULE_LINE.finditer(published or "")}
    old_text = {m.group(1): m.group(2) for m in _RULE_LINE.finditer(published or "")}
    new = [(m.group(1), m.group(0), m.group(2)) for m in _RULE_LINE.finditer(proposed or "")]
    lines, added, used = [], [], 0
    for short, line, text in new:
        ku = conn.execute("SELECT * FROM knowledge_unit WHERE tenant_id=? AND id LIKE ?", (tenant_id, short + "%")).fetchone()
        ku = db.row(ku, "entities", "source_eps") if ku else None
        if ku is None:
            continue
        if short in old and withdrawn(conn, tenant_id, ku):
            continue  # rút khỏi core
        if short in old and old_text[short] == text:
            keep = line  # nội dung không đổi: chỉ bộ đếm ✓/✗ cập nhật, không cần duyệt
        else:
            ok, _ = rule_eligible(conn, tenant_id, ku, policy)
            keep = line if ok else old.get(short)
            if ok:
                added.append(ku)
        if keep is None:
            continue
        if used + len(keep) > settings.budget_core * 4:
            if keep != old.get(short):
                added = [a for a in added if a["id"] != ku["id"]]  # vượt ngân sách core: để người duyệt chọn
                keep = old.get(short)
            if keep is None:
                continue
        lines.append(keep)
        used += len(keep)
    # Quy ước đã publish mà không còn trong bản mới (hết hiệu lực ở L1) thì tự gỡ.
    if not lines:
        return None, added
    return "# Quy ước (luôn nạp)\n\n" + "\n".join(lines) + "\n", added


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

        # Bản publish ngay: playbook, hồ sơ thực thể theo bản mới; core gồm quy ước đủ điều kiện tự publish; Skill giữ bản đang publish.
        auto_files = {p: c for p, c in new_files.items() if not _needs_review(p)} | {p: c for p, c in current.items() if p.startswith("skills/")}
        core, auto_rules = _auto_core(conn, tenant_id, current.get("core/rules.md"), new_files.get("core/rules.md"))
        if core:
            auto_files["core/rules.md"] = core
        auto_changed = _changed(current, auto_files)
        result = None
        if auto_changed:
            sha = _commit_main(tenant_id, auto_files, current, auto_changed, actor, f"compile {run_id[:8]}: tự publish {len(auto_changed)} file")
            result = _record(conn, tenant_id, run_id, "published", actor, changed=auto_changed, commit_sha=sha, decided_at=db.iso(), decided_by="auto",
                             score_before=before, score_after=after,
                             summary=json.dumps({"rules_auto": [{"ku": k["id"], "content": k["content"]} for k in auto_rules]}, ensure_ascii=False) if auto_rules else None)
            for k in auto_rules:
                conn.execute("INSERT INTO ku_event(tenant_id,ku_id,op,actor,reason,at) VALUES (?,?,?,?,?,?)",
                             (tenant_id, k["id"], "AUTOPUBLISH", "compiler", "tự publish vào khối core", db.iso()))
            current = read_published(tenant_id)
        review_changed = _changed(current, new_files)
        if not review_changed:
            _supersede_pending(conn, tenant_id)
            return result

        summary = _summary(conn, tenant_id, current, new_files)
        fingerprint = json.dumps({p: new_files.get(p) for p in review_changed}, sort_keys=True, ensure_ascii=False)
        pending = conn.execute("SELECT * FROM compile_run WHERE tenant_id=? AND status='pending_review' ORDER BY created_at DESC LIMIT 1", (tenant_id,)).fetchone()
        # Nội dung cần duyệt không đổi thì giữ đề xuất cũ (kể cả khi main đã đi tiếp: _merge áp riêng file cần duyệt).
        if pending is not None and (pending["summary"] or "").endswith(fingerprint):
            _supersede_pending(conn, tenant_id, keep=pending["id"])
            # Lý do chờ duyệt đổi theo thời gian (số ngày, số lần hữu ích): làm mới tóm tắt.
            conn.execute("UPDATE compile_run SET summary=? WHERE id=?", (json.dumps(summary, ensure_ascii=False) + "\n" + fingerprint, pending["id"]))
            pending = conn.execute("SELECT * FROM compile_run WHERE id=?", (pending["id"],)).fetchone()
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
