"""Ghép một lượt hội thoại: lịch sử, tri thức theo lượt, số đo tri thức trong trace."""
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops import conversation, db, llm

ROOT = Path(__file__).resolve().parent.parent


def _measure_module():
    spec = importlib.util.spec_from_file_location("measure_context", ROOT / "scripts" / "measure_context.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_turn_matches_assembled_context(users):
    alice = users["alice"]
    alice.run("xem pod trong namespace default")
    with db.session() as conn:
        history = {"messages": [{"role": "user", "content": "trước"}], "last_task": "xem pod", "intent": [], "calls": []}
        turn = conversation.build_turn(conn, alice.tenant, "xem log", "k8s_ops", history)
        ctx = conversation.assemble_context(conn, alice.tenant, "xem pod xem log", "k8s_ops")
    assert turn.stable == ctx["stable"] and turn.turn_context == ctx["turn"] and turn.ku_used == ctx["ku_used"]
    assert turn.history is history["messages"]
    assert turn.stats["knowledge_chars"] == len(ctx["turn"]) and turn.stats["skipped_chars"] == 0 and turn.stats["skipped_ids"] == []


def test_context_text_joins_stable_and_turn(users):
    with db.session() as conn:
        ctx = conversation.assemble_context(conn, users["alice"].tenant, "xem pod", "k8s_ops")
    assert ctx["text"] == "\n\n".join(p for p in (ctx["stable"], ctx["turn"]) if p)


def test_trace_records_knowledge_size(users):
    ep = users["alice"].run("xem pod trong namespace default")
    step = next(s for s in ep["trace"] if s["type"] == "context")
    assert {"knowledge_chars", "skipped_chars", "skipped_ids", "tokens_est", "has_core", "has_playbook"} <= step.keys()


def test_task_blocks_put_context_before_task():
    assert conversation.task_blocks("hỏi", "") == "hỏi"
    blocks = conversation.task_blocks("hỏi", "tri thức")
    assert blocks[0]["text"].startswith("<context") and "tri thức" in blocks[0]["text"] and blocks[1] == {"type": "text", "text": "hỏi"}


def test_measure_counts_knowledge_repeated_across_turns(tmp_path):
    path = tmp_path / "m.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE episode(conversation_id TEXT, transcript TEXT, started_at TEXT)")

    def turn(lines, task):
        ctx = conversation.task_blocks(task, "<knowledge section=\"units\">\n" + "\n".join(lines) + "\n</knowledge>")
        return json.dumps({"format": "anthropic", "messages": [{"role": "user", "content": ctx}, {"role": "assistant", "content": "ok"}]})

    conn.executemany("INSERT INTO episode VALUES (?,?,?)", [("c1", turn(["- [ku:aaaa] một", "- [ku:bbbb] hai"], "a"), "1"),
                                                            ("c1", turn(["- [ku:aaaa] một", "- [ku:cccc] ba"], "b"), "2"),
                                                            ("c2", None, "3")])
    conn.commit()
    convs = {c["conversation_id"]: c for c in _measure_module().measure(sqlite3.connect(f"file:{path}?mode=ro", uri=True))}
    c1 = convs["c1"]
    assert c1["turns"] == 2 and c1["repeated_chars"] == len("- [ku:aaaa] một")
    second = len("- [ku:aaaa] một") + len("- [ku:cccc] ba")
    assert [t["repeated_pct"] for t in c1["per_turn"]] == [0, round(100 * len("- [ku:aaaa] một") / second)]
    assert convs["c2"]["knowledge_chars"] == 0


# --- Không gửi lại tri thức đã có trong lịch sử ---

PLAYBOOK = "# k8s_ops\n\n## Cạm bẫy\n- [ku:feed0001] ImagePullBackOff: kiểm tra registry trước"


def _ku(n, content):
    return {"id": f"{n:08x}" + "0" * 24, "kind": "lesson", "title": f"KU {n}", "content": content, "applies_when": ""}


class Recording(llm.HeuristicProvider):
    """Ghi lại đúng những gì gửi model và trả transcript như provider thật, để lượt sau phát lại nguyên văn."""
    name = "recording"

    def __init__(self):
        self.calls = []

    def route(self, task):
        return {"task_type": "observability" if "metric" in task else "k8s_ops", "entities": [], "tokens": 0}

    def run_agent(self, task, context, tools, call_tool, history=None, on_text=None, cancelled=None, turn_context=""):
        self.calls.append({"history": json.loads(json.dumps(history or [])), "turn": turn_context})
        user = {"role": "user", "content": conversation.task_blocks(task, turn_context)}
        return {"answer": "xong", "tokens": 0, "messages": [user, {"role": "assistant", "content": [{"type": "text", "text": "xong"}]}]}


