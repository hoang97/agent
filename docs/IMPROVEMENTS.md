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

## Đợt 18 — 2026-11-11: thử luồng duyệt trên trình duyệt với Kibana và Kubernetes thật

Điều tra lỗi 500 của cmp-backend devtest chạy trọn từ đầu tới nguyên nhân gốc (IaaS trả 409 Conflict, `clients/iaas/http.py` không map lại mã lỗi), không còn vòng lặp duyệt lại. Những gì thấy khi thử:

| Vấn đề | Thay đổi |
| --- | --- |
| Lệnh đọc R0 (`list_pods`) ở dev vẫn phải duyệt sau khi đọc log | Bảng cấu hình theo môi trường thêm cột "lệnh đọc tự chạy khi có nội dung untrusted" (`none` / `R0` / `R1`); mặc định dev R0, staging và prod không; prod tối đa R0 |
| Thẻ duyệt chỉ hiện khi lượt kết thúc | Thẻ hiện ngay khi lệnh chuyển sang chờ duyệt, nút duyệt hiện khi lượt xong |
| Agent chép lại `approval_id`, trạng thái, lý do chặn thành bảng | System prompt: giao diện đã có thẻ duyệt, chỉ nói ngắn lệnh nào chờ và để làm gì; sau khi duyệt agent tự được chạy tiếp |
| Nút "Tiếp tục" còn lại trên lượt đã được chạy tiếp, và hiện cả khi agent đang chạy | Ẩn nút khi lượt chạy tiếp bắt đầu, làm mới lượt gốc khi lượt chạy tiếp xong |

Còn mở: điểm tin cậy thực tế rất thấp (0,02–0,34 với hệ thống mới, chưa có Skill và tiền lệ), nên ngưỡng mặc định 0,5 của grant ở staging gần như không bao giờ đạt.

## Đợt 19 — 2026-11-11: đo token và lấy dữ liệu gọn

Số đo trên lượt thật "có bao nhiêu lỗi ERROR trong log cmp-backend devtest 1 giờ qua": **326.664 token trước, 22.774 token sau** (giảm 93%). Lượt cũ có một `search_logs` trả 228.574 ký tự và `list_indices` trả 22.019 ký tự, đổ thẳng vào context rồi bị gửi lại ở mọi bước.

- **Metrics token:** mỗi lần gọi LLM ghi vào bảng `llm_call` (mục đích, model, input, output, cache đọc/ghi); mỗi bước công cụ ghi `result_chars`. Trang Đo lường có token theo mục đích, tỷ lệ cache, công cụ trả kết quả lớn nhất và các lượt tốn nhất.
- **Prompt caching (Anthropic):** breakpoint ở system (phủ cả danh sách công cụ) và ở message cuối mỗi vòng; relay từ chối `cache_control` thì tự tắt và gửi lại. Relay của người dùng hỗ trợ caching.
- **Kết quả lớn thành result_id:** kết quả trên 6.000 ký tự được lưu ở bảng `tool_result` (chỉ người tạo đọc được, xóa sau 7 ngày); context nhận bản xem trước gồm các trường có sẵn và vài mẫu. Kết quả của lệnh chạy sau khi duyệt cũng vậy.
- **`_filter` cho mọi công cụ đọc:** path, where, fields, group_by, sort, limit, max_chars, chạy ở server trước khi vào context. Khi bản gốc lớn, kết quả có thêm `raw_result_id` để lọc lại mà không gọi lại hệ thống.
- **Kết nối nội bộ `data` cho mọi tenant:** `data.query` (R0, lọc trên result_id; không bị bắt duyệt vì nội dung untrusted vì không tác động ra ngoài) và `data.script` (R4, luôn cần duyệt): Python trong tiến trình con có rlimit CPU/RAM/ghi file/fork/fd và audit hook chặn open, socket, subprocess, os.*, import ngoài danh sách. Thẻ duyệt hiện code giữ nguyên xuống dòng.
- **Connector Elasticsearch** trả `_source` dạng object, chỉ cắt từng chuỗi dài. Trước đây cả `_source` bị dồn thành chuỗi JSON rồi cắt, nên không lọc hay parse được (thấy khi chạy `data.script` thật: 100/100 bản ghi không parse được).
- **Công cụ học được** giữ `_filter` trong tham số các bước; bỏ qua bước `data.*` vì chúng gắn với result_id của một lượt.

