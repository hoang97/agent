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

## Đợt 12 — 2026-11-11: công cụ thật từ template và kết nối

- **Bỏ mô phỏng:** xóa toàn bộ backend mô phỏng (`kagent/backends`) và danh mục cố định (`kagent/catalog`).
- **Template:** 7 template trong `kagent/templates`: Kubernetes, Grafana, Elasticsearch/Kibana, Web, HTTP API, PostgreSQL, MySQL. Connector trong `kagent/connectors` gọi REST API, WebSocket exec và driver CSDL thật.
- **Kết nối:**
  - Template cộng tham số riêng, gắn tenant (admin quản lý) hoặc cá nhân (chính chủ quản lý), và gắn môi trường.
  - Secret mã hóa, không trả lại client.
  - Công cụ đọc bật sẵn, công cụ ghi mặc định tắt.
- **Lớp mạng `kagent/net.py`:** chặn loopback, link-local, metadata; mạng nội bộ cần org_admin; ghim IP chống DNS rebinding; không tự theo redirect; giới hạn kích thước và thời gian.
- **Phân quyền:** công cụ cá nhân chỉ chủ sở hữu dùng. Luật chặn khớp cả tên theo template (`k8s.exec`) cho mọi kết nối. Lệnh chờ duyệt không chạy được nếu kết nối bị tắt hoặc gỡ sau đó.
- **Dữ liệu cũ:** công cụ mô phỏng còn trong DB tự chuyển removed khi khởi động; công cụ học được dựa trên chúng bị chặn.
- **Test:** chạy connector thật trên hạ tầng giả ở tầng HTTP (`tests/fakes.py`) và SQLite.
- **Lỗi phát hiện khi gọi Internet thật:** TLS dùng kho chứng chỉ hệ thống nên lỗi trên Windows; nay dùng bộ CA của certifi. Ngoài ra, tên công cụ học được từng kích hoạt nhầm từ khóa của công cụ khác trong planner heuristic.
- **Lưu ý về lịch sử commit:** các commit từ `6de5c6d` đến `fd1f497` không chạy được riêng lẻ, vì việc xóa phần mô phỏng lọt vào commit đầu tiên. Trạng thái chạy đúng là `3b6dc4a`. Muốn hoàn tác đợt này thì revert cả dải `git revert --no-commit 6de5c6d^..3b6dc4a`.

## Đợt 13 — 2026-11-11: LLM-as-judge cho episode chưa có kết quả

- **Vấn đề:** `outcome` chỉ suy từ trạng thái công cụ, nên lượt chat không gọi công cụ giữ `unknown`. Reflector thiếu tín hiệu, còn KU đã nạp cho các lượt đó không bao giờ được cộng điểm hữu ích. Tài liệu kiến trúc yêu cầu Reflector tự chấm khi chưa có kết quả.
- **Thay đổi:** trước khi phản tư, episode `unknown` được model mạnh chấm thành success / partial / failure, ghi `outcome_src='llm_judge'`. Judge trả `unknown` hoặc giá trị lạ thì giữ nguyên. Chế độ heuristic không chấm.
- **Không chấm:** lượt bị dừng, lượt còn lệnh chờ duyệt, và episode đã có kết quả (từ công cụ hoặc người dùng). Phản hồi người dùng gửi cùng lúc không bị ghi đè.
- **An toàn:** episode có nội dung untrusted mà judge chấm thành công thì không cộng điểm hữu ích cho KU đã dùng, vì nội dung đó có thể lái lời chấm.
- **README:** ghi rõ cần Python 3.10 trở lên; Python 3.9 không cài được.

## Đợt 14 — 2026-11-11: Docker Compose và lấy danh sách model

- **Docker Compose:** thêm `Dockerfile`, `docker-compose.yml`, `.dockerignore`. Image gốc kéo qua registry nội bộ, pip cài qua Nexus, vì ở mạng nội bộ đường Internet trực tiếp tới PyPI và mirror Debian quá chậm (pip timeout, `apt-get update` treo). Dùng bản `python:3.12-bookworm` đầy đủ vì có sẵn `git`, khỏi cần apt.
- **Lấy danh sách model:** tab Cài đặt có nút "Lấy danh sách model", gọi `POST /api/settings/llm-models` (org admin) rồi gợi ý tên model cho hai ô model. Dùng Models API của Anthropic (tự lật trang) hoặc OpenAI. Endpoint không hỗ trợ liệt kê thì báo để nhập tay.
- **An toàn:** ô key để trống thì dùng key đang lưu, nhưng chỉ khi nhà cung cấp và Base URL trùng cấu hình đã lưu; nếu không, đổi Base URL là gửi được key đã lưu tới máy chủ khác. Base URL vẫn qua kiểm tra HTTPS và địa chỉ công khai. Phản hồi không chứa key.

