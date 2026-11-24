# Kế hoạch: làm gọn phần hội thoại và cách đưa tri thức vào context

## Tiến độ

| Giai đoạn | Trạng thái | Ghi chú |
|---|---|---|
| 0.1 Script đo `scripts/measure_context.py` | Xong | Mốc: 23 hội thoại nhiều lượt, 587.683 ký tự tri thức, 70% đã gửi ở lượt trước; hội thoại dài nhất 16 lượt lặp 82% |
| 0.2 Trường trace `knowledge_chars`, `skipped_chars`, `skipped_ids` | Xong | Hai trường `skipped_*` luôn là 0 cho tới giai đoạn 2 |
| 0.3 Golden task | Xong | 12 câu từ câu hỏi thật, ghi vào DB của tenant (không để trong repo). Điểm mốc 0,9: đủ từ khóa ở cả 12 câu, phần thiếu do router heuristic xếp sai domain ở 4 câu |
| 1 `aiops/conversation.py` | Xong | So từng byte trên bản sao dữ liệu thật: 266/266 kết quả `load_history` và `assemble_context` giống code cũ; 773 test xanh |
| 2 Bỏ phần đã gửi | Xong | Mô phỏng trên 23 hội thoại thật: tri thức gửi giảm 61% (hội thoại dài 197k → 40k ký tự), `ku_used` không đổi, golden giữ 0,9; 780 test xanh. Tắt được bằng `AIOPS_KNOWLEDGE_DEDUP=0` |
| 3 Bỏ trùng trong lượt | Xong | KU đã có trong core/playbook chiếm 25% ký tự units. Bản gửi đủ giảm 12% (1,45M → 1,28M ký tự); khi đã bỏ phần gửi ở lượt trước thì gần như không giảm thêm (562k), vì playbook vốn chỉ gửi một lần mỗi hội thoại. Golden giữ 0,9; 784 test xanh |
| 4 Playbook theo độ liên quan | Hoãn | Số đo giai đoạn 5 cho thấy tri thức chỉ chiếm khoảng 7% input agent, và đó là trước khi bỏ trùng; playbook giờ chỉ gửi một lần mỗi hội thoại. Phần còn tiết kiệm được nhỏ, trong khi rủi ro mất tri thức cần thiết thì có thật. Riêng thiếu sót "có metadata Skill nhưng không có tool đọc `SKILL.md`" đã làm riêng: công cụ `read_skill` |
| 5 Usage thật | Xong | Màn Đo lường có mục "Tri thức gửi kèm lượt" (ký tự mới, ký tự bỏ qua do trùng, tỷ lệ bỏ qua, tri thức trên input agent) và hai cột tương ứng ở bảng lượt tốn nhất. Token tri thức = ký tự/4 × số lần gọi agent của lượt, vì khối tri thức nằm trong prefix của mọi lần gọi. Số thật 30 lượt đầu: tri thức chiếm 7% input agent. Giữ `tokens_est` trong trace làm số ước lượng; số thật nằm ở `llm_call`. 785 test xanh |

Sau kế hoạch (19/11, theo ý tưởng "LLM wiki"): truy xuất mở rộng theo cạnh `related` mới (498 cạnh trên dữ liệu thật) và agent có `search_knowledge`/`read_knowledge_page` để tự tra giữa lượt. Khối `<context>` không đổi, không gửi danh sách trang vào context: tên trang đi trong kết quả `search_knowledge` và trang `index`. Golden giữ 0,9 cả sau khi nối cạnh lẫn với trang thực thể mới.

Ghi chú khi triển khai giai đoạn 2:

- Thẻ playbook đổi thành `<knowledge section="playbook" task_type="…" rev="…">`. Lịch sử cũ chưa có `rev`, nên ở lượt đầu tiên sau khi deploy, playbook được gửi lại một lần.
- KU được tính là đã gửi khi đúng dòng đó (id + nội dung) có trong khối `<context>` của lịch sử đang giữ lại. KU bị sửa thì dòng đổi, nên tự được gửi lại.
- Khối sự cố được bỏ khi lịch sử đã có đúng khối đó.

Ghi chú khi triển khai giai đoạn 3 (khác kế hoạch ở cả ba mục):

