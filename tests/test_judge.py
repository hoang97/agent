"""LLM-as-judge: chấm episode chưa có kết quả trước khi Reflector và bộ đếm dùng tới."""
import json

from kagent import agent, db, knowledge, learning, llm

from test_llm import msg, provider, text


class Judges(llm.HeuristicProvider):
    def __init__(self, outcome):
        self.outcome = outcome
        self.seen = []

    def judge(self, episode):
        self.seen.append(episode["id"])
        return {"outcome": self.outcome, "reason": "chấm thử"}


def _active_ku(tenant):
    with db.session() as conn:
        return knowledge.add(conn, tenant, kind="insight", title="Giới thiệu ngắn gọn", content="Khi viết giới thiệu hãy mở đầu bằng một câu tóm tắt",
                             source_eps=[], source_trust="user", status="active")


def _episode(conn, tenant, episode_id):
    return agent.get_episode(conn, tenant, episode_id)


def test_unknown_episode_is_judged_and_counts(users):
    alice, admin = users["alice"], users["admin"]
    ku = _active_ku(alice.tenant)
    ep = alice.run("viết giới thiệu ngắn gọn có câu tóm tắt")
    assert ep["outcome"] == "unknown" and ku["id"] in ep["ku_used"]

    judge = Judges("success")
    llm.set_provider(judge)
    admin.post("/api/learning/run")
    assert judge.seen == [ep["id"]]
    with db.session() as conn:
        got = _episode(conn, alice.tenant, ep["id"])
    assert (got["outcome"], got["outcome_src"]) == ("success", "llm_judge")
    assert alice.get(f"/api/ku/{ku['id']}").json()["helpful_cnt"] == 1


def test_judge_unknown_leaves_episode_unlabeled(users):
    alice, admin = users["alice"], users["admin"]
    ep = alice.run("viết giới thiệu ngắn gọn")
    llm.set_provider(Judges("unknown"))
    admin.post("/api/learning/run")
    with db.session() as conn:
        got = _episode(conn, alice.tenant, ep["id"])
    assert got["outcome"] == "unknown" and got["outcome_src"] is None and got["processed_at"]


def test_user_feedback_is_not_rejudged(users):
    alice, admin = users["alice"], users["admin"]
    ep = alice.run("viết giới thiệu ngắn gọn")
    alice.post(f"/api/episodes/{ep['id']}/feedback", json={"outcome": "failure"})
    judge = Judges("success")
    llm.set_provider(judge)
    admin.post("/api/learning/run")
    assert judge.seen == []
    with db.session() as conn:
        assert _episode(conn, alice.tenant, ep["id"])["outcome_src"] == "user_feedback"


def test_pending_or_cancelled_episode_is_not_judged(users):
    alice = users["alice"]
    ep = alice.run("viết giới thiệu ngắn gọn")
    judge = Judges("failure")
    with db.session() as conn:
        for extra in ({"type": "tool_call", "tool": "k8s.scale", "status": "pending"}, {"type": "cancelled"}):
            episode = _episode(conn, alice.tenant, ep["id"])
            episode["trace"] = [*episode["trace"], extra]
            learning._judge(conn, alice.tenant, judge, episode)
    assert judge.seen == []


def test_judged_success_on_untrusted_content_does_not_count_helpful(users):
    alice = users["alice"]
    ku = _active_ku(alice.tenant)
    ep = alice.run("viết giới thiệu ngắn gọn có câu tóm tắt")
    with db.session() as conn:
        episode = _episode(conn, alice.tenant, ep["id"])
        episode["trace"] = [*episode["trace"], {"type": "tool_call", "tool": "web.fetch", "status": "blocked", "untrusted": True}]
        learning.process_episode(conn, alice.tenant, Judges("success"), episode)
        assert _episode(conn, alice.tenant, ep["id"])["outcome"] == "success"
    assert alice.get(f"/api/ku/{ku['id']}").json()["helpful_cnt"] == 0


def test_structured_judge_uses_strong_model_and_validates():
    p, fake = provider([msg([text(json.dumps({"outcome": "partial", "reason": "thiếu bảng"}))]),
                        msg([text(json.dumps({"outcome": "great", "reason": "x"}))])])
    assert p.judge({"input": {"task": "x"}}) == {"outcome": "partial", "reason": "thiếu bảng"}
    assert fake.calls[0]["model"] == "claude-opus-5-5"
    assert p.judge({"input": {"task": "x"}})["outcome"] == "unknown"
