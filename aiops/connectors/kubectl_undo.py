"""Kế hoạch hoàn tác cho kubectl_write/kubectl_admin (xem undo.py), suy ra từ trạng thái cụm thay vì viết tay từng hành động.

- Lệnh sửa (apply, patch, scale, set, label...): chạy thử phía server với -o json để biết chính xác đối tượng nào đổi,
  chụp bản hiện tại; hoàn tác = replace về bản đã chụp. Đối tượng chưa tồn tại = vừa tạo; hoàn tác = xoá đúng đối tượng đó.
- delete: chụp manifest rồi mới xoá; hoàn tác = tạo lại. PVC: đổi PV sang Retain trước khi xoá để giữ dữ liệu (xoá mềm).
- Không hoàn tác được: exec, run, drain, taint, namespace, PV, node, CRD, và mọi thứ chạm Secret (không lưu nội dung secret vào aiops).
Trước khi hoàn tác so resourceVersion với lúc ngay sau lệnh: khác nghĩa là có người sửa sau đó, không tự đè.
"""
from __future__ import annotations

import copy
import json
import shlex

from . import kubectl
from .base import ConnectorError
from .k8s_rbac import TOKEN_KEY

PROFILE = {"kubectl_write": "write", "kubectl_admin": "admin"}
UNSUPPORTED = {"exec": "lệnh chạy trong pod không hoàn tác được", "run": "pod chạy lệnh tùy ý không hoàn tác được",
               "drain": "drain đuổi pod khỏi node, không đưa lại được bằng một lệnh", "certificate": "duyệt chứng chỉ không thu hồi được",
               "taint": "chưa hỗ trợ hoàn tác taint", "diff": None}
NOT_RESTORABLE = {"Namespace": "xoá namespace kéo theo mọi thứ bên trong", "PersistentVolume": "PV giữ dữ liệu",
                  "Node": "node không tạo lại được từ manifest", "CustomResourceDefinition": "xoá CRD kéo theo mọi custom resource"}
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet"}
SELECTORS = ("-l", "--selector", "--all", "-A", "--all-namespaces", "--field-selector")
META_DROP = ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "selfLink", "deletionTimestamp", "deletionGracePeriodSeconds")


def _call(p: dict, profile: str, argv: list[str], manifest: str | None = None, dry: bool = False) -> str:
    """Gọi kubectl không cắt stdout (cần JSON nguyên vẹn). Lệnh do code dựng nhưng vẫn qua parse để giữ các chặn của kubectl.py."""
    argv, info = kubectl.parse(shlex.join(argv), profile)
    if dry:
        argv = kubectl._with_dry_run(argv)
    code, out, err, timed_out = kubectl._exec(argv, kubectl.kubeconfig(p, kubectl._token(p, profile)), manifest, 30)
    if timed_out:
        raise ConnectorError("kubectl hết giờ")
    if code != 0:
        raise ConnectorError(kubectl._error_text(err or out)[:600] or f"kubectl lỗi (mã {code})")
    return out


def _resource(obj: dict) -> str:
    api = obj.get("apiVersion", "v1")
    group, _, version = api.rpartition("/")
    return f"{obj['kind'].lower()}.{version}.{group}" if group else obj["kind"].lower()


def _ns_args(ns: str | None) -> list[str]:
    return ["-n", ns] if ns else []


def _target(obj: dict) -> dict:
    md = obj.get("metadata") or {}
    ns, name, kind = md.get("namespace"), md.get("name"), obj.get("kind")
    return {"key": f"{kind}/{ns or ''}/{name}", "label": f"{kind.lower()}/{name}", "kind": kind, "name": name, "namespace": ns,
            "resource": _resource(obj)}


def _get(p: dict, profile: str, t: dict) -> dict | None:
    try:
        return json.loads(_call(p, profile, ["get", f"{t['resource']}/{t['name']}", *_ns_args(t.get("namespace")), "-o", "json"]))
    except ConnectorError as exc:
        if "NotFound" in str(exc) or "not found" in str(exc):
            return None
        raise


def clean(obj: dict, for_create: bool = False) -> dict:
    o = copy.deepcopy(obj)
    o.pop("status", None)
    md = o.setdefault("metadata", {})
    for k in META_DROP:
        md.pop(k, None)
    if for_create and o.get("kind") == "Service":
        spec = o.get("spec") or {}
        if spec.get("clusterIP") not in (None, "None"):  # IP cũ có thể đã cấp cho service khác; headless thì phải giữ
            spec.pop("clusterIP", None)
            spec.pop("clusterIPs", None)
    return o


