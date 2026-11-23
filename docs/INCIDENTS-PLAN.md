# Kế hoạch cải thiện phần sự cố

Cập nhật 2026-11-14. Tham chiếu quy trình của repo `ops/incidents` (incident là issue, mỗi hành động là một comment
`<hành động> → <kết quả>`, postmortem 12 mục, mức S1–S4 theo ảnh hưởng khách hàng) và session recap của Claude Code.

Mục tiêu: hội thoại sự cố là nguồn dữ liệu. Cảnh báo mở hội thoại, hội thoại đề xuất và chạy hành động, mọi bước được lưu vết
và chờ duyệt khi cần, cuối cùng có một bản tóm tắt ngắn cho lãnh đạo.

## Theo tài liệu Cloud Operations (2026-11-23)

Đối chiếu kagent với kiến trúc tham chiếu Gartner G00834503, benchmark AIOps (AWS, Azure, Google) và Cloud Monitor 2.0 của Alibaba.
Làm theo thứ tự, mỗi việc một commit; chi tiết ở `IMPROVEMENTS.md`.

| # | Việc | Trạng thái |
| --- | --- | --- |
| 1 | Chỉ ra thay đổi gây lỗi: `data.recent_changes`, prompt điều tra đối chiếu thay đổi trước cảnh báo đầu tiên | xong (Đợt 42) |
| 3 | Đo độ chính xác của điều tra: người chấm nguyên nhân đúng/sai, tỉ lệ đúng, MTTR | xong (Đợt 43) |
| 4 | Link bằng chứng tới công cụ gốc (Grafana Explore, Kibana Discover) dựng từ truy vấn | xong (Đợt 44) |
| 2 | Kênh thông báo sự cố mới và thẻ chờ duyệt (thay việc 8 bên dưới) | chưa |
| 5 | Mức S1–S4 theo ảnh hưởng (thay việc 6 bên dưới) | chưa |
| 9 | Topology nhiều tầng (vật lý → hypervisor → VM → node → workload) | chờ data model của nhóm Cloud Operations |

Chưa cần: che dữ liệu nhạy cảm trước khi gửi LLM, nhận phản ánh ngoài Alertmanager (netchat, GNOC), mở công cụ qua MCP.

## Đã xong

| Việc | Commit |
| --- | --- |
| Cảnh báo gom thành sự cố, điều tra chỉ đọc trong hội thoại dùng chung, hành động qua gateway, xác minh | `8d127fc`, `0f6eeb8` |
| Hành động khắc phục hiện trong hội thoại điều tra | `2a0444e` |
| Lệnh hết hạn duyệt hiện rõ: câu dẫn, lý do phải duyệt, giờ hết hạn, nút "Nhờ agent đề xuất lại"; "Ẩn hành động" không ẩn thẻ đã quyết | `b8a0716` |
| Tóm tắt cho lãnh đạo: tự viết lại khi sự cố yên 5 phút sau hoạt động mới, 2–4 câu khoảng 400 ký tự, ở cuối hội thoại và đầu trang sự cố | `2acd66b` |
| Gom đúng sự cố kéo dài: sự cố còn cảnh báo đang bắn luôn nhận cảnh báo mới; bắn lại trong 24 giờ sau khi hết thì mở lại sự cố cũ; sự cố đang mở khớp luật nhãn hoặc node/workload thì tự gộp vào sự cố cũ nhất, gộp mang theo lệnh chờ duyệt và link hội thoại cũ | `0526e95` |

Đã chốt về bản tóm tắt: chỉ là phần tóm tắt cho lãnh đạo (chuyện gì, bao lâu, nguyên nhân, đang chờ gì). Chi tiết hành động và
cảnh báo xem ở dòng sự kiện, không lặp lại trong bản tóm tắt, không có 5 Whys hay action item.

## Việc tiếp theo

Xếp theo thứ tự nên làm.

### 1. Dòng sự kiện đủ để thay nhật ký xử lý

Bản tóm tắt dựa vào dòng sự kiện cho chi tiết, nhưng hiện mỗi lệnh chỉ ghi "chạy X".

- Mỗi lệnh ghi: ai yêu cầu (kagent tự động hay người vận hành trong chat), tự chạy hay ai duyệt lúc nào, bị từ chối kèm ghi chú,
  hết hạn, lỗi; kết quả tóm một dòng; xác minh lúc nào, cảnh báo đã hết hay còn.
- Gồm cả lệnh người dùng tự yêu cầu trong hội thoại sự cố, hiện chỉ nằm trong trace mà không vào `incident.actions`.
- Dữ liệu đã có (`approval_request.decided_by`, `decided_at`, `deny_reason`, `result`; `incident.actions`), không cần gọi model.

