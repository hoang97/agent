# Công cụ: template và kết nối

Agent chỉ dùng công cụ thật. Mỗi công cụ được tạo từ một **template** cộng với **kết nối** do admin hoặc user cấu hình.

- **Template** (`kagent/templates/*.yaml`, lưu trong git) khai báo: tham số kết nối cần điền, bộ công cụ, metadata rủi ro của từng công cụ. Handler gọi hệ thống thật nằm trong `kagent/connectors/`.
- **Kết nối** là một bản của template với tham số riêng (URL, token, namespace...). Kết nối được gắn với tenant hoặc một user, và với các môi trường dev/staging/prod.
- Công cụ sinh ra có tên `<tên_kết_nối>.<công_cụ>`, ví dụ `k8s_prod.scale`. Tên theo template (`k8s.scale`) dùng trong luật chặn của chính sách, để một luật áp cho mọi kết nối Kubernetes.

## Template có sẵn

| Template | Công cụ (mức rủi ro) | Tham số chính |
| --- | --- | --- |
| `k8s` Kubernetes | list_pods R0, list_deployments R0, pod_logs R1, scale R2, rollout_restart R3, delete_pod R3, exec R4 | kubeconfig (+ context) hoặc api_url + token ServiceAccount, ca_cert, namespace, allowed_namespaces, max_replicas. Kubeconfig phải nhúng sẵn chứng chỉ (`kubectl config view --raw --minify --flatten`); dùng token hoặc chứng chỉ client, không hỗ trợ exec/auth-provider; api_url, token điền tay được ưu tiên hơn kubeconfig |
| `grafana` | search_dashboards, list_datasources, prometheus_query, list_alerts (R0), create_silence R3 | url, token service account, default_datasource_uid |
| `elasticsearch` (Kibana) | search_logs R1, list_indices R0, delete_index R3 | url Elasticsearch hoặc Kibana (đi qua Console proxy), api_key hoặc username/password, index_pattern |
| `web` | fetch_page R0 | allowed_domains, max_chars |
| `http_api` | get R1, post R3 | base_url, auth_header, auth_value, allowed_paths |
| `postgres`, `mysql` | query R1 (chỉ đọc, 15 giây, 200 dòng), execute R3 (có chạy thử rồi rollback) | host, port, database, user, password |

`python -m kagent.tools` in danh sách template, công cụ và mức rủi ro.

## Ai được làm gì

| Việc | Kết nối dùng chung (tenant) | Kết nối cá nhân |
| --- | --- | --- |
| Tạo, sửa, tạm tắt, gỡ kết nối | tenant_admin | chính chủ |
| Bật/tắt, nâng mức rủi ro, thu hẹp môi trường và vai trò của từng công cụ | tenant_admin | chính chủ |
| Thấy và dùng công cụ | thành viên có vai trò được phép, ở môi trường được phép | chỉ chính chủ |
| Cho kết nối vào mạng nội bộ (`allow_private`) | org_admin | org_admin |
| Duyệt lệnh cần duyệt | người yêu cầu tự duyệt (khi tenant bật tự duyệt) hoặc người có vai trò duyệt | như bên trái |

Khi tạo kết nối, công cụ đọc được bật ngay; công cụ ghi, xóa và exec mặc định tắt, chủ kết nối phải bật. Mọi lệnh vẫn qua gateway: luật chặn, ma trận mức rủi ro × môi trường, điểm tin cậy, audit.

## An toàn khi gọi hệ thống thật

- **Secret** (token, mật khẩu, API key) được mã hóa bằng khóa chủ `KAGENT_SECRET_KEY` và không bao giờ trả về giao diện; khi sửa, để trống ô secret là giữ nguyên giá trị cũ.
- **Mạng:**
  - Luôn chặn loopback, link-local (gồm địa chỉ metadata cloud `169.254.169.254`), multicast và địa chỉ dành riêng.
  - Mạng nội bộ (10.x, 172.16-31.x, 192.168.x...) chỉ được dùng khi org_admin bật cho kết nối đó.
  - DNS được phân giải và kiểm tra trước, rồi kết nối được ghim vào IP đã kiểm tra (chống DNS rebinding). TLS vẫn xác thực theo tên máy gốc, dùng bộ CA của certifi, CA riêng của kết nối được nạp thêm.
  - Không tự theo redirect; công cụ Web tự theo tối đa 5 lần và kiểm tra lại từng bước.
