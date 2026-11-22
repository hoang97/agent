# Kế hoạch nâng cấp gom cảnh báo

Cập nhật 2026-11-20. Tham chiếu sách *SRE with AIOps* (Behl, Kanikarapu 2026): Ch4 "Advanced alert correlation and noise
reduction" (tr. 124–127) và Ch8 "Intelligent event grouping" (tr. 218–219), đối chiếu với `kagent/incidents/grouping.py`.

Mục tiêu: một sự cố thật thành đúng một sự cố trên kagent. Không gom chung những sự cố không liên quan, không tách lẻ một sự cố
thành nhiều cái. Trong mỗi sự cố, chỉ ra cảnh báo nào là gốc, cảnh báo nào là triệu chứng. Mọi thay đổi phải đo được trên
dữ liệu thật trước khi bật.

## Đã làm (2026-11-20, nhánh `feat/alert-grouping`)

Đã làm đủ 7 bước bên dưới, mỗi phần có cờ riêng trong `grouping` của cài đặt cảnh báo, mặc định tắt hết. Tắt hết thì kagent gom
y như cũ: 62 test của `test_incidents.py` không phải sửa dòng nào.

| Bước | Code | Cài đặt |
|---|---|---|
| 0. Đo và chạy lại | `incidents/quality.py`, mục "Gom cảnh báo" ở trang Đo lường, `scripts/grouping_replay.py` | luôn bật |
| 1. Gom theo lô | `grouping.group_new`, `scoring.cluster` | `grouping.batch` |
| 2. Điểm bằng chứng, topology | `incidents/scoring.py`, `incidents/topology.py`, bảng `topology_edge` | `grouping.scoring`, `grouping.topology_minutes` |
| 3. Gốc và triệu chứng | `scoring.roles`, `grouping.apply_roles`, cột `alert_event.role` | có topology là có |
| 4. Lịch sử gộp/tách | `grouping.pair_stats`, `grouping.blocked_pairs` | trong `grouping.scoring` |
| 5. LLM quyết phần lưng chừng | `grouping._place`, `grouping._correlate` | trong `grouping.scoring`, `grouping.ai_max_calls` |
| 6. Giảm nhiễu | `ingest.insert` (cột `alert_event.flapping`), `grouping._attach` | `grouping.flap_count`, `grouping.open_min_severity` |

Khác với bản plan đầu tiên:

- **Khi chấm điểm, `group_labels` không còn là luật 1.0.** Giữ nó làm luật 1.0 thì để `[cluster]` sẽ gom cả cụm vào một sự cố,
  còn để `[cluster, namespace]` thì namespace lại thành bằng chứng đủ để gom, đúng cái cần bỏ. Thay bằng `partition_labels`
  (mặc định `[cluster]`): khác nhau ở các nhãn này thì không bao giờ chung sự cố, giống nhau thì không phải bằng chứng.
  `group_labels` chỉ còn dùng khi tắt chấm điểm, mặc định cho tenant mới giữ nguyên.
- **Đọc topology không qua gateway**, giống cách `ingest.fetch` lấy cảnh báo: đây là việc nền định kỳ, không có người dùng để auto
  mode xét ý định, mà ô R1 ở prod của ma trận mặc định là `judge`, nên đi qua gateway sẽ gọi classifier, có khi tạo thẻ duyệt,
  mỗi 30 phút. kagent đọc bằng quyền nhóm đọc của kết nối và vẫn ghi `tool_call_audit`. Mỗi namespace có cảnh báo trong 7 ngày
  là một lệnh `kubectl get svc,deploy,sts,ds,ingress -o jsonpath=…`. Kết quả bị cắt ở 20 000 ký tự thì báo "bị cắt".
- **Cạnh "gọi" suy từ biến môi trường của container** (vd. `CURRENCY_SERVICE_ADDR=currencyservice:7000`): tên Service cùng
  namespace, `svc.ns` với ns đã đọc, hoặc có hậu tố `.svc`. Giá trị biến chỉ đọc trong bộ nhớ, DB chỉ lưu cạnh.
- **Không có bảng `alert_pair_stat`.** Thống kê tính lại từ sự cố đã xong trong 90 ngày và các lần tách, giữ trong bộ nhớ 10 phút.
  Như vậy không phải móc vào mọi chỗ đóng/mở lại/gộp/tách, và không đếm trùng khi sự cố mở lại rồi hết lần nữa.
