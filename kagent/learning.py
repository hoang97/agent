"""Luồng học nền: Reflector → Curator → Cổng duyệt. Agent làm việc không bao giờ chờ bước này."""
from __future__ import annotations

import json
import threading

from . import db, entities, knowledge, learned, llm
from .config import settings
from .knowledge import scrub
from .retrieval import near, tokenize

_lock = threading.Lock()


def _source_trust(item: dict, episode: dict, untrusted_seen: bool) -> str:
    """Không tin nhãn do model tự khai: 'user' phải khớp nguyên văn với lời người dùng."""
    if item["source"] == "user_input":
        said = set(tokenize(episode["input"]["task"]))
        if episode.get("human_edit"):
            said |= set(tokenize(json.dumps(episode["human_edit"], ensure_ascii=False)))
        words = tokenize(item["content"])
        if words and sum(w in said for w in words) / len(words) >= 0.6:
            return "user"
    if untrusted_seen or item["source"] == "external":
        return "external_untrusted"
    return "verified_tool"


STEP_RESULT_CHARS = 1500  # Reflector cần biết bước nào làm gì và ra sao, không cần toàn bộ dữ liệu trả về


def _episode_view(episode: dict) -> dict:
    """Bản gửi cho Reflector/judge: lọc bí mật và chỉ giữ phần đầu kết quả của từng bước để không tốn token vô ích."""
    view = dict(episode)
    view["trace"] = []
    for step in episode.get("trace") or []:
        if step.get("type") == "tool_call":
            step = {k: v for k, v in step.items() if k not in ("confidence_parts", "dry_run")}
            text = json.dumps(step.get("result"), ensure_ascii=False)
            if len(text) > STEP_RESULT_CHARS:
                step["result"] = text[:STEP_RESULT_CHARS] + f"…(đã cắt {len(text) - STEP_RESULT_CHARS} ký tự)"
        view["trace"].append(step)
    return json.loads(scrub(json.dumps(view, ensure_ascii=False)))


def _judge(conn, tenant_id: str, provider, episode: dict) -> None:
    """LLM-as-judge cho episode chưa có kết quả, để Reflector và bộ đếm có tín hiệu."""
    if episode["outcome"] != "unknown":
        return
    trace = episode["trace"]
    # Lượt bị dừng hoặc còn lệnh chờ duyệt chưa có kết quả cuối để chấm.
    if any(s.get("type") == "cancelled" or s.get("status") == "pending" for s in trace):
        return
    verdict = provider.judge(_episode_view(episode))
    if verdict["outcome"] not in llm.JUDGED_OUTCOMES:
        return
    # Điều kiện outcome='unknown' giữ phản hồi người dùng gửi cùng lúc không bị ghi đè.
    cur = conn.execute("UPDATE episode SET outcome=?, outcome_src='llm_judge' WHERE id=? AND tenant_id=? AND outcome='unknown'",
                       (verdict["outcome"], episode["id"], tenant_id))
    if cur.rowcount:
        episode["outcome"], episode["outcome_src"] = verdict["outcome"], "llm_judge"


def _update_counters(conn, tenant_id: str, episode: dict, blamed: set[str], untrusted_seen: bool) -> None:
    # Nội dung untrusted có thể lái lời chấm của judge, nên "thành công" do judge chấm khi đó không cộng điểm hữu ích.
    judged_on_untrusted = episode.get("outcome_src") == "llm_judge" and untrusted_seen
    clean_success = episode["outcome"] == "success" and not episode.get("human_edit") and not judged_on_untrusted
    reason = f"bộ đếm từ episode {episode['id']}"
    for ku_id in episode["ku_used"]:
        ku = knowledge.get(conn, tenant_id, ku_id)
        if ku is None:
            continue
        # Phản hồi sau khi đã học sẽ cho episode chạy lại luồng học; mỗi episode chỉ được tính một lần.
        if conn.execute("SELECT 1 FROM ku_event WHERE tenant_id=? AND ku_id=? AND reason=? LIMIT 1", (tenant_id, ku_id, reason)).fetchone():
            continue
        changes = None
        if ku_id in blamed:
            changes = {"harmful_cnt": ku["harmful_cnt"] + 1}
        elif clean_success:
            changes = {"helpful_cnt": ku["helpful_cnt"] + 1}
        if changes is None:
            continue
        merged = {**ku, **changes}
        changes["confidence"] = knowledge.recompute_confidence(merged)
        knowledge.update(conn, tenant_id, ku_id, changes, "UPDATE", "curator", reason)
        if merged["harmful_cnt"] > merged["helpful_cnt"] and ku["status"] == "active":
            knowledge.update(conn, tenant_id, ku_id, {"status": "deprecated", "valid_to": db.iso()}, "DEPRECATE", "curator", "số lần có hại vượt số lần hữu ích")


