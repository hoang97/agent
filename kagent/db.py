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
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied','expired')),
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
    conn.executescript(SCHEMA)
    if "conversation_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(episode)")}:
        conn.execute("ALTER TABLE episode ADD COLUMN conversation_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_episode_conversation ON episode(conversation_id, started_at)")
    if "self_approve" not in {r["name"] for r in conn.execute("PRAGMA table_info(tenant)")}:
        # Mặc định người yêu cầu tự duyệt lệnh của mình; admin có thể tắt cho tenant cần duyệt chặt.
        conn.execute("ALTER TABLE tenant ADD COLUMN self_approve INTEGER NOT NULL DEFAULT 1")
    registry = {r["name"] for r in conn.execute("PRAGMA table_info(tool_registry)")}
    for column, ddl in (("catalog_tier", "TEXT"), ("risk_floor", "TEXT"), ("roles", "TEXT NOT NULL DEFAULT '[\"member\"]'"),
                        ("status", "TEXT NOT NULL DEFAULT 'active'"), ("synced_at", "TEXT")):
        if column not in registry:
            conn.execute(f"ALTER TABLE tool_registry ADD COLUMN {column} {ddl}")


def row(r: sqlite3.Row | None, *json_fields: str) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for f in json_fields:
        if d.get(f) is not None:
            d[f] = json.loads(d[f])
    return d


def rows(rs, *json_fields: str) -> list[dict]:
    return [row(r, *json_fields) for r in rs]