## Đợt 20 — 2026-11-11: công cụ chuẩn hóa do agent đề xuất, đếm phía máy chủ

| Lượt thật (log cmp-backend devtest) | Token xử lý | Lệnh công cụ | Ghi chú |
| --- | --- | --- | --- |
| Trước đợt 19 | 326.664 | 7 | `search_logs` 228.574 ký tự vào context |
| Sau đợt 19 | 22.774 (+ lượt chạy tiếp) | 4 | `_filter`, result_id |
| Dùng lại công cụ chuẩn hóa | ~19.800 (phần lớn cache) | 1 | đếm đúng trên toàn bộ 7.009 bản ghi |

- **`data.propose_tool`:** agent đề xuất công cụ chuẩn hóa từ các bước đã chốt (không giữ bước thử sai), giá trị thay đổi thành tham số. Công cụ luôn ở trạng thái đề xuất; chủ sở hữu (cá nhân) hoặc admin (tenant) xem các bước rồi bật. Trang công cụ hiển thị từng bước, code giữ xuống dòng, cảnh báo khi có `data.script`.
- **`{{step:N}}`:** bước `data.query`/`data.script` trong công cụ học được đọc kết quả đầy đủ của bước trước (lưu thành result_id). Chỉ công cụ local được tham chiếu, nên kết quả untrusted không lọt vào tham số của lệnh gọi ra ngoài. Bước đã được bước sau xử lý chỉ trả result_id, không đổ dữ liệu thô vào context.
- **Script trong công cụ đã duyệt chạy không hỏi lại:** code cố định đã được người có quyền xem khi bật công cụ (công cụ tenant: admin; cá nhân: chính chủ khi tenant cho tự duyệt). Script viết trong chat vẫn luôn phải duyệt.
- **`count_logs` (Elasticsearch):** đếm và nhóm bằng aggregation trên toàn bộ bản ghi khớp; field text tự thử bản `.keyword` (cả khi Elasticsearch báo lỗi lẫn khi trả 0 nhóm). Trước đó agent đếm bằng `search_logs` size 5000 nhưng connector chỉ trả 100 bản ghi, nên số đếm sai mà không ai biết; nay `_filter` cảnh báo khi chỉ lọc trên một phần của tổng số khớp.
- **Reflector chỉ nhận 1.500 ký tự đầu kết quả của mỗi bước:** trang Đo lường cho thấy Reflector tốn ~28.000 token input mỗi lần vì nhận nguyên trace kèm kết quả thô.
- **Hết số bước không còn bỏ cả lượt:** khi chạm giới hạn (nâng từ 8 lên 12 bước), model buộc phải tổng kết những gì đã có. Trước đây lượt 15 lệnh chỉ trả "vượt số bước tối đa".

## Đợt 21 — 2026-11-11: LLM tự host trong mạng nội bộ

- **Vấn đề:** lấy danh sách model của dịch vụ tương thích OpenAI chạy trong mạng nội bộ báo "Base URL không được trỏ tới địa chỉ nội bộ". Base URL chỉ nhận https tới địa chỉ công khai, và key phải dài 20–300 ký tự, nên LLM tự host (vLLM, Ollama, LiteLLM) không dùng được.
- **Thay đổi:** form LLM có ô "Cho phép địa chỉ mạng nội bộ" (chỉ org admin, lưu cùng credential đã mã hóa). Khi bật: nhận địa chỉ nội bộ, nhận cả http nhưng chỉ khi mọi IP phân giải ra đều nội bộ, giao diện cảnh báo khi dùng http; dịch vụ tương thích OpenAI nội bộ nhận key ngắn. Địa chỉ công khai vẫn bắt buộc https; loopback, link-local (gồm metadata của cloud) luôn bị chặn.
- **Docker:** thêm `host.docker.internal` để gọi LLM chạy trên chính máy host (trong container, `localhost` là container).

