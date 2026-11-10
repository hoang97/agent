"""PostgreSQL và MySQL thật. Truy vấn đọc chạy trong giao dịch chỉ đọc, có giới hạn thời gian và số dòng."""
from __future__ import annotations

import datetime
import decimal
import uuid

from .. import net
from .base import ConnectorError, need

MAX_ROWS = 200
TIMEOUT_MS = 15000


def _port(p, kind: str) -> int:
    return int(p.get("port") or (5432 if kind == "postgres" else 3306))


def _open(p, kind: str, read_only: bool):
    """Trả về kết nối DB-API. Ghim vào IP đã kiểm tra; TLS (nếu có) vẫn theo tên máy gốc."""
    ip = net.check_host(p["host"], _port(p, kind), p.get("_allow_private", False))
    if kind == "postgres":
        import psycopg
        conn = psycopg.connect(host=p["host"], hostaddr=ip, port=_port(p, kind), dbname=p["database"], user=p["user"],
                               password=p.get("password") or "", sslmode=p.get("sslmode") or "prefer", connect_timeout=10,
                               options=f"-c statement_timeout={TIMEOUT_MS}")
        conn.read_only = read_only
        return conn
    import pymysql
    conn = pymysql.connect(host=ip, port=_port(p, kind), user=p["user"], password=p.get("password") or "", database=p["database"],
                           connect_timeout=10, read_timeout=TIMEOUT_MS // 1000 + 5, write_timeout=20, autocommit=False,
                           ssl={"server_hostname": p["host"]} if p.get("require_tls") else None)
    with conn.cursor() as cur:
        cur.execute(f"SET SESSION max_execution_time={TIMEOUT_MS}")
        if read_only:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
    return conn


def _value(v):
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time, decimal.Decimal, uuid.UUID)):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} byte>"
    return v


def _driver_error(exc: Exception) -> ConnectorError:
    return ConnectorError(f"CSDL báo lỗi: {str(exc).strip().splitlines()[0][:300] if str(exc).strip() else exc.__class__.__name__}")


def _run(p, kind: str, sql: str, read_only: bool, commit: bool):
    try:
        conn = _open(p, kind, read_only)
    except net.NetError:
        raise
    except Exception as exc:  # lỗi kết nối của driver
        raise ConnectorError(f"không kết nối được CSDL: {exc.__class__.__name__}") from exc
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description:
            rows = cur.fetchmany(MAX_ROWS + 1)
            result = {"columns": [d[0] for d in cur.description], "rows": [[_value(v) for v in r] for r in rows[:MAX_ROWS]],
                      "truncated": len(rows) > MAX_ROWS}
        else:
            result = {"affected_rows": cur.rowcount}
        if commit:
            conn.commit()
        else:
            conn.rollback()
        return result
    except ConnectorError:
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise _driver_error(exc) from exc
    finally:
        conn.close()


def _query(kind):
    def query(p, args):
        return _run(p, kind, need(args, "sql"), read_only=True, commit=False)
    return query


def _execute(kind):
    def execute(p, args):
        result = _run(p, kind, need(args, "sql"), read_only=False, commit=True)
        return {"affected_rows": result.get("affected_rows")}
    return execute


def _execute_check(kind):
    def check(p, args):
        result = _run(p, kind, need(args, "sql"), read_only=False, commit=False)  # chạy rồi rollback
        return {"would_affect_rows": result.get("affected_rows"), "objects": 1}
    return check


def _ping(kind):
    def ping(p, args=None):
        _run(p, kind, "SELECT 1", read_only=True, commit=False)
        return {"ok": True}
    return ping


pg_query, pg_execute, pg_execute_check, pg_ping = _query("postgres"), _execute("postgres"), _execute_check("postgres"), _ping("postgres")
my_query, my_execute, my_execute_check, my_ping = _query("mysql"), _execute("mysql"), _execute_check("mysql"), _ping("mysql")
