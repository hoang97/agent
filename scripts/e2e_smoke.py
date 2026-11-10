"""Kiểm thử trọn vòng qua HTTP trên một bản đang chạy.

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
        self.c = httpx.Client(base_url=BASE, timeout=60)
        r = self.c.post("/api/auth/login", json={"username": name, "password": PW[name]})
        r.raise_for_status()
        me = self.c.get("/api/me").json()
        self.provider = me["llm_provider"]
        self.c.headers["X-Tenant-Id"] = next(t["id"] for t in me["tenants"] if t["name"] == "team-platform")

    def run(self, task, env="dev"):
        r = self.c.post("/api/runs", json={"task": task, "env": env})
        r.raise_for_status()
        return r.json()


def steps(ep):
    return [s for s in ep["trace"] if s["type"] == "tool_call"]


check("health", httpx.get(BASE + "/api/health", timeout=30).json() == {"status": "ok"})
check("chặn truy cập khi chưa đăng nhập", httpx.get(BASE + "/api/me", timeout=30).status_code == 401)
check("giao diện", "Knowledge Agent" in httpx.get(BASE + "/", timeout=30).text)

alice, bob, admin = U("alice"), U("bob"), U("admin")
print("provider:", alice.provider)

ep = alice.run("xem pod trong namespace default")
check("đọc R0 tự chạy", steps(ep) and steps(ep)[0]["status"] == "ok", ep["trace"])

ep = alice.run("scale deployment api lên 3 replica trong namespace payments")
s = steps(ep)[0]
check("lệnh ghi ở namespace mới phải chờ duyệt", s["status"] == "pending", s)
pending = next(a for a in alice.c.get("/api/approvals?status=pending").json() if a["id"] == s["approval_id"])
check("người yêu cầu được tự quyết định lệnh của mình", pending["can_decide"] and pending["mine"], pending)
r = alice.c.post(f"/api/approvals/{s['approval_id']}/decide", json={"action": "deny", "reason": f"lý do {marker}"})
check("từ chối kèm lý do", r.json().get("status") == "denied", r.text)

ep = alice.run("đọc secret của app web")
check("đọc secret bị chặn cứng", steps(ep)[0]["decision"] == "blocked", steps(ep))

alice.run(f"nhớ rằng cụm {marker} dùng chung DB với QA")
learned = admin.c.post("/api/learning/run").json()
check("luồng học chạy", learned["processed"] >= 1, learned)
active = alice.c.get("/api/ku?status=active").json()
fact = next((k for k in active if marker in k["content"] and k["kind"] == "fact"), None)
check("fact do người dùng nói được thăng hạng", fact, active[:2])
lessons = alice.c.get("/api/ku?kind=lesson").json()
check("lý do từ chối thành lesson", any(marker in k["content"] for k in lessons))

ep = alice.run(f"cụm {marker} dùng DB nào")
check("agent nạp lại tri thức", fact and fact["id"] in ep["ku_used"], ep["ku_used"])

run = admin.c.post("/api/compile").json()
check("Compiler chạy", run.get("status") in ("published", "pending_review", "noop"), run)
check("trang đo lường", alice.c.get("/api/metrics").json()["episodes"] >= 5)
check("audit có bản ghi", len(alice.c.get("/api/audit").json()) > 0)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} đạt")
sys.exit(1 if failed else 0)