- KU đã có trong core/playbook không bị bỏ hẳn khỏi `units` mà giữ dòng ngắn `- [ku:id] (kind) tiêu đề: (nội dung ở playbook)`, có kèm điều kiện áp dụng nếu có. Lý do: playbook chỉ ghi nội dung; bỏ cả dòng thì mất tiêu đề và golden tụt còn 0,87, vì từ "storage" của câu etcd chỉ có trong tiêu đề KU. Id trong playbook có dạng `[ku:id ✓3 ✗0]`.
- Hiện không playbook nào vượt ngân sách (lớn nhất 5,5k trên 12k ký tự), nên việc cắt theo mục `##` (`_clip_sections`) chỉ là phòng hờ. Khi vượt, mục quá dài bị bỏ trọn và các mục sau vẫn được xét tiếp.
- Câu tìm KU vẫn ghép câu trước, vì bỏ hẳn câu trước thì làm hỏng câu trả lời cho câu hỏi của agent (ví dụ "Size PVC 2Gi, MR cho cả A+B+C+D" mất KU về Kafka). Thay đổi chỉ là:
  - câu trước giờ là câu gần nhất do người dùng viết (`last_user_task`), không phải lời hệ thống của lượt tiếp sau duyệt;
  - chính lượt tiếp sau duyệt thì tìm theo câu và entity của người dùng.
  
  Trên dữ liệu thật, cả 64 lượt có kết quả khác đều theo hướng bỏ KU nhiễu về "chờ duyệt" và lấy về KU đúng chủ đề. Golden chỉ có câu đơn lẻ nên không đo được mục này.

Khác với kế hoạch ở giai đoạn 1: không giữ `agent.assemble_context`/`agent.load_history` làm wrapper; `compiler.golden_score`, `incidents/summary.py` và test gọi thẳng `conversation.*`.

## Mục tiêu và ràng buộc

**Mục tiêu**

- Ở hội thoại dài, giảm 70–80% lượng tri thức phải gửi.
- Gom logic hội thoại về một chỗ.
- Model không thấy ít tri thức hơn hiện tại.

**Ràng buộc giữ nguyên**

- System prompt cố định theo tenant.
- Transcript chỉ nối thêm, không sửa lượt cũ.
- `budget_history` giữ 24k.
- Vẫn chạy được cả Anthropic và OpenAI-compatible.

**Số đo hiện tại** (hội thoại 16 lượt trên dữ liệu thật):

- Khối `<context>` khoảng 10k ký tự mỗi lượt, chiếm 30–60% phần transcript mới của lượt đó.
- 82% tri thức đã gửi ở lượt trước (98.731 / 120.423 ký tự). Nhiều lượt lặp 100%.
- Mỗi lượt có 4/8 KU của playbook (k8s_ops) trùng với KU vừa tìm được.
- Tri thức lặp lại ăn vào `budget_history`, nên lịch sử hội thoại thật bị cắt sớm hơn. Mỗi lượt cũng phải trả tiền ghi cache cho khoảng 2,5–3k token đã gửi rồi.

### Vì sao không chuyển sang Claude Agent SDK

- SDK chỉ chạy với Claude, không chạy trực tiếp với provider OpenAI-compatible.
- Duyệt lệnh qua `can_use_tool` chặn ngay lúc đang chạy, nên phải giữ tiến trình sống tới khi người duyệt bấm. aiops cần duyệt bất đồng bộ.
- Session lưu thành file JSONL trên đĩa. aiops cần lưu trong DB, tách theo tenant.
- SDK không tự bỏ phần KU trùng giữa các lượt, nên vấn đề này vẫn phải tự giải quyết.
- Phần "harness" SDK làm thay chỉ khoảng 500 dòng. Phần phức tạp là nghiệp vụ riêng của aiops.

Từ Claude Code chỉ học thiết kế: nạp chi tiết khi cần, không gửi lại thứ đã đưa vào phiên, index + tool đọc chi tiết.

---

## Giai đoạn 0: Đo trước khi sửa (nửa ngày)

1. **Script đo offline** `scripts/measure_context.py`. Script đọc episode của từng hội thoại và tính:
   - số ký tự tri thức mỗi lượt;
   - tỉ lệ dòng đã gửi trước đó;
   - tỉ lệ `<context>` so với transcript mới.

   Script đọc thẳng DB, không gọi LLM. Dùng nó làm mốc trước/sau cho từng giai đoạn.
2. **Thêm trường vào trace `context`** (`aiops/agent.py`, trong `_run_task`): `knowledge_chars`, `skipped_chars`, `skipped_ids`. Đây chỉ là JSON trong trace, không cần migration DB.
3. **Tạo golden task.** Hiện có 0 golden task, nên `golden_score` trả `None` và không chặn được gì. Cần khoảng 10–15 câu hỏi thật cho `k8s_ops`, `observability`, `general`, kèm `expect_terms`. Thiếu bước này thì giai đoạn 4 không có gì để đo.

