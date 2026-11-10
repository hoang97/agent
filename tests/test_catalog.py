import json
import textwrap

import pytest

from conftest import tool_steps
from kagent import agent, db, tools
from kagent.tools import CatalogError, Tool, load_catalog, minimum_tier


def write(tmp_path, body, name="x.yaml"):
    (tmp_path / name).write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


GOOD = """
server: mcp-x
description: thử
tools:
  - name: x.read
    description: đọc
    handler: sim:k8s_get
    action_class: read
    input_schema:
      properties: {namespace: {type: string}}
"""


def test_shipped_catalog_is_valid_and_consistent():
    loaded, servers = load_catalog()
    assert len(loaded) == 17 and len(servers) == 5
    for t in loaded.values():
        assert tools.TIERS.index(t.risk_tier) >= tools.TIERS.index(minimum_tier(t.action_class, t.reversible, t.blast_radius, t.data_sensitivity))
        assert set(t.required) <= set(t.properties)
        assert t.dry_run == "none" or t.check is not None
    assert not [t.name for t in loaded.values() if t.warnings]


@pytest.mark.parametrize("meta,tier", [
    (("read", "n/a", "object", "internal"), "R0"),
    (("read", "n/a", "object", "personal"), "R1"),
    (("read", "n/a", "object", "secret"), "R4"),
    (("write", "yes", "workload", "internal"), "R2"),
    (("write", "partial", "workload", "internal"), "R3"),
    (("write", "yes", "namespace", "internal"), "R3"),
    (("destructive", "no", "workload", "internal"), "R3"),
    (("destructive", "no", "cluster", "internal"), "R4"),
    (("external_send", "no", "object", "public"), "R3"),
    (("exec", "no", "object", "internal"), "R4"),
])
def test_minimum_tier(meta, tier):
    assert minimum_tier(*meta) == tier


def test_good_manifest_loads_with_defaults(tmp_path):
    loaded, servers = load_catalog(write(tmp_path, GOOD))
    t = loaded["x.read"]
    assert servers == {"mcp-x": "thử"} and t.risk_tier == "R0" and t.envs == ("dev", "staging", "prod") and t.roles == ("member",)
    assert t.input_schema == {"type": "object", "properties": {"namespace": {"type": "string"}}, "required": []}


@pytest.mark.parametrize("change,message", [
    (("action_class: read", "action_class: write\n    reversible: \"yes\"\n    blast_radius: object\n    risk_tier: R0"), "thấp hơn mức tối thiểu R2"),
    (("action_class: read", "action_class: delete"), "action_class phải thuộc"),
    (("handler: sim:k8s_get", "handler: os:system"), "không có module kagent.backends.os"),
    (("handler: sim:k8s_get", "handler: sim:khong_co"), "không có hàm khong_co"),
    (("handler: sim:k8s_get", "handler: ../../x:y"), "handler phải có dạng"),
    (("name: x.read", "name: XRead"), "tên phải có dạng"),
    (("    description: đọc\n", ""), "thiếu description"),
    (("properties: {namespace: {type: string}}", "properties: {namespace: {type: string}}\n      required: [khac]"), "required có trường"),
    (("action_class: read", "action_class: read\n    dry_run: server"), "cần khai báo check"),
    (("action_class: read", "action_class: read\n    envs: [prod, qa]"), "envs phải là"),
    (("action_class: read", "action_class: read\n    roles: []"), "roles phải là"),
    (("action_class: read", "action_class: read\n    owner: ai"), "khóa không hợp lệ"),
    (("action_class: read", "action_class: read\n    annotations: {readOnlyHint: maybe}"), "annotations chỉ gồm"),
])
def test_invalid_manifest_rejected(tmp_path, change, message):
    with pytest.raises(CatalogError, match=message):
        load_catalog(write(tmp_path, GOOD.replace(*change)))


def test_errors_collected_across_files(tmp_path):
    write(tmp_path, GOOD.replace("action_class: read", "action_class: nope"), "a.yaml")
    write(tmp_path, GOOD.replace("server: mcp-x", "server: mcp-y"), "b.yaml")
    with pytest.raises(CatalogError) as exc:
        load_catalog(tmp_path)
    assert any("a.yaml" in e for e in exc.value.errors)