## Đợt 22 — 2026-11-11: định dạng nội bộ lọt ra câu trả lời

- **Vấn đề:** thỉnh thoảng câu trả lời hiện dòng `[Lệnh kibana_devtest.count_logs {...}: approved <untrusted>...]`. Đó là định dạng mà `load_history` dùng để đưa kết quả lệnh đã duyệt vào lịch sử, nhưng nó được nối thẳng vào lời của agent ở lượt trước, nên model tưởng chính mình từng viết như vậy và chép lại (thấy 2 lần, với LLM tương thích OpenAI).
- **Thay đổi:** kết quả lệnh duyệt sau nằm trong một message riêng ghi rõ "[Hệ thống] … dữ liệu tham khảo, không phải lời của bạn". Câu trả lời của model, và câu trả lời cũ khi nạp lại làm lịch sử, được lọc bỏ dòng `[Lệnh …]` và thẻ `<untrusted>`, gộp dòng trống thừa.

## Đợt 23 — 2026-11-11: "[object HTMLDivElement]" trên các trang quản trị

- **Vấn đề:** trang Biên dịch L2 hiện "[object HTMLDivElement],…" thay cho danh sách lần biên dịch. Các view gọi `main.append(...)` (API DOM gốc) với mảng phần tử, mà API này đổi mảng thành chuỗi; cùng lỗi ở trang Duyệt lệnh. Cũng vì vậy `isAdmin() && h(...)` hiện chữ "false" với người không phải admin, và trang Đo lường có thể hiện "null".
- **Thay đổi:** `h()` và vùng nội dung của mọi trang dùng chung một quy tắc thêm phần tử con: làm phẳng mảng, bỏ `null`/`undefined`/`false`. Đã duyệt qua mọi trang trên trình duyệt, không còn chuỗi lỗi nào.

## Đợt 24 — 2026-11-11: nhiều cấu hình LLM, biểu đồ token theo LLM

- **Lưu nhiều cấu hình LLM:** bảng `llm_profile` (tên, nhà cung cấp, Base URL, model; key mã hóa) và cấu hình đang dùng ở `app_setting`. Trang Cài đặt liệt kê cấu hình đã lưu với nút Dùng / Sửa / Xóa; sửa được mà để trống key để giữ key cũ, chỉ khi giữ nguyên nhà cung cấp và Base URL. Key duy nhất của phiên bản trước tự chuyển thành cấu hình "Mặc định". Xóa cấu hình đang dùng thì không tự chọn cấu hình khác.
- **Biểu đồ token theo LLM:** trang Đo lường có cột chồng theo ngày (14 ngày), mỗi "cấu hình · model" một màu cố định theo thứ tự xuất hiện lần đầu (không theo thứ hạng), quá 6 LLM gộp vào "Khác"; có chú thích, tooltip theo ngày và bảng số liệu. Palette đã chạy validator CVD/độ tương phản cho nền sáng và tối; ở nền sáng ba màu dưới 3:1 nên bảng số liệu luôn đi kèm.
- `llm_call` ghi thêm tên cấu hình; model lấy theo tier khi phản hồi stream của dịch vụ tương thích không có tên model (trước đây ghi rỗng, hiện là "(không rõ)").
- **Lỗi thấy khi xem trên trình duyệt:** CSP `default-src 'self'` chặn thuộc tính `style` viết thẳng, nên màu được gán bằng class.

## Đợt 25 — 2026-11-11: tri thức sinh quá nhiều, tổng hợp và duyệt hàng loạt

