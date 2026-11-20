"""Schema SQLite cho L0, L1, L3, multi-tenant, gateway. Tên bảng theo tài liệu kiến trúc."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS org (id TEXT PRIMARY KEY, name TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tenant (
  id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES org(id), name TEXT NOT NULL UNIQUE,
  isolation TEXT NOT NULL DEFAULT 'pooled',
  policy TEXT NOT NULL DEFAULT '{}',
  auto_promote INTEGER NOT NULL DEFAULT 1,
  change_freeze INTEGER NOT NULL DEFAULT 0,
  token_quota_daily INTEGER NOT NULL DEFAULT 2000000,
  write_quota_daily INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS app_user (
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
  is_org_admin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS membership (
  tenant_id TEXT NOT NULL REFERENCES tenant(id), user_id TEXT NOT NULL REFERENCES app_user(id),
  role TEXT NOT NULL, PRIMARY KEY (tenant_id, user_id, role)
);

CREATE TABLE IF NOT EXISTS session (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES app_user(id), expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episode (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenant(id), user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL, scope TEXT NOT NULL, task_type TEXT, env TEXT NOT NULL,
  started_at TEXT NOT NULL, input TEXT NOT NULL, trace TEXT NOT NULL, output TEXT,
  ku_used TEXT NOT NULL DEFAULT '[]', tokens INTEGER NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL DEFAULT 'unknown', outcome_src TEXT, human_edit TEXT, processed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_episode_tenant ON episode(tenant_id, started_at);
CREATE INDEX IF NOT EXISTS ix_episode_unprocessed ON episode(tenant_id, processed_at);

CREATE TRIGGER IF NOT EXISTS episode_append_only BEFORE UPDATE ON episode
WHEN NEW.input IS NOT OLD.input OR NEW.trace IS NOT OLD.trace OR NEW.output IS NOT OLD.output
  OR NEW.ku_used IS NOT OLD.ku_used OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT, 'episode is append-only'); END;

CREATE TRIGGER IF NOT EXISTS episode_no_delete BEFORE DELETE ON episode
BEGIN SELECT RAISE(ABORT, 'episode is append-only'); END;

CREATE TABLE IF NOT EXISTS knowledge_unit (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenant(id),
  kind TEXT NOT NULL CHECK (kind IN ('rule','fact','procedure','lesson','insight')),
  scope TEXT NOT NULL, domain TEXT NOT NULL DEFAULT 'general',
  title TEXT NOT NULL, content TEXT NOT NULL, applies_when TEXT, entities TEXT NOT NULL DEFAULT '[]',
  embedding TEXT, confidence REAL NOT NULL DEFAULT 0.5,
  helpful_cnt INTEGER NOT NULL DEFAULT 0, harmful_cnt INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'candidate'
    CHECK (status IN ('candidate','active','deprecated','rejected')),
  valid_from TEXT, valid_to TEXT, recorded_at TEXT NOT NULL,
  superseded_by TEXT REFERENCES knowledge_unit(id),
  source_eps TEXT NOT NULL, source_trust TEXT NOT NULL
    CHECK (source_trust IN ('user','verified_tool','internal_doc','external_untrusted')),
  version INTEGER NOT NULL DEFAULT 1, last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ku_tenant ON knowledge_unit(tenant_id, status);

CREATE VIRTUAL TABLE IF NOT EXISTS ku_fts USING fts5(
  ku_id UNINDEXED, tenant_id UNINDEXED, title, content, applies_when, entities,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS ku_edge (
  tenant_id TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL,
  PRIMARY KEY (src, dst, rel)
);

CREATE TABLE IF NOT EXISTS ku_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, ku_id TEXT NOT NULL, op TEXT NOT NULL,
  before TEXT, after TEXT, actor TEXT NOT NULL, reason TEXT, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ku_event ON ku_event(tenant_id, ku_id);

CREATE TABLE IF NOT EXISTS tool_registry (
  tenant_id TEXT NOT NULL REFERENCES tenant(id), tool TEXT NOT NULL, server TEXT NOT NULL,
  action_class TEXT NOT NULL, risk_tier TEXT NOT NULL, reversible TEXT NOT NULL,
  blast_radius TEXT NOT NULL, data_sensitivity TEXT NOT NULL, dry_run TEXT NOT NULL,
  untrusted_output INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL, input_schema TEXT NOT NULL,
  envs TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, approved_by TEXT,
  PRIMARY KEY (tenant_id, tool)
);

CREATE TABLE IF NOT EXISTS approval_request (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenant(id), episode_id TEXT,
  requester_id TEXT NOT NULL, original_request TEXT NOT NULL,
  tool TEXT NOT NULL, env TEXT NOT NULL, args TEXT NOT NULL, dry_run TEXT,
  risk_tier TEXT NOT NULL, confidence REAL NOT NULL, reasons TEXT NOT NULL, rollback_hint TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','expired','skipped')),
  required_approvals INTEGER NOT NULL DEFAULT 1, decided_by TEXT NOT NULL DEFAULT '[]',
  deny_reason TEXT, result TEXT, learned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_approval ON approval_request(tenant_id, status);

CREATE TABLE IF NOT EXISTS approval_grant (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT NOT NULL, tool TEXT NOT NULL,
  env TEXT NOT NULL, namespace TEXT, expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_call_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, episode_id TEXT, user_id TEXT NOT NULL,
  tool TEXT NOT NULL, env TEXT NOT NULL, args_hash TEXT NOT NULL, args TEXT NOT NULL,
  risk_tier TEXT, confidence REAL, decision TEXT NOT NULL, reasons TEXT, result_status TEXT, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit ON tool_call_audit(tenant_id, at);

CREATE TABLE IF NOT EXISTS sim_state (tenant_id TEXT PRIMARY KEY, state TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS skill (
  tenant_id TEXT NOT NULL, name TEXT NOT NULL, domain TEXT NOT NULL, tools TEXT NOT NULL,
  success_rate_30d REAL NOT NULL DEFAULT 0, PRIMARY KEY (tenant_id, name)
);

CREATE TABLE IF NOT EXISTS llm_profile (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, base_url TEXT, model_strong TEXT, model_fast TEXT,
  ciphertext TEXT NOT NULL, last4 TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT
);

CREATE TABLE IF NOT EXISTS app_setting (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS entity_alias (
  tenant_id TEXT NOT NULL, alias TEXT NOT NULL, canonical TEXT NOT NULL, created_by TEXT, created_at TEXT,
  PRIMARY KEY (tenant_id, alias)
);

CREATE TABLE IF NOT EXISTS ku_consolidation (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status TEXT NOT NULL, considered INTEGER NOT NULL,
  items TEXT NOT NULL, batches TEXT NOT NULL, done_batches TEXT NOT NULL DEFAULT '[]',
  groups TEXT NOT NULL DEFAULT '[]', drops TEXT NOT NULL DEFAULT '[]', errors TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- Nguồn tri thức ngoài episode (sources.py): tài liệu tải lên, bản ghi sự cố. ref của sự cố = <incident_id>@<cause_at>.
CREATE TABLE IF NOT EXISTS source_doc (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
  trust TEXT NOT NULL, ref TEXT, created_by TEXT, user_id TEXT, activate INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', result TEXT, created_at TEXT NOT NULL, done_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_doc ON source_doc(tenant_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_source_doc_ref ON source_doc(tenant_id, kind, ref) WHERE ref IS NOT NULL;

-- Đề xuất gộp KU đang dùng (dedup.py), chờ admin duyệt. ids là JSON đã sắp xếp, để nhận ra nhóm admin đã bỏ qua.
CREATE TABLE IF NOT EXISTS ku_merge_proposal (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, ids TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
  applies_when TEXT, reason TEXT, entity TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT, merged_into TEXT
);
CREATE INDEX IF NOT EXISTS ix_ku_merge_proposal ON ku_merge_proposal(tenant_id, status);
CREATE TABLE IF NOT EXISTS ku_dedup_seen (tenant_id TEXT NOT NULL, fingerprint TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (tenant_id, fingerprint));

CREATE TABLE IF NOT EXISTS ku_lint (tenant_id TEXT PRIMARY KEY, report TEXT NOT NULL, at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS tool_result (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT, episode_id TEXT, tool TEXT NOT NULL,
  untrusted INTEGER NOT NULL DEFAULT 0, chars INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tool_result_created ON tool_result(created_at);

CREATE TABLE IF NOT EXISTS llm_call (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, episode_id TEXT, purpose TEXT NOT NULL, model TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_call_tenant ON llm_call(tenant_id, at);
CREATE INDEX IF NOT EXISTS ix_llm_call_episode ON llm_call(episode_id);

CREATE TABLE IF NOT EXISTS golden_task (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, query TEXT NOT NULL, expect_domain TEXT NOT NULL,
  expect_terms TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenant(id), user_id TEXT NOT NULL,
  title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_conversation ON conversation(tenant_id, user_id, updated_at);

CREATE TABLE IF NOT EXISTS learned_tool (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenant(id), name TEXT NOT NULL, scope TEXT NOT NULL,
  description TEXT NOT NULL, params TEXT NOT NULL, steps TEXT NOT NULL, signature TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','active','rejected','disabled')), origin TEXT NOT NULL,
  source_eps TEXT NOT NULL, created_by TEXT NOT NULL, approved_by TEXT, note TEXT,
  uses INTEGER NOT NULL DEFAULT 0, successes INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, scope, name)
);
CREATE INDEX IF NOT EXISTS ix_learned_signature ON learned_tool(tenant_id, signature);

CREATE TABLE IF NOT EXISTS user_profile (
  user_id TEXT PRIMARY KEY REFERENCES app_user(id), instructions TEXT NOT NULL DEFAULT '',
  style TEXT NOT NULL DEFAULT 'default', default_env TEXT NOT NULL DEFAULT 'dev', updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connection (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenant(id), name TEXT NOT NULL, template TEXT NOT NULL, title TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('tenant','user')), owner_user TEXT, envs TEXT NOT NULL, roles TEXT NOT NULL,
  params TEXT NOT NULL, secrets TEXT NOT NULL, allow_private INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','removed')),
  created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_connection_tenant ON connection(tenant_id, status);

CREATE TABLE IF NOT EXISTS app_secret (
  name TEXT PRIMARY KEY, ciphertext TEXT NOT NULL, last4 TEXT NOT NULL,
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compile_run (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status TEXT NOT NULL, branch TEXT, diff TEXT,
  changed TEXT NOT NULL DEFAULT '[]', needs_review INTEGER NOT NULL DEFAULT 0,
  score_before REAL, score_after REAL, commit_sha TEXT, actor TEXT NOT NULL, reason TEXT,
  created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT
);

-- Cảnh báo lấy về từ Alertmanager/Prometheus (incidents.py). Một lần bắn = một dòng; bắn lại sau khi hết là dòng mới (starts_at khác).
CREATE TABLE IF NOT EXISTS alert_event (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT NOT NULL, starts_at TEXT NOT NULL,
  alertname TEXT, severity TEXT, labels TEXT NOT NULL, summary TEXT, env TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'firing', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, resolved_at TEXT,
  incident_id TEXT, join_method TEXT, join_score REAL, join_reason TEXT,
  UNIQUE(tenant_id, source, fingerprint, starts_at)
);
CREATE INDEX IF NOT EXISTS ix_alert_incident ON alert_event(incident_id);
CREATE INDEX IF NOT EXISTS ix_alert_firing ON alert_event(tenant_id, source, status);

CREATE TABLE IF NOT EXISTS incident (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, env TEXT NOT NULL, title TEXT NOT NULL, severity TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  stage TEXT NOT NULL DEFAULT 'new',
  root_cause TEXT, confidence REAL, evidence TEXT NOT NULL DEFAULT '[]', actions TEXT NOT NULL DEFAULT '[]', episode_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_alert_at TEXT NOT NULL, resolved_at TEXT, closed_by TEXT,
  investigate_after TEXT, verify_due TEXT
);
CREATE INDEX IF NOT EXISTS ix_incident_open ON incident(tenant_id, status, last_alert_at);

CREATE TABLE IF NOT EXISTS incident_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL, at TEXT NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL, data TEXT
);
CREATE INDEX IF NOT EXISTS ix_incident_event ON incident_event(incident_id, id);

-- Lệnh khắc phục của sự cố: kagent đề xuất sau điều tra (origin=investigation) hoặc người chạy trong hội thoại của sự cố (origin=chat).
-- Thay cột incident.actions (JSON, còn giữ tới bản sau, không ghi thêm).
CREATE TABLE IF NOT EXISTS incident_action (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL,
  episode_id TEXT, approval_id TEXT, origin TEXT NOT NULL, requested_by TEXT NOT NULL,
  tool TEXT NOT NULL, args TEXT, why TEXT, risk_tier TEXT, reasons TEXT NOT NULL DEFAULT '[]', dry_run TEXT,
  status TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0, proposed_at TEXT NOT NULL,
  decided_by TEXT, decided_at TEXT, deny_reason TEXT, ran_at TEXT,
  result TEXT, result_line TEXT, verified INTEGER, verified_at TEXT, superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_incident_action ON incident_action(incident_id, proposed_at);
CREATE INDEX IF NOT EXISTS ix_incident_action_approval ON incident_action(approval_id);
CREATE INDEX IF NOT EXISTS ix_incident_action_pending ON incident_action(tenant_id, status);

-- Ảnh chụp mỗi lượt điều tra của sự cố (rounds.py): kết luận, entity liên quan, tín hiệu và topology con tại lúc điều tra.
-- Không sửa sau khi ghi; panel sự cố đọc từ đây để nhận định luôn gắn với lượt đã sinh ra nó.
CREATE TABLE IF NOT EXISTS incident_round (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL, round INTEGER NOT NULL,
  episode_id TEXT, created_at TEXT NOT NULL, steps INTEGER NOT NULL DEFAULT 0,
  report TEXT NOT NULL, entities TEXT NOT NULL DEFAULT '[]', signals TEXT NOT NULL DEFAULT '[]', graph TEXT NOT NULL DEFAULT '{}',
  UNIQUE(incident_id, round)
);

-- Việc cần người làm ngoài khả năng của kagent (vd. đẩy lại image lên registry). source: summary (bản tóm tắt nêu) | user.
-- removed_at: bản tóm tắt sau không còn nêu việc chưa xong; việc đã xong thì giữ.
CREATE TABLE IF NOT EXISTS incident_task (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, incident_id TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
  created_at TEXT NOT NULL, created_by TEXT, done_by TEXT, done_at TEXT, removed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_incident_task ON incident_task(incident_id, created_at);

-- Lệnh ghi đã chạy kèm kế hoạch hoàn tác (undo.py): snapshot trước khi chạy, lệnh hoàn tác, kiểm tra sau khi chạy.
-- status: verifying | ok | check_failed | conflict | reverted | revert_failed | purged | applied (không có bước kiểm tra)
CREATE TABLE IF NOT EXISTS change_record (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, episode_id TEXT, approval_id TEXT, incident_id TEXT, user_id TEXT NOT NULL,
  env TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, recipe TEXT NOT NULL, plan TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, check_result TEXT, verify_until TEXT, created_at TEXT NOT NULL,
  reverted_at TEXT, reverted_by TEXT, revert_result TEXT, purge_at TEXT, purged_at TEXT, purged_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_change_status ON change_record(tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_change_approval ON change_record(approval_id);

-- Bằng chứng một loại hoàn tác chạy được: source = revert (hoàn tác thật thành công) | drill (diễn tập ở dev/staging).
CREATE TABLE IF NOT EXISTS undo_evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, recipe TEXT NOT NULL, source TEXT NOT NULL, change_id TEXT, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_undo_evidence ON undo_evidence(tenant_id, recipe);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def later(**delta) -> str:
    return iso(now() + timedelta(**delta))


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """Giao dịch ghi. Lồng nhau thì khối ngoài quyết định commit."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_schema(conn: sqlite3.Connection) -> None:
    fresh_actions = not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='incident_action'").fetchone()
    conn.executescript(SCHEMA)
    if "conversation_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(episode)")}:
        conn.execute("ALTER TABLE episode ADD COLUMN conversation_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_episode_conversation ON episode(conversation_id, started_at)")
    if "transcript" not in {r["name"] for r in conn.execute("PRAGMA table_info(episode)")}:
        conn.execute("ALTER TABLE episode ADD COLUMN transcript TEXT")  # tin nhắn đã gửi model ở lượt này, phát lại nguyên văn ở lượt sau (conversation.load_history)
    if "self_approve" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        # Mặc định người yêu cầu tự duyệt lệnh của mình; admin có thể tắt cho tenant cần duyệt chặt.
        conn.execute("ALTER TABLE tenant ADD COLUMN self_approve INTEGER NOT NULL DEFAULT 1")
    if "rule_autopublish" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        conn.execute("ALTER TABLE tenant ADD COLUMN rule_autopublish TEXT NOT NULL DEFAULT '{}'")
    if "summary" not in {r["name"] for r in conn.execute("PRAGMA table_info(compile_run)")}:
        conn.execute("ALTER TABLE compile_run ADD COLUMN summary TEXT")  # tóm tắt dễ đọc của phần cần duyệt (quy ước, skill)
    if "kind" not in {r["name"] for r in conn.execute("PRAGMA table_info(ku_consolidation)")}:
        conn.execute("ALTER TABLE ku_consolidation ADD COLUMN kind TEXT")  # bộ lọc loại khi tạo đề xuất; NULL = mọi loại
    if "created_at" not in {r["name"] for r in conn.execute("PRAGMA table_info(session)")}:
        conn.execute("ALTER TABLE session ADD COLUMN created_at TEXT")  # mốc để giới hạn tuổi tối đa của phiên trượt
    if "llm" not in {r["name"] for r in conn.execute("PRAGMA table_info(llm_call)")}:
        conn.execute("ALTER TABLE llm_call ADD COLUMN llm TEXT")  # tên cấu hình LLM đã dùng
    if "cache_known" not in {r["name"] for r in conn.execute("PRAGMA table_info(llm_call)")}:
        # 0: dịch vụ không báo token cache (vd. vLLM tắt prompt_tokens_details). Dữ liệu cũ: chỉ Claude và lời gọi có số cache là chắc có báo.
        conn.execute("ALTER TABLE llm_call ADD COLUMN cache_known INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE llm_call SET cache_known=0 WHERE cache_read=0 AND cache_write=0 AND coalesce(model,'') NOT LIKE 'claude%'")
    if "cache_write_1h" not in {r["name"] for r in conn.execute("PRAGMA table_info(llm_call)")}:
        # phần của cache_write ghi với TTL 1h (giá 2× thay vì 1,25×); dữ liệu cũ không tách được nên để 0
        conn.execute("ALTER TABLE llm_call ADD COLUMN cache_write_1h INTEGER NOT NULL DEFAULT 0")
    if "grant_policy" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        conn.execute("ALTER TABLE tenant ADD COLUMN grant_policy TEXT NOT NULL DEFAULT '{}'")
    approval_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_request'").fetchone()["sql"]
    if "'skipped'" not in approval_sql:
        # SQLite không sửa được CHECK: dựng lại bảng từ chính câu CREATE hiện có để giữ các cột đã ALTER thêm.
        with tx(conn):
            conn.execute(approval_sql.replace("'expired')", "'expired','skipped')").replace("approval_request", "approval_request_new", 1))
            conn.execute("INSERT INTO approval_request_new SELECT * FROM approval_request")
            conn.execute("DROP TABLE approval_request")
            conn.execute("ALTER TABLE approval_request_new RENAME TO approval_request")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_approval ON approval_request(tenant_id, status)")
    approval_cols = {r["name"] for r in conn.execute("PRAGMA table_info(approval_request)")}
    if "why" not in approval_cols:
        conn.execute("ALTER TABLE approval_request ADD COLUMN why TEXT")  # câu agent tự mô tả lệnh, chỉ để hiện trên thẻ duyệt
    if "note" not in approval_cols:
        conn.execute("ALTER TABLE approval_request ADD COLUMN note TEXT")  # ghi chú người duyệt gửi agent khi chạy lệnh
    if "auto_mode" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        conn.execute("ALTER TABLE tenant ADD COLUMN auto_mode TEXT NOT NULL DEFAULT '{}'")  # luật của classifier (automode.py); grant_policy cũ không còn dùng
    if "incident_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(conversation)")}:
        conn.execute("ALTER TABLE conversation ADD COLUMN incident_id TEXT")  # hội thoại điều tra của sự cố: mọi thành viên tenant mở được
    incident_cols = {r["name"] for r in conn.execute("PRAGMA table_info(incident)")}
    if "conversation_id" not in incident_cols:
        conn.execute("ALTER TABLE incident ADD COLUMN conversation_id TEXT")
    # Tóm tắt cho lãnh đạo, viết lại khi sự cố yên sau hoạt động mới; summary_basis là mốc hoạt động cuối mà bản tóm tắt đã tính tới.
    for column in ("summary", "summary_at", "summary_basis"):
        if column not in incident_cols:
            conn.execute(f"ALTER TABLE incident ADD COLUMN {column} TEXT")
    if "repeats" not in incident_cols:
        # Số lần sự cố quay lại: mở lại sau khi hết, hoặc nhận sự cố khác gộp vào.
        conn.execute("ALTER TABLE incident ADD COLUMN repeats INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE incident SET repeats=(SELECT count(*) FROM incident_event e WHERE e.incident_id=incident.id "
                     "AND e.kind IN ('reopened','merge','auto_merge'))")
    event_cols = {r["name"] for r in conn.execute("PRAGMA table_info(incident_event)")}
    for column in ("actor", "action_id"):  # actor: kagent | system | alert | id người dùng
        if column not in event_cols:
            conn.execute(f"ALTER TABLE incident_event ADD COLUMN {column} TEXT")
    if fresh_actions:
        from .incidents import actions
        actions.migrate(conn)
    if "cause" not in incident_cols:
        # Nguyên nhân theo lượt điều tra mới nhất; root_cause cũ chỉ là lượt đầu tiên.
        for column, ddl in (("cause", "TEXT"), ("cause_at", "TEXT"), ("cause_episode", "TEXT"), ("investigations", "INTEGER NOT NULL DEFAULT 0")):
            conn.execute(f"ALTER TABLE incident ADD COLUMN {column} {ddl}")
        conn.execute("UPDATE incident SET cause=root_cause, cause_episode=episode_id, "
                     "cause_at=(SELECT max(at) FROM incident_event e WHERE e.incident_id=incident.id AND e.kind='investigated'), "
                     "investigations=(SELECT count(*) FROM incident_event e WHERE e.incident_id=incident.id AND e.kind='investigated')")
        from .incidents import actions, grouping
        actions.rebuild_events(conn)  # sự kiện của lệnh viết lại theo dạng "lệnh → kết quả", người làm tính lúc đọc
        grouping.reset_long_titles(conn)
    if "alert_settings" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        conn.execute("ALTER TABLE tenant ADD COLUMN alert_settings TEXT NOT NULL DEFAULT '{}'")  # luồng tự xử lý cảnh báo (incidents.py)
    if "undo_settings" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        conn.execute("ALTER TABLE tenant ADD COLUMN undo_settings TEXT NOT NULL DEFAULT '{}'")  # undo.py: kiểm tra sau khi chạy, thùng rác, nút dừng
    if "undo" not in {r["name"] for r in conn.execute("PRAGMA table_info(approval_request)")}:
        conn.execute("ALTER TABLE approval_request ADD COLUMN undo TEXT")  # tóm tắt kế hoạch hoàn tác hiện trên thẻ duyệt
    registry = {r["name"] for r in conn.execute("PRAGMA table_info(tool_registry)")}
    for column, ddl in (("catalog_tier", "TEXT"), ("risk_floor", "TEXT"), ("roles", "TEXT NOT NULL DEFAULT '[\"member\"]'"),
                        ("status", "TEXT NOT NULL DEFAULT 'active'"), ("synced_at", "TEXT"), ("connection_id", "TEXT"),
                        ("template", "TEXT"), ("owner_user", "TEXT"), ("canonical", "TEXT")):
        if column not in registry:
            conn.execute(f"ALTER TABLE tool_registry ADD COLUMN {column} {ddl}")


def row(r: sqlite3.Row | None, *json_fields: str) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f], parse_constant=lambda _: None)  # trace cũ có thể chứa NaN/Infinity, không trả được qua JSON
    return d


def rows(rs, *json_fields: str) -> list[dict]:
    return [row(r, *json_fields) for r in rs]
