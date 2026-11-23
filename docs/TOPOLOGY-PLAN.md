# Topology nhiều tầng (bản phác, chờ góp ý)

Bản nháp 2026-11-24, chưa làm code. Đã chốt: topology là dữ kiện có thời hạn, mức tin theo từng cạnh (mục Nguyên tắc). Mục 9 trong kế hoạch theo tài liệu Cloud Operations (`INCIDENTS-PLAN.md`). Cần khớp với data model
nhóm Cloud Operations đang làm trước khi chốt; chỗ nào còn phải quyết ghi ở mục **Câu hỏi mở**.

## Vì sao cần

- Biên bản MVP: "có đủ thông tin topology từ lớp vật lý, hypervisor, cloud control plane, K8s, PaaS, ứng dụng, cho phép vẽ topology".
- Gartner G00834503: Topology Mapping là nền cho tương quan và Causal AI; thiếu topology động thì không tách được nguyên nhân khỏi triệu chứng.
- Alibaba Cloud Monitor 2.0 coi topology thực thể là kho dữ liệu riêng (UModel), ngang hàng log, metric, trace, event.
- **Dữ liệu của chính kagent (tenant devtest, 47 lượt điều tra):** agent tự nối các tầng bằng tay ở mỗi lượt. 68 lần gọi
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
| `name`, `env` | tên hiển thị; môi trường của kagent |
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

Mỗi cạnh có `source`, `how`: `api` (đọc thẳng, ví dụ `spec.nodeName`), `match` (suy ra bằng khoá chung, ví dụ `providerID` của node
khớp UUID máy ảo), `manual`; và khoảng hiệu lực `valid_from`, `valid_to` (NULL = đang đúng), `last_seen`. Quét lại thấy cạnh đổi thì đóng
cạnh cũ (`valid_to`) và mở cạnh mới, không ghi đè, để trả lời được "lúc cảnh báo bắn, pod chạy trên node nào". Cạnh `match` hiện kèm cách
khớp để người kiểm được.

**Ánh xạ cảnh báo → thực thể** (`alert_entity`): từ nhãn `instance`, `node`, `pod`, `namespace`, `persistentvolumeclaim`, `device`
của cảnh báo. 104/133 cảnh báo có `instance`, 66 có `pod`, nên phần lớn cảnh báo gắn được ít nhất một thực thể.

## Tầng và nguồn

| Tầng | Kiểu thực thể | Nguồn có sẵn trong kagent | Khoảng trống |
| --- | --- | --- | --- |
| Vật lý | `dc.host`, `dc.rack` | chưa có | cần DCIM (biên bản nhắc) hoặc khai tay |
| Hypervisor | `os.hypervisor` | OpenStack `os-hypervisors`, trường host của server (cần quyền admin) | tài khoản kagent có thể không đủ quyền |
| Cloud control plane | `os.server`, `os.volume`, `os.network`, `os.project` | OpenStack `/servers/detail`, `/volumes/detail` | backend của volume (`os-vol-host-attr:host`) cần quyền admin |
| K8s | `k8s.cluster`, `k8s.node`, `k8s.workload`, `k8s.pod`, `k8s.pvc` | `kubectl get nodes,pods,pvc,pv -o json` | node ↔ VM nhờ `spec.providerID` (`openstack:///<uuid>`), cần kiểm trên cụm thật |
| PaaS, ứng dụng | `app.service` (CMP, AI Platform, DBaaS...) | nhãn `service` của cảnh báo, Service k8s | quan hệ giữa dịch vụ cần khai tay hoặc lấy từ trace |
| Khách hàng | `customer` | chưa có | mapping khách hàng ↔ project (module CRM trên CMP); khi có thì mức S1–S4 gợi ý được theo thực thể |

## Cách thu thập

- **Đọc trực tiếp lúc điều tra là mặc định:** `topology.neighbors` gọi API qua gateway với tài khoản bot (quyền, audit, giới hạn kết nối vẫn
  áp dụng), kết quả là bước `S<n>` bình thường nên trích làm căn cứ được, đồng thời cập nhật cạnh đã lưu.
- **Quét định kỳ chỉ phần ít đổi và rẻ** (node, VM, volume, PV; mặc định 15 phút) để có lịch sử cạnh và phát hiện thay đổi; pod và
  workload đọc lúc cần. Không thêm agent hay collector mới.
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

## Các giai đoạn đề xuất

| Giai đoạn | Nội dung | Phụ thuộc |
| --- | --- | --- |
| A | Bảng `entity`, `entity_edge` (có khoảng hiệu lực), `alert_entity`; quét định kỳ node, PV, server, volume; cạnh node ↔ VM, PV ↔ volume; công cụ `topology.neighbors` đọc trực tiếp và mức tin theo cạnh; thay đổi cạnh vào `recent_changes` | chỉ kết nối đã có |
| B | Gom cảnh báo theo tổ tiên chung; blast radius trên thẻ Mức độ; màn Topology | A |
| C | Hypervisor và backend lưu trữ (cần quyền admin OpenStack), host vật lý (DCIM hoặc khai tay), quan hệ dịch vụ | quyền, DCIM, data model chung |
| D | Khách hàng ↔ project, gợi ý S1–S4 theo thực thể | module CRM |

Đo trước và sau giai đoạn A: số lần gọi API mỗi lượt điều tra cho cảnh báo etcd/disk (hiện khoảng 3–5), thời gian tới nhận định đầu tiên
(trung vị 4,7 phút), tỉ lệ nhận định đúng (số chấm từ Đợt 43).

## Câu hỏi mở

1. **Khớp với data model chung:** dùng tên kiểu thực thể và khoá theo data model nhóm Cloud Operations, hay kagent giữ mô hình riêng và
   xuất/nhập qua một định dạng chung? Đề xuất: kagent dùng cùng tên kiểu và khoá, để sau này đọc topology từ hệ thống chung thay cho tự quét.
2. **Nguồn sự thật:** khi có hệ thống topology/CMDB chung, kagent chỉ đọc từ đó hay vẫn tự quét để bổ sung? Tự quét nhanh hơn nhưng có thể lệch.
3. **Quyền OpenStack admin** để thấy host của VM và backend của volume: cấp cho tài khoản kagent, hay để tầng này cho nguồn khác (DCIM)?
4. **Chu kỳ quét và ngưỡng tin theo loại cạnh:** 15 phút cho node, PV, server, volume có chấp nhận được không; ngưỡng cụ thể cho từng loại
   cạnh (đề xuất ở mục Nguyên tắc) cần người vận hành từng tầng xác nhận.
5. **Phạm vi:** chỉ hạ tầng nội bộ (Private Cloud) trước, hay cả tài nguyên của khách hàng trên Public Cloud? Tài nguyên khách hàng kéo theo
   yêu cầu về dữ liệu cá nhân và phân quyền theo tenant.
