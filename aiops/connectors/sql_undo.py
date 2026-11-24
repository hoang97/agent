"""Hoàn tác cho execute của PostgreSQL/MySQL: chỉ các dạng câu lệnh nhận ra chắc chắn, còn lại là không hoàn tác được.

- DROP TABLE  → xoá mềm: đổi tên sang bảng thùng rác; hoàn tác = đổi tên lại.
- DELETE/UPDATE/TRUNCATE → sao lưu các dòng sẽ bị đổi sang bảng riêng, kiểm tra đủ số dòng rồi mới chạy; hoàn tác = ghi lại từ bản sao lưu.
  Quá max_rows dòng thì không sao lưu (khôi phục quá lâu), lệnh phải có người duyệt.
- Migration pha mở rộng (ADD COLUMN, CREATE TABLE, CREATE INDEX) → hoàn tác = xoá đúng thứ vừa thêm.
- Pha thu gọn (DROP/RENAME/đổi kiểu cột, DROP INDEX/VIEW/SCHEMA) luôn cần người duyệt.
Giữa lúc sao lưu và lúc chạy lệnh có một khoảng ngắn không cùng giao dịch: dòng ghi vào đúng lúc đó có thể không nằm trong bản sao lưu.
"""
from __future__ import annotations

import re
import uuid

from . import sql
from .base import ConnectorError

NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
_F = re.I | re.S
DROP_TABLE = re.compile(rf"^drop\s+table\s+(if\s+exists\s+)?({NAME})$", _F)
TRUNCATE = re.compile(rf"^truncate\s+(?:table\s+)?({NAME})$", _F)
DELETE = re.compile(rf"^delete\s+from\s+({NAME})(?:\s+where\s+(.+))?$", _F)
UPDATE = re.compile(rf"^update\s+({NAME})\s+set\s+(.+?)(?:\s+where\s+(.+))?$", _F)
ADD_COLUMN = re.compile(rf"^alter\s+table\s+({NAME})\s+add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+[^,]+$", _F)
CREATE_TABLE = re.compile(rf"^create\s+table\s+(if\s+not\s+exists\s+)?({NAME})\b", _F)
CREATE_INDEX = re.compile(rf"^create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(if\s+not\s+exists\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+on\s+({NAME})\b", _F)
CONTRACT = re.compile(r"^(alter\s+table\s+.+\s(drop|rename|modify|change)\b|alter\s+table\s+.+\salter\s+(column\s+)?\S+\s+(type|set\s+data\s+type)\b"
                      r"|drop\s+(index|view|schema|database|column|sequence|function|trigger)\b)", _F)
CONTRACT_REASON = "pha thu gọn của migration (xoá, đổi tên, đổi kiểu cột hoặc xoá index/view/schema) luôn cần người duyệt"


def _kind(p: dict) -> str:
    return "mysql" if p.get("_template") == "mysql" else "postgres"


def _session(p: dict, fn, commit: bool):
    conn = sql._open(p, _kind(p), False)
    try:
        cur = conn.cursor()
        out = fn(cur)
        conn.commit() if commit else conn.rollback()
        return out
    except ConnectorError:
        conn.rollback()
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise sql._driver_error(exc) from exc
    finally:
        conn.close()


def _scalar(cur, statement: str):
    cur.execute(statement)
    return cur.fetchone()[0]


def _exists(p: dict, table: str) -> bool:
    try:
        _session(p, lambda cur: cur.execute(f"SELECT 1 FROM {table} WHERE 1=0"), False)
        return True
    except ConnectorError:
        return False


def _has_column(p: dict, table: str, column: str) -> bool:
    try:
        _session(p, lambda cur: cur.execute(f"SELECT {column} FROM {table} WHERE 1=0"), False)
        return True
    except ConnectorError:
        return False


def primary_key(cur, kind: str, table: str) -> list[str]:
    if kind == "postgres":
        cur.execute("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
                    "WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY a.attnum", (table,))
    else:
        schema, _, name = table.rpartition(".")
        cur.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=COALESCE(NULLIF(%s,''),DATABASE()) "
                    "AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' ORDER BY ORDINAL_POSITION", (schema, name))
    return [r[0] for r in cur.fetchall()]


def _columns(cur, table: str) -> list[str]:
    cur.execute(f"SELECT * FROM {table} WHERE 1=0")
    return [d[0] for d in cur.description]


def _side(table: str, tag: str) -> str:
    """Bảng phụ cùng schema: _aiops_<tag>_<tên>_<8 ký tự>; tên giới hạn để vừa 63 ký tự của PostgreSQL."""
    schema, _, name = table.rpartition(".")
    side = f"_aiops_{tag}_{name[:30]}_{uuid.uuid4().hex[:8]}"
    return f"{schema}.{side}" if schema else side


def _rename(p: dict, src: str, dst: str) -> str:
    short = dst.rpartition(".")[2]
    return f"RENAME TABLE {src} TO {dst}" if _kind(p) == "mysql" else f"ALTER TABLE {src} RENAME TO {short}"


