# Knowledge Agent

Agent tự tích lũy tri thức, có gateway phân quyền công cụ và giao diện web. Thiết kế gốc nằm ở
`Kiến trúc tích lũy tri thức tự động cho AI Agent.md`; phạm vi MVP và các điểm khác thiết kế ở `docs/PLAN.md`;
lịch sử cải tiến ở `docs/IMPROVEMENTS.md`.

Agent dùng công cụ thật (Kubernetes, OpenStack, Grafana, Prometheus, Alertmanager, Elasticsearch/Kibana, Web, HTTP API, PostgreSQL, MySQL). Admin hoặc user tạo **kết nối** từ template
bằng tham số của riêng mình ở trang **Kết nối công cụ**; chi tiết ở `docs/TOOLS.md`.

## Tri thức

Tri thức đến từ ba nguồn: lượt chat (Reflector → Curator → cổng duyệt), tài liệu tải lên ở **Tri thức > Nguồn** (runbook,
postmortem, ghi chú markdown) và sự cố đã xử lý xong. Mỗi KU trỏ về nguồn của nó; tài liệu được giữ nguyên bản, KU ghi đoạn
đã tách ra. Compiler dựng playbook, quy ước core và trang cho từng thực thể (`entities/*.md`); agent tự tra giữa lượt bằng
`search_knowledge` và `read_knowledge_page`. Vòng nền kiểm tra kho mỗi ngày: loại ứng viên quá hạn, nối KU cùng chủ đề,
gợi ý gộp tên thực thể, gộp KU trùng ý (nhóm chỉ toàn ứng viên tự gộp, bản gộp vẫn là ứng viên; gộp KU đang dùng thì chờ admin duyệt). Biến liên quan: `AIOPS_LINT_STALE_DAYS` (mặc định 7), `AIOPS_INCIDENT_LEARN` (mặc định 1).

## Chạy cục bộ

Cần Python 3.10 trở lên.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q -n auto                      # cả bộ, song song; khi sửa thì chỉ chạy file test liên quan
AIOPS_ADMIN_PASSWORD=<mật-khẩu> AIOPS_DEMO_PASSWORD=<mật-khẩu> .venv/bin/uvicorn aiops.api:app --port 8000
```

Tài khoản mẫu: `admin` (quản trị), `alice` (member), `bob` và `carol` (approver). Không đặt biến mật khẩu thì ứng dụng
sinh mật khẩu ngẫu nhiên vào `data/initial-credentials.txt`. API docs ở `/api/docs`.

## Chạy bằng Docker Compose

```bash
printf 'AIOPS_SECRET_KEY=%s\n' "$(openssl rand -base64 32 | tr '+/' '-_')" > .env   # chỉ lần đầu
docker compose up -d --build
docker compose exec aiops cat /data/initial-credentials.txt
```

Mở `http://127.0.0.1:8000`. Dữ liệu nằm trong volume `aiops-data`. Mặc định image gốc lấy từ Docker Hub, pip từ PyPI,
kubectl từ dl.k8s.io. Mạng nội bộ không ra được Internet thì trỏ sang mirror trong `.env` (đã gitignore):
`AIOPS_BASE_IMAGE`, `AIOPS_PIP_INDEX_URL`, `AIOPS_PIP_TRUSTED_HOST`, `AIOPS_KUBECTL_URL`.
Các biến khác: `AIOPS_BIND=0.0.0.0` để mở cho LAN, `AIOPS_PORT`, `ANTHROPIC_API_KEY`, `AIOPS_ADMIN_PASSWORD`, `AIOPS_DEMO_PASSWORD`.

## Bật LLM

Org admin chọn nhà cung cấp (Anthropic, relay Anthropic, OpenAI hoặc dịch vụ tương thích OpenAI) và nhập key ở tab **Cài đặt**.
OpenAI bắt buộc nhập tên model. Thông tin được mã hóa khi lưu, ưu tiên hơn biến môi trường `ANTHROPIC_API_KEY`.
Thiếu khóa, agent chạy chế độ heuristic: lập kế hoạch theo từ khóa, không gọi LLM.
Model mặc định: `claude-opus-5-5` cho agent, Reflector, Curator; `claude-haiku-5-5` cho router và kiểm tra độc lập
(đổi bằng `AIOPS_MODEL_STRONG`, `AIOPS_MODEL_FAST`). Yêu cầu tới Opus bật server-side fallback khi model từ chối.

## Triển khai lên Ubuntu

```bash
sudo DOMAIN=<tên-miền> REPO_DIR=$PWD bash deploy/install.sh
```

Cài systemd service `aiops` và Caddy (HTTPS tự động). Cấu hình ở `/etc/aiops/aiops.env`, dữ liệu ở `/opt/aiops/data`.
