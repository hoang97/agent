# Nhật ký cải tiến

Mỗi đợt ghi: vấn đề phát hiện, cách phát hiện, thay đổi, commit liên quan.

## Đợt 0 — 2026-11-10: khởi tạo

Dựng MVP theo `docs/PLAN.md`.

## Đợt 1 — 2026-11-10: sửa lỗi từ kiểm thử tự động

| Vấn đề | Phát hiện bằng | Thay đổi |
| --- | --- | --- |
| `git merge` của Compiler chạy thiếu `core.autocrlf=false`, cây làm việc bị bẩn nên `git revert` thất bại | `test_rollback_with_git_revert` | Mọi lệnh git đi qua `_git_proc` |
| Phản hồi sau khi đã học làm bộ đếm hữu ích tăng hai lần | Rà soát luồng học, thêm `test_feedback_after_learning_does_not_double_count` | Mỗi episode chỉ tính một lần cho mỗi KU, đối chiếu `ku_event` |
| File có thể bị đổi sang CRLF, làm `install.sh` hỏng trên Ubuntu | Cảnh báo git trên Windows | Thêm `.gitattributes` ép LF |

## Đợt 2 — 2026-11-10: triển khai thử

- Server Ubuntu 24.04 đã có RustDesk và Docker; ứng dụng chạy riêng bằng systemd service `kagent` sau Caddy, nghe `127.0.0.1:8000`.
- Chỉ cần cổng 443 mở từ ngoài: Caddy lấy chứng chỉ bằng thử thách TLS-ALPN, không cần cổng 80.
- `httpx` không còn đi kèm SDK `anthropic` 2.x nên kiểm thử trọn vòng trên server dùng virtualenv tạm riêng.
- Cookie `Secure` khiến client HTTP không gửi lại phiên, nên kiểm thử trên server chạy bản tạm với `KAGENT_COOKIE_SECURE=0` và dữ liệu riêng, xóa sau khi xong.

## Đợt 3 — 2026-11-10: HTTPS công khai

- Sau khi mở cổng 443, Caddy vẫn thử HTTP-01 qua cổng 80 đang đóng và gây thêm lần xác thực thất bại. `deploy/Caddyfile` nay tắt thử thách HTTP, chỉ dùng TLS-ALPN.
- Đã lấy được chứng chỉ Let's Encrypt cho `remote171.duckdns.org`; từ ngoài `/` trả 200, `/api/health` trả 200, `/api/me` khi chưa đăng nhập trả 401.

## Đợt 4 — 2026-11-11: đổi mật khẩu

- Mật khẩu ban đầu cần xoay vòng được sau khi bàn giao. Thêm `POST /api/auth/password` và tab Tài khoản: bắt buộc mật khẩu hiện tại, tối thiểu 12 ký tự, giới hạn số lần nhập sai, đăng xuất các phiên khác.

## Đợt 5 — 2026-11-11: nhập LLM key trên giao diện

- Tab Cài đặt cho org admin: key được kiểm tra bằng Models API trước khi lưu, mã hóa Fernet trong bảng `app_secret`, API chỉ trả 4 ký tự cuối.
- Khóa chủ `KAGENT_SECRET_KEY` nằm trong `/etc/kagent/kagent.env`, ngoài thư mục dữ liệu, để bản sao DB không kèm khóa giải mã.
- Thứ tự dùng key: giao diện → biến môi trường → chế độ heuristic.

## Đợt 6 — 2026-11-11: hỗ trợ relay tương thích Anthropic

- Form LLM thêm Base URL và loại xác thực (API key hoặc Auth token), cũng đọc `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` từ môi trường.
- Base URL chỉ nhận HTTPS tới địa chỉ công khai, vì server gửi credential tới đó.
- Với relay: kiểm tra bằng một tin nhắn ngắn thay cho Models API, và không gửi beta server-side fallback.

## Đợt 7 — 2026-11-11: hỗ trợ OpenAI và dịch vụ tương thích

- Thêm `OpenAIProvider` dùng Chat Completions; form LLM có chọn nhà cung cấp và tên model (cũng áp dụng cho relay Anthropic).
- Dịch vụ tương thích không hỗ trợ `json_schema` thì tự chuyển sang đưa schema vào chỉ dẫn.
- Đầu ra của Router, Reflector, Curator được kiểm lại trước khi ghi, vì không phải dịch vụ nào cũng ép đúng schema.