---

## Giai đoạn 1: Gom phần hội thoại về một module, chưa đổi hành vi (1 ngày)

Tạo module mới `aiops/conversation.py` và chuyển vào đó:

| Hiện ở | Chuyển thành |
|---|---|
| `agent.load_history` | `conversation.load_history` |
| `agent.assemble_context` và `_personal`, `_tool_notes`, `_clip` | `select_knowledge()` (chọn gì) và `render_context()` (dựng text) |
| `llm._task_blocks` | `conversation.task_blocks` |
| Đoạn ghép query, context, history trong `_run_task` | `build_turn(conn, tenant, conversation_id, task, task_type, …) -> Turn` |

```python
@dataclass
class Turn:
    stable: str            # đi vào system, byte-stable theo tenant
    history: list[dict]    # transcript phát lại nguyên văn
    turn_context: str      # khối <context> của lượt này
    ku_used: list[str]     # mọi KU liên quan (để chấm helpful/harmful), kể cả KU không gửi lại
    stats: dict            # knowledge_chars, skipped_chars...
```

- Provider (`run_agent` của Anthropic và OpenAI) giữ nguyên chữ ký, chỉ dịch định dạng.
- Giữ `agent.assemble_context` làm wrapper mỏng để `compiler.golden_score` và các test cũ vẫn chạy.
- **Tiêu chí xong:** test snapshot cho thấy `stable`, `turn_context` và `history` giống từng byte trước và sau refactor, trên vài hội thoại mẫu. Các test `test_chat.py`, `test_approval_flow.py`, `test_learned_personal.py`, `test_session_state.py` phải xanh.

---

## Giai đoạn 2: Lượt sau chỉ gửi tri thức chưa gửi (1–1,5 ngày)

Đây là giai đoạn có tác động lớn nhất.

**Lấy danh sách "đã gửi" từ chính lịch sử được phát lại, không lưu bảng riêng.** Duyệt các khối `<context>` trong `history` mà `load_history` giữ lại, rồi thu thập:

- KU: marker `[ku:xxxxxxxx]`, kèm hash ngắn của nội dung.
- Playbook: đổi thẻ thành `<knowledge section="playbook" task_type="k8s_ops" rev="<hash 8 ký tự>">`.

Làm từ lịch sử nên kết quả tự đúng trong mọi trường hợp:

- Lượt cũ bị cắt vì vượt 24k: KU của nó không còn trong lịch sử, nên tự được gửi lại.
- Lượt phát lại dạng tóm lược (khác provider): không có `<context>`, nên không tính là đã gửi.
- Lượt bị hủy hoặc lỗi không lưu transcript: cũng không tính.
- Không cần migration, không có state lệch.

**Quy tắc cho từng loại**

| Loại | Gửi lại khi |
|---|---|
| Playbook | `task_type` đổi, `rev` đổi (có commit L2 mới), hoặc chưa có trong lịch sử giữ lại |
| KU (units) | chưa có id đó, hoặc nội dung đổi (KU bị sửa hay được consolidate) |
| Ghi chú công cụ | như KU |
| Khối sự cố (`incidents.context_block`) | hash nội dung đổi, vì trạng thái sự cố thay đổi |
| Core, preferences, skills | không đổi, đã nằm trong system |

**Nhắc lại bằng một dòng ngắn thay vì gửi lại nội dung.** KU vẫn liên quan tới câu hỏi mới nhưng đã gửi thì thêm một dòng:

```
<knowledge section="recall">Vẫn áp dụng (đã gửi ở trên): [ku:1a2b3c4d], [ku:5e6f7a8b]</knowledge>
```

Mỗi KU tốn khoảng 15 ký tự thay vì khoảng 300. Model vẫn biết KU nào đang liên quan, nên không mất tín hiệu.

**Ghi nhận KU đã dùng:** `ku_used` vẫn gồm cả KU bỏ qua vì trùng. Nếu không, Reflector sẽ hiểu sai là KU không được dùng và không chấm helpful/harmful.

**Lượt không còn tri thức mới:** bỏ hẳn khối `<context>`, chỉ gửi câu hỏi.

**Test mới** `tests/test_conversation_knowledge.py`:

- Lượt 2 cùng câu hỏi chỉ gửi phần recall.
- Đổi `task_type` thì playbook được gửi lại.
- Publish L2 mới (`rev` đổi) thì playbook được gửi lại.
- Lịch sử bị cắt thì KU được gửi lại.
- Đổi provider giữa hội thoại thì gửi đủ.
- `ku_used` vẫn đủ.
- Phần đầu (prefix) của lượt cũ không đổi byte nào.

