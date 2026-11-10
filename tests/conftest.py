import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fakes
from kagent import api as api_mod
from kagent.config import settings
from kagent import llm

PASSWORD = "test-password"


@pytest.fixture()
def client():
    tmp = Path(tempfile.mkdtemp(prefix="kagent-test-"))
    settings.configure(data_dir=tmp, admin_password=PASSWORD, demo_password=PASSWORD, scheduler_enabled=False, llm_provider="heuristic", anthropic_key="")
    api_mod._failures.clear()
    llm.set_provider(None)
    with TestClient(api_mod.app) as c:
        yield c
    llm.set_provider(None)
    shutil.rmtree(tmp, ignore_errors=True)


class Session:
    """Một người dùng đã đăng nhập, gắn với một tenant."""

    def __init__(self, client, username, tenant_name="team-platform"):
        self.c = client
        self.username = username
        self.cookie = None
        r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.cookie = r.cookies["kagent_session"]
        client.cookies.clear()
        me = self.get("/api/me", tenant=False).json()
        self.tenant = next(t["id"] for t in me["tenants"] if t["name"] == tenant_name) if me["tenants"] else None

    def _call(self, method, path, tenant=True, **kw):
        headers = {"X-Tenant-Id": self.tenant} if tenant else {}
        self.c.cookies.clear()
        self.c.cookies.set("kagent_session", self.cookie)
        r = self.c.request(method, path, headers=headers, **kw)
        self.c.cookies.clear()
        return r

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, **kw):
        return self._call("POST", path, **kw)

    def put(self, path, **kw):
        return self._call("PUT", path, **kw)

    def patch(self, path, **kw):
        return self._call("PATCH", path, **kw)

    def run(self, task, env="dev"):
        r = self.post("/api/runs", json={"task": task, "env": env})
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture(autouse=True)
def infra(monkeypatch, tmp_path):
    """Mọi test chạy connector thật trên hạ tầng giả: không DNS, không mạng, không CSDL thật."""
    from kagent import net
    from kagent.connectors import k8s as k8s_connector, sql as sql_connector
    fake = fakes.Infra()
    monkeypatch.setattr(net, "resolve", fakes.resolve)
    monkeypatch.setattr(net, "transport_override", fake.transport)
    monkeypatch.setattr(sql_connector, "_open", fakes.sqlite_open(tmp_path / "shop.db"))
    monkeypatch.setattr(k8s_connector, "_exec_ws", fakes.exec_ws)
    return fake


def create_connections(tenant_id: str, enable_all: bool = True) -> None:
    from kagent import connections, db
    with db.session() as conn:
        admin = conn.execute("SELECT id FROM app_user WHERE username='admin'").fetchone()["id"]
        for name, template, params, secrets in fakes.CONNECTIONS:
            connections.create(conn, tenant_id, admin, is_admin=True, is_org_admin=True, template=template, name=name, scope="tenant",
                               envs=["dev", "staging", "prod"], roles=["member"], params=params, secrets=secrets, enable_all=enable_all)


@pytest.fixture()
def users(client):
    sessions = {name: Session(client, name) for name in ("admin", "alice", "bob", "carol")}
    create_connections(sessions["admin"].tenant)
    return sessions


def tool_steps(ep):
    return [s for s in ep["trace"] if s["type"] == "tool_call"]
