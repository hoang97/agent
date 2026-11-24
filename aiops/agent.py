"""Luồng đọc trong phiên: router → truy xuất → lắp ngữ cảnh theo ngân sách → thực thi qua gateway → ghi episode."""
from __future__ import annotations

import contextvars
import json
import queue
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace

from . import compiler, connections, db, gateway, learned, live, llm, results
from .conversation import (PAGE_DESCRIPTION, PAGE_SCHEMA, PAGE_TOOL, SEARCH_DESCRIPTION, SEARCH_SCHEMA, SEARCH_TOOL, SKILL_TOOL, build_turn, clean_answer,
                           load_history, page_tool, search_tool, skill_files)
from .tools import callable_by


class QuotaExceeded(Exception):
    pass


def tokens_today(conn, tenant_id: str) -> int:
    start = db.now().date().isoformat()
    return conn.execute("SELECT coalesce(sum(tokens),0) t FROM episode WHERE tenant_id=? AND started_at>=?", (tenant_id, start)).fetchone()["t"]


CONTINUE_TASK = ("Người dùng đã quyết định các lệnh chờ duyệt ở lượt trước; kết quả, ghi chú và lý do từ chối nằm trong lịch sử ngay trên. "
                 "Tiếp tục nhiệm vụ trước đó: không gọi lại lệnh đã chạy. Lệnh bị từ chối kèm ghi chú thì làm theo ghi chú; "
                 "nếu ghi chú cho phép chạy sau khi kiểm tra hoặc sửa, làm xong việc đó rồi gọi lại công cụ để có thẻ duyệt mới.")
# Lời agent nói có lệnh chờ duyệt mà lượt này không có lệnh nào chờ: model chép mẫu câu từ lịch sử thay vì gọi công cụ.
PENDING_CLAIM = re.compile(r"(chờ|đợi)\s+(bạn\s+|người dùng\s+|anh/chị\s+)?(phê\s+)?duyệt|awaiting approval|pending approval", re.I)
PENDING_NUDGE = ("[Hệ thống] Câu trả lời vừa rồi nói có lệnh đang chờ duyệt, nhưng lượt này bạn chưa gọi công cụ nào ra thẻ duyệt, "
                 "nên người dùng không có gì để duyệt. Muốn chạy lệnh thì gọi công cụ ngay bây giờ, kể cả lệnh từng bị từ chối khi điều kiện "
                 "người dùng nêu đã thỏa; hệ thống sẽ tạo thẻ duyệt. Không định chạy thì viết một câu ngắn sửa lại, không nói có lệnh chờ duyệt.")
WHY_SCHEMA = {"type": "string", "description": "Một câu ngắn nói lệnh này làm gì và vì sao, hiện trên thẻ duyệt cho người dùng đọc"}
QUEUED_PREFIX = "[Người dùng nhắn thêm trong lúc bạn đang làm; xét ngay, có thể đổi hướng việc đang làm]\n"
ASK_TOOL = "ask_user"
ASK_SCHEMA = {
    "type": "object", "required": ["questions"],
    "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
        "type": "object", "required": ["question", "options"],
        "properties": {
            "question": {"type": "string", "description": "Câu hỏi đầy đủ, đọc riêng vẫn hiểu"},
            "header": {"type": "string", "description": "Nhãn rất ngắn (tối đa 12 ký tự)"},
            "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
                "type": "object", "required": ["label"],
                "properties": {"label": {"type": "string", "description": "1-5 từ"}, "description": {"type": "string", "description": "hệ quả, đánh đổi"}}}},
            "multi_select": {"type": "boolean"}}}}},
}
ASK_DESCRIPTION = ("Hỏi người dùng khi cần họ quyết định mà không tự suy ra được từ yêu cầu, dữ liệu hay mặc định hợp lý "
                   "(chọn giữa các cách xử lý, đối tượng mơ hồ, đánh đổi rủi ro). 1-4 câu hỏi, mỗi câu 2-4 lựa chọn; lựa chọn nên dùng đứng đầu "
                   "và thêm \"(khuyên dùng)\" vào cuối nhãn. Giao diện tự thêm ô \"Khác\" để người dùng tự gõ, đừng tự liệt kê. "
                   "Không dùng để hỏi xin phép chạy lệnh: gọi thẳng công cụ, gateway sẽ hỏi duyệt khi cần. "
                   "Gọi xong thì kết thúc lượt bằng một câu ngắn; câu trả lời đến ở lượt sau.")

