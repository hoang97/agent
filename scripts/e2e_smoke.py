"""Kiểm thử trọn vòng qua HTTP trên một bản đang chạy, gọi Internet thật qua kết nối Web.

Dùng: E2E_ADMIN_PASSWORD=... E2E_DEMO_PASSWORD=... python scripts/e2e_smoke.py https://example.org
Tạo dữ liệu thử trong tenant team-platform, nên chỉ chạy trên môi trường thử nghiệm.
"""
import os
import sys
import uuid

import httpx

BASE = sys.argv[1].rstrip("/")
PW = {"admin": os.environ["E2E_ADMIN_PASSWORD"]}
PW["alice"] = PW["bob"] = os.environ["E2E_DEMO_PASSWORD"]
marker = uuid.uuid4().hex[:8]
results: list[tuple[str, bool, str]] = []


def check(name, ok, info=""):
    results.append((name, bool(ok), str(info)))
    print(("PASS" if ok else "FAIL"), name, info if not ok else "")


class U:
    def __init__(self, name):
        self.c = httpx.Client(base_url=BASE, timeout=90)
        self.c.post("/api/auth/login", json={"username": name, "password": PW[name]}).raise_for_status()
        me = self.c.get("/api/me").json()
        self.c.headers["X-Tenant-Id"] = next(t["id"] for t in me["tenants"] if t["name"] == "team-platform")

    def run(self, task, env="dev"):
        r = self.c.post("/api/runs", json={"task": task, "env": env})
        r.raise_for_status()
        return r.json()


def steps(ep):
    return [s for s in ep["trace"] if s["type"] == "tool_call"]


check("health", httpx.get(BASE + "/api/health", timeout=30).json() == {"status": "ok"})
check("chặn truy cập khi chưa đăng nhập", httpx.get(BASE + "/api/me", timeout=30).status_code == 401)
alice, bob, admin = U("alice"), U("bob"), U("admin")

web_name = f"web_{marker}"
r = alice.c.post("/api/connections", json={"template": "web", "name": web_name, "scope": "user", "envs": ["dev"]})
check("user tạo kết nối cá nhân", r.status_code == 200, r.text)
ep = alice.run("Đọc trang https://example.com")
s = steps(ep)
check("đọc trang web thật qua kết nối", s and s[0]["tool"] == f"{web_name}.fetch_page" and s[0]["status"] == "ok" and "Example Domain" in s[0]["result"]["title"], s)
check("nội dung web được coi là chưa tin cậy", s and s[0].get("untrusted") is True)
check("người khác không thấy kết nối cá nhân", all(c["name"] != web_name for c in bob.c.get("/api/connections").json()))

r = alice.c.post("/api/connections", json={"template": "http_api", "name": f"loc_{marker}", "scope": "user", "params": {"base_url": "http://127.0.0.1:8000"}})
check("chặn kết nối vào loopback", r.status_code == 400 and "bị chặn" in r.text, r.text)
r = alice.c.post("/api/connections", json={"template": "http_api", "name": f"meta_{marker}", "scope": "user", "params": {"base_url": "http://169.254.169.254"}})
check("chặn địa chỉ metadata của cloud", r.status_code == 400, r.text)
r = alice.c.post("/api/connections", json={"template": "web", "name": f"shared_{marker}", "scope": "tenant"})
check("member không tạo được kết nối dùng chung", r.status_code == 403, r.text)
r = admin.c.post("/api/connections", json={"template": "http_api", "name": f"gh_{marker}", "scope": "tenant", "envs": ["dev"],
                                           "params": {"base_url": "https://api.github.com", "allowed_paths": ["/zen"]}})
check("admin tạo kết nối dùng chung", r.status_code == 200, r.text)
conn_id = r.json().get("id")
check("kiểm tra kết nối gọi API thật", admin.c.post(f"/api/connections/{conn_id}/test").json().get("ok") is True)

alice.run(f"nhớ rằng cụm {marker} dùng chung DB với QA")
learned = admin.c.post("/api/learning/run").json()
check("luồng học chạy", learned["processed"] >= 1, learned)
active = alice.c.get("/api/ku?status=active").json()
fact = next((k for k in active if marker in k["content"] and k["kind"] == "fact"), None)
check("fact do người dùng nói được thăng hạng", fact, active[:2])
ep = alice.run(f"cụm {marker} dùng DB nào")
check("agent nạp lại tri thức", fact and fact["id"] in ep["ku_used"], ep["ku_used"])
check("audit có bản ghi", any(a["tool"] == f"{web_name}.fetch_page" for a in alice.c.get("/api/audit").json()))

for c in alice.c.get("/api/connections").json():
    if c["name"] == web_name:
        alice.c.delete(f"/api/connections/{c['id']}")
if conn_id:
    admin.c.delete(f"/api/connections/{conn_id}")
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} đạt")
sys.exit(1 if failed else 0)
