# Kế hoạch triển khai — Knowledge Agent MVP

Nguồn yêu cầu: `Kiến trúc tích lũy tri thức tự động cho AI Agent.md` (gọi tắt: *tài liệu kiến trúc*).

## Mục tiêu MVP

Một ứng dụng web chạy được, có giao diện, thể hiện trọn vòng lặp của tài liệu kiến trúc:
agent làm việc → ghi nhật ký → học nền → tri thức được kiểm duyệt → biên dịch → agent lần sau dùng lại;
mọi lệnh gọi công cụ đi qua gateway có phân mức rủi ro, multi-tenant theo team.

## Ánh xạ tài liệu kiến trúc → mã nguồn

| Thành phần | Tài liệu kiến trúc | MVP | File |
| --- | --- | --- | --- |
| L0 Episode | Bảng chỉ ghi thêm | Trigger SQLite chặn sửa/xóa phần bất biến | `aiops/db.py` |
| L1 KU, `ku_edge`, `ku_event` | Nguyên tử, hai trục thời gian, audit | Đầy đủ các trường, rollback theo `ku_event` | `aiops/knowledge.py` |
| Nguồn ngoài episode | — (thêm sau, theo ý tưởng "LLM wiki") | Tài liệu tải lên và sự cố đã xử lý xong lưu nguyên bản ở `source_doc`, LLM tách KU từng đoạn, đi qua Curator và cổng duyệt; `source_eps` ghi `doc:<id>#<đoạn>` | `aiops/sources.py` |
| Bảo trì kho | — (thêm sau) | Mỗi ngày: loại ứng viên quá hạn, nối cạnh `related`, gợi ý gộp thực thể, báo KU không dùng và mâu thuẫn còn treo; gộp KU trùng theo cụm trang thực thể (nhóm chỉ toàn ứng viên tự gộp, gộp KU đang dùng chờ admin duyệt) | `aiops/lint.py`, `aiops/dedup.py` |
| L2 Tri thức biên dịch | Repo git, playbook có ID + bộ đếm, Skill | Repo git riêng mỗi tenant, nhánh `proposal/*` thay cho pull request; thêm trang thực thể (KU theo loại, sự cố đã gặp, liên quan) và `entities/index.md` | `aiops/compiler.py` |
| L3 Chỉ mục | Vector + BM25 + đồ thị | Vector băm + FTS5 BM25 + mở rộng 1 bước trên `ku_edge`; cạnh `related` do Curator ghi theo IDF thực thể chung | `aiops/retrieval.py`, `aiops/knowledge.py` |
| Luồng đọc | Router → truy xuất → lắp ngữ cảnh theo ngân sách → ghi episode | Như thiết kế; agent tra thêm giữa lượt bằng `search_knowledge`, `read_knowledge_page` | `aiops/agent.py`, `aiops/conversation.py` |
| Luồng học | Reflector → Curator → Cổng duyệt → Compiler | Như thiết kế, chạy nền theo lịch và theo sự kiện; episode chưa có kết quả được LLM-as-judge chấm trước khi phản tư | `aiops/learning.py`, `aiops/scheduler.py` |
| Đo lường | Golden task, chỉ số tuần | Golden task chặn publish, trang Đo lường | `aiops/compiler.py`, `aiops/api.py` |
| MCP Gateway | Danh mục, kiểm quyền, audit | Template công cụ (git) + kết nối do admin/user tạo với tham số riêng, gắn tenant/user/môi trường; connector gọi hệ thống thật qua lớp mạng chống SSRF; secret mã hóa; 3 cửa quyết định, audit | `aiops/templates/`, `aiops/connectors/`, `aiops/connections.py`, `aiops/net.py`, `aiops/gateway.py`, `docs/TOOLS.md` |
| Phân cấp quyền | R0–R4 × môi trường × tin cậy | Ma trận, `auto_execute`, ép hỏi; **khác thiết kế:** điểm tin cậy và đánh dấu nội dung untrusted được thay bằng auto mode (classifier chỉ đọc ý người dùng và lệnh đã gọi, xem `docs/TOOLS.md`) | `aiops/gateway.py`, `aiops/automode.py` |
| Phê duyệt | Card, 3 lựa chọn, 30 phút, không tự duyệt | **Khác thiết kế theo quyết định của chủ dự án (2026-11-11):** người yêu cầu tự duyệt lệnh của mình ngay trong chat, một xác nhận là đủ. Admin có thể tắt theo tenant để quay về luật cũ (không tự duyệt, R3 prod cần 2 người). Thay đổi công cụ dùng chung vẫn cần admin | `aiops/gateway.py` |
| Skill / thủ tục chín | Biên dịch procedure thành Skill | Thêm công cụ học được: chuỗi bước tự đề xuất hoặc lưu từ chat, chạy qua gateway | `aiops/learned.py` |
| Phạm vi `user:<id>` | Tri thức theo người dùng | Hồ sơ cá nhân, ký ức cá nhân riêng tư, không vào L2 chung | `aiops/agent.py`, `aiops/learning.py` |
| Multi-tenant | RLS PostgreSQL, overlay chỉ siết | Lọc `tenant_id` ở tầng truy vấn, overlay chỉ siết | `aiops/gateway.py`, `aiops/api.py` |