## Đợt 15 — 2026-11-11: Kibana và thông báo lỗi kết nối

- **Phát hiện khi thử kết nối thật của người dùng:** kết nối "Kibana devtest" báo `HTTP 302 : ` vì URL là Kibana, còn connector gọi API của Elasticsearch nên bị chuyển sang `/login`. Kết nối PostgreSQL báo `ConnectionTimeout` mà không rõ lý do: host `postgres-cluster-rw.postgres-cluster` là tên dịch vụ Kubernetes, còn DNS của mạng văn phòng lại phân giải nó ra một IP công khai.
- **Kibana:** connector Elasticsearch nhận ra Kibana (header `kbn-name` hoặc chuyển hướng tới `/login`) rồi gửi lệnh qua Console proxy `/api/console/proxy`. Với Elasticsearch thường, mỗi lệnh vẫn chỉ gửi một lần.
- **Lỗi dễ hiểu hơn:** lỗi 3xx nói rõ bị chuyển hướng tới đâu. Lỗi kết nối CSDL nêu `host:port` và lỗi của driver. Tên máy có dạng `service.namespace` hoặc `*.svc` thì có thêm gợi ý rằng tên đó chỉ phân giải được trong cluster.

## Đợt 16 — 2026-11-11: kết nối Kubernetes bằng kubeconfig

- **Theo yêu cầu của chủ dự án:** template `k8s` nhận nội dung kubeconfig (field `kubeconfig`, secret nhiều dòng) và `context` tùy chọn. `api_url` và `token` không còn bắt buộc; điền tay thì được ưu tiên hơn giá trị trong kubeconfig, ví dụ khi `server` trong kubeconfig là địa chỉ mà máy chủ kagent không tới được.
- **Hỗ trợ:** token, chứng chỉ client (mTLS), `certificate-authority-data`, `insecure-skip-tls-verify`. Không chạy được `exec`/`auth-provider` (aws, gcloud, oidc) vì máy chủ không được chạy lệnh ngoài; kubeconfig trỏ tới file trên máy cũng bị từ chối. Cả hai trường hợp đều báo rõ cách xử lý.
- **An toàn:** kubeconfig được mã hóa như mọi secret, không trả về giao diện. Địa chỉ `server` qua cùng lớp chống SSRF. Chứng chỉ client được ghi tạm với quyền 600 để nạp vào TLS, rồi xóa ngay.
- **Template:** thêm khóa `validate` để kiểm tra chéo các tham số khi tạo hoặc sửa kết nối, và kiểu field `secret_text`.
- **Lỗi thấy khi người dùng thử:** tạo kết nối bằng kubeconfig vẫn báo "cần api_url hoặc kubeconfig". File tĩnh trả về không có `Cache-Control` nên trình duyệt vẫn chạy `app.js` cũ: ô kubeconfig hiện thành ô một dòng, và giá trị bị gửi trong `params`, nơi server lặng lẽ bỏ qua secret. Nay file tĩnh trả `Cache-Control: no-cache` (vẫn nhận 304 nhờ ETag), và secret gửi nhầm trong `params` được nhận và lưu như secret.

## Đợt 17 — 2026-11-11: luồng duyệt lệnh

Phát hiện khi người dùng điều tra lỗi 500 qua Kibana thật: 6 lượt chat liên tiếp, lượt nào cũng chờ duyệt.

| Vấn đề | Thay đổi |
| --- | --- |
| "Cho phép trong 1 giờ" không có tác dụng: gateway bỏ qua grant khi có lý do ép hỏi, mà log từ `search_logs` làm cả hội thoại bị đánh dấu untrusted | Grant được vượt qua lý do untrusted theo cấu hình từng môi trường ở trang Công cụ & Chính sách (mặc định dev chỉ lệnh đọc, staging lệnh đọc khi tin cậy ≥ 0,5, prod không). Lý do ép hỏi khác (change freeze, đối tượng mới, quota) vẫn luôn phải hỏi |
| Duyệt xong agent dừng; người dùng gõ "đã duyệt", agent lại đặt truy vấn mới và lại phải chờ duyệt | Khi lượt mới nhất không còn lệnh chờ, agent tự chạy tiếp với kết quả đó; server chặn chạy tiếp hai lần |
| Kết quả lệnh đã duyệt vào lịch sử chỉ 300 ký tự đầu, agent không đọc được `request_id` | Lượt mới nhất nhận tới 24.000 ký tự kết quả, kèm tham số lệnh, bọc `<untrusted>` khi là log/web |
| Thẻ duyệt trong chat không hiện tham số; nhiều lệnh song song phải duyệt từng cái | Thẻ hiện tham số, kết quả thu gọn; nút "Chạy tất cả" và "Cho phép tất cả trong 1 giờ"; grant mới cấp chạy luôn các lệnh đọc cùng loại đang chờ |