@pytest.fixture
def knowledge(monkeypatch):
    state = {"files": {"playbooks/k8s_ops.md": PLAYBOOK, "playbooks/observability.md": "# obs\n- [ku:feed0002] dùng aggregation"},
             "kus": [_ku(1, "pod log 404 do ReplicaSet mới"), _ku(2, "0 hit không chứng minh là không có lỗi")]}
    monkeypatch.setattr(conversation.compiler, "read_published", lambda tenant_id: dict(state["files"]))
    monkeypatch.setattr(conversation, "retrieve", lambda *a, **k: list(state["kus"]))
    provider = Recording()
    llm.set_provider(provider)
    return SimpleNamespace(state=state, provider=provider)


def _chat(user, message, cid=None):
    r = user.post("/api/chat", json={"message": message, "conversation_id": cid, "env": "dev"})
    assert r.status_code == 200, r.text
    evs = [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]
    return next(e for e in evs if e["type"] == "done")["message"], evs[0]["conversation_id"]


def test_second_turn_sends_only_recall(users, knowledge):
    alice = users["alice"]
    first, cid = _chat(alice, "xem pod")
    second, _ = _chat(alice, "xem pod lần nữa", cid)
    t1, t2 = (c["turn"] for c in knowledge.provider.calls)
    assert 'section="playbook" task_type="k8s_ops"' in t1 and "pod log 404" in t1
    assert "playbook" not in t2.split('section="recall"')[0] and "pod log 404" not in t2
    assert "playbook k8s_ops" in t2 and "[ku:00000001]" in t2 and "[ku:00000002]" in t2
    assert first["ku_used"] == second["ku_used"]
    ctx = next(s for s in conversation_trace(second) if s["type"] == "context")
    assert ctx["skipped_chars"] > 0 and len(ctx["skipped_ids"]) == 2


def conversation_trace(message):
    with db.session() as conn:
        return json.loads(conn.execute("SELECT trace FROM episode WHERE id=?", (message["id"],)).fetchone()[0])


def test_history_replayed_byte_for_byte(users, knowledge):
    alice = users["alice"]
    _, cid = _chat(alice, "xem pod")
    _chat(alice, "xem pod lần nữa", cid)
    _chat(alice, "xem pod lần ba", cid)
    calls = knowledge.provider.calls
    assert calls[2]["history"][:len(calls[1]["history"])] == calls[1]["history"]
    assert calls[1]["history"][0]["content"][0]["text"].endswith(f"{calls[0]['turn']}\n</context>")


def test_new_knowledge_still_sent(users, knowledge):
    alice = users["alice"]
    _, cid = _chat(alice, "xem pod")
    knowledge.state["kus"].append(_ku(3, "ImagePullBackOff lan nhiều namespace"))
    knowledge.state["kus"][0] = {**knowledge.state["kus"][0], "content": "pod log 404: lấy pod mới từ ReplicaSet"}
    _chat(alice, "xem pod tiếp", cid)
    t2 = knowledge.provider.calls[1]["turn"]
    assert "ImagePullBackOff lan nhiều namespace" in t2 and "lấy pod mới từ ReplicaSet" in t2
    assert "0 hit không chứng minh" not in t2 and "[ku:00000002]" in t2


def test_playbook_resent_on_task_type_or_revision_change(users, knowledge):
    alice = users["alice"]
    _, cid = _chat(alice, "xem pod")
    _chat(alice, "xem metric cpu", cid)
    assert 'task_type="observability"' in knowledge.provider.calls[1]["turn"]
    knowledge.state["files"]["playbooks/k8s_ops.md"] = PLAYBOOK + "\n- [ku:feed0003] mục mới"
    _chat(alice, "xem pod", cid)
    assert "mục mới" in knowledge.provider.calls[2]["turn"]


def test_turn_without_context_in_history_gets_full_knowledge(users, knowledge):
    """Lượt phát lại dạng tóm lược (khác provider) hoặc đã bị cắt khỏi ngân sách thì không có <context>: gửi đủ."""
    alice = users["alice"]
    _, cid = _chat(alice, "xem pod")
    with db.session() as conn:
        conn.execute("UPDATE episode SET transcript=NULL WHERE conversation_id=?", (cid,))
    _chat(alice, "xem pod lần nữa", cid)
    t2 = knowledge.provider.calls[1]["turn"]
    assert 'section="playbook"' in t2 and "pod log 404" in t2 and 'section="recall"' not in t2


