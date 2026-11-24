# Refactor phần sự cố theo thiết kế giao diện mới

Lập 2026-11-14. Gom các việc 1–8 của [INCIDENTS-PLAN.md](INCIDENTS-PLAN.md) và mockup giao diện sự cố mới (dữ liệu thật của
`svc-document`) thành một lộ trình refactor. Mỗi giai đoạn deploy và commit riêng, giao diện cũ chạy được tới khi giai đoạn 3 xong.

## Vì sao phải refactor, không vá thêm

| Chỗ | Hiện tại | Hệ quả |
| --- | --- | --- |
| Hành động | JSON `incident.actions`, bị ghi đè tại chỗ ở `act`, `_sync_approvals`, `follow_up`, `merge`; `on_approval` tìm bằng `actions LIKE '%id%'` | Không truy vấn được "lệnh nào đang chờ", không lưu ai duyệt, lý do từ chối, lúc xác minh; lệnh người dùng tự chạy trong chat không có trong đó |
| Dòng thời gian | `timeline()` ghép 4 nguồn lúc đọc: `alert_event`, `incident_event`, `incident.actions`, quét `trace` của mọi episode | Mỗi lệnh chỉ còn "chạy X"; lệnh lỗi, hết hạn, bị từ chối không hiện; đọc trace mỗi lần mở trang |
| Nhận định | `title` bị thay bằng câu kết luận dài; `root_cause` chỉ là lượt đầu | Tiêu đề dài, nguyên nhân cũ (việc 5) |
| Trạng thái | `stage` một giá trị; hết quota thành `error` | Không biết "đang chờ gì", không tự chạy lại (việc 3) |
| Module | `incidents.py` 1134 dòng: lấy cảnh báo, gom, điều tra, hành động, tóm tắt, view | Các giai đoạn sau thêm khoảng 500 dòng nữa |

## Thiết kế giao diện mới

Mockup: `incident-ui.html` (artifact đã gửi). Mỗi phần của trang đều đọc dữ liệu từ một nguồn nhất định, ghi ở cột "Nguồn" của bảng dưới.

### Trang chi tiết

```
┌ Đầu trang ─────────────────────────────────────────────────────────────┐
│ Sự cố · dev · namespace svc-document                                   │
│ UI và Wiki.js của svc-document không chạy          [Mở hội thoại] [⋯] │
│ (Cần người xử lý) (Đang mở 2 ngày) (7/12 đang bắn) (Lặp lại 13 lần)    │
├ Đang chờ (2) ──────────────────────────────┬ Mốc thời gian ───────────┤
│ ✗ Tạo Service … hết hạn 17:18 13/11        │ Bắt đầu 15:00 12/11       │
│   vì auto mode chặn …  [Nhờ agent đề xuất] │ aiops nhận 20:15 (5h15)  │
│ ○ Đẩy lại image lên registry (ngoài cluster)│ Phục hồi: chưa            │
│   [Đánh dấu đã xong]                        ├ Cảnh báo (theo tên) ─────┤
├ Tóm tắt ─────────── cập nhật 15:15 [Tóm tắt ngay] │ TargetDown  2 đang bắn │
│ 2–4 câu cho lãnh đạo                        │ … [Xem nhãn, tách]        │
├ Dòng sự kiện [Tất cả|Hành động|Điều tra|Cảnh báo] ├ Nguyên nhân ─────────┤
│ Ngày 13/11                                  │ lượt 3 · 16:48 · 0.60     │
│ 00:40 aiops đề xuất  rollout undo → cần duyệt │ • thiếu Service …      │
│ 00:52 admin duyệt     rollout undo → lỗi: …  │ • registry thiếu blob … │
│ 16:48 aiops · lần 3  14 lệnh đọc → 2 nguyên nhân ├ Sự cố liên quan ──────┤
│ 17:18 hết hạn         tạo Service → chưa chạy│ 13 sự cố từ 12/11 …      │
└─────────────────────────────────────────────┴──────────────────────────┘
```

