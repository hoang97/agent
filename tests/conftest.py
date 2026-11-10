import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


@pytest.fixture()
def users(client):
    return {name: Session(client, name) for name in ("admin", "alice", "bob", "carol")}


def tool_steps(ep):
    return [s for s in ep["trace"] if s["type"] == "tool_call"]
