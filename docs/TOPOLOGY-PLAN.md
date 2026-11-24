# Topology nhiều tầng (bản phác, chờ góp ý)

Bản 2026-11-24. Giai đoạn A đã làm (`IMPROVEMENTS.md` Đợt 47). Đã chốt: topology là dữ kiện có thời hạn, mức tin theo từng cạnh (mục Nguyên tắc), và 5 điểm ở mục Đã chốt. Mục 9 trong kế hoạch theo tài liệu Cloud Operations (`INCIDENTS-PLAN.md`). Cần khớp với data model
nhóm Cloud Operations đang làm trước khi chốt; các quyết định ở mục **Đã chốt**, phần chưa quyết ở **Còn mở**.

## Vì sao cần

- Biên bản MVP: "có đủ thông tin topology từ lớp vật lý, hypervisor, cloud control plane, K8s, PaaS, ứng dụng, cho phép vẽ topology".
- Gartner G00834503: Topology Mapping là nền cho tương quan và Causal AI; thiếu topology động thì không tách được nguyên nhân khỏi triệu chứng.
- Alibaba Cloud Monitor 2.0 coi topology thực thể là kho dữ liệu riêng (UModel), ngang hàng log, metric, trace, event.
- **Dữ liệu của chính aiops (tenant devtest, 47 lượt điều tra):** agent tự nối các tầng bằng tay ở mỗi lượt. 68 lần gọi
  `/servers/detail` và `/volumes/detail`, 48 lần `get nodes`, phần lớn để đi từ cảnh báo etcd hay disk của node k8s xuống máy ảo
  OpenStack và volume bên dưới. 59/133 cảnh báo là `etcdHighCommitDurations`, 14 là `NodeDiskIOSaturation`: đúng loại sự cố mà
  nguyên nhân nằm ở tầng dưới (đĩa, backend lưu trữ, host) chứ không ở tầng cảnh báo bắn ra.
- Gom cảnh báo hiện chỉ dựa trên nhãn và quan hệ k8s (cùng node, cùng workload). Hai node k8s nằm trên cùng một host vật lý, hoặc hai VM
  dùng chung backend lưu trữ, vẫn thành hai sự cố riêng.

## Nguyên tắc: topology là dữ kiện có thời hạn

Hạ tầng thay đổi theo thời gian, kể cả ngay trong lúc sự cố (pod bị đẩy sang node khác, VM migrate, volume gắn lại). Nên topology không
phải một kho sự thật chung: mỗi cạnh chỉ đúng trong một khoảng thời gian, và mức tin tính theo từng cạnh tại thời điểm đang hỏi (thường
là lúc cảnh báo bắn).

| Cạnh | Mức tin cho câu hỏi "lúc t" |
| --- | --- |
| Đọc thẳng từ API, khoảng `valid_from`–`valid_to` bao trùm t | tin cậy (`verified_tool`), trích làm căn cứ được |
| Đọc từ API nhưng lần thấy gần nhất cũ hơn ngưỡng của loại cạnh | gợi ý; agent đọc lại trực tiếp thì thành tin cậy |
| Suy bằng khoá chung (`providerID` khớp UUID máy ảo) | gợi ý, tới khi một lần đọc xác nhận |
| Người khai tay (host vật lý, quan hệ dịch vụ) | như tài liệu nội bộ (`internal_doc`), có ngày rà lại |

Ngưỡng theo tốc độ thay đổi của loại cạnh: pod ↔ node chỉ tin bản đọc trong chính lượt điều tra; volume ↔ VM vài giờ; VM ↔ host tin được
khi không có sự kiện migrate sau lần thấy gần nhất; host ↔ rack gần như không đổi. Thấy sự kiện thay đổi trên cạnh thì đóng cạnh cũ ngay,
không chờ hết ngưỡng. Bản thân thay đổi topology là tín hiệu: ghi thành sự kiện thay đổi và hiện trong `data.recent_changes`, cạnh lệnh
ghi và deploy.