| Phần | Nội dung | Nguồn | Giai đoạn |
| --- | --- | --- | --- |
| Tiêu đề | Ngắn, theo triệu chứng (≤ 60 ký tự), không phải câu kết luận | `incident.title` do báo cáo điều tra viết riêng trường `symptom`; kết luận sang `cause` | 2 |
| Chip trạng thái | Cần người xử lý / Đang điều tra / Chờ duyệt / Chờ quota / Đang xác minh / Đã hết; đã mở bao lâu; số cảnh báo đang bắn; lặp lại N lần | tính lúc đọc từ `incident_action`, stage, `alert_event`, sự kiện `reopened`/`merged` | 2, 4 |
| Menu ⋯ | Điều tra lại, Gộp vào…, Đóng kèm ghi chú | API sẵn có | 2 |
| Đang chờ | Lệnh chờ duyệt (Duyệt/Từ chối), lệnh hết hạn hoặc bị từ chối chưa có lệnh thay (Nhờ agent đề xuất lại), lệnh lỗi, việc cần người ngoài cluster (Đánh dấu đã xong), chờ quota, điều tra lỗi | `incident_action` + `incident_task` + stage | 2, 5 |
| Tóm tắt | Như hiện tại (`summaryCard`) | `incident.summary` | có |
| Dòng sự kiện | Nhật ký xử lý: giờ, ai (cảnh báo / aiops · điều tra lần N / tên người / hết hạn), `hành động → kết quả`; nhóm theo ngày; lọc theo loại | `incident_event` + bắt đầu/hết của `alert_event` | 1, 2 |
| Mốc thời gian | Cảnh báo đầu tiên bắt đầu, aiops nhận, thời gian phát hiện, phục hồi, đã kéo dài | `min(alert_event.starts_at)`, `created_at`, `resolved_at` | 2 |
| Cảnh báo | Gom theo `alertname`: số đang bắn/đã hết; nhóm gom theo nhãn nào; bảng đầy đủ (nhãn, tách) trong `<details>` | `alert_event` | 2 |
| Nguyên nhân | Nhận định của lượt điều tra mới nhất kèm số lượt, giờ, độ tin; bằng chứng và số lệnh đọc trong `<details>` | `incident.cause*` cập nhật mỗi lượt (việc 5) | 2 |
| Sự cố liên quan | Sự cố đã hết hoặc đã đóng cùng nhóm trong 30 ngày; sự cố đang mở nên gộp | `_rule_match` mở rộng | 4 |

Bỏ khỏi trang: thẻ "Hành động" riêng (đã nằm trong Đang chờ và Dòng sự kiện), bảng cảnh báo 12 dòng ở thân trang. Theo quy tắc cũ
dòng sự kiện vẫn không hiện mốc nội bộ (lúc gộp, lúc bắt đầu điều tra). "Ai duyệt" là thông tin của lệnh, nằm trên dòng của lệnh, không phải dòng riêng.

### Danh sách

- Bộ lọc: **Cần người xử lý** (mặc định), Đang mở, Đã hết, Đã đóng, Tất cả.
- Mỗi dòng: tiêu đề triệu chứng, một câu (câu đầu của tóm tắt, chưa có thì nguyên nhân), chip trạng thái, "lặp lại N lần", "mở 2 ngày",
  "7/12 đang bắn".
- Gợi ý gộp ở đầu danh sách cho các sự cố đang mở khớp nhau mà tự gộp bỏ qua (khớp theo AI, hoặc đang có việc dở); nút "Gộp vào sự cố gốc".

## Mô hình dữ liệu mới

### Bảng `incident_action` (thay `incident.actions`)

```sql
CREATE TABLE incident_action (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL,
  episode_id TEXT, approval_id TEXT, origin TEXT NOT NULL,      -- investigation | chat
  requested_by TEXT NOT NULL,                                   -- aiops-alerts hoặc user chat
  tool TEXT NOT NULL, args TEXT, why TEXT, risk_tier TEXT, reasons TEXT NOT NULL DEFAULT '[]', dry_run TEXT,
  status TEXT NOT NULL,          -- pending | ok | error | denied | expired | blocked | invalid
  auto INTEGER NOT NULL DEFAULT 0, proposed_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT, deny_reason TEXT, ran_at TEXT,
  result TEXT, result_line TEXT, verified INTEGER, verified_at TEXT, superseded_by TEXT
);
CREATE INDEX ix_incident_action ON incident_action(incident_id, proposed_at);
CREATE INDEX ix_incident_action_approval ON incident_action(approval_id);
```

- `origin='chat'`: gateway ghi một dòng khi lệnh ghi chạy (hoặc thành yêu cầu duyệt) trong hội thoại có `conversation.incident_id`.
  Phần quét trace trong `timeline()` không cần nữa.
- `decided_by`, `decided_at`, `deny_reason` chép từ `approval_request` khi đồng bộ; `on_approval` tìm theo `approval_id` có index.
- `result_line`: một dòng tóm tắt kết quả, lấy từ lỗi hoặc dòng đầu stdout, không gọi model.
- `superseded_by`: lệnh đề xuất lại thay cho lệnh hết hạn hoặc bị từ chối, để lệnh cũ rời khỏi "Đang chờ".
- Migration: chép từng phần tử JSON sang bảng. Bản này giữ cột `actions` (không ghi thêm), xoá ở bản sau.

