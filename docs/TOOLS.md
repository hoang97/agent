# Công cụ: template và kết nối

Agent chỉ dùng công cụ thật. Mỗi công cụ được tạo từ một **template** cộng với **kết nối** do admin hoặc user cấu hình.

- **Template** (`kagent/templates/*.yaml`, lưu trong git) khai báo: tham số kết nối cần điền, bộ công cụ, metadata rủi ro của từng công cụ. Handler gọi hệ thống thật nằm trong `kagent/connectors/`.
- **Kết nối** là một bản của template với tham số riêng (URL, token, namespace...). Kết nối được gắn với tenant hoặc một user, và với các môi trường dev/staging/prod.
- Công cụ sinh ra có tên `<tên_kết_nối>.<công_cụ>`, ví dụ `k8s_prod.kubectl_write`. Tên theo template (`k8s.kubectl_write`) dùng trong luật chặn của chính sách, để một luật áp cho mọi kết nối Kubernetes.

## Template có sẵn

| Template | Công cụ (mức rủi ro) | Tham số chính |
| --- | --- | --- |
| `k8s` Kubernetes | kubectl_read R1, kubectl_write R3 (có chạy thử), kubectl_admin R4 (xem [Quyền Kubernetes](#quyền-kubernetes-và-công-cụ-kubectl)) | kubeconfig (+ context) hoặc api_url + token của tài khoản quản trị, ca_cert, namespace (mặc định của kubectl). Kubeconfig phải nhúng sẵn chứng chỉ (`kubectl config view --raw --minify --flatten`); dùng token hoặc chứng chỉ client, không hỗ trợ exec/auth-provider; api_url, token điền tay được ưu tiên hơn kubeconfig |
| `grafana` | search_dashboards, list_datasources, prometheus_query, list_alerts (R0), create_silence R3 | url, token service account, default_datasource_uid |
| `prometheus` (cả Mimir, Thanos, VictoriaMetrics) | query, query_range, list_alerts, list_rules, list_targets, label_values, metric_metadata (R0). query_range trả min/max/avg/last và vài điểm mẫu thay vì toàn bộ chuỗi | url, token hoặc username/password, tenant_header (X-Scope-OrgID) |
| `alertmanager` | list_alerts (gom theo alertname, severity), list_silences (R0), create_silence R3, expire_silence R3 (cả hai có chạy thử) | url, token hoặc username/password, tenant_header |
| `elasticsearch` (Kibana) | search_logs R1, list_indices R0, delete_index R3 | url Elasticsearch hoặc Kibana (đi qua Console proxy), api_key hoặc username/password, index_pattern |
| `web` | fetch_page R0 | allowed_domains, max_chars |
| `data` (nội bộ, tự có ở mọi tenant) | query R0 (lọc kết quả đã lưu theo result_id), script R4 (Python trong sandbox, luôn cần duyệt) | — |
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

## Quyền Kubernetes và công cụ kubectl

Ba công cụ `kubectl_read`, `kubectl_write`, `kubectl_admin` chạy được mọi lệnh kubectl trong phạm vi quyền của nhóm tương ứng. Kiểm soát quyền giao cho RBAC của cụm thay vì kagent đọc lệnh để đoán, nên mức rủi ro của từng công cụ là cố định:

| Nhóm | Verb được cấp | Công cụ | Mức |
| --- | --- | --- | --- |
| Đọc | get, list, watch | kubectl_read: get, describe, logs, top, events, explain, auth can-i, rollout status/history | R1 |
| Ghi | thêm create, update, patch | kubectl_write: thêm apply, create, patch, replace, label, annotate, scale, set, rollout restart/undo, cordon, taint, diff. Luôn chạy thử `--dry-run=server` trước | R3 |
| Quản trị | bất kỳ | kubectl_admin: thêm delete, drain, exec, run, certificate approve | R4 |

Cách dùng:

1. Tạo kết nối Kubernetes bằng kubeconfig hoặc token của **tài khoản quản trị**: tài khoản này phải tạo được ServiceAccount, Secret, Role/ClusterRole và Binding. Kubernetes không cho cấp quyền mà tài khoản tạo đang không có, nên trên thực tế cần gần như cluster-admin. Thông tin này được lưu mã hóa để lần sau đổi quyền không phải dán lại.
2. Bấm **Quyền Kubernetes** trên thẻ kết nối và khai quy tắc cho từng nhóm: namespace (`*` là toàn cụm), API group (`core` là nhóm lõi), resource, verb.
3. Bấm **Xem trước**. kagent so cấu hình với cụm, liệt kê object sẽ tạo, sửa, xóa, và chạy thử từng thay đổi bằng `dryRun=All`, nhờ đó bắt sớm các lỗi như namespace không tồn tại hay cấp quyền vượt quyền đang có.
4. Bấm **Áp dụng**. Mỗi nhóm có quy tắc được một ServiceAccount `kagent-<tên kết nối>-<8 ký tự id>-<nhóm>` trong namespace `kagent-system` (tự tạo nếu chưa có), kèm Secret token dài hạn. Mọi object mang nhãn `kagent.io/connection=<id kết nối>`.
5. Bật các công cụ kubectl_* ở trang Danh mục & quyền, vì công cụ ghi, xóa mặc định tắt.

Quy tắc:

- Nhóm cao hơn được thừa hưởng quyền của nhóm thấp hơn: RoleBinding của nhóm đọc gắn cả SA ghi và SA quản trị.
- Namespace cụ thể tạo Role + RoleBinding trong namespace đó; `*` tạo ClusterRole + ClusterRoleBinding.
- Nhóm đọc và ghi không được cấp `secrets`, `pods/exec`, `pods/attach`, `pods/portforward`, các `*/proxy`, `serviceaccounts/token`; nhóm ghi thêm `pods/eviction` (tạo eviction là xóa pod). Những quyền này chỉ cấp được cho nhóm quản trị (R4).
- API group `*` là mọi group. Resource `*` (cả `pods/*`, `*/scale`) ở nhóm quản trị được ghi nguyên vào Role. Ở nhóm đọc và ghi, kagent đọc API discovery của cụm rồi đổi `*` thành danh sách resource cụ thể, khớp như RBAC (gồm cả subresource), bỏ các loại nhạy cảm ở trên. Danh sách chỉ giữ resource có verb đã chọn, và chỉ giữ resource cấp namespace khi quy tắc không phải namespace `*`. Cài CRD mới thì lần Xem trước sau báo lệch, bấm Áp dụng để cập nhật. API tổng hợp đang lỗi (ví dụ metrics-server hỏng) bị bỏ qua kèm cảnh báo.
- Khi quy tắc ghi chạm tới workload, hệ thống cảnh báo: ai tạo được pod thì chạy được pod với ServiceAccount bất kỳ trong namespace đó.
- Chưa áp dụng quyền thì các công cụ kubectl_* báo lỗi, không bao giờ chạy bằng tài khoản quản trị. Nút **Kiểm tra** cho biết danh tính của từng SA.
- **Kết quả tin cậy theo lệnh:** kubectl_* khai `untrusted_output` nhưng có `trusted_when`. Lệnh chỉ trả dữ liệu có cấu trúc được coi là tin cậy, nên không ép duyệt các lệnh sau và không chặn việc học tri thức. Đó là: `get` các loại có sẵn (pods, deploy, svc, nodes, ns, jobs, pvc, ingress, hpa...) dạng bảng, `-o wide` hoặc `-o name`; `top`; `version`; `api-resources`; `auth can-i/whoami`; `rollout status`; và xác nhận của lệnh ghi khi không có `-o`. Còn lại là chưa tin cậy, gồm `logs`, `describe`, `events`, `-o yaml/json/jsonpath`, configmap, CRD (cột in tùy ý), `explain`, `exec`, `--raw`.
- **Gỡ khỏi cụm** xóa mọi object có nhãn của kết nối, nhưng giữ namespace. Gỡ kết nối cũng tự gỡ các object đó; nếu không gỡ được, giao diện báo nhãn để xóa tay.

kagent vẫn chặn những gì RBAC không chặn được:

- Lệnh phải bắt đầu bằng lệnh con, ví dụ `get pods -n app`.
- Không có `proxy`, `port-forward`, `attach`, `cp`, `edit`, `config`, `kustomize`, `cluster-info dump` hay plugin.
- Không đọc file trên máy chủ kagent: chỉ cho `-f -` với manifest truyền qua tham số `manifest`. Chặn `--from-file`, `--from-env-file`, `-o *-file`, `-k`.
- Không đổi danh tính hay đích đến: chặn `--as`, `--token`, `--server`/`-s`, `--kubeconfig`, `--context`, `--username`/`--password`.
- Không lộ token (`-v`) và không chạy kéo dài: chặn `-i`, `-t`, `--watch`, `logs -f`.
- Flag ngắn chỉ dùng `-n -l -c -o -L -p -A -h -f`, viết tách (`-n app`); các flag khác viết dạng dài.

Cách chạy kubectl:

- Mỗi lần chạy, kagent tạo kubeconfig tạm (quyền 600) trỏ thẳng vào IP đã được lớp mạng kiểm tra, kèm `tls-server-name` là tên máy gốc. Nhờ vậy vẫn chặn mạng nội bộ khi chưa được phép và chống DNS rebinding.
- kubectl chạy với env sạch, `HOME` tạm, timeout 1-120 giây (mặc định 30).
- Image cài kubectl từ `KUBECTL_URL` (build arg, mặc định trỏ Nexus nội bộ). Ngoài mạng nội bộ, đặt `KAGENT_KUBECTL_URL` cho docker compose, hoặc cài kubectl vào PATH (biến `KAGENT_KUBECTL` để chỉ đường dẫn khác).

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
  - Tham số `{{x}}` đặt được giữa chuỗi, ví dụ `{"command": "scale deployment/{{dep}} --replicas={{n}} -n {{ns}}"}`. Giá trị thay vào chỉ được là một từ gồm chữ, số và `._:/=@+,-`, không mở đầu bằng `-`, nên không chèn thêm được flag. `{{step:N}}` vẫn phải đứng riêng một giá trị.
  - Khi học, hệ thống tự tách tham số từ `command`: so các lệnh cùng khung ở những lượt lặp lại, phần khác nhau thành tham số; khi lưu từ một lượt chat, từ nào có trong câu yêu cầu thành tham số. Các chuỗi khác như SQL, URL được giữ nguyên.