def test_dedup_can_be_turned_off(users, knowledge, monkeypatch):
    monkeypatch.setattr(conversation.settings, "knowledge_dedup", False)
    alice = users["alice"]
    _, cid = _chat(alice, "xem pod")
    _chat(alice, "xem pod lần nữa", cid)
    t1, t2 = (c["turn"] for c in knowledge.provider.calls)
    assert t1 == t2


def test_drop_sent_reads_both_provider_formats():
    sel = {"task_type": "k8s_ops", "core": "", "playbook": PLAYBOOK, "skill_meta": "", "personal": [], "notes": [], "incident": "",
           "units": [(_ku(1, "a"), "- [ku:00000001] (lesson) KU 1: a")], "ku_used": []}
    stable, turn = conversation.render_context(sel)
    sent = conversation.sent_knowledge([{"role": "user", "content": conversation.task_blocks("q", turn)}])
    dropped, skipped, ids = conversation.drop_sent(sel, sent)
    assert dropped["playbook"] == "" and dropped["units"] == [] and ids == [_ku(1, "a")["id"]] and skipped > 0
    assert conversation.drop_sent(sel, conversation.sent_knowledge([]))[0]["units"] == sel["units"]
    joined = "\n\n".join(b["text"] for b in conversation.task_blocks("q", turn))  # định dạng OpenAI: content là chuỗi
    assert conversation.drop_sent(sel, conversation.sent_knowledge([{"role": "user", "content": joined}]))[0]["units"] == []
    tool_result = [{"type": "tool_result", "tool_use_id": "x", "content": joined}]  # kết quả công cụ không phải tri thức đã gửi
    assert conversation.drop_sent(sel, conversation.sent_knowledge([{"role": "user", "content": tool_result}]))[0]["units"] == sel["units"]


# --- Bỏ trùng trong cùng lượt, cắt theo mục, câu tìm KU ---

def test_ku_in_playbook_not_repeated_in_units(users, knowledge):
    knowledge.state["files"]["playbooks/k8s_ops.md"] = PLAYBOOK + "\n- [ku:00000001 ✓2 ✗0] pod log 404 do ReplicaSet mới"
    first, _ = _chat(users["alice"], "xem pod")
    t1 = knowledge.provider.calls[0]["turn"]
    units = t1.split('section="units"')[1]
    assert "- [ku:00000001] (lesson) KU 1: (nội dung ở playbook)" in units and "0 hit không chứng minh" in units
    assert t1.count("pod log 404 do ReplicaSet mới") == 1
    ctx = next(s for s in conversation_trace(first) if s["type"] == "context")
    assert _ku(1, "")["id"] in ctx["ku_used"]


def test_clip_sections_keeps_whole_sections():
    text = "# t\n\n## A\n" + "a" * 30 + "\n## B\n" + "b" * 200 + "\n## C\nc"
    clipped = conversation._clip_sections(text, 20)
    assert clipped == "# t\n\n## A\n" + "a" * 30 + "\n## C\nc"
    assert conversation._clip_sections("x\n" * 100, 10) == conversation._clip("x\n" * 100, 10)
    assert conversation._clip_sections(text, 1000) == text


def test_knowledge_query_skips_system_continuation():
    hist = {"last_task": "Người dùng đã quyết định...", "last_user_task": "sửa cấu hình kafka", "last_user_entities": ["kafka"]}
    assert conversation.knowledge_query("Người dùng đã quyết định...", hist, ["lệnh chờ duyệt"], True) == ("sửa cấu hình kafka", ["kafka"])
    assert conversation.knowledge_query("MR cho cả A+B", hist, ["MR"], False) == ("sửa cấu hình kafka MR cho cả A+B", ["MR"])
    assert conversation.knowledge_query("xem pod", {"last_user_task": ""}, None, True) == ("xem pod", None)