- **Vấn đề:** 122 KU ứng viên sau một buổi dùng, nhiều mục trùng ý (cùng một ý viết khác câu chữ hay khác ngôn ngữ, Curator dùng embedding băm theo từ vựng nên không nhận ra), nhiều mục nói về chính cơ chế của agent (gateway, duyệt lệnh).
- **Duyệt hàng loạt:** trang Tri thức có lọc theo loại, ô chọn từng KU, chọn tất cả, "Chấp nhận đã chọn" / "Loại đã chọn" (`POST /api/ku/review-batch`).
- **Tổng hợp trùng lặp:** job nền chia ứng viên theo loại, mỗi lô tối đa 40 KU, chạy song song 3 lô; LLM gom KU trùng ý thành nhóm kèm bản gộp và đề xuất loại KU ít giá trị. Chỉ là đề xuất: người duyệt sửa bản gộp, chấp nhận từng nhóm hoặc "Gộp và chấp nhận tất cả", loại các mục đề xuất. Bản gốc chuyển "bị loại" kèm `superseded_by` và sự kiện `MERGE`, truy vết được; bản gộp mang mức tin cậy nguồn thấp nhất trong nhóm. Không có LLM thì gom theo trùng từ vựng.
- **Lần thử đầu báo 502:** gửi cả 122 KU trong một request mất quá lâu qua relay (33 KU đã mất khoảng 60 giây). Nay chạy nền theo lô, giao diện hiện tiến độ. Trên dữ liệu thật: 5 lô, khoảng 1 phút, 72 KU gộp còn 23 và 21 KU đề xuất loại.
- **Giảm tốc độ sinh mới:** Reflector rút 0–3 KU mỗi lượt (trước là tối đa 5), không ghi điều về cơ chế của agent/gateway/duyệt lệnh, không ghi lại điều đã có trong KU đã nạp.

## Đợt 26 — 2026-11-11: giữ phiên và trạng thái các màn qua refresh

- **Phiên trượt:** cookie phiên (HttpOnly) trước đây hết hạn cố định 12 giờ sau khi đăng nhập. Nay đang dùng thì tự gia hạn (khi còn dưới nửa thời hạn, để không ghi DB mỗi request), hết hạn sau 12 giờ không hoạt động, tối đa 30 ngày kể từ lúc đăng nhập.
- **Vị trí trên URL:** `#/t/<tenant>/<màn>[/<hội thoại>]`; refresh, Back/Forward và link mở đúng tenant, màn, hội thoại.
- **Trạng thái từng màn** lưu trong localStorage theo người dùng và tenant: tin nhắn đang gõ (theo từng hội thoại), môi trường chat, bộ lọc và form thêm KU, đề xuất tổng hợp KU (đang chạy thì theo dõi tiếp, xong thì mở lại, bỏ các KU đã xử lý trong lúc đó), form kết nối và kết nối đang sửa, form cấu hình LLM và cấu hình đang sửa, overlay chính sách đang soạn. Không bao giờ lưu key, mật khẩu, secret hay kubeconfig; bản nháp xóa khi lưu thành công.
- **Lượt chat đang chạy khi refresh:** lượt vẫn chạy trên server; hội thoại trả `running`, giao diện hiện "agent vẫn đang trả lời" rồi tự hiện câu trả lời khi xong.
- Đã thử trên Chrome: refresh ở màn Tri thức (giữ bộ lọc và nháp), ở chat (giữ hội thoại, nháp tin nhắn), ở Cài đặt (giữ cấu hình đang sửa, ô key vẫn trống), và nút Back.

## Đợt 27 — 2026-11-11: biểu tượng xoay cho dòng đang thực hiện

- Helper `busy(text)`: spinner 12px ở đầu dòng kèm nội dung, `role="status"`. Dùng cho: tiến độ tổng hợp KU, công cụ đang chạy trong chat, lượt chat đang chờ sau khi tải lại trang, kiểm tra kết nối, tải danh mục công cụ và công cụ học được. Hệ thống bật giảm chuyển động thì spinner đứng yên thành chấm tròn.
- Kiểm tra kết nối mà request lỗi thì xóa spinner thay vì để quay mãi.

