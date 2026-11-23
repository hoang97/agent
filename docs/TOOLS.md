# Công cụ: template và kết nối

Agent chỉ dùng công cụ thật. Mỗi công cụ được tạo từ một **template** cộng với **kết nối** do admin hoặc user cấu hình.

- **Template** (`kagent/templates/*.yaml`, lưu trong git) khai báo: tham số kết nối cần điền, bộ công cụ, metadata rủi ro của từng công cụ. Handler gọi hệ thống thật nằm trong `kagent/connectors/`.
- **Kết nối** là một bản của template với tham số riêng (URL, token, namespace...). Kết nối được gắn với tenant hoặc một user, và với các môi trường dev/staging/prod.
- Công cụ sinh ra có tên `<tên_kết_nối>.<công_cụ>`, ví dụ `k8s_prod.kubectl_write`. Tên theo template (`k8s.kubectl_write`) dùng trong luật chặn của chính sách, để một luật áp cho mọi kết nối Kubernetes.

## Template có sẵn

| Template | Công cụ (mức rủi ro) | Tham số chính |
| --- | --- | --- |
| `k8s` Kubernetes | kubectl_read R1, kubectl_write R3 (có chạy thử), kubectl_admin R4 (xem [Quyền Kubernetes](#quyền-kubernetes-và-công-cụ-kubectl)); helm_read R1 (kèm helm diff, helm drift), helm_write R3 (chạy thử bằng helm diff), helm_admin R4 (xem [Helm](#helm)) | kubeconfig (+ context) hoặc api_url + token của tài khoản quản trị, ca_cert, namespace (mặc định của kubectl). Kubeconfig phải nhúng sẵn chứng chỉ (`kubectl config view --raw --minify --flatten`); dùng token hoặc chứng chỉ client, không hỗ trợ exec/auth-provider; api_url, token điền tay được ưu tiên hơn kubeconfig |
| `grafana` | search_dashboards, list_datasources, prometheus_query, list_alerts (R0), create_silence R3 | url, token service account, default_datasource_uid, ui_url |
| `prometheus` (cả Mimir, Thanos, VictoriaMetrics) | query, query_range, list_alerts, list_rules, list_targets, label_values, metric_metadata (R0). query_range trả min/max/avg/last và vài điểm mẫu thay vì toàn bộ chuỗi | url, token hoặc username/password, tenant_header (X-Scope-OrgID), ui_url |
| `alertmanager` | list_alerts (gom theo alertname, severity), list_silences (R0), create_silence R3, expire_silence R3 (cả hai có chạy thử) | url, token hoặc username/password, tenant_header |
| `elasticsearch` (Kibana) | search_logs R1, list_indices R0, delete_index R3 | url Elasticsearch hoặc Kibana (đi qua Console proxy), api_key hoặc username/password, index_pattern, ui_url (địa chỉ Kibana) |
| `web` | fetch_page R0 | allowed_domains, max_chars |
| `data` (nội bộ, tự có ở mọi tenant) | query R0 (lọc kết quả đã lưu theo result_id), recent_changes R0 (lệnh ghi đã chạy thành công qua kagent và thay đổi topology quanh một thời điểm), topology R0 (các tầng dưới của thực thể trong cảnh báo, mức tin theo cạnh), script R4 (Python trong sandbox, luôn cần duyệt) | — |
| `http_api` | get R1, post R3 | base_url, auth_header, auth_value, allowed_paths |
| `openstack` | list_services R0, api_read R1, api_write R3, api_admin R4 (xem [OpenStack](#openstack)) | config (clouds.yaml hoặc openrc), cloud, password, region, interface, ca_cert |
| `gitlab` | list_projects, list_pipelines, get_pipeline, list_deployments, list_commits, list_merge_requests, list_issues (R0); job_log, get_merge_request, get_file, api_read (R1; api_read chặn biến CI, token, khoá, webhook, runner); retry_pipeline, cancel_pipeline, retry_job, play_job, create_note (R3); api_admin R4 | url (gồm cả đường dẫn con nếu GitLab cài dưới /gitlab), token (scope read_api để đọc, api + role Developer để ghi), default_project |
| `postgres`, `mysql` | query R1 (chỉ đọc, 15 giây, 200 dòng), execute R3 (có chạy thử rồi rollback) | host, port, database, user, password |

`python -m kagent.tools` in danh sách template, công cụ và mức rủi ro.

`ui_url` (tuỳ chọn) là địa chỉ mở trên trình duyệt, dùng khi url là địa chỉ nội bộ chỉ kagent gọi được. Trang sự cố dùng nó để dựng link mở lại bước truy vấn của agent (`kagent/links.py`): Prometheus `/graph`, Grafana Explore (datasource và PromQL của bước), Kibana Discover (query Lucene và khoảng thời gian). Elasticsearch không có giao diện, nên chỉ có link khi `ui_url` là Kibana hoặc url là Kibana (cổng 5601 hay tên có chữ kibana). Link không mang token: người mở phải đăng nhập công cụ đó.

## Ai được làm gì

| Việc | Kết nối dùng chung (tenant) | Kết nối cá nhân |
| --- | --- | --- |
| Tạo, sửa, tạm tắt, gỡ kết nối | tenant_admin | chính chủ |
| Bật/tắt, nâng mức rủi ro, thu hẹp môi trường và vai trò của từng công cụ | tenant_admin | chính chủ |
| Thấy và dùng công cụ | thành viên có vai trò được phép, ở môi trường được phép | chỉ chính chủ |
| Cho kết nối vào mạng nội bộ (`allow_private`) | org_admin | org_admin |
| Duyệt lệnh cần duyệt | người yêu cầu tự duyệt (khi tenant bật tự duyệt) hoặc người có vai trò duyệt | như bên trái |

Khi tạo kết nối, công cụ đọc được bật ngay; công cụ ghi, xóa và exec mặc định tắt, chủ kết nối phải bật. Mọi lệnh vẫn qua gateway: luật chặn, ma trận mức rủi ro × môi trường, auto mode, audit (xem [Gateway quyết định thế nào](#gateway-quyết-định-thế-nào)).

## An toàn khi gọi hệ thống thật

- **Secret** (token, mật khẩu, API key) được mã hóa bằng khóa chủ `KAGENT_SECRET_KEY` và không bao giờ trả về giao diện; khi sửa, để trống ô secret là giữ nguyên giá trị cũ.
- **Mạng:**
  - Luôn chặn loopback, link-local (gồm địa chỉ metadata cloud `169.254.169.254`), multicast và địa chỉ dành riêng.
  - Mạng nội bộ (10.x, 172.16-31.x, 192.168.x...) chỉ được dùng khi org_admin bật cho kết nối đó.
  - DNS được phân giải và kiểm tra trước, rồi kết nối được ghim vào IP đã kiểm tra (chống DNS rebinding). TLS vẫn xác thực theo tên máy gốc, dùng bộ CA của certifi, CA riêng của kết nối được nạp thêm.
  - Không tự theo redirect; công cụ Web tự theo tối đa 5 lần và kiểm tra lại từng bước.
- **Proxy:**
  - Org admin khai URL file PAC ở **Cài đặt → Proxy**. kagent tự tải file về, giữ 5 phút rồi tải lại; tải lỗi thì dùng bản đã nạp trước đó, và lỗi được ghi nhớ nên không tải lại ở mọi request. Ô **Thử một địa chỉ** cho biết PAC trả đường nào.
  - Mỗi kết nối HTTP và Kubernetes có ô `proxy`: `auto` (mặc định, theo PAC; chưa khai PAC thì đi thẳng), `none` (đi thẳng), hoặc `http://host:port`.
  - Mã PAC chạy bằng `pypac`/`dukpy` trong tiến trình con có hạn 5 giây, nên PAC lỗi hay treo không làm treo kagent. Kết quả tra theo tên máy được giữ 5 phút.
  - Đường `PROXY a; PROXY b; DIRECT` được thử lần lượt khi gặp lỗi kết nối. SOCKS chưa hỗ trợ. kubectl chỉ nhận đường đầu tiên, qua `proxy-url` trong kubeconfig tạm.
  - Đích đi qua proxy vẫn phải phân giải được ở máy kagent và qua các kiểm tra IP như khi đi thẳng; chỉ việc kết nối là do proxy làm, nên không ghim IP được. HTTPS đi qua CONNECT, TLS xác thực đầu cuối theo tên máy gốc.
  - Proxy lấy từ PAC (do org admin khai) được dùng luôn, kể cả khi nằm trong mạng nội bộ. Proxy khai tay trên kết nối phải qua kiểm tra địa chỉ như mọi đích khác.
  - Postgres, MySQL luôn đi thẳng. Lỗi hết thời gian chờ có ghi rõ là đã đi thẳng hay qua proxy nào; nút **Kiểm tra** cũng hiện đường đi.
  - Ai sửa được file PAC là đổi được đường đi của mọi kết nối, kể cả nơi token được gửi tới và nội dung các kết nối `http://` không mã hóa. Vì vậy chỉ dùng URL do bên quản trị mạng giữ.
- **Giới hạn:**
  - Phản hồi tối đa 2 MB, timeout 20 giây.
  - SQL đọc chạy trong giao dịch chỉ đọc.
  - Namespace Kubernetes có thể bị giới hạn theo kết nối; đường dẫn HTTP API có thể bị giới hạn theo tiền tố.
- **Nội dung chưa tin cậy:** log, trang web, kết quả SQL, HTTP API và exec được đánh dấu chưa tin cậy và bọc trong thẻ `<untrusted>` khi đưa vào prompt của agent. Auto mode không đọc kết quả công cụ, nên nội dung này không lái được quyết định cho chạy. Chuỗi bước chạy sau nội dung chưa tin cậy không được học thành công cụ.
- **Quyền tối thiểu:** nên cấp cho token của kết nối quyền nhỏ nhất cần thiết (RBAC Kubernetes, role Grafana Viewer/Editor, tài khoản CSDL chỉ đọc). Đó là lớp giới hạn ngoài cùng, giữ an toàn kể cả khi chính sách bị cấu hình sai.

## Gateway quyết định thế nào

Theo cách của auto mode trong Claude Code. Lệnh dừng ở bước đầu tiên khớp:

1. **Chặn cố định:** luật `deny` theo tên công cụ, ô `block` của ma trận.
2. **Bắt buộc hỏi:** change freeze, vượt quota ghi trong ngày (chỉ lệnh ghi), chạy thử phía server lỗi, công tắc `auto_execute` (`off`: mọi lệnh; `read_only`: trừ R0), lý do của luồng sự cố. Grant không vượt qua được các lý do này.
3. **Ma trận:**

   | | dev | staging | prod |
   | --- | --- | --- | --- |
   | R0 | tự chạy | tự chạy | tự chạy |
   | R1 | tự chạy | tự chạy | auto mode |
   | R2 | auto mode | auto mode | luôn hỏi |
   | R3 | auto mode | luôn hỏi | 2 người duyệt |
   | R4 | luôn hỏi | chặn | chặn |

   Overlay của tenant chỉ được siết (tự chạy → auto mode → hỏi → chặn). Overlay cũ có ngưỡng `ask_below` được đọc thành ô auto mode.
4. **Auto mode:** một model xét lệnh thay người duyệt.
   - **Đầu vào:** lời người dùng trong hội thoại, các lệnh họ đã duyệt, và tên, tham số, trạng thái các lệnh agent đã gọi. Không có kết quả công cụ, không có lời của agent.
   - **Luật** ở trang Chính sách, viết bằng chữ, mỗi tenant tự thêm. Dòng `$defaults` chèn luật mặc định; mục không có `$defaults` thay hẳn luật mặc định của mục đó.
     - `environment`: mô tả hạ tầng.
     - `hard_deny`: chặn kể cả khi người dùng yêu cầu.
     - `soft_deny`: chặn, trừ khi người dùng yêu cầu đích danh đúng hành động và đối tượng, hoặc khớp `allow`.
     - `allow`: ngoại lệ của `soft_deny`.
   - **Hai bước:** bước nhanh chỉ trả có/không và nghiêng về chặn. Chỉ khi bị đánh dấu thì bước suy xét kỹ mới chạy và nêu luật bị vi phạm.
   - **Kết quả:**
     - Cho qua thì lệnh chạy.
     - Chặn thì agent nhận lý do (`auto mode chặn: <luật>: <lý do>`) và tìm cách khác. Chặn 3 lần liên tiếp, hoặc 20 lần trong một lượt, thì thành yêu cầu duyệt.
     - Model lỗi hoặc chưa cấu hình LLM thì hỏi người. Chế độ heuristic chỉ cho qua lệnh đọc mà mọi đối tượng có trong lời người dùng.
   - **Công cụ local** (`data.*`) ở ô auto mode thì chạy luôn, vì không tác động ra ngoài. `data.script` là R4 nên vẫn luôn hỏi.
   - **Ô luôn hỏi:** classifier vẫn chạy, nhưng chỉ ghi nhận xét vào lý do cho người duyệt xem.
5. **Grant "cho phép trong 1 giờ":** cùng người, cùng công cụ, cùng môi trường và namespace thì tự chạy, trừ khi có lý do ở bước 2.
6. **Lệnh ghi ở ô auto mode còn phải hoàn tác được** (`kagent/undo.py`). Auto mode cho qua chưa đủ; thiếu một điều kiện dưới đây thì lệnh thành yêu cầu duyệt, thẻ ghi rõ điều kiện nào chưa đạt:
   - (a) **Hoàn tác được:** connector tự dựng lệnh hoàn tác từ trạng thái đọc ngay trước khi chạy, không ai viết playbook, không do model viết.
   - (b) **Đúng phạm vi:** lệnh chạm tối đa 5 đối tượng nêu tên, không chọn theo selector hay `--all`; hoàn tác chỉ đưa đúng các đối tượng đó về bản đã chụp.
   - (c) **Có bằng chứng:** chạy thử lệnh hoàn tác qua, hoặc loại hoàn tác này đã từng hoàn tác hay diễn tập thành công ở tenant, hoặc admin đánh dấu tin cậy.
   - (d) **Trạng thái bình thường:** đối tượng không đang rollout dở và không có thay đổi khác của kagent đang chờ kiểm tra.

   Ma trận không đổi: ô luôn hỏi vẫn luôn hỏi, R2+ ở prod vẫn cần người. Classifier cũng thấy `action.undo` (hoàn tác được không, bằng cách nào); luật mặc định "Không hoàn tác được" chặn lệnh ghi không hoàn tác được trừ khi người dùng yêu cầu đích danh.

### Hoàn tác, kiểm tra sau khi chạy, thùng rác

Theo cách Google SRE (rollback là cách giảm thiệt hại chung, phải diễn tập), Amazon (chuẩn bị rollback trước mỗi lần deploy, tránh cửa một chiều) và Claude Code (đọc trạng thái trước lệnh có thể làm mất việc).

| Công cụ | Cách hoàn tác | Không hoàn tác được |
| --- | --- | --- |
| `kubectl_write` | Chạy thử phía server với `-o json` để biết đúng đối tượng sẽ đổi, chụp bản hiện tại; hoàn tác = `replace` về bản chụp. Đối tượng vừa tạo: xoá đúng nó (cần nhóm quản trị đã áp dụng). `cordon`↔`uncordon`, `rollout pause`↔`resume`. `rollout restart`: không có gì cần hoàn tác. | `taint`, mọi thứ chạm Secret (kagent không lưu nội dung secret) |
| `kubectl_admin` | `delete`: chụp manifest rồi mới xoá, hoàn tác = tạo lại. Pod có controller: không cần, controller tạo lại. PVC: đổi PV sang `Retain` trước khi xoá (xoá mềm), hoàn tác = gắn lại PV. | namespace, PV, node, CRD, Secret, `exec`, `run`, `drain` |
| `helm_write` | `upgrade`, `rollback`: ghi revision đang chạy, hoàn tác = `helm rollback` về đúng revision đó. `install` (hoặc `upgrade --install` khi chưa có release): hoàn tác = `uninstall` đúng release vừa cài. Đã có revision mới sau lệnh thì coi là bị sửa, không tự đè. | `install --generate-name`; namespace do `--create-namespace` tạo không bị xoá |
| `helm_admin` | Như `helm_write` với install/upgrade/rollback. | `uninstall` (xoá cả lịch sử revision), `test` |
| Elasticsearch `delete_index` | Xoá mềm: đóng index thay vì xoá; mở lại để khôi phục. | index đang đóng |
| PostgreSQL/MySQL `execute` | `DROP TABLE` → đổi tên sang bảng `_kagent_trash_*`. `DELETE`/`UPDATE`/`TRUNCATE` → sao lưu các dòng sang `_kagent_bak_*`, đếm đủ dòng rồi mới chạy (`UPDATE` cần khoá chính). `ADD COLUMN`, `CREATE TABLE/INDEX` → xoá đúng thứ vừa thêm. | `INSERT`, nhiều câu trong một lần gọi, quá `max_rows` dòng; pha thu gọn của migration (`DROP/RENAME/đổi kiểu cột`, `DROP INDEX/VIEW/SCHEMA`) luôn cần người duyệt |
| Silence (Alertmanager, Grafana) | Tạo: gỡ đúng silence vừa tạo. Gỡ: tạo lại cùng matchers tới hạn cũ. | |
| GitLab `create_note` | Xoá đúng comment vừa tạo; comment đã bị sửa thì coi là có thay đổi khác. | email thông báo đã gửi; `retry_pipeline`, `cancel_pipeline`, `retry_job`, `play_job` (job đã chạy, như deploy hay migrate, không tự hoàn tác) |
| OpenStack `api_write` | Action có cặp ngược (`os-stop`↔`os-start`, `pause`↔`unpause`, `resize`→`revertResize`...). `PUT/PATCH` một tài nguyên: đọc giá trị cũ của các trường sắp đổi rồi ghi lại. | `POST` tạo mới, action khác |

- **Kiểm tra sau khi chạy:** chờ `verify_minutes` (mặc định 5) rồi kiểm tra workload đã sẵn sàng (rollout đủ pod) và không có cảnh báo mới trên namespace hay đối tượng của lệnh (từ nguồn cảnh báo đã kết nối ở tab Sự cố). Có cảnh báo mới thì báo không đạt ngay. Lệnh đã **tự chạy** mà không đạt thì tự hoàn tác (`auto_revert`); lệnh người đã duyệt thì chỉ báo, người quyết định.
- **Nút Hoàn tác** trên dòng lệnh trong chat, thẻ đã duyệt và thùng rác: người chạy lệnh hoặc người có quyền duyệt ở môi trường đó bấm được. Trước khi hoàn tác, kagent so đối tượng với lúc ngay sau lệnh; đã bị ai sửa thì không tự đè mà hiện danh sách khác biệt để người bấm **Vẫn hoàn tác**. Tự hoàn tác gặp trường hợp này thì dừng và báo.
- **Diễn tập:** lần đầu một loại hoàn tác chạy ở môi trường bật `drill` (mặc định dev), kagent hoàn tác ngay, so với bản chụp, rồi chạy lại lệnh gốc. Thành công thì loại đó có bằng chứng cho lần sau. Không bao giờ diễn tập ở prod.
- **Thùng rác:** thứ bị xoá mềm và bản sao lưu nằm ở trang Chính sách, khôi phục được bất cứ lúc nào. Sau `trash_days` (mặc định 7) ngày thì báo đến hạn; xoá thật luôn do người có quyền duyệt bấm, không tự chạy.
- **Nút dừng:** **Dừng mọi lệnh tự chạy** ở đầu trang Chính sách: mọi lệnh ghi phải có người duyệt và không tự hoàn tác; lệnh đọc vẫn chạy.
- **Giới hạn:** giữa lúc sao lưu SQL và lúc chạy lệnh có một khoảng ngắn không cùng giao dịch. Cảnh báo chỉ gắn được với lệnh qua nhãn `namespace` hoặc tên đối tượng. MySQL tự commit DDL nên không chạy thử DDL (chạy thử sẽ là chạy thật): lệnh DDL trên MySQL luôn hỏi người.

## Thẻ duyệt và câu hỏi trong chat

Theo cách Claude Code hỏi và chờ người dùng.
- **Thẻ duyệt:** hiện câu agent tự mô tả lệnh (tham số `_why` của công cụ ghi), lệnh cụ thể, lý do cần duyệt, kết quả chạy thử và cách hoàn tác. `_why` chỉ để người duyệt đọc: không gửi tới connector, không vào classifier, không vào công cụ học được.
- **Các lựa chọn:** **Chạy**; **Chạy, và cho phép `<công cụ>` ở `<môi trường>`, namespace `<ns>` trong 1 giờ** (nút ghi rõ phạm vi của grant); **Từ chối**. Ô ghi chú dùng chung cho cả ba:
  - Từ chối kèm ghi chú thì agent tự làm tiếp theo ghi chú. Ví dụ "kiểm tra registry trước": agent kiểm tra, rồi gọi lại lệnh để có thẻ duyệt mới.
  - Từ chối không kèm ghi chú thì lượt dừng.
  - Chạy kèm ghi chú thì agent nhận ghi chú cùng kết quả.
  - Ghi chú từ chối thành bài học (KU lesson). Ghi chú của cả hai loại được đưa vào ý người dùng cho auto mode.
- **Nhắn tiếp thay vì duyệt thì lệnh cũ bị bỏ qua:** gửi tin nhắn mới trong hội thoại thì mọi lệnh còn chờ duyệt của các lượt trước chuyển sang `skipped`, kể cả lệnh prod đã có một trong hai người duyệt. Chỉ đề xuất của lượt mới nhất còn duyệt được. Agent thấy lệnh "bị bỏ qua" khi đọc lại lịch sử và tự đề xuất lại nếu yêu cầu mới vẫn cần. Bỏ qua không tính là ý của người dùng cho auto mode. Lượt chạy tiếp sau khi duyệt không bỏ qua gì. Lệnh của lượt mới nhất vẫn hết hạn sau 30 phút.
- **Nhắn thêm khi agent đang chạy** (như Claude Code): khi ô nhập có chữ, nút Gửi thành "Gửi thêm"; khi ô nhập trống, nút vẫn là Dừng.
  - Tin vào hộp tin của lượt (`POST /api/conversations/{id}/queue`). Agent đọc tin ngay sau lệnh đang chạy, nằm cùng message với kết quả lệnh, nên lượt không bị ngắt. Nếu model vừa định kết thúc mà còn tin chưa đọc, nó đọc tin đó rồi làm tiếp trong cùng lượt.
  - Tin được ghi vào trace (`user_message`) và hiện đúng chỗ trong luồng. Tin cũng được thêm vào ý người dùng cho auto mode, ở lượt này và các lượt sau.
  - Không bỏ qua lệnh đang chờ duyệt, khác với tin nhắn mới gửi thành lượt mới.
  - Tin đến sau lần đọc cuối (lượt sắp xong) được trả về trong sự kiện `leftover`, giao diện gửi thành lượt kế tiếp. Lượt điều tra sự cố không nhận tin (409).
- **Agent không được nói "đang chờ duyệt" khi lượt đó không có lệnh nào chờ.** Câu kết thúc nói vậy thì runner nhắc agent một lần, để agent gọi công cụ hoặc viết lại câu trả lời.
- **`ask_user`:** agent hỏi 1–4 câu, mỗi câu 2–4 lựa chọn (lựa chọn khuyên dùng đứng đầu), giao diện tự thêm ô "Khác". Chỉ có một câu chọn một thì bấm là gửi luôn. Câu trả lời đi như tin nhắn của người dùng ở lượt sau. Gọi `ask_user` xong, các lệnh khác trong lượt bị chặn. Không có `ask_user` trong lượt điều tra sự cố.
- **`read_skill`:** đọc toàn bộ `SKILL.md` của một skill đã publish; system chỉ chứa tên và mô tả của skill. Chỉ có khi tenant có skill, tham số `name` giới hạn trong danh sách skill hiện có. Chạy thẳng, không qua gateway hay duyệt, dùng được cả trong lượt điều tra sự cố.
- **`search_knowledge`, `read_knowledge_page`:** agent tự tra kho tri thức giữa lượt. `search_knowledge` dùng đúng truy xuất lai của đầu lượt (8 KU) và trả kèm tên các trang thực thể liên quan; KU tìm được tính vào `ku_used` nên cũng được cộng điểm hữu ích hay có hại. `read_knowledge_page` đọc `entities/<thực thể>.md` (tối đa 12k ký tự), `entity="index"` cho danh sách trang. Luôn có trong danh sách công cụ, kể cả khi kho trống, để prefix không đổi theo điều kiện. Chạy thẳng, không qua gateway, dùng được cả trong lượt chỉ đọc.

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
- **Kết quả tin cậy theo lệnh:** kubectl_* khai `untrusted_output` nhưng có `trusted_when`. Lệnh chỉ trả dữ liệu có cấu trúc được coi là tin cậy, nên không chặn việc học tri thức và công cụ học được. Đó là: `get` các loại có sẵn (pods, deploy, svc, nodes, ns, jobs, pvc, ingress, hpa...) dạng bảng, `-o wide` hoặc `-o name`; `top`; `version`; `api-resources`; `auth can-i/whoami`; `rollout status`; và xác nhận của lệnh ghi khi không có `-o`. Còn lại là chưa tin cậy, gồm `logs`, `describe`, `events`, `-o yaml/json/jsonpath`, configmap, CRD (cột in tùy ý), `explain`, `exec`, `--raw`. Khi học tri thức từ sự cố, `describe` và `get events` (bảng, `-o json`, `custom-columns` chỉ gồm trường cấu trúc) còn có `trusted_view`: giữ khoá theo danh sách cho phép (trạng thái, reason, exit code, restart count, image, node, tài nguyên...), bỏ cột Message, annotation, label, env, command/args; ô không phải một token, tuổi hay ngày thì bị lược. Agent vẫn thấy nguyên văn và lệnh vẫn bị coi là chưa tin cậy.
- **Gỡ khỏi cụm** xóa mọi object có nhãn của kết nối, nhưng giữ namespace. Gỡ kết nối cũng tự gỡ các object đó; nếu không gỡ được, giao diện báo nhãn để xóa tay.

kagent vẫn chặn những gì RBAC không chặn được:

- Lệnh phải bắt đầu bằng lệnh con, ví dụ `get pods -n app`.
- Không có `proxy`, `port-forward`, `attach`, `cp`, `edit`, `config`, `kustomize`, `cluster-info dump` hay plugin.
- Không đọc file trên máy chủ kagent: chỉ cho `-f -` với manifest truyền qua tham số `manifest`. Chặn `--from-file`, `--from-env-file`, `-o *-file`, `-k`.
- Không đổi danh tính hay đích đến: chặn `--as`, `--token`, `--server`/`-s`, `--kubeconfig`, `--context`, `--username`/`--password`.
- Không lộ token (`-v`) và không chạy kéo dài: chặn `-i`, `-t`, `--watch`, `logs -f`.
- Flag ngắn chỉ dùng `-n -l -c -o -L -p -A -h -f`, viết tách (`-n app`); các flag khác viết dạng dài.
- Không có shell. Lệnh đọc được nối `| grep [-i -v -E -F -w -c] [-e] mẫu`, `| head -n N`, `| tail -n N`, `| wc -l`; kagent tự lọc stdout bằng Python. Các toán tử shell khác (`;`, `&&`, `>`, `$(`...) bị từ chối với thông báo rõ ràng, không để kubectl hiểu nhầm thành tên tài nguyên.

Cách chạy kubectl:

- Mỗi lần chạy, kagent tạo kubeconfig tạm (quyền 600) trỏ thẳng vào IP đã được lớp mạng kiểm tra, kèm `tls-server-name` là tên máy gốc. Nhờ vậy vẫn chặn mạng nội bộ khi chưa được phép và chống DNS rebinding.
- kubectl chạy với env sạch, `HOME` tạm, timeout 1-120 giây (mặc định 30).
- Image cài kubectl từ `KUBECTL_URL` (build arg, mặc định dl.k8s.io). Mạng nội bộ không tải được thì đặt `KAGENT_KUBECTL_URL` trỏ mirror cho docker compose, hoặc cài kubectl vào PATH (biến `KAGENT_KUBECTL` để chỉ đường dẫn khác).

## Helm

Ba công cụ `helm_read`, `helm_write`, `helm_admin` chạy helm 3 cùng plugin [helm-diff](https://github.com/databus23/helm-diff) và [helm-drift](https://github.com/nikhilsbhat/helm-drift) trên cụm của kết nối Kubernetes.

Helm lưu release trong Secret (`sh.helm.release.v1.*`) của namespace. Theo RBAC của kagent, chỉ nhóm quản trị được đọc Secret, nên **cả ba công cụ đều chạy bằng ServiceAccount nhóm quản trị** và cần nhóm này đã được áp dụng. RBAC vì thế không phân biệt được lệnh đọc với lệnh ghi; kagent tự phân lệnh theo nội dung:

| Công cụ | Lệnh | Mức |
| --- | --- | --- |
| helm_read | `list`, `status`, `history`, `show chart/values/readme/crds/all`, `template`, `get notes/metadata`, `diff upgrade/rollback/revision/release`, `drift run/all`, `version` | R1 |
| helm_write | thêm `install`, `upgrade` (kể cả `--install`), `rollback`. Chạy thử bằng `helm diff`, hoàn tác bằng `helm rollback` | R3 |
| helm_admin | thêm `uninstall`, `test`, `get values/manifest/hooks/all`, `diff --show-secrets` (kết quả có thể chứa Secret) | R4 |

- **helm diff** che giá trị Secret theo mặc định; `--show-secrets` chuyển lệnh sang helm_admin. Có `--detailed-exitcode` thì mã thoát 2 (có khác biệt) không tính là lỗi.
- **helm drift** so release với trạng thái thật trong cụm để tìm chỗ bị sửa tay, ví dụ `drift run web -n app --from-release`. kagent tự thêm `--disable-error-on-drift`, vì có drift là kết quả chứ không phải lỗi, và `--no-color`.
- **Chạy thử của helm_write** đổi lệnh thành `helm diff upgrade --allow-unreleased` hoặc `helm diff rollback`, bỏ các flag chỉ dành cho lệnh ghi (`--wait`, `--atomic`, `--timeout`...). Thiếu plugin helm-diff thì chạy `--dry-run=server` nhưng không hiện manifest, vì manifest có Secret ở dạng thường.
- **Chart:** dùng `oci://...`, `https://.../chart.tgz` hoặc tên chart kèm `--repo https://...`. URL qua kiểm tra mạng như mọi đích khác, và tải chart qua proxy của kết nối (`NO_PROXY` giữ API server). Không có `helm repo add`, `registry login`, `plugin install`: mỗi lần chạy dùng `HOME`, cache và config tạm.
- **Values:** truyền nội dung qua tham số `values` với `-f -`. kagent ghi ra file tạm, vì helm-drift không đọc values từ stdin.
- **Chặn:** đọc file hoặc chạy chương trình trên máy chủ (`-f đường dẫn`, `--set-file`, `--post-renderer`, `--ca-file`, `--cert-file`, `--key-file`, `--keyring`, `--verify`, `--custom-diff`, `--temp-path`, `--output-dir`); đổi danh tính hay đích đến (`--kubeconfig`, mọi `--kube-*` trừ `--kube-version`); thông tin đăng nhập repo (`--username`, `--password`). Không dùng `--dry-run` trực tiếp, vì đã có bước chạy thử.
- **Thời gian chạy:** lệnh đọc mặc định 60 giây, lệnh ghi 300 giây, tối đa 600. Lệnh ghi chưa có `--timeout` thì kagent tự thêm `--timeout` bằng đúng thời gian đó, để helm tự dừng chờ trước khi tiến trình bị giết, tránh release kẹt ở `pending-upgrade`.
- **Kết quả tin cậy:** chỉ `list` và `version`. `status` có NOTES, `history` có mô tả, manifest do người viết chart ghi.
- **Image:** cài helm từ `HELM_URL` (mặc định get.helm.sh v3.19.0; helm-diff 3.15 cần helm đủ mới để đọc `platformHooks` trong plugin.yaml), helm-diff từ `HELM_DIFF_URL`, helm-drift từ `HELM_DRIFT_URL` vào `/opt/helm/plugins`. Mạng nội bộ thì đặt các biến `KAGENT_HELM_URL`, `KAGENT_HELM_DIFF_URL`, `KAGENT_HELM_DRIFT_URL` trỏ mirror. Đặt rỗng thì bỏ qua; công cụ sẽ báo "chưa cài" khi được gọi.


## OpenStack

Kết nối dán nguyên clouds.yaml hoặc file openrc, rồi đăng nhập Keystone v3 bằng password, application credential hoặc token.
- **Mật khẩu:** openrc tải từ Horizon không chứa mật khẩu (dòng `OS_PASSWORD=$OS_PASSWORD_INPUT`), nên điền vào ô `password`.
- **File nhiều cloud:** clouds.yaml có nhiều cloud thì điền ô `cloud`.
- **Token:** lưu trong bộ nhớ tới gần lúc hết hạn. Gặp 401 thì kagent đăng nhập lại một lần.
- **TLS:** `cacert` và `verify` trong clouds.yaml không có tác dụng, vì `cacert` là đường dẫn file trên máy khác. Dùng ô `ca_cert` và `verify_tls` của kết nối.
- **Endpoint:** lấy từ service catalog theo `interface` (mặc định public) và `region`. Mỗi endpoint vẫn qua kiểm tra mạng như mọi đích khác. Endpoint nằm trong mạng nội bộ thì org_admin phải bật `allow_private` cho kết nối.

Agent gọi API bằng `service` (type trong catalog, hoặc tên quen như nova, neutron, cinder, glance), `path` tính từ URL endpoint, `query`, `body` và `microversion`.
Công cụ chia theo việc lệnh gọi làm gì. Role của tài khoản trong project vẫn là giới hạn thật, nên nên dùng application credential với role `reader` hoặc `member`.

| Công cụ | Gọi được |
| --- | --- |
| api_read R1 | GET, HEAD; POST action `os-getConsoleOutput` (log console máy ảo) |
| api_write R3 | POST, PUT, PATCH: tạo hoặc sửa tài nguyên, action máy ảo (os-start, os-stop, reboot, resize, migrate...) |
| api_admin R4 | Mọi lệnh, gồm: DELETE; action phá dữ liệu (rebuild, forceDelete, evacuate, reset state, revert volume, khôi phục backup đè volume); mọi thay đổi Keystone; đọc bí mật (payload Barbican, credential EC2, mật khẩu máy ảo) |

OpenStack không có chế độ chạy thử phía server, nên api_write và api_admin không có bước kiểm tra trước. Kết quả bị coi là chưa tin cậy, vì tên, mô tả, metadata, tags, user_data và tên mạng do khách hàng tự ghi; agent vẫn thấy nguyên kết quả. Khi học tri thức từ sự cố, api_read có `trusted_view`: chỉ giữ các trường theo danh sách cho phép (id, status/state, host, AZ, flavor, size, thời gian, IP...), giá trị chuỗi phải là một token không khoảng trắng, chỉ đi vào nhánh có tên đã biết (`servers`, `volumes`, `items`...). Phần còn lại là căn cứ tin cậy cho KU.

## Tự xử lý cảnh báo (tab Sự cố)

Bật ở **Cài đặt → Cảnh báo** (tenant admin). Nguồn là các kết nối Alertmanager/Prometheus dùng chung. Chỉ nên chọn một trong hai cho cùng một cụm, vì cảnh báo của Prometheus chính là cảnh báo gửi sang Alertmanager, chọn cả hai sẽ bị đếm đôi.

1. **Lấy cảnh báo:** vòng nền 15 giây; mỗi tenant poll theo `poll_seconds` (mặc định 60). Một lần bắn của cảnh báo = (nguồn, fingerprint, startsAt). Cảnh báo không còn trong danh sách thì được đánh dấu đã hết; mọi cảnh báo của sự cố hết thì sự cố chuyển sang "đã hết".
2. **Gom nhóm**, xét trong khung `window_minutes` (mặc định 10 phút) và cùng môi trường:
   - **Luật nhãn:** mọi nhãn trong `group_labels` mà hai cảnh báo cùng có phải bằng nhau. Mặc định là `cluster`, `namespace`, nên kết quả không phụ thuộc thứ tự cảnh báo đến.
   - **Quan hệ k8s:** cùng node, hoặc cùng workload (suy từ tên pod hay nhãn deployment/statefulset). Nhãn pod/service/instance của exporter kube-state-metrics bị bỏ qua.
   - **AI:** chỉ dùng khi hai cách trên không quyết được; gộp nếu điểm ≥ `ai_min_score`. Lần Gộp/Tách gần nhất của người dùng được đưa vào làm ví dụ.
3. **Điều tra:** sau `debounce_seconds`, agent chạy bằng tài khoản `kagent-alerts` (vai trò member) và chỉ được dùng công cụ đọc. Công cụ ghi bị chặn ngay cả khi model tự gọi tên. Kết quả là báo cáo gồm tiêu đề, nguyên nhân, độ tin, bằng chứng và tối đa 2 hành động. Bước điều tra luôn đối chiếu với thay đổi ngay trước cảnh báo đầu tiên: `data.recent_changes` cho lệnh ghi đã chạy qua kagent, và với kết nối đang có thì rollout history, helm history, deploy GitLab, instance action của OpenStack cho thay đổi làm ngoài kagent.
4. **Hành động:** đi qua gateway như mọi lệnh khác. Bị ép hỏi khi:
   - tự xử lý đang tắt ở môi trường đó;
   - đã vượt `max_auto_actions_per_hour`;
   - lần tự xử lý trước không làm hết cảnh báo.

   Không bị ép hỏi thì gateway áp ma trận như mọi lệnh. Ô auto mode (ví dụ R3 ở dev) được classifier xét theo `auto_rules`, tức luật tự xử lý do admin viết. Luật này đứng ở chỗ lời người dùng, kèm tên và nhãn của cảnh báo. Classifier không đọc báo cáo, log hay kết quả điều tra, vì những thứ đó viết từ dữ liệu người ngoài ghi được. Luồng này không có agent để thử cách khác, nên classifier chặn thì chuyển ngay thành yêu cầu duyệt. Độ tin của báo cáo chỉ để tham khảo, không còn quyết định tự chạy.
5. **Xác minh:** lệnh chạy xong (tự động hoặc sau khi duyệt), sau `verify_minutes` kagent xem cảnh báo của sự cố đã hết chưa. Hết thì "đã xử lý"; còn thì "cần người xử lý", và lần sau không tự chạy nữa.

**Topology:** kagent quét k8s và OpenStack của kết nối dùng chung 6 giờ một lần, và quét lại khi có sự cố mới (lần quét gần nhất cách hơn 15 phút); tầng cần quyền admin OpenStack (hypervisor, backend lưu trữ) chỉ dựng khi tài khoản có quyền. Công cụ `data.topology` trả cạnh "tin cậy" hoặc "gợi ý" kèm lệnh đọc lại; xem `docs/TOPOLOGY-PLAN.md`.

**Thông báo:** Cài đặt → Cảnh báo có mục webhook (tenant admin): sự cố mới, lệnh chờ duyệt, sự cố cần người được POST JSON có trường `text` kèm link về trang sự cố; chỉ báo việc xảy ra sau lúc bật, mỗi việc một lần.

Màn **Sự cố** gồm danh sách (lọc theo trạng thái, tự làm mới 15 giây, dải số liệu 30 ngày: tỉ lệ nhận định đúng, MTTR, mức xử lý) và trang chi tiết: nguyên nhân, bằng chứng, các lệnh đã đọc, cảnh báo kèm lý do được gộp (nút Tách), hành động kèm lý do không tự chạy (nút Duyệt/Từ chối), chấm nhận định của lượt điều tra (Đúng / Đúng một phần / Sai), thẻ Mức độ (đánh giá ảnh hưởng → S1–S4 và có cần postmortem), dòng thời gian, nút Gộp/Đóng/Điều tra lại.

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
       # trusted_when: module:ham    (args) -> True khi lệnh này chỉ trả dữ liệu có cấu trúc; hạ cờ cả khi agent làm việc
       # trusted_view: module:ham    (result) -> phần có cấu trúc; chỉ dùng khi học tri thức từ sự cố
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

- **Chạy song song:** khi model gọi nhiều lệnh trong cùng một lượt, các lệnh đọc liền nhau chạy song song. Mỗi lệnh ghi chạy riêng, theo đúng thứ tự model đưa ra, để hai lệnh ghi không đè nhau và lệnh đọc không thấy trạng thái giữa chừng của một lệnh ghi. Ở chế độ heuristic, lệnh đọc chạy trước, lệnh ghi chạy sau.
- **Giới hạn song song:** mặc định 8 lệnh một lượt. Tenant hạ được bằng khóa `max_parallel_tools` trong overlay chính sách, nhưng không tăng được quá baseline. Mỗi kết nối chạy tối đa 4 lệnh cùng lúc (`gateway.PER_CONNECTION`), tính cả các lượt khác, để không dồn tải vào một API server.
- **Agent con** (`agent.delegate {task, tools}`): agent chính giao một việc tra cứu đơn giản, gọi nhiều lần trong một lượt để các agent con chạy song song. Để rẻ:
  - Agent con chỉ nhận môi trường cùng bản giao việc. Không có lịch sử, KU hay lời nhắc hệ thống của agent chính, nên `task` phải tự đủ nghĩa.
  - Agent con chỉ thấy các công cụ đọc được giao trong `tools`, cộng `data.query` để lọc kết quả lớn. Gọi công cụ khác thì bị chặn, giao công cụ ghi thì lệnh giao việc báo `invalid`.
  - Agent con dùng model tầng `fast`, tối đa `max_subagent_steps` (6) bước, cache TTL 5 phút. Agent chính chỉ nhận lại bản tóm tắt.
  - Lệnh của agent con vẫn qua gateway với ý định gốc của người dùng và được ghi vào trace, có `sub` là số của agent con. Token được tính vào lượt và có mục đích `subagent` trong thống kê.
- **Chạy nền** (`agent.run_background {tool, args}`): chạy một lệnh đọc lâu ở nền, ví dụ `kubectl rollout status` hoặc `wait` với `timeout_seconds` tới 120, truy vấn chậm, hay `agent.delegate`. Lệnh trả ngay `job_id`, agent làm việc khác trong lúc chờ.
  - Kết quả được chèn vào sau lệnh kế tiếp của agent, cùng cách với tin người dùng nhắn thêm.
  - Agent định kết thúc lượt khi còn lệnh nền thì runner đợi lệnh xong đầu tiên, đưa kết quả cho agent rồi chạy tiếp. Mỗi lần như vậy tính một bước.
  - Hết lượt (hết bước, lỗi, dừng) mà lệnh nền chưa xong thì lệnh bị bỏ, ghi vào trace là `background_dropped`. Vì chỉ là lệnh đọc nên bỏ được.
  - Chỉ nhận công cụ đọc có trong danh sách của lượt. Lệnh vẫn qua gateway. Trong trace, lệnh nền có `background` là `job_id`.
- **Công cụ học được** (`learned.<tên>`) là chuỗi bước gọi các công cụ ở trên, sinh từ thao tác lặp lại hoặc lưu từ chat:
  - Mỗi bước vẫn qua gateway với quyền của người gọi.
  - Nếu kết nối của một bước bị gỡ hoặc tắt, công cụ học được bị chặn theo.
  - Tham số `{{x}}` đặt được giữa chuỗi, ví dụ `{"command": "scale deployment/{{dep}} --replicas={{n}} -n {{ns}}"}`. Giá trị thay vào chỉ được là một từ gồm chữ, số và `._:/=@+,-`, không mở đầu bằng `-`, nên không chèn thêm được flag. `{{step:N}}` vẫn phải đứng riêng một giá trị.
  - Khi học, hệ thống tự tách tham số từ `command`: so các lệnh cùng khung ở những lượt lặp lại, phần khác nhau thành tham số; khi lưu từ một lượt chat, từ nào có trong câu yêu cầu thành tham số. Các chuỗi khác như SQL, URL được giữ nguyên.