Hệ quả cho cách dùng: cạnh tin cậy dùng như mọi căn cứ khác; cạnh chỉ là gợi ý thì chỉ để agent biết đọc gì tiếp, để tạo gợi ý gộp
(không tự gộp) và gợi ý khoanh vùng ảnh hưởng cho người.

## Mô hình dữ liệu

Hai bảng, tách mô hình khỏi nơi lưu dữ liệu đo (như DataLink của UModel): topology không chứa metric, chỉ chứa thực thể, quan hệ và
cách tìm dữ liệu đo của thực thể đó.

**`entity`**: một thứ có thật đang chạy.

| Cột | Ý nghĩa |
| --- | --- |
| `id` | khoá ổn định: `<type>:<khoá tự nhiên>`, ví dụ `os.server:<uuid>`, `k8s.node:<cluster>/<name>`, `k8s.pod:<cluster>/<ns>/<name>` |
| `type` | xem bảng tầng bên dưới |
| `name`, `env` | tên hiển thị; môi trường của aiops |
| `attrs` | JSON: thuộc tính cần cho khoanh vùng (status, flavor, AZ, storage class...); không chứa secret |
| `source` | kết nối đã thấy nó (`k8s_devtest`, `openstack_x`) hoặc `manual` |
| `first_seen`, `last_seen` | lần quét đầu và gần nhất; quá N lần quét không thấy thì `gone_at` (giữ lại để điều tra sự cố cũ) |

Thực thể không mang mức tin; mức tin nằm ở cạnh.

**`entity_edge`**: quan hệ có hướng `from → to`.

| Quan hệ | Ví dụ |
| --- | --- |
| `runs_on` | pod → node, node k8s → VM, VM → hypervisor, hypervisor → máy vật lý |
| `uses` | VM → volume, pod → PVC → PV → volume, volume → backend lưu trữ |
| `member_of` | node → cluster, pod → workload (Deployment/StatefulSet), VM → project |
| `depends_on` | workload → service khác (từ Service/Endpoint, khai tay hoặc trace sau này) |

Mỗi cạnh có `source`, `how`: `api` (tham chiếu thẳng trong dữ liệu API: `providerID` của node trỏ UUID máy ảo, `volumeHandle` của PV,
volume gắn vào máy ảo), `match` (suy ra, ví dụ trùng IP nội bộ; luôn là gợi ý), `manual`; và khoảng hiệu lực `valid_from`, `valid_to` (NULL = đang đúng), `last_seen`. Quét lại thấy cạnh đổi thì đóng
cạnh cũ (`valid_to`) và mở cạnh mới, không ghi đè, để trả lời được "lúc cảnh báo bắn, pod chạy trên node nào". Cạnh `match` hiện kèm cách
khớp để người kiểm được.

**Ánh xạ cảnh báo → thực thể** (tính lúc đọc, không lưu bảng): từ nhãn `instance`, `node`, `pod`, `namespace`, `persistentvolumeclaim`, `device`
của cảnh báo. 104/133 cảnh báo có `instance`, 66 có `pod`, nên phần lớn cảnh báo gắn được ít nhất một thực thể.

## Tầng và nguồn

| Tầng | Kiểu thực thể | Nguồn có sẵn trong aiops | Khoảng trống |
| --- | --- | --- | --- |
| Vật lý | `dc.host`, `dc.rack` | chưa có | cần DCIM (biên bản nhắc) hoặc khai tay |
| Hypervisor | `os.hypervisor` | OpenStack `os-hypervisors`, trường host của server (cần quyền admin) | tài khoản aiops có thể không đủ quyền |
| Cloud control plane | `os.server`, `os.volume`, `os.network`, `os.project` | OpenStack `/servers/detail`, `/volumes/detail` | backend của volume (`os-vol-host-attr:host`) cần quyền admin |
| K8s | `k8s.cluster`, `k8s.node`, `k8s.workload`, `k8s.pod`, `k8s.pvc` | `kubectl get nodes,pods,pvc,pv -o json` | node ↔ VM nhờ `spec.providerID` (`openstack:///<uuid>`), cần kiểm trên cụm thật |
| PaaS, ứng dụng | `app.service` (CMP, AI Platform, DBaaS...) | nhãn `service` của cảnh báo, Service k8s | quan hệ giữa dịch vụ cần khai tay hoặc lấy từ trace |
| Khách hàng | `customer` | chưa có | mapping khách hàng ↔ project (module CRM trên CMP); khi có thì mức S1–S4 gợi ý được theo thực thể |

