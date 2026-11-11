"""Lưu thông tin xác thực LLM đã mã hóa. Chỉ được giải mã trong tiến trình server, không bao giờ trả về client.

Có thể lưu nhiều cấu hình LLM (bảng llm_profile) và chọn một cấu hình đang dùng (app_setting llm_active).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, replace

from cryptography.fernet import Fernet, InvalidToken

from . import db
from .config import settings

LEGACY_NAME = "anthropic_api_key"  # bản ghi app_secret của phiên bản chỉ lưu một key
ACTIVE = "llm_active"
PROFILE_NAME = re.compile(r"^[\w .:/()-]{1,60}$")
log = logging.getLogger("kagent.vault")


class VaultError(ValueError):
    pass


@dataclass(frozen=True)
class Credential:
    key: str
    base_url: str | None = None  # None = gọi thẳng API của Anthropic
    auth_type: str = "api_key"  # api_key (x-api-key) | token (Authorization: Bearer); chỉ áp dụng cho anthropic
    provider: str = "anthropic"  # anthropic | openai (gồm cả dịch vụ tương thích OpenAI)
    model_strong: str | None = None
    model_fast: str | None = None
    allow_private: bool = False  # org admin cho phép Base URL trong mạng nội bộ (LLM tự host)
    profile: str | None = None  # tên cấu hình, dùng để đo token theo từng LLM


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


def _decrypt(ciphertext: str) -> Credential | None:
    try:
        plain = _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        log.warning("không giải mã được thông tin xác thực LLM đã lưu; khóa chủ có thể đã đổi")
        return None
    if not plain.startswith("{"):  # bản ghi rất cũ chỉ chứa khóa
        return Credential(plain)
    data = json.loads(plain)
    return Credential(**{k: v for k, v in data.items() if k in Credential.__dataclass_fields__})


def _migrate(conn) -> None:
    """Key duy nhất của phiên bản cũ thành cấu hình "Mặc định" đang dùng."""
    r = conn.execute("SELECT * FROM app_secret WHERE name=?", (LEGACY_NAME,)).fetchone()
    if r is None:
        return
    cred = _decrypt(r["ciphertext"])
    if cred is not None and not conn.execute("SELECT 1 FROM llm_profile").fetchone():
        pid = _insert(conn, "Mặc định", cred, r["updated_by"])
        _set_active(conn, pid)
    conn.execute("DELETE FROM app_secret WHERE name=?", (LEGACY_NAME,))


def _insert(conn, name: str, cred: Credential, username: str) -> str:
    pid, now = db.new_id(), db.iso()
    conn.execute("INSERT INTO llm_profile(id,name,provider,base_url,model_strong,model_fast,ciphertext,last4,created_at,updated_at,updated_by) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, name, cred.provider, cred.base_url, cred.model_strong, cred.model_fast, _encrypt(replace(cred, profile=name)),
                  cred.key[-4:], now, now, username))
    return pid


def _encrypt(cred: Credential) -> str:
    return _fernet().encrypt(json.dumps(asdict(cred)).encode()).decode()


def _set_active(conn, profile_id: str | None) -> None:
    conn.execute("INSERT INTO app_setting(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (ACTIVE, profile_id))


def _active_id(conn) -> str | None:
    _migrate(conn)
    r = conn.execute("SELECT value FROM app_setting WHERE key=?", (ACTIVE,)).fetchone()
    return r["value"] if r else None


def _clean_name(name: str | None) -> str:
    name = " ".join((name or "").split())
    if not PROFILE_NAME.match(name):
        raise VaultError("tên cấu hình gồm 1-60 ký tự chữ, số, khoảng trắng và . : / ( ) -")
    return name


def default_name(conn, cred: Credential) -> str:
    """Tên gợi ý khi người dùng không đặt: nhà cung cấp và model, thêm số nếu trùng."""
    base = f"{'OpenAI' if cred.provider == 'openai' else 'Anthropic'}{' · ' + cred.model_strong if cred.model_strong else ''}"[:50]
    name, n = base, 2
    while conn.execute("SELECT 1 FROM llm_profile WHERE name=?", (name,)).fetchone():
        name, n = f"{base} ({n})", n + 1
    return name


def get_profile(conn, profile_id: str) -> Credential | None:
    _migrate(conn)
    r = conn.execute("SELECT ciphertext, name FROM llm_profile WHERE id=?", (profile_id,)).fetchone()
    cred = _decrypt(r["ciphertext"]) if r else None
    return replace(cred, profile=r["name"]) if cred else None


def save_profile(conn, cred: Credential, username: str, *, name: str | None = None, profile_id: str | None = None, activate: bool = True) -> str:
    _migrate(conn)
    name = _clean_name(name) if name else None
    if profile_id:
        r = conn.execute("SELECT name FROM llm_profile WHERE id=?", (profile_id,)).fetchone()
        if r is None:
            raise VaultError("không tìm thấy cấu hình LLM")
        name = name or r["name"]
        if conn.execute("SELECT 1 FROM llm_profile WHERE name=? AND id!=?", (name, profile_id)).fetchone():
            raise VaultError(f"đã có cấu hình tên {name}")
        conn.execute("UPDATE llm_profile SET name=?, provider=?, base_url=?, model_strong=?, model_fast=?, ciphertext=?, last4=?, updated_at=?, updated_by=? "
                     "WHERE id=?", (name, cred.provider, cred.base_url, cred.model_strong, cred.model_fast, _encrypt(replace(cred, profile=name)),
                                    cred.key[-4:], db.iso(), username, profile_id))
    else:
        name = name or default_name(conn, cred)
        if conn.execute("SELECT 1 FROM llm_profile WHERE name=?", (name,)).fetchone():
            raise VaultError(f"đã có cấu hình tên {name}")
        profile_id = _insert(conn, name, cred, username)
    if activate:
        _set_active(conn, profile_id)
    log.info("cấu hình LLM %s được lưu bởi %s", name, username)
    return profile_id


def activate(conn, profile_id: str, username: str) -> None:
    if get_profile(conn, profile_id) is None:
        raise VaultError("không tìm thấy cấu hình LLM")
    _set_active(conn, profile_id)
    log.info("cấu hình LLM %s được chọn bởi %s", profile_id, username)


def delete_profile(conn, profile_id: str, username: str) -> None:
    _migrate(conn)
    if conn.execute("DELETE FROM llm_profile WHERE id=?", (profile_id,)).rowcount == 0:
        raise VaultError("không tìm thấy cấu hình LLM")
    if _active_id(conn) == profile_id:
        _set_active(conn, None)  # không tự chọn cấu hình khác: người dùng chủ động chọn
    log.info("cấu hình LLM %s bị xóa bởi %s", profile_id, username)


# Giao diện cũ (một key): lưu là tạo/cập nhật rồi chọn, xóa là xóa cấu hình đang dùng.

def save(conn, cred: Credential, username: str, name: str | None = None) -> str:
    return save_profile(conn, cred, username, name=name)


def delete(conn, username: str) -> None:
    active = _active_id(conn)
    if active:
        delete_profile(conn, active, username)


def _stored(conn) -> Credential | None:
    active = _active_id(conn)
    return get_profile(conn, active) if active else None


def _env() -> Credential | None:
    if settings.anthropic_key:
        return Credential(settings.anthropic_key, settings.anthropic_base_url or None, profile="biến môi trường")
    if settings.anthropic_auth_token:
        return Credential(settings.anthropic_auth_token, settings.anthropic_base_url or None, "token", profile="biến môi trường")
    return None


def resolve() -> tuple[Credential | None, str]:
    """Trả về (thông tin xác thực, nguồn). Cấu hình chọn trên giao diện được ưu tiên hơn biến môi trường."""
    with db.session() as conn:
        cred = _stored(conn)
    if cred:
        return cred, "ui"
    cred = _env()
    return cred, "env" if cred else "none"


def profiles(conn) -> list[dict]:
    """Danh sách an toàn để hiển thị; không bao giờ chứa key."""
    active = _active_id(conn)
    out = []
    for r in conn.execute("SELECT * FROM llm_profile ORDER BY updated_at DESC, rowid DESC").fetchall():
        cred = _decrypt(r["ciphertext"])
        out.append({"id": r["id"], "name": r["name"], "provider": r["provider"], "base_url": r["base_url"], "model_strong": r["model_strong"],
                    "model_fast": r["model_fast"], "auth_type": cred.auth_type if cred else None, "allow_private": bool(cred and cred.allow_private),
                    "last4": r["last4"], "updated_at": r["updated_at"], "updated_by": r["updated_by"], "active": r["id"] == active,
                    "readable": cred is not None})
    return out


def status(conn) -> dict:
    """Cấu hình đang dùng và danh sách đã lưu; không bao giờ chứa khóa."""
    items = profiles(conn)
    current = next((p for p in items if p["active"]), None)
    source = "ui" if current and current["readable"] else None
    cred = None if source else _env()
    source = source or ("env" if cred else "none")
    base = current if source == "ui" else {
        "id": None, "name": cred.profile if cred else None, "provider": cred.provider if cred else None, "base_url": cred.base_url if cred else None,
        "model_strong": None, "model_fast": None, "auth_type": cred.auth_type if cred else None, "allow_private": False,
        "last4": cred.key[-4:] if cred else None, "updated_at": None, "updated_by": None}
    return {"source": source, "profile_id": base["id"], "profile": base["name"], "last4": base["last4"], "base_url": base["base_url"],
            "auth_type": base["auth_type"], "provider": base["provider"], "model_strong": base["model_strong"], "model_fast": base["model_fast"],
            "allow_private": base["allow_private"], "updated_at": base["updated_at"], "updated_by": base["updated_by"], "profiles": items}
