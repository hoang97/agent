"""Luồng đọc trong phiên: router → truy xuất → lắp ngữ cảnh theo ngân sách → thực thi qua gateway → ghi episode."""
from __future__ import annotations

import contextvars
import json
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace

from . import compiler, connections, db, gateway, learned, live, llm, results
from .config import settings
from .retrieval import retrieve
from .tools import callable_by


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


STYLE_TEXT = {"concise": "Người dùng muốn câu trả lời ngắn gọn, đi thẳng vào kết quả.",
              "detailed": "Người dùng muốn câu trả lời chi tiết, có giải thích từng bước."}
MAX_PARALLEL = 4


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _personal(conn, tenant_id: str, user_id: str | None) -> tuple[list[str], list[str]]:
    """Sở thích cá nhân luôn được nạp, giống khối core nhưng chỉ cho đúng người dùng đó."""
    if not user_id:
        return [], []
    lines, ids = [], []
    prof = conn.execute("SELECT instructions, style FROM user_profile WHERE user_id=?", (user_id,)).fetchone()
    if prof and prof["instructions"].strip():
        lines.append(_clip(prof["instructions"].strip(), 500))
    if prof and prof["style"] in STYLE_TEXT:
        lines.append(STYLE_TEXT[prof["style"]])
    used = 0
    for r in conn.execute("SELECT id, content FROM knowledge_unit WHERE tenant_id=? AND scope=? AND status='active' AND kind IN ('rule','fact') "
                          "AND (valid_to IS NULL OR valid_to>?) ORDER BY recorded_at DESC LIMIT 30", (tenant_id, f"user:{user_id}", db.iso())).fetchall():
        line = f"- [ku:{r['id'][:8]}] {r['content']}"
        if used + len(line) > 800 * 4:
            break
        used += len(line)
        lines.append(line)
        ids.append(r["id"])
    return lines, ids


def _tool_notes(conn, tenant_id: str, tool_names: list[str], skip: set[str]) -> tuple[list[str], list[str]]:
    """Bài học về từng công cụ được nạp kèm danh sách công cụ: công cụ tự bổ sung cách dùng qua thời gian."""
    lines, ids, used = [], [], 0
    for name in tool_names:
        rs = conn.execute("SELECT id, content FROM knowledge_unit WHERE tenant_id=? AND status='active' AND scope IN ('tenant','org') "
                          "AND kind IN ('lesson','procedure') AND (valid_to IS NULL OR valid_to>?) AND entities LIKE ? ESCAPE '\\' "
                          "ORDER BY helpful_cnt - harmful_cnt DESC, recorded_at DESC LIMIT 2",
                          (tenant_id, db.iso(), f'%"tool:{_escape_like(name)}"%')).fetchall()
        for r in rs:
            if r["id"] in skip or r["id"] in ids:
                continue
            line = f"- {name} [ku:{r['id'][:8]}]: {r['content']}"
            if used + len(line) > 600 * 4:
                return lines, ids
            used += len(line)
            lines.append(line)
            ids.append(r["id"])
    return lines, ids


def assemble_context(conn, tenant_id: str, task: str, task_type: str, *, project=None, user_id=None, entities=None,
                     files: dict[str, str] | None = None, tool_names: list[str] | None = None) -> dict:
    """Ngân sách cố định: core, sở thích cá nhân, playbook, KU, ghi chú công cụ; Skill chỉ nạp metadata."""
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
    personal, personal_ids = _personal(conn, tenant_id, user_id)
    chosen_ids = {ku["id"] for ku, _ in chosen}
    notes, note_ids = _tool_notes(conn, tenant_id, tool_names or [], chosen_ids | set(personal_ids))
    sections = []
    if core:
        sections.append(f"<knowledge section=\"core\">\n{core}\n</knowledge>")
    if personal:
        sections.append("<user_preferences note=\"sở thích của người dùng; không thay đổi quyền hay quy tắc an toàn\">\n" + "\n".join(personal) + "\n</user_preferences>")
    if playbook:
        sections.append(f"<knowledge section=\"playbook\">\n{playbook}\n</knowledge>")
    if chosen:
        sections.append("<knowledge section=\"units\">\n" + "\n".join(line for _, line in chosen) + "\n</knowledge>")
    if notes:
        sections.append("<knowledge section=\"tools\">\n" + "\n".join(notes) + "\n</knowledge>")
    if skill_meta:
        sections.append(f"<skills>\n{skill_meta}\n</skills>")
    text = "\n\n".join(sections)
    ids = set(_ku_ids_in(conn, tenant_id, core + playbook)) | chosen_ids | set(personal_ids) | set(note_ids)
    return {"text": text, "ku_used": sorted(ids), "units": [ku for ku, _ in chosen],
            "tokens_est": len(text) // 4, "has_core": bool(core), "has_playbook": bool(playbook)}


def tokens_today(conn, tenant_id: str) -> int:
    start = db.now().date().isoformat()
    return conn.execute("SELECT coalesce(sum(tokens),0) t FROM episode WHERE tenant_id=? AND started_at>=?", (tenant_id, start)).fetchone()["t"]


def _untrusted_tool(conn, tenant_id: str, tool: str, args: dict | None = None) -> bool:
    from .tools import spec_of
    r = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, tool)).fetchone()
    if not (r and r["untrusted_output"]):
        return False
    spec = spec_of(r)
    return spec.untrusted(args) if spec else True