def _diff(a, b, path: str = "", out: list | None = None) -> list:
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            _diff(a.get(k), b.get(k), f"{path}.{k}" if path else k, out)
    elif a != b:
        out.append((path, a, b))
    return out


def _short(v) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= 40 else s[:39] + "…"


def _rolling(obj: dict) -> str | None:
    """Đang rollout dở thì rollback về bản chụp có thể đè lên bản đang lên (như Google: rollback không an toàn khi đang có config push)."""
    if obj.get("kind") not in WORKLOADS:
        return None
    md, st, spec = obj.get("metadata") or {}, obj.get("status") or {}, obj.get("spec") or {}
    if st.get("observedGeneration", md.get("generation", 0)) < md.get("generation", 0):
        return f"{obj['kind'].lower()}/{md.get('name')} đang rollout dở"
    want = spec.get("replicas", st.get("desiredNumberScheduled"))
    updated = st.get("updatedReplicas", st.get("updatedNumberScheduled"))
    if want is not None and updated is not None and updated < want:
        return f"{obj['kind'].lower()}/{md.get('name')} đang rollout dở ({updated}/{want} pod đã lên bản mới)"
    return None


def _ready(obj: dict | None) -> tuple[bool, str]:
    if obj is None:
        return False, "không còn"
    kind, md, st, spec = obj.get("kind"), obj.get("metadata") or {}, obj.get("status") or {}, obj.get("spec") or {}
    label = f"{kind.lower()}/{md.get('name')}"
    if kind == "DaemonSet":
        want, ok = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
    elif kind in WORKLOADS:
        want, ok = spec.get("replicas", 1), min(st.get("updatedReplicas", 0) or 0, st.get("availableReplicas", st.get("readyReplicas", 0)) or 0)
    else:
        return True, f"{label} có"
    if st.get("observedGeneration", md.get("generation", 0)) < md.get("generation", 0):
        return False, f"{label} chưa nhận cấu hình mới"
    return ok >= want, f"{label} {ok}/{want} sẵn sàng"


def _positionals(argv: list[str]) -> list[str]:
    return kubectl._positionals(argv)


def _ns_of(argv: list[str], p: dict) -> str:
    for i, a in enumerate(argv):
        if a in ("-n", "--namespace") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(("-n=", "--namespace=")):
            return a.split("=", 1)[1]
    return p.get("namespace") or "default"


def _scope(argv: list[str], targets: list[dict]) -> tuple[bool, str]:
    if any(a in SELECTORS or a.startswith(tuple(f"{s}=" for s in SELECTORS)) for a in argv):
        return False, "chọn đối tượng theo selector hoặc --all, lần chạy khác có thể chạm đối tượng khác"
    return True, ", ".join(t["label"] for t in targets)


# ---------- lập kế hoạch ----------

def plan_undo(p: dict, tool: str, args: dict) -> dict:
    profile = PROFILE.get(tool)
    if profile is None:
        return {"reversible": False, "why": "công cụ chỉ đọc"}
    argv, info = kubectl.parse(args.get("command"), profile)
    sub, key = info["sub"], info["key"]
    if info["read_only"]:
        return {"reversible": True, "noop": True, "recipe": "read", "summary": "lệnh không đổi gì", "targets": []}
    if sub in UNSUPPORTED:
        return {"reversible": False, "why": UNSUPPORTED[sub] or "lệnh không đổi gì", "recipe": key.replace(" ", "_")}
    if sub in ("cordon", "uncordon"):
        return _plan_cordon(p, argv, sub)
    if key in ("rollout pause", "rollout resume"):
        return _plan_pause(p, argv, key)
    if sub == "delete":
        return _plan_delete(p, argv, args, profile)
    return _plan_change(p, argv, args, profile, key)