### Sự kiện `incident_event` thành nhật ký

Thêm cột `actor` (`aiops`, id người dùng, `alert`, `system`) và `action_id`. Ghi sự kiện ngay lúc việc xảy ra:

| kind | Khi | Dòng hiện |
| --- | --- | --- |
| `investigated` | lượt điều tra xong | `aiops · điều tra lần N` · `M lệnh đọc → kết luận (độ tin)` |
| `proposed` | `act` hoặc chat đưa lệnh qua gateway | không hiện: dòng sự kiện chỉ có lệnh đã thực sự chạy; sự kiện vẫn ghi làm mốc cho bản tóm tắt |
| `ran`, `failed` | lệnh chạy xong | `<người> duyệt` hoặc `tự động` · `lệnh → result_line` |
| `denied`, `expired`, `skipped` | quyết định, hết hạn, hoặc hội thoại có tin nhắn mới trước khi duyệt | không hiện (như `proposed`) |
| `verified` | xác minh | `cảnh báo đã hết` / `còn N cảnh báo` |
| `reopened`, `resolved`, `closed` | như hiện tại | |
| `no_action`, `quota`, `error`, `stopped` | trạng thái chờ người | không hiện; mục "Đang chờ" đọc chúng |

Bắt đầu và hết của cảnh báo vẫn lấy từ `alert_event` lúc đọc: giờ `starts_at` do nguồn cảnh báo cung cấp, có thể trước lúc aiops lấy về.

### Cột mới của `incident`

| Cột | Dùng cho |
| --- | --- |
| `cause`, `cause_at`, `cause_episode`, `investigations` (số lượt) | Nguyên nhân theo lượt mới nhất (việc 5); `root_cause` cũ chép sang |
| `title` giữ nghĩa triệu chứng; báo cáo điều tra thêm trường `symptom` | Tiêu đề ngắn |
| stage mới `waiting_quota`, không hẹn giờ: người bấm Tiếp tục khi có quota hoặc đã đổi provider | Việc 3 |
| `repeats` (đếm khi mở lại hoặc gộp) | "Lặp lại N lần", điều kiện postmortem |

### Bảng `incident_task` (việc cần người)

`id, incident_id, text, source (summary|user), created_at, done_by, done_at`. Bản tóm tắt trả JSON `{summary, human_tasks[]}`,
aiops thêm task mới vào bảng và giữ task người đã đánh dấu xong.

## Tách module

`aiops/incidents.py` thành package, `__init__.py` re-export đúng các tên `api.py`, `scheduler.py` và test đang dùng
(`settings`, `poll`, `get`, `merge`, `on_approval`, `tick`, …):

| File | Nội dung |
| --- | --- |
| `settings.py` | `DEFAULTS`, `check_settings`, `settings`, `save_settings`, `bot_user` |
| `ingest.py` | `fetch`, `poll`, `source_status` |
| `grouping.py` | `match`, `assign`, `_rule_match`, `_attach`, `merge`, `auto_merge`, `split`, `related` |
| `investigate.py` | `start_investigation`, `investigate`, quota, `_recover_orphans` |
| `actions.py` | `act`, `record_chat_action`, `sync_approvals`, `follow_up`, `verify` |
| `summary.py` | `last_activity`, `due_summaries`, `summarize`, tasks |
| `views.py` | `list_incidents`, `get`, `timeline`, `waiting`, `milestones`, `alert_groups`, `actions_by_episode`, `describe` |
| `__init__.py` | re-export, `tick` |

Rủi ro: monkeypatch phải trỏ vào module con. Hiện chỉ có một chỗ (`tests/test_incidents.py:424`); kiểm tra lại khi tách.

## Lộ trình