- **Vai trò gốc/triệu chứng chỉ có khi topology nối được các cảnh báo.** Không có topology thì không đoán theo giờ bắn.
- **Cảnh báo dưới `open_min_severity`** mà không khớp sự cố nào thì nằm ngoài (`incident_id` rỗng, lý do ghi ở `join_reason`).
  Sau này có sự cố khớp thì nó cũng không được kéo vào. Trang Đo lường đếm số cảnh báo này.
- **LLM chỉ được hỏi từ `suggest_threshold`** (mặc định 0.4). Hai cảnh báo khác namespace mà không có cạnh topology chỉ có điểm
  cùng đợt (0.2), nên không được hỏi. Luật cũ thì hỏi LLM cho mọi cảnh báo không khớp luật. Đây là đánh đổi cần replay
  với `--llm real` để biết có làm giảm recall trên dữ liệu thật hay không.

Còn phải làm trước khi bật cho tenant thật:

1. Lấy bản sao DB prod theo CLAUDE.md, chạy `scripts/grouping_replay.py` với `--llm off`, rồi `--llm real` (tốn tiền), lấy điểm
   mốc của cách gom hiện tại (dự kiến các sự cố `svc-document` ngày 13/11).
2. Bật `topology_minutes` trước (chỉ đọc, chưa đổi cách gom), xem số cạnh và lỗi ở trang cài đặt, chạy replay lại với topology đó.
3. Bật `batch`, rồi `scoring` nếu replay không giảm recall. Theo dõi tỉ lệ người tách/gộp tay ở trang Đo lường.

## Hiện trạng (trước đợt này)

Luồng hiện tại (`ingest.poll` → `grouping.assign`): xét từng cảnh báo mới theo thứ tự `starts_at`, rồi gắn vào sự cố đang mở
đầu tiên khớp theo thứ tự ưu tiên sau.

1. Luật nhãn: mọi nhãn trong `group_labels` (mặc định `cluster`, `namespace`) mà hai cảnh báo cùng có phải bằng nhau. Điểm 1.0.
2. Quan hệ k8s: cùng `node`, hoặc cùng workload (tách từ tên pod). Điểm 0.9.
3. LLM `correlate`: gọi một lần cho mỗi cảnh báo chưa khớp, xét 5 sự cố gần nhất, nhận khi điểm ≥ `ai_min_score`.
4. Mở lại sự cố đã hết trong `reopen_hours` nếu khớp luật 1 hoặc 2. Ngoài ra `auto_merge` tự gộp các sự cố đang mở khớp nhau.

| Kỹ thuật trong sách | kagent |
|---|---|
| Gom theo thời gian (cửa sổ ngắn) | Chưa có. `window_minutes` chỉ dùng để lọc sự cố ứng viên, không gom các cảnh báo bắn cùng lúc với nhau |
| Gom theo topology | Có một phần: cùng node hoặc cùng workload. Chưa có Service → workload, Ingress → Service, hay phụ thuộc giữa các dịch vụ |
| Gom theo ngữ nghĩa | Có qua LLM, nhưng gọi cho từng cảnh báo, nên một cơn bão cảnh báo tốn N lần gọi |
| Học từ lịch sử | Yếu: chỉ nhét 10 dòng gộp/tách gần nhất vào prompt |
| Điểm tin cậy của nhóm | Chỉ có điểm cho từng cảnh báo (`join_score`), không có điểm cho cả sự cố |
| Cảnh báo gốc (IRCA) và triệu chứng | Chưa có. "Gốc" là cảnh báo đến đầu tiên |

Các lỗi đã thấy hoặc suy ra được từ code:

- **Phụ thuộc thứ tự đến.** Ghi chú trong `config.py` đã nói: một cảnh báo thiếu nhãn có thể nối hai nhóm vốn không liên quan.
- **Gom thừa theo namespace.** Với `group_labels` mặc định, mọi cảnh báo trong một namespace dùng chung vào chung một sự cố.
  Cảnh báo chỉ có nhãn `namespace` (như `HighCPU · web` trong test) nối được với mọi thứ khác trong namespace đó.
- **Gom thiếu khi lỗi lan qua namespace.** `frontend` ở `shop` gọi `payments` ở namespace khác: không luật nào khớp, nên phải nhờ LLM,
  và LLM chỉ thấy 5 sự cố cùng 8 cảnh báo đầu của mỗi sự cố.
- **Gom sai thì phí điều tra.** Mỗi sự cố mở ra là một lượt điều tra tốn token. Bị tách lẻ thì điều tra trùng lặp, gom thừa thì
  agent bị nhiễu.
- **Không đo được.** Không biết tỉ lệ cảnh báo trên sự cố, cũng không biết người phải tách hay gộp tay bao nhiêu lần.

## Thiết kế từng bước