def _plan_cordon(p: dict, argv: list[str], sub: str) -> dict:
    pos = _positionals(argv)
    if len(pos) != 1:
        return {"reversible": False, "why": "chỉ hoàn tác được cordon/uncordon một node đặt tên", "recipe": sub}
    t = {"key": f"Node//{pos[0]}", "label": f"node/{pos[0]}", "kind": "Node", "name": pos[0], "namespace": None, "resource": "node"}
    node = _get(p, "write", t)
    if node is None:
        raise ConnectorError(f"không có node {pos[0]}")
    want = sub == "cordon"
    was = bool((node.get("spec") or {}).get("unschedulable"))
    inverse = "uncordon" if want else "cordon"
    if was == want:
        return {"reversible": True, "noop": True, "recipe": sub, "summary": f"node/{pos[0]} đã ở trạng thái này", "targets": [t], "scoped": True}
    undo_dry = _dry_ok(p, "write", [inverse, pos[0]])
    return {"reversible": True, "recipe": sub, "summary": f"{inverse} node/{pos[0]}", "targets": [t], "scoped": True, "scope": t["label"],
            "snapshot": {"unschedulable": was}, "undo": [{"op": "kubectl", "profile": "write", "argv": [inverse, pos[0]]}], "undo_dry": undo_dry}


def _plan_pause(p: dict, argv: list[str], key: str) -> dict:
    pos, ns = _positionals(argv), _ns_of(argv, p)
    if len(pos) != 2:
        return {"reversible": False, "why": "chỉ hoàn tác được với một workload đặt tên", "recipe": key.replace(" ", "_")}
    inverse = "resume" if key.endswith("pause") else "pause"
    target = pos[1]
    t = {"key": f"rollout/{ns}/{target}", "label": target, "name": target.split("/")[-1], "namespace": ns}
    return {"reversible": True, "recipe": key.replace(" ", "_"), "summary": f"rollout {inverse} {target}", "targets": [t], "scoped": True,
            "scope": target, "undo": [{"op": "kubectl", "profile": "write", "argv": ["rollout", inverse, target, "-n", ns]}], "namespaces": [ns]}


def _dry_ok(p: dict, profile: str, argv: list[str], manifest: str | None = None) -> bool:
    try:
        _call(p, profile, argv, manifest, dry=True)
        return True
    except ConnectorError:
        return False


def _discover(p: dict, argv: list[str], manifest: str | None, profile: str) -> list[dict]:
    """Đối tượng lệnh sẽ đổi, theo chính API server: chạy thử phía server với -o json."""
    base = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("-o", "--output"):
            skip = True
            continue
        if a.startswith(("-o=", "--output=")):
            continue
        base.append(a)
    out = _call(p, profile, [*base, "-o", "json"], manifest, dry=True)
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise ConnectorError("chạy thử không trả JSON, không xác định được đối tượng sẽ đổi") from exc
    items = data.get("items", []) if data.get("kind", "").endswith("List") else [data]
    return [i for i in items if isinstance(i, dict) and i.get("kind") and (i.get("metadata") or {}).get("name")]


def _plan_change(p: dict, argv: list[str], args: dict, profile: str, key: str) -> dict:
    objs = _discover(p, argv, args.get("manifest"), profile)
    if not objs:
        return {"reversible": False, "why": "chạy thử không cho biết đối tượng nào sẽ đổi", "recipe": key.replace(" ", "_")}
    kinds = sorted({o["kind"] for o in objs})
    recipe = f"{key.replace(' ', '_')}:{'+'.join(kinds)}"
    if "Secret" in kinds:
        return {"reversible": False, "why": "lệnh chạm Secret; aiops không lưu nội dung secret để hoàn tác", "recipe": recipe}
    targets, undo_ops, snapshot, changes, preflight, created = [], [], {}, [], [], []
    restore_dry = True
    for after in objs:
        t = _target(after)
        targets.append(t)
        before = _get(p, profile, t)
        if before is None:
            if not p.get(TOKEN_KEY["admin"]):
                return {"reversible": False, "why": f"hoàn tác {t['label']} vừa tạo cần quyền xoá của nhóm quản trị, nhóm này chưa áp dụng vào cụm",
                        "recipe": recipe, "targets": targets}
            created.append(t["label"])
            undo_ops.append({"op": "kubectl", "profile": "admin", "argv": ["delete", f"{t['resource']}/{t['name']}", *_ns_args(t["namespace"])],
                             "target": t["key"], "created": True})
            continue
        problem = _rolling(before)
        if problem:
            preflight.append(problem)
        snap = clean(before)
        diff = [d for d in _diff(snap, clean(after)) if not d[0].endswith("kubectl.kubernetes.io/restartedAt")]
        if not diff and key != "rollout restart":
            continue
        snapshot[t["key"]] = snap
        changes += [f"{t['label']} {path}: {_short(new)} → {_short(old)}" for path, old, new in diff[:3]]
        manifest = json.dumps(snap, ensure_ascii=False)
        undo_ops.append({"op": "kubectl", "profile": "write", "argv": ["replace", "-f", "-"], "manifest": manifest, "target": t["key"]})
        restore_dry = restore_dry and _dry_ok(p, "write", ["replace", "-f", "-"], manifest)
    scoped, scope = _scope(argv, targets)
    namespaces = sorted({t["namespace"] for t in targets if t["namespace"]})
    if key == "rollout restart":
        return {"reversible": True, "noop": True, "recipe": recipe, "targets": targets, "scoped": scoped, "scope": scope, "preflight": preflight,
                "summary": "không có trạng thái cần hoàn tác: pod được tạo lại với cùng cấu hình", "namespaces": namespaces}
    if not undo_ops:
        return {"reversible": True, "noop": True, "recipe": recipe, "targets": targets, "scoped": scoped, "scope": scope,
                "summary": "chạy thử cho thấy lệnh không đổi gì", "namespaces": namespaces}
    parts = []
    if changes:
        parts.append("đưa về bản đã chụp (" + "; ".join(changes[:3]) + ("; …" if len(changes) > 3 else "") + ")")
    if created:
        parts.append("xoá " + ", ".join(created) + " vừa tạo")
    return {"reversible": True, "recipe": recipe, "summary": ", ".join(parts), "targets": targets, "scoped": scoped, "scope": scope,
            "snapshot": snapshot, "undo": undo_ops, "undo_dry": restore_dry if snapshot and not created else None,
            "preflight": preflight, "namespaces": namespaces}


