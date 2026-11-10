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
