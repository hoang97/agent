"""CSDL SQL MÔ PHỎNG. Dữ liệu mẫu lưu trong sim_state; mỗi lệnh dựng một SQLite trong bộ nhớ từ dữ liệu đó.

Giới hạn an toàn: một câu lệnh mỗi lần, authorizer chỉ cho đọc (hoặc DML khi được phép ghi),
không ATTACH/PRAGMA/DDL, giới hạn số dòng trả về và số bước máy ảo.
"""
from __future__ import annotations

import copy
import sqlite3

from .sim import ToolError

MAX_ROWS = 200
MAX_PROGRESS_CALLS = 200  # mỗi lần gọi cách nhau 1000 lệnh máy ảo
SEED = {
    "customers": {
        "columns": ["id INTEGER PRIMARY KEY", "name TEXT", "city TEXT", "email TEXT"],
        "rows": [[1, "Nguyễn An", "Hà Nội", "an@example.com"], [2, "Trần Bình", "Đà Nẵng", "binh@example.com"],
                 [3, "Lê Chi", "TP.HCM", "chi@example.com"], [4, "Phạm Dũng", "Hà Nội", "dung@example.com"]],
    },
    "orders": {
        "columns": ["id INTEGER PRIMARY KEY", "customer_id INTEGER", "amount INTEGER", "status TEXT", "created_at TEXT"],
        "rows": [[101, 1, 250000, "paid", "2026-11-01"], [102, 2, 990000, "failed", "2026-11-02"], [103, 1, 120000, "paid", "2026-11-05"],
                 [104, 3, 450000, "pending", "2026-11-08"], [105, 4, 780000, "failed", "2026-11-09"], [106, 2, 330000, "paid", "2026-11-10"]],
    },
}
_READ = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_WRITE = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_BLOCKED_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


def _tables(state: dict) -> dict:
    return state.setdefault("sql", copy.deepcopy(SEED))


def _open(state: dict, allow_write: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    for name, table in _tables(state).items():
        conn.execute(f'CREATE TABLE "{name}" ({", ".join(table["columns"])})')
        if table["rows"]:
            conn.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" * len(table["rows"][0]))})', table["rows"])
    allowed = _READ | (_WRITE if allow_write else set())

    def authorize(action, arg1, arg2, _db, _trigger):
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    calls = {"n": 0}

    def progress():
        calls["n"] += 1
        return 1 if calls["n"] > MAX_PROGRESS_CALLS else 0

    conn.set_authorizer(authorize)
    conn.set_progress_handler(progress, 1000)
    return conn


def _run(conn: sqlite3.Connection, sql: str):
    if not isinstance(sql, str) or not sql.strip():
        raise ToolError("thiếu câu lệnh SQL")
    try:
        return conn.execute(sql)
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "not authorized" in message:
            raise ToolError("câu lệnh không được phép với công cụ này") from exc
        if "interrupted" in message:
            raise ToolError("câu lệnh chạy quá lâu") from exc
        raise ToolError(f"SQL lỗi: {message}") from exc
    except sqlite3.Warning as exc:  # nhiều câu lệnh trong một lần gọi
        raise ToolError("mỗi lần chỉ chạy một câu lệnh SQL") from exc


def query(state, args):
    conn = _open(state, allow_write=False)
    try:
        cur = _run(conn, args.get("sql"))
        rows = cur.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in cur.description or []]
    finally:
        conn.close()
    return {"columns": columns, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS}


def _changes(state, args, keep: bool) -> int:
    conn = _open(state, allow_write=True)
    try:
        before = conn.total_changes
        _run(conn, args.get("sql"))
        affected = conn.total_changes - before
        if keep:
            conn.set_authorizer(None)
            for name, table in _tables(state).items():
                table["rows"] = [list(r) for r in conn.execute(f'SELECT * FROM "{name}"').fetchall()]
    finally:
        conn.close()
    return affected


def execute(state, args):
    return {"affected_rows": _changes(state, args, keep=True)}


def execute_check(state, args):
    _tables(state)
    probe = copy.deepcopy(state)
    return {"would_affect_rows": _changes(probe, args, keep=False), "objects": 1}