## Cách thu thập

- **Đọc trực tiếp lúc điều tra là mặc định:** `topology.neighbors` gọi API qua gateway với tài khoản bot (quyền, audit, giới hạn kết nối vẫn
  áp dụng), kết quả là bước `S<n>` bình thường nên trích làm căn cứ được, đồng thời cập nhật cạnh đã lưu.
- **Quét định kỳ chỉ phần ít đổi và rẻ** (node, VM, volume, PV) để có lịch sử cạnh và phát hiện thay đổi; pod và workload đọc lúc cần.
  Không có sự cố thì 6 giờ quét một lần. Khi có sự cố mở mà lần quét gần nhất đã cách hơn 15 phút thì quét lại một lần cho sự cố đó.
  Không thêm agent hay collector mới.
- **Best effort theo quyền:** tài khoản OpenStack có quyền admin thì dựng cả tầng hạ tầng (hypervisor, host của VM, backend của volume);
  không có quyền thì bỏ qua tầng đó, vẫn dựng các tầng còn lại. Màn Topology ghi rõ tầng nào bị bỏ qua và vì sao (thiếu quyền, chưa có nguồn).
- **Khai tay** cho tầng chưa có nguồn (host vật lý, quan hệ dịch vụ): form ở màn Topology và nhập CSV/YAML.
- Kết quả quét không đưa vào LLM; chỉ code đọc trường có cấu trúc (như `trusted_view`), nên chữ tự do trong tên hay mô tả không lái được gì.

## Cách dùng

1. **Công cụ cho agent `topology.neighbors(entity|alert, depth, at)`** (R0): trả chuỗi tầng dưới và các thực thể dùng chung tài nguyên, mỗi
   cạnh kèm mức tin tại `at` (mặc định lúc cảnh báo bắn); cạnh pod ↔ node và cạnh hết ngưỡng được đọc lại trực tiếp.
   Ví dụ cảnh báo etcd trên node X trả về: node X → VM (status, flavor) → volume (type, backend) → các VM khác cùng backend, kèm cảnh báo
   đang bắn trên từng thực thể. Thay 3–5 lần gọi API mỗi lượt điều tra; prompt điều tra gọi nó đầu tiên.
2. **Gom cảnh báo theo topology:** "cùng tổ tiên trong N bước" (cùng host vật lý, cùng backend lưu trữ) tại thời điểm hai cảnh báo bắn.
   Tự gộp chỉ khi mọi cạnh trên đường nối đều tin cậy tại thời điểm đó; có cạnh chỉ là gợi ý thì thành gợi ý gộp. Lý do gộp ghi rõ đường
   nối (`node a → host h ← node b`) và mức tin từng cạnh.
3. **Khoanh vùng ảnh hưởng (blast radius):** từ thực thể lỗi đi lên các tầng trên để liệt kê workload, dịch vụ, project bị ảnh hưởng;
   đưa vào thẻ Mức độ như gợi ý cho người đánh giá S1–S4, ghi rõ phần nào dựa trên cạnh chưa kiểm chứng.
4. **Màn Topology:** đồ thị theo tầng (dùng lại khung đồ thị kéo thả của tri thức L3), lọc theo cluster/project, tô màu thực thể đang có cảnh
   báo; trang sự cố có hình nhỏ quanh thực thể của sự cố. Nhấn để làm nổi đường nối, không lọc (như quy ước UI hiện có).
