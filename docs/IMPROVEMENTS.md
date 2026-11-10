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

## Đợt 8 — 2026-11-11: giao diện chat

- Thay tab Agent bằng giao diện chat: thanh bên chứa lịch sử hội thoại, đổi tên, xóa; câu trả lời stream qua SSE; nút dừng; Markdown, bảng, khối code có nút sao chép; đánh giá, sửa đầu ra, gửi lại; bố cục mobile. Các trang quản trị chuyển vào thanh bên.
- Hội thoại nhiều lượt: bảng `conversation`, `episode.conversation_id`, lịch sử nạp theo ngân sách cố định, gồm kết quả các lệnh được duyệt sau đó. Xóa hội thoại chỉ ẩn khỏi danh sách, episode vẫn là nhật ký bất biến.
- Nội dung untrusted nằm trong lịch sử làm các lượt sau của hội thoại đó bị ép hỏi, vì nó vẫn có thể lái kế hoạch của agent.
- Markdown dựng thành nút DOM, không dùng innerHTML.
- Sửa lỗi phát hiện khi kiểm thử giao diện: lệnh đã duyệt nhưng chạy lỗi vẫn hiện như thành công.
- Caddy không nén và không đệm `/api/chat`.

## Đợt 9 — 2026-11-11: chuẩn hóa danh mục công cụ

- Danh mục chuyển từ mã Python sang manifest YAML trong `kagent/catalog` (mỗi file một MCP server). Handler tách sang `kagent/backends`. Hướng dẫn ở `docs/TOOLS.md`.
- Manifest được kiểm tra chặt khi khởi động và bằng `python -m kagent.tools`: khóa lạ, enum, handler, schema, trùng tên. Mức rủi ro tối thiểu tính từ metadata, không được khai thấp hơn. Công cụ chưa phân loại bị coi là destructive. Annotation MCP chỉ sinh cảnh báo.
- Danh mục tự đồng bộ vào mọi tenant khi khởi động. Trước đây tenant đã có không nhận được công cụ mới. Công cụ mới mặc định tắt, công cụ bị gỡ chuyển trạng thái removed và bị chặn.
- Tenant được siết theo môi trường, vai trò và mức rủi ro (chỉ nâng). Agent chỉ thấy công cụ người yêu cầu được dùng. Gateway kiểm lại ở mọi lệnh và lúc duyệt.
- Bỏ quyền hạ mức rủi ro của org admin qua giao diện: muốn hạ thì sửa manifest và review trong git.

## Đợt 10 — 2026-11-11: nhiều công cụ cùng lúc, công cụ tự học, cá nhân hóa

- **Song song:** các lệnh trong một lượt chạy song song (tối đa 4), mỗi lệnh qua gateway và audit riêng. Kết quả chưa tin cậy chỉ ảnh hưởng các lượt sau. Thêm máy chủ `mcp-sql` mô phỏng, và `k8s.exec` chạy được `psql`/`mysql` trong pod.
- **Công cụ học được:** chuỗi bước tự đề xuất khi lặp lại thành công từ 3 lần, hoặc lưu từ chat. Mỗi bước vẫn qua gateway; công cụ tenant cần admin duyệt; tự tắt khi tỷ lệ thành công thấp. Bài học về từng công cụ được nạp kèm danh sách công cụ.
- **Cá nhân hóa:** chỉ dẫn riêng, phong cách trả lời, môi trường mặc định. Ký ức cá nhân học từ câu kiểu "tôi thích…", tự kích hoạt, chỉ chủ sở hữu thấy và xóa được.
- **Sửa lỗi rò tri thức:** ký ức cá nhân và tri thức riêng của dự án không còn bị biên dịch vào playbook chung. Curator chỉ so KU trong cùng phạm vi.
- **Lỗi phát hiện khi thử giao diện:** truy vấn chạy song song bị coi như chạy nối tiếp khi kiểm tra nội dung ngoài; bước con của công cụ học được bị ép hỏi sai; tên công cụ tiếng Việt bị mất ký tự.

## Đợt 11 — 2026-11-11: tự duyệt lệnh

- **Theo yêu cầu của chủ dự án:** người dùng tự duyệt lệnh công cụ của mình ngay trên thẻ trong chat hoặc ở trang Duyệt lệnh, một xác nhận là đủ. Đây là điểm khác tài liệu kiến trúc.
- **Giữ nguyên:**
  - luật chặn cứng và ô chặn của ma trận;
  - bắt buộc nêu lý do khi từ chối (lý do thành bài học);
  - kiểm tra lại quyền công cụ lúc duyệt;
  - audit ghi đúng người duyệt.
- **Thay đổi công cụ dùng chung vẫn cần admin:** cấu hình danh mục và công cụ học được của tenant.
- **Công tắc `self_approve` theo tenant:** mặc định bật; tắt thì quay về luật cũ.
- **Lỗi thấy khi thử giao diện:** lệnh duyệt xong nhưng chạy lỗi vẫn báo "Đã chạy lệnh". Giờ thông báo báo lỗi, và thẻ duyệt làm nổi bật khi lần chạy thử đã thất bại.