def test_duplicate_tool_and_server_rejected(tmp_path):
    write(tmp_path, GOOD, "a.yaml")
    write(tmp_path, GOOD, "b.yaml")
    with pytest.raises(CatalogError, match="đã được khai|bị trùng"):
        load_catalog(tmp_path)


def test_unclassified_tool_treated_as_destructive(tmp_path):
    loaded, _ = load_catalog(write(tmp_path, GOOD.replace("    action_class: read\n", "")))
    t = loaded["x.read"]
    assert t.action_class == "destructive" and t.risk_tier == "R3" and not t.classified and t.warnings


def test_hint_mismatch_is_warning_not_reclassification(tmp_path):
    loaded, _ = load_catalog(write(tmp_path, GOOD.replace("action_class: read", "action_class: write\n    reversible: \"yes\"\n    blast_radius: object\n    annotations: {readOnlyHint: true}")))
    t = loaded["x.read"]
    assert t.action_class == "write" and t.risk_tier == "R2" and "readOnlyHint" in t.warnings[0]


def test_cli_reports_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["kagent.tools", str(write(tmp_path, GOOD.replace("action_class: read", "action_class: nope")))])
    assert tools.main() == 1 and "action_class" in capsys.readouterr().out


# --- Đồng bộ vào tenant ---

def extra_tool(name="x.new"):
    return Tool(name=name, server="mcp-k8s", description="mới", action_class="read", reversible="n/a", blast_radius="object",
                data_sensitivity="internal", risk_tier="R0", run=lambda state, args: {"ok": True}, properties={})


def registry(conn, tenant, tool):
    return conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND tool=?", (tenant, tool)).fetchone()


def test_new_catalog_tool_added_disabled_for_existing_tenants(users, monkeypatch):
    alice, admin = users["alice"], users["admin"]
    monkeypatch.setitem(tools.TOOLS, "x.new", extra_tool())
    with db.session() as conn:
        tools.sync_registry(conn)
        row = registry(conn, alice.tenant, "x.new")
    assert row["enabled"] == 0 and row["approved_by"] is None and row["status"] == "active"
    view = next(t for t in admin.get("/api/tools").json() if t["tool"] == "x.new")
    assert view["is_new"] is True
    step = tool_steps(alice.run("xem pod"))[0]  # công cụ cũ vẫn chạy
    assert step["status"] == "ok"
    admin.patch("/api/tools/x.new", json={"enabled": True})
    assert next(t for t in admin.get("/api/tools").json() if t["tool"] == "x.new")["is_new"] is False


def test_removed_tool_is_blocked_and_kept_for_audit(users, monkeypatch):
    alice, admin = users["alice"], users["admin"]
    monkeypatch.delitem(tools.TOOLS, "k8s.get")
    with db.session() as conn:
        tools.sync_registry(conn)
        assert registry(conn, alice.tenant, "k8s.get")["status"] == "removed"
    step = tool_steps(alice.run("xem pod"))[0]
    assert step["decision"] == "blocked" and "gỡ" in step["reasons"][0]
    assert admin.patch("/api/tools/k8s.get", json={"enabled": True}).status_code == 409
    monkeypatch.undo()
    with db.session() as conn:
        tools.sync_registry(conn)
    assert tool_steps(alice.run("xem pod"))[0]["status"] == "ok"


def test_sync_keeps_tenant_choices_and_raised_tier(users):
    admin = users["admin"]
    admin.patch("/api/tools/k8s.get", json={"risk_tier": "R2", "envs": ["dev"], "roles": ["tenant_admin"], "enabled": False})
    with db.session() as conn:
        tools.sync_registry(conn)
        row = registry(conn, admin.tenant, "k8s.get")
    assert row["risk_tier"] == "R2" and json.loads(row["envs"]) == ["dev"] and json.loads(row["roles"]) == ["tenant_admin"] and row["enabled"] == 0


def test_catalog_raise_applies_to_tenants(users, monkeypatch):
    admin = users["admin"]
    raised = tools.TOOLS["k8s.get"]
    monkeypatch.setitem(tools.TOOLS, "k8s.get", Tool(**{**raised.__dict__, "risk_tier": "R1"}))
    with db.session() as conn:
        tools.sync_registry(conn)
        assert registry(conn, admin.tenant, "k8s.get")["risk_tier"] == "R1"


