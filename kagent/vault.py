"""Lưu thông tin xác thực LLM đã mã hóa. Chỉ được giải mã trong tiến trình server, không bao giờ trả về client."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import settings

NAME = "anthropic_api_key"
log = logging.getLogger("kagent.vault")


@dataclass(frozen=True)
class Credential:
    key: str
    base_url: str | None = None  # None = gọi thẳng API của Anthropic
    auth_type: str = "api_key"  # api_key (x-api-key) | token (Authorization: Bearer); chỉ áp dụng cho anthropic
    provider: str = "anthropic"  # anthropic | openai (gồm cả dịch vụ tương thích OpenAI)
    model_strong: str | None = None
    model_fast: str | None = None
    allow_private: bool = False  # org admin cho phép Base URL trong mạng nội bộ (LLM tự host)


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


def save(conn, cred: Credential, username: str) -> None:
    payload = json.dumps(asdict(cred))
    conn.execute(
        "INSERT INTO app_secret(name,ciphertext,last4,updated_at,updated_by) VALUES (?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext, last4=excluded.last4, "
        "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
        (NAME, _fernet().encrypt(payload.encode()).decode(), cred.key[-4:], db.iso(), username))
    log.info("thông tin xác thực LLM được cập nhật bởi %s", username)


def delete(conn, username: str) -> None:
    conn.execute("DELETE FROM app_secret WHERE name=?", (NAME,))
    log.info("thông tin xác thực LLM bị xóa bởi %s", username)


def _stored(conn) -> Credential | None:
    r = conn.execute("SELECT ciphertext FROM app_secret WHERE name=?", (NAME,)).fetchone()
    if r is None:
        return None
    try:
        plain = _fernet().decrypt(r["ciphertext"].encode()).decode()
    except InvalidToken:
        log.warning("không giải mã được thông tin xác thực LLM đã lưu; khóa chủ có thể đã đổi")
        return None
    if not plain.startswith("{"):  # bản ghi cũ chỉ chứa khóa
        return Credential(plain)
    return Credential(**json.loads(plain))


def _env() -> Credential | None:
    if settings.anthropic_key:
        return Credential(settings.anthropic_key, settings.anthropic_base_url or None)
    if settings.anthropic_auth_token:
        return Credential(settings.anthropic_auth_token, settings.anthropic_base_url or None, "token")
    return None


def resolve() -> tuple[Credential | None, str]:
    """Trả về (thông tin xác thực, nguồn). Giá trị nhập trên giao diện được ưu tiên hơn biến môi trường."""
    with db.session() as conn:
        cred = _stored(conn)
    if cred:
        return cred, "ui"
    cred = _env()
    return cred, "env" if cred else "none"


def status(conn) -> dict:
    """Thông tin an toàn để hiển thị; không bao giờ chứa khóa."""
    cred = _stored(conn)
    source = "ui"
    updated_at = updated_by = None
    if cred:
        r = conn.execute("SELECT updated_at, updated_by FROM app_secret WHERE name=?", (NAME,)).fetchone()
        updated_at, updated_by = r["updated_at"], r["updated_by"]
    else:
        cred = _env()
        source = "env" if cred else "none"
    return {"source": source, "last4": cred.key[-4:] if cred else None, "base_url": cred.base_url if cred else None,
            "auth_type": cred.auth_type if cred else None, "provider": cred.provider if cred else None,
            "model_strong": cred.model_strong if cred else None, "model_fast": cred.model_fast if cred else None,
            "allow_private": bool(cred and cred.allow_private),
            "updated_at": updated_at, "updated_by": updated_by}