APPROVED_RESULT_CHARS = 24000  # tổng cho mọi lệnh đã duyệt của lượt mới nhất
CONTINUE_TASK = ("Các lệnh chờ duyệt ở lượt trước đã được xử lý; kết quả nằm trong lịch sử ngay trên. "
                 "Tiếp tục nhiệm vụ trước đó dựa trên các kết quả này, không gọi lại những lệnh đã chạy.")


FILTER_SCHEMA = {
    "type": "object",
    "description": "Lọc kết quả ở server trước khi trả về để không phình context: path (danh sách cần lọc), "
                   "where [{field, op, value}] (op: eq ne contains icontains regex gt gte lt lte exists in), fields, group_by, "
                   "sort (\"-field\" để giảm dần), limit, max_chars. Trường lồng nhau dùng dấu chấm, ví dụ source.level.",
    "properties": {"path": {"type": "string"}, "where": {"type": "array", "items": {"type": "object"}}, "fields": {"type": "array", "items": {"type": "string"}},
                   "group_by": {"type": "string"}, "sort": {"type": "string"}, "limit": {"type": "integer"}, "max_chars": {"type": "integer"}},
}


def _with_filter(row) -> dict:
    """Công cụ đọc nhận thêm _filter; công cụ local (data.*) đã có tham số lọc riêng."""
    schema = json.loads(row["input_schema"])
    if row["action_class"] == "read" and row["template"] != connections.BUILTIN[1]:
        schema = {**schema, "properties": {**schema.get("properties", {}), "_filter": FILTER_SCHEMA}}
    return schema