def _plan_delete(p: dict, argv: list[str], args: dict, profile: str) -> dict:
    names = _call(p, profile, [*argv, "-o", "name"], args.get("manifest"), dry=True).split()
    if not names:
        return {"reversible": False, "why": "chạy thử không cho biết đối tượng nào sẽ bị xoá", "recipe": "delete"}
    ns = _ns_of(argv, p)
    targets, undo_ops, prepare, purge, snapshot, notes, kinds = [], [], [], [], {}, [], set()
    for name in names:
        res, _, obj_name = name.partition("/")
        obj = _get(p, profile, {"resource": res, "name": obj_name, "namespace": ns}) or _get(p, profile, {"resource": res, "name": obj_name, "namespace": None})
        if obj is None:
            continue
        t = _target(obj)
        targets.append(t)
        kind = obj["kind"]
        kinds.add(kind)
        if kind in NOT_RESTORABLE:
            return {"reversible": False, "why": f"xoá {t['label']}: {NOT_RESTORABLE[kind]}", "recipe": f"delete:{kind}", "targets": targets}
        if kind == "Secret":
            return {"reversible": False, "why": "aiops không lưu nội dung secret để tạo lại", "recipe": "delete:Secret", "targets": targets}
        owners = [o for o in (obj["metadata"].get("ownerReferences") or []) if o.get("controller")]
        if kind == "Pod" and owners:
            notes.append(f"{t['label']} do {owners[0]['kind']} {owners[0]['name']} quản lý sẽ được tạo lại")
            continue
        snap = clean(obj, for_create=True)
        snapshot[t["key"]] = snap
        if kind == "PersistentVolumeClaim":
            pv = (obj.get("spec") or {}).get("volumeName")
            if not pv:
                return {"reversible": False, "why": f"{t['label']} chưa gắn PV, không giữ được dữ liệu", "recipe": "delete:PersistentVolumeClaim"}
            vol = _get(p, profile, {"resource": "persistentvolume", "name": pv, "namespace": None})
            policy = ((vol or {}).get("spec") or {}).get("persistentVolumeReclaimPolicy", "Delete")
            if policy != "Retain":
                prepare.append({"op": "kubectl", "profile": "write", "argv": ["patch", f"persistentvolume/{pv}", "-p",
                                json.dumps({"spec": {"persistentVolumeReclaimPolicy": "Retain"}})]})
                purge.append({"op": "kubectl", "profile": "write", "argv": ["patch", f"persistentvolume/{pv}", "-p",
                              json.dumps({"spec": {"persistentVolumeReclaimPolicy": policy}})]})
            undo_ops.append({"op": "kubectl", "profile": "write", "argv": ["patch", f"persistentvolume/{pv}", "--type=json", "-p",
                             json.dumps([{"op": "remove", "path": "/spec/claimRef"}])]})
        undo_ops.append({"op": "kubectl", "profile": "write", "argv": ["create", "-f", "-"], "manifest": json.dumps(snap, ensure_ascii=False),
                         "target": t["key"], "deleted": True})
    scoped, scope = _scope(argv, targets)
    recipe = "delete:" + "+".join(sorted(kinds)) if kinds else "delete"
    if not undo_ops:
        return {"reversible": True, "noop": True, "recipe": recipe, "targets": targets, "scoped": scoped, "scope": scope,
                "summary": "; ".join(notes) or "không có gì bị xoá", "namespaces": sorted({t["namespace"] for t in targets if t["namespace"]})}
    soft = bool(purge) or "PersistentVolumeClaim" in kinds
    undo_dry = _dry_ok(p, "write", prepare[0]["argv"]) if prepare else None
    labels = [t["label"] for t in targets if t["key"] in snapshot]
    summary = "tạo lại " + ", ".join(labels) + " từ manifest đã chụp" + (", gắn lại PV giữ dữ liệu" if soft else "") + (f"; {'; '.join(notes)}" if notes else "")
    return {"reversible": True, "recipe": recipe, "summary": summary, "targets": targets, "scoped": scoped, "scope": scope, "snapshot": snapshot,
            "undo": undo_ops, "prepare": prepare, "purge": purge, "soft": soft, "undo_dry": undo_dry,
            "namespaces": sorted({t["namespace"] for t in targets if t["namespace"]})}


