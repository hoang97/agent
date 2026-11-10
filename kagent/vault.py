"""Lưu khóa LLM đã mã hóa. Khóa chỉ được giải mã trong tiến trình server, không bao giờ trả về client."""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import settings

NAME = "anthropic_api_key"
log = logging.getLogger("kagent.vault")


def _fernet() -> Fernet:
    """Khóa chủ lấy từ môi trường; bản chạy cục bộ không đặt biến thì sinh file quyền 600 trong thư mục dữ liệu."""
    if settings.secret_key:
        return Fernet(settings.secret_key.encode())
    path = settings.data_dir / "secret.key"
    if not path.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return Fernet(path.read_bytes().strip())


def save(conn, key: str, username: str) -> None:
    conn.execute(
        "INSERT INTO app_secret(name,ciphertext,last4,updated_at,updated_by) VALUES (?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext, last4=excluded.last4, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (NAME, _fernet().encrypt(key.encode()).decode(), key[-4:], db.iso(), username))
    log.info("khóa LLM được cập nhật bởi %s", username)


def delete(conn, username: str) -> None:
    conn.execute("DELETE FROM app_secret WHERE name=?", (NAME,))
    log.info("khóa LLM bị xóa bởi %s", username)


def _stored_key(conn) -> str | None:
    r = conn.execute("SELECT ciphertext FROM app_secret WHERE name=?", (NAME,)).fetchone()
    if r is None:
        return None
    try:
        return _fernet().decrypt(r["ciphertext"].encode()).decode()
    except InvalidToken:
        log.warning("không giải mã được khóa LLM đã lưu; khóa chủ có thể đã đổi")
        return None


def resolve() -> tuple[str | None, str]:
    """Trả về (khóa, nguồn). Khóa nhập trên giao diện được ưu tiên hơn biến môi trường."""
    with db.session() as conn:
        key = _stored_key(conn)
    if key:
        return key, "ui"
    if settings.anthropic_key:
        return settings.anthropic_key, "env"
    return None, "none"


def status(conn) -> dict:
    """Thông tin an toàn để hiển thị; không bao giờ chứa khóa."""
    r = conn.execute("SELECT last4, updated_at, updated_by FROM app_secret WHERE name=?", (NAME,)).fetchone()
    if r is not None and _stored_key(conn):
        return {"source": "ui", "last4": r["last4"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
    if settings.anthropic_key:
        return {"source": "env", "last4": settings.anthropic_key[-4:], "updated_at": None, "updated_by": None}
    return {"source": "none", "last4": None, "updated_at": None, "updated_by": None}