| # | Giai đoạn | Việc trong plan | Thay đổi | Kiểm tra |
| --- | --- | --- | --- | --- |
| 0 | Tách module | — | Chỉ dời code, không đổi hành vi | toàn bộ test (`-n auto`) |
| 1 | Hành động và nhật ký | 1 | `incident_action` + migration; sự kiện có `actor`/`action_id`; gateway ghi lệnh trong chat sự cố; `timeline()` chỉ đọc sự kiện; `on_approval` theo index | test migration trên bản chép `aiops.db`; dòng sự kiện của `b70c928e` có rollout undo lỗi, tạo Service hết hạn, ai duyệt |
| 2 | API view và trang chi tiết mới | 5 | `get()` trả `header`, `waiting`, `events`, `milestones`, `alert_groups`, `cause`; báo cáo điều tra có `symptom`; nguyên nhân cập nhật mỗi lượt; viết lại `incidentDetail` 2 cột, menu ⋯, lọc sự kiện | Chrome: `b70c928e` đối chiếu mockup; màn hẹp thành 1 cột |
| 3 | Quota và danh sách | 3 | stage `waiting_quota` khi tenant hết quota hoặc provider trả 429, dừng tới khi người bấm Tiếp tục; danh sách mới, lọc "Cần người xử lý" | test với clock; Chrome danh sách |
| 4 | Lặp lại và liên quan | 2 | `repeats`; `related()` 30 ngày; gợi ý gộp cho sự cố tự gộp bỏ qua | test; Chrome |
| 5 | Việc cần người và ngữ cảnh agent | 4 | Tóm tắt trả `human_tasks`; `incident_task` + "Đánh dấu đã xong"; tóm tắt mới nhất và việc đang chờ vào `<context>` của lượt chat sự cố (không đổi system prompt) | test `assemble_context`; Chrome |
| 6 | Cần quyết định trước | 6, 7, 8 | S1–S4 và postmortem; duyệt bản tóm tắt; kênh báo thẻ chờ duyệt | — |

Giai đoạn 1 là nền cho mọi phần còn lại. Có thể làm giai đoạn 2 trước bằng cách đọc JSON cũ, nhưng khi đó "Đang chờ" thiếu lệnh trong
chat và thiếu người duyệt, rồi phải viết lại. Giai đoạn 0 có thể gộp vào giai đoạn 1 nếu muốn ít commit hơn.

## Tiến độ

| # | Commit | Khác so với kế hoạch |
| --- | --- | --- |
| 0 | `2a6d690` | Thêm `common.py`; module cài đặt tên `config.py` vì `settings` trùng tên hàm `incidents.settings` |
| 1 | `b3a05e0` | Lệnh tự chạy chỉ ghi một sự kiện `ran`; chat chỉ ghi lệnh ghi đã chạy, lỗi hoặc chờ duyệt |
| 2 | `b73d801` | Dữ liệu dòng sự kiện vẫn ở khóa `timeline` (có `who`, `cat`); "ai làm" tính lúc đọc, câu sự kiện chỉ còn "lệnh → kết quả"; thêm "Nhờ agent đề xuất lại" (gửi lại đúng lệnh, luôn tạo thẻ duyệt) và "Bỏ qua"; tiêu đề dài kiểu kết luận được đặt lại theo cảnh báo gốc |
| 3 | `fb8bfc4`, `d9f858b` | Không tự điều tra lại lúc 0h: dừng ở Chờ quota (tenant hết quota hoặc provider trả 429) tới khi người bấm Tiếp tục |
| 4 | `d4d9139` | "Lặp lại" đếm lần mở lại và lần nhận sự cố khác gộp vào; sự cố liên quan cùng tiêu đề gom một dòng kèm số lần; gợi ý gộp chỉ theo luật nhãn/quan hệ k8s (không gọi AI), bỏ cặp người đã tách |
| 5 | (commit này) | Người thêm việc tay ở menu "Gộp, đóng, thêm việc…"; việc tóm tắt nêu mà lần sau không còn nêu thì bỏ, việc người thêm hay đã xong thì giữ; khối `<incident>` vào ngữ cảnh mọi lượt của hội thoại sự cố, kể cả lượt điều tra |
| — | `6d77dd8`, `470043e` | Ngoài kế hoạch: Cài đặt → Quota, org admin đặt quota token và lệnh ghi cho từng tenant, xem mức dùng hôm nay và 7 ngày |

## Câu hỏi cho giai đoạn 6

- S1–S4: dev/staging mặc định S4; prod hỏi người bằng `ask_user` (khách hàng có bị ảnh hưởng, phạm vi rộng hay hẹp, có mất dữ liệu),
  rồi áp cây quyết định trong README `ops/incidents`.
- Ai được duyệt bản tóm tắt: người có quyền duyệt lệnh R2+ ở môi trường đó, hay mọi thành viên tenant.
- Kênh báo khi có thẻ chờ duyệt: thông báo trình duyệt (không cần hạ tầng), email hay chat nội bộ. Thời hạn duyệt riêng cho lệnh của sự cố
  (hiện 30 phút).
