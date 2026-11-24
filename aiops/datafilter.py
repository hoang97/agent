"""Bộ lọc khai báo chạy ở server trên kết quả công cụ, để agent chỉ đưa phần cần thiết vào context.

Spec (mọi khóa đều tùy chọn):
  path      đường dẫn tới danh sách cần lọc, ví dụ "hits" hoặc "data.result"; để trống thì lấy danh sách lớn nhất
  where     [{field, op, value}], op: eq ne contains icontains regex gt gte lt lte exists in; các điều kiện AND với nhau
  fields    chỉ giữ các trường này (đường dẫn có dấu chấm)
  group_by  đếm số phần tử theo giá trị của một trường
  sort      tên trường, thêm "-" ở đầu để giảm dần
  limit     số phần tử tối đa trả về (mặc định 20, tối đa 200)
  max_chars cắt mỗi giá trị chuỗi (mặc định 500)
Không chạy code do agent viết: chỉ thao tác dữ liệu theo các phép cố định ở trên.
"""
from __future__ import annotations

import json
import re

OPS = ("eq", "ne", "contains", "icontains", "regex", "gt", "gte", "lt", "lte", "exists", "in")
SPEC_KEYS = {"path", "where", "fields", "group_by", "sort", "limit", "max_chars"}
MAX_LIMIT = 200
_MISSING = object()


class FilterError(ValueError):
    pass


def _maybe_json(value):
    """Connector có thể trả bản ghi dạng chuỗi JSON (ví dụ _source của Elasticsearch); lọc được vào bên trong."""
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def get_path(item, path: str):
    current = item
    for part in path.split("."):
        current = _maybe_json(current)
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return _maybe_json(current)


def _largest_list(data, prefix: str = "", depth: int = 0) -> tuple[str, list] | None:
    if isinstance(data, list):
        return prefix, data
    best = None
    if isinstance(data, dict) and depth < 3:
        for key, value in data.items():
            found = _largest_list(_maybe_json(value), f"{prefix}.{key}" if prefix else key, depth + 1)
            if found and (best is None or len(found[1]) > len(best[1])):
                best = found
    return best


def _compare(left, op: str, right) -> bool:
    if op == "exists":
        return (left is not _MISSING) == bool(right if right is not None else True)
    if left is _MISSING:
        return op == "ne"
    if op == "eq":
        return left == right or str(left) == str(right)
    if op == "ne":
        return not (left == right or str(left) == str(right))
    if op == "in":
        return isinstance(right, list) and (left in right or str(left) in [str(x) for x in right])
    text = left if isinstance(left, str) else json.dumps(left, ensure_ascii=False)
    if op == "contains":
        return str(right) in text
    if op == "icontains":
        return str(right).lower() in text.lower()
    if op == "regex":
        try:
            return re.search(str(right), text) is not None
        except re.error as exc:
            raise FilterError(f"regex không hợp lệ: {exc}") from exc
    try:
        a, b = (float(left), float(right)) if not isinstance(left, str) or not isinstance(right, str) else (left, right)
    except (TypeError, ValueError):
        a, b = str(left), str(right)
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _clip(value, max_chars: int):
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + f"…(+{len(value) - max_chars})"
    if isinstance(value, dict):
        return {k: _clip(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v, max_chars) for v in value[:50]]
    return value


def check(spec) -> dict:
    if not isinstance(spec, dict) or set(spec) - SPEC_KEYS:
        raise FilterError(f"_filter chỉ gồm các khóa {sorted(SPEC_KEYS)}")
    for cond in spec.get("where") or []:
        if not isinstance(cond, dict) or not isinstance(cond.get("field"), str) or cond.get("op", "eq") not in OPS:
            raise FilterError(f"điều kiện where không hợp lệ: {cond!r}; op thuộc {list(OPS)}")
    for key in ("fields",):
        if spec.get(key) is not None and (not isinstance(spec[key], list) or not all(isinstance(f, str) for f in spec[key])):
            raise FilterError(f"{key} phải là danh sách tên trường")
    for key in ("limit", "max_chars"):
        if spec.get(key) is not None and (isinstance(spec[key], bool) or not isinstance(spec[key], int) or spec[key] < 1):
            raise FilterError(f"{key} phải là số nguyên dương")
    return spec