Xếp theo thứ tự đã làm. Mỗi bước có cờ riêng trong cài đặt cảnh báo, mặc định tắt cho tới khi bước 0 cho thấy kết quả tốt hơn.
Chỗ nào khác với phần dưới thì theo mục "Đã làm" ở trên.

### 0. Đo chất lượng gom và chạy lại trên dữ liệu cũ

Làm trước tiên, vì mọi bước sau đều cần có số để so.

- **Chỉ số online**, hiện ở trang Đo lường, theo tenant và môi trường:
  - tỉ lệ nén: số cảnh báo / số sự cố (sách đặt mục tiêu 100:1 cho bão cảnh báo)
  - tỉ lệ người tách (gom thừa) và tỉ lệ người gộp tay hoặc nhận gợi ý gộp (gom thiếu), tính trên số sự cố
  - số lần gọi LLM `correlate` trên mỗi cảnh báo
  - thời gian từ lúc cảnh báo đầu tiên bắn tới lúc mở sự cố (MTTE trong sách)
- **Replay offline** (`scripts/grouping_replay.py`): lấy `alert_event` từ bản sao DB của tenant, chạy lại thuật toán gom theo thứ tự thời gian,
  rồi so với nhóm "đúng". Nhóm đúng là trạng thái cuối cùng sau khi người đã tách/gộp tay; sự cố không ai sửa coi là đúng.
  Chấm bằng pairwise precision/recall: hai cảnh báo có cùng nhóm hay không.
- Chạy trên bản sao DB theo cách trong CLAUDE.md, không đụng DB đang chạy. LLM giả lập bằng câu trả lời đã ghi trong
  `join_reason`, để replay không tốn tiền.
- Hoàn thành khi: có điểm mốc của thuật toán hiện tại trên dữ liệu prod (dự kiến lấy các sự cố `svc-document` ngày 13/11).

### 1. Gom theo lô và cửa sổ thời gian

- Các cảnh báo mới trong cùng một vòng poll được gom thành cụm trước, bằng thành phần liên thông theo `match`. Sau đó mỗi cụm mới
  được khớp với sự cố đang mở, thay vì xử lý từng cảnh báo một. Như vậy kết quả không còn phụ thuộc thứ tự đến.
- Cảnh báo bắn cách nhau ≤ `burst_seconds` (mặc định 120, theo cửa sổ 2 phút ở tr. 218) có thêm điểm "cùng đợt". Chỉ cùng đợt thôi
  thì chưa đủ để gom, phải có thêm ít nhất một bằng chứng khác (bước 2).
- LLM gọi một lần cho mỗi cụm, không phải mỗi cảnh báo.
- Hoàn thành khi: bão 50 cảnh báo cùng workload trong test mở đúng 1 sự cố với tối đa 1 lần gọi LLM; replay không giảm recall.

### 2. Điểm bằng chứng thay cho khớp có/không, và topology k8s thường trực

Bỏ kiểu khớp có/không, thay bằng tổng điểm từ nhiều bằng chứng. Gom khi đạt `join_threshold`; nằm giữa `suggest_threshold`
và `join_threshold` thì đưa vào gợi ý gộp (`suggestions` đã có). Bảng điểm ban đầu, chỉnh lại theo replay:

| Bằng chứng | Điểm |
|---|---|
| Cùng cảnh báo bắn lại (cùng fingerprint) | 1.0 |
| Cùng workload | 0.9 |
| Có cạnh phụ thuộc trực tiếp (Service → workload, Ingress → Service, workload gọi Service) | 0.8 |
| Cùng node, khi cảnh báo là cảnh báo mức node hoặc pod | 0.7 |
| Cùng namespace, không có gì khác | 0.4: không tự gom, chỉ cộng thêm |
| Cùng đợt (bước 1) | +0.2 |
| Lịch sử cho thấy cặp này hay đi cùng nhau (bước 4) | +0.0 đến +0.3 |

- **Topology thường trực**: bộ lập lịch định kỳ chạy lệnh đọc `kubectl get svc,deploy,sts,ds,ingress -A -o json` qua kết nối k8s
  của tenant, dựng các cạnh selector và backend, lưu vào bảng `topology_edge(tenant, env, src, dst, kind, seen_at)`. Đọc không qua
  gateway mà như lấy cảnh báo, có ghi audit (xem mục "Đã làm").
- Bảng này cũng là nền cho panel topology và cho M3 (RCA) sau này: panel đọc từ đây, không chỉ dựng từ một lượt điều tra.
- Khi chấm điểm, namespace chỉ còn là bằng chứng yếu; `group_labels` không dùng nữa mà thay bằng `partition_labels`
  (xem mục "Đã làm"). Mặc định `group_labels` không đổi.
