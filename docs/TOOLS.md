# Danh mục công cụ

Mọi công cụ agent dùng được khai trong `kagent/catalog/*.yaml`. Mỗi file ứng với một MCP server. Manifest lưu trong git và được review như code, đúng với yêu cầu "metadata do admin duyệt, lưu trong git cùng chính sách" của tài liệu kiến trúc.

Ứng dụng đọc và kiểm tra toàn bộ danh mục khi khởi động. Chỉ cần một lỗi là service không chạy, để không có công cụ nào lọt vào mà chưa được phân loại đúng.

## Thêm một công cụ

1. Viết handler trong `kagent/backends/<module>.py`. Hàm nhận `(state, args)` và trả về `dict`. Lỗi nghiệp vụ báo bằng `ToolError`. Nếu công cụ có dry-run, viết thêm hàm `check` có cùng chữ ký và không sửa state.
2. Thêm mục vào manifest của server, hoặc tạo file mới cho server mới:

   ```yaml
   server: mcp-k8s
   description: Kubernetes
   defaults:            # áp cho mọi công cụ trong file, có thể ghi đè từng công cụ
     envs: [dev, staging, prod]
     roles: [member]
   tools:
     - name: k8s.scale                 # dạng nhom.hanh_dong
       description: Đổi số replica của deployment
       handler: sim:k8s_scale          # kagent.backends.sim.k8s_scale
       check: sim:k8s_scale_check      # bắt buộc khi dry_run: server
       action_class: write             # read | write | destructive | exec | external_send
       reversible: "yes"               # yes | partial | no | n/a
       blast_radius: workload          # object | workload | namespace | cluster
       data_sensitivity: internal      # public | internal | personal | confidential | secret
       dry_run: server                 # none | server
       untrusted_output: false         # true nếu đầu ra chứa nội dung bên ngoài (log, web)
       # risk_tier: R3                 # tùy chọn, chỉ được cao hơn mức tính ra
       rollback_hint: Scale lại về số replica cũ
       annotations: {idempotentHint: true}   # gợi ý MCP, chỉ dùng để cảnh báo
       input_schema:
         properties:
           deployment: {type: string}
           replicas: {type: integer}
         required: [deployment, replicas]
   ```

3. Chạy `python -m kagent.tools` để xem danh mục, mức rủi ro tính ra và cảnh báo. Lệnh trả mã 1 nếu manifest sai.
4. Chạy `pytest -q`, commit, rồi triển khai.
5. Sau khi triển khai, công cụ xuất hiện ở mọi tenant với nhãn **Mới** và **đang tắt**. Admin của từng tenant xem rồi bật ở trang **Công cụ & Chính sách**.

## Mức rủi ro

Mức tối thiểu được tính từ metadata. Manifest khai `risk_tier` thấp hơn mức này thì bị từ chối.

| Điều kiện | Mức tối thiểu |
| --- | --- |
| `exec`, hoặc `data_sensitivity: secret` | R4 |
| `destructive` trên `namespace` hoặc `cluster` | R4 |
| `destructive` trên `object` hoặc `workload`; `external_send` | R3 |
| `write` đảo ngược được (`reversible: "yes"`) trên `object` hoặc `workload` | R2 |
| `write` khác | R3 |
| `read` dữ liệu `personal` hoặc `confidential` | R1 |
| `read` khác | R0 |

Công cụ không khai `action_class` bị coi là chưa phân loại: `destructive`, phạm vi `workload`, tức R3.

Annotation của MCP server (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) do server tự khai nên chỉ là gợi ý. Lệch với metadata thì chỉ hiện cảnh báo, không đổi phân loại.

## Phân quyền theo tenant

Danh mục là trần quyền, mỗi tenant chỉ được siết thêm trong trang **Công cụ & Chính sách**:

- **Bật/tắt.** Công cụ mới luôn tắt cho tới khi admin bật.
- **Mức rủi ro:** chỉ được nâng. Chọn lại đúng mức của danh mục để bỏ phần nâng.
- **Môi trường:** tập con của `envs` trong danh mục.
- **Vai trò:** tập con của `roles` trong danh mục cộng `tenant_admin`. Danh mục cho `member` thì tenant có thể thu hẹp xuống các nhóm `approver:*`.

Một lệnh chỉ chạy khi qua đủ các điều kiện:

1. Công cụ còn trong danh mục và đang bật.
2. Môi trường được phép.
3. Người yêu cầu có vai trò được phép (`tenant_admin` và `org_admin` luôn được).
4. Lệnh qua ba cửa của gateway: luật chặn, ma trận mức rủi ro × môi trường, điểm tin cậy.