def apply(data, spec: dict) -> dict:
    """Trả về {path, total, matched, items | groups}. Không sửa dữ liệu gốc."""
    check(spec)
    if spec.get("path"):
        items = get_path(data, spec["path"])
        if not isinstance(items, list):
            raise FilterError(f"'{spec['path']}' không phải danh sách")
        path = spec["path"]
    else:
        found = _largest_list(_maybe_json(data))
        if found is None:
            raise FilterError("kết quả không có danh sách nào để lọc; hãy chỉ path")
        path, items = found
    where = spec.get("where") or []
    matched = [it for it in items if all(_compare(get_path(it, c["field"]), c.get("op", "eq"), c.get("value")) for c in where)]
    out: dict = {"path": path, "total": len(items), "matched": len(matched)}
    reported = data.get("total") if isinstance(data, dict) else None
    if isinstance(reported, int) and not isinstance(reported, bool) and reported > len(items):
        # Đếm trên một trang kết quả dễ bị hiểu nhầm là đếm toàn bộ.
        out["warning"] = (f"chỉ lọc trên {len(items)}/{reported} bản ghi khớp do giới hạn số bản ghi trả về; "
                          "muốn đếm toàn bộ hãy dùng công cụ đếm phía máy chủ (ví dụ count_logs)")
    limit = min(spec.get("limit") or 20, MAX_LIMIT)
    max_chars = spec.get("max_chars") or 500
    if spec.get("group_by"):
        counts: dict[str, int] = {}
        for it in matched:
            key = get_path(it, spec["group_by"])
            label = "(không có)" if key is _MISSING else key if isinstance(key, str) else json.dumps(key, ensure_ascii=False)
            counts[label] = counts.get(label, 0) + 1
        groups = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        out["groups"] = [{"key": _clip(k, max_chars), "count": c} for k, c in groups[:limit]]
        out["group_count"] = len(groups)
        return out
    if spec.get("sort"):
        field, reverse = spec["sort"].lstrip("-"), spec["sort"].startswith("-")
        matched = sorted(matched, key=lambda it: (get_path(it, field) is _MISSING, str(get_path(it, field))), reverse=reverse)
    rows = matched[:limit]
    if spec.get("fields"):
        rows = [{f: (None if (v := get_path(it, f)) is _MISSING else v) for f in spec["fields"]} for it in rows]
    out["items"] = [_clip(r, max_chars) for r in rows]
    return out


def field_names(items: list, sample: int = 20, depth: int = 2) -> list[str]:
    names: dict[str, None] = {}

    def walk(value, prefix: str, level: int) -> None:
        value = _maybe_json(value)
        if isinstance(value, dict) and level < depth:
            for k, v in value.items():
                walk(v, f"{prefix}.{k}" if prefix else k, level + 1)
        elif prefix:
            names[prefix] = None

    for it in items[:sample]:
        walk(it, "", 0)
    return list(names)[:60]


def preview(data, max_items: int = 3, max_chars: int = 300) -> dict:
    """Bản xem trước của kết quả lớn: cấu trúc, các trường có sẵn và vài phần tử đầu."""
    found = _largest_list(_maybe_json(data))
    out: dict = {}
    if isinstance(data, dict):
        out["keys"] = {k: (f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__) for k, v in list(data.items())[:30]}
        out["scalars"] = {k: _clip(v, 200) for k, v in data.items() if isinstance(v, (str, int, float, bool)) and k != (found[0] if found else None)}
    if found:
        path, items = found
        out.update(path=path, count=len(items), fields=field_names(items), sample=[_clip(it, max_chars) for it in items[:max_items]])
    return out