def _t(table: str, extra: str = "") -> dict:
    return {"key": f"table/{table}", "label": f"bảng {table}" + extra, "name": table.rpartition(".")[2]}


def plan_undo(p: dict, tool: str, args: dict) -> dict:
    if tool != "execute":
        return {"reversible": False, "why": "công cụ chỉ đọc"}
    text = str(args.get("sql") or "").strip().rstrip(";").strip()
    if ";" in text:
        return {"reversible": False, "why": "nhiều câu lệnh trong một lần gọi; tách từng câu để hoàn tác được", "recipe": "multi"}
    if m := DROP_TABLE.match(text):
        return _plan_drop(p, m.group(2), bool(m.group(1)))
    if CONTRACT.match(text):
        return {"reversible": False, "why": CONTRACT_REASON, "forced": [CONTRACT_REASON], "recipe": "contract"}
    if m := TRUNCATE.match(text):
        return _plan_backup(p, "truncate", m.group(1), None)
    if m := DELETE.match(text):
        return _plan_backup(p, "delete", m.group(1), m.group(2))
    if m := UPDATE.match(text):
        return _plan_backup(p, "update", m.group(1), m.group(3))
    if m := ADD_COLUMN.match(text):
        table, column = m.group(1), m.group(2)
        if _has_column(p, table, column):
            return {"reversible": True, "noop": True, "recipe": "add_column", "summary": f"cột {column} đã có sẵn", "targets": [_t(table)]}
        return {"reversible": True, "recipe": "add_column", "summary": f"xoá cột {column} vừa thêm vào {table}", "targets": [_t(table)],
                "scoped": True, "scope": f"cột {table}.{column}", "undo": [{"op": "sql", "statements": [f"ALTER TABLE {table} DROP COLUMN {column}"]}]}
    if m := CREATE_TABLE.match(text):
        table = m.group(2)
        if _exists(p, table):
            return {"reversible": True, "noop": True, "recipe": "create_table", "summary": f"bảng {table} đã có sẵn", "targets": [_t(table)]}
        return {"reversible": True, "recipe": "create_table", "summary": f"xoá bảng {table} vừa tạo", "targets": [_t(table)], "scoped": True,
                "scope": f"bảng {table}", "undo": [{"op": "sql", "statements": [f"DROP TABLE {table}"]}]}
    if m := CREATE_INDEX.match(text):
        index, table = m.group(2), m.group(3)
        drop = f"DROP INDEX {index} ON {table}" if _kind(p) == "mysql" else f"DROP INDEX {index}"
        return {"reversible": True, "recipe": "create_index", "summary": f"xoá index {index} vừa tạo", "targets": [_t(table, f", index {index}")],
                "scoped": True, "scope": f"index {index}", "undo": [{"op": "sql", "statements": [drop]}]}
    if re.match(r"^insert\b", text, re.I):
        return {"reversible": False, "why": "chưa hỗ trợ hoàn tác INSERT (cần biết khoá của dòng vừa thêm)", "recipe": "insert"}
    return {"reversible": False, "why": "không nhận ra dạng câu lệnh để lập cách hoàn tác", "recipe": "unknown"}


def _plan_drop(p: dict, table: str, if_exists: bool) -> dict:
    if not _exists(p, table):
        if if_exists:
            return {"reversible": True, "noop": True, "recipe": "soft_drop_table", "summary": f"bảng {table} không tồn tại", "targets": []}
        raise ConnectorError(f"không có bảng {table}")
    trash = _side(table, "trash")
    return {"reversible": True, "soft": True, "recipe": "soft_drop_table", "summary": f"xoá mềm: đổi tên bảng {table} sang {trash}, đổi lại để khôi phục",
            "targets": [_t(table)], "scoped": True, "scope": f"bảng {table}", "snapshot": {"table": table, "trash": trash},
            "instead": [{"op": "sql", "statements": [_rename(p, table, trash)], "result": {"dropped_table": table, "trash_table": trash}}],
            "undo": [{"op": "sql", "statements": [_rename(p, trash, table)]}], "purge": [{"op": "sql", "statements": [f"DROP TABLE {trash}"]}]}