### 2. Sự cố lặp lại cùng nguyên nhân

Nguyên nhân chính đã sửa: trước đây chỉ sự cố nhận cảnh báo mới trong 10 phút gần nhất mới được xét để gom, nên sự cố kéo dài
nhiều ngày (`b70c928e`) không nhận cảnh báo bắn lại và `svc-document` mở thêm 13 sự cố riêng. Đã chốt: "cùng nhóm" là khớp luật
nhãn (`group_labels`) hoặc cùng node/workload; mở lại trong 24 giờ; tự gộp các sự cố đang mở, khớp theo AI thì không tự gộp.

Còn lại:
- Đếm số lần lặp (mở lại, gộp) và hiện "lặp lại N lần" trên trang sự cố và danh sách.
- Sự cố đã hết hoặc đã đóng cùng nhóm trong 30 ngày: hiện ở mục "Sự cố liên quan" (không gộp).

### 3. Điều tra hỏng vì hết quota không được chạy lại

Nhiều sự cố ngày 13/11 dừng ở "không điều tra được: tenant đã hết quota token trong ngày" và nằm yên ở trạng thái lỗi.

- Tự xếp lịch điều tra lại khi quota mở lại (đầu ngày), nếu sự cố vẫn còn cảnh báo.
- Trang sự cố nói rõ sự cố đang chờ quota, không chỉ ghi "lỗi".

### 4. Agent biết bản tóm tắt mới nhất

Khi người dùng hỏi tiếp trong hội thoại sự cố, agent chưa nhận bản tóm tắt nên có thể trả lời lệch kết luận đã có.

- Đưa bản tóm tắt mới nhất vào khối `<context>` của lượt đó (theo quy tắc prompt cache: không đổi system prompt, không sửa lịch sử).

### 5. "Nguyên nhân" trên trang sự cố bị cũ

`root_cause` chỉ ghi ở lần điều tra đầu. Bản tóm tắt đã cập nhật theo các lượt sau, nhưng thẻ "Nguyên nhân" vẫn hiện nhận định cũ
(ví dụ `b70c928e` lần đầu ghi "image không tồn tại", sau đó mới ra hai nguyên nhân).

- Cập nhật `root_cause` mỗi khi có lượt điều tra mới, hoặc ghi rõ nhận định đó thuộc lượt nào và lúc nào.

### 6. Mức S1–S4 và đánh giá có cần postmortem

- dev/staging mặc định S4 (chỉ ảnh hưởng nội bộ).
- prod: hỏi người bằng `ask_user` điều kagent không biết (khách hàng có bị ảnh hưởng, diện rộng hay hẹp, có mất dữ liệu),
  rồi áp cây quyết định ở README mục 3 của `ops/incidents`.
- Cần postmortem theo README mục 5, dùng kết quả của việc 2 cho điều kiện "S3 lặp lại ≥ 2 lần trong 30 ngày".

### 7. Duyệt bản tóm tắt

- Trạng thái nháp → đã duyệt; đã duyệt thì không tự ghi đè, chỉ báo "có thay đổi sau khi duyệt".
- Câu hỏi mở: ai được duyệt — người có quyền duyệt lệnh R2+ ở môi trường đó, hay mọi thành viên tenant.

### 8. Thẻ duyệt hết hạn khi không có ai trực

Cả hai lệnh tạo Service cho `svc-document` hết hạn sau 30 phút vì không ai xem.

- Thiếu kênh báo cho người duyệt (chưa có Slack, email hay thông báo trình duyệt). Đây là khoảng trống của hệ thống,
  không phải giới hạn của thiết kế; đề xuất thêm một kênh thông báo khi có thẻ chờ duyệt của sự cố.
- Cân nhắc thời hạn duyệt riêng cho lệnh của sự cố.

## Tạm hoãn

- Đẩy sang GitLab `ops/incidents`: tạo issue Incident có Phần A, comment từng dòng nhật ký theo mẫu
  `<hành động> → <kết quả>`, gắn `incident::mitigated` để bot của repo đó tạo MR postmortem. Cần kết nối GitLab có quyền ghi,
  mọi lệnh ghi đi qua gateway và luôn cần duyệt.

## Còn phải kiểm tra trên giao diện

- Thẻ tóm tắt chuyển sang "có hoạt động mới" khi gửi tin nhắn trong hội thoại sự cố.
- Mục "Tóm tắt sự cố" trong Cài đặt cảnh báo (đã có test API, chưa mở trên trình duyệt).
- 14 bản tóm tắt viết bằng prompt đầu tiên (700–800 ký tự, vài bản cắt giữa câu) chỉ được viết lại khi có hoạt động mới hoặc bấm
  "Tóm tắt ngay".