Khi ma trận yêu cầu duyệt, mặc định chính người yêu cầu xác nhận lệnh ngay trên thẻ trong chat ("Chạy lệnh", "Cho phép trong 1 giờ" hoặc "Từ chối" kèm lý do). Một xác nhận là đủ. Luật chặn cứng và ô "chặn" của ma trận vẫn giữ nguyên. Admin có thể tắt tự duyệt cho một tenant ở trang **Công cụ & Chính sách**; khi đó lệnh phải do người có vai trò duyệt khác xác nhận, và R3 ở prod cần 2 người.

Thay đổi công cụ dùng chung luôn cần admin:
- bật/tắt, đổi mức rủi ro, môi trường hoặc vai trò của công cụ trong danh mục;
- duyệt, sửa, tắt hoặc từ chối công cụ học được của tenant.

Công cụ học được cá nhân do chính chủ quản lý.

Agent chỉ được thấy những công cụ mà người yêu cầu được dùng ở môi trường hiện tại. Gateway vẫn kiểm lại ở mọi lệnh. Lệnh đang chờ duyệt mà công cụ bị tắt hoặc bị thu hẹp quyền thì không thể duyệt để chạy, chỉ còn từ chối được.

## Gỡ một công cụ

Xóa mục khỏi manifest rồi triển khai. Bản ghi của công cụ ở mọi tenant chuyển trạng thái **Đã gỡ** và mọi lệnh gọi bị chặn. Audit và cấu hình cũ được giữ lại. Nếu sau này thêm lại đúng tên đó, cấu hình cũ của tenant được dùng tiếp.

## Chạy nhiều công cụ cùng lúc

Các lệnh mà model gọi trong cùng một lượt được chạy song song, tối đa 4 lệnh một lúc, mỗi lệnh dùng kết nối riêng. Mỗi lệnh vẫn đi riêng qua gateway và được ghi audit riêng.

Kết quả chưa tin cậy của một lệnh trong nhóm không làm các lệnh cùng nhóm bị ép hỏi, vì chúng được lập kế hoạch cùng lúc. Nó chỉ ảnh hưởng tới các lượt sau. Ở chế độ heuristic, các lệnh đọc liền nhau chạy song song, còn lệnh ghi luôn chạy riêng.

Máy chủ `mcp-sql` (mô phỏng, có dữ liệu mẫu `customers` và `orders`) có hai công cụ:
- `sql.query` chỉ đọc, mức R1.
- `sql.execute` cho INSERT, UPDATE, DELETE, mức R3, có dry-run.

`k8s.exec` hiểu lệnh `psql -c "…"` và `mysql -e "…"` để chạy SQL bên trong pod. Đây vẫn là lệnh R4 nên luôn phải duyệt.

## Công cụ học được

Một công cụ học được là một chuỗi bước gọi công cụ trong danh mục, có tên dạng `learned.<ten>`. Công cụ học được sinh ra theo hai cách:

- **Tự đề xuất:** khi cùng một chuỗi công cụ thành công từ 3 lượt trở lên trong 30 ngày. Giá trị giống nhau giữa các lượt thành hằng số, giá trị khác nhau thành tham số. Nếu chỉ một người dùng chuỗi đó thì đề xuất là công cụ cá nhân của người đó, ngược lại là công cụ của tenant. Chuỗi đã bị từ chối sẽ không được đề xuất lại.
- **Lưu từ chat:** bấm biểu tượng lưu dưới một câu trả lời. Giá trị bạn đã gõ trong yêu cầu sẽ thành tham số.

Quy tắc an toàn:

- Mỗi bước vẫn đi qua gateway với quyền của người gọi. Ghép bước không tạo thêm quyền nào: nếu không được dùng một bước, cả công cụ bị chặn.
- Tham số chỉ thay nguyên giá trị (`"{{ten}}"`), không nội suy vào chuỗi lệnh.
- Công cụ của tenant cần tenant_admin duyệt. Công cụ cá nhân do chính chủ bật và chỉ chủ sở hữu thấy.
- Lượt có bước chạy sau nội dung chưa tin cậy thì không được học và không lưu được.
- Tỷ lệ thành công dưới 50% sau từ 4 lần dùng thì công cụ tự tắt. Bật lại thì bộ đếm được đặt lại.

Ngoài ra, bài học về từng công cụ (KU loại lesson hoặc procedure có entity `tool:<tên>`) được nạp vào ngữ cảnh kèm danh sách công cụ. Nhờ vậy cách dùng công cụ được bổ sung dần từ lỗi và thành công thực tế.