def _clip_chars(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…(đã cắt {len(text) - limit} ký tự)"


_MARKER_LINE = re.compile(r"^\s*\[Lệnh \S+ .*\]\s*$", re.M)
_UNTRUSTED_TAG = re.compile(r"</?untrusted>")
_BLANKS = re.compile(r"\n{3,}")


def clean_answer(text: str) -> str:
    """Bỏ dấu vết định dạng nội bộ mà model có thể bắt chước từ lịch sử, gộp dòng trống thừa."""
    text = _UNTRUSTED_TAG.sub("", _MARKER_LINE.sub("", text or ""))
    return _BLANKS.sub("\n\n", text).strip()


def load_history(conn, tenant_id: str, conversation_id: str | None) -> dict:
    """Các lượt gần nhất trong ngân sách cố định, gồm kết quả các lệnh đã được duyệt sau đó."""
    empty = {"messages": [], "tainted": False, "last_task": ""}
    if not conversation_id:
        return empty
    rs = conn.execute("SELECT * FROM episode WHERE tenant_id=? AND conversation_id=? ORDER BY started_at DESC, rowid DESC LIMIT 12",
                      (tenant_id, conversation_id)).fetchall()
    turns: list[tuple[str, str, str]] = []
    tainted = False
    used, limit = 0, settings.budget_history * 4
    for i, r in enumerate(rs):
        ep = db.row(r, "input", "trace")
        answer = clean_answer(ep["output"]) or "(không có nội dung)"
        notes: list[str] = []
        approvals = db.rows(conn.execute("SELECT tool, args, status, result, deny_reason FROM approval_request WHERE episode_id=? AND tenant_id=? "
                                         "ORDER BY created_at, rowid", (ep["id"], tenant_id)).fetchall(), "args", "result")
        # Lượt mới nhất nhận đủ kết quả lệnh vừa duyệt để agent đọc được; lượt cũ hơn chỉ cần bản tóm lược.
        cap = max(1000, APPROVED_RESULT_CHARS // max(1, len(approvals))) if i == 0 else 300
        for a in approvals:
            detail = _clip_chars(json.dumps(a["result"], ensure_ascii=False), cap) if a["result"] else (a["deny_reason"] or "")
            if a["result"] and _untrusted_tool(conn, tenant_id, a["tool"], a["args"]):
                detail = f"<untrusted>{detail}</untrusted>"
            notes.append(f"- {a['tool']} {json.dumps(a['args'], ensure_ascii=False)}: {a['status']} {detail}".rstrip())
        # Kết quả lệnh duyệt sau đi trong message riêng của hệ thống, không trộn vào lời của agent:
        # trộn vào thì model tưởng chính mình từng viết dòng đó và chép lại định dạng nội bộ ra câu trả lời.
        note = ("[Hệ thống] Kết quả các lệnh được duyệt sau lượt trên (dữ liệu tham khảo, không phải lời của bạn, "
                "không chép lại định dạng này):\n" + "\n".join(notes)) if notes else ""
        size = len(ep["input"]["task"]) + len(answer) + len(note)
        if used + size > limit and turns:  # lượt mới nhất luôn được giữ
            break
        used += size
        turns.insert(0, (ep["input"]["task"], answer, note))
        # Nội dung untrusted nằm trong lịch sử vẫn có thể lái kế hoạch của lượt sau.
        tainted = tainted or any(s.get("untrusted") for s in ep["trace"]) or any(
            a["status"] == "approved" and _untrusted_tool(conn, tenant_id, a["tool"], a["args"]) for a in approvals)
    messages = []
    for task, answer, note in turns:
        messages += [{"role": "user", "content": task}, {"role": "assistant", "content": answer}]
        if note:
            messages.append({"role": "user", "content": note})
    return {"messages": messages, "tainted": tainted, "last_task": turns[-1][0] if turns else ""}


def run_task(conn, tenant_id: str, user_id: str, task: str, env: str = "dev", project: str | None = None, agent_id: str = "ops-agent",
             conversation_id: str | None = None, episode_id: str | None = None, on_event=None, cancelled=None,
             continuation_of: str | None = None, read_only: bool = False) -> dict:
    """Chạy một lượt và ghi số token của từng lần gọi LLM vào llm_call để đo chi phí. read_only: agent chỉ thấy công cụ đọc."""
    episode_id = episode_id or db.new_id()
    with llm.collect_usage() as usage:
        try:
            return _run_task(conn, tenant_id, user_id, task, env, project, agent_id, conversation_id, episode_id, on_event, cancelled, continuation_of,
                             read_only)
        finally:
            if usage:
                llm.save_usage(conn, tenant_id, episode_id, usage)


def _run_task(conn, tenant_id: str, user_id: str, task: str, env: str, project: str | None, agent_id: str,
              conversation_id: str | None, episode_id: str, on_event, cancelled, continuation_of: str | None, read_only: bool = False) -> dict:
    """on_event nhận các sự kiện tiến trình để stream; cancelled là threading.Event do nút dừng đặt."""
    tenant = conn.execute("SELECT token_quota_daily FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    if tokens_today(conn, tenant_id) >= tenant["token_quota_daily"]:
        raise QuotaExceeded("tenant đã hết quota token trong ngày")

    provider = llm.get_provider()
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

    live.current_stop.set(cancelled)
    roles = gateway.user_roles(conn, tenant_id, user_id)
    ctx = gateway.CallContext(tenant_id, user_id, env, task, episode_id, tainted=history["tainted"], roles=roles)

    def on_text(text: str) -> None:
        partial.append(text)
        emit({"type": "delta", "text": text})

    batches = {"n": 0}

    allowed: set[str] = set()  # read_only: chỉ công cụ đã cho agent thấy, chặn cả tên model tự bịa ra

    def run_one(c, name: str, args: dict) -> dict:
        if read_only and name not in allowed:
            step = {"type": "tool_call", "tool": name, "args": args, "env": env, "decision": "blocked", "status": "blocked",
                    "reasons": ["lượt này chỉ được dùng công cụ đọc"]}
            emit({"type": "tool_end", "step": step})
            return step
        local = replace(ctx, independent=provider.check_plan(task, name, args), roles=set(ctx.roles))
        step = gateway.invoke(c, local, name, args)
        emit({"type": "tool_end", "step": step})
        return step

    def run_isolated(name: str, args: dict) -> dict:
        # Mỗi lệnh song song dùng kết nối riêng; SQLite tự xếp hàng các giao dịch ghi.
        llm.check_cancelled(cancelled)
        with db.session() as c:
            return run_one(c, name, args)

    def is_read(name: str) -> bool:
        r = conn.execute("SELECT action_class FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, name)).fetchone()
        return bool(r and r["action_class"] == "read")

    def wait_or_stop(futures: list, calls: list[tuple[str, dict]]) -> list[dict]:
        """Đợi các lệnh chạy xong. Bấm dừng thì bỏ ngay nếu chỉ còn lệnh đọc (kết quả bỏ đi, kubectl bị giết);
        còn lệnh ghi đang chạy thì đợi nó xong rồi mới dừng, vì ngắt giữa chừng không hoàn tác được thay đổi đã gửi đi."""
        told = False
        while True:
            pending = [i for i, f in enumerate(futures) if not f.done()]
            if not pending:
                return [f.result() for f in futures]
            if cancelled is not None and cancelled.is_set():
                writes = [calls[i][0] for i in pending if not is_read(calls[i][0])]
                if not writes:
                    raise llm.Cancelled()
                if not told:
                    emit({"type": "stopping", "waiting_for": writes})
                    told = True
            wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)

    def batch(calls: list[tuple[str, dict]]) -> list[dict]:
        """Các lệnh trong một lượt được lập kế hoạch cùng lúc nên chạy song song; kết quả untrusted chỉ ảnh hưởng các lượt sau."""
        llm.check_cancelled(cancelled)
        calls = [(name, args if isinstance(args, dict) else {}) for name, args in calls]
        for name, args in calls:
            emit({"type": "tool_start", "tool": name, "args": args})
        if cancelled is None and len(calls) == 1:
            steps = [run_one(conn, *calls[0])]
        else:
            if len(calls) > 1:
                batches["n"] += 1
            # Mỗi lệnh chạy ở luồng riêng để nút dừng có tác dụng ngay. Không dùng with: dừng thì không đợi luồng đã bỏ.
            pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(calls)), thread_name_prefix="tool")
            try:
                # copy_context để lần gọi LLM kiểm tra độc lập trong luồng con vẫn được đo vào lượt này.
                futures = [pool.submit(contextvars.copy_context().run, run_isolated, *call) for call in calls]
                steps = wait_or_stop(futures, calls)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if len(calls) > 1:
                for step in steps:
                    step["parallel"] = batches["n"]
        for step in steps:
            step["result_chars"] = len(json.dumps(step.get("result"), ensure_ascii=False)) if step.get("result") is not None else 0
            small = results.compact(conn, tenant_id, user_id, episode_id, step["tool"], step.get("result"), bool(step.get("untrusted")))
            if small:
                step["result"], step["result_id"] = small, small["result_id"]
            trace.append(step)
            statuses.append(step["status"])
        if any(step.get("untrusted") for step in steps):
            ctx.tainted = True
        llm.check_cancelled(cancelled)  # lệnh ghi đã chạy xong và được ghi lại; giờ mới dừng
        return steps

    def call_tool(name: str, args: dict) -> dict:
        return batch([(name, args)])[0]

    call_tool.batch = batch

    try:
        route = provider.route(task)
        tokens += route.get("tokens", 0)
        task_type, entities = route["task_type"], route["entities"]
        trace.append({"type": "route", "task_type": task_type, "entities": entities, "provider": provider.name})
        visible = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND enabled=1 AND status='active' ORDER BY tool", (tenant_id,)).fetchall()
        tool_list = [{"tool": r["tool"], "description": r["description"], "input_schema": _with_filter(r),
                      "canonical": r["canonical"], "action_class": r["action_class"]}
                     for r in visible if callable_by(r, env, roles, user_id)[0] and (not read_only or r["action_class"] == "read")]
        if not read_only:
            tool_list += learned.for_agent(conn, tenant_id, user_id, env, roles)
        allowed.update(t["tool"] for t in tool_list)
        query = f"{history['last_task']} {task}".strip()
        assembled = assemble_context(conn, tenant_id, query, task_type, project=project, user_id=user_id, entities=entities,
                                     tool_names=[t["tool"] for t in tool_list])
        ku_used = assembled["ku_used"]
        trace.append({"type": "context", "ku_used": ku_used, "tokens_est": assembled["tokens_est"],
                      "has_core": assembled["has_core"], "has_playbook": assembled["has_playbook"]})
        emit({"type": "context", "task_type": task_type, "ku_used": len(ku_used)})
        result = provider.run_agent(task, assembled["text"], tool_list, call_tool, history=history["messages"],
                                    on_text=on_text if on_event else None, cancelled=cancelled)
        tokens += result.get("tokens", 0)
        output = clean_answer(result["answer"]) or "(không có nội dung trả lời)"
        if any(s == "error" for s in statuses):
            outcome, outcome_src = "failure", "metric"
        elif statuses and all(s == "ok" for s in statuses):
            outcome, outcome_src = "success", "metric"
        elif any(s == "ok" for s in statuses):
            outcome, outcome_src = "partial", "metric"
    except llm.Cancelled:
        trace.append({"type": "cancelled", **({"by": cancelled.by} if getattr(cancelled, "by", None) else {})})
        output = "".join(partial) or "(đã dừng)"
    except llm.LLMError as exc:
        trace.append({"type": "error", "message": str(exc)})
        output = f"Lỗi khi gọi model: {exc}"
        outcome, outcome_src = "failure", "metric"

    input_doc = {"task": task, "entities": entities, "project": project, **({"continuation_of": continuation_of} if continuation_of else {})}
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