def test_env_restriction_blocks_and_hides_tool(users):
    alice, admin = users["alice"], users["admin"]
    assert admin.patch("/api/tools/k8s.get", json={"envs": ["staging"]}).status_code == 200
    step = tool_steps(alice.run("xem pod"))[0]
    assert step["decision"] == "blocked" and "môi trường dev" in step["reasons"][0]
    assert tool_steps(alice.run("xem pod", env="staging"))[0]["status"] == "ok"


def test_role_restriction_blocks_member_and_hides_from_agent(users):
    alice, admin, bob = users["alice"], users["admin"], users["bob"]
    assert admin.patch("/api/tools/k8s.get", json={"roles": ["approver:dev"]}).status_code == 200
    assert "vai trò" in tool_steps(alice.run("xem pod"))[0]["reasons"][0]
    assert tool_steps(bob.run("xem pod"))[0]["status"] == "ok"  # bob có approver:dev
    with db.session() as conn:
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        rows = conn.execute("SELECT * FROM tool_registry WHERE tenant_id=? AND enabled=1", (alice.tenant,)).fetchall()
        roles = {"member"}
        assert "k8s.get" not in [r["tool"] for r in rows if tools.callable_by(r, "dev", roles)[0]]


def test_agent_tool_list_filtered_by_permissions(users):
    from kagent import llm
    alice, admin = users["alice"], users["admin"]
    admin.patch("/api/tools/k8s.exec", json={"roles": ["tenant_admin"]})
    admin.patch("/api/tools/k8s.scale", json={"envs": ["prod"]})
    seen = {}

    class Spy(llm.HeuristicProvider):
        def run_agent(self, task, context, tool_list, call_tool, **kw):
            seen["tools"] = {t["tool"] for t in tool_list}
            return {"answer": "x", "tokens": 0}

    llm.set_provider(Spy())
    alice.run("xem pod")
    assert "k8s.get" in seen["tools"] and "k8s.exec" not in seen["tools"] and "k8s.scale" not in seen["tools"]
    admin.run("xem pod")
    assert "k8s.exec" in seen["tools"]


@pytest.mark.parametrize("body,status", [
    ({"risk_tier": "R0"}, 403),          # k8s.scale ở danh mục là R2
    ({"risk_tier": "R9"}, 400),
    ({"envs": []}, 400),
    ({"envs": ["qa"]}, 400),
    ({"roles": ["viewer"]}, 400),
    ({"roles": []}, 400),
    ({"risk_tier": "R3", "envs": ["dev", "prod"], "roles": ["member", "tenant_admin"]}, 200),
])
def test_patch_only_tightens(users, body, status):
    assert users["admin"].patch("/api/tools/k8s.scale", json=body).status_code == status


def test_lowering_back_to_catalog_tier_clears_override(users):
    admin = users["admin"]
    admin.patch("/api/tools/k8s.scale", json={"risk_tier": "R4"})
    view = admin.patch("/api/tools/k8s.scale", json={"risk_tier": "R2"}).json()
    assert view["risk_tier"] == "R2" and view["risk_floor"] is None and view["tier_choices"] == ["R2", "R3", "R4"]


def test_member_cannot_patch(users):
    assert users["alice"].patch("/api/tools/k8s.get", json={"enabled": False}).status_code == 403


def test_tool_view_fields(users):
    view = next(t for t in users["alice"].get("/api/tools").json() if t["tool"] == "kibana.search")
    assert view["catalog_tier"] == "R1" and view["effective_envs"] == ["dev", "staging", "prod"] and view["role_choices"] == list(tools.ROLE_CHOICES)
    assert view["server_description"].startswith("Kibana") and view["untrusted_output"] == 1


def test_approval_refused_if_tool_disabled_after_request(users):
    alice, bob, admin = users["alice"], users["bob"], users["admin"]
    step = tool_steps(alice.run("scale deployment web lên 5 replica"))[0]
    admin.patch("/api/tools/k8s.scale", json={"enabled": False})
    r = bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert r.status_code == 409
    assert bob.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "deny", "reason": "đã tắt"}).json()["status"] == "denied"


def test_migration_adds_registry_columns(client):
    with db.session() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_registry)")}
        assert {"catalog_tier", "risk_floor", "roles", "status", "synced_at"} <= cols
        assert conn.execute("SELECT count(*) c FROM tool_registry WHERE catalog_tier IS NULL").fetchone()["c"] == 0
