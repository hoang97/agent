"""Luồng đọc trong phiên: router → truy xuất → lắp ngữ cảnh theo ngân sách → thực thi qua gateway → ghi episode."""
from __future__ import annotations

import json
import re

from . import compiler, db, gateway, llm
from .config import settings
from .retrieval import retrieve
from .tools import TOOLS


class QuotaExceeded(Exception):
    pass


def _clip(text: str, budget_tokens: int) -> str:
    limit = budget_tokens * 4
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0]


def _ku_ids_in(conn, tenant_id: str, text: str) -> list[str]:
    out = []
    for short in sorted(set(re.findall(r"\[ku:([0-9a-f]{4,12})", text))):
        r = conn.execute("SELECT id FROM knowledge_unit WHERE tenant_id=? AND id LIKE ?", (tenant_id, short + "%")).fetchone()
        if r:
            out.append(r["id"])
    return out


def assemble_context(conn, tenant_id: str, task: str, task_type: str, *, project=None, user_id=None, entities=None,
                     files: dict[str, str] | None = None) -> dict:
    """Ngân sách cố định: core, playbook, KU; Skill chỉ nạp metadata."""
    files = compiler.read_published(tenant_id) if files is None else files
    core = _clip(files.get("core/rules.md", ""), settings.budget_core)
    playbook = _clip(files.get(f"playbooks/{task_type}.md", ""), settings.budget_playbook)
    skills = [path for path in files if path.startswith("skills/") and path.endswith("SKILL.md")]
    skill_meta = "\n".join(compiler.skill_metadata(files[p]) for p in skills)
    kus = retrieve(conn, tenant_id, task, project=project, user_id=user_id, entities=entities, k=15)
    budget = settings.budget_ku * 4
    chosen, used = [], 0
    for ku in kus:
        line = f"- [ku:{ku['id'][:8]}] ({ku['kind']}) {ku['title']}: {ku['content']}"
        if ku.get("applies_when"):
            line += f" (áp dụng: {ku['applies_when']})"
        if used + len(line) > budget:
            break
        chosen.append((ku, line))
        used += len(line) + 1
    sections = []
    if core:
        sections.append(f"<knowledge section=\"core\">\n{core}\n</knowledge>")
    if playbook:
        sections.append(f"<knowledge section=\"playbook\">\n{playbook}\n</knowledge>")
    if chosen:
        sections.append("<knowledge section=\"units\">\n" + "\n".join(line for _, line in chosen) + "\n</knowledge>")
    if skill_meta:
        sections.append(f"<skills>\n{skill_meta}\n</skills>")
    text = "\n\n".join(sections)
    ids = set(_ku_ids_in(conn, tenant_id, core + playbook)) | {ku["id"] for ku, _ in chosen}
    return {"text": text, "ku_used": sorted(ids), "units": [ku for ku, _ in chosen],
            "tokens_est": len(text) // 4, "has_core": bool(core), "has_playbook": bool(playbook)}


def tokens_today(conn, tenant_id: str) -> int:
    start = db.now().date().isoformat()
    return conn.execute("SELECT coalesce(sum(tokens),0) t FROM episode WHERE tenant_id=? AND started_at>=?", (tenant_id, start)).fetchone()["t"]


def load_history(conn, tenant_id: str, conversation_id: str | None) -> dict:
    """Các lượt gần nhất trong ngân sách cố định, gồm kết quả các lệnh đã được duyệt sau đó."""
    empty = {"messages": [], "tainted": False, "last_task": ""}
    if not conversation_id:
        return empty
    rs = conn.execute("SELECT * FROM episode WHERE tenant_id=? AND conversation_id=? ORDER BY started_at DESC, rowid DESC LIMIT 12",
                      (tenant_id, conversation_id)).fetchall()
    turns: list[tuple[str, str]] = []
    tainted = False
    used, limit = 0, settings.budget_history * 4
    for r in rs:
        ep = db.row(r, "input", "trace")
        answer = ep["output"] or "(không có nội dung)"
        approvals = db.rows(conn.execute("SELECT tool, status, result, deny_reason FROM approval_request WHERE episode_id=? AND tenant_id=?",
                                         (ep["id"], tenant_id)).fetchall(), "result")
        for a in approvals:
            detail = json.dumps(a["result"], ensure_ascii=False)[:300] if a["result"] else (a["deny_reason"] or "")
            answer += f"\n[Lệnh {a['tool']}: {a['status']} {detail}]".rstrip()
        size = len(ep["input"]["task"]) + len(answer)
        if used + size > limit:
            break
        used += size
        turns.insert(0, (ep["input"]["task"], answer))
        # Nội dung untrusted nằm trong lịch sử vẫn có thể lái kế hoạch của lượt sau.
        tainted = tainted or any(s.get("untrusted") for s in ep["trace"]) or any(
            a["status"] == "approved" and a["tool"] in TOOLS and TOOLS[a["tool"]].untrusted_output for a in approvals)
    messages = [m for task, answer in turns for m in ({"role": "user", "content": task}, {"role": "assistant", "content": answer})]
    return {"messages": messages, "tainted": tainted, "last_task": turns[-1][0] if turns else ""}


def run_task(conn, tenant_id: str, user_id: str, task: str, env: str = "dev", project: str | None = None, agent_id: str = "ops-agent",
             conversation_id: str | None = None, episode_id: str | None = None, on_event=None, cancelled=None) -> dict:
    """on_event nhận các sự kiện tiến trình để stream; cancelled là threading.Event do nút dừng đặt."""
    tenant = conn.execute("SELECT token_quota_daily FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    if tokens_today(conn, tenant_id) >= tenant["token_quota_daily"]:
        raise QuotaExceeded("tenant đã hết quota token trong ngày")

    provider = llm.get_provider()
    episode_id = episode_id or db.new_id()
    started = db.iso()
    trace: list[dict] = []
    tokens = 0
    output = None
    ku_used: list[str] = []
    task_type = None
    entities: list[str] = []
    outcome, outcome_src = "unknown", None
    statuses: list[str] = []
    partial: list[str] = []
    emit = on_event or (lambda event: None)
    history = load_history(conn, tenant_id, conversation_id)

    ctx = gateway.CallContext(tenant_id, user_id, env, task, episode_id, tainted=history["tainted"])

    def on_text(text: str) -> None:
        partial.append(text)
        emit({"type": "delta", "text": text})

    def call_tool(name: str, args: dict) -> dict:
        nonlocal tokens
        llm.check_cancelled(cancelled)
        if not isinstance(args, dict):
            args = {}
        emit({"type": "tool_start", "tool": name, "args": args})
        ctx.independent = provider.check_plan(task, name, args)
        step = gateway.invoke(conn, ctx, name, args)
        if step.get("untrusted"):
            ctx.tainted = True
        trace.append(step)
        statuses.append(step["status"])
        emit({"type": "tool_end", "step": step})
        return step

    try:
        route = provider.route(task)
        tokens += route.get("tokens", 0)
        task_type, entities = route["task_type"], route["entities"]
        trace.append({"type": "route", "task_type": task_type, "entities": entities, "provider": provider.name})
        query = f"{history['last_task']} {task}".strip()
        assembled = assemble_context(conn, tenant_id, query, task_type, project=project, user_id=user_id, entities=entities)
        ku_used = assembled["ku_used"]
        trace.append({"type": "context", "ku_used": ku_used, "tokens_est": assembled["tokens_est"],
                      "has_core": assembled["has_core"], "has_playbook": assembled["has_playbook"]})
        emit({"type": "context", "task_type": task_type, "ku_used": len(ku_used)})
        visible = conn.execute("SELECT tool, description, input_schema FROM tool_registry WHERE tenant_id=? AND enabled=1 ORDER BY tool", (tenant_id,)).fetchall()
        tool_list = [{"tool": r["tool"], "description": r["description"], "input_schema": json.loads(r["input_schema"])} for r in visible]
        result = provider.run_agent(task, assembled["text"], tool_list, call_tool, history=history["messages"],
                                    on_text=on_text if on_event else None, cancelled=cancelled)
        tokens += result.get("tokens", 0)
        output = result["answer"]
        if any(s == "error" for s in statuses):
            outcome, outcome_src = "failure", "metric"
        elif statuses and all(s == "ok" for s in statuses):
            outcome, outcome_src = "success", "metric"
        elif any(s == "ok" for s in statuses):
            outcome, outcome_src = "partial", "metric"
    except llm.Cancelled:
        trace.append({"type": "cancelled"})
        output = "".join(partial) or "(đã dừng)"
    except llm.LLMError as exc:
        trace.append({"type": "error", "message": str(exc)})
        output = f"Lỗi khi gọi model: {exc}"
        outcome, outcome_src = "failure", "metric"

    input_doc = {"task": task, "entities": entities, "project": project}
    conn.execute(
        "INSERT INTO episode(id,tenant_id,user_id,agent_id,scope,task_type,env,started_at,input,trace,output,ku_used,tokens,outcome,outcome_src,conversation_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, tenant_id, user_id, agent_id, f"project:{project}" if project else "tenant", task_type, env, started,
         json.dumps(input_doc, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), output,
         json.dumps(ku_used), tokens, outcome, outcome_src, conversation_id))
    now = db.iso()
    for ku_id in ku_used:
        conn.execute("UPDATE knowledge_unit SET last_used_at=? WHERE id=? AND tenant_id=?", (now, ku_id, tenant_id))
    if conversation_id:
        conn.execute("UPDATE conversation SET updated_at=? WHERE id=? AND tenant_id=?", (now, conversation_id, tenant_id))
    return get_episode(conn, tenant_id, episode_id)


def get_episode(conn, tenant_id: str, episode_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM episode WHERE id=? AND tenant_id=?", (episode_id, tenant_id)).fetchone()
    return db.row(r, "input", "trace", "ku_used", "human_edit")