def test_history_remembers_last_user_task(users):
    alice = users["alice"]
    first, cid = _chat(alice, "xem pod kafka")
    with db.session() as conn:  # episode chỉ được thêm, không sửa: chép lượt đầu thành một lượt tiếp sau duyệt
        row = dict(conn.execute("SELECT * FROM episode WHERE id=?", (first["id"],)).fetchone())
        row.update(id="cont-1", started_at=row["started_at"] + "Z",
                   input=json.dumps({**json.loads(row["input"]), "task": "hệ thống tiếp tục", "continuation_of": first["id"]}))
        conn.execute(f"INSERT INTO episode({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        h = conversation.load_history(conn, alice.tenant, cid)
    assert h["last_user_task"] == "xem pod kafka" and h["last_task"] == "hệ thống tiếp tục"


# --- Công cụ đọc SKILL.md ---

SKILL = "---\nname: k8s_ops-workflow\ndescription: Quy trình k8s đã kiểm chứng\n---\n\n# k8s_ops-workflow\n\n1. Xem rollout history trước"


class SkillReader(Recording):
    def run_agent(self, task, context, tools, call_tool, history=None, on_text=None, cancelled=None, turn_context=""):
        self.tools = tools
        self.context = context
        self.steps = [call_tool(conversation.SKILL_TOOL, {"name": "k8s_ops-workflow"}), call_tool(conversation.SKILL_TOOL, {"name": "khong-co"})]
        return super().run_agent(task, context, tools, call_tool, history, on_text, cancelled, turn_context)


def test_agent_reads_skill_body(users, knowledge):
    knowledge.state["files"]["skills/k8s_ops-workflow/SKILL.md"] = SKILL
    provider = SkillReader()
    llm.set_provider(provider)
    _chat(users["alice"], "xem pod")
    tool = next(t for t in provider.tools if t["tool"] == conversation.SKILL_TOOL)
    assert tool["input_schema"]["properties"]["name"]["enum"] == ["k8s_ops-workflow"]
    assert "read_skill" in provider.context and "k8s_ops-workflow: Quy trình k8s" in provider.context
    ok, bad = provider.steps
    assert ok["status"] == "ok" and "rollout history" in ok["result"]["content"]
    assert bad["status"] == "invalid" and "k8s_ops-workflow" in bad["reasons"][0]


def test_no_skill_tool_without_skills(users, knowledge):
    provider = SkillReader()
    llm.set_provider(provider)
    _chat(users["alice"], "xem pod")
    assert all(t["tool"] != conversation.SKILL_TOOL for t in provider.tools) and "<skills" not in provider.context


# --- Công cụ tra tri thức giữa lượt ---

class KnowledgeReader(Recording):
    def run_agent(self, task, context, tools, call_tool, history=None, on_text=None, cancelled=None, turn_context=""):
        self.tools = tools
        self.steps = [call_tool(conversation.SEARCH_TOOL, {"query": "kibana index"}), call_tool(conversation.SEARCH_TOOL, {"query": " "}),
                      call_tool(conversation.PAGE_TOOL, {"entity": "Kibana"}), call_tool(conversation.PAGE_TOOL, {"entity": "index"}),
                      call_tool(conversation.PAGE_TOOL, {"entity": "khong-co"})]
        return super().run_agent(task, context, tools, call_tool, history, on_text, cancelled, turn_context)


def test_agent_searches_knowledge_and_reads_pages(users, knowledge):
    extra = {**_ku(3, "index log nằm ở k8s-staging-*"), "entities": ["kibana"]}
    knowledge.state["kus"] = [extra]
    knowledge.state["files"].update({"entities/kibana.md": "# kibana\n\n1 tri thức đang hiệu lực.\n", "entities/index.md": "# Thực thể\n\n- kibana (1)\n"})
    provider = KnowledgeReader()
    llm.set_provider(provider)
    episode, _ = _chat(users["alice"], "xem pod")
    names = [t["tool"] for t in provider.tools]
    assert conversation.SEARCH_TOOL in names and conversation.PAGE_TOOL in names
    search, empty, page, index, missing = provider.steps
    assert search["status"] == "ok" and search["result"]["results"][0]["id"] == "ku:00000003" and search["result"]["pages"] == ["kibana"]
    assert empty["status"] == "invalid"
    assert page["status"] == "ok" and page["result"]["content"].startswith("# kibana")
    assert "kibana (1)" in index["result"]["content"]
    assert missing["status"] == "invalid" and "index" in missing["reasons"][0]
    with db.session() as conn:
        used = json.loads(conn.execute("SELECT ku_used FROM episode WHERE id=?", (episode["id"],)).fetchone()["ku_used"])
    assert extra["id"] in used  # KU tìm giữa lượt cũng được tính hữu ích/có hại như KU nạp đầu lượt


def test_knowledge_tools_present_without_any_knowledge(users, knowledge):
    knowledge.state.update(files={}, kus=[])
    provider = SkillReader()
    llm.set_provider(provider)
    _chat(users["alice"], "xem pod")
    assert {conversation.SEARCH_TOOL, conversation.PAGE_TOOL} <= {t["tool"] for t in provider.tools}