- **Giới hạn:**
  - Phản hồi tối đa 2 MB, timeout 20 giây.
  - SQL đọc chạy trong giao dịch chỉ đọc.
  - Namespace Kubernetes có thể bị giới hạn theo kết nối; đường dẫn HTTP API có thể bị giới hạn theo tiền tố.
- **Nội dung chưa tin cậy:** log, trang web, kết quả SQL, HTTP API và exec được đánh dấu chưa tin cậy. Lệnh chạy sau chúng trong cùng hội thoại bị ép hỏi.
- **Quyền tối thiểu:** nên cấp cho token của kết nối quyền nhỏ nhất cần thiết (RBAC Kubernetes, role Grafana Viewer/Editor, tài khoản CSDL chỉ đọc). Đó là lớp giới hạn ngoài cùng, giữ an toàn kể cả khi chính sách bị cấu hình sai.

## Thêm một template mới

1. Viết handler trong `kagent/connectors/<module>.py`. Mỗi handler nhận `(params, args)`, trong đó `params` là tham số kết nối đã giải mã secret, có thêm `_allow_private`. Dùng `connectors.base.http()` hoặc `net.client()` để mọi lệnh gọi đều qua lớp mạng an toàn. Lỗi nghiệp vụ báo bằng `ConnectorError`.
2. Tạo `kagent/templates/<tên>.yaml`:

   ```yaml
   template: ten_template
   title: Tên hiển thị
   description: Mô tả ngắn
   ping: module:ham_kiem_tra        # dùng cho nút "Kiểm tra"
   validate: module:ham_kiem_tham_so # tùy chọn: kiểm tra chéo các tham số khi tạo/sửa kết nối, báo lỗi bằng ConnectorError
   fields:                           # type: string | url | secret | secret_text (secret nhiều dòng) | text | boolean | integer | list
     - {name: url, type: url, required: true, description: Địa chỉ dịch vụ}
     - {name: token, type: secret, required: true}
   tools:
     - name: lam_gi_do
       description: Mô tả cho agent
       handler: module:ham
       check: module:ham_chay_thu    # bắt buộc khi dry_run: server
       action_class: write           # read | write | destructive | exec | external_send
       reversible: "yes"             # yes | partial | no | n/a
       blast_radius: workload        # object | workload | namespace | cluster
       data_sensitivity: internal    # public | internal | personal | confidential | secret
       dry_run: server
       untrusted_output: false
       input_schema:
         properties: {name: {type: string}}
         required: [name]
   ```

3. Chạy `python -m kagent.tools` và `pytest -q`. Test connector chạy trên hạ tầng giả ở tầng HTTP (xem `tests/fakes.py`).
4. Commit và triển khai. Template mới xuất hiện trong form tạo kết nối.

Mức rủi ro tối thiểu được tính từ metadata; template khai `risk_tier` thấp hơn thì service không khởi động:

| Điều kiện | Mức tối thiểu |
| --- | --- |
| `exec`, hoặc `data_sensitivity: secret` | R4 |
| `destructive` trên `namespace` hoặc `cluster` | R4 |
| `destructive` trên `object` hoặc `workload`; `external_send` | R3 |
| `write` đảo ngược được trên `object` hoặc `workload` | R2 |
| `write` khác | R3 |
| `read` dữ liệu `personal` hoặc `confidential` | R1 |
| `read` khác | R0 |

## Chạy nhiều công cụ cùng lúc và công cụ học được

- **Chạy song song:** các lệnh model gọi trong cùng một lượt chạy song song, tối đa 4 lệnh. Kết quả chưa tin cậy chỉ ảnh hưởng các lượt sau. Ở chế độ heuristic, lệnh đọc chạy trước, lệnh ghi chạy sau.
- **Công cụ học được** (`learned.<tên>`) là chuỗi bước gọi các công cụ ở trên, sinh từ thao tác lặp lại hoặc lưu từ chat:
  - Mỗi bước vẫn qua gateway với quyền của người gọi.
  - Nếu kết nối của một bước bị gỡ hoặc tắt, công cụ học được bị chặn theo.
