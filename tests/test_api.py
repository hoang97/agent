from conftest import PASSWORD, Session
from aiops import db, knowledge


def test_requires_login(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/episodes", headers={"X-Tenant-Id": "x"}).status_code == 401


def test_health_is_public(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_wrong_password_throttled(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "alice", "password": "sai"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429


def test_cookie_flags(client):
    r = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_logout_invalidates_session(users):
    alice = users["alice"]
    assert alice.get("/api/me", tenant=False).status_code == 200
    alice.post("/api/auth/logout", tenant=False)
    assert alice.get("/api/me", tenant=False).status_code == 401


NEW_PASSWORD = "mat-khau-moi-du-dai"


def test_change_password_rotates_credentials_and_other_sessions(client, users):
    alice = users["alice"]
    other = Session(client, "alice")
    r = alice.post("/api/auth/password", tenant=False, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    assert alice.get("/api/me", tenant=False).status_code == 200
    assert other.get("/api/me", tenant=False).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": NEW_PASSWORD}).status_code == 200


def test_change_password_validation(users):
    alice = users["alice"]
    url = "/api/auth/password"
    assert alice.post(url, tenant=False, json={"current_password": "sai", "new_password": NEW_PASSWORD}).status_code == 403
    assert alice.post(url, tenant=False, json={"current_password": PASSWORD, "new_password": "ngan"}).status_code == 422
    assert alice.post(url, tenant=False, json={"current_password": PASSWORD, "new_password": PASSWORD}).status_code in (400, 422)


def test_change_password_requires_login(client):
    assert client.post("/api/auth/password", json={"current_password": "a", "new_password": NEW_PASSWORD}).status_code == 401


def test_change_password_throttled(users):
    alice = users["alice"]
    for _ in range(5):
        alice.post("/api/auth/password", tenant=False, json={"current_password": "sai", "new_password": NEW_PASSWORD})
    r = alice.post("/api/auth/password", tenant=False, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 429


def test_security_headers(client):
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"


def test_ui_assets_served(client):
    assert "<title>aiops</title>" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_tenant_isolation(client, users):
    admin = users["admin"]
    alice = users["alice"]
    finance = Session(client, "admin", "team-finance")
    with db.session() as conn:
        ku = knowledge.add(conn, finance.tenant, kind="fact", title="Bí mật tài chính", content="Lương quý ba", source_eps=[], source_trust="user", status="active")
    ep = finance.run("xem pod")

    # alice không phải thành viên team-finance
    alice_tenant = alice.tenant
    alice.tenant = finance.tenant
    assert alice.get("/api/ku").status_code == 403
    assert alice.get("/api/episodes").status_code == 403
    alice.tenant = alice_tenant

    # Dù đúng tenant của mình, id thuộc tenant khác vẫn không truy cập được
    assert alice.get(f"/api/ku/{ku['id']}").status_code == 404
    assert alice.get(f"/api/episodes/{ep['id']}").status_code == 404
    assert all(k["id"] != ku["id"] for k in admin.get("/api/ku").json())

    # truy xuất của agent không lẫn tenant
    run = alice.run("lương quý ba tài chính")
    assert ku["id"] not in run["ku_used"]


def test_cross_tenant_approval_rejected(client, users):
    finance = Session(client, "admin", "team-finance")
    step = next(s for s in users["alice"].run("scale deployment web lên 5 replica")["trace"] if s["type"] == "tool_call")
    assert finance.post(f"/api/approvals/{step['approval_id']}/decide", json={"action": "approve_once"}).status_code == 404


def test_org_scope_ku_visible_across_tenants(client, users):
    finance = Session(client, "admin", "team-finance")
    with db.session() as conn:
        ku = knowledge.add(conn, finance.tenant, kind="fact", title="Chính sách chung", content="Chuẩn đặt tên zeta áp dụng toàn tổ chức", scope="org", source_eps=[], source_trust="user", status="active")
    assert ku["id"] in users["alice"].run("chuẩn đặt tên zeta")["ku_used"]


def test_invalid_env_rejected(users):
    assert users["alice"].post("/api/runs", json={"task": "x", "env": "qa"}).status_code == 400


def test_token_quota_blocks_runs(users):
    alice = users["alice"]
    with db.session() as conn:
        conn.execute("UPDATE tenant SET token_quota_daily=0 WHERE id=?", (alice.tenant,))
    assert alice.post("/api/runs", json={"task": "xem pod"}).status_code == 429


def test_org_admin_sets_quota_per_tenant_and_others_only_read(users):
    admin, alice = users["admin"], users["alice"]
    q = alice.get("/api/quota").json()
    assert q["token_quota_daily"] == 2000000 and q["tokens_today"] == 0 and not q["can_manage"] and q["resets_at"].endswith("T00:00:00+00:00")
    assert alice.get("/api/quotas").status_code == 403
    assert alice.put(f"/api/quotas/{alice.tenant}", json={"token_quota_daily": 5, "write_quota_daily": 5}).status_code == 403
    alice.run("xem pod")
    items = {i["name"]: i for i in admin.get("/api/quotas").json()["items"]}
    assert set(items) == {"team-platform", "team-finance"} and items["team-platform"]["tokens_7d"] == items["team-platform"]["tokens_today"]
    finance = items["team-finance"]["tenant_id"]
    assert admin.put(f"/api/quotas/{finance}", json={"token_quota_daily": -1, "write_quota_daily": 1}).status_code == 422
    assert admin.put("/api/quotas/khong-co", json={"token_quota_daily": 1, "write_quota_daily": 1}).status_code == 404
    row = admin.put(f"/api/quotas/{alice.tenant}", json={"token_quota_daily": 0, "write_quota_daily": 7}).json()
    assert (row["token_quota_daily"], row["write_quota_daily"]) == (0, 7)
    assert {i["name"]: i["token_quota_daily"] for i in admin.get("/api/quotas").json()["items"]} == {"team-platform": 0, "team-finance": 2000000}
    assert alice.post("/api/runs", json={"task": "xem pod"}).status_code == 429


def test_metrics_shape(users):
    alice = users["alice"]
    alice.run("xem pod")
    m = alice.get("/api/metrics").json()
    assert m["episodes"] == 1 and m["labeled_pct"] == 100.0 and m["weekly"][0]["tasks"] == 1



def test_save_answer_as_knowledge(users):
    alice = users["alice"]
    plain = alice.run("chào bạn")
    queried = alice.run("chạy `SELECT count(*) FROM orders`")
    assert not any(s.get("untrusted") for s in plain["trace"]) and any(s.get("untrusted") for s in queried["trace"])
    body = {"kind": "fact", "title": "Đếm đơn", "content": "Bảng orders có 6 đơn ở staging", "scope": "tenant"}
    ku = alice.post(f"/api/episodes/{plain['id']}/knowledge", json=body).json()
    assert ku["status"] == "candidate" and ku["source_trust"] == "user" and ku["source_eps"] == [plain["id"]]
    tainted = alice.post(f"/api/episodes/{queried['id']}/knowledge", json=body).json()
    assert tainted["source_trust"] == "external_untrusted" and tainted["status"] == "candidate"  # không rửa nội dung ngoài thành lời người dùng
    mine = alice.post(f"/api/episodes/{queried['id']}/knowledge", json={**body, "scope": "user"}).json()
    assert mine["status"] == "active" and mine["scope"].startswith("user:") and mine["source_trust"] == "user"
    assert alice.post("/api/episodes/khong-co/knowledge", json=body).status_code == 404
    assert alice.post(f"/api/episodes/{plain['id']}/knowledge", json={**body, "kind": "x"}).status_code == 400
