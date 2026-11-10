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

Agent chỉ được thấy những công cụ mà người yêu cầu được dùng ở môi trường hiện tại. Gateway vẫn kiểm lại ở mọi lệnh. Lệnh đang chờ duyệt mà công cụ bị tắt hoặc bị thu hẹp quyền thì không thể duyệt để chạy, chỉ còn từ chối được.

## Gỡ một công cụ

Xóa mục khỏi manifest rồi triển khai. Bản ghi của công cụ ở mọi tenant chuyển trạng thái **Đã gỡ** và mọi lệnh gọi bị chặn. Audit và cấu hình cũ được giữ lại. Nếu sau này thêm lại đúng tên đó, cấu hình cũ của tenant được dùng tiếp.