- Hoàn thành khi: ca currencyservice của `seed_panel_demo.py`, chuyển `frontend` sang namespace khác, vẫn gom thành 1 sự cố nhờ cạnh
  phụ thuộc; hai lỗi không liên quan trong cùng một namespace dùng chung tách thành 2 sự cố.

### 3. Cảnh báo gốc và triệu chứng

- Trong mỗi sự cố, chọn một cảnh báo gốc. Theo topology: cảnh báo ở đối tượng mà các cảnh báo khác nằm phía hạ nguồn của nó
  (đi ngược chiều gọi). Hoà thì xét cảnh báo bắn sớm hơn, rồi nặng hơn. Lưu vào `alert_event.role` (`root`/`symptom`) cùng lý do.
- Tiêu đề sự cố lấy theo cảnh báo gốc. Prompt điều tra liệt kê cảnh báo gốc trước, có ghi rõ đâu là triệu chứng.
- Trang sự cố và panel nhóm triệu chứng dưới cảnh báo gốc. Khi điều tra ra nguyên nhân khác, agent được sửa vai trò này.
- Đây chỉ là gợi ý thứ tự cho điều tra, không thay RCA (M3).

### 4. Học từ lịch sử gộp và tách

- Thống kê theo cặp khoá `alertname` cộng loại đối tượng (vd. `KubeServiceNoEndpoints/svc`): các cặp trong cùng một sự cố đã xong
  cộng `together`, mỗi lần người tách cộng `split`. Tính lại từ dữ liệu, giữ 10 phút, không có bảng riêng.
- Điểm lịch sử = độ tin của tỉ lệ `together/(together+split)`, chỉ tính khi đã có ít nhất 3 lần quan sát.
- Người đã tách một cặp thì cặp đó không được tự gom lại trong 7 ngày, kể cả khi luật khớp.
- Cái này thay cho việc nhét 10 dòng phản hồi vào prompt LLM. Prompt vẫn nhận thống kê của các cặp liên quan.

### 5. LLM chỉ quyết phần lưng chừng

- Chỉ gọi `correlate` khi tổng điểm nằm giữa hai ngưỡng, tức là khi luật và topology chưa đủ chắc để tự quyết.
- Đưa cho LLM: cụm cảnh báo, các ứng viên kèm cạnh topology và thống kê cặp. Không giới hạn cứng 5 sự cố, mà chọn theo điểm.
- Giới hạn số lần gọi mỗi vòng poll. Hết lượt gọi thì cụm còn lại mở sự cố riêng và vào gợi ý gộp.
- Ghi quyết định và điểm vào `join_reason` để replay dùng lại được.

### 6. Giảm nhiễu trước khi mở sự cố

- **Cảnh báo chập chờn**: cùng fingerprint bắn rồi hết từ 3 lần trở lên trong 1 giờ thì đánh dấu `flapping`. Vẫn gắn vào sự cố cũ
  nhưng không mở lại điều tra mỗi lần bắn. Dòng sự kiện ghi "chập chờn N lần".
- **Mức nghiêm trọng thấp**: cảnh báo `info` hoặc không có mức chỉ gắn vào sự cố đang có. Nếu không có sự cố nào phù hợp thì
  không mở sự cố mới và không điều tra; cài đặt `open_min_severity`, mặc định tắt (gợi ý `warning`).
- Có thể làm bước này song song với bước 1, vì độc lập và rẻ.

## Không làm trong đợt này

- Gom log theo ngữ nghĩa bằng embedding (Ch4 "semantic similarity"). Hiện nguồn cảnh báo chỉ có Alertmanager và Prometheus,
  chưa có cảnh báo từ log. Xem lại khi làm M4 (ảnh chụp chẩn đoán, gom mẫu log).
- Mô hình ML phân cụm (DBSCAN) hay điểm xác suất học có giám sát. Chưa đủ dữ liệu có nhãn; điểm bằng chứng cộng thống kê cặp
  là bước đệm hợp lý, và có thể thay sau mà không đổi giao diện.
- Topology từ trace hoặc service mesh. Để cho M3.

## Kiểm thử

- Sửa `incidents/*` thì chạy `test_incidents.py test_grouping.py`. `tests/test_grouping.py` có điểm bằng chứng, gom theo lô,
  cảnh báo gốc. Thêm migration trong `db.py` thì chạy cả bộ.
- Mỗi bước phải chạy replay của bước 0 trên bản sao DB và ghi kết quả (precision, recall, tỉ lệ nén, số lần gọi LLM) vào commit.
  Không được giảm recall so với điểm mốc.