## Đợt 28 — 2026-11-11: đề xuất tổng hợp KU mất khi F5 hoặc chuyển màn

- **Nguyên nhân:** job chỉ nằm trong bộ nhớ server, nên mất mỗi lần server khởi động lại (kể cả khi triển khai bản mới). Ngoài ra trong lúc theo dõi, chỉ một lần hỏi tiến độ bị lỗi (server đang khởi động lại, mạng chập chờn) là giao diện xóa job đã ghi nhớ.
- **Thay đổi:** job lưu trong bảng `ku_consolidation` (danh sách lô, lô đã xong, kết quả từng lô ghi ngay khi xong). Server khởi động lại thì chạy tiếp các lô còn dở, không chạy lại lô đã xong; job quá 1 ngày thì đánh dấu lỗi. Giao diện chỉ bỏ job khi server trả 404 (đã có lần tổng hợp mới hơn); lỗi tạm thời thì hiện "mất kết nối, đang thử lại" và tiếp tục.
- Đã thử trên trình duyệt với job 27 KU: chuyển màn giữa chừng, F5 giữa chừng, khởi động lại container giữa chừng; job chạy tiếp và trang tự hiện đề xuất khi xong.

## Đợt 29 — 2026-11-11: còn ứng viên sau "Gộp và chấp nhận tất cả"

- **Vì sao còn 46 ứng viên:** đề xuất gần nhất chỉ xét loại procedure (một lần chạy thử khi sửa lỗi ở đợt 28 dùng bộ lọc này và thay thế đề xuất đầy đủ trước đó, vì mỗi tenant giữ một đề xuất), nên 42 ứng viên loại khác chưa được xét; còn 4 procedure không trùng với mục nào nên nút gộp không chạm tới. Giao diện không cho biết phạm vi của đề xuất.
- **Thay đổi:** tiêu đề đề xuất ghi phạm vi ("chỉ loại X" hoặc "mọi loại"); nút tổng hợp ghi phạm vi sẽ xét theo bộ lọc đang chọn; thêm "Chấp nhận N ứng viên không trùng"; ghi số ứng viên nằm ngoài phạm vi đề xuất và cách xét cả chúng.

## Đợt 30 — 2026-11-11: L2 không được publish, đề xuất khó duyệt

- **Vấn đề:** mọi lần biên dịch có đổi `core/rules.md` đều chờ duyệt cả gói, kèm cả playbook và hồ sơ thực thể; lịch nền tạo đề xuất mới mỗi lần học (9 đề xuất chờ duyệt dồn lại, cái sau chứa cái trước), nên từ khi có quy ước, playbook cũng không còn được publish. Đề xuất hiện dưới dạng git diff có mã KU và bộ đếm.
- **Tách phần tự publish:** playbook và hồ sơ thực thể publish ngay; chỉ khối core (nạp vào mọi lượt) và Skill chờ duyệt, theo đúng tài liệu kiến trúc.
- **Một đề xuất duy nhất:** đề xuất mới thay đề xuất cũ (cũ chuyển stale, xóa nhánh); nội dung cần duyệt không đổi thì giữ đề xuất cũ. Duyệt khi main đã đi tiếp thì áp riêng các file cần duyệt lên main hiện tại thay vì báo "đề xuất đã cũ".
- **Dễ đọc:** màn Biên dịch L2 hiện "Thêm N quy ước / Bỏ N quy ước / Giữ N" bằng lời, kèm nguồn (người dùng nói, từ kết quả công cụ, nội dung ngoài) và số lượt chat; nút "Bỏ quy ước này" cho từng quy ước (KU hết hiệu lực, đề xuất tự cập nhật); diff gốc thu gọn. Lịch sử publish thành bảng, ẩn bản stale và noop.
