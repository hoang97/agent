# kagent

## Chạy test

Chỉ chạy các file test liên quan đến phần vừa sửa. Cả bộ (~780 test) chạy một lần trước khi commit, và chỉ khi sửa code dùng chung: schema `db.py`, `CallContext`, chữ ký `gateway.invoke`/`evaluate`, `conversation.py`.

Test chạy trong container `kagent:local`, repo mount vào `/src`. Phải có `-e HOME=/tmp` vì user trong image không ghi được `/app/.local`; thiếu thì báo `No module named pytest`.

```sh
docker run --rm -v "$PWD":/src -w /src -e HOME=/tmp kagent:local sh -c \
  'pip install --user -q -r requirements-dev.txt && python -m pytest -q tests/test_llm.py'
# cả bộ: python -m pytest -n auto -q
```

Thu hẹp thêm bằng `-k` hoặc `--lf`.

| Sửa ở | Chạy |
|---|---|
| `connectors/*`, `templates/*` | `test_connectors.py` + file riêng của connector |
| `connectors/k*.py` (kubectl, kubeconfig) | `test_kubectl.py test_kubeconfig.py` |
| `connectors/helm*.py` | `test_helm.py` |
| `connectors/openstack.py` | `test_openstack.py` |
| `connectors/gitlab*.py` | `test_gitlab.py` |
| `connectors/prometheus.py`, `alertmanager.py` | `test_monitoring.py` |
| `connectors/data.py`, `datafilter.py`, `results.py` | `test_data_tools.py` |
| `undo.py`, `connectors/*_undo.py` | `test_undo.py` |
| `gateway.py`, `automode.py` | `test_gateway.py test_approval_flow.py test_judge.py` |
| `agent.py` | `test_chat.py test_multi_tools.py test_stop.py test_conversation.py` |
| `conversation.py` | `test_conversation.py test_chat.py test_approval_flow.py` |
| `llm.py` | `test_llm.py test_llm_openai.py test_llm_key.py test_llm_models.py test_token_metrics.py` |
| `knowledge.py`, `retrieval.py`, `learning.py` | `test_knowledge.py test_knowledge_graph.py test_ku_consolidate.py test_sources.py` |
| `lint.py` | `test_lint.py` |
| `dedup.py` | `test_dedup.py test_ku_consolidate.py` |
| `sources.py` | `test_sources.py test_lint.py` |
| `learned.py` | `test_learned_personal.py` |
| `compiler.py` | `test_compiler.py test_conversation.py` |
| `entities.py` | `test_entities.py` |
| `api.py` | `test_api.py test_session_state.py` |
| `incidents/*` | `test_incidents.py test_grouping.py` |
| `proxy.py`, `net.py` | `test_proxy.py` |
| chỉ `static/*.js`, `*.css` | không cần pytest: `node --check`, rồi xem trên trình duyệt |

Sửa phần tri thức gửi kèm lượt (KU, playbook, tìm KU, cạnh `ku_edge`, công cụ tra tri thức) thì chạy thêm `compiler.golden_score` trên DB của tenant: điểm mốc 0,9, không được giảm (xem `docs/CONTEXT-PLAN.md`).

Chạy golden hay thử migration trên bản sao, không đụng DB đang chạy:

```sh
docker exec agent-kagent-1 python -c "import sqlite3; s=sqlite3.connect('/data/kagent.db'); d=sqlite3.connect('/tmp/k.db'); s.backup(d)"
mkdir -p "$DRY" && docker cp agent-kagent-1:/tmp/k.db "$DRY/kagent.db" && docker cp agent-kagent-1:/data/knowledge "$DRY/knowledge" && chmod -R 777 "$DRY"
docker run --rm -v "$PWD":/src -v "$DRY":/dry -w /src -e HOME=/tmp -e PYTHONPATH=/src kagent:local sh -c \
  'git config --global --add safe.directory "*"; python script.py'   # script gọi settings.configure(data_dir=Path("/dry")) trước khi import phần còn lại
```

- Dùng `backup()`, không `cp` file: DB chạy WAL nên file chính có thể thiếu dữ liệu.
- Phải có `chmod`, vì user trong image không ghi được thư mục mount.
- Phải có `safe.directory`, vì repo tri thức của tenant thuộc user khác.
- Cần gọi LLM thật thì thêm `--env-file .env --network host`. Lệnh này tốn tiền.

Thêm connector mới thì tạo fake trong `tests/fakes.py`, kèm `tests/test_<name>.py`, rồi thêm một dòng vào bảng trên.
