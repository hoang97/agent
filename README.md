# Knowledge Agent

Agent tự tích lũy tri thức, có gateway phân quyền công cụ và giao diện web. Thiết kế gốc nằm ở
`Kiến trúc tích lũy tri thức tự động cho AI Agent.md`; phạm vi MVP và các điểm khác thiết kế ở `docs/PLAN.md`;
lịch sử cải tiến ở `docs/IMPROVEMENTS.md`.

Agent dùng công cụ thật (Kubernetes, Grafana, Elasticsearch/Kibana, Web, HTTP API, PostgreSQL, MySQL). Admin hoặc user tạo **kết nối** từ template
bằng tham số của riêng mình ở trang **Kết nối công cụ**; chi tiết ở `docs/TOOLS.md`.

## Chạy cục bộ

Cần Python 3.10 trở lên.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
KAGENT_ADMIN_PASSWORD=<mật-khẩu> KAGENT_DEMO_PASSWORD=<mật-khẩu> .venv/bin/uvicorn kagent.api:app --port 8000
```

Tài khoản mẫu: `admin` (quản trị), `alice` (member), `bob` và `carol` (approver). Không đặt biến mật khẩu thì ứng dụng
sinh mật khẩu ngẫu nhiên vào `data/initial-credentials.txt`. API docs ở `/api/docs`.

## Bật LLM

Org admin chọn nhà cung cấp (Anthropic, relay Anthropic, OpenAI hoặc dịch vụ tương thích OpenAI) và nhập key ở tab **Cài đặt**.
OpenAI bắt buộc nhập tên model. Thông tin được mã hóa khi lưu, ưu tiên hơn biến môi trường `ANTHROPIC_API_KEY`.
Thiếu khóa, agent chạy chế độ heuristic: lập kế hoạch theo từ khóa, không gọi LLM.
Model mặc định: `claude-opus-5-5` cho agent, Reflector, Curator; `claude-haiku-5-5` cho router và kiểm tra độc lập
(đổi bằng `KAGENT_MODEL_STRONG`, `KAGENT_MODEL_FAST`). Yêu cầu tới Opus bật server-side fallback khi model từ chối.

## Triển khai lên Ubuntu

```bash
sudo DOMAIN=<tên-miền> REPO_DIR=$PWD bash deploy/install.sh
```

Cài systemd service `kagent` và Caddy (HTTPS tự động). Cấu hình ở `/etc/kagent/kagent.env`, dữ liệu ở `/opt/kagent/data`.