def curate_item(conn, tenant_id: str, provider, item: dict, episode: dict, trust: str) -> str:
    scope = f"user:{episode['user_id']}" if item.get("scope") == "user" else episode["scope"]
    candidates = near(conn, tenant_id, f"{item['title']} {item['content']}", item["kind"], settings.near_threshold, scope=scope)
    decision = provider.curate(item, candidates)
    op, target = decision["op"], decision.get("target")
    target_ku = knowledge.get(conn, tenant_id, target) if target else None
    domain = episode.get("task_type") or "general"
    common = dict(kind=item["kind"], title=item["title"], content=item["content"], applies_when=item["applies_when"],
                  entities=item["entities"], source_eps=[episode["id"]], source_trust=trust, scope=scope, domain=domain)

    if op == "NOOP" and target_ku:
        sources = sorted(set(target_ku["source_eps"]) | {episode["id"]})
        if sources != target_ku["source_eps"]:
            knowledge.update(conn, tenant_id, target, {"source_eps": sources}, "UPDATE", "curator", "xác nhận từ episode độc lập")
        return "NOOP"
    if op in ("UPDATE", "MERGE") and target_ku:
        changes = {"content": item["content"], "applies_when": item["applies_when"],
                   "source_eps": sorted(set(target_ku["source_eps"]) | {episode["id"]})}
        if trust == "external_untrusted":
            # Nội dung chưa tin cậy không được sửa KU đang có; thêm thành ứng viên riêng để người xem xét.
            knowledge.add(conn, tenant_id, **common, reason=decision["reason"])
            return "ADD"
        knowledge.update(conn, tenant_id, target, changes, op, "curator", decision["reason"])
        return op
    if op == "INVALIDATE" and target_ku:
        with db.tx(conn):
            new = knowledge.add(conn, tenant_id, **common, reason=decision["reason"])
            knowledge.add_edge(conn, tenant_id, new["id"], target, "contradicts")
        return "INVALIDATE"
    knowledge.add(conn, tenant_id, **common, reason=decision["reason"])
    return "ADD"


def gate(conn, tenant_id: str) -> dict:
    """Cổng thăng hạng: rule luôn cần người duyệt; external_untrusted không bao giờ tự lên."""
    tenant = conn.execute("SELECT auto_promote FROM tenant WHERE id=?", (tenant_id,)).fetchone()
    result = {"promoted": 0, "needs_review": 0}
    rs = conn.execute("SELECT id FROM knowledge_unit WHERE tenant_id=? AND status='candidate'", (tenant_id,)).fetchall()
    for r in rs:
        ku = knowledge.get(conn, tenant_id, r["id"])
        contradiction = conn.execute(
            "SELECT 1 FROM ku_edge e JOIN knowledge_unit k ON k.id=e.dst WHERE e.src=? AND e.rel='contradicts' AND k.status='active'",
            (ku["id"],)).fetchone()
        reason = None
        if ku["scope"].startswith("user:") and ku["source_trust"] == "user":
            # Ký ức cá nhân do chính người dùng nói chỉ ảnh hưởng tới họ nên không cần người duyệt.
            reason = "sở thích cá nhân do người dùng nói thẳng"
        elif tenant["auto_promote"] and ku["kind"] != "rule" and ku["source_trust"] != "external_untrusted":
            if ku["source_trust"] == "user":
                reason = "nguồn là người dùng nói thẳng"
            elif len(set(ku["source_eps"])) >= 2 and not contradiction:
                reason = "được xác nhận bởi từ 2 episode độc lập"
        if reason:
            knowledge.promote(conn, tenant_id, ku["id"], "gate", reason)
            result["promoted"] += 1
        else:
            result["needs_review"] += 1
    return result