def _plan_backup(p: dict, verb: str, table: str, where: str | None) -> dict:
    kind = _kind(p)
    cond = f" WHERE {where}" if where else ""
    recipe = f"{verb}_rows"

    def inspect(cur):
        return _scalar(cur, f"SELECT count(*) FROM {table}{cond}"), _columns(cur, table), (primary_key(cur, kind, table) if verb == "update" else [])
    count, columns, pk = _session(p, inspect, False)
    limit = p.get("_undo_max_rows", 100_000)
    if count > limit:
        return {"reversible": False, "why": f"lệnh đổi {count} dòng, quá ngưỡng sao lưu {limit} dòng (khôi phục sẽ quá lâu)", "recipe": recipe,
                "targets": [_t(table)]}
    if verb == "update" and not pk:
        return {"reversible": False, "why": f"bảng {table} không có khoá chính, không ghi lại đúng dòng cũ được", "recipe": recipe}
    bak = _side(table, "bak")
    backup = {"op": "sql", "statements": [f"CREATE TABLE {bak} AS SELECT * FROM {table}{cond}"],
              "expect": [f"SELECT count(*) FROM {bak}", f"SELECT count(*) FROM {table}{cond}"]}
    plan = {"reversible": True, "soft": True, "backup": True, "recipe": recipe, "targets": [_t(table, f" ({count} dòng)")],
            "scoped": bool(where) or verb == "truncate" and count <= limit, "scope": f"{count} dòng của bảng {table}" + ("" if where else " (cả bảng)"),
            "snapshot": {"table": table, "backup": bak, "rows": count, "pk": pk, "columns": columns}, "prepare": [backup],
            "purge": [{"op": "sql", "statements": [f"DROP TABLE {bak}"]}]}
    if verb in ("delete", "truncate"):
        plan["summary"] = f"ghi lại {count} dòng từ bản sao lưu {bak}"
        plan["undo"] = [{"op": "sql", "statements": [f"INSERT INTO {table} SELECT * FROM {bak}"]}]
        if verb == "delete" and not where:
            plan["scoped"], plan["scope"] = False, f"xoá cả bảng {table}"
        return plan
    keys = ", ".join(pk)
    rest = [c for c in columns if c not in pk]
    if kind == "mysql":
        on = " AND ".join(f"t.{k}=b.{k}" for k in pk)
        restore = f"UPDATE {table} t JOIN {bak} b ON {on} SET " + ", ".join(f"t.{c}=b.{c}" for c in rest)
    else:
        match = " AND ".join(f"b.{k}={table}.{k}" for k in pk)
        restore = (f"UPDATE {table} SET ({', '.join(rest)}) = (SELECT {', '.join('b.' + c for c in rest)} FROM {bak} b WHERE {match}) "
                   f"WHERE ({keys}) IN (SELECT {keys} FROM {bak})")
    aft = _side(table, "aft")
    plan.update(summary=f"ghi lại giá trị cũ của {count} dòng từ bản sao lưu {bak}", undo=[{"op": "sql", "statements": [restore]}],
                after_ops=[{"op": "sql", "statements": [f"CREATE TABLE {aft} AS SELECT * FROM {table} WHERE ({keys}) IN (SELECT {keys} FROM {bak})"]}])
    plan["snapshot"]["after"] = aft
    plan["purge"][0]["statements"].append(f"DROP TABLE {aft}")
    if not where:
        plan["scoped"], plan["scope"] = False, f"sửa cả bảng {table}"
    return plan


def run_op(p: dict, op: dict) -> dict:
    if op.get("op") != "sql":
        raise ConnectorError(f"bước không hợp lệ: {op.get('op')}")

    def go(cur):
        affected = 0
        for statement in op["statements"]:
            cur.execute(statement)
            affected += max(cur.rowcount or 0, 0)
        if op.get("expect"):
            a, b = (_scalar(cur, q) for q in op["expect"])
            if a != b:
                raise ConnectorError(f"bản sao lưu có {a} dòng, cần {b}; huỷ, chưa chạy lệnh")
        return {**(op.get("result") or {}), "affected_rows": affected}
    return _session(p, go, True)


def after_change(p: dict, plan: dict, result) -> dict:
    for op in plan.get("after_ops") or []:
        run_op(p, op)
    return plan


def drift(p: dict, plan: dict) -> list[str]:
    snap = plan.get("snapshot") or {}
    recipe = plan.get("recipe", "")
    if recipe.endswith("soft_drop_table"):
        return [f"đã có bảng {snap['table']} mới cùng tên"] if _exists(p, snap["table"]) else []
    if recipe.endswith("truncate_rows"):
        n = _session(p, lambda cur: _scalar(cur, f"SELECT count(*) FROM {snap['table']}"), False)
        return [f"bảng {snap['table']} đã có {n} dòng mới sau lệnh"] if n else []
    if recipe.endswith("update_rows") and snap.get("after"):
        cols = ", ".join(snap["columns"])
        keys = ", ".join(snap["pk"])
        q = (f"SELECT count(*) FROM (SELECT {cols} FROM {snap['table']} WHERE ({keys}) IN (SELECT {keys} FROM {snap['after']}) "
             f"EXCEPT SELECT {cols} FROM {snap['after']}) x")
        n = _session(p, lambda cur: _scalar(cur, q), False)
        return [f"{n} dòng của bảng {snap['table']} đã bị sửa tiếp sau lệnh"] if n else []
    return []


def same_as_before(p: dict, plan: dict) -> bool | None:
    snap = plan.get("snapshot") or {}
    if snap.get("table") and snap.get("rows") is not None and not plan.get("recipe", "").endswith("update_rows"):
        return None
    return _exists(p, snap["table"]) if snap.get("table") else None