5. **Tri thức:** tên thực thể của KU nối được với thực thể thật (`entities.py` hiện chỉ chuẩn hoá tên), để KU về "backend lưu trữ X" hiện ra khi
   sự cố chạm vào VM dùng backend đó.

## Giai đoạn B (kế hoạch, chưa code)

Đã chốt 2026-11-24: (1) cảnh báo chỉ có nhãn pod thì quét thêm pod của các namespace trong cảnh báo khi có sự cố; (2) không đổi prompt điều
tra, phần nghi gây lỗi suy từ dữ liệu đã có; (3) gom theo tầng dưới chung tự động khi mọi cạnh trên đường nối đều tin cậy. Thứ tự tầng của
đồ thị viết thẳng trong code, không thêm bảng cấu hình; khi có data model chung thì sửa code.

**Đo trên bản sao DB (59 sự cố của tenant devtest, theo topology hiện tại):** 32 sự cố gắn được thực thể; 24 chỉ có nhãn pod (chủ yếu
namespace `cmp-document`), sẽ gắn được khi quét pod; 3 không gắn được. 1/59 sự cố có chuỗi 5 Whys. 24 sự cố có từ 2 thực thể trở lên dùng
chung tầng dưới, nhưng phần dùng chung hầu hết là backend lưu trữ của 268 hoặc 1478 thực thể hay hypervisor của 308 máy ảo: dùng chung
như vậy là bình thường, không phải dấu hiệu gây lỗi.

### B1. Đồ thị topology trên trang sự cố (xong, Đợt 48)

