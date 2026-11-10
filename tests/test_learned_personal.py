"""Công cụ học được và cá nhân hóa."""
import json

import pytest

from conftest import tool_steps
from kagent import agent, compiler, db, knowledge, learned, llm


def run_three(user, tasks):
    return [user.run(t) for t in tasks]


TASKS = ["xem pod trong namespace default và metric grafana cpu",
         "xem pod trong namespace payments và metric grafana bộ nhớ",
         "xem pod trong namespace default và metric grafana lỗi 5xx"]


def test_repeated_success_proposes_personal_tool_with_params(users):
    alice, admin = users["alice"], users["admin"]
    run_three(alice, TASKS)
    assert admin.post("/api/learning/run").json()["learned_tools_proposed"] == 1
    tool = alice.get("/api/learned-tools").json()[0]
    assert tool["status"] == "proposed" and tool["personal"] and tool["origin"] == "auto"
    assert tool["flow"] == "k8s.get → grafana.query"
    assert set(tool["params"]) == {"namespace", "query"} and tool["risk_tier"] == "R0"
    # Không đề xuất lại cùng chuỗi
    run_three(alice, TASKS)
    assert admin.post("/api/learning/run").json()["learned_tools_proposed"] == 0


def test_shared_sequence_becomes_tenant_proposal_needing_admin(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    alice.run(TASKS[0]); bob.run(TASKS[1]); alice.run(TASKS[2])
    admin.post("/api/learning/run")
    assert alice.get("/api/learned-tools").json() == []  # member không thấy đề xuất của tenant
    tool = admin.get("/api/learned-tools").json()[0]
    assert tool["scope"] == "tenant" and tool["status"] == "proposed"
    assert alice.patch(f"/api/learned-tools/{tool['id']}", json={"status": "active"}).status_code == 403
    assert admin.patch(f"/api/learned-tools/{tool['id']}", json={"status": "active"}).json()["status"] == "active"
    assert alice.get("/api/learned-tools").json()[0]["status"] == "active"


def test_learned_tool_runs_each_step_through_gateway(users):
    alice = users["alice"]
    run_three(alice, TASKS)
    users["admin"].post("/api/learning/run")
    tool = alice.get("/api/learned-tools").json()[0]
    alice.patch(f"/api/learned-tools/{tool['id']}", json={"status": "active"})
    ep = alice.run(f"dùng {tool['name']} namespace=payments query=cpu")
    step = tool_steps(ep)[0]
    assert step["status"] == "ok" and step["learned"] and [s["tool"] for s in step["steps"]] == ["k8s.get", "grafana.query"]
    assert step["steps"][0]["result"]["namespace"] == "payments"
    audit_tools = [a["tool"] for a in alice.get("/api/audit").json()[:2]]
    assert set(audit_tools) == {"k8s.get", "grafana.query"}


def test_learned_tool_cannot_bypass_permissions(users):
    alice, admin = users["alice"], users["admin"]
    run_three(alice, TASKS)
    admin.post("/api/learning/run")
    tool = alice.get("/api/learned-tools").json()[0]
    alice.patch(f"/api/learned-tools/{tool['id']}", json={"status": "active"})
    admin.patch("/api/tools/grafana.query", json={"roles": ["tenant_admin"]})
    step = tool_steps(alice.run(f"dùng {tool['name']} namespace=default query=cpu"))[0]
    assert step["status"] == "blocked" and "grafana.query" in step["reasons"][0]


def test_learned_tool_param_validation(users):
    alice = users["alice"]
    run_three(alice, TASKS)
    users["admin"].post("/api/learning/run")
    tool = alice.get("/api/learned-tools").json()[0]
    alice.patch(f"/api/learned-tools/{tool['id']}", json={"status": "active"})
    step = tool_steps(alice.run(f"dùng {tool['name']} namespace=default"))[0]
    assert step["status"] == "invalid" and "query" in step["reasons"][0]


def test_learned_tool_visible_to_agent_only_when_usable(users):
    alice, bob = users["alice"], users["bob"]
    run_three(alice, TASKS)
    users["admin"].post("/api/learning/run")
    tool = alice.get("/api/learned-tools").json()[0]
    seen = {}

    class Spy(llm.HeuristicProvider):
        def run_agent(self, task, context, tool_list, call_tool, **kw):
            seen.setdefault(task, {t["tool"] for t in tool_list})
            return {"answer": "x", "tokens": 0}

    llm.set_provider(Spy())
    alice.run("lần 1")
    assert tool["name"] not in seen["lần 1"]  # chưa bật
    alice.patch(f"/api/learned-tools/{tool['id']}", json={"status": "active"})
    alice.run("lần 2")
    bob.run("lần 3")
    assert tool["name"] in seen["lần 2"] and tool["name"] not in seen["lần 3"]  # công cụ cá nhân của alice


def test_tainted_sequences_are_not_learned(users):
    alice, admin = users["alice"], users["admin"]
    for _ in range(3):
        alice.run("tìm log lỗi rồi xem pod rồi tạo tài liệu tổng hợp")
    admin.post("/api/learning/run")
    assert alice.get("/api/learned-tools").json() == []


def test_save_episode_as_personal_tool(users):
    alice, bob = users["alice"], users["bob"]
    ep = alice.run("scale deployment web lên 4 replica trong namespace default")
    step = tool_steps(ep)[0]
    bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    ok_ep = alice.run("xem pod trong namespace default")
    r = alice.post("/api/learned-tools/from-episode", json={"episode_id": ok_ep["id"], "name": "Xem pod nhanh"})
    assert r.status_code == 200, r.text
    tool = r.json()
    assert tool["name"] == "learned.xem_pod_nhanh" and tool["status"] == "active" and tool["params"] == {"namespace": {"type": "string", "description": "ví dụ: default"}}
    assert bob.post("/api/learned-tools/from-episode", json={"episode_id": ok_ep["id"]}).status_code == 403
    assert alice.post("/api/learned-tools/from-episode", json={"episode_id": ep["id"]}).status_code == 400  # bước pending, không có bước ok
    assert tool_steps(alice.run("dùng learned.xem_pod_nhanh namespace=payments"))[0]["steps"][0]["result"]["namespace"] == "payments"


def test_save_episode_as_tenant_tool_needs_admin(users):
    alice, admin = users["alice"], users["admin"]
    ep = alice.run("xem pod trong namespace default")
    tool = alice.post("/api/learned-tools/from-episode", json={"episode_id": ep["id"], "scope": "tenant"}).json()
    assert tool["status"] == "proposed" and tool["scope"] == "tenant"
    assert admin.patch(f"/api/learned-tools/{tool['id']}", json={"status": "rejected"}).json()["status"] == "rejected"


def test_save_refused_for_tainted_episode(users):
    alice = users["alice"]
    alice.run("xem pod trong namespace default")
    ep = alice.run("tìm log lỗi rồi xem pod rồi tạo tài liệu tổng hợp")
    r = alice.post("/api/learned-tools/from-episode", json={"episode_id": ep["id"]})
    assert r.status_code == 400 and "chưa tin cậy" in r.text


def test_parallel_untrusted_reads_can_be_saved(users):
    alice = users["alice"]
    ep = alice.run("chạy `SELECT count(*) FROM orders` và `SELECT count(*) FROM customers`")
    assert all(s["untrusted"] for s in tool_steps(ep))
    tool = alice.post("/api/learned-tools/from-episode", json={"episode_id": ep["id"], "name": "dem"}).json()
    assert tool["flow"] == "sql.query → sql.query"
    step = tool_steps(alice.run("dùng learned.dem"))[0]
    assert step["status"] == "ok" and [s["result"]["rows"] for s in step["steps"]] == [[[6]], [[4]]]


@pytest.mark.parametrize("text,expected", [("Thống kê đơn hàng", "thong_ke_don_hang"), ("Đếm lỗi 5xx!", "dem_loi_5xx"), ("", "cong_cu")])
def test_slug_strips_vietnamese_diacritics(text, expected):
    assert learned.slug(text) == expected


def test_tainted_detection_respects_parallel_groups():
    a = {"untrusted": True, "parallel": 1}
    b = {"untrusted": False, "parallel": 1}
    c = {"untrusted": False}
    assert not learned._tainted([a, b])
    assert learned._tainted([a, b, c])
    assert learned._tainted([{"untrusted": True}, {"untrusted": False}])


def test_low_success_rate_auto_disables(users):
    alice = users["alice"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        row = learned.create(conn, alice.tenant, name="learned.scale_x", description="d", scope=f"user:{uid}", status="active", origin="manual",
                             source_eps=[], created_by=uid, params={"replicas": {"type": "integer"}},
                             steps=[{"tool": "k8s.scale", "args": {"namespace": "default", "deployment": "web", "replicas": "{{replicas}}"}}])
    alice.run("xem pod trong namespace default")
    for _ in range(4):
        step = tool_steps(alice.run("dùng learned.scale_x replicas=50"))[0]  # replicas > 20 lỗi
        assert step["status"] in ("error", "blocked")
    view = alice.get("/api/learned-tools").json()[0]
    assert view["status"] == "disabled" and "tự tắt" in view["note"]
    # Bật lại thì đặt lại bộ đếm
    assert alice.patch(f"/api/learned-tools/{row['id']}", json={"status": "active"}).json()["uses"] == 0


@pytest.mark.parametrize("steps,params,message", [
    ([], {}, "1 đến"),
    ([{"tool": "khong.co", "args": {}}], {}, "danh mục"),
    ([{"tool": "k8s.scale", "args": {"deployment": "web"}}], {}, "thiếu tham số bắt buộc"),
    ([{"tool": "k8s.get", "args": {"namespace": "{{ns}}"}}], {}, "chưa được khai báo"),
    ([{"tool": "k8s.get", "args": {}}], {"ns": {"type": "string"}}, "không được dùng"),
    ([{"tool": "k8s.get", "args": {"namespace": "{{ns}}"}}], {"ns": {"type": "float"}}, "không hợp lệ"),
])
def test_learned_validation(steps, params, message):
    with pytest.raises(learned.LearnedError, match=message):
        learned.validate(steps, params)


def test_personal_tool_private_to_owner(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    ep = alice.run("xem pod trong namespace default")
    tool = alice.post("/api/learned-tools/from-episode", json={"episode_id": ep["id"]}).json()
    assert bob.get("/api/learned-tools").json() == [] and admin.get("/api/learned-tools").json() == []
    assert admin.patch(f"/api/learned-tools/{tool['id']}", json={"status": "disabled"}).status_code == 404
    assert tool_steps(bob.run(f"dùng {tool['name']} namespace=default"))[0]["status"] == "blocked"


# ---- Cá nhân hóa ----

def test_profile_roundtrip_and_validation(users):
    alice = users["alice"]
    body = {"instructions": "Trả lời bằng tiếng Việt, password=abc123", "style": "concise", "default_env": "staging"}
    saved = alice.put("/api/me/profile", tenant=False, json=body).json()
    assert saved["style"] == "concise" and "abc123" not in saved["instructions"]
    assert alice.get("/api/me", tenant=False).json()["profile"]["default_env"] == "staging"
    assert alice.put("/api/me/profile", tenant=False, json={**body, "style": "khac"}).status_code == 400
    assert alice.put("/api/me/profile", tenant=False, json={**body, "instructions": "x" * 2001}).status_code == 422


def test_profile_injected_into_context_for_owner_only(users):
    alice, bob = users["alice"], users["bob"]
    alice.put("/api/me/profile", tenant=False, json={"instructions": "Luôn kết thúc bằng tóm tắt", "style": "detailed", "default_env": "dev"})
    with db.session() as conn:
        ids = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM app_user")}
        mine = agent.assemble_context(conn, alice.tenant, "xem pod", "k8s_ops", user_id=ids["alice"])
        theirs = agent.assemble_context(conn, alice.tenant, "xem pod", "k8s_ops", user_id=ids["bob"])
    assert "Luôn kết thúc bằng tóm tắt" in mine["text"] and "chi tiết" in mine["text"] and "<user_preferences" in mine["text"]
    assert "tóm tắt" not in theirs["text"]


def test_personal_preference_learned_auto_and_private(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    alice.run("tôi thích câu trả lời có bảng tổng hợp ở cuối")
    admin.post("/api/learning/run")
    mem = alice.get("/api/me/memories").json()
    assert len(mem) == 1 and mem[0]["status"] == "active" and mem[0]["kind"] == "rule"
    assert bob.get("/api/me/memories").json() == []
    assert all(k["id"] != mem[0]["id"] for k in admin.get("/api/ku").json())
    assert admin.get(f"/api/ku/{mem[0]['id']}").status_code == 404
    ep = alice.run("xem pod")
    assert mem[0]["id"] in ep["ku_used"]
    assert mem[0]["id"] not in bob.run("xem pod")["ku_used"]


def test_tenant_rule_still_needs_review(users):
    alice, admin = users["alice"], users["admin"]
    alice.run("luôn thông báo kênh ops trước khi restart dịch vụ")
    admin.post("/api/learning/run")
    assert alice.get("/api/me/memories").json() == []
    assert alice.get("/api/ku?status=candidate").json()[0]["scope"] == "tenant"


def test_user_can_delete_own_memory_only(users):
    alice, bob = users["alice"], users["bob"]
    alice.run("tôi muốn mọi câu trả lời đều ngắn gọn")
    users["admin"].post("/api/learning/run")
    mem = alice.get("/api/me/memories").json()[0]
    assert bob._call("DELETE", f"/api/me/memories/{mem['id']}").status_code == 404
    assert alice._call("DELETE", f"/api/me/memories/{mem['id']}").status_code == 200
    assert alice.get("/api/me/memories").json() == []
    assert mem["id"] not in alice.run("xem pod")["ku_used"]


def test_personal_memories_not_compiled_into_tenant_playbook(users):
    alice, admin = users["alice"], users["admin"]
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        knowledge.add(conn, alice.tenant, kind="rule", title="riêng", content="Bí mật cá nhân của alice", scope=f"user:{uid}", source_eps=[], source_trust="user", status="active")
        knowledge.add(conn, alice.tenant, kind="lesson", title="dự án", content="Ghi chú dự án zeta", scope="project:zeta", domain="general", source_eps=[], source_trust="user", status="active")
        files, _ = compiler.generate(conn, alice.tenant)
    assert not any("Bí mật cá nhân" in c or "dự án zeta" in c for c in files.values())


def test_personal_curation_does_not_touch_other_users(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    alice.run("tôi thích câu trả lời ngắn gọn có gạch đầu dòng")
    bob.run("tôi thích câu trả lời ngắn gọn có gạch đầu dòng")
    admin.post("/api/learning/run")
    a, b = alice.get("/api/me/memories").json(), bob.get("/api/me/memories").json()
    assert len(a) == 1 and len(b) == 1 and a[0]["id"] != b[0]["id"]


def test_tool_notes_learned_from_errors_are_loaded(users):
    alice, admin = users["alice"], users["admin"]
    alice.run("xem pod trong namespace default")
    alice.run("scale deployment web lên 50 replica")  # lỗi: replicas > 20
    admin.post("/api/learning/run")
    lesson = next(k for k in alice.get("/api/ku?kind=lesson").json() if "tool:k8s.scale" in k["entities"])
    admin.post(f"/api/ku/{lesson['id']}/review", json={"approve": True})
    with db.session() as conn:
        ctx = agent.assemble_context(conn, alice.tenant, "tạo dashboard", "observability", tool_names=["k8s.scale", "k8s.get"])
    assert '<knowledge section="tools">' in ctx["text"] and "k8s.scale" in ctx["text"] and lesson["id"] in ctx["ku_used"]


def test_manual_personal_memory_active_immediately(users):
    alice = users["alice"]
    ku = alice.post("/api/ku", json={"kind": "fact", "title": "Tên", "content": "Gọi tôi là An", "scope": "user"}).json()
    assert ku["status"] == "active" and ku["scope"].startswith("user:")