# ---------- sau khi chạy, kiểm tra, hoàn tác ----------

def run_op(p: dict, op: dict) -> dict:
    if op.get("op") != "kubectl":
        raise ConnectorError(f"bước không hợp lệ: {op.get('op')}")
    out = _call(p, op["profile"], op["argv"], op.get("manifest"))
    return {"command": "kubectl " + shlex.join(op["argv"]), "stdout": out.strip()[:500]}


def _targets(plan: dict) -> list[dict]:
    return [t for t in plan.get("targets") or [] if t.get("resource")]


def comparable(obj: dict | None) -> dict | None:
    """Phần người dùng sửa được; bỏ status và annotation do controller tự ghi (resourceVersion đổi cả khi chỉ status đổi)."""
    if obj is None:
        return None
    md = obj.get("metadata") or {}
    ann = {k: v for k, v in (md.get("annotations") or {}).items() if k != "deployment.kubernetes.io/revision"}
    return {"spec": obj.get("spec"), "labels": md.get("labels"), "annotations": ann, "data": obj.get("data")}


def after_change(p: dict, plan: dict, result) -> dict:
    after = {}
    for t in _targets(plan):
        obj = _get(p, "write", t)
        if obj is not None:
            after[t["key"]] = comparable(obj)
    return {**plan, "after": after}


def drift(p: dict, plan: dict) -> list[str]:
    out = []
    deleted = {op.get("target") for op in plan.get("undo") or [] if op.get("deleted")}
    recipe = plan.get("recipe", "")
    if recipe.startswith(("k8s.cordon", "k8s.uncordon")):
        t = plan["targets"][0]
        node = _get(p, "write", t)
        want = recipe.startswith("k8s.cordon")
        if node is not None and bool((node.get("spec") or {}).get("unschedulable")) != want:
            out.append(f"{t['label']} đã được đổi lại sau lệnh")
        return out
    for t in _targets(plan):
        obj = _get(p, "write", t)
        if t["key"] in deleted:
            if obj is not None:
                out.append(f"đã có {t['label']} mới cùng tên")
            continue
        if t["key"] not in (plan.get("after") or {}):
            continue
        if obj is None:
            out.append(f"{t['label']} đã bị xoá sau lệnh")
            continue
        changed = [d[0] for d in _diff(plan["after"][t["key"]], comparable(obj))]
        if changed:
            out.append(f"{t['label']} đã bị sửa sau lệnh: " + ", ".join(changed[:3]))
    return out


def check_change(p: dict, plan: dict) -> dict:
    details, ready = [], True
    for t in _targets(plan):
        if t["kind"] not in WORKLOADS:
            continue
        ok, text = _ready(_get(p, "write", t))
        ready = ready and ok
        details.append(text)
    return {"ready": ready, "detail": ", ".join(details)}


def same_as_before(p: dict, plan: dict) -> bool:
    for t in _targets(plan):
        obj = _get(p, "write", t)
        snap = (plan.get("snapshot") or {}).get(t["key"])
        created = any(op.get("target") == t["key"] and op.get("created") for op in plan.get("undo") or [])
        if created:
            if obj is not None:
                return False
        elif snap is not None:
            if obj is None or _diff(clean(obj).get("spec"), snap.get("spec")):
                return False
    return True