def process_episode(conn, tenant_id: str, provider, episode: dict) -> dict:
    trace = episode["trace"]
    untrusted_seen = any(s.get("untrusted") for s in trace)
    blamed: set[str] = set()
    ops: list[str] = []
    _judge(conn, tenant_id, provider, episode)
    view = _episode_view(episode)
    view["known_entities"] = entities.top(conn, tenant_id)
    for item in provider.reflect(view):
        if item.get("blame_ku") in episode["ku_used"]:
            blamed.add(item["blame_ku"])
        ops.append(curate_item(conn, tenant_id, provider, item, episode, _source_trust(item, episode, untrusted_seen)))
    _update_counters(conn, tenant_id, episode, blamed, untrusted_seen)
    conn.execute("UPDATE episode SET processed_at=? WHERE id=? AND tenant_id=?", (db.iso(), episode["id"], tenant_id))
    return {"episode": episode["id"], "ops": ops}


def learn_from_denials(conn, tenant_id: str) -> int:
    """Lý do từ chối lệnh trở thành KU lesson."""
    rs = conn.execute("SELECT * FROM approval_request WHERE tenant_id=? AND status='denied' AND learned=0", (tenant_id,)).fetchall()
    for r in rs:
        reason = r["deny_reason"] or ""
        env = r["env"]
        if r["episode_id"]:
            knowledge.add(conn, tenant_id, kind="lesson", title=f"{r['tool']} bị từ chối ở {env}", content=f"Lệnh {r['tool']} ở {env} từng bị từ chối: {reason}",
                          applies_when=f"khi định dùng {r['tool']} ở {env}", entities=[f"tool:{r['tool']}"], source_eps=[r["episode_id"]],
                          source_trust="user", domain="k8s_ops" if r["tool"].startswith("k8s") else "general", reason="lý do từ chối của người duyệt")
        conn.execute("UPDATE approval_request SET learned=1 WHERE id=?", (r["id"],))
    return len(rs)


def should_learn(conn, tenant_id: str) -> bool:
    r = conn.execute(
        "SELECT count(*) c, sum(outcome='failure' OR human_edit IS NOT NULL) urgent FROM episode WHERE tenant_id=? AND processed_at IS NULL",
        (tenant_id,)).fetchone()
    denied = conn.execute("SELECT 1 FROM approval_request WHERE tenant_id=? AND status='denied' AND learned=0 LIMIT 1", (tenant_id,)).fetchone()
    return bool(r["c"] >= settings.learn_batch_n or r["urgent"] or denied)


def run_learning(conn, tenant_id: str, limit: int = 50) -> dict:
    with _lock:
        provider = llm.get_provider()
        rs = conn.execute("SELECT * FROM episode WHERE tenant_id=? AND processed_at IS NULL ORDER BY started_at LIMIT ?", (tenant_id, limit)).fetchall()
        processed, errors = [], []
        for r in rs:
            episode = db.row(r, "input", "trace", "ku_used", "human_edit")
            with llm.collect_usage() as usage:
                try:
                    processed.append(process_episode(conn, tenant_id, provider, episode))
                except llm.LLMError as exc:
                    errors.append({"episode": episode["id"], "error": str(exc)})
                finally:
                    if usage:
                        llm.save_usage(conn, tenant_id, episode["id"], usage)
        denials = learn_from_denials(conn, tenant_id)
        gated = gate(conn, tenant_id)
        proposed = learned.propose(conn, tenant_id)
    return {"processed": len(processed), "details": processed, "errors": errors, "denials": denials, "learned_tools_proposed": proposed, **gated}
