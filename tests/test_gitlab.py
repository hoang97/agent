"""Connector GitLab trên GitLab giả (cài dưới đường dẫn con /gitlab)."""
import pytest

import fakes
from aiops import connections, db, gateway, llm
from aiops.connectors import gitlab
from aiops.connectors.base import ConnectorError

GL = {"_allow_private": False, "url": "https://gitlab.test/gitlab/", "token": fakes.GITLAB_TOKEN, "default_project": "team/shop"}


def test_reads(infra):
    assert gitlab.ping(GL) == {"ok": True, "version": "17.5.0", "user": "aiops-bot"}
    assert gitlab.list_projects(GL, {"search": "shop"})["projects"][0]["path"] == "team/shop"
    assert gitlab.list_pipelines(GL, {"ref": "release/1.2"})["pipelines"][0] == {
        "id": 11, "ref": "release/1.2", "status": "failed", "source": "push", "sha": "abcdef12", "created_at": None, "url": None}
    pipe = gitlab.get_pipeline(GL, {"pipeline_id": 11})
    assert pipe["user"] == "dev1" and [(j["name"], j["status"], j["environment"]) for j in pipe["jobs"]] == [
        ("test", "failed", None), ("deploy-prod", "manual", "production")]
    log = gitlab.job_log(GL, {"job_id": 21, "contains": "fail"})
    assert log == {"job_id": 21, "total_lines": 1, "log": "FAIL db timeout"}
    assert gitlab.job_log(GL, {"job_id": "21"})["log"].startswith("$ make test\nok 1")
    assert gitlab.list_deployments(GL, {"environment": "production"})["deployments"][0]["job_id"] == 19
    assert gitlab.get_file(GL, {"path": "/.gitlab-ci.yml", "ref": "main"})["content"] == "stages: [test]\n# ref=main\n"
    assert gitlab.api_read(GL, {"path": "/api/v4/projects/7/environments"})["json"] == [{"id": 1, "name": "production"}]
    assert ("GET", "/gitlab/api/v4/projects/team%2Fshop/pipelines") in infra.gl_calls


@pytest.mark.parametrize("args", [{"path": "/projects/7/variables"}, {"path": "/projects/7/hooks/1"}, {"path": "/runners?scope=all"},
                                  {"path": "projects"}, {"path": "/projects/../user"}, {"path": "https://evil.test/x"}])
def test_api_read_refuses_secrets_and_odd_paths(infra, args):
    with pytest.raises(ConnectorError):
        gitlab.api_read(GL, args)


@pytest.mark.parametrize("args", [{"project": "../x", "pipeline_id": 1}, {"pipeline_id": "1;2"}, {"project": "a b/c", "pipeline_id": 1}])
def test_bad_ids(infra, args):
    with pytest.raises(ConnectorError):
        gitlab.get_pipeline(GL, args)


def test_writes_and_errors(infra):
    assert gitlab.retry_pipeline(GL, {"pipeline_id": 11})["status"] == "running"
    assert gitlab.cancel_pipeline(GL, {"project": "team/shop", "pipeline_id": 11})["status"] == "canceled"
    assert gitlab.play_job(GL, {"job_id": 22}) == {"job_id": 22, "name": "deploy-prod", "status": "pending", "url": None}
    assert gitlab.create_note(GL, {"target": "merge_request", "iid": 3, "body": "LGTM"}) == {"note_id": 101, "target": "merge_request !3"}
    with pytest.raises(ConnectorError, match="target"):
        gitlab.create_note(GL, {"target": "commit", "iid": 3, "body": "x"})
    with pytest.raises(ConnectorError, match="401"):
        gitlab.ping({**GL, "token": "sai"})
    with pytest.raises(ConnectorError, match="thiếu tham số project"):
        gitlab.list_pipelines({**GL, "default_project": ""}, {})


class Allow:
    def classify_action(self, data):
        return {"allow": True, "rule": "", "reason": ""}


def test_note_undo_through_gateway(users, infra, monkeypatch):
    monkeypatch.setattr(llm, "get_provider", lambda: Allow())
    tenant = users["admin"].tenant
    with db.session() as conn:
        admin = conn.execute("SELECT id FROM app_user WHERE username='admin'").fetchone()["id"]
        connections.create(conn, tenant, admin, is_admin=True, is_org_admin=True, template="gitlab", name="gl", scope="tenant",
                           envs=["dev", "staging", "prod"], roles=["member"], params={"url": GL["url"], "default_project": "team/shop"},
                           secrets={"token": fakes.GITLAB_TOKEN}, enable_all=True)
        uid = conn.execute("SELECT id FROM app_user WHERE username='alice'").fetchone()["id"]
        ctx = gateway.CallContext(tenant, uid, "dev", "comment LGTM vào MR 3", intent=["comment LGTM vào MR 3"])
        step = gateway.invoke(conn, ctx, "gl.create_note", {"target": "merge_request", "iid": 3, "body": "LGTM"})
    assert step["status"] == "pending" and step["undo"]["summary"] == "xoá đúng comment vừa tạo trên MR !3"
    r = users["alice"].post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"})
    assert r.status_code == 200, r.text
    assert list(infra.gl_notes) == [102]  # 101 bị xoá khi diễn tập ở dev, rồi tạo lại thành 102
    change = users["alice"].get("/api/approvals?status=approved").json()[0]["change"]
    infra.gl_notes[102]["updated_at"] = "sửa tay"
    r = users["alice"].post(f"/api/changes/{change['id']}/undo", json={})
    assert r.status_code == 409 and "đã được sửa" in r.text
    assert users["alice"].post(f"/api/changes/{change['id']}/undo", json={"force": True}).json()["status"] == "reverted"
    assert infra.gl_notes == {}