---

## Giai đoạn 3: Bỏ trùng trong cùng một lượt (nửa ngày)

1. KU đã nằm trong playbook (`_ku_ids_in(playbook)`) thì không đưa vào `units` nữa. Ngân sách dư dùng cho KU xếp hạng tiếp theo.
2. Nếu tổng tri thức vẫn vượt ngân sách, cắt theo ranh giới mục `##` thay vì cắt ngang dòng ở `_clip`.
3. Bỏ cách ghép `query = last_task + task` cho bước tìm KU: lịch sử đã giữ ngữ cảnh rồi, ghép thêm câu trước kéo về KU của câu cũ. Thay bằng chỉ dùng `task`, cộng thêm entity của lượt trước. Cần so trên golden task trước khi chốt.

---

## Giai đoạn 4: Chọn mục playbook theo độ liên quan (thử nghiệm sau cờ, 1–2 ngày)

Chỉ làm sau khi giai đoạn 0 đã có golden task.

- **Phương án 4a (đề xuất):** playbook vốn được dựng từ KU, nên chỉ cần bỏ việc nạp nguyên file. Đưa KU của playbook `task_type` hiện tại vào bước tìm KU với hệ số ưu tiên, luôn giữ mục "Quy ước" và "Cạm bẫy". Không thêm tool, không thêm vòng gọi.
- **Phương án 4b:** chỉ gửi index (tiêu đề và `[ku:id]`), thêm tool chỉ đọc `knowledge.read(ids)` để model tự lấy chi tiết, giống MEMORY.md và Skill của Claude Code.
  - Cùng tool này giải quyết luôn một **thiếu sót hiện tại**: metadata Skill đã được đưa vào system nhưng không có tool nào nạp thân `SKILL.md`.
  - Lưu ý: `golden_score` hiện chấm bằng cách tìm từ khóa trong text context, nên 4b sẽ bị điểm thấp dù thực tế có thể vẫn tốt. Phải chấm thêm bằng kết quả episode (outcome, số lần gọi `knowledge.read`).
- Bật bằng cờ `settings.knowledge_mode = "full" | "ranked" | "index"`, mặc định giữ `full` cho tới khi số đo tốt hơn.

---

## Giai đoạn 5: Đo bằng `usage` thật (nửa ngày, làm song song)

- Thay `tokens_est = len // 4` bằng số liệu từ `llm.collect_usage`: `input`, `cache_read`, `cache_write` của từng lượt. Ghi tỉ lệ token tri thức trên input.
- Màn token metrics thêm cột "tri thức mới" và "bỏ qua do trùng".
- Provider không báo cache (`cache_known=False`) thì chỉ hiện số input, không hiện hit rate.

---

## Thứ tự và tiêu chí nghiệm thu

| # | Việc | Nghiệm thu |
|---|---|---|
| 0 | Script đo, trường trace, golden task | Có số mốc; `golden_score` khác `None` |
| 1 | `conversation.py` | Snapshot giống từng byte; test liên quan xanh |
| 2 | Bỏ phần đã gửi | Tri thức ở hội thoại 16 lượt giảm từ khoảng 120k xuống dưới 30k ký tự; cache hit Anthropic không giảm; golden không giảm |
| 3 | Bỏ trùng trong lượt | Không còn KU nằm ở cả playbook và units |
| 5 | Usage thật | Metrics hiển thị phần tri thức mới và phần bỏ qua |
| 4 | Ranked hoặc index (sau cờ) | Chỉ bật khi golden và tỉ lệ episode thành công không giảm |

**Mỗi giai đoạn:**

- Chỉ chạy test liên quan.
- Trước khi commit thì chạy toàn bộ suite.
- Build lại container và kiểm tra healthy.
- Commit lên main theo kiểu conventional, ví dụ `refactor(chat): gom lịch sử và context vào conversation.py`, `feat(knowledge): không gửi lại tri thức đã có trong hội thoại`.

**Rủi ro chính**

- Model "quên" một KU đã gửi từ lâu ở hội thoại rất dài: dòng recall nhắc lại id, và KU sẽ tự được gửi lại khi lượt chứa nó bị cắt khỏi lịch sử.
- Prefix bị lệch làm mất cache: test so byte ở giai đoạn 1 và 2 sẽ bắt được.

Ước lượng tổng là 4–6 ngày làm việc, chưa tính giai đoạn 4.