- **Phạm vi:** thực thể của cảnh báo (tại lúc cảnh báo bắt đầu) và chuỗi tầng dưới; phần còn lại gộp thành nhóm có đếm ("+18 máy ảo khác
  trên host"), trừ thực thể khác đang có cảnh báo thì vẽ riêng. Bố cục theo tầng từ trên xuống: cluster → node → máy ảo → hypervisor;
  nhánh lưu trữ PVC → PV → volume → backend. SVG tĩnh theo tầng, không dùng bố cục lực.
- **Làm nổi:** nghi gây lỗi (nổi nhất) > đang bị cảnh báo > đường nối, nhóm gộp. Cạnh tin cậy nét liền, gợi ý nét đứt kèm lý do. Bấm
  thực thể thì làm nổi đường nối, không lọc; hiện tên, loại, cảnh báo, lệnh đọc lại. Ghi rõ cảnh báo không gắn được và tầng bị bỏ qua.
- **Nghi gây lỗi, luôn ghi là "nghi" kèm lý do:**
  1. thực thể mà mắt xích 5 Whys có căn cứ đã đọc (`api_read /servers/<id>`, `/volumes/<id>`, `describe node X`...), ưu tiên mắt xích sâu nhất;
  2. không có thì tầng dưới của thực thể bị cảnh báo, gần nhất trước, **chỉ khi đặc hiệu**: chính nó đang có cảnh báo, hoặc trong các
     thực thể cùng loại với thực thể bị cảnh báo phụ thuộc vào nó (trực tiếp hay gián tiếp), từ 2 thực thể và từ 80% trở lên đang bị cảnh
     báo. Đếm cả cảnh báo ở sự cố khác đang mở, vì cảnh báo của cùng một lỗi có thể chưa được gộp. Đường nối có cạnh gợi ý thì vẫn nghi
     nhưng lý do ghi "có cạnh gợi ý, cần kiểm chứng". Không đặc hiệu thì vẽ là "tầng dưới chung, dùng bởi N" mà không làm nổi.
- **Pod:** khi sự cố mở, lần quét do sự cố (mục lịch quét) đọc thêm `get pods -n <ns> -o custom-columns=...` cho namespace của cảnh báo,
  lưu `k8s.pod` (cạnh pod → node, pod → PVC). Cạnh pod → node vẫn chỉ tin bản đọc trong chính lượt điều tra, như mục Nguyên tắc.
- **Backend:** `topology.incident_view(incident)` tính lúc đọc, trả kèm `GET /api/incidents/{id}`.

### B2. Khoanh vùng ảnh hưởng trên thẻ Mức độ (xong, Đợt 49)

Từ thực thể nghi gây lỗi đi ngược lên: số máy ảo, node, project bị ảnh hưởng, hiện như gợi ý cho người đánh giá S1–S4. Dùng chung phép
tính với B1; không có thực thể nghi gây lỗi thì không hiện.

### B3. Gom cảnh báo theo tầng dưới chung (xong, Đợt 50)

Luật "cùng host", "cùng backend" ở bước quan hệ, trước AI, với cùng điều kiện đặc hiệu như B1 (không thì mọi cảnh báo trên backend lớn đều
bị gộp). Tự gộp khi mọi cạnh trên đường nối tin cậy tại lúc hai cảnh báo bắn; có cạnh gợi ý thì chỉ thành gợi ý gộp. Lý do gộp ghi đường
nối (`node a → host h ← node b`).

### B4. Màn Topology riêng (để sau)

Tìm thực thể bất kỳ, xem đồ thị quanh nó tại một thời điểm; dùng lại cách vẽ của B1.

Thứ tự: B1 → B2 → B3, mỗi việc một commit. Đo lại sau B1: tỉ lệ sự cố gắn được thực thể (hiện 32/59).

## Các giai đoạn đề xuất

| Giai đoạn | Nội dung | Phụ thuộc |
| --- | --- | --- |
| A | Bảng `entity`, `entity_edge` (có khoảng hiệu lực), `alert_entity`; quét định kỳ node, PV, server, volume, và hypervisor, backend lưu trữ khi tài khoản OpenStack có quyền admin; cạnh node ↔ VM, PV ↔ volume; công cụ `topology.neighbors` đọc trực tiếp và mức tin theo cạnh; thay đổi cạnh vào `recent_changes` | chỉ kết nối đã có |
| B | Đồ thị trên trang sự cố, khoanh vùng ảnh hưởng, gom theo tầng dưới chung, màn Topology (chi tiết ở mục Giai đoạn B) | A |
| C | Host vật lý (DCIM hoặc khai tay), quan hệ dịch vụ | DCIM, data model chung |
| D | Khách hàng ↔ project, gợi ý S1–S4 theo thực thể | module CRM |

Đo trước và sau giai đoạn A: số lần gọi API mỗi lượt điều tra cho cảnh báo etcd/disk (hiện khoảng 3–5), thời gian tới nhận định đầu tiên
(trung vị 4,7 phút), tỉ lệ nhận định đúng (số chấm từ Đợt 43).

## Đã chốt (2026-11-24)

1. **Data model chung:** aiops sửa theo data model của nhóm Cloud Operations (tên kiểu thực thể, khoá, quan hệ), không giữ mô hình riêng.
   Khi data model chưa công bố, tên kiểu và khoá trong bản này là tạm, đổi theo data model chung khi có.
2. **Nguồn:** aiops tiếp tục tự quét. Đọc từ hệ thống topology/CMDB chung chưa làm khi hệ thống đó chưa có.
3. **Quyền OpenStack admin:** best effort. Có quyền thì dựng cả tầng hạ tầng, không có thì bỏ qua tầng đó, các tầng khác vẫn dựng.
4. **Chu kỳ quét:** 6 giờ một lần khi không có sự cố; có sự cố mà lần quét gần nhất cách hơn 15 phút thì quét lại một lần.
5. **Phạm vi:** tạm thời chỉ hạ tầng nội bộ (Private Cloud), chưa đụng tài nguyên của khách hàng trên Public Cloud.

## Còn mở

- Ngưỡng tin theo loại cạnh (mục Nguyên tắc) là đề xuất, cần người vận hành từng tầng xác nhận.
- Danh sách kiểu thực thể và khoá chờ data model chung.
