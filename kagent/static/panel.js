"use strict";
// Bọc trong IIFE: app.js và chat.js đã có các tên chung như SVG_NS, ago, pct ở phạm vi toàn cục.
(() => {
// Panel sự cố (single pane): cột phải của hội thoại điều tra (mode "compact") và phần đầu trang sự cố (mode "full").
// Dữ liệu từ GET /api/incidents/{id}/panel: như trang sự cố, kèm ảnh chụp của một lượt điều tra (round).
// Mọi nội dung động đi qua textContent (h() và svgEl()), không dùng innerHTML.
//
// Topology: cạnh chuẩn hoá về hai loại.
//   contains   — gom nhóm (workload chứa pod, service chứa pod); lỗi không lan theo cạnh này, dùng để gộp node.
//   depends_on — A cần B (gọi, chạy trên, trỏ tới, đọc, gắn); lỗi lan ngược chiều cạnh: B hỏng thì A bị ảnh hưởng.
// Dạng gọn (chat) vẽ đường lan lỗi thành chuỗi; dạng đầy đủ vẽ đồ thị theo cột lối vào → ứng dụng → dữ liệu, máy chủ là dải riêng.

const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const NODE_TONE = { ok: "ok", warn: "warn", bad: "bad", nodata: "nodata" };
const RANK_STATUS = { bad: 3, warn: 2, nodata: 1, ok: 0 };
const worst = (a, b) => ((RANK_STATUS[a] ?? 1) >= (RANK_STATUS[b] ?? 1) ? a : b);
const KIND_SHORT = { deployment: "deploy", statefulset: "sts", daemonset: "ds", service: "svc", pod: "pod", node: "node", pvc: "pvc", replicaset: "rs",
  ingress: "ingress", database: "db", cache: "cache", queue: "queue", external: "bên ngoài", host: "máy", hypervisor: "hypervisor", server: "vm",
  process: "tiến trình", endpoint: "endpoint", lb: "lb", workload: "workload", instance: "instance" };
const SUB_LABEL = { calls: "gọi", routes_to: "trỏ tới", reads: "đọc", mounts: "gắn", runs_on: "chạy trên", depends_on: "phụ thuộc", uses: "dùng", connects: "kết nối" };
// Vai trò của node để chia cột; máy chủ đi dải riêng; instance được gộp vào node cha nếu có.
const ROLE_OF = { ingress: "entry", lb: "entry", gateway: "entry", database: "data", cache: "data", queue: "data", topic: "data", pvc: "data", volume: "data",
  external: "data", bucket: "data", node: "host", host: "host", hypervisor: "host" };
const INSTANCE_KINDS = new Set(["pod", "instance", "container"]);
const WORKLOAD_KINDS = new Set(["deployment", "statefulset", "daemonset", "workload", "replicaset", "job", "cronjob"]);
const roleOf = (kind) => ROLE_OF[kind] || "app";

const ago = (s) => {
  if (!s) return "";
  const m = Math.max(0, Math.round((Date.now() - new Date(s)) / 60000));
  return m < 1 ? "vừa xong" : m < 60 ? `${m} phút` : m < 1440 ? `${Math.floor(m / 60)} giờ ${m % 60} phút` : `${Math.floor(m / 1440)} ngày`;
};
const hhmm = (s) => (s ? new Date(s).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" }) : "");
const kindLabel = (k) => KIND_SHORT[k] || k || "?";
const entityLabel = (e) => `${kindLabel(e.kind)}/${e.name}`;
const pct = (v) => (typeof v === "number" ? `${Math.round(v * 100)}%` : "—");
const confBar = (v) => h("span", { class: "ip-conf" }, h("span", { class: "ip-conf-track" }, h("span", { class: "ip-conf-bar", style: `width:${Math.round((v || 0) * 100)}%` })), pct(v));
const fmtNum = (n) => (typeof n === "number" ? (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(".", ",")}k` : String(Math.round(n * 10) / 10).replace(".", ",")) : "");

function fmtVal(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v !== "number") return String(v);
  const a = Math.abs(v);
  return a >= 100 ? Math.round(v).toLocaleString("vi-VN") : a >= 1 ? v.toFixed(1).replace(/\.0$/, "") : v.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

// Nút gập của một khối: nhớ trạng thái theo tên khối.
function panelBlock(key, title, meta, body, opts) {
  const open = store.get(`panel:${key}`, true);
  const det = h("details", { class: "ip-block" + (opts && opts.cls ? " " + opts.cls : ""), open: open ? "" : null },
    h("summary", {}, h("span", { class: "ip-title" }, title), meta && h("span", { class: "small muted ip-meta" }, meta)), body);
  det.addEventListener("toggle", () => store.set(`panel:${key}`, det.open ? null : false));
  return det;
}

// Sparkline: đường số liệu, đường nền đứt nét, vạch dọc lúc cảnh báo bắn (đỏ) và lúc lệnh chạy (xanh).
function sparkline(series, opts) {
  const W = (opts && opts.width) || 120, H = (opts && opts.height) || 28, P = 2;
  const pts = (series || []).filter((p) => Array.isArray(p) && p.length >= 2 && Number.isFinite(+p[1]));
  if (pts.length < 2) return h("span", { class: "small muted ip-nospark" }, "không có số liệu");
  const xs = pts.map((p) => +p[0]), ys = pts.map((p) => +p[1]);
  const base = opts && Number.isFinite(+opts.baseline) ? +opts.baseline : null;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys, base ?? Infinity), y1 = Math.max(...ys, base ?? -Infinity);
  if (y1 === y0) { y1 += 1; y0 -= 1; }
  const X = (x) => P + ((x - x0) / (x1 - x0 || 1)) * (W - 2 * P);
  const Y = (y) => H - P - ((y - y0) / (y1 - y0)) * (H - 2 * P);
  const marks = ((opts && opts.marks) || []).map((m) => ({ ...m, t: new Date(m.at).getTime() / 1000 })).filter((m) => m.t >= x0 && m.t <= x1);
  const last = pts[pts.length - 1];
  return svgEl("svg", { class: "ip-spark " + ((opts && opts.tone) || ""), viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
    "aria-label": (opts && opts.label) || "biểu đồ nhỏ" },
    base !== null && svgEl("line", { class: "ip-spark-base", x1: P, x2: W - P, y1: Y(base), y2: Y(base) }),
    marks.map((m) => svgEl("line", { class: "ip-spark-mark " + m.kind, x1: X(m.t), x2: X(m.t), y1: 0, y2: H }, svgEl("title", {}, m.label))),
    svgEl("polyline", { class: "ip-spark-line", points: pts.map((p) => `${X(+p[0]).toFixed(1)},${Y(+p[1]).toFixed(1)}`).join(" ") }),
    svgEl("circle", { class: "ip-spark-dot", cx: X(+last[0]), cy: Y(+last[1]), r: 2 }),
    svgEl("title", {}, (opts && opts.label) || ""));
}

// ---------- mô hình topology ----------

// Đổi cạnh của mọi nguồn (K8s cũ: owns/selects/hosts; mới: contains/depends_on) về {from, to, rel, sub}.
function normalizeEdge(e) {
  const t = e.type || e.rel || "depends_on";
  if (t === "owns" || t === "selects" || t === "contains") return { from: e.from, to: e.to, rel: "contains", sub: t, attrs: e.attrs || {} };
  if (t === "hosts") return { from: e.to, to: e.from, rel: "depends_on", sub: "runs_on", attrs: e.attrs || {} };
  return { from: e.from, to: e.to, rel: "depends_on", sub: e.rel === "depends_on" ? (e.sub || e.type || "depends_on") : (e.sub || t), attrs: e.attrs || {} };
}

// Topology con khi lượt chưa có graph: dựng tạm từ entity (workload chứa pod theo tên, Service chứa pod của workload cùng tên).
function graphFromEntities(entities) {
  const list = entities || [];
  const nodes = list.map((e) => ({ id: e.id, kind: e.kind, name: e.name, status: e.suspect ? "bad" : "nodata", suspect: e.suspect }));
  const edges = [];
  for (const e of list) {
    const ns = e.namespace || "?";
    if (e.kind === "pod") {
      const owner = list.find((o) => WORKLOAD_KINDS.has(o.kind) && (o.namespace || "?") === ns && e.name.startsWith(o.name + "-"));
      if (owner) edges.push({ from: owner.id, to: e.id, type: "contains" });
    }
    if (e.kind === "service") {
      const wl = list.find((o) => WORKLOAD_KINDS.has(o.kind) && (o.namespace || "?") === ns && o.name === e.name);
      if (wl) edges.push({ from: e.id, to: wl.id, type: "routes_to" });
    }
  }
  return { nodes, edges, derived: true };
}

// Cảnh báo khớp một node theo đúng loại nhãn: nhãn service chỉ khớp Service, nhãn deployment chỉ khớp workload, nhãn pod khớp
// workload chứa pod đó, nhãn node khớp máy chủ. Nhãn chung (app, job, instance) khớp mọi loại cùng tên.
const LABEL_KINDS = {
  service: ["service"], service_name: ["service"], deployment: ["deployment", "workload"], statefulset: ["statefulset", "workload"],
  daemonset: ["daemonset", "workload"], pod: ["pod", "instance"], pod_name: ["pod", "instance"], node: ["node", "host"], instance_node: ["node", "host"],
  persistentvolumeclaim: ["pvc"], ingress: ["ingress"],
};
const GENERIC_LABELS = new Set(["app", "app_kubernetes_io_name", "job", "instance", "host", "hostname", "container"]);
function alertsFor(alerts, node) {
  const pods = new Set((node.instances || []).map((i) => i.name));
  return (alerts || []).filter((a) => Object.entries(a.labels || {}).some(([k, v]) => {
    if (LABEL_KINDS[k]) {
      if ((k === "pod" || k === "pod_name") && pods.has(v)) return true;
      return LABEL_KINDS[k].includes(node.kind) && v === node.name;
    }
    return GENERIC_LABELS.has(k) && String(v).split(":")[0] === node.name;
  }));
}

// Gộp instance vào node cha, nâng cạnh depends_on của instance lên node cha, tách máy chủ ra dải riêng, rồi tính
// nghi phạm, đường lan lỗi (từ node có triệu chứng tới nghi phạm) và blast radius (mọi node phụ thuộc vào nghi phạm).
function buildTopology(raw, entities, alerts) {
  const src = raw && (raw.nodes || []).length ? raw : graphFromEntities(entities);
  const all = new Map((src.nodes || []).map((n) => [n.id, { ...n, attrs: n.attrs || {}, instances: [], hosts: new Set() }]));
  const susp = new Set((entities || []).filter((e) => e.suspect).map((e) => e.id));
  for (const n of all.values()) if (susp.has(n.id)) n.suspect = true;
  const edges = (src.edges || []).map(normalizeEdge).filter((e) => all.has(e.from) && all.has(e.to));

  // Node cha của instance: ưu tiên workload; service chứa pod thì thành service → workload (trỏ tới).
  const parent = new Map();
  for (const e of edges) {
    if (e.rel !== "contains" || !INSTANCE_KINDS.has(all.get(e.to).kind)) continue;
    const cur = parent.get(e.to);
    if (!cur || (WORKLOAD_KINDS.has(all.get(e.from).kind) && !WORKLOAD_KINDS.has(all.get(cur).kind))) parent.set(e.to, e.from);
  }
  const lifted = [];
  for (const e of edges) {
    if (e.rel === "contains" && INSTANCE_KINDS.has(all.get(e.to).kind) && parent.get(e.to) !== e.from) {
      lifted.push({ from: e.from, to: parent.get(e.to), rel: "depends_on", sub: "routes_to", attrs: {} });
    }
  }
  const rep = (id) => parent.get(id) || id;
  for (const [child, p] of parent) {
    const c = all.get(child), pn = all.get(p);
    pn.instances.push(c);
    if (c.status === "bad" || c.status === "warn") pn.status = worst(pn.status || "nodata", c.status === "bad" ? "warn" : c.status);
  }
  const isHost = (id) => roleOf(all.get(id).kind) === "host";
  const hosts = [...all.values()].filter((n) => isHost(n.id));
  const visible = new Map([...all.values()].filter((n) => !parent.has(n.id) && !isHost(n.id)).map((n) => [n.id, n]));
  const dep = new Map();
  for (const e of [...edges, ...lifted]) {
    if (e.rel !== "depends_on") continue;
    const a = rep(e.from), b = rep(e.to);
    if (a === b) continue;
    if (isHost(b) && visible.has(a)) { visible.get(a).hosts.add(b); continue; }
    if (!visible.has(a) || !visible.has(b)) continue;
    const key = `${a}>${b}`;
    const cur = dep.get(key);
    if (cur) {
      if (typeof e.attrs.rps === "number") cur.attrs.rps = (cur.attrs.rps || 0) + e.attrs.rps;
      if (typeof e.attrs.error_rate === "number") cur.attrs.error_rate = Math.max(cur.attrs.error_rate || 0, e.attrs.error_rate);
    } else dep.set(key, { from: a, to: b, sub: e.sub, attrs: { ...e.attrs } });
  }
  const depEdges = [...dep.values()];
  for (const n of visible.values()) {
    n.alerts = alertsFor(alerts, n);
    n.status = n.status || "nodata";
    if (n.instances.some((i) => i.suspect)) n.suspect = true;
  }
  const out = new Map([...visible.keys()].map((id) => [id, []]));
  const inn = new Map([...visible.keys()].map((id) => [id, []]));
  for (const e of depEdges) { out.get(e.from).push(e); inn.get(e.to).push(e); }

  // Blast radius: đi ngược cạnh depends_on từ nghi phạm.
  const suspects = [...visible.values()].filter((n) => n.suspect).map((n) => n.id);
  const blast = new Set();
  const queue = [...suspects];
  while (queue.length) {
    const id = queue.shift();
    for (const e of inn.get(id)) if (!blast.has(e.from) && !suspects.includes(e.from)) { blast.add(e.from); queue.push(e.from); }
  }
  // Đường lan lỗi: từ mỗi node có triệu chứng (cảnh báo hoặc bất thường) trong blast radius, đường ngắn nhất xuôi depends_on tới nghi phạm.
  const pathEdges = new Set();
  let chain = [];
  const symptoms = [...blast].filter((id) => visible.get(id).alerts.length || visible.get(id).status === "bad");
  for (const s of symptoms) {
    const prev = new Map([[s, null]]);
    const q = [s];
    let hit = null;
    while (q.length && !hit) {
      const id = q.shift();
      for (const e of out.get(id)) {
        if (prev.has(e.to)) continue;
        prev.set(e.to, e);
        if (suspects.includes(e.to)) { hit = e.to; break; }
        q.push(e.to);
      }
    }
    if (!hit) continue;
    const path = [];
    for (let cur = hit; prev.get(cur); cur = prev.get(cur).from) path.unshift(prev.get(cur));
    path.forEach((e) => pathEdges.add(e));
    if (path.length > (chain.length ? chain.length - 1 : 0) || !chain.length) chain = [s, ...path.map((e) => e.to)].map((id, i) => ({ node: visible.get(id), edge: i > 0 ? path[i - 1] : null }));
  }
  if (!chain.length && suspects.length) chain = [{ node: visible.get(suspects[0]), edge: null }];
  const hostList = hosts.map((hn) => ({ ...hn, runs: [...visible.values()].filter((n) => n.hosts.has(hn.id)).map((n) => n.id), alerts: alertsFor(alerts, hn) }));
  return { nodes: visible, edges: depEdges, hosts: hostList, suspects, blast, pathEdges, chain, out, inn, derived: !!src.derived, all };
}

// Xếp đồ thị theo cột vai trò; trong cột ứng dụng, xuống dòng theo độ sâu chuỗi phụ thuộc; cột dữ liệu và lối vào xếp theo trọng tâm hàng xóm.
function layoutTopology(t, width) {
  const COL = { entry: [0, 0.2], app: [0.2, 0.74], data: [0.74, 1] };
  const RH = 64, NH = 40, TOP = 30;
  const nodes = [...t.nodes.values()];
  const depth = new Map();
  const app = nodes.filter((n) => roleOf(n.kind) === "app");
  const isApp = new Set(app.map((n) => n.id));
  const visit = (id, seen) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0;
    seen.add(id);
    const ups = t.inn.get(id).filter((e) => isApp.has(e.from));
    const d = ups.length ? Math.max(...ups.map((e) => visit(e.from, seen) + 1)) : 0;
    depth.set(id, d);
    return d;
  };
  app.forEach((n) => visit(n.id, new Set()));
  const rows = [];
  for (const n of app) (rows[depth.get(n.id)] ||= []).push(n);
  const pos = new Map();
  const [a0, a1] = COL.app;
  const appRows = [];
  rows.forEach((r) => { for (let i = 0; i < (r || []).length; i += 3) appRows.push(r.slice(i, i + 3)); });
  const nw = Math.min(150, (width * (a1 - a0)) / Math.max(1, ...appRows.map((r) => r.length)) - 14);
  appRows.forEach((r, i) => r.forEach((n, j) => {
    const span = (width * (a1 - a0)) / r.length;
    pos.set(n.id, { x: width * a0 + span * j + span / 2, y: TOP + i * RH + NH / 2, w: nw });
  }));
  const side = (role) => {
    const list = nodes.filter((n) => roleOf(n.kind) === role);
    const bary = (n) => {
      const ys = [...t.inn.get(n.id), ...t.out.get(n.id)].map((e) => pos.get(e.from === n.id ? e.to : e.from)).filter(Boolean).map((p) => p.y);
      return ys.length ? ys.reduce((s, y) => s + y, 0) / ys.length : 9999;
    };
    list.sort((x, y) => bary(x) - bary(y));
    const [c0, c1] = COL[role];
    let lastY = TOP - RH;
    list.forEach((n) => {
      const y = Math.max(bary(n) === 9999 ? lastY + RH : bary(n), lastY + NH + 12);
      pos.set(n.id, { x: width * (c0 + c1) / 2, y, w: Math.min(140, width * (c1 - c0) - 12) });
      lastY = y;
    });
  };
  side("entry");
  side("data");
  const maxY = Math.max(TOP + NH, ...[...pos.values()].map((p) => p.y + NH / 2));
  return { pos, height: maxY + 16, NH };
}

// ---------- các khối hiển thị ----------

function nodeBadges(n) {
  return [
    n.suspect && h("span", { class: "ip-badge bad", title: "nghi là gốc" }, "★ gốc"),
    n.alerts && n.alerts.length > 0 && h("span", { class: "ip-badge bad", title: n.alerts.map((a) => a.alertname).join(", ") }, `🔔 ${n.alerts.length}`),
    (n.attrs.changes || []).length > 0 && h("span", { class: "ip-badge info", title: n.attrs.changes.map((c) => `${hhmm(c.at)} ${c.type}: ${c.detail}`).join("\n") },
      `⟳ ${n.attrs.changes[0].type} ${hhmm(n.attrs.changes[0].at)}`),
    (n.attrs.log_patterns || []).some((p) => p.status === "new" || p.status === "up") && h("span", { class: "ip-badge warn", title: "mẫu log lỗi mới hoặc tăng" },
      `log ${(n.attrs.log_patterns || []).filter((p) => p.status === "new" || p.status === "up").length} mẫu`),
  ];
}

const instanceLine = (n) => {
  if (!n.instances.length) return n.attrs.ready || "";
  const bad = n.instances.filter((i) => i.status === "bad").length;
  return `${n.instances.length - bad}/${n.instances.length} ${kindLabel(n.instances[0].kind)} ổn`;
};

function edgeText(e) {
  if (!e) return "";
  const a = e.attrs || {};
  return [SUB_LABEL[e.sub] || e.sub, typeof a.rps === "number" && `${fmtNum(a.rps)} req/s`, typeof a.error_rate === "number" && `lỗi ${pct(a.error_rate)}`,
    typeof a.p95_ms === "number" && `p95 ${fmtNum(a.p95_ms)} ms`].filter(Boolean).join(" · ");
}

function topSignal(n, signals) {
  const own = (signals || []).filter((s) => s.entity === n.id || n.instances.some((i) => i.id === s.entity)).sort((a, b) => (b.score || 0) - (a.score || 0))[0];
  if (!own) return null;
  return own.rule ? `${own.name}: ${own.rule}` : `${own.name} ${fmtVal(own.now)} (nền ${fmtVal(own.baseline)})`;
}

const topPattern = (n) => (n.attrs.log_patterns || []).find((p) => p.status === "new") || (n.attrs.log_patterns || []).find((p) => p.status === "up");

// Dạng chuỗi cho panel trong chat: đường lan lỗi từ triệu chứng tới nghi phạm, mỗi bước một thẻ nhỏ.
function chainView(t, r, onSelect, onExpand) {
  const steps = t.chain;
  const box = h("div", { class: "ip-chain" });
  if (!steps.length) {
    const bad = [...t.nodes.values()].filter((n) => n.status === "bad" || n.alerts.length).slice(0, 4);
    box.append(h("p", { class: "small muted" }, t.suspects.length ? "Chưa nối được triệu chứng với nghi phạm trên đồ thị." : "Lượt này chưa chỉ ra đối tượng nghi là gốc."),
      ...bad.map((n) => stepCard(n, r, onSelect)));
  } else {
    steps.forEach((s, i) => {
      if (i > 0) box.append(h("div", { class: "ip-chain-edge" + (t.pathEdges.has(s.edge) ? " hot" : "") }, edgeText(steps[i].edge) || "phụ thuộc"));
      box.append(stepCard(s.node, r, onSelect));
    });
  }
  const onPath = new Set(steps.map((s) => s.node.id));
  const dragged = [...t.blast].filter((id) => !onPath.has(id)).map((id) => t.nodes.get(id));
  const hostsUsed = t.hosts.filter((hn) => hn.runs.some((id) => onPath.has(id)));
  return h("div", {}, box,
    dragged.length > 0 && h("div", { class: "small ip-chain-more" }, h("span", { class: "muted" }, "Bị kéo theo: "),
      dragged.map((n, i) => [i > 0 && ", ", h("button", { type: "button", class: "linkish", onclick: () => onSelect(n.id) }, n.name)])),
    hostsUsed.length > 0 && h("div", { class: "small ip-chain-more" }, h("span", { class: "muted" }, "Chạy trên: "),
      hostsUsed.map((hn, i) => [i > 0 && ", ", h("button", { type: "button", class: "linkish", onclick: () => onSelect(hn.id) }, hn.name),
        h("span", { class: "muted" }, ` (${hn.status === "bad" ? "bất thường" : hn.status === "warn" ? "lệch" : hn.status === "ok" ? "ổn" : "chưa rõ"})`)])),
    h("button", { type: "button", class: "ghost small-btn ip-expand", onclick: onExpand }, "Xem đồ thị đầy đủ"),
    t.derived && h("div", { class: "small muted" }, "Dựng tạm từ đối tượng trong lượt điều tra, chưa đọc quan hệ thật."));
}

function stepCard(n, r, onSelect) {
  const sig = topSignal(n, r && r.signals);
  const pat = topPattern(n);
  return h("button", { type: "button", class: `ip-step ${NODE_TONE[n.status] || "nodata"}${n.suspect ? " suspect" : ""}`, onclick: () => onSelect(n.id) },
    h("div", { class: "ip-step-head" }, h("span", { class: "ip-kind" }, kindLabel(n.kind)), h("span", { class: "ip-step-name" }, n.name), h("span", { class: "spacer" }), nodeBadges(n)),
    h("div", { class: "ip-step-sub" }, [n.instances.length ? instanceLine(n) : null, sig || (n.instances.length ? null : n.attrs.ready)].filter(Boolean).join(" · ")),
    pat && h("div", { class: "ip-step-log" }, `“${pat.template}” ×${fmtNum(pat.count)}${pat.status === "new" ? " · mới" : pat.factor ? ` · ×${pat.factor}` : ""}`));
}

// Đồ thị đầy đủ: cột lối vào / ứng dụng / dữ liệu, cạnh trên đường lan lỗi tô đậm, vùng blast radius tô nền, máy chủ là dải dưới cùng.
function graphView(t, opts) {
  const width = opts.width || 760;
  const { pos, height, NH } = layoutTopology(t, width);
  const hostH = t.hosts.length ? 58 : 0;
  const H = height + hostH + 8;
  const hostSel = opts.selected && t.hosts.some((hn) => hn.id === opts.selected) ? t.hosts.find((hn) => hn.id === opts.selected) : null;
  const dim = (id) => hostSel && !hostSel.runs.includes(id);
  const clip = (s, w) => { const n = Math.floor((w - 12) / 6.6); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const blastBox = (() => {
    const ps = [...t.blast, ...t.suspects].map((id) => pos.get(id)).filter(Boolean);
    if (ps.length < 2) return null;
    const x0 = Math.min(...ps.map((p) => p.x - p.w / 2)) - 10, x1 = Math.max(...ps.map((p) => p.x + p.w / 2)) + 10;
    const y0 = Math.min(...ps.map((p) => p.y - NH / 2)) - 10, y1 = Math.max(...ps.map((p) => p.y + NH / 2)) + 18;
    return svgEl("g", {}, svgEl("rect", { class: "ip-blast", x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 10 }),
      svgEl("text", { class: "ip-blast-label", x: x0 + 8, y: y1 - 5 }, `vùng bị ảnh hưởng · ${t.blast.size} đối tượng`));
  })();
  const edgePath = (e) => {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b) return null;
    const sameCol = Math.abs(a.x - b.x) < 4;
    let x1 = a.x, y1 = a.y, x2 = b.x, y2 = b.y;
    if (sameCol || Math.abs(b.y - a.y) > Math.abs(b.x - a.x) * 0.6) { y1 += (b.y > a.y ? 1 : -1) * NH / 2; y2 -= (b.y > a.y ? 1 : -1) * NH / 2; }
    else { x1 += (b.x > a.x ? 1 : -1) * a.w / 2; x2 -= (b.x > a.x ? 1 : -1) * b.w / 2; }
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const d = sameCol || Math.abs(y2 - y1) > Math.abs(x2 - x1) ? `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}` : `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
    const hot = t.pathEdges.has(e);
    const w = typeof e.attrs.rps === "number" ? Math.min(4, 1 + Math.log10(1 + e.attrs.rps) / 1.2) : 1.2;
    return svgEl("path", { class: "ip-edge" + (hot ? " hot" : "") + (dim(e.from) && dim(e.to) ? " dim" : ""), d, "stroke-width": hot ? Math.max(w, 2.5) : w,
      "marker-end": hot ? "url(#ip-arrow-hot)" : "url(#ip-arrow)" }, svgEl("title", {}, `${e.from} → ${e.to}: ${edgeText(e)}`));
  };
  const nodeG = (n) => {
    const p = pos.get(n.id);
    const sel = opts.selected === n.id;
    const meta = [n.instances.length ? instanceLine(n) : null, n.alerts.length ? `🔔${n.alerts.length}` : null, (n.attrs.changes || []).length ? "⟳" : null, n.suspect ? "★" : null]
      .filter(Boolean).join(" ");
    return svgEl("g", { class: `ip-node ${NODE_TONE[n.status] || "nodata"}${n.suspect ? " suspect" : ""}${sel ? " selected" : ""}${t.blast.has(n.id) ? " impacted" : ""}${dim(n.id) ? " dim" : ""}`,
      tabindex: 0, role: "button", "aria-label": `${entityLabel(n)}, ${n.status}${n.suspect ? ", nghi là gốc" : ""}`,
      onclick: () => opts.onSelect(n.id), onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); opts.onSelect(n.id); } } },
      svgEl("rect", { x: p.x - p.w / 2, y: p.y - NH / 2, width: p.w, height: NH, rx: 6 }),
      svgEl("text", { class: "ip-node-kind", x: p.x - p.w / 2 + 7, y: p.y - 5 }, kindLabel(n.kind)),
      meta && svgEl("text", { class: "ip-node-meta", x: p.x + p.w / 2 - 6, y: p.y - 5, "text-anchor": "end" }, meta),
      svgEl("text", { class: "ip-node-name", x: p.x - p.w / 2 + 7, y: p.y + 11 }, clip(n.name, p.w)),
      svgEl("title", {}, [n.id, topSignal(n, opts.signals)].filter(Boolean).join("\n")));
  };
  const hostsG = t.hosts.length > 0 && (() => {
    const y = height + 8, w = Math.min(130, (width - 80) / t.hosts.length - 10);
    return svgEl("g", {}, svgEl("rect", { class: "ip-host-strip", x: 2, y, width: width - 4, height: hostH - 6, rx: 8 }),
      svgEl("text", { class: "ip-node-kind", x: 10, y: y + 16 }, "máy chủ"),
      t.hosts.map((hn, i) => {
        const x = 70 + i * (w + 10);
        return svgEl("g", { class: `ip-node ${NODE_TONE[hn.status] || "nodata"}${opts.selected === hn.id ? " selected" : ""}`, tabindex: 0, role: "button",
          "aria-label": `máy ${hn.name}, ${hn.status}`, onclick: () => opts.onSelect(hn.id) },
          svgEl("rect", { x, y: y + 9, width: w, height: 32, rx: 6 }),
          svgEl("text", { class: "ip-node-name", x: x + 7, y: y + 29 }, clip(hn.name, w)),
          svgEl("title", {}, `${hn.id}: ${hn.runs.length} đối tượng chạy trên máy này`));
      }));
  })();
  const cols = [["lối vào", 0.1], ["ứng dụng", 0.47], ["dữ liệu, bên ngoài", 0.87]];
  return h("div", { class: "ip-topo-wrap" },
    svgEl("svg", { class: "ip-topo", viewBox: `0 0 ${width} ${H}`, width, height: H, role: "group", "aria-label": "Topology của sự cố" },
      svgEl("defs", {},
        svgEl("marker", { id: "ip-arrow", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" }, svgEl("path", { d: "M0,0 L8,4 L0,8 z", class: "ip-arrow" })),
        svgEl("marker", { id: "ip-arrow-hot", viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse" }, svgEl("path", { d: "M0,0 L8,4 L0,8 z", class: "ip-arrow hot" }))),
      cols.map(([label, x]) => svgEl("text", { class: "ip-col-label", x: width * x, y: 14, "text-anchor": "middle" }, label)),
      blastBox,
      t.edges.map(edgePath),
      [...t.nodes.values()].map(nodeG),
      hostsG),
    h("div", { class: "small muted ip-legend" },
      h("span", { class: "ip-dot bad" }), "bất thường ", h("span", { class: "ip-dot warn" }), "lệch ", h("span", { class: "ip-dot ok" }), "bình thường ",
      h("span", { class: "ip-dot nodata" }), "chưa có số liệu · ", h("span", { class: "ip-legend-hot" }), "đường lan lỗi · ★ nghi là gốc · ⟳ vừa thay đổi",
      t.derived ? " · dựng tạm từ entity, chưa đọc quan hệ thật" : ""));
}

function signalRow(s, marks, onEntity) {
  const tone = NODE_TONE[s.status] || "nodata";
  const name = s.entity ? s.entity.split("/").pop() : "";
  return h("div", { class: "ip-signal " + tone },
    h("div", { class: "ip-signal-head" },
      h("span", { class: "ip-dot " + tone, "aria-hidden": "true" }),
      h("button", { type: "button", class: "linkish", onclick: () => onEntity(s.entity) }, name),
      h("span", { class: "muted" }, " · " + s.name),
      h("span", { class: "spacer" }),
      s.link && h("a", { href: s.link, target: "_blank", rel: "noopener", class: "small", title: "Mở trong Grafana" }, "↗")),
    h("div", { class: "ip-signal-body" },
      s.series && s.series.length ? sparkline(s.series, { baseline: s.baseline, marks, tone, label: `${name} ${s.name}` }) : null,
      h("span", { class: "small" }, s.rule ? s.rule : `hiện ${fmtVal(s.now)} · nền ${fmtVal(s.baseline)}`)));
}

// Log của một node: số lỗi theo phút (sparkline) và các mẫu log; mẫu bình thường ẩn sau "hiện thêm".
function logsBlock(n, marks) {
  const lg = n.attrs.logs;
  const pats = n.attrs.log_patterns || [];
  if (!lg && !pats.length) return null;
  const notable = pats.filter((p) => p.status === "new" || p.status === "up");
  const rest = pats.filter((p) => !notable.includes(p));
  const pat = (p) => h("div", { class: "ip-pattern " + (p.status || "") },
    h("div", { class: "ip-pattern-head" },
      h("span", { class: "ip-badge " + (p.status === "new" ? "bad" : p.status === "up" ? "warn" : "") }, p.status === "new" ? "mới" : p.status === "up" ? `×${p.factor || "?"}` : "thường"),
      h("code", {}, p.template), h("span", { class: "spacer" }), h("span", { class: "muted" }, `×${fmtNum(p.count)}`)),
    p.sample && h("div", { class: "ip-pattern-sample" }, p.sample),
    p.first_seen && h("div", { class: "muted" }, `xuất hiện từ ${hhmm(p.first_seen)}`));
  return h("div", { class: "ip-logs" },
    h("div", { class: "ip-signal-head" }, h("strong", {}, "Log"), h("span", { class: "muted" }, lg ? ` · lỗi ${fmtNum(lg.errors)} (nền ~${fmtNum(lg.baseline_per_min)}/phút)` : ""),
      h("span", { class: "spacer" }), lg && lg.link && h("a", { href: lg.link, target: "_blank", rel: "noopener", class: "small" }, "Kibana ↗")),
    lg && lg.series && sparkline(lg.series, { tone: notable.length ? "bad" : "", marks, label: "số dòng lỗi mỗi phút", width: 160 }),
    notable.map(pat),
    rest.length > 0 && h("details", { class: "ip-more" }, h("summary", { class: "small muted" }, `${rest.length} mẫu bình thường`), rest.map(pat)));
}

// Dò tên entity trong một câu bằng chứng để gắn tag bấm được.
function entitiesIn(text, entities) {
  const t = String(text || "").toLowerCase();
  const hit = (entities || []).filter((e) => new RegExp(`(^|[^a-z0-9-])${e.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[^a-z0-9-])`).test(t));
  const names = new Set(hit.map((e) => e.name));
  return hit.filter((e) => ![...names].some((n) => n !== e.name && n.includes(e.name)))
    .sort((a, b) => !!b.suspect - !!a.suspect)
    .filter((e, i, arr) => arr.findIndex((x) => x.name === e.name) === i);
}

function incidentPanel(el, incidentId, opts) {
  opts = { mode: "compact", ...(opts || {}) };
  let data = null;
  let roundNo = null;
  let selected = null;
  let expanded = false;
  let timer = 0;
  let alive = true;
  let topo = null;
  let modalOpen = false;  // để chỉ chuyển focus vào modal lúc vừa mở, không phải mỗi lần tải lại
  el.classList.add("incident-panel", opts.mode);
  const modal = h("div", { class: "ip-modal", hidden: true, role: "dialog", "aria-label": "Đồ thị topology của sự cố" });
  (opts.modalHost || el).append(modal);
  modal.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { expanded = false; paint(); } });

  const marksOf = (d) => [
    ...(d.milestones && d.milestones.first_alert ? [{ at: d.milestones.first_alert, kind: "alert", label: "cảnh báo bắt đầu" }] : []),
    ...(d.actions || []).filter((a) => a.ran_at).map((a) => ({ at: a.ran_at, kind: "action", label: `chạy: ${a.result_line || a.tool}` })),
  ];

  async function reload() {
    if (!alive) return;
    try {
      data = await api("GET", `/api/incidents/${incidentId}/panel` + (roundNo ? `?round=${roundNo}` : ""));
      topo = data.round ? buildTopology(data.round.graph, data.round.entities, data.alerts) : null;
      paint();
    } catch (e) {
      el.replaceChildren(h("div", { class: "small muted pad" }, "Không tải được panel: " + e.message));
    }
    schedule();
  }
  function schedule() {
    clearTimeout(timer);
    if (alive && data && data.status === "open") timer = setTimeout(() => { if (document.visibilityState === "visible") reload(); else schedule(); }, 30000);
  }
  const onVisible = () => { if (!el.isConnected) return destroy(); if (document.visibilityState === "visible") reload(); };
  document.addEventListener("visibilitychange", onVisible);
  function destroy() { alive = false; clearTimeout(timer); modal.remove(); document.removeEventListener("visibilitychange", onVisible); }

  const select = (id) => { selected = selected === id ? null : id; paint(); };

  function head(d) {
    const tone = (typeof STATE_TONE !== "undefined" && STATE_TONE[d.state.key]) || "";
    const firing = (d.alerts || []).filter((a) => a.status === "firing").length;
    const busyNow = d.investigating || d.stage === "investigating";
    return h("div", { class: "ip-head" },
      h("div", { class: "ip-chips" },
        h("span", { class: "badge " + tone }, d.state.label),
        d.severity && h("span", { class: "badge" }, d.severity),
        h("span", { class: "badge" }, d.env),
        d.repeats > 0 && h("span", { class: "badge warn" }, `lặp lại ${d.repeats} lần`)),
      opts.mode === "full" ? null : h("div", { class: "ip-title-line", title: d.title }, d.title),
      h("div", { class: "small muted" },
        busyNow ? busy("agent đang điều tra…")
          : [d.status === "open" ? `mở ${ago(d.milestones.first_alert || d.created_at)}` : d.resolved_at ? `đã hết lúc ${fmtTime(d.resolved_at)}` : "đã đóng",
            ` · ${firing}/${(d.alerts || []).length} cảnh báo đang bắn`]),
      h("div", { class: "ip-actions" },
        d.status === "open" && !busyNow && h("button", { type: "button", class: "ghost small-btn", onclick: guard(async () => {
          await api("POST", `/api/incidents/${incidentId}/investigate`); toast("Đã bắt đầu điều tra lại"); await reload();
        }) }, "Điều tra lại"),
        opts.mode === "compact"
          ? h("button", { type: "button", class: "ghost small-btn", onclick: () => go("incidents", undefined, incidentId) }, "Mở trang sự cố ↗")
          : d.conversation_id && h("button", { type: "button", class: "ghost small-btn", onclick: () => go("chat", d.conversation_id) }, "Mở hội thoại điều tra ↗")));
  }

  function conclusion(d, r) {
    if (!r) {
      return panelBlock("conclusion", "Kết luận", null, h("p", { class: "small muted" },
        d.investigating || d.stage === "investigating" ? "Đang điều tra lượt đầu…" : "Chưa có lượt điều tra nào."));
    }
    const ents = r.entities || [];
    const rep = r.report || {};
    const meta = `lượt ${r.round}${r.created_at ? " · " + hhmm(r.created_at) : ""} · ${r.steps || 0} lệnh đọc` + (r.synthesized ? " · dựng lại từ dữ liệu cũ" : "");
    const roundSel = (d.rounds || []).length > 1 && h("select", { class: "ip-round", "aria-label": "Chọn lượt điều tra", onchange: (e) => { roundNo = +e.target.value || null; reload(); } },
      [...d.rounds].reverse().map((x, i) => h("option", { value: i === 0 ? "" : x.round, selected: (roundNo || d.rounds[d.rounds.length - 1].round) === x.round },
        `Lượt ${x.round} · ${hhmm(x.created_at)}${i === 0 ? " (mới nhất)" : ""}`)));
    const chip = (e) => h("button", { type: "button", class: "chip ip-ent" + (e.suspect ? " suspect" : "") + (selected === e.id ? " on" : ""), title: e.id, onclick: () => select(e.id) }, entityLabel(e));
    const hyps = Array.isArray(rep.hypotheses) && rep.hypotheses.length ? rep.hypotheses : null;
    const body = [
      roundSel,
      hyps
        ? h("ol", { class: "ip-hyps" }, hyps.slice(0, 3).map((x) => h("li", {},
          h("div", { class: "ip-hyp-head" }, h("strong", {}, x.entity ? x.entity.split("/").pop() : "?"), h("span", { class: "muted small" }, " · " + (x.category === "unknown" ? "chưa xác định" : x.category || "")),
            h("span", { class: "spacer" }), confBar(x.confidence)),
          x.mechanism && h("div", { class: "small" }, x.mechanism))))
        : h("div", {},
          h("p", { class: "ip-cause" }, rep.root_cause || "Chưa rõ nguyên nhân."),
          h("div", { class: "ip-conf-line small muted" }, "Độ tin ", confBar(rep.confidence))),
      (rep.evidence || []).length > 0 && h("details", { class: "ip-evidence", open: opts.mode === "full" ? "" : null },
        h("summary", { class: "small" }, `Bằng chứng (${rep.evidence.length})`),
        h("ul", {}, rep.evidence.map((ev) => {
          const text = typeof ev === "string" ? ev : ev.text;
          return h("li", { class: "small" }, text, " ", entitiesIn(text, ents).map(chip));
        }))),
      ents.length > 0 && h("details", { class: "ip-entities" }, h("summary", { class: "small" }, `Đối tượng liên quan (${ents.length})`),
        h("div", { class: "ip-row wrap" }, ents.map(chip))),
    ];
    return panelBlock("conclusion", "Kết luận", meta, body);
  }

  function topology(d, r) {
    if (!topo || !topo.nodes.size) return null;
    const meta = topo.suspects.length ? `${topo.blast.size} đối tượng bị ảnh hưởng` : `${topo.nodes.size} đối tượng`;
    if (opts.mode === "compact") {
      return panelBlock("topology", "Đường lan lỗi", meta, chainView(topo, r, select, () => { expanded = true; paint(); }));
    }
    return panelBlock("topology", "Topology", meta, graphView(topo, { width: 760, selected, onSelect: select, signals: r.signals }));
  }

  function signals(d, r) {
    let list = [...((r && r.signals) || [])];
    if (!list.length) return null;
    if (selected) list = list.filter((s) => s.entity === selected || (topo && topo.nodes.get(selected) && topo.nodes.get(selected).instances.some((i) => i.id === s.entity)));
    const susp = new Set(((r && r.entities) || []).filter((e) => e.suspect).map((e) => e.id));
    list.sort((a, b) => (susp.has(b.entity) - susp.has(a.entity)) || ((b.score || 0) - (a.score || 0)));
    if (!list.length) return null;
    const max = opts.mode === "compact" && !selected ? 4 : list.length;
    return panelBlock("signals", "Số liệu", selected ? "lọc theo đối tượng đang chọn" : "lúc điều tra",
      [list.slice(0, max).map((s) => signalRow(s, marksOf(d), select)),
        list.length > max && h("div", { class: "small muted" }, `và ${list.length - max} tín hiệu khác ở trang sự cố`)]);
  }

  function waitingBlock(d) {
    const items = d.waiting || [];
    const verifying = d.stage === "verifying" && d.verify_due;
    if (!items.length && !verifying) return null;
    const rows = items.map((w) => {
      if (w.kind === "approval") {
        // Giống thẻ duyệt trong chat: lý do làm dòng chính, lệnh một dòng (đủ khi rê chuột), để không tưởng là hai đề xuất khác nhau.
        return h("div", { class: "ip-wait warn" }, h("span", {}, "⏳ "), h("div", { class: "ip-wait-text" }, h("strong", {}, w.why || w.label),
          w.why && h("div", { class: "ip-wait-cmd", title: w.label }, w.label),
          h("div", { class: "ip-wait-foot" }, h("span", { class: "small muted" }, w.expires_at ? `hết hạn ${hhmm(w.expires_at)}` : ""),
            h("button", { type: "button", class: "small-btn", onclick: () => (opts.onShowApproval ? opts.onShowApproval(w) : d.conversation_id && go("chat", d.conversation_id)) },
              opts.onShowApproval ? "Tới thẻ duyệt ↓" : "Xem"))));
      }
      if (w.kind === "task") {
        return h("label", { class: "ip-wait" }, h("input", { type: "checkbox", onchange: guard(async () => {
          await api("POST", `/api/incidents/${incidentId}/tasks/${w.task_id}/done`, { done: true }); await reload();
        }) }), h("span", { class: "ip-wait-text" }, w.text));
      }
      if (w.kind === "retry") {
        return h("div", { class: "ip-wait bad" }, h("span", {}, "↺ "), h("div", { class: "ip-wait-text" }, h("strong", {}, w.label),
          h("div", { class: "small muted" }, [w.status, w.deny_reason || w.result_line].filter(Boolean).join(" · "))));
      }
      return h("div", { class: "ip-wait " + (w.kind === "error" ? "bad" : "warn") }, h("span", { class: "ip-wait-text small" }, w.text));
    });
    if (verifying) rows.push(h("div", { class: "ip-wait info" }, h("span", { class: "ip-wait-text small" }, `Đang chờ xác minh tới ${hhmm(d.verify_due)}`)));
    return panelBlock("waiting", "Đang chờ", `${rows.length} việc`, rows, { cls: "ip-waiting" });
  }

  function relatedBlock(d) {
    const rel = d.related || {};
    const items = [...(rel.open || []), ...(rel.history || [])];
    if (opts.mode === "compact") {
      return items.length ? h("div", { class: "small muted ip-related" }, `${items.length} sự cố liên quan · `,
        h("button", { type: "button", class: "linkish", onclick: () => go("incidents", undefined, incidentId) }, "xem ở trang sự cố")) : null;
    }
    if (!items.length) return null;
    return panelBlock("related", "Liên quan", `${rel.days || 30} ngày`, h("ul", { class: "ip-related-list" }, items.map((x) => h("li", { class: "small" },
      h("button", { type: "button", class: "linkish", onclick: () => go("incidents", undefined, x.id) }, x.title), " ",
      h("span", { class: "muted" }, [x.status, x.count ? `${x.count} lần` : null, x.reason].filter(Boolean).join(" · "))))));
  }

  function nodeCard(d, r, id) {
    const n = topo && (topo.nodes.get(id) || topo.hosts.find((x) => x.id === id) || topo.all.get(id));
    if (!n) return null;
    const ents = (r && r.entities) || [];
    const ent = ents.find((e) => e.id === id) || {};
    const [ns, kind, name] = id.split("/");
    const attrs = n.attrs || {};
    const ids = new Set([id, ...(n.instances || []).map((i) => i.id)]);
    const sigs = ((r && r.signals) || []).filter((s) => ids.has(s.entity));
    const alerts = n.alerts || alertsFor(d.alerts, { name, instances: [] });
    const outE = topo.out.get(id) || [], inE = topo.inn.get(id) || [];
    const link = (nid) => h("button", { type: "button", class: "chip", onclick: () => select(nid) }, (topo.all.get(nid) || {}).name || nid);
    const row = (label, ...val) => h("div", { class: "ip-kv" }, h("span", { class: "muted" }, label), h("span", {}, ...val));
    const status = n.status || (ent.suspect ? "bad" : "nodata");
    const isHost = topo.hosts.some((x) => x.id === id);
    return h("div", { class: "ip-node-card", role: "region", "aria-label": `Thông tin ${id}` },
      h("div", { class: "ip-node-head" }, h("span", { class: "ip-dot " + (NODE_TONE[status] || "nodata") }), h("span", { class: "ip-kind" }, kindLabel(kind)), h("strong", {}, name),
        h("span", { class: "spacer" }), iconButton("close", "Đóng", () => select(id))),
      h("div", { class: "small muted" }, ns !== "-" ? `namespace ${ns}` : isHost ? "máy chủ" : "", n.suspect ? " · nghi là gốc" : "",
        topo.blast.has(id) ? " · bị ảnh hưởng bởi nghi phạm" : "",
        ent.from ? ` · thấy trong ${ent.from.map((f) => (f === "alert" ? "cảnh báo" : "lệnh đọc")).join(", ")}` : ""),
      attrs.ready !== undefined && row("Trạng thái", `${attrs.ready}`, attrs.restarts !== undefined ? ` · restart ${attrs.restarts}` : "", attrs.waiting_reason ? ` · ${attrs.waiting_reason}` : ""),
      (n.instances || []).length > 0 && row("Instance", h("div", { class: "ip-rel" }, n.instances.map((i) => h("div", {},
        h("span", { class: "ip-dot " + (NODE_TONE[i.status] || "nodata") }), " ", h("code", {}, i.name),
        i.attrs && i.attrs.host ? h("span", { class: "muted" }, ` · ${i.attrs.host}`) : "")))),
      attrs.image && row("Image", h("code", {}, attrs.image)),
      n.hosts && n.hosts.size > 0 && row("Chạy trên", h("div", { class: "ip-row wrap" }, [...n.hosts].map(link))),
      isHost && n.runs && n.runs.length > 0 && row("Đang chạy", h("div", { class: "ip-row wrap" }, n.runs.map(link))),
      (attrs.changes || []).length > 0 && row("Thay đổi gần", h("div", {}, attrs.changes.map((c) => h("div", {}, `${hhmm(c.at)} ${c.type}: ${c.detail}`)))),
      (attrs.events || []).length > 0 && row("Sự kiện K8s", attrs.events.map((e) => `${e.reason} ×${e.count}`).join(" · ")),
      alerts.length > 0 && row("Cảnh báo", h("div", {}, alerts.map((a) => h("div", {}, a.alertname, " ", h("span", { class: "muted" }, a.status === "firing" ? "(đang bắn)" : "(đã hết)"))))),
      (outE.length > 0 || inE.length > 0) && row("Phụ thuộc", h("div", { class: "ip-rel" },
        outE.map((e) => h("div", {}, h("span", { class: "muted small" }, `${edgeText(e)} → `), link(e.to))),
        inE.map((e) => h("div", {}, link(e.from), h("span", { class: "muted small" }, ` ${edgeText(e)} → đối tượng này`))))),
      logsBlock(n, marksOf(d)),
      sigs.length > 0 && h("div", { class: "ip-node-sigs" }, sigs.map((s) => signalRow(s, marksOf(d), () => {}))),
      !attrs.ready && !attrs.image && !sigs.length && !attrs.logs && h("p", { class: "small muted" }, "Lượt này chưa đọc thuộc tính, số liệu và log của đối tượng; hỏi agent để kiểm tra kỹ."),
      h("div", { class: "ip-actions" },
        attrs.links && attrs.links.logs && h("a", { href: attrs.links.logs, target: "_blank", rel: "noopener", class: "small" }, "Log ↗"),
        attrs.links && attrs.links.grafana && h("a", { href: attrs.links.grafana, target: "_blank", rel: "noopener", class: "small" }, "Grafana ↗"),
        opts.onAsk && h("button", { type: "button", class: "ghost small-btn", onclick: () => opts.onAsk(`Kiểm tra kỹ ${kind} ${name}${ns !== "-" ? ` trong namespace ${ns}` : ""}: `) }, "Hỏi agent về đối tượng này")));
  }

  function paintModal(d, r) {
    modal.hidden = !expanded || !topo;
    if (modal.hidden) { modal.replaceChildren(); modalOpen = false; return; }
    const card = selected && nodeCard(d, r, selected);
    modal.replaceChildren();
    appendKids(modal, [
      h("div", { class: "ip-modal-head" }, h("strong", {}, "Topology của sự cố"), h("span", { class: "small muted" }, ` · lượt ${r.round}`), h("span", { class: "spacer" }),
        iconButton("close", "Đóng", () => { expanded = false; paint(); })),
      h("div", { class: "ip-modal-body" }, h("div", { class: "ip-modal-graph" }, graphView(topo, { width: 760, selected, onSelect: select, signals: r.signals })),
        card && h("div", { class: "ip-modal-card" }, card)),
    ]);
    if (!modalOpen) { modal.tabIndex = -1; modal.focus({ preventScroll: true }); modalOpen = true; }
  }

  function paint() {
    if (!data) return;
    const d = data, r = d.round;
    const scrollTop = el.scrollTop;
    const summary = opts.summary || (d.summary && typeof summaryCard === "function" ? summaryCard(d.summary, incidentId) : null);
    el.replaceChildren();
    const card = selected && !expanded && nodeCard(d, r, selected);
    if (opts.mode === "compact" && card) appendKids(el, [head(d), card, topology(d, r), signals(d, r)]);
    else if (opts.mode === "full") appendKids(el, [head(d), summary, conclusion(d, r), topology(d, r), selected && nodeCard(d, r, selected), signals(d, r), waitingBlock(d), relatedBlock(d)]);
    else appendKids(el, [head(d), summary, conclusion(d, r), topology(d, r), signals(d, r), waitingBlock(d), relatedBlock(d)]);
    el.scrollTop = scrollTop;
    paintModal(d, r);
  }

  reload();
  return { reload, destroy, get data() { return data; } };
}

window.incidentPanel = incidentPanel;
window.incidentTopology = buildTopology;  // để kiểm thử và dùng lại ở tab Bản đồ sau này
})();
