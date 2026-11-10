"""Danh mục công cụ chuẩn hóa.

- Mỗi MCP server là một file manifest YAML trong kagent/catalog, lưu trong git và review như code.
- Handler nằm trong kagent/backends; manifest chỉ được trỏ tới module trong package đó.
- Mức rủi ro tối thiểu được tính từ metadata; manifest chỉ được khai cao hơn, không được thấp hơn.
- Mỗi tenant có một bản ghi tool_registry được đồng bộ từ danh mục và chỉ được siết thêm:
  bật/tắt, nâng mức rủi ro, thu hẹp môi trường và vai trò được gọi.

Kiểm tra danh mục: python -m kagent.tools
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from . import db
from .backends.sim import ToolError, initial_state, load_state, save_state  # noqa: F401  (giữ API cũ)

CATALOG_DIR = Path(__file__).parent / "catalog"
TIERS = ("R0", "R1", "R2", "R3", "R4")
ENVS = ("dev", "staging", "prod")
ACTION_CLASSES = ("read", "write", "destructive", "exec", "external_send")
REVERSIBLE = ("yes", "partial", "no", "n/a")
BLAST_RADIUS = ("object", "workload", "namespace", "cluster")
SENSITIVITY = ("public", "internal", "personal", "confidential", "secret")
DRY_RUN = ("none", "server")
ROLE_CHOICES = ("member", "tenant_admin", "approver:dev", "approver:staging", "approver:prod")
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
# Công cụ chưa phân loại bị coi là destructive (theo tài liệu kiến trúc).
UNCLASSIFIED = {"action_class": "destructive", "reversible": "no", "blast_radius": "workload", "data_sensitivity": "internal"}
FILE_KEYS = {"server", "description", "defaults", "tools"}
TOOL_KEYS = {"name", "description", "handler", "check", "action_class", "reversible", "blast_radius", "data_sensitivity", "dry_run",
             "untrusted_output", "risk_tier", "envs", "roles", "rollback_hint", "annotations", "input_schema"}
_NAME = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
_HANDLER = re.compile(r"^([a-z0-9_]+):([a-z_][a-z0-9_]*)$")


class CatalogError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("danh mục công cụ không hợp lệ:\n" + "\n".join(f"- {e}" for e in errors))
        self.errors = errors


@dataclass
class Tool:
    name: str
    server: str
    description: str
    action_class: str
    reversible: str
    blast_radius: str
    data_sensitivity: str
    risk_tier: str
    run: Callable[[dict, dict], dict]
    properties: dict
    required: tuple = ()
    envs: tuple = ENVS
    roles: tuple = ("member",)
    dry_run: str = "none"
    untrusted_output: bool = False
    check: Callable[[dict, dict], dict] | None = None
    rollback_hint: str = ""
    classified: bool = True
    warnings: list = field(default_factory=list)

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": self.properties, "required": list(self.required)}


def minimum_tier(action_class: str, reversible: str, blast_radius: str, data_sensitivity: str) -> str:
    """Mức rủi ro thấp nhất mà metadata cho phép, theo bảng R0-R4 của tài liệu kiến trúc."""
    if action_class == "exec" or data_sensitivity == "secret":
        return "R4"
    if action_class == "destructive":
        return "R4" if blast_radius in ("namespace", "cluster") else "R3"
    if action_class == "external_send":
        return "R3"
    if action_class == "write":
        return "R2" if reversible == "yes" and blast_radius in ("object", "workload") else "R3"
    return "R1" if data_sensitivity in ("personal", "confidential") else "R0"


def max_tier(*tiers: str) -> str:
    return max(tiers, key=TIERS.index)


def _resolve(ref, where: str, errors: list[str]):
    m = _HANDLER.match(ref) if isinstance(ref, str) else None
    if not m:
        errors.append(f"{where}: handler phải có dạng 'module:hàm' trong kagent.backends, nhận {ref!r}")
        return None
    try:
        module = importlib.import_module(f"kagent.backends.{m.group(1)}")
    except ImportError:
        errors.append(f"{where}: không có module kagent.backends.{m.group(1)}")
        return None
    fn = getattr(module, m.group(2), None)
    if not callable(fn):
        errors.append(f"{where}: không có hàm {m.group(2)} trong kagent.backends.{m.group(1)}")
        return None
    return fn


def _choice(entry, key, allowed, default, where, errors):
    value = entry.get(key, default)
    if value not in allowed:
        errors.append(f"{where}: {key} phải thuộc {list(allowed)}, nhận {value!r}")
    return value


def _subset(value, allowed, key, where, errors) -> tuple:
    if not isinstance(value, list) or not value or any(v not in allowed for v in value):
        errors.append(f"{where}: {key} phải là danh sách không rỗng gồm {list(allowed)}, nhận {value!r}")
        return ()
    return tuple(v for v in allowed if v in value)


def _tool(entry: dict, server: str, defaults: dict, path: str, errors: list[str]) -> Tool | None:
    if not isinstance(entry, dict) or "name" not in entry:
        errors.append(f"{path}: mỗi công cụ phải là object có 'name'")
        return None
    name = entry["name"]
    where = f"{path}:{name}"
    if not isinstance(name, str) or not _NAME.match(name):
        errors.append(f"{where}: tên phải có dạng nhom.hanh_dong (chữ thường, số, gạch dưới)")
    unknown = set(entry) - TOOL_KEYS
    if unknown:
        errors.append(f"{where}: khóa không hợp lệ {sorted(unknown)}")
    if not isinstance(entry.get("description"), str) or not entry["description"].strip():
        errors.append(f"{where}: thiếu description")
    classified = "action_class" in entry
    base = {} if classified else UNCLASSIFIED
    action_class = _choice(entry, "action_class", ACTION_CLASSES, base.get("action_class"), where, errors)
    reversible = _choice(entry, "reversible", REVERSIBLE, base.get("reversible", "n/a" if action_class == "read" else None), where, errors)
    blast = _choice(entry, "blast_radius", BLAST_RADIUS, base.get("blast_radius", "object" if action_class == "read" else None), where, errors)
    sensitivity = _choice(entry, "data_sensitivity", SENSITIVITY, base.get("data_sensitivity", "internal"), where, errors)
    dry_run = _choice(entry, "dry_run", DRY_RUN, "none", where, errors)
    untrusted = entry.get("untrusted_output", False)
    if not isinstance(untrusted, bool):
        errors.append(f"{where}: untrusted_output phải là true/false")

    floor = minimum_tier(action_class, reversible, blast, sensitivity) if action_class in ACTION_CLASSES else "R4"
    tier = entry.get("risk_tier", floor)
    if tier not in TIERS:
        errors.append(f"{where}: risk_tier phải thuộc {list(TIERS)}")
        tier = "R4"
    elif TIERS.index(tier) < TIERS.index(floor):
        errors.append(f"{where}: risk_tier {tier} thấp hơn mức tối thiểu {floor} tính từ metadata")

    envs = _subset(entry.get("envs", defaults.get("envs", list(ENVS))), ENVS, "envs", where, errors)
    roles = _subset(entry.get("roles", defaults.get("roles", ["member"])), ROLE_CHOICES, "roles", where, errors)

    schema = entry.get("input_schema", {})
    props = schema.get("properties", {}) if isinstance(schema, dict) else None
    required = schema.get("required", []) if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not isinstance(required, list) or set(schema) - {"properties", "required"}:
        errors.append(f"{where}: input_schema chỉ gồm properties (object) và required (danh sách)")
        props, required = {}, []
    elif set(required) - set(props):
        errors.append(f"{where}: required có trường không nằm trong properties: {sorted(set(required) - set(props))}")

    run = _resolve(entry.get("handler"), where, errors)
    check = _resolve(entry["check"], where, errors) if "check" in entry else None
    if dry_run == "server" and "check" not in entry:
        errors.append(f"{where}: dry_run: server cần khai báo check")

    warnings: list[str] = [] if classified else ["chưa phân loại: bị coi là destructive"]
    hints = entry.get("annotations", {})
    if not isinstance(hints, dict) or set(hints) - set(HINTS) or not all(isinstance(v, bool) for v in hints.values()):
        errors.append(f"{where}: annotations chỉ gồm {list(HINTS)} với giá trị true/false")
        hints = {}
    # Annotation do MCP server tự khai chỉ là gợi ý: lệch với metadata đã duyệt thì cảnh báo, không đổi phân loại.
    if hints.get("readOnlyHint") and action_class != "read":
        warnings.append("server khai readOnlyHint nhưng metadata không phải read")
    if hints.get("readOnlyHint") is False and action_class == "read":
        warnings.append("server khai không read-only nhưng metadata là read")
    if hints.get("destructiveHint") and action_class not in ("destructive", "exec"):
        warnings.append("server khai destructiveHint nhưng metadata không phải destructive")
    if hints.get("openWorldHint") and action_class == "read" and not untrusted:
        warnings.append("server khai openWorldHint nhưng đầu ra chưa đánh dấu untrusted_output")
    if errors:
        return None
    return Tool(name=name, server=server, description=entry["description"].strip(), action_class=action_class, reversible=reversible,
                blast_radius=blast, data_sensitivity=sensitivity, risk_tier=tier, run=run, properties=props, required=tuple(required),
                envs=envs, roles=roles, dry_run=dry_run, untrusted_output=untrusted, check=check,
                rollback_hint=str(entry.get("rollback_hint", "")), classified=classified, warnings=warnings)


def load_catalog(directory: Path = CATALOG_DIR) -> tuple[dict[str, Tool], dict[str, str]]:
    """Đọc mọi manifest; gom hết lỗi rồi báo một lần để người sửa thấy đủ."""
    tools: dict[str, Tool] = {}
    servers: dict[str, str] = {}
    errors: list[str] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: YAML lỗi: {exc}")
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("server"), str) or not isinstance(doc.get("tools"), list):
            errors.append(f"{path.name}: cần có server (chuỗi) và tools (danh sách)")
            continue
        if set(doc) - FILE_KEYS:
            errors.append(f"{path.name}: khóa không hợp lệ {sorted(set(doc) - FILE_KEYS)}")
        if doc["server"] in servers:
            errors.append(f"{path.name}: server {doc['server']} đã được khai ở file khác")
        servers[doc["server"]] = str(doc.get("description", ""))
        defaults = doc.get("defaults") or {}
        if not isinstance(defaults, dict) or set(defaults) - {"envs", "roles"}:
            errors.append(f"{path.name}: defaults chỉ gồm envs và roles")
            defaults = {}
        for entry in doc["tools"]:
            local: list[str] = []
            tool = _tool(entry, doc["server"], defaults, path.name, local)
            errors += local
            if tool is None:
                continue
            if tool.name in tools:
                errors.append(f"{path.name}:{tool.name}: tên công cụ bị trùng")
            tools[tool.name] = tool
    if errors:
        raise CatalogError(errors)
    return tools, servers


TOOLS, SERVERS = load_catalog()


def role_choices(tool: Tool | None) -> list[str]:
    """Vai trò tenant được chọn: vai trò của danh mục, tenant_admin, và nhóm approver nếu danh mục cho member (nhóm hẹp hơn)."""
    if tool is None:
        return []
    allowed = set(tool.roles) | {"tenant_admin"}
    if "member" in tool.roles:
        allowed |= {r for r in ROLE_CHOICES if r.startswith("approver:")}
    return [r for r in ROLE_CHOICES if r in allowed]


def effective(row) -> dict:
    """Quyền thực tế của tenant = lựa chọn của tenant ∩ danh mục."""
    tool = TOOLS.get(row["tool"])
    chosen_envs = json.loads(row["envs"])
    chosen_roles = json.loads(row["roles"])
    envs = [e for e in ENVS if tool and e in tool.envs and e in chosen_envs]
    roles = [r for r in role_choices(tool) if r in chosen_roles]
    return {"tier": row["risk_tier"], "envs": envs, "roles": roles}


def callable_by(row, env: str, user_roles: set[str]) -> tuple[bool, str | None]:
    if row["status"] != "active" or row["tool"] not in TOOLS:
        return False, "công cụ đã bị gỡ khỏi danh mục"
    if not row["enabled"]:
        return False, "công cụ chưa bật cho tenant"
    eff = effective(row)
    if env not in eff["envs"]:
        return False, f"công cụ không được dùng ở môi trường {env}"
    if "tenant_admin" not in user_roles and not set(eff["roles"]) & user_roles:
        return False, "vai trò của bạn không được dùng công cụ này"
    return True, None


def sync_registry(conn, tenant_id: str | None = None, enable_new: bool = False) -> dict:
    """Đồng bộ danh mục vào mọi tenant. Công cụ mới mặc định tắt chờ admin bật; công cụ bị gỡ chuyển removed."""
    tenants = [tenant_id] if tenant_id else [r["id"] for r in conn.execute("SELECT id FROM tenant").fetchall()]
    stats = {"added": 0, "updated": 0, "removed": 0}
    now = db.iso()
    with db.tx(conn):
        for tid in tenants:
            existing = {r["tool"]: dict(r) for r in conn.execute("SELECT * FROM tool_registry WHERE tenant_id=?", (tid,)).fetchall()}
            for tool in TOOLS.values():
                meta = (tool.server, tool.action_class, tool.risk_tier, tool.reversible, tool.blast_radius, tool.data_sensitivity, tool.dry_run,
                        int(tool.untrusted_output), tool.description, json.dumps(tool.input_schema, ensure_ascii=False))
                row = existing.get(tool.name)
                if row is None:
                    conn.execute(
                        "INSERT INTO tool_registry(server,action_class,catalog_tier,reversible,blast_radius,data_sensitivity,dry_run,untrusted_output,"
                        "description,input_schema,tenant_id,tool,risk_tier,envs,roles,enabled,approved_by,status,synced_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                        (*meta, tid, tool.name, tool.risk_tier, json.dumps(list(tool.envs)), json.dumps(list(tool.roles)),
                         int(enable_new), "system" if enable_new else None, now))
                    stats["added"] += 1
                    continue
                tier = max_tier(tool.risk_tier, row["risk_floor"]) if row["risk_floor"] in TIERS else tool.risk_tier
                conn.execute(
                    "UPDATE tool_registry SET server=?,action_class=?,catalog_tier=?,reversible=?,blast_radius=?,data_sensitivity=?,dry_run=?,"
                    "untrusted_output=?,description=?,input_schema=?,risk_tier=?,status='active',synced_at=? WHERE tenant_id=? AND tool=?",
                    (*meta, tier, now, tid, tool.name))
                stats["updated"] += 1
            for name, row in existing.items():
                if name not in TOOLS and row["status"] != "removed":
                    conn.execute("UPDATE tool_registry SET status='removed', synced_at=? WHERE tenant_id=? AND tool=?", (now, tid, name))
                    stats["removed"] += 1
    return stats


def main() -> int:
    """In danh mục và cảnh báo; trả mã lỗi 1 nếu manifest không hợp lệ."""
    try:
        tools, servers = load_catalog(Path(sys.argv[1]) if len(sys.argv) > 1 else CATALOG_DIR)
    except CatalogError as exc:
        print(exc)
        return 1
    for server, desc in servers.items():
        print(f"\n{server} — {desc}")
        for t in (t for t in tools.values() if t.server == server):
            note = f"  ! {'; '.join(t.warnings)}" if t.warnings else ""
            print(f"  {t.risk_tier}  {t.name:<26} {t.action_class:<13} envs={','.join(t.envs)} roles={','.join(t.roles)}{note}")
    print(f"\n{len(tools)} công cụ hợp lệ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