## Quyết định kỹ thuật và điểm khác tài liệu kiến trúc

| Quyết định | Lý do | Đường nâng cấp |
| --- | --- | --- |
| SQLite (WAL + FTS5) thay PostgreSQL + pgvector | Máy chủ thử nghiệm một node, không phụ thuộc dịch vụ ngoài, dễ sao lưu | Thay `db.py`; schema giữ nguyên tên bảng và cột, thêm RLS |
| Embedding băm cục bộ 256 chiều | Không cần API embedding; đủ cho so khớp từ vựng | Thay `embed()` bằng model embedding, đặt lại ngưỡng cosine về 0,85 |
| Tách `tenant_id` ở tầng truy vấn thay RLS | SQLite không có RLS; có test cô lập tenant | Bật RLS khi chuyển PostgreSQL |
| Connector gọi thẳng REST API của từng hệ thống thay vì qua MCP server | Ít thành phần phải vận hành; cùng lớp gateway, audit và phân quyền | Thêm template kiểu `mcp` gọi MCP server qua HTTP |
| Hai LLM provider: Claude và heuristic | Chạy được khi chưa có API key; provider heuristic cho kết quả xác định để kiểm thử | Đặt `ANTHROPIC_API_KEY` |
| Sau khi mọi lệnh chờ của một lượt đã được quyết định, agent tự chạy một lượt tiếp với kết quả đầy đủ của các lệnh đó (không resume giữa vòng lặp LLM) | Dùng lại luồng chat stream, không phải lưu trạng thái giữa chừng của model | Resume đúng vị trí trong vòng lặp tool |
| Quy ước core tự publish khi qua điều kiện chặn và do tenant_admin nói thẳng hoặc đã kiểm chứng qua sử dụng (≥3 lượt, ≥3 hữu ích, 0 có hại, ≥7 ngày). **Khác tài liệu kiến trúc theo quyết định của chủ dự án (2026-11-11)** | Đề xuất dồn lại, người duyệt khó theo kịp | Cấu hình tắt theo tenant |
| Auto mode thay điểm tin cậy và cờ untrusted: ô "judge" của ma trận do classifier quyết định theo luật viết bằng chữ; classifier không đọc kết quả công cụ. **Khác tài liệu kiến trúc theo quyết định của chủ dự án (2026-11-12)**: tài liệu dùng điểm tin cậy 4 thành phần và ép hỏi mọi lệnh chịu ảnh hưởng từ nội dung untrusted | Điểm tin cậy khó đoán (lệnh mới gần như luôn dưới ngưỡng), đọc log một lần là mọi lệnh sau phải duyệt | Đo tỉ lệ chặn nhầm/bỏ sót của classifier trên audit thật |
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
AIOPS_ADMIN_PASSWORD=doi-mat-khau .venv/bin/uvicorn aiops.api:app --port 8000
```
