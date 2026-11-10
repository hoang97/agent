# Kế hoạch triển khai — Knowledge Agent MVP

Nguồn yêu cầu: `Kiến trúc tích lũy tri thức tự động cho AI Agent.md` (gọi tắt: *tài liệu kiến trúc*).

## Mục tiêu MVP

Một ứng dụng web chạy được, có giao diện, thể hiện trọn vòng lặp của tài liệu kiến trúc:
agent làm việc → ghi nhật ký → học nền → tri thức được kiểm duyệt → biên dịch → agent lần sau dùng lại;
mọi lệnh gọi công cụ đi qua gateway có phân mức rủi ro, multi-tenant theo team.

## Ánh xạ tài liệu kiến trúc → mã nguồn

| Thành phần | Tài liệu kiến trúc | MVP | File |
| --- | --- | --- | --- |
| L0 Episode | Bảng chỉ ghi thêm | Trigger SQLite chặn sửa/xóa phần bất biến | `kagent/db.py` |
| L1 KU, `ku_edge`, `ku_event` | Nguyên tử, hai trục thời gian, audit | Đầy đủ các trường, rollback theo `ku_event` | `kagent/knowledge.py` |
| L2 Tri thức biên dịch | Repo git, playbook có ID + bộ đếm, Skill | Repo git riêng mỗi tenant, nhánh `proposal/*` thay cho pull request | `kagent/compiler.py` |
| L3 Chỉ mục | Vector + BM25 + đồ thị | Vector băm + FTS5 BM25 + mở rộng 1 bước trên `ku_edge` | `kagent/retrieval.py` |
| Luồng đọc | Router → truy xuất → lắp ngữ cảnh theo ngân sách → ghi episode | Như thiết kế | `kagent/agent.py` |
| Luồng học | Reflector → Curator → Cổng duyệt → Compiler | Như thiết kế, chạy nền theo lịch và theo sự kiện | `kagent/learning.py`, `kagent/scheduler.py` |
| Đo lường | Golden task, chỉ số tuần | Golden task chặn publish, trang Đo lường | `kagent/compiler.py`, `kagent/api.py` |
| MCP Gateway | Danh mục, kiểm quyền, audit | Danh mục + 3 cửa quyết định + audit; **backend công cụ là mô phỏng** | `kagent/gateway.py`, `kagent/tools.py` |
| Phân cấp quyền | R0–R4 × môi trường × tin cậy | Ma trận, `auto_execute`, điểm tin cậy 4 thành phần, ép hỏi | `kagent/gateway.py` |
| Phê duyệt | Card, 3 lựa chọn, 30 phút, không tự duyệt | Như thiết kế; R3 prod cần 2 người | `kagent/gateway.py` |
| Multi-tenant | RLS PostgreSQL, overlay chỉ siết | Lọc `tenant_id` ở tầng truy vấn, overlay chỉ siết | `kagent/gateway.py`, `kagent/api.py` |

## Quyết định kỹ thuật và điểm khác tài liệu kiến trúc

| Quyết định | Lý do | Đường nâng cấp |
| --- | --- | --- |
| SQLite (WAL + FTS5) thay PostgreSQL + pgvector | Máy chủ thử nghiệm một node, không phụ thuộc dịch vụ ngoài, dễ sao lưu | Thay `db.py`; schema giữ nguyên tên bảng và cột, thêm RLS |
| Embedding băm cục bộ 256 chiều | Không cần API embedding; đủ cho so khớp từ vựng | Thay `embed()` bằng model embedding, đặt lại ngưỡng cosine về 0,85 |
| Tách `tenant_id` ở tầng truy vấn thay RLS | SQLite không có RLS; có test cô lập tenant | Bật RLS khi chuyển PostgreSQL |
| Công cụ MCP là bản mô phỏng | Chưa có cụm k8s/Grafana thật; không để agent công khai chạm hạ tầng thật | Thay hàm `run` trong `tools.py` bằng client MCP |
| Hai LLM provider: Claude và heuristic | Chạy được khi chưa có API key; provider heuristic cho kết quả xác định để kiểm thử | Đặt `ANTHROPIC_API_KEY` |
| Sau khi duyệt lệnh, chỉ thực thi lệnh đó; kết quả vào lịch sử của lượt chat sau | Giữ MVP đơn giản, không tiếp tục vòng lặp LLM | Thêm resume theo `approval_request` |
| FastAPI + JS thuần, không bước build | Triển khai đơn giản | — |

## Giai đoạn và commit

1. Tài liệu kế hoạch.
2. Khung dự án, cấu hình, schema dữ liệu.
3. Tri thức L1, truy xuất L3.
4. Provider LLM.
5. Gateway, công cụ, chính sách.
6. Agent và luồng đọc.
7. Luồng học, Compiler.
8. API, xác thực.
9. Giao diện.
10. Kiểm thử tự động.
11. Triển khai: systemd, reverse proxy HTTPS.
12. Kiểm thử trên bản triển khai, sửa lỗi; mỗi đợt ghi vào `docs/IMPROVEMENTS.md`.

## Chạy và kiểm thử

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
KAGENT_ADMIN_PASSWORD=doi-mat-khau .venv/bin/uvicorn kagent.api:app --port 8000
```
