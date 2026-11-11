"""Template công cụ và các luật phân quyền trên bảng tool_registry.

- Mỗi template (kagent/templates/*.yaml) khai báo tham số kết nối và bộ công cụ, handler nằm trong kagent/connectors.
- Admin hoặc user tạo một *kết nối* từ template với tham số của riêng mình (xem connections.py);
  mỗi công cụ của kết nối thành một dòng tool_registry tên "<kết_nối>.<công_cụ>".
- Mức rủi ro tối thiểu tính từ metadata của template; tenant chỉ được nâng, không được hạ.

Kiểm tra template: python -m kagent.tools
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

TEMPLATE_DIR = Path(__file__).parent / "templates"
TIERS = ("R0", "R1", "R2", "R3", "R4")
ENVS = ("dev", "staging", "prod")
ACTION_CLASSES = ("read", "write", "destructive", "exec", "external_send")
REVERSIBLE = ("yes", "partial", "no", "n/a")
BLAST_RADIUS = ("object", "workload", "namespace", "cluster")
SENSITIVITY = ("public", "internal", "personal", "confidential", "secret")
DRY_RUN = ("none", "server")
FIELD_TYPES = ("string", "url", "secret", "secret_text", "text", "boolean", "integer", "list")
ROLE_CHOICES = ("member", "tenant_admin", "approver:dev", "approver:staging", "approver:prod")
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
UNCLASSIFIED = {"action_class": "destructive", "reversible": "no", "blast_radius": "workload", "data_sensitivity": "internal"}
FILE_KEYS = {"template", "title", "description", "ping", "validate", "fields", "tools"}
TOOL_KEYS = {"name", "description", "handler", "check", "action_class", "reversible", "blast_radius", "data_sensitivity", "dry_run",
             "untrusted_output", "risk_tier", "rollback_hint", "annotations", "input_schema"}
FIELD_KEYS = {"name", "type", "required", "default", "description"}
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_HANDLER = re.compile(r"^([a-z0-9_]+):([a-z_][a-z0-9_]*)$")


class CatalogError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("template công cụ không hợp lệ:\n" + "\n".join(f"- {e}" for e in errors))
        self.errors = errors


@dataclass
class Field:
    name: str
    type: str
    required: bool = False
    default: object = None
    description: str = ""

    @property
    def secret(self) -> bool:
        return self.type in ("secret", "secret_text")


@dataclass
class ToolSpec:
    name: str
    template: str
    description: str
    action_class: str
    reversible: str
    blast_radius: str
    data_sensitivity: str
    risk_tier: str
    run: Callable[[dict, dict], dict]
    properties: dict
    required: tuple = ()
    dry_run: str = "none"
    untrusted_output: bool = False
    check: Callable[[dict, dict], dict] | None = None
    rollback_hint: str = ""
    classified: bool = True
    warnings: list = field(default_factory=list)

    @property
    def canonical(self) -> str:
        return f"{self.template}.{self.name}"

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": self.properties, "required": list(self.required)}


@dataclass
class Template:
    name: str
    title: str
    description: str
    ping: Callable
    fields: list[Field]
    tools: dict[str, ToolSpec]
    validate: Callable | None = None  # kiểm tra chéo giữa các tham số khi tạo hoặc sửa kết nối


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
        errors.append(f"{where}: handler phải có dạng 'module:hàm' trong kagent.connectors, nhận {ref!r}")
        return None
    try:
        module = importlib.import_module(f"kagent.connectors.{m.group(1)}")
    except ImportError:
        errors.append(f"{where}: không có module kagent.connectors.{m.group(1)}")
        return None
    fn = getattr(module, m.group(2), None)
    if not callable(fn):
        errors.append(f"{where}: không có hàm {m.group(2)} trong kagent.connectors.{m.group(1)}")
        return None
    return fn


def _choice(entry, key, allowed, default, where, errors):
    value = entry.get(key, default)
    if value not in allowed:
        errors.append(f"{where}: {key} phải thuộc {list(allowed)}, nhận {value!r}")
    return value


def _field(entry, where: str, errors: list[str]) -> Field | None:
    if not isinstance(entry, dict) or set(entry) - FIELD_KEYS or not _IDENT.match(str(entry.get("name", ""))):
        errors.append(f"{where}: tham số kết nối không hợp lệ {entry!r}")
        return None
    if entry.get("type") not in FIELD_TYPES:
        errors.append(f"{where}:{entry['name']}: type phải thuộc {list(FIELD_TYPES)}")
        return None
    return Field(entry["name"], entry["type"], bool(entry.get("required", False)), entry.get("default"), str(entry.get("description", "")))


def _tool(entry: dict, template: str, path: str, errors: list[str]) -> ToolSpec | None:
    if not isinstance(entry, dict) or "name" not in entry:
        errors.append(f"{path}: mỗi công cụ phải là object có 'name'")
        return None
    name = entry["name"]
    where = f"{path}:{name}"
    if not isinstance(name, str) or not _IDENT.match(name):
        errors.append(f"{where}: tên phải gồm chữ thường, số, gạch dưới")
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
    if hints.get("readOnlyHint") and action_class != "read":
        warnings.append("annotation khai readOnlyHint nhưng metadata không phải read")
    if hints.get("destructiveHint") and action_class not in ("destructive", "exec"):
        warnings.append("annotation khai destructiveHint nhưng metadata không phải destructive")
    if errors:
        return None
    return ToolSpec(name=name, template=template, description=entry["description"].strip(), action_class=action_class, reversible=reversible,
                    blast_radius=blast, data_sensitivity=sensitivity, risk_tier=tier, run=run, properties=props, required=tuple(required),
                    dry_run=dry_run, untrusted_output=untrusted, check=check, rollback_hint=str(entry.get("rollback_hint", "")),
                    classified=classified, warnings=warnings)


def load_templates(directory: Path = TEMPLATE_DIR) -> dict[str, Template]:
    """Đọc mọi template; gom hết lỗi rồi báo một lần."""
    templates: dict[str, Template] = {}
    errors: list[str] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{path.name}: YAML lỗi: {exc}")
            continue
        if not isinstance(doc, dict) or not _IDENT.match(str(doc.get("template", ""))) or not isinstance(doc.get("tools"), list):
            errors.append(f"{path.name}: cần có template (tên chữ thường) và tools (danh sách)")
            continue
        if set(doc) - FILE_KEYS:
            errors.append(f"{path.name}: khóa không hợp lệ {sorted(set(doc) - FILE_KEYS)}")
        name = doc["template"]
        if name in templates:
            errors.append(f"{path.name}: template {name} đã được khai ở file khác")
        local: list[str] = []
        fields = [f for f in (_field(e, path.name, local) for e in doc.get("fields") or []) if f]
        if len({f.name for f in fields}) != len(fields):
            local.append(f"{path.name}: tên tham số kết nối bị trùng")
        ping = _resolve(doc.get("ping"), f"{path.name}:ping", local)
        validate = _resolve(doc["validate"], f"{path.name}:validate", local) if "validate" in doc else None
        tools: dict[str, ToolSpec] = {}
        for entry in doc["tools"]:
            tool_errors: list[str] = []
            spec = _tool(entry, name, path.name, tool_errors)
            local += tool_errors
            if spec is None:
                continue
            if spec.name in tools:
                local.append(f"{path.name}:{spec.name}: tên công cụ bị trùng")
            tools[spec.name] = spec
        errors += local
        if not local:
            templates[name] = Template(name, str(doc.get("title") or name), str(doc.get("description") or ""), ping, fields, tools, validate)
    if errors:
        raise CatalogError(errors)
    return templates


TEMPLATES = load_templates()


def spec_of(row) -> ToolSpec | None:
    """ToolSpec của một dòng tool_registry (None nếu template hoặc công cụ không còn)."""
    template = TEMPLATES.get(row["template"] or "")
    return template.tools.get(row["tool"].split(".", 1)[-1]) if template else None


def effective(row) -> dict:
    return {"tier": row["risk_tier"], "envs": [e for e in ENVS if e in json.loads(row["envs"])],
            "roles": [r for r in ROLE_CHOICES if r in json.loads(row["roles"])]}


def callable_by(row, env: str, user_roles: set[str], user_id: str | None = None) -> tuple[bool, str | None]:
    if row["status"] != "active" or spec_of(row) is None:
        return False, "công cụ đã bị gỡ hoặc kết nối không còn hoạt động"
    owner = row["owner_user"]
    if owner and owner != user_id:
        return False, "đây là công cụ cá nhân của người khác"
    if not row["enabled"]:
        return False, "công cụ chưa bật"
    eff = effective(row)
    if env not in eff["envs"]:
        return False, f"công cụ không được dùng ở môi trường {env}"
    if not owner and "tenant_admin" not in user_roles and not set(eff["roles"]) & user_roles:
        return False, "vai trò của bạn không được dùng công cụ này"
    return True, None


def main() -> int:
    try:
        templates = load_templates(Path(sys.argv[1]) if len(sys.argv) > 1 else TEMPLATE_DIR)
    except CatalogError as exc:
        print(exc)
        return 1
    for t in templates.values():
        print(f"\n{t.name} — {t.title}: {', '.join(f.name + ('*' if f.required else '') for f in t.fields)}")
        for s in t.tools.values():
            note = f"  ! {'; '.join(s.warnings)}" if s.warnings else ""
            print(f"  {s.risk_tier}  {s.name:<20} {s.action_class:<13}{' untrusted' if s.untrusted_output else ''}{note}")
    print(f"\n{len(templates)} template, {sum(len(t.tools) for t in templates.values())} công cụ hợp lệ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
