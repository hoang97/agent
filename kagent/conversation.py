"""Một lượt hội thoại gửi model: lịch sử phát lại, tri thức chọn cho lượt, và cách ghép chúng.

Quy tắc giữ prompt cache: system chỉ gồm phần ổn định theo tenant; tri thức theo câu hỏi đi trong tin nhắn của lượt;
lịch sử phát lại nguyên văn và chỉ nối thêm, không sửa lượt cũ.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from . import compiler, db
from .config import settings
from .retrieval import retrieve

SKILL_TOOL = "read_skill"
SEARCH_TOOL = "search_knowledge"
PAGE_TOOL = "read_knowledge_page"
SEARCH_K = 8
PAGE_CHARS = 12000
SEARCH_DESCRIPTION = ("Tìm thêm trong kho tri thức của nhóm (bài học, sự thật, quy trình đã kiểm chứng, nguyên nhân sự cố cũ) khi tri thức "
                      "gửi kèm lượt chưa đủ, hoặc khi giữa chừng gặp service, lỗi, chủ đề mới. Trả về các KU khớp nhất và tên trang thực thể liên quan.")
PAGE_DESCRIPTION = ("Đọc trang của một thực thể (service, cụm, index, mã lỗi, công cụ): mọi tri thức đang hiệu lực về nó theo loại, "
                    "sự cố đã gặp, thực thể và tri thức liên quan. Tên lấy từ kết quả search_knowledge hoặc trang \"index\" (danh sách mọi trang).")
SEARCH_SCHEMA = {"type": "object", "required": ["query"], "properties": {
    "query": {"type": "string", "description": "Câu hỏi hoặc từ khóa, vd. \"wikijs lỗi migration\""},
    "entities": {"type": "array", "items": {"type": "string"}, "description": "Tên thực thể cụ thể nếu biết, để ưu tiên KU gắn với chúng"}}}
PAGE_SCHEMA = {"type": "object", "required": ["entity"], "properties": {
    "entity": {"type": "string", "description": "Tên thực thể đúng như trong tri thức, hoặc \"index\" để xem danh sách trang"}}}
STYLE_TEXT = {"concise": "Người dùng muốn câu trả lời ngắn gọn, đi thẳng vào kết quả.",
              "detailed": "Người dùng muốn câu trả lời chi tiết, có giải thích từng bước."}
APPROVED_RESULT_CHARS = 24000  # tổng cho mọi lệnh đã duyệt của lượt mới nhất

_MARKER_LINE = re.compile(r"^\s*\[Lệnh \S+ .*\]\s*$", re.M)
_UNTRUSTED_TAG = re.compile(r"</?untrusted>")
_BLANKS = re.compile(r"\n{3,}")
_CONTEXT = re.compile(r"<context note=.*?</context>", re.S)
_PLAYBOOK_TAG = re.compile(r'<knowledge section="playbook" task_type="([^"]+)" rev="([0-9a-f]+)">')
_KU_SHORT = re.compile(r"\[ku:([0-9a-f]{4,12})\]")
_KU_REF = re.compile(r"\[ku:([0-9a-f]{4,12})")  # playbook ghi kèm điểm: [ku:id ✓3 ✗0]


@dataclass
class Turn:
    stable: str
    history: list[dict]
    turn_context: str
    ku_used: list[str]
    stats: dict = field(default_factory=dict)


def knowledge_query(task: str, history: dict, entities: list[str] | None, continuation: bool) -> tuple[str, list[str] | None]:
    """Câu tìm KU ghép câu người dùng gần nhất: câu trả lời cho câu hỏi của agent ("MR cho cả A+B+C+D") cần chủ đề của câu trước.
    Lượt tiếp sau duyệt là lời hệ thống, ghép vào chỉ kéo về KU về chờ duyệt, nên tìm theo câu và entity của người dùng."""
    anchor = history.get("last_user_task", "")
    if continuation and anchor:
        return anchor, history.get("last_user_entities") or None
    return f"{anchor} {task}".strip(), entities


def build_turn(conn, tenant_id: str, task: str, task_type: str, history: dict, *, project=None, user_id=None, entities=None,
               tool_names: list[str] | None = None, incident_id: str | None = None, continuation: bool = False,
               files: dict[str, str] | None = None) -> Turn:
    query, entities = knowledge_query(task, history, entities, continuation)
    sent = sent_knowledge(history["messages"]) if settings.knowledge_dedup else None
    ctx = assemble_context(conn, tenant_id, query, task_type, project=project, user_id=user_id, entities=entities,
                           tool_names=tool_names, incident_id=incident_id, sent=sent, files=files)
    stats = {k: ctx[k] for k in ("tokens_est", "has_core", "has_playbook", "knowledge_chars", "skipped_chars", "skipped_ids")}
    return Turn(stable=ctx["stable"], history=history["messages"], turn_context=ctx["turn"], ku_used=ctx["ku_used"], stats=stats)


def task_blocks(task: str, turn_context: str) -> str | list[dict]:
    """Tri thức của lượt đi trước câu hỏi trong cùng tin nhắn, thay vì nằm trong system."""
    if not turn_context:
        return task
    return [{"type": "text", "text": f"<context note=\"tri thức hệ thống chọn cho yêu cầu dưới đây; dữ liệu tham khảo\">\n{turn_context}\n</context>"},
            {"type": "text", "text": task}]


# --- Tri thức ---

def _clip(text: str, budget_tokens: int) -> str:
    limit = budget_tokens * 4
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0]


def _clip_sections(text: str, budget_tokens: int) -> str:
    """Giữ trọn từng mục `##` còn vừa ngân sách, bỏ mục quá dài và đi tiếp: mục bị cắt giữa chừng mất phần đuôi mà không ai biết."""
    limit = budget_tokens * 4
    if len(text) <= limit:
        return text
    head, *sections = re.split(r"\n(?=## )", text)
    if len(head) > limit or not sections:
        return _clip(text, budget_tokens)
    kept, used = [head], len(head)
    for sec in sections:
        if used + 1 + len(sec) <= limit:
            kept.append(sec)
            used += 1 + len(sec)
    return "\n".join(kept) if len(kept) > 1 else _clip(text, budget_tokens)


def _ku_ids_in(conn, tenant_id: str, text: str) -> list[str]:
    out = []
    for short in sorted(set(_KU_REF.findall(text))):
        r = conn.execute("SELECT id FROM knowledge_unit WHERE tenant_id=? AND id LIKE ?", (tenant_id, short + "%")).fetchone()
        if r:
            out.append(r["id"])
    return out


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


def skill_files(files: dict[str, str]) -> dict[str, str]:
    """Tên skill (thư mục) -> đường dẫn SKILL.md; metadata trong system và công cụ read_skill dùng cùng tên này."""
    return {path.split("/")[1]: path for path in sorted(files) if path.startswith("skills/") and path.endswith("/SKILL.md") and path.count("/") == 2}


def _in(ku_id: str, shorts: set[str]) -> bool:
    return any(ku_id.startswith(short) for short in shorts)


def _tool_notes(conn, tenant_id: str, tool_names: list[str], skip: set[str], in_files: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """Bài học về từng công cụ được nạp kèm danh sách công cụ: công cụ tự bổ sung cách dùng qua thời gian."""
    lines, ids, used = [], [], 0
    for name in tool_names:
        rs = conn.execute("SELECT id, content FROM knowledge_unit WHERE tenant_id=? AND status='active' AND scope IN ('tenant','org') "
                          "AND kind IN ('lesson','procedure') AND (valid_to IS NULL OR valid_to>?) AND entities LIKE ? ESCAPE '\\' "
                          "ORDER BY helpful_cnt - harmful_cnt DESC, recorded_at DESC LIMIT 2",
                          (tenant_id, db.iso(), f'%"tool:{_escape_like(name)}"%')).fetchall()
        for r in rs:
            if r["id"] in skip or r["id"] in ids or _in(r["id"], in_files):
                continue
            line = f"- {name} [ku:{r['id'][:8]}]: {r['content']}"
            if used + len(line) > 600 * 4:
                return lines, ids
            used += len(line)
            lines.append(line)
            ids.append(r["id"])
    return lines, ids


def search_tool(conn, tenant_id: str, args: dict, files: dict[str, str], *, project=None, user_id=None) -> tuple[dict, list[str]]:
    """Kết quả cho search_knowledge và id KU đã trả về (tính vào ku_used như tri thức nạp đầu lượt)."""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "cần query"}, []
    wanted = [e for e in args.get("entities") or [] if isinstance(e, str)][:5]
    kus = retrieve(conn, tenant_id, query, project=project, user_id=user_id, entities=wanted or None, k=SEARCH_K)
    pages = []
    for ku in kus:
        for e in ku.get("entities") or []:
            if e not in pages and compiler.find_page(files, e):
                pages.append(e)
    items = [{"id": f"ku:{ku['id'][:8]}", "kind": ku["kind"], "title": ku["title"], "content": ku["content"],
              **({"applies_when": ku["applies_when"]} if ku.get("applies_when") else {}), "entities": ku.get("entities") or []} for ku in kus]
    return {"results": items, "pages": pages[:10]} if items else {"results": [], "note": "không có tri thức nào khớp"}, [ku["id"] for ku in kus]


def page_tool(files: dict[str, str], args: dict) -> tuple[dict | None, str | None]:
    entity = str(args.get("entity") or "").strip()
    path = "entities/index.md" if entity.lower() == "index" else compiler.find_page(files, entity) if entity else None
    if path and path in files:
        return {"content": _clip_chars(files[path], PAGE_CHARS)}, None
    return None, f"không có trang cho {entity!r}; gọi {PAGE_TOOL} với entity=\"index\" để xem danh sách, hoặc dùng {SEARCH_TOOL}"


def select_knowledge(conn, tenant_id: str, task: str, task_type: str, *, project=None, user_id=None, entities=None,
                     files: dict[str, str] | None = None, tool_names: list[str] | None = None, incident_id: str | None = None) -> dict:
    """Ngân sách cố định: core, sở thích cá nhân, playbook, KU, ghi chú công cụ; Skill chỉ nạp metadata."""
    files = compiler.read_published(tenant_id) if files is None else files
    core = _clip_sections(files.get("core/rules.md", ""), settings.budget_core)
    playbook = _clip_sections(files.get(f"playbooks/{task_type}.md", ""), settings.budget_playbook)
    in_files = set(_KU_REF.findall(core + playbook))
    skill_meta = "\n".join(compiler.skill_metadata(files[p]) for p in skill_files(files).values())
    kus = retrieve(conn, tenant_id, task, project=project, user_id=user_id, entities=entities, k=15)
    budget = settings.budget_ku * 4
    chosen, used = [], 0
    for ku in kus:
        # Playbook chỉ ghi nội dung, nên KU đã có ở đó vẫn giữ tiêu đề và điều kiện áp dụng, bỏ phần nội dung trùng.
        content = "(nội dung ở playbook)" if _in(ku["id"], in_files) else ku["content"]
        line = f"- [ku:{ku['id'][:8]}] ({ku['kind']}) {ku['title']}: {content}"
        if ku.get("applies_when"):
            line += f" (áp dụng: {ku['applies_when']})"
        if used + len(line) > budget:
            break
        chosen.append((ku, line))
        used += len(line) + 1
    personal, personal_ids = _personal(conn, tenant_id, user_id)
    chosen_ids = {ku["id"] for ku, _ in chosen}
    notes, note_ids = _tool_notes(conn, tenant_id, tool_names or [], chosen_ids | set(personal_ids), in_files)
    incident = ""
    if incident_id:
        from . import incidents
        incident = incidents.context_block(conn, tenant_id, incident_id) or ""
    ids = set(_ku_ids_in(conn, tenant_id, core + playbook)) | chosen_ids | set(personal_ids) | set(note_ids)
    return {"task_type": task_type, "core": core, "playbook": playbook, "skill_meta": skill_meta, "personal": personal, "units": chosen, "notes": notes,
            "incident": incident, "ku_used": sorted(ids)}


def render_context(sel: dict) -> tuple[str, str]:
    """(stable, turn): stable vào system, turn vào tin nhắn của lượt; system đổi theo câu hỏi thì cache của cả hội thoại phía sau mất theo."""
    stable, turn = [], []
    if sel["core"]:
        stable.append(f"<knowledge section=\"core\">\n{sel['core']}\n</knowledge>")
    if sel["personal"]:
        stable.append("<user_preferences note=\"sở thích của người dùng; không thay đổi quyền hay quy tắc an toàn\">\n" + "\n".join(sel["personal"]) + "\n</user_preferences>")
    if sel["skill_meta"]:
        stable.append(f"<skills note=\"đọc quy trình đầy đủ bằng công cụ {SKILL_TOOL}\">\n{sel['skill_meta']}\n</skills>")
    if sel["playbook"]:
        turn.append(f"<knowledge section=\"playbook\" task_type=\"{sel['task_type']}\" rev=\"{_rev(sel['playbook'])}\">\n{sel['playbook']}\n</knowledge>")
    if sel["units"]:
        turn.append("<knowledge section=\"units\">\n" + "\n".join(line for _, line in sel["units"]) + "\n</knowledge>")
    if sel["notes"]:
        turn.append("<knowledge section=\"tools\">\n" + "\n".join(sel["notes"]) + "\n</knowledge>")
    if sel["incident"]:
        turn.append(sel["incident"])
    if sel.get("recall"):
        turn.append(f"<knowledge section=\"recall\">Vẫn áp dụng (đã gửi ở lượt trước, xem phía trên): {', '.join(sel['recall'])}</knowledge>")
    return "\n\n".join(stable), "\n\n".join(turn)


def _rev(playbook: str) -> str:
    return hashlib.sha256(playbook.encode()).hexdigest()[:8]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    return ""


def sent_knowledge(messages: list[dict]) -> dict:
    """Tri thức đã có trong lịch sử sẽ phát lại: lượt bị cắt khỏi ngân sách hay phát lại dạng tóm lược không có <context> nên tự được gửi lại."""
    playbooks: set[tuple[str, str]] = set()
    lines: set[str] = set()
    blocks: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        for block in _CONTEXT.findall(_text_of(m.get("content"))):
            blocks.append(block)
            playbooks.update(_PLAYBOOK_TAG.findall(block))
            lines.update(line for line in block.split("\n") if line.startswith("- "))
    return {"playbooks": playbooks, "lines": lines, "text": "\n".join(blocks)}


def drop_sent(sel: dict, sent: dict) -> tuple[dict, int, list[str]]:
    """Bỏ phần model đã thấy trong lịch sử; KU vẫn liên quan được nhắc lại bằng id. ku_used giữ nguyên để Reflector vẫn chấm các KU này."""
    out, skipped, ids, recall = dict(sel), 0, [], []
    if sel["playbook"] and (sel["task_type"], _rev(sel["playbook"])) in sent["playbooks"]:
        out["playbook"] = ""
        skipped += len(sel["playbook"])
        recall.append(f"playbook {sel['task_type']}")
    units = []
    for ku, line in sel["units"]:
        if line in sent["lines"]:
            skipped += len(line)
            ids.append(ku["id"])
            recall.append(f"[ku:{ku['id'][:8]}]")
        else:
            units.append((ku, line))
    out["units"] = units
    notes = []
    for line in sel["notes"]:
        if line in sent["lines"]:
            skipped += len(line)
            recall.extend(f"[ku:{short}]" for short in _KU_SHORT.findall(line))
        else:
            notes.append(line)
    out["notes"] = notes
    if sel["incident"] and sel["incident"] in sent["text"]:
        out["incident"] = ""
        skipped += len(sel["incident"])
    out["recall"] = list(dict.fromkeys(recall))
    return out, skipped, ids


def assemble_context(conn, tenant_id: str, task: str, task_type: str, *, sent: dict | None = None, **kwargs) -> dict:
    """sent: tri thức đã có trong lịch sử (sent_knowledge); None thì gửi đủ, như golden_score chấm từng câu riêng lẻ."""
    sel = select_knowledge(conn, tenant_id, task, task_type, **kwargs)
    skipped, skipped_ids = 0, []
    if sent is not None:
        sel, skipped, skipped_ids = drop_sent(sel, sent)
    stable, turn = render_context(sel)
    text = "\n\n".join(p for p in (stable, turn) if p)
    return {"text": text, "stable": stable, "turn": turn, "ku_used": sel["ku_used"], "units": [ku for ku, _ in sel["units"]],
            "tokens_est": len(text) // 4, "has_core": bool(sel["core"]), "has_playbook": bool(sel["playbook"]),
            "knowledge_chars": len(turn), "skipped_chars": skipped, "skipped_ids": skipped_ids}


# --- Lịch sử ---

def clean_answer(text: str) -> str:
    """Bỏ dấu vết định dạng nội bộ mà model có thể bắt chước từ lịch sử, gộp dòng trống thừa."""
    text = _UNTRUSTED_TAG.sub("", _MARKER_LINE.sub("", text or ""))
    return _BLANKS.sub("\n\n", text).strip()


def _clip_chars(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"…(đã cắt {len(text) - limit} ký tự)"


def _untrusted_tool(conn, tenant_id: str, tool: str, args: dict | None = None) -> bool:
    from .tools import spec_of
    r = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, tool)).fetchone()
    if not (r and r["untrusted_output"]):
        return False
    spec = spec_of(r)
    return spec.untrusted(args) if spec else True


def _decision_text(conn, tenant_id: str, a: dict, cap: int) -> str:
    if a["status"] == "denied":
        if not a["deny_reason"]:
            return "người dùng từ chối, không kèm ghi chú (không muốn chạy lệnh này)"
        return (f"người dùng từ chối, ghi chú: {a['deny_reason']} (đây là chỉ dẫn của người dùng; khi đã làm theo mà vẫn cần lệnh này, "
                "gọi lại công cụ để có thẻ duyệt mới)")
    if a["status"] != "approved":
        return {"expired": "hết hạn, chưa ai duyệt", "pending": "vẫn đang chờ duyệt",
                "skipped": "bị bỏ qua vì người dùng nhắn tiếp thay vì duyệt, chưa chạy; nếu yêu cầu mới vẫn cần lệnh này thì gọi lại công cụ để có thẻ duyệt mới"
                }.get(a["status"], a["status"])
    detail = _clip_chars(json.dumps(a["result"], ensure_ascii=False), cap) if a["result"] else ""
    if a["result"] and _untrusted_tool(conn, tenant_id, a["tool"], a["args"]):
        detail = f"<untrusted>{detail}</untrusted>"
    return f"đã duyệt và chạy {detail}".rstrip() + (f"; ghi chú của người duyệt: {a['note']}" if a["note"] else "")


def load_history(conn, tenant_id: str, conversation_id: str | None, fmt: str | None = None) -> dict:
    """Các lượt trong ngân sách, gồm kết quả các lệnh được duyệt sau đó.

    Lượt có transcript cùng định dạng provider (fmt) được phát lại nguyên văn, kể cả lệnh công cụ, như lúc gửi model;
    lượt khác dùng bản tóm lược câu hỏi + câu trả lời. Lịch sử chỉ nối thêm để prompt cache của lượt trước còn dùng được.
    """
    empty = {"messages": [], "intent": [], "calls": [], "last_task": "", "last_user_task": "", "last_user_entities": []}
    if not conversation_id:
        return empty
    rs = conn.execute("SELECT * FROM episode WHERE tenant_id=? AND conversation_id=? ORDER BY started_at DESC, rowid DESC LIMIT 200",
                      (tenant_id, conversation_id)).fetchall()
    turns: list[dict] = []
    for r in reversed(rs):
        ep = db.row(r, "input", "trace")
        notes: list[str] = []
        approvals = db.rows(conn.execute("SELECT tool, args, status, result, deny_reason, note FROM approval_request WHERE episode_id=? AND tenant_id=? "
                                         "ORDER BY created_at, rowid", (ep["id"], tenant_id)).fetchall(), "args", "result")
        # Cùng giới hạn cho mọi lượt: cắt ngắn hơn khi lượt này cũ đi là sửa lịch sử, mất cache từ đó về sau.
        cap = max(1000, APPROVED_RESULT_CHARS // max(1, len(approvals)))
        for a in approvals:
            notes.append(f"- {a['tool']} {json.dumps(a['args'], ensure_ascii=False)}: {_decision_text(conn, tenant_id, a, cap)}")
        # Kết quả lệnh duyệt sau đi trong message riêng của hệ thống, không trộn vào lời của agent:
        # trộn vào thì model tưởng chính mình từng viết dòng đó và chép lại định dạng nội bộ ra câu trả lời.
        note = ("[Hệ thống] Quyết định của người dùng với các lệnh chờ duyệt ở lượt trên (dữ liệu tham khảo, không phải lời của bạn, "
                "không chép lại định dạng này):\n" + "\n".join(notes)) if notes else ""
        transcript = json.loads(r["transcript"]) if r["transcript"] else None
        if transcript and fmt and transcript.get("format") == fmt and transcript.get("messages"):
            messages = list(transcript["messages"])
        else:
            messages = [{"role": "user", "content": ep["input"]["task"]},
                        {"role": "assistant", "content": clean_answer(ep["output"]) or "(không có nội dung)"}]
        if note:
            messages.append({"role": "user", "content": note})
        # Cho auto mode: lời người dùng và lệnh họ đã duyệt là ý của họ; lệnh agent đã gọi chỉ gồm tên, tham số, trạng thái.
        decided = [f"[người dùng {'đã duyệt' if a['status'] == 'approved' else 'từ chối'} lệnh] {a['tool']} {json.dumps(a['args'], ensure_ascii=False)}"
                   + (f", ghi chú: {a['note'] or a['deny_reason']}" if a["note"] or a["deny_reason"] else "")
                   for a in approvals if a["status"] in ("approved", "denied")]
        turns.append({"task": ep["input"]["task"], "continuation": bool(ep["input"].get("continuation_of")), "entities": ep["input"].get("entities") or [],
                      "messages": messages, "size": len(json.dumps(messages, ensure_ascii=False)),
                      "intent": [ep["input"]["task"], *(s["text"] for s in ep["trace"] if s.get("type") == "user_message"), *decided],
                      "calls": [{"tool": s["tool"], "args": s.get("args"), "status": s.get("status")} for s in ep["trace"] if s.get("type") == "tool_call"]})
    # Vượt ngân sách thì bỏ một mạch lượt cũ tới còn nửa ngân sách, để điểm cắt (đầu prefix) chỉ dời thỉnh thoảng
    # thay vì dời ở mọi lượt như cửa sổ trượt. Lượt mới nhất luôn được giữ.
    start, used, limit = 0, 0, settings.budget_history * 4
    for k, t in enumerate(turns):
        used += t["size"]
        if used > limit:
            while start < k and used > limit // 2:
                used -= turns[start]["size"]
                start += 1
    kept = turns[start:]
    user_turn = next((t for t in reversed(kept) if not t["continuation"]), None)
    return {"messages": [m for t in kept for m in t["messages"]], "intent": [i for t in kept for i in t["intent"]],
            "calls": [c for t in kept for c in t["calls"]], "last_task": kept[-1]["task"] if kept else "",
            "last_user_task": user_turn["task"] if user_turn else "", "last_user_entities": user_turn["entities"] if user_turn else []}
