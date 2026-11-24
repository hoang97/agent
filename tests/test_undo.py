"""Tự chạy lệnh ghi chỉ khi hoàn tác được và hoàn tác đáng tin; kiểm tra sau khi chạy; nút Hoàn tác; xoá mềm và sao lưu."""
import json

import pytest

from aiops import db, gateway, llm, undo
from aiops.connectors import sql_undo

DEPLOY = json.dumps({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "worker", "namespace": "default"},
                     "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "worker", "image": "worker:1"}]}}}})


class Allow:
    def __init__(self):
        self.seen = []

    def classify_action(self, data):
        self.seen.append(data)
        return {"allow": True, "rule": "", "reason": ""}


@pytest.fixture()
def judge(monkeypatch):
    j = Allow()
    monkeypatch.setattr(llm, "get_provider", lambda: j)
    return j


def _ctx(conn, users, env="dev", task="làm theo yêu cầu", who="alice"):
    uid = conn.execute("SELECT id FROM app_user WHERE username=?", (who,)).fetchone()["id"]
    return gateway.CallContext(users[who].tenant, uid, env, task, intent=[task])


def _call(users, tool, args, env="dev"):
    with db.session() as conn:
        return gateway.invoke(conn, _ctx(conn, users, env), tool, args)


def _change(change_id):
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM change_record WHERE id=?", (change_id,)).fetchone())


def _settings(users, **value):
    r = users["admin"].patch("/api/tenant", json={"undo_settings": value})
    assert r.status_code == 200, r.text
    return r.json()["undo"]


def _approve(users, approval_id, who="alice"):
    r = users[who].post(f"/api/approvals/{approval_id}/decide", json={"action": "approve_once"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- cổng tự chạy ----------

def test_scale_with_dry_run_undo_runs_and_reverts(users, infra, judge):
    _settings(users, drill={"dev": False})
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    assert step["status"] == "ok" and step["change_id"]
    assert all(c["ok"] for c in step["undo"]["checks"]) and "chạy thử lệnh hoàn tác qua" in step["undo"]["checks"][2]["text"]
    assert "spec.replicas: 4 → 3" in step["undo"]["summary"]
    assert judge.seen[0]["action"]["undo"] == {"reversible": True, "how": step["undo"]["summary"]}
    assert infra.deployments["default"]["web"] == 4 and _change(step["change_id"])["status"] == "verifying"

    r = users["alice"].post(f"/api/changes/{step['change_id']}/undo", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reverted" and infra.deployments["default"]["web"] == 3
    with db.session() as conn:
        assert undo.evidence(conn, users["alice"].tenant, {"recipe": "k8s.scale:Deployment"}) == ["đã hoàn tác thành công 1 lần"]
        assert conn.execute("SELECT count(*) c FROM tool_call_audit WHERE tool='undo:k8s.kubectl_write' AND result_status='ok'").fetchone()["c"] == 1


def test_no_evidence_asks_then_trusted_recipe_runs(users, infra, judge):
    _settings(users, drill={"dev": False})
    step = _call(users, "k8s.kubectl_write", {"command": "create -f -", "manifest": DEPLOY})
    assert step["status"] == "pending"
    failed = [c["text"] for c in step["undo"]["checks"] if not c["ok"]]
    assert failed == ["Chưa có bằng chứng: loại hoàn tác k8s.create:Deployment chưa từng chạy ở tenant"]
    assert step["reasons"][0].startswith("không tự chạy vì: Chưa có bằng chứng")
    card = users["alice"].get("/api/approvals?status=pending").json()[0]
    assert card["undo"]["summary"] == "xoá deployment/worker vừa tạo"

    _settings(users, trusted=["k8s.create:Deployment"])
    step = _call(users, "k8s.kubectl_write", {"command": "create -f -", "manifest": DEPLOY.replace("worker", "worker2")})
    assert step["status"] == "ok" and "admin đánh dấu tin cậy" in step["undo"]["checks"][2]["text"]
    assert users["alice"].post(f"/api/changes/{step['change_id']}/undo", json={}).json()["status"] == "reverted"
    assert "worker2" not in infra.deployments["default"]


def test_approved_create_is_drilled_in_dev_then_runs_alone(users, infra, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "create -f -", "manifest": DEPLOY})
    assert step["status"] == "pending"
    out = _approve(users, step["approval_id"])
    assert out["status"] == "approved" and "worker" in infra.deployments["default"]
    change = users["alice"].get(f"/api/approvals?status=approved").json()[0]["change"]
    assert change["check"]["drill"] == "đã diễn tập hoàn tác và chạy lại lệnh gốc"
    step = _call(users, "k8s.kubectl_write", {"command": "create -f -", "manifest": DEPLOY.replace("worker", "worker3")})
    assert step["status"] == "ok" and "đã diễn tập 1 lần" in step["undo"]["checks"][2]["text"]


def test_not_reversible_asks_and_card_says_why(users, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "taint nodes node-1 key=v:NoSchedule"})
    assert step["status"] == "pending"
    assert step["undo"]["checks"] == [{"ok": False, "key": "a", "text": "Không hoàn tác được: chưa hỗ trợ hoàn tác taint"}]


def test_rollout_in_progress_asks(users, infra, judge):
    infra.deployments["default"]["web"] = 3
    original = infra.deployment

    def rolling(ns, name, replicas=None):
        obj = original(ns, name, replicas)
        obj["status"]["updatedReplicas"] = 1
        return obj
    infra.deployment = rolling
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    assert step["status"] == "pending"
    assert "Trạng thái hiện tại: deployment/web đang rollout dở (1/3 pod đã lên bản mới)" in [c["text"] for c in step["undo"]["checks"]]


def test_selector_is_not_scoped(users, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default -l app=web"})
    assert step["status"] == "pending"
    assert any(c["key"] == "b" and not c["ok"] for c in step["undo"]["checks"])


def test_red_button_stops_auto_writes(users, judge):
    _settings(users, paused=True)
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    assert step["status"] == "pending" and "admin đã dừng mọi lệnh ghi tự chạy" in step["reasons"]
    assert _call(users, "k8s.kubectl_read", {"command": "get pods -n default"})["status"] == "ok"


# ---------- hoàn tác khi đối tượng đã bị sửa ----------

def test_drift_blocks_undo_until_forced(users, infra, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    infra.set_deployment("default", "web", 6)  # người khác scale tiếp sau lệnh
    r = users["alice"].post(f"/api/changes/{step['change_id']}/undo", json={})
    assert r.status_code == 409 and r.json()["detail"]["drift"] == ["deployment/web đã bị sửa sau lệnh: spec.replicas"]
    assert infra.deployments["default"]["web"] == 6
    r = users["alice"].post(f"/api/changes/{step['change_id']}/undo", json={"force": True})
    assert r.status_code == 200 and infra.deployments["default"]["web"] == 3


def test_only_requester_or_approver_can_undo(users, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    assert users["carol"].post(f"/api/changes/{step['change_id']}/undo", json={}).status_code == 403
    assert users["admin"].post(f"/api/changes/{step['change_id']}/undo", json={}).status_code == 200


# ---------- kiểm tra sau khi chạy ----------

def _due(change_id):
    with db.session() as conn:
        conn.execute("UPDATE change_record SET verify_until=? WHERE id=?", (db.iso(), change_id))


def test_verify_ok(users, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    _due(step["change_id"])
    with db.session() as conn:
        undo.tick(conn)
    row = _change(step["change_id"])
    assert row["status"] == "ok" and json.loads(row["check_result"])["detail"] == "deployment/web 4/4 sẵn sàng"


def test_not_ready_after_bake_time_reverts_auto_run(users, infra, judge):
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    infra.k8s_unready[("default", "web")] = 2
    _due(step["change_id"])
    with db.session() as conn:
        undo.tick(conn)
    row = _change(step["change_id"])
    assert row["status"] == "reverted" and row["reverted_by"] == "auto" and infra.deployments["default"]["web"] == 3
    assert json.loads(row["check_result"])["detail"] == "deployment/web 2/4 sẵn sàng"


def test_new_alert_fails_check_early_and_respects_auto_revert_off(users, judge):
    _settings(users, auto_revert=False)
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    with db.session() as conn:
        conn.execute("INSERT INTO alert_event(id,tenant_id,source,fingerprint,starts_at,alertname,labels,env,first_seen,last_seen) "
                     "VALUES ('a1',?,'am','f',?, 'HighErrorRate', ?, 'dev', ?, ?)",
                     (users["alice"].tenant, db.iso(), json.dumps({"namespace": "default"}), db.iso(), db.iso()))
        undo.tick(conn)
    row = _change(step["change_id"])
    assert row["status"] == "check_failed" and json.loads(row["check_result"])["alerts"] == ["HighErrorRate"]


def test_concurrent_change_on_same_object_asks(users, judge):
    _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=4 -n default"})
    step = _call(users, "k8s.kubectl_write", {"command": "scale deployment/web --replicas=5 -n default"})
    assert step["status"] == "pending"
    assert "Trạng thái hiện tại: deployment/web đang có thay đổi khác chờ kiểm tra" in [c["text"] for c in step["undo"]["checks"]]


# ---------- xoá mềm và sao lưu ----------

def test_delete_index_becomes_close_and_lives_in_trash(users, infra, judge):
    step = _call(users, "logs.delete_index", {"index": "logs-old"})
    assert step["status"] == "pending" and step["undo"]["soft"]
    _approve(users, step["approval_id"])
    assert "logs-old" in infra.indices and "logs-old" in infra.closed
    trash = users["alice"].get("/api/trash").json()
    assert [t["targets"] for t in trash] == [["index logs-old"]] and trash[0]["purge_at"]
    assert users["alice"].post(f"/api/changes/{trash[0]['id']}/undo", json={}).json()["status"] == "reverted"
    assert "logs-old" not in infra.closed and users["alice"].get("/api/trash").json() == []


def test_purge_needs_approver(users, infra, judge):
    step = _call(users, "logs.delete_index", {"index": "logs-old"})
    _approve(users, step["approval_id"])
    change = users["alice"].get("/api/trash").json()[0]
    assert users["carol"].post(f"/api/changes/{change['id']}/purge").status_code == 403
    assert users["admin"].post(f"/api/changes/{change['id']}/purge").json()["status"] == "purged"
    assert "logs-old" not in infra.indices


def _rows(sql):
    with db.session() as conn:
        tenant = conn.execute("SELECT id FROM tenant WHERE name='team-platform'").fetchone()["id"]
    from aiops import connections
    with db.session() as conn:
        _, _, _, params = connections.resolve(conn, tenant, "db.query")
    from aiops.connectors import sql as sql_connector
    return sql_connector._run(params, "postgres", sql, read_only=True, commit=False)["rows"]


def test_drop_table_is_renamed_and_restored(users, judge):
    step = _call(users, "db.execute", {"sql": "DROP TABLE orders"})
    assert step["status"] == "pending" and step["undo"]["soft"]
    _approve(users, step["approval_id"])
    tables = [r[0] for r in _rows("SELECT name FROM sqlite_master WHERE type='table'")]
    assert "orders" not in tables and any(t.startswith("_aiops_trash_orders_") for t in tables)
    change = users["alice"].get("/api/trash").json()[0]
    users["alice"].post(f"/api/changes/{change['id']}/undo", json={})
    assert _rows("SELECT count(*) FROM orders") == [[6]]


def test_delete_rows_backed_up_and_restored(users, judge):
    step = _call(users, "db.execute", {"sql": "DELETE FROM orders WHERE status='failed'"})
    assert step["undo"]["summary"].startswith("ghi lại 2 dòng từ bản sao lưu")
    _approve(users, step["approval_id"])
    assert _rows("SELECT count(*) FROM orders") == [[4]]
    change = users["alice"].get("/api/trash").json()[0]
    users["alice"].post(f"/api/changes/{change['id']}/undo", json={})
    assert _rows("SELECT id FROM orders WHERE status='failed' ORDER BY id") == [[102], [105]]


def test_update_rows_restored_and_drift_detected(users, judge, monkeypatch):
    monkeypatch.setattr(sql_undo, "primary_key", lambda cur, kind, table: ["id"])
    step = _call(users, "db.execute", {"sql": "UPDATE orders SET status='paid' WHERE status='failed'"})
    _approve(users, step["approval_id"])
    change = users["alice"].get("/api/trash").json()[0]
    from aiops.connectors import sql as sql_connector
    with db.session() as conn:
        from aiops import connections
        _, _, _, params = connections.resolve(conn, users["alice"].tenant, "db.execute")
    sql_connector._run(params, "postgres", "UPDATE orders SET amount=1 WHERE id=102", read_only=False, commit=True)
    r = users["alice"].post(f"/api/changes/{change['id']}/undo", json={})
    assert r.status_code == 409 and r.json()["detail"]["drift"] == ["1 dòng của bảng orders đã bị sửa tiếp sau lệnh"]
    users["alice"].post(f"/api/changes/{change['id']}/undo", json={"force": True})
    assert _rows("SELECT id FROM orders WHERE status='failed' ORDER BY id") == [[102], [105]]


def test_too_many_rows_not_reversible(users, judge):
    _settings(users, max_rows=1)
    step = _call(users, "db.execute", {"sql": "DELETE FROM orders WHERE status='failed'"})
    assert step["undo"]["checks"][0]["text"] == "Không hoàn tác được: lệnh đổi 2 dòng, quá ngưỡng sao lưu 1 dòng (khôi phục sẽ quá lâu)"


def test_migration_phases(users, judge):
    add = _call(users, "db.execute", {"sql": "ALTER TABLE orders ADD COLUMN note TEXT"})
    assert add["undo"]["reversible"] and add["undo"]["summary"] == "xoá cột note vừa thêm vào orders"
    drop = _call(users, "db.execute", {"sql": "ALTER TABLE orders DROP COLUMN status"})
    assert drop["status"] == "pending" and sql_undo.CONTRACT_REASON in drop["reasons"]


# ---------- silence ----------

def test_grafana_silence_undo_expires_created_one(users, infra, judge):
    step = _call(users, "grafana.create_silence", {"matchers": "alertname=HighCPU"})
    _approve(users, step["approval_id"])
    change = users["alice"].get("/api/approvals?status=approved").json()[0]["change"]
    assert change["summary"] == "gỡ đúng silence vừa tạo"
    assert users["alice"].post(f"/api/changes/{change['id']}/undo", json={}).json()["status"] == "reverted"
    assert infra.expired == ["s1", "s2"]  # s1 gỡ khi diễn tập ở dev, rồi tạo lại thành s2


# ---------- cài đặt ----------

@pytest.mark.parametrize("value", [{"verify_minutes": 0}, {"drill": {"prod": True}}, {"unknown": 1}, {"paused": "yes"}])
def test_settings_validation(users, value):
    assert users["admin"].patch("/api/tenant", json={"undo_settings": value}).status_code == 400


def test_settings_admin_only(users):
    assert users["alice"].patch("/api/tenant", json={"undo_settings": {"paused": True}}).status_code == 403


def test_delete_deployment_snapshot_recreated(users, infra, judge):
    step = _call(users, "k8s.kubectl_admin", {"command": "delete deployment/api -n default"})
    assert step["status"] == "pending" and step["undo"]["summary"] == "tạo lại deployment/api từ manifest đã chụp"
    _approve(users, step["approval_id"])
    assert "api" not in infra.deployments["default"]
    change = users["alice"].get("/api/approvals?status=approved").json()[0]["change"]
    assert users["alice"].post(f"/api/changes/{change['id']}/undo", json={}).json()["status"] == "reverted"
    assert infra.deployments["default"]["api"] == 2


def test_mysql_refuses_to_dry_run_ddl():
    from aiops.connectors import sql
    from aiops.connectors.base import ConnectorError
    with pytest.raises(ConnectorError, match="tự commit DDL"):
        sql.my_execute_check({"host": "db.test"}, {"sql": "DROP TABLE orders"})