DELEGATE_TOOL = "agent.delegate"
DELEGATE_SCHEMA = {
    "type": "object", "required": ["task", "tools"],
    "properties": {
        "task": {"type": "string", "description": "Việc cần làm, tự đủ nghĩa: đối tượng, namespace, môi trường, khoảng thời gian, câu cần trả lời, số liệu cần giữ"},
        "tools": {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string"}, "description": "Tên các công cụ đọc agent con được dùng, chỉ những cái cần"}},
}
DELEGATE_DESCRIPTION = ("Giao một việc tra cứu đơn giản, chỉ đọc, cho agent con chạy bằng model rẻ; chỉ nhận lại bản tóm tắt nên context của bạn không phình vì kết quả thô. "
                        "Dùng khi cần gom dữ liệu từ nhiều nguồn độc lập (pod, metric, log, ticket...): gọi nhiều lần trong cùng một lượt để các agent con chạy song song. "
                        "Agent con không thấy hội thoại, tri thức hay kết quả trước đó, nên task phải tự đủ nghĩa. "
                        "Không giao việc cần lệnh ghi, cần suy luận nhiều bước hay chỉ là một lệnh bạn tự gọi được.")

BACKGROUND_TOOL = "agent.run_background"
BACKGROUND_SCHEMA = {
    "type": "object", "required": ["tool", "args"],
    "properties": {"tool": {"type": "string", "description": "Tên công cụ đọc cần chạy nền"},
                   "args": {"type": "object", "description": "Tham số như khi gọi thẳng công cụ đó"}},
}
BACKGROUND_DESCRIPTION = ("Chạy một lệnh đọc lâu ở nền (kubectl rollout status hoặc wait với timeout_seconds dài, truy vấn chậm, agent.delegate) "
                          "để làm việc khác trong lúc chờ. Trả ngay job_id. Kết quả tự đến sau lệnh kế tiếp của bạn; "
                          "nếu bạn kết thúc lượt khi lệnh nền chưa xong, hệ thống đợi nó xong rồi đưa kết quả để bạn làm tiếp. "
                          "Chỉ dùng khi có việc khác làm song song; lệnh nhanh thì gọi thẳng.")

SKILL_DESCRIPTION = ("Đọc toàn bộ quy trình của một skill trong danh sách <skills>. Gọi khi nhiệm vụ khớp mô tả của skill, "
                     "trước khi bắt tay làm, rồi làm theo các bước trong đó.")


FILTER_SCHEMA = {
    "type": "object",
    "description": "Lọc kết quả ở server trước khi trả về để không phình context: path (danh sách cần lọc), "
                   "where [{field, op, value}] (op: eq ne contains icontains regex gt gte lt lte exists in), fields, group_by, "
                   "sort (\"-field\" để giảm dần), limit, max_chars. Trường lồng nhau dùng dấu chấm, ví dụ source.level.",
    "properties": {"path": {"type": "string"}, "where": {"type": "array", "items": {"type": "object"}}, "fields": {"type": "array", "items": {"type": "string"}},
                   "group_by": {"type": "string"}, "sort": {"type": "string"}, "limit": {"type": "integer"}, "max_chars": {"type": "integer"}},
}


def _with_filter(row) -> dict:
    """Công cụ đọc nhận thêm _filter (công cụ local data.* đã có tham số lọc riêng); công cụ ghi nhận thêm _why cho thẻ duyệt."""
    schema = json.loads(row["input_schema"])
    if row["action_class"] == "read" and row["template"] != connections.BUILTIN[1]:
        schema = {**schema, "properties": {**schema.get("properties", {}), "_filter": FILTER_SCHEMA}}
    elif row["action_class"] != "read":
        schema = {**schema, "properties": {**schema.get("properties", {}), "_why": WHY_SCHEMA}}
    return schema


def _questions(args) -> list[dict] | None:
    """Câu hỏi hợp lệ của ask_user, đã cắt độ dài; None nếu sai dạng."""
    qs = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(qs, list) or not 1 <= len(qs) <= 4:
        return None
    out = []
    for q in qs:
        opts = q.get("options") if isinstance(q, dict) else None
        if not isinstance(q, dict) or not str(q.get("question") or "").strip() or not isinstance(opts, list) or not 2 <= len(opts) <= 4:
            return None
        if not all(isinstance(o, dict) and str(o.get("label") or "").strip() for o in opts):
            return None
        out.append({"question": str(q["question"]).strip()[:500], "header": str(q.get("header") or "").strip()[:20],
                    "multi_select": bool(q.get("multi_select")),
                    "options": [{"label": str(o["label"]).strip()[:80], "description": str(o.get("description") or "").strip()[:300]} for o in opts]})
    return out


def run_task(conn, tenant_id: str, user_id: str, task: str, env: str = "dev", project: str | None = None, agent_id: str = "ops-agent",
             conversation_id: str | None = None, episode_id: str | None = None, on_event=None, cancelled=None,
             continuation_of: str | None = None, read_only: bool = False, inbox=None) -> dict:
    """Chạy một lượt và ghi số token của từng lần gọi LLM vào llm_call để đo chi phí. read_only: agent chỉ thấy công cụ đọc.
    inbox: hàm trả các tin người dùng gửi thêm trong lúc lượt đang chạy."""
    episode_id = episode_id or db.new_id()
    with llm.collect_usage(tenant_id) as usage:
        try:
            return _run_task(conn, tenant_id, user_id, task, env, project, agent_id, conversation_id, episode_id, on_event, cancelled, continuation_of,
                             read_only, inbox)
        finally:
            if usage:
                llm.save_usage(conn, tenant_id, episode_id, usage)


def _run_task(conn, tenant_id: str, user_id: str, task: str, env: str, project: str | None, agent_id: str,
              conversation_id: str | None, episode_id: str, on_event, cancelled, continuation_of: str | None, read_only: bool = False,
              inbox=None) -> dict:
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
    transcript = None
    statuses: list[str] = []
    partial: list[str] = []
    raw_emit = on_event or (lambda event: None)

    def emit(event: dict) -> None:
        """Lệnh do agent con chạy mang số của agent con, để giao diện xếp dưới dòng giao việc."""
        sub = current_sub.get()
        if sub is not None and event.get("type") == "tool_start":
            event = {**event, "sub": sub}
        elif sub is not None and event.get("type") == "tool_end":
            event["step"]["sub"] = sub
        raw_emit(event)
    history = load_history(conn, tenant_id, conversation_id, provider.name)

    live.current_stop.set(cancelled)
    roles = gateway.user_roles(conn, tenant_id, user_id)
    conv = conn.execute("SELECT incident_id FROM conversation WHERE id=?", (conversation_id,)).fetchone() if conversation_id else None
    incident_id = conv["incident_id"] if conv else None
    max_parallel = gateway.get_policy(conn, tenant_id)["max_parallel_tools"]
    ctx = gateway.CallContext(tenant_id, user_id, env, task, episode_id, intent=[*history["intent"], task], calls=list(history["calls"]), roles=roles,
                              incident_id=None if read_only else incident_id)

    def on_text(text: str) -> None:
        partial.append(text)
        emit({"type": "delta", "text": text})

    flushed = {"chars": 0}

    def flush_text() -> None:
        """Đoạn chữ agent viết từ lần trước tới giờ thành một mục của trace, để giao diện xếp chữ và lệnh đúng thứ tự đã xảy ra."""
        text = "".join(partial)
        if text[flushed["chars"]:].strip():
            trace.append({"type": "text", "text": text[flushed["chars"]:]})
        flushed["chars"] = len(text)

    batches = {"n": 0, "subs": 0, "sub_tokens": 0}
    counter = threading.Lock()  # agent con chạy ở nhiều luồng cùng tăng các bộ đếm trên
    current_sub: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_sub", default=None)
    sub_tools: dict[str, dict] = {}  # công cụ đọc agent con được chọn
    read_names: set[str] = set()  # tính sẵn: lệnh nền được tạo từ luồng con, không dùng conn của lượt để tra
    jobs: dict = {"n": 0, "running": {}, "closed": False, "pool": None}
    finished: queue.Queue = queue.Queue()

    allowed: set[str] = set()  # read_only: chỉ công cụ đã cho agent thấy, chặn cả tên model tự bịa ra

    asked: list[dict] = []
    files: dict[str, str] = {}
    skills: dict[str, str] = {}  # tên skill -> đường dẫn SKILL.md trong bản đã publish

    def run_one(c, name: str, args: dict) -> dict:
        if name == ASK_TOOL and not read_only:
            questions = _questions(args)
            step = {"type": "tool_call", "tool": ASK_TOOL, "args": args, "env": env, "decision": "asked",
                    "status": "asked" if questions else "invalid", "questions": questions,
                    "reasons": [] if questions else ["questions phải gồm 1-4 câu, mỗi câu có question và 2-4 lựa chọn có label"],
                    "result": {"note": "Câu hỏi đã hiện cho người dùng kèm nút chọn. Kết thúc lượt bằng một câu ngắn, câu trả lời đến ở lượt sau."}
                    if questions else None}
            if questions:
                asked.append(step)
            emit({"type": "tool_end", "step": step})
            return step
        if asked:
            step = {"type": "tool_call", "tool": name, "args": args, "env": env, "decision": "blocked", "status": "blocked",
                    "reasons": ["đang đợi người dùng trả lời câu hỏi; kết thúc lượt"]}
            emit({"type": "tool_end", "step": step})
            return step
        if name == SKILL_TOOL:
            path = skills.get(str(args.get("name", "")))
            step = {"type": "tool_call", "tool": SKILL_TOOL, "args": args, "env": env, "decision": "allowed",
                    "status": "ok" if path else "invalid", "result": {"content": files[path]} if path else None,
                    "reasons": [] if path else [f"không có skill này; skill hiện có: {', '.join(sorted(skills)) or '(không có)'}"]}
            emit({"type": "tool_end", "step": step})
            return step
        if name == DELEGATE_TOOL:
            return delegate(args)
        if name == BACKGROUND_TOOL:
            return background(args)
        if name == SEARCH_TOOL:
            result, found = search_tool(c, tenant_id, args, files, project=project, user_id=user_id)
            ku_used.extend(i for i in found if i not in ku_used)
            step = {"type": "tool_call", "tool": SEARCH_TOOL, "args": args, "env": env, "decision": "allowed",
                    "status": "invalid" if "error" in result else "ok", "result": result, "reasons": [result["error"]] if "error" in result else []}
            emit({"type": "tool_end", "step": step})
            return step
        if name == PAGE_TOOL:
            result, why = page_tool(files, args)
            step = {"type": "tool_call", "tool": PAGE_TOOL, "args": args, "env": env, "decision": "allowed",
                    "status": "ok" if result else "invalid", "result": result, "reasons": [why] if why else []}
            emit({"type": "tool_end", "step": step})
            return step
        if read_only and name not in allowed:
            step = {"type": "tool_call", "tool": name, "args": args, "env": env, "decision": "blocked", "status": "blocked",
                    "reasons": ["lượt này chỉ được dùng công cụ đọc"]}
            emit({"type": "tool_end", "step": step})
            return step
        local = replace(ctx, roles=set(ctx.roles))
        started = time.monotonic()
        step = gateway.invoke(c, local, name, args)
        step["ms"] = int((time.monotonic() - started) * 1000)
        emit({"type": "tool_end", "step": step})
        return step

    def run_isolated(name: str, args: dict) -> dict:
        # Mỗi lệnh song song dùng kết nối riêng; SQLite tự xếp hàng các giao dịch ghi.
        llm.check_cancelled(cancelled)
        with db.session() as c:
            return run_one(c, name, args)

    def delegate(args: dict) -> dict:
        """Agent con chỉ nhận bản giao việc và các công cụ được chọn; lệnh của nó vẫn qua gateway với ý định gốc của người dùng (ctx)."""
        task_text = str(args.get("task") or "").strip()
        names = list(dict.fromkeys(n for n in args.get("tools") or [] if isinstance(n, str))) if isinstance(args.get("tools"), list) else []
        unknown = [n for n in names if n not in sub_tools]
        if not task_text or not names or unknown:
            why = f"không dùng được cho agent con: {', '.join(unknown)}; " if unknown else "cần task và tools; "
            step = {"type": "tool_call", "tool": DELEGATE_TOOL, "args": args, "env": env, "decision": "allowed", "status": "invalid",
                    "reasons": [why + f"công cụ đọc có thể giao: {', '.join(sorted(sub_tools))}"]}
            emit({"type": "tool_end", "step": step})
            return step
        if "data.query" in sub_tools and "data.query" not in names:
            names.append("data.query")  # kết quả lớn chỉ trả result_id; không có data.query thì agent con không đọc tiếp được
        with counter:
            batches["subs"] += 1
            sub_id = batches["subs"]
        allowed_sub = set(names)

        def sub_call(name: str, a: dict) -> dict:
            return sub_batch(sub_id, allowed_sub, [(name, a)])[0]

        sub_call.batch = lambda calls: sub_batch(sub_id, allowed_sub, calls)
        started = time.monotonic()
        step = {"type": "tool_call", "tool": DELEGATE_TOOL, "args": args, "env": env, "decision": "allowed", "sub_id": sub_id}
        try:
            out = provider.run_subagent(f"Môi trường: {env}\n\n{task_text}", [sub_tools[n] for n in names], sub_call, cancelled=cancelled)
            with counter:
                batches["sub_tokens"] += out.get("tokens", 0)
            step |= {"status": "ok", "result": {"text": out["answer"]}}
        except llm.LLMError as exc:
            step |= {"status": "error", "result": {"error": str(exc)}, "reasons": [f"agent con lỗi: {exc}"]}
        step["ms"] = int((time.monotonic() - started) * 1000)
        step["steps"] = [{"tool": s["tool"], "status": s["status"]} for s in list(trace) if s.get("sub") == sub_id]
        emit({"type": "tool_end", "step": step})
        return step

    def background(args: dict) -> dict:
        inner, inner_args = args.get("tool"), args.get("args")
        if not isinstance(inner, str) or inner == BACKGROUND_TOOL or inner not in read_names or not isinstance(inner_args, dict):
            step = {"type": "tool_call", "tool": BACKGROUND_TOOL, "args": args, "env": env, "decision": "allowed", "status": "invalid",
                    "reasons": [f"chỉ chạy nền được công cụ đọc trong danh sách: {', '.join(sorted(read_names - {BACKGROUND_TOOL}))}"]}
            emit({"type": "tool_end", "step": step})
            return step
        with counter:
            jobs["n"] += 1
            job_id = f"bg{jobs['n']}"
            if jobs["pool"] is None:
                jobs["pool"] = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="bg")
            emit({"type": "tool_start", "tool": inner, "args": inner_args, "background": job_id})
            jobs["running"][job_id] = inner
            jobs["pool"].submit(contextvars.copy_context().run, run_background, job_id, inner, inner_args)
        step = {"type": "tool_call", "tool": BACKGROUND_TOOL, "args": args, "env": env, "decision": "allowed", "status": "ok",
                "result": {"job_id": job_id, "note": "đang chạy nền; kết quả sẽ tự đến"}}
        emit({"type": "tool_end", "step": step})
        return step

    def run_background(job_id: str, name: str, args: dict) -> None:
        try:
            step = run_isolated(name, args)
        except Exception as exc:  # gồm Cancelled: lượt đã dừng thì kết quả không còn ai đọc
            step = {"type": "tool_call", "tool": name, "args": args, "env": env, "decision": "allowed", "status": "error", "result": {"error": str(exc) or "đã dừng"}}
        step["background"] = job_id
        with counter:
            if jobs["closed"]:
                return
        with db.session() as c:
            record(c, [step])
        finished.put((job_id, step))

    def sub_batch(sub_id: int, allowed_sub: set[str], calls: list[tuple[str, dict]]) -> list[dict]:
        """Lệnh của agent con: luôn ở luồng riêng với kết nối riêng, vì luồng đang chạy agent con không được dùng conn của lượt."""
        llm.check_cancelled(cancelled)
        token = current_sub.set(sub_id)
        try:
            calls = [(name, a if isinstance(a, dict) else {}) for name, a in calls]
            for name, a in calls:
                emit({"type": "tool_start", "tool": name, "args": a})

            def run_sub(name: str, a: dict) -> dict:
                if name not in allowed_sub:
                    step = {"type": "tool_call", "tool": name, "args": a, "env": env, "decision": "blocked", "status": "blocked",
                            "reasons": [f"agent con chỉ được dùng: {', '.join(sorted(allowed_sub))}"]}
                    emit({"type": "tool_end", "step": step})
                    return step
                return run_isolated(name, a)

            pool = ThreadPoolExecutor(max_workers=min(max_parallel, len(calls)), thread_name_prefix="subtool")
            try:
                futures = [pool.submit(contextvars.copy_context().run, run_sub, *call) for call in calls]
                steps = wait_or_stop(futures, calls, reads_only=True)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if len(calls) > 1:
                with counter:
                    batches["n"] += 1
                    n = batches["n"]
                for step in steps:
                    step["parallel"] = n
            with db.session() as c:
                record(c, steps)
            return steps
        finally:
            current_sub.reset(token)

    def record(c, steps: list[dict]) -> None:
        for step in steps:
            step["result_chars"] = len(json.dumps(step.get("result"), ensure_ascii=False)) if step.get("result") is not None else 0
            small = results.compact(c, tenant_id, user_id, episode_id, step["tool"], step.get("result"), bool(step.get("untrusted")))
            if small:
                step["result"], step["result_id"] = small, small["result_id"]
            trace.append(step)
            if step["status"] != "asked":
                statuses.append(step["status"])

    def is_read(name: str) -> bool:
        if name in (SKILL_TOOL, SEARCH_TOOL, PAGE_TOOL, DELEGATE_TOOL, BACKGROUND_TOOL):
            return True
        r = conn.execute("SELECT action_class FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant_id, name)).fetchone()
        return bool(r and r["action_class"] == "read")

    def wait_or_stop(futures: list, calls: list[tuple[str, dict]], reads_only: bool = False) -> list[dict]:
        """Đợi các lệnh chạy xong. Bấm dừng thì bỏ ngay nếu chỉ còn lệnh đọc (kết quả bỏ đi, kubectl bị giết);
        còn lệnh ghi đang chạy thì đợi nó xong rồi mới dừng, vì ngắt giữa chừng không hoàn tác được thay đổi đã gửi đi."""
        told = False
        while True:
            pending = [i for i, f in enumerate(futures) if not f.done()]
            if not pending:
                return [f.result() for f in futures]
            if cancelled is not None and cancelled.is_set():
                writes = [] if reads_only else [calls[i][0] for i in pending if not is_read(calls[i][0])]
                if not writes:
                    raise llm.Cancelled()
                if not told:
                    emit({"type": "stopping", "waiting_for": writes})
                    told = True
            wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)

    def batch(calls: list[tuple[str, dict]]) -> list[dict]:
        """Các lệnh model gọi cùng lúc. Lệnh đọc liền nhau chạy song song; mỗi lệnh ghi chạy riêng theo đúng thứ tự,
        để hai lệnh ghi không đè nhau và lệnh đọc không thấy trạng thái giữa chừng của một lệnh ghi."""
        llm.check_cancelled(cancelled)
        flush_text()
        calls = [(name, args if isinstance(args, dict) else {}) for name, args in calls]
        groups: list[tuple[list[tuple[str, dict]], bool]] = []
        for call in calls:
            read = is_read(call[0])
            if read and groups and groups[-1][1]:
                groups[-1][0].append(call)
            else:
                groups.append(([call], read))
        steps: list[dict] = []
        for group, _ in groups:
            steps += run_group(group)
            llm.check_cancelled(cancelled)  # lệnh ghi đã chạy xong và được ghi lại; giờ mới dừng
        return steps

    def run_group(calls: list[tuple[str, dict]]) -> list[dict]:
        for name, args in calls:
            emit({"type": "tool_start", "tool": name, "args": args})
        if cancelled is None and len(calls) == 1:
            steps = [run_one(conn, *calls[0])]
        else:
            if len(calls) > 1:
                with counter:
                    batches["n"] += 1
                    n = batches["n"]
            # Mỗi lệnh chạy ở luồng riêng để nút dừng có tác dụng ngay. Không dùng with: dừng thì không đợi luồng đã bỏ.
            pool = ThreadPoolExecutor(max_workers=min(max_parallel, len(calls)), thread_name_prefix="tool")
            try:
                # copy_context để lần gọi auto mode trong luồng con vẫn được đo vào lượt này.
                futures = [pool.submit(contextvars.copy_context().run, run_isolated, *call) for call in calls]
                steps = wait_or_stop(futures, calls)
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            if len(calls) > 1:
                for step in steps:
                    step["parallel"] = n
        record(conn, steps)
        return steps

    def call_tool(name: str, args: dict) -> dict:
        return batch([(name, args)])[0]

    call_tool.batch = batch
    nudged = {"done": False}

    def review_answer(text: str) -> str | None:
        """Câu kết thúc nói có lệnh chờ duyệt mà lượt này không có: nhắc agent một lần để gọi công cụ hoặc sửa lời."""
        if nudged["done"] or not PENDING_CLAIM.search(text or "") or any(s.get("status") == "pending" for s in trace if s.get("type") == "tool_call"):
            return None
        nudged["done"] = True
        trace.append({"type": "nudge", "reason": "nói có lệnh chờ duyệt nhưng chưa gọi công cụ"})
        return PENDING_NUDGE

    call_tool.review_answer = review_answer

    def drain(final: bool = False) -> str | None:
        """Kết quả lệnh nền đã xong và tin người dùng gửi thêm. final: model định kết thúc lượt, còn lệnh nền thì đợi lệnh xong đầu tiên."""
        texts = [t for t in (inbox() if inbox else []) if t.strip()]
        ready = []
        while True:
            try:
                ready.append(finished.get_nowait())
            except queue.Empty:
                break
        while final and not ready and not texts and jobs["running"]:
            llm.check_cancelled(cancelled)
            try:
                ready.append(finished.get(timeout=0.2))
            except queue.Empty:
                texts = [t for t in (inbox() if inbox else []) if t.strip()]
        if not ready and not texts:
            return None
        flush_text()
        parts = []
        for job_id, step in ready:
            with counter:
                jobs["running"].pop(job_id, None)
            parts.append(f"[Lệnh nền {job_id} ({step['tool']}) đã xong] {llm._tool_payload(step)}")
        for text in texts:
            trace.append({"type": "user_message", "text": text})
            ctx.intent.append(text)  # auto mode xét lệnh sau theo cả lời mới của người dùng
            emit({"type": "user_message", "text": text})
        if texts:
            parts.append(QUEUED_PREFIX + "\n\n".join(texts))
        return "\n\n".join(parts)

    def close_jobs() -> None:
        """Hết lượt: lệnh nền chưa xong bị bỏ (chỉ là lệnh đọc), ghi lại để biết."""
        with counter:
            jobs["closed"] = True
            left = dict(jobs["running"])
        if left:
            trace.append({"type": "background_dropped", "jobs": left})
        if jobs["pool"] is not None:
            jobs["pool"].shutdown(wait=False, cancel_futures=True)

    call_tool.drain = drain

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
            tool_list.append({"tool": ASK_TOOL, "description": ASK_DESCRIPTION, "input_schema": ASK_SCHEMA, "canonical": ASK_TOOL, "action_class": "read"})
        files.update(compiler.read_published(tenant_id))
        skills.update(skill_files(files))
        # Luôn có, kể cả khi kho còn trống: công cụ xuất hiện theo điều kiện thì đổi prefix (mất cache) mỗi lần điều kiện đổi.
        tool_list.append({"tool": SEARCH_TOOL, "description": SEARCH_DESCRIPTION, "action_class": "read", "canonical": SEARCH_TOOL, "input_schema": SEARCH_SCHEMA})
        tool_list.append({"tool": PAGE_TOOL, "description": PAGE_DESCRIPTION, "action_class": "read", "canonical": PAGE_TOOL, "input_schema": PAGE_SCHEMA})
        if skills:
            tool_list.append({"tool": SKILL_TOOL, "description": SKILL_DESCRIPTION, "action_class": "read", "canonical": SKILL_TOOL,
                              "input_schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string", "enum": sorted(skills)}}}})
        sub_tools.update((t["tool"], t) for t in tool_list if t.get("action_class") == "read" and t["tool"] not in (ASK_TOOL, SEARCH_TOOL, PAGE_TOOL, SKILL_TOOL))
        if sub_tools:
            tool_list.append({"tool": DELEGATE_TOOL, "description": DELEGATE_DESCRIPTION, "action_class": "read", "canonical": DELEGATE_TOOL,
                              "input_schema": DELEGATE_SCHEMA})
            tool_list.append({"tool": BACKGROUND_TOOL, "description": BACKGROUND_DESCRIPTION, "action_class": "read", "canonical": BACKGROUND_TOOL,
                              "input_schema": BACKGROUND_SCHEMA})
        read_names.update(t["tool"] for t in tool_list if t.get("action_class") == "read" and t["tool"] != ASK_TOOL)
        allowed.update(t["tool"] for t in tool_list)
        turn = build_turn(conn, tenant_id, task, task_type, history, project=project, user_id=user_id, entities=entities,
                          tool_names=[t["tool"] for t in tool_list], incident_id=incident_id, continuation=bool(continuation_of), files=files)
        ku_used = turn.ku_used
        trace.append({"type": "context", "ku_used": ku_used, **turn.stats})
        emit({"type": "context", "task_type": task_type, "ku_used": len(ku_used)})
        result = provider.run_agent(task, turn.stable, tool_list, call_tool, history=turn.history, turn_context=turn.turn_context,
                                    on_text=on_text if on_event else None, cancelled=cancelled)
        tokens += result.get("tokens", 0) + batches["sub_tokens"]
        if result.get("messages"):
            transcript = {"format": provider.name, "messages": result["messages"]}
        flush_text()
        output = clean_answer(result["answer"]) or "(không có nội dung trả lời)"
        if any(s == "error" for s in statuses):
            outcome, outcome_src = "failure", "metric"
        elif statuses and all(s == "ok" for s in statuses):
            outcome, outcome_src = "success", "metric"
        elif any(s == "ok" for s in statuses):
            outcome, outcome_src = "partial", "metric"
    except llm.Cancelled:
        flush_text()
        trace.append({"type": "cancelled", **({"by": cancelled.by} if getattr(cancelled, "by", None) else {})})
        output = "".join(partial) or "(đã dừng)"
    except llm.LLMError as exc:
        trace.append({"type": "error", "message": str(exc)})
        output = f"Lỗi khi gọi model: {exc}"
        outcome, outcome_src = "failure", "metric"
    finally:
        close_jobs()

    input_doc = {"task": task, "entities": entities, "project": project, **({"continuation_of": continuation_of} if continuation_of else {})}
    conn.execute(
        "INSERT INTO episode(id,tenant_id,user_id,agent_id,scope,task_type,env,started_at,input,trace,output,ku_used,tokens,outcome,outcome_src,conversation_id,"
        "transcript) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, tenant_id, user_id, agent_id, f"project:{project}" if project else "tenant", task_type, env, started,
         json.dumps(input_doc, ensure_ascii=False), json.dumps(trace, ensure_ascii=False), output,
         json.dumps(ku_used), tokens, outcome, outcome_src, conversation_id, json.dumps(transcript, ensure_ascii=False) if transcript else None))
    now = db.iso()
    for ku_id in ku_used:
        conn.execute("UPDATE knowledge_unit SET last_used_at=? WHERE id=? AND tenant_id=?", (now, ku_id, tenant_id))
    if conversation_id:
        conn.execute("UPDATE conversation SET updated_at=? WHERE id=? AND tenant_id=?", (now, conversation_id, tenant_id))
    return get_episode(conn, tenant_id, episode_id)


def get_episode(conn, tenant_id: str, episode_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM episode WHERE id=? AND tenant_id=?", (episode_id, tenant_id)).fetchone()
    return db.row(r, "input", "trace", "ku_used", "human_edit")
