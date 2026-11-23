"use strict";
// Giao diện một trang. Mọi nội dung động đi qua textContent để tránh XSS.
const state = { me: null, tenant: null, tab: "chat", conversationId: null, env: "dev" };
const root = document.getElementById("app");

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v);
  }
  appendKids(el, kids);
  return el;
}

// Cùng quy tắc cho h() và mọi view: làm phẳng mảng, bỏ null/undefined/false. Element.append gốc không làm vậy
// nên mảng phần tử thành chữ "[object HTMLDivElement],…" và `cond && h(...)` thành chữ "false".
function appendKids(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    Element.prototype.append.call(el, kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

// Thay toàn bộ con của el theo cùng quy tắc với h(); replaceChildren gốc biến `cond && …` thành chữ "false".
function setKids(el, ...kids) {
  el.replaceChildren();
  appendKids(el, kids);
  return el;
}

// Như setKids nhưng thêm vào cuối; nhận cả DocumentFragment và SVG nên không gọi qua Element.prototype.
function addKids(el, ...kids) {
  el.append(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
  return el;
}

async function api(method, path, body) {
  const headers = {};
  if (state.tenant) headers["X-Tenant-Id"] = state.tenant.id;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" });
  const data = await res.json().catch(() => null);
  if (res.status === 401 && path !== "/api/auth/login") { state.me = null; renderLogin(); throw new Error("Phiên đã hết hạn"); }
  if (!res.ok) {
    const detail = data && data.detail;
    const err = new Error(typeof detail === "string" ? detail : detail && detail.message ? detail.message : `Lỗi ${res.status}`);
    err.status = res.status;  // để nơi gọi phân biệt "không còn" (404) với lỗi tạm thời
    if (detail && typeof detail === "object" && !Array.isArray(detail)) err.data = detail;
    throw err;
  }
  return data;
}

function toast(msg, isErr) {
  const el = h("div", { class: "toast" + (isErr ? " err" : ""), role: "status" }, msg);
  addKids(document.body, el);
  setTimeout(() => el.remove(), isErr ? 7000 : 3000);
}

const guard = (fn) => async (ev) => {
  const btn = ev && ev.currentTarget instanceof HTMLButtonElement ? ev.currentTarget : null;
  if (btn) btn.disabled = true;
  try { await fn(ev); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
};

// Dòng "đang thực hiện": biểu tượng xoay nhỏ đầu dòng kèm nội dung. role=status để trình đọc màn hình đọc khi thay đổi.
function busy(text) {
  return h("span", { class: "busy", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), text);
}

const fmtTime = (s) => (s ? new Date(s).toLocaleString("vi-VN") : "—");
const CLASS = { proposed: "warn", disabled: "bad", success: "ok", ok: "ok", active: "ok", auto: "ok", approved: "ok", published: "ok", failure: "bad", error: "bad", blocked: "bad", denied: "bad", rejected: "bad", deprecated: "bad", invalid: "bad", pending: "warn", candidate: "warn", pending_review: "warn", partial: "warn", expired: "warn", skipped: "warn" };
const LABEL = { success: "thành công", failure: "thất bại", partial: "một phần", unknown: "chưa rõ", ok: "ok", error: "lỗi", blocked: "bị chặn", pending: "chờ duyệt", auto: "tự chạy", approved: "đã duyệt", denied: "từ chối", expired: "hết hạn", skipped: "bỏ qua", invalid: "sai tham số", candidate: "ứng viên", active: "hiệu lực", deprecated: "hết hiệu lực", rejected: "bị loại", pending_review: "chờ duyệt", proposed: "đề xuất", disabled: "đã tắt", published: "đã publish", noop: "không đổi", stale: "đã cũ", reverted: "đã hoàn tác" };
const badge = (v) => h("span", { class: "badge " + (CLASS[v] || "") }, LABEL[v] || v);
const isAdmin = () => state.me.is_org_admin || state.tenant.roles.includes("tenant_admin");

function renderLogin() {
  state.tenant = null;
  const user = h("input", { placeholder: "Tên đăng nhập", autocomplete: "username", "aria-label": "Tên đăng nhập" });
  const pass = h("input", { type: "password", placeholder: "Mật khẩu", autocomplete: "current-password", "aria-label": "Mật khẩu" });
  const submit = guard(async (ev) => {
    ev.preventDefault();
    await api("POST", "/api/auth/login", { username: user.value, password: pass.value });
    await boot();
  });
  setKids(root, h("form", { class: "card login", onsubmit: submit }, h("h2", {}, "Knowledge Agent"), h("p", { class: "muted" }, "Agent tự tích lũy tri thức"), user, pass, h("button", { type: "submit" }, "Đăng nhập")));
  user.focus();
}

const TABS = [["incidents", "Sự cố"], ["knowledge", "Tri thức"], ["tools", "Công cụ"], ["approvals", "Duyệt lệnh"], ["episodes", "Nhật ký"], ["metrics", "Đo lường"], ["audit", "Audit"], ["settings", "Cài đặt"], ["account", "Tài khoản"]];
// Link và localStorage cũ còn trỏ tới các mục đã gộp.
const OLD_TABS = { connections: ["tools", "connections"], compile: ["knowledge", "compiled"], policy: ["settings", "policy"] };
const VIEWS = {};
let conversationList = null;

function closeSidebar() { document.querySelector(".sidebar")?.classList.remove("open"); document.querySelector(".scrim")?.classList.remove("open"); }
// Vị trí hiện tại nằm trên URL (#/t/<tenant>/<màn>[/<hội thoại>]) để refresh, Back/Forward và link mở đúng chỗ.
function routeHash() {
  const extra = state.tab === "chat" ? state.conversationId : state.sub;
  return `#/t/${state.tenant.id}/${state.tab}${extra ? "/" + extra : ""}`;
}
function syncUrl(replace) {
  if (!state.tenant || location.hash === routeHash()) return;
  history[replace ? "replaceState" : "pushState"](null, "", routeHash());
}
function applyHash() {
  let [, t, tenantId, tab, extra] = location.hash.split("/");
  if (t !== "t") return;
  const tenant = state.me.tenants.find((x) => x.id === tenantId);
  if (tenant) state.tenant = tenant;
  if (OLD_TABS[tab]) [tab, extra] = OLD_TABS[tab];
  state.tab = tab === "chat" || TABS.some(([id]) => id === tab) ? tab : "chat";
  state.conversationId = state.tab === "chat" && extra ? extra : null;
  state.sub = state.tab !== "chat" && extra ? extra : null;
}
window.addEventListener("popstate", () => { if (state.me) { applyHash(); renderShell(); } });
function go(tab, conversationId, sub) {
  state.tab = tab;
  state.sub = sub || null;
  if (conversationId !== undefined) state.conversationId = conversationId;
  syncUrl();
  renderShell();
}

// Thanh tab con trong một mục (vd Tri thức: Đồ thị / Tri thức / Runbook). Tab con nằm trên URL nên refresh giữ nguyên.
async function subTabs(main, tabs) {
  const key = tabs.some(([k]) => k === state.sub) ? state.sub : tabs[0][0];
  const body = h("div");
  addKids(main, h("div", { class: "subtabs", role: "tablist" }, tabs.map(([k, label]) =>
    h("button", { type: "button", role: "tab", class: "subtab" + (k === key ? " active" : ""), "aria-selected": k === key ? "true" : "false",
      onclick: () => { if (k !== key) go(state.tab, undefined, k); } }, label))), body);
  body.append = (...kids) => appendKids(body, kids);
  await tabs.find(([k]) => k === key)[2](body);
}

// Trạng thái từng màn (nội dung đang gõ, bộ lọc, job đang chạy) lưu trong trình duyệt theo người dùng và tenant.
// Không bao giờ lưu key, mật khẩu hay secret. localStorage có thể bị chặn (cửa sổ ẩn danh) nên mọi truy cập đều bọc try.
const store = {
  key(k) { return `kagent:${state.me ? state.me.username : "-"}:${state.tenant ? state.tenant.id : "-"}:${k}`; },
  get(k, fallback) { try { const v = localStorage.getItem(this.key(k)); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(k, v) { try { if (v === null || v === undefined || v === "") localStorage.removeItem(this.key(k)); else localStorage.setItem(this.key(k), JSON.stringify(v)); } catch {} },
  clear(prefix) {
    try { const p = this.key(prefix); for (const k of Object.keys(localStorage)) if (k.startsWith(p)) localStorage.removeItem(k); } catch {}
  },
};
// Gắn giá trị một ô nhập với store: khôi phục ngay, lưu mỗi lần đổi. Ô mật khẩu không bao giờ được lưu.
function persist(el, k) {
  if (el.type === "password") return el;
  const prop = el.type === "checkbox" ? "checked" : "value";
  const saved = store.get(k, undefined);
  if (saved !== undefined) el[prop] = saved;
  const save = () => store.set(k, el.type === "checkbox" ? el.checked : el.value);
  el.addEventListener("input", save);
  el.addEventListener("change", save);
  return el;
}

function dateGroup(iso) {
  const days = (Date.now() - new Date(iso).getTime()) / 86400000;
  if (new Date(iso).toDateString() === new Date().toDateString()) return "Hôm nay";
  return days < 7 ? "7 ngày qua" : days < 30 ? "30 ngày qua" : "Cũ hơn";
}

function conversationItem(c) {
  const rename = guard(async () => {
    const input = h("input", { class: "rename", value: c.title, "aria-label": "Tên hội thoại" });
    const save = async () => { const title = input.value.trim(); if (title && title !== c.title) await api("PATCH", `/api/conversations/${c.id}`, { title }); refreshConversations(); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); if (e.key === "Escape") { input.value = c.title; input.blur(); } });
    input.addEventListener("blur", () => save().catch((e) => toast(e.message, true)));
    setKids(item, input);
    input.select();
  });
  const remove = guard(async () => {
    await api("DELETE", `/api/conversations/${c.id}`);
    toast("Đã xóa hội thoại khỏi danh sách");
    if (state.conversationId === c.id) go("chat", null); else refreshConversations();
  });
  const item = h("div", { class: "conv" + (state.tab === "chat" && state.conversationId === c.id ? " active" : "") },
    h("button", { type: "button", class: "conv-open", title: c.pending ? `${c.title} · ${c.pending} lệnh chờ duyệt` : c.title, onclick: () => { closeSidebar(); go("chat", c.id); } },
      c.pending ? h("span", { class: "dot-wait", "aria-label": `${c.pending} lệnh chờ duyệt` }) : null, c.title),
    iconButton("edit", "Đổi tên", rename), iconButton("trash", "Xóa", remove));
  return item;
}

async function refreshConversations() {
  if (!conversationList) return;
  const list = await api("GET", "/api/conversations");
  const waiting = list.reduce((n, c) => n + (c.pending || 0), 0);
  document.title = (waiting ? `(${waiting}) ` : "") + "Knowledge Agent";
  const groups = new Map();
  for (const c of list) { const g = dateGroup(c.updated_at); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
  setKids(conversationList, ...(list.length ? [...groups].flatMap(([g, items]) => [h("div", { class: "group" }, g), ...items.map(conversationItem)]) : [h("div", { class: "small pad" }, "Chưa có hội thoại nào.")]));
}

function renderShell() {
  const tenants = h("select", { "aria-label": "Tenant", onchange: (e) => { state.tenant = state.me.tenants.find((t) => t.id === e.target.value); state.env = store.get("env", state.env); go("chat", null); } },
    state.me.tenants.map((t) => h("option", { value: t.id, selected: t.id === state.tenant.id }, t.name)));
  const logout = guard(async () => { await api("POST", "/api/auth/logout"); state.me = null; state.conversationId = null; state.tab = "chat"; history.replaceState(null, "", location.pathname); renderLogin(); });
  conversationList = h("div", { class: "conv-list" });
  const pages = TABS;
  const adminOpen = pages.some(([id]) => id === state.tab);
  const sidebar = h("aside", { class: "sidebar" },
    h("div", { class: "brand" }, h("strong", {}, "Knowledge Agent"), tenants),
    h("button", { type: "button", class: "new-chat", onclick: () => { closeSidebar(); go("chat", null); } }, icon("plus"), "Hội thoại mới"),
    conversationList,
    h("details", { class: "admin", open: adminOpen ? "" : null }, h("summary", {}, icon("chevron"), "Quản trị"),
      pages.map(([id, label]) => h("button", { type: "button", class: "nav-item" + (state.tab === id ? " active" : ""), onclick: () => { closeSidebar(); go(id); } }, label))),
    h("div", { class: "user" }, h("span", {}, state.me.username), h("button", { type: "button", class: "ghost small-btn", onclick: logout }, "Đăng xuất")));
  const scrim = h("div", { class: "scrim", onclick: closeSidebar });
  const model = { anthropic: "Claude", openai: "OpenAI" }[state.me.llm_provider] || "Heuristic";
  const title = state.tab === "chat" ? "" : (TABS.find(([id]) => id === state.tab) || [])[1];
  const top = h("header", { class: "top" }, iconButton("menu", "Mở danh sách", () => { sidebar.classList.add("open"); scrim.classList.add("open"); }, "menu-btn"), h("span", { class: "top-title" }, title), h("span", { class: "spacer" }), h("span", { class: "badge info" }, "Model: " + model));
  const main = h("main", { class: state.tab === "chat" ? "chat" : "page" });
  main.append = (...kids) => appendKids(main, kids);
  setKids(root, h("div", { class: "layout" }, scrim, sidebar, h("div", { class: "content" }, top, main)));
  refreshConversations().catch((e) => toast(e.message, true));
  VIEWS[state.tab](main).catch((e) => toast(e.message, true));
}

function traceView(trace) {
  return trace.filter((s) => s.type === "tool_call" || s.type === "error").map((s) => {
    if (s.type === "error") return h("div", { class: "step bad" }, h("b", {}, "Lỗi: "), s.message);
    const cls = s.status === "ok" ? "ok" : s.status === "pending" ? "pending" : "bad";
    return h("div", { class: "step " + cls },
      h("div", {}, h("b", {}, s.tool), " ", s.risk_tier && h("span", { class: "badge" }, s.risk_tier), " ", h("span", { class: "badge" }, s.env), " ", badge(s.decision), " ", badge(s.status),
        s.untrusted && h("span", { class: "badge warn" }, "untrusted")),
      s.reasons && s.reasons.length > 0 && h("div", { class: "small" }, s.reasons.join("; ")),
      h("pre", {}, JSON.stringify(s.args)), s.result && h("pre", {}, JSON.stringify(s.result, null, 1)));
  });
}

function feedbackBox(ep, onDone) {
  const text = h("textarea", { class: "hidden", value: ep.output || "", "aria-label": "Bản đã sửa" });
  const send = (outcome, final) => guard(async () => { await api("POST", `/api/episodes/${ep.id}/feedback`, { outcome, final_text: final ? text.value : null }); toast("Đã ghi phản hồi"); if (onDone) onDone(); });
  const save = h("button", { class: "hidden", onclick: send("partial", true) }, "Lưu bản sửa");
  return h("div", {}, h("div", { class: "row" }, h("span", { class: "small" }, "Đánh giá:"),
    h("button", { class: "ghost", onclick: send("success", false) }, "Tốt"),
    h("button", { class: "ghost", onclick: send("failure", false) }, "Sai"),
    h("button", { class: "ghost", onclick: () => { text.classList.remove("hidden"); save.classList.remove("hidden"); } }, "Sửa đầu ra")), text, save);
}

function episodeCard(ep, onDone) {
  return h("div", { class: "card" }, h("div", { class: "row spread" }, h("h3", {}, ep.input.task), h("div", {}, badge(ep.outcome), h("span", { class: "badge" }, ep.task_type || "?"), h("span", { class: "badge" }, ep.env))),
    h("pre", {}, ep.output || ""), traceView(ep.trace),
    h("div", { class: "small" }, `KU đã nạp: ${ep.ku_used.length} · Token quy đổi: ${ep.tokens}`), feedbackBox(ep, onDone));
}


// ---------- Sự cố: cảnh báo được gom, điều tra, xử lý tự động ----------
const SEV_CLASS = { critical: "bad", error: "bad", high: "bad", warning: "warn", medium: "warn" };
const STAGE_CLASS = { handled: "ok", verifying: "info", investigating: "info", awaiting_approval: "warn", reported: "warn", needs_human: "bad", error: "bad", cleared: "ok" };
const STEP_STATUS = { ok: "đã chạy", dry_run_only: "chỉ chạy thử", pending: "chờ duyệt", approved: "đã duyệt", denied: "bị từ chối", expired: "hết hạn duyệt", skipped: "bị bỏ qua", blocked: "bị chặn", error: "lỗi", invalid: "sai tham số" };
function ago(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 60 ? `${Math.round(s)} giây trước` : s < 3600 ? `${Math.round(s / 60)} phút trước` : s < 86400 ? `${Math.round(s / 3600)} giờ trước` : fmtTime(iso);
}
function sevBadge(sev) { return h("span", { class: "badge " + (SEV_CLASS[sev] || ""), title: "Mức của cảnh báo" }, sev || "không rõ"); }
const S_CLASS = { S1: "bad", S2: "bad", S3: "warn", S4: "" };
// Mức sự cố S1–S4 theo ảnh hưởng khách hàng (incidents/impact.py); chưa có người đánh giá thì là gợi ý.
function sBadge(x) { return x && h("span", { class: "badge " + (x.source === "human" ? S_CLASS[x.sev] : ""), title: x.source === "human" ? "Mức sự cố, người đã đánh giá" : "Mức gợi ý, chưa đánh giá ảnh hưởng" }, x.source === "human" ? x.sev : `${x.sev}?`); }
function actionText(a) { return a.args && a.args.command ? `${a.tool.split(".").pop()} ${a.args.command}` : `${a.tool} ${JSON.stringify(a.args || {})}`; }
// Tự làm mới khi còn đứng ở màn này; mỗi lần render tạo token mới nên vòng cũ tự dừng.
let incidentTimer = 0;
function refreshLater(ms, token, fn) { setTimeout(() => { if (incidentTimer === token && state.tab === "incidents") fn().catch(() => {}); }, ms); }

VIEWS.incidents = (main) => (state.sub ? incidentDetail(main, state.sub) : incidentList(main));

const LIST_FILTERS = [["attention", "Cần người xử lý"], ["open", "Đang mở"], ["resolved", "Đã hết"], ["closed", "Đã đóng"], ["all", "Tất cả"]];

async function incidentList(main) {
  const token = ++incidentTimer;
  let status = store.get("incidents:filter", "attention");
  const box = h("div", { class: "ix" });
  addKids(main, box);
  let stats = null;
  async function load() {
    const [data, st] = await Promise.all([api("GET", `/api/incidents?status=${status}`), stats || api("GET", "/api/incidents/stats?days=30")]);
    stats = st;
    const tabs = h("div", { class: "row ix-tabs", role: "group", "aria-label": "Lọc sự cố" }, LIST_FILTERS.map(([k, label]) =>
      h("button", { type: "button", class: "chip" + (status === k ? " on" : ""), "aria-pressed": String(status === k),
        onclick: () => { status = k; store.set("incidents:filter", k); load(); } }, `${label}${data.counts[k] !== undefined ? ` (${data.counts[k]})` : ""}`)),
      h("span", { class: "spacer" }), h("span", { class: "small" }, `Tự xử lý hôm nay: ${data.auto_today}`));
    const srcInfo = Object.entries(data.sources || {});
    const statusLine = data.enabled
      ? h("div", { class: "small" }, `Lấy cảnh báo lần cuối: ${data.last_poll ? ago(new Date(data.last_poll * 1000).toISOString()) : "chưa"} `,
          ...srcInfo.map(([name, st]) => h("span", { class: "badge " + (st.error ? "bad" : "ok") }, `${name}: ${st.error ? "lỗi" : st.count + " cảnh báo"}`)),
          ...srcInfo.filter(([, st]) => st.error).map(([name, st]) => h("div", { class: "warn-text" }, `${name}: ${st.error}`)))
      : h("div", { class: "card muted" }, "Luồng tự xử lý cảnh báo đang tắt. ", isAdmin() ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("settings", undefined, "alerts"); } }, "Bật ở Cài đặt → Cảnh báo") : "Nhờ admin bật ở Cài đặt → Cảnh báo.");
    const row = (i) => {
      const tone = STATE_TONE[i.state.key] || "";
      const started = i.first_alert || i.created_at;
      const age = i.status === "open" ? `mở ${span(Date.now() - new Date(started))}` : i.resolved_at ? `hết sau ${span(new Date(i.resolved_at) - new Date(started))}` : "đã đóng";
      const open = () => go("incidents", undefined, i.id);
      return h("div", { class: "ix-inc", role: "link", tabindex: "0", onclick: open, onkeydown: (e) => { if (e.key === "Enter") open(); } },
        h("span", { class: "ix-stripe " + tone }),
        h("div", { class: "ix-t" }, h("strong", {}, i.title), i.line && h("p", {}, i.line),
          h("span", { class: "small" }, [i.env, i.sources.join(", "), `cảnh báo mới nhất ${ago(i.last_alert_at)}`].filter(Boolean).join(" · "))),
        h("div", { class: "ix-state" }, h("span", { class: "pill " + tone }, i.investigating ? "Đang điều tra" : i.state.label), sBadge(i.sev), sevBadge(i.severity),
          i.repeats > 0 && h("span", { class: "pill" }, `Lặp lại ${i.repeats} lần`)),
        h("div", { class: "ix-when" }, age, h("br"), `${i.firing}/${i.alerts} đang bắn`));
    };
    const suggest = data.suggestions.length > 0 && h("section", { class: "card ix-suggest", "aria-label": "Gợi ý gộp" },
      h("strong", {}, `${data.suggestions.length} sự cố đang mở trùng nhóm với sự cố mở trước đó`),
      h("p", { class: "small" }, "kagent không tự gộp vì sự cố đang có việc dở (điều tra, chờ duyệt, xác minh) hoặc tự gộp đang tắt. Gộp để xử lý một lần."),
      data.suggestions.map((g) => h("div", { class: "ix-rel-open" },
        h("div", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("incidents", undefined, g.from.id); } }, g.from.title), " → ",
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("incidents", undefined, g.into.id); } }, g.into.title),
          h("div", { class: "small" }, `${g.reason} · sự cố gốc mở ${hmd(g.into.created_at)}`)),
        h("button", { type: "button", class: "small-btn", onclick: guard(async () => { await api("POST", `/api/incidents/${g.from.id}/merge`, { into: g.into.id }); toast("Đã gộp"); await load(); }) }, "Gộp vào sự cố gốc"))));
    const empty = { attention: "Không có sự cố nào cần người xử lý.", open: "Không có sự cố đang mở." }[status] || "Không có sự cố.";
    setKids(box);
    appendKids(box, [tabs, statusLine, stats.incidents > 0 && statsCard(stats), suggest, data.items.length ? h("section", { class: "card ix-list", "aria-label": "Sự cố" }, data.items.map(row)) : h("div", { class: "card muted" }, empty)]);
    refreshLater(15000, token, load);
  }
  await load();
}

const pad2 = (n) => String(n).padStart(2, "0");
function hm(iso) { const d = new Date(iso); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; }
function dm(iso) { const d = new Date(iso); return `${pad2(d.getDate())}/${pad2(d.getMonth() + 1)}`; }
function hmd(iso) { return iso ? `${hm(iso)} ${dm(iso)}` : "—"; }
function span(ms) {
  const m = Math.max(0, Math.round(ms / 60000));
  if (m < 60) return `${m} phút`;
  const hh = Math.floor(m / 60);
  if (hh < 24) return `${hh} giờ ${m % 60} phút`;
  return `${Math.floor(hh / 24)} ngày ${hh % 24} giờ`;
}
const STATE_TONE = { needs_human: "bad", awaiting_approval: "warn", waiting_quota: "warn", investigating: "info", queued: "info", verifying: "info", handled: "ok", cleared: "ok", closed: "" };
const EV_MARK = { alert: "alert", alert_resolved: "clear", resolved: "clear", reopened: "alert", investigated: "agent",
  ran: "human", verified: "clear", closed: "human", task_added: "human", task_done: "clear", task_reopened: "human", failed: "fail",
  change_checked: "clear", change_check_failed: "fail", reverted: "human", revert_failed: "fail" };
const EV_FILTERS = [["all", "Tất cả"], ["action", "Hành động"], ["agent", "Điều tra"], ["alert", "Cảnh báo"]];

// Lệnh và lượt điều tra: việc đã làm bằng lời, lệnh gọn một dòng, kết quả và kiểm tra sau khi chạy; tham số đầy đủ gập lại.
function evBody(e) {
  const k = e.check;
  return [h("span", { class: "ix-title" }, e.title),
    e.cmd && e.cmd !== e.title && h("code", { class: "ix-cmd", title: e.cmd }, e.cmd),
    (e.result || k) && h("span", { class: "ix-out" + (e.kind === "failed" ? " bad" : "") }, e.result && "→ " + e.result,
      k && h("span", { class: "pill " + (k.ok ? "ok" : k.ok === false ? "bad" : ""), title: k.at ? "kiểm tra lúc " + hmd(k.at) : "" },
        (k.ok ? "kiểm tra sau: ổn" : k.ok === false ? "kiểm tra sau: lỗi" : "kiểm tra sau: không rõ") + (k.text ? " · " + k.text : ""))),
    (e.why || e.detail) && h("details", { class: "ix-more" }, h("summary", {}, "Lý do và tham số"),
      e.why && h("p", { class: "small" }, e.why), e.detail && h("pre", { class: "ix-args" }, e.detail))];
}

function causeBody(p) {
  const list = (cls, label, items) => items.length > 0 && h("div", { class: cls }, h("div", { class: "ix-cause-h" }, label), h("ul", {}, items.map((x) => h("li", {}, inlineMd(x)))));
  return [h("p", { class: "ix-lead" }, p.hedge && h("span", { class: "pill warn" }, p.hedge), " ", inlineMd(p.lead)),
    list("ix-points", "Diễn biến và bằng chứng", p.points), list("ix-gaps", "Chưa xác nhận", p.gaps)];
}

const RC_TONE = { 0: "", 1: "warn", 2: "info", 3: "ok" };
const TASK_SOURCE = { summary: "Việc cần người, kagent nêu trong bản tóm tắt", prevent: "Việc phòng ngừa từ 5 Whys",
  why: "Chuỗi 5 Whys dừng vì hết căn cứ: trả lời rồi thêm vào chuỗi ở thẻ Nguyên nhân" };

// Chuỗi 5 Whys, mức đã xử lý tới đâu và form xác nhận (incidents/rootcause.py). act: hàm gọi API rồi tải lại trang.
function rootCauseBody(rc, id, act, openIncident) {
  const tag = (w) => w.by_user ? h("span", { class: "pill" }, "người viết") : !w.grounded && h("span", { class: "pill warn" }, "giả thuyết");
  const rows = [...rc.whys, { question: "", answer: "" }].map((w) => ({
    q: h("input", { value: w.question || "", placeholder: "Tại sao …?", maxlength: "300", "aria-label": "Câu hỏi tại sao" }),
    a: h("textarea", { rows: "2", maxlength: "600", placeholder: "Câu trả lời (để trống để bỏ mắt xích)", "aria-label": "Câu trả lời" }, w.answer || ""),
  }));
  const weak = h("input", { type: "checkbox", checked: rc.weakness });
  const prevent = h("input", { placeholder: "Vd. thêm bước CI chặn cấu hình sai", maxlength: "200", "aria-label": "Việc phòng ngừa" });
  const form = h("details", { class: "ix-rc-form" }, h("summary", {}, rc.confirmed ? "Sửa và xác nhận lại chuỗi" : "Xác nhận hoặc sửa chuỗi"),
    h("p", { class: "small" }, "Hỏi tại sao cho tới khi gặp một điểm yếu sửa được của hệ thống (cấu hình, kiến trúc, công cụ, quy trình); không dừng ở \"do người làm sai\"."),
    rows.map((r, i) => h("div", { class: "ix-rc-row" }, h("span", { class: "small muted" }, `Tại sao ${i + 1}`), r.q, r.a)),
    h("label", { class: "small" }, weak, " Mắt xích cuối là điểm yếu sửa được"),
    h("label", { class: "small" }, "Việc phòng ngừa (cần để đạt mức 3)"), prevent,
    h("button", { type: "button", class: "small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/rootcause/confirm`, {
      whys: rows.filter((r) => r.a.value.trim()).map((r, i, all) => ({ question: r.q.value.trim(), answer: r.a.value.trim(), weakness: i === all.length - 1 && weak.checked })),
      prevent: prevent.value.trim() }), "Đã xác nhận chuỗi 5 Whys") }, "Xác nhận"));
  return h("div", { class: "ix-rc" },
    h("div", { class: "row" }, h("span", { class: "pill " + RC_TONE[rc.level] }, `Mức ${rc.level}: ${rc.label}`),
      rc.notes.length > 0 && h("span", { class: "small" }, rc.notes.join("; "))),
    rc.recurred && h("p", { class: "small" }, "Nguyên nhân lặp lại ở ", openIncident(rc.recurred)),
    rc.whys.length > 0 && h("div", {}, h("div", { class: "ix-cause-h" }, "5 Whys"),
      h("ol", { class: "ix-whys" }, rc.whys.map((w) => h("li", {}, h("div", { class: "small muted" }, w.question), h("div", {}, inlineMd(w.answer), " ", tag(w),
        w.weakness && h("span", { class: "pill ok" }, "điểm yếu sửa được")),
        (w.links || []).length > 0 && h("div", { class: "small" }, w.links.map((l) => srcLink(l))))))),
    rc.whys.length > 0 && rc.stop === "no_evidence" && h("p", { class: "small" }, "Chuỗi dừng vì hết căn cứ", rc.next_question ? `; cần người trả lời: ${rc.next_question}` : "."),
    rc.factors.length > 0 && h("div", {}, h("div", { class: "ix-cause-h" }, "Yếu tố góp phần"),
      h("ul", {}, rc.factors.map((f) => h("li", {}, `${f.label}: `, inlineMd(f.text), " ", !f.grounded && h("span", { class: "pill warn" }, "giả thuyết"))))),
    rc.prevent.length > 0 && h("div", {}, h("div", { class: "ix-cause-h" }, "Việc phòng ngừa"), h("ul", {}, rc.prevent.map((t) => h("li", {}, t)))),
    rc.confirmed_at && h("p", { class: "small" }, `${rc.confirmed_by || "?"} xác nhận lúc ${hmd(rc.confirmed_at)}`),
    form);
}

const CUSTOMER_CHOICES = [["unknown", "Chưa biết"], ["internal", "Chỉ nội bộ (CI, alert sai…)"], ["none", "Chưa ảnh hưởng khách hàng"],
  ["few", "Vài khách hàng hoặc một tính năng"], ["many", "Nhiều khách hàng"]];
const IMPACT_FLAGS = [["data_loss", "Mất dữ liệu"], ["control_plane", "Control plane khách hàng không gọi được (API server, console…)"],
  ["redundancy_lost", "Mất dự phòng (vd. một node etcd chết, quorum còn)"], ["missed_by_alert", "Cảnh báo không phát hiện, khách hàng báo hoặc tự thấy"],
  ["near_miss", "Suýt thành sự cố lớn"], ["spread", "Lan sang service khác, hoặc khó xử lý"]];

// Khoanh vùng từ thực thể nghi gây lỗi (topology.blast): gợi ý cho người đánh giá, không tự đổi mức.
function blastLine(topo) {
  const b = topo && topo.available && topo.blast;
  if (!b) return null;
  const node = topo.nodes.find((n) => n.id === b.entity);
  const parts = Object.entries(b.counts).map(([k, n]) => `${n} ${k}`);
  if (b.projects) parts.push(`${b.projects} project`);
  return h("div", { class: "small ix-blast" }, h("div", { class: "ix-cause-h" }, "Khoanh vùng theo topology (gợi ý)"),
    `Nếu ${node ? node.type_label + " " + node.label : b.entity} là nguyên nhân thì có thể ảnh hưởng: ${parts.join(", ") || "không thấy thứ gì phụ thuộc"}.`,
    h("div", { class: "muted" }, "Theo topology đã quét, chưa kiểm chứng; dùng để ước lượng số khách hàng, project bị ảnh hưởng khi đánh giá mức."));
}

// Mức S1–S4 và có phải viết postmortem không (incidents/impact.py): người trả lời, kagent áp luật của repo incidents.
function impactBody(im, id, act, topo) {
  const a = im.answers || {};
  const customers = h("select", { "aria-label": "Ảnh hưởng khách hàng" }, CUSTOMER_CHOICES.map(([v, label]) => h("option", { value: v, selected: (a.customers || "unknown") === v }, label)));
  const flags = IMPACT_FLAGS.map(([k, label]) => [k, h("input", { type: "checkbox", checked: !!a[k] }), label]);
  const note = h("input", { value: a.note || "", maxlength: "300", placeholder: "Ghi chú: khách hàng, project bị ảnh hưởng (tuỳ chọn)", "aria-label": "Ghi chú ảnh hưởng" });
  const pm = im.postmortem;
  return h("section", { class: "card", "aria-labelledby": "ix-i-h" }, h("div", { class: "ix-sec" }, h("h3", { id: "ix-i-h" }, "Mức độ")),
    h("div", { class: "row" }, sBadge(im), h("span", { class: "small" }, `${im.label} · ${im.reason}`)),
    im.source !== "human" && h("p", { class: "small muted" }, "Mức gợi ý: kagent không biết khách hàng nào bị ảnh hưởng. Chưa chắc thì chọn mức cao hơn, hạ sau."),
    pm.required ? h("p", { class: "small" }, h("span", { class: "pill warn" }, pm.suggested_only ? "Có thể cần postmortem" : "Cần postmortem"), " ", pm.reasons.join("; "),
      pm.due ? ` · hạn ${hmd(pm.due)}` : "") : h("p", { class: "small muted" }, "Chưa thuộc trường hợp bắt buộc postmortem."),
    a.by && h("p", { class: "small muted" }, `Đánh giá lúc ${hmd(a.at)}`),
    blastLine(topo),
    h("details", {}, h("summary", { class: "small" }, im.source === "human" ? "Đánh giá lại" : "Đánh giá ảnh hưởng"),
      h("label", { class: "field" }, "Khách hàng bị ảnh hưởng", customers),
      flags.map(([, box, label]) => h("label", { class: "row small" }, box, label)), note,
      h("button", { type: "button", class: "small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/impact`, {
        customers: customers.value, note: note.value.trim(), ...Object.fromEntries(flags.map(([k, box]) => [k, box.checked])) }), "Đã lưu đánh giá") }, "Lưu")));
}

// Link tới Prometheus, Grafana Explore, Kibana Discover dựng từ tham số đã chạy (links.py); mở tab mới, không gửi referrer.
const srcLink = (l) => h("a", { href: l.url, target: "_blank", rel: "noopener noreferrer", class: "ix-src" }, l.label + " ↗");

const REVIEW_TONE = { correct: "ok", partial: "warn", wrong: "bad" };

// Chấm nhận định của lượt điều tra hiện tại (incidents/quality.py); lượt mới được chấm riêng.
function reviewBody(rv, id, act) {
  const note = h("input", { placeholder: "Ghi chú: sai ở đâu, nguyên nhân thật là gì (tuỳ chọn)", maxlength: "500", "aria-label": "Ghi chú khi chấm" });
  const send = (verdict) => act(() => api("POST", `/api/incidents/${id}/review`, { verdict, note: note.value.trim() }), "Đã ghi nhận");
  return h("div", { class: "ix-review" },
    h("div", { class: "row" }, h("span", { class: "small" }, "Nhận định của kagent ở lượt này:"),
      rv ? h("span", { class: "pill " + REVIEW_TONE[rv.verdict], title: rv.note || "" }, rv.label) : h("span", { class: "small muted" }, "chưa chấm"),
      rv && h("span", { class: "small muted" }, `${rv.by || "?"} · ${hmd(rv.at)}${rv.source === "confirm" ? " · suy từ lúc xác nhận chuỗi" : ""}`)),
    h("details", {}, h("summary", { class: "small" }, rv ? "Chấm lại" : "Chấm đúng hay sai"), note,
      h("div", { class: "row" }, [["correct", "Đúng"], ["partial", "Đúng một phần"], ["wrong", "Sai"]].map(([v, label]) =>
        h("button", { type: "button", class: "ghost small-btn", onclick: send(v) }, label)))));
}

const pct = (x) => x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`;
const mins = (m) => m === null || m === undefined ? "—" : span(m * 60000);

// Số liệu 30 ngày: tỉ lệ nhận định đúng, MTTR, thời gian tới khi có nhận định, mức xử lý (incidents/quality.py).
function statsCard(st) {
  const r = st.reviews;
  return h("section", { class: "card ix-stats", "aria-label": `Số liệu ${st.days} ngày` },
    h("div", { class: "row" }, h("strong", {}, `${st.days} ngày`),
      h("span", {}, `${st.incidents} sự cố, ${st.investigated} đã điều tra`),
      h("span", { title: "Tỉ lệ lượt điều tra được chấm đúng trên số lượt đã chấm" },
        `Nhận định đúng: ${pct(st.accuracy)}`, r.total ? ` (tính cả đúng một phần ${pct(st.accuracy_partial)}; đã chấm ${r.total}/${st.rounds} lượt)` : " (chưa chấm lượt nào)"),
      h("span", { title: "Trung vị thời gian từ cảnh báo đầu tiên tới khi mọi cảnh báo hết" }, `MTTR: ${mins(st.mttr_minutes.median)}`),
      h("span", { title: "Trung vị thời gian từ lúc nhận sự cố tới khi có nhận định đầu tiên" }, `Có nhận định sau: ${mins(st.first_insight_minutes.median)}`),
      h("span", {}, "Mức 0–3: " + [0, 1, 2, 3].map((n) => st.levels[n]).join(" / "))),
    st.wrong.length > 0 && h("details", {}, h("summary", { class: "small" }, `${st.wrong.length} lượt bị chấm sai`),
      h("ul", { class: "small" }, st.wrong.map((w) => h("li", {}, h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("incidents", undefined, w.id); } }, w.title),
        w.note ? ` · ${w.note}` : "")))));
}

async function copyText(text, area) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    area.select(); document.execCommand("copy");  // trang mở qua http theo IP: không có clipboard API
  }
  toast("Đã chép");
}

// Bản nháp issue và postmortem cho repo incident của tổ chức (incidents/export.py); TODO là phần người phải điền.
function exportCard(x) {
  const box = (label, text, rows) => {
    const area = h("textarea", { readonly: true, rows: String(rows), "aria-label": label }, text);
    return h("div", { class: "ix-exp" }, h("div", { class: "ix-sec" }, h("h3", {}, label),
      h("button", { type: "button", class: "ghost small-btn", onclick: guard(() => copyText(text, area)) }, "Chép")), area);
  };
  const i = x.issue;
  return h("section", { class: "card ix-export", "aria-label": "Bản nháp cho repo incidents" },
    h("p", { class: "small" }, `Severity gợi ý: ${i.severity} (${i.severity_reason}). Thay <SERVICE> và service::<x>, điền các chỗ TODO trước khi tạo issue hoặc merge postmortem; kagent không đoán ảnh hưởng khách hàng.`),
    box("Tiêu đề và nhãn", `${i.title}\n${i.labels.join(" ")}`, 2),
    box("Mô tả issue (Phần A)", i.body, 10),
    box("Comment nhật ký xử lý (mỗi dòng một comment)", i.comments.join("\n"), Math.min(10, Math.max(3, i.comments.length))),
    box("Postmortem: các mục kagent có dữ liệu", x.postmortem, 16));
}

async function incidentDetail(main, id) {
  const token = ++incidentTimer;
  const box = h("div", { class: "ix" });
  addKids(main, box);
  let filter = "all";
  let menuOpen = false;
  let exportOpen = false;
  let topo = null;  // tính nặng hơn trang sự cố và ít đổi: tải một lần, nút Làm mới tải lại
  async function load() {
    const [d, open, draft] = await Promise.all([api("GET", `/api/incidents/${id}`), api("GET", "/api/incidents?status=open"),
      exportOpen ? api("GET", `/api/incidents/${id}/export`) : null]);
    if (!topo) topo = await api("GET", `/api/incidents/${id}/topology`).catch(() => ({ available: false, reason: "không tải được topology" }));
    const act = (fn, msg) => guard(async () => { await fn(); if (msg) toast(msg); await load(); });
    const busyNow = d.investigating || d.stage === "investigating";
    const tone = STATE_TONE[d.state.key] || "";
    const firing = d.alerts.filter((a) => a.status === "firing").length;
    const m = d.milestones;
    const started = m.first_alert || m.received;
    const age = d.status === "open" ? `Đang mở ${span(Date.now() - new Date(started))}` : m.resolved ? `Đã hết sau ${span(new Date(m.resolved) - new Date(started))}` : "Đã đóng";

    // Đầu trang: tiêu đề triệu chứng, chip trạng thái, nút chính; gộp và đóng nằm trong menu.
    const others = open.items.filter((i) => i.id !== d.id);
    const into = h("select", { "aria-label": "Gộp vào sự cố" }, h("option", { value: "" }, "Chọn sự cố…"), others.map((i) => h("option", { value: i.id }, `${i.title} (${dm(i.created_at)})`)));
    const closeNote = h("input", { placeholder: "Ghi chú khi đóng (tuỳ chọn)", "aria-label": "Ghi chú khi đóng" });
    const taskText = h("input", { placeholder: "Vd. đẩy lại image lên registry", maxlength: "200", "aria-label": "Việc cần người" });
    const pop = h("div", { class: "ix-pop", id: "ix-pop", hidden: !menuOpen },
      others.length > 0 && [h("label", { class: "small" }, "Gộp vào sự cố"), into, h("button", { type: "button", class: "ghost small-btn", onclick: guard(async () => {
        if (!into.value) { toast("Chọn sự cố để gộp vào", true); return; }
        await api("POST", `/api/incidents/${id}/merge`, { into: into.value }); toast("Đã gộp"); menuOpen = false; go("incidents", undefined, into.value);
      }) }, "Gộp")],
      h("label", { class: "small" }, "Thêm việc cần người"), taskText,
      h("button", { type: "button", class: "ghost small-btn", onclick: act(async () => {
        if (!taskText.value.trim()) throw new Error("Nhập việc cần làm");
        await api("POST", `/api/incidents/${id}/tasks`, { text: taskText.value.trim() }); menuOpen = false;
      }, "Đã thêm việc") }, "Thêm việc"),
      h("label", { class: "small" }, "Đóng sự cố"), closeNote,
      h("button", { type: "button", class: "danger small-btn", onclick: act(async () => { await api("POST", `/api/incidents/${id}/close`, { note: closeNote.value }); menuOpen = false; }, "Đã đóng sự cố") }, "Đóng sự cố"));
    const more = h("button", { type: "button", class: "ghost small-btn", "aria-expanded": String(menuOpen), "aria-controls": "ix-pop",
      onclick: () => { menuOpen = !menuOpen; pop.hidden = !menuOpen; more.setAttribute("aria-expanded", String(menuOpen)); } }, "Gộp, đóng, thêm việc…");
    const where = d.grouped_by.map((g) => g.replace("=", " ")).join(", ");
    const head = h("section", { class: "card ix-head " + tone, "aria-label": "Sự cố" },
      h("div", {}, h("button", { type: "button", class: "ghost small-btn", onclick: () => go("incidents") }, "← Danh sách")),
      h("div", { class: "ix-top" },
        h("div", {}, h("span", { class: "ix-eyebrow" }, ["Sự cố", d.env, where].filter(Boolean).join(" · ")), h("h1", {}, d.title),
          h("div", { class: "row" }, h("span", { class: "pill " + tone }, d.state.label), sBadge(d.impact), sevBadge(d.severity), h("span", { class: "pill" }, age),
            h("span", { class: "pill " + (firing ? "warn" : "ok") }, `${firing}/${d.alerts.length} cảnh báo đang bắn`),
            d.repeats > 0 && h("span", { class: "pill", title: "Số lần mở lại sau khi hết, hoặc nhận sự cố khác gộp vào" }, `Lặp lại ${d.repeats} lần`)),
          d.merged_from.length > 0 && h("div", { class: "small" }, `Gộp từ ${d.merged_from.length} sự cố: `,
            d.merged_from.map((x, i) => [i ? ", " : "", x.conversation_id
              ? h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("chat", x.conversation_id); } }, x.title) : x.title]))),
        h("div", { class: "row" },
          d.conversation_id && h("button", { type: "button", class: "small-btn", onclick: () => go("chat", d.conversation_id) }, busyNow ? "Xem agent đang điều tra" : "Mở hội thoại điều tra"),
          d.status === "open" && h("button", { type: "button", class: "ghost small-btn", disabled: busyNow || d.stage === "verifying",
            onclick: act(() => api("POST", `/api/incidents/${id}/investigate`), "Đã bắt đầu điều tra lại") }, busyNow ? "Đang điều tra…" : "Điều tra lại"),
          h("button", { type: "button", class: "ghost small-btn", "aria-expanded": String(exportOpen), onclick: act(() => { exportOpen = !exportOpen; }) },
            exportOpen ? "Ẩn bản nháp cho repo incidents" : "Soạn cho repo incidents"),
          d.status !== "closed" && h("div", { class: "ix-menu" }, more, pop))));
    const exported = draft && exportCard(draft);

    // Đang chờ: việc cần người, mỗi việc kèm nút làm luôn.
    const waitItem = (w) => {
      if (w.kind === "approval") {
        const reason = h("input", { placeholder: "Lý do từ chối", "aria-label": "Lý do từ chối" });
        return h("div", { class: "ix-item" }, h("span", { class: "ix-dot", "aria-hidden": "true" }),
          h("div", {}, h("strong", {}, w.summary || w.why || h("code", {}, w.command)), (w.summary || w.why) && h("code", {}, w.command),
            h("span", { class: "small" }, `${w.requested_by || "kagent"} đề xuất, chờ duyệt tới ${hmd(w.expires_at)}${w.reasons.length ? `. Phải duyệt vì: ${w.reasons.join("; ")}` : ""}`),
            w.dry_run && h("details", {}, h("summary", {}, "Kết quả chạy thử"), h("pre", {}, JSON.stringify(w.dry_run, null, 1))),
            h("div", { class: "row" }, reason)),
          h("div", { class: "ix-acts" },
            h("button", { type: "button", class: "small-btn", onclick: act(() => api("POST", `/api/approvals/${w.approval_id}/decide`, { action: "approve_once" }), "Đã duyệt") }, "Duyệt và chạy"),
            h("button", { type: "button", class: "ghost small-btn", onclick: guard(async () => {
              if (!reason.value.trim()) { toast("Nhập lý do từ chối", true); reason.focus(); return; }
              await api("POST", `/api/approvals/${w.approval_id}/decide`, { action: "deny", reason: reason.value.trim() }); toast("Đã từ chối"); await load();
            }) }, "Từ chối")));
      }
      if (w.kind === "retry") {
        const what = { expired: `Hết hạn duyệt lúc ${hmd(w.at)}, chưa chạy.`, denied: `${w.decided_by || "Người duyệt"} từ chối lúc ${hmd(w.at)}${w.deny_reason ? `: ${w.deny_reason}` : "."}`,
          error: `Chạy lỗi lúc ${hmd(w.at)}${w.result_line ? `: ${w.result_line}` : "."}` }[w.status];
        return h("div", { class: "ix-item" }, h("span", { class: "ix-dot bad", "aria-hidden": "true" }),
          h("div", {}, h("strong", {}, w.summary || w.why || h("code", {}, w.command)), (w.summary || w.why) && h("code", {}, w.command),
            h("span", { class: "small" }, what, w.status === "expired" && w.reasons.length ? ` Phải duyệt vì: ${w.reasons.join("; ")}` : "")),
          h("div", { class: "ix-acts" },
            h("button", { type: "button", class: "ghost small-btn", onclick: act(async () => {
              const r = await api("POST", `/api/incidents/${id}/actions/${w.action_id}/retry`);
              if (r.status !== "pending") toast(`Lệnh ${STEP_STATUS[r.status] || r.status}: ${r.reasons.join("; ")}`, true);
            }, "Đã tạo thẻ duyệt mới") }, "Nhờ agent đề xuất lại"),
            h("button", { type: "button", class: "ghost small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/actions/${w.action_id}/dismiss`), "Đã bỏ qua") }, "Bỏ qua")));
      }
      if (w.kind === "quota") {
        const why = w.source === "provider" ? "Provider LLM báo hết quota hoặc đang giới hạn tốc độ." : "Tenant đã dùng hết quota token trong ngày.";
        return h("div", { class: "ix-item" }, h("span", { class: "ix-dot bad", "aria-hidden": "true" }),
          h("div", {}, h("strong", {}, "Điều tra đang dừng vì hết quota"), h("span", { class: "small" }, `${why} Bấm Tiếp tục sau khi có quota lại (admin tăng quota, sang ngày mới) hoặc đã đổi sang provider/model khác.`),
            w.source === "provider" ? h("span", { class: "small" }, w.text)
              : h("a", { href: "#", class: "small", onclick: (e) => { e.preventDefault(); go("settings", undefined, "quota"); } }, state.me.is_org_admin ? "Xem và tăng quota ở Cài đặt → Quota" : "Xem quota ở Cài đặt → Quota (org admin đặt quota)")),
          h("div", { class: "ix-acts" }, h("button", { type: "button", class: "small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/investigate`), "Đã tiếp tục điều tra") }, "Tiếp tục")));
      }
      if (w.kind === "task") {
        return h("div", { class: "ix-item" }, h("span", { class: "ix-dot info", "aria-hidden": "true" }),
          h("div", {}, h("strong", {}, w.text), h("span", { class: "small" }, TASK_SOURCE[w.source] || `Việc cần người, thêm lúc ${hmd(w.created_at)}`)),
          h("div", { class: "ix-acts" }, h("button", { type: "button", class: "ghost small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/tasks/${w.task_id}/done`, { done: true }), "Đã đánh dấu xong") }, "Đánh dấu đã xong")));
      }
      const isError = w.kind === "error";
      return h("div", { class: "ix-item" }, h("span", { class: "ix-dot " + (isError ? "bad" : "info"), "aria-hidden": "true" }),
        h("div", {}, h("strong", {}, isError ? "Điều tra lỗi" : "Cần người xem"), h("span", { class: "small" }, w.text)),
        h("div", { class: "ix-acts" }, isError
          ? h("button", { type: "button", class: "ghost small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/investigate`), "Đã bắt đầu điều tra lại") }, "Điều tra lại")
          : d.conversation_id && h("button", { type: "button", class: "ghost small-btn", onclick: () => go("chat", d.conversation_id) }, "Hỏi tiếp trong hội thoại")));
    };
    const doneTasks = d.tasks_done.length > 0 && h("details", { class: "ix-done" }, h("summary", {}, `${d.tasks_done.length} việc cần người đã xong`),
      h("ul", { class: "small" }, d.tasks_done.map((t) => h("li", {}, `${t.text} · ${t.done_by || "?"} đánh dấu lúc ${hmd(t.done_at)} `,
        h("button", { type: "button", class: "ghost small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/tasks/${t.task_id}/done`, { done: false }), "Đã mở lại việc") }, "Mở lại")))));
    const wait = (d.waiting.length > 0 || doneTasks) && h("section", { class: "card ix-wait", "aria-labelledby": "ix-wait-h" },
      h("div", { class: "ix-sec" }, h("h2", { id: "ix-wait-h" }, "Đang chờ"), h("span", { class: "small" }, d.waiting.length ? `${d.waiting.length} việc` : "không còn việc nào")),
      d.waiting.map(waitItem), doneTasks);

    // Dòng sự kiện: nhóm theo ngày, lọc theo loại.
    const tl = h("ol", { class: "ix-tl" });
    const paint = () => {
      const kids = [];
      let day = null;
      for (const e of d.timeline) {
        if (filter !== "all" && e.cat !== filter) continue;
        if (dm(e.at) !== day) { day = dm(e.at); kids.push(h("li", { class: "ix-day" }, "Ngày " + day)); }
        kids.push(h("li", { class: "ix-ev" }, h("time", { datetime: e.at, title: fmtTime(e.at) }, hm(e.at)),
          h("div", { class: "ix-mark " + (EV_MARK[e.kind] || "") }, h("span", {})),
          h("div", { class: "ix-body" }, h("span", { class: "ix-who" }, e.who), e.title ? evBody(e) : h("span", { class: "ix-res" }, e.text))));
      }
      setKids(tl, ...(kids.length ? kids : [h("li", { class: "small muted" }, "Chưa có sự kiện.")]));
    };
    paint();
    const filters = h("div", { class: "ix-filters", role: "group", "aria-label": "Lọc dòng sự kiện" });
    const paintFilters = () => setKids(filters, ...EV_FILTERS.map(([k, label]) => h("button", { type: "button", class: "chip" + (filter === k ? " on" : ""),
      "aria-pressed": String(filter === k), onclick: () => { filter = k; paintFilters(); paint(); } }, label)));
    paintFilters();
    const events = h("section", { class: "card", "aria-labelledby": "ix-tl-h" }, h("div", { class: "ix-sec" }, h("h3", { id: "ix-tl-h" }, "Dòng sự kiện"), filters), tl);

    // Cột phải.
    const detect = m.first_alert && new Date(m.received) - new Date(m.first_alert);
    const impactCard = impactBody(d.impact, id, act, topo);
    const times = h("section", { class: "card", "aria-labelledby": "ix-t-h" }, h("div", { class: "ix-sec" }, h("h3", { id: "ix-t-h" }, "Mốc thời gian")),
      h("dl", { class: "ix-kv" },
        h("dt", {}, "Bắt đầu"), h("dd", {}, hmd(m.first_alert)),
        h("dt", {}, "kagent nhận"), h("dd", {}, hmd(m.received)),
        h("dt", {}, "Thời gian phát hiện"), h("dd", {}, detect > 0 ? h("span", { class: "pill " + (detect > 15 * 60000 ? "warn" : "") }, span(detect)) : "ngay"),
        h("dt", {}, "Phục hồi"), h("dd", {}, m.resolved ? hmd(m.resolved) : "chưa"),
        h("dt", {}, m.resolved ? "Kéo dài" : "Đã kéo dài"), h("dd", {}, span((m.resolved ? new Date(m.resolved) : Date.now()) - new Date(started)))));
    const alertRows = d.alerts.map((a) => {
      const keys = ["namespace", "service", "pod", "node", "job", "instance"].filter((k) => a.labels[k]).map((k) => `${k}=${a.labels[k]}`).join(" ");
      return h("tr", {}, h("td", {}, h("strong", {}, a.alertname || "?"), h("div", { class: "small" }, keys), h("div", { class: "small" }, a.summary || "")),
        h("td", { class: "small" }, `${a.status === "firing" ? "đang bắn" : "đã hết"} · từ ${hmd(a.starts_at || a.first_seen)}`,
          h("div", {}, a.join_method === "root" ? "gốc" : `${a.join_label}: ${a.join_reason || ""}`)),
        h("td", {}, d.alerts.length > 1 && a.join_method !== "root" && h("button", { type: "button", class: "ghost small-btn", onclick: act(() => api("POST", `/api/incidents/${id}/split`, { alert_id: a.id }), "Đã tách thành sự cố riêng") }, "Tách")));
    });
    const alerts = h("section", { class: "card", "aria-labelledby": "ix-a-h" },
      h("div", { class: "ix-sec" }, h("h3", { id: "ix-a-h" }, `Cảnh báo (${d.alerts.length})`), d.grouped_by.length > 0 && h("span", { class: "small" }, "gộp theo " + d.grouped_by.join(", "))),
      h("div", { style: "margin-top:8px" }, d.alert_groups.map((g) => h("div", { class: "ix-al" }, h("span", {}, g.alertname),
        h("span", { class: "pill " + (g.firing ? "warn" : "ok") }, g.firing ? `${g.firing} đang bắn` : `${g.resolved} đã hết`)))),
      h("details", { style: "margin-top:8px" }, h("summary", {}, "Xem nhãn, giờ bắt đầu, tách cảnh báo"), h("table", { class: "incident-table" }, h("tbody", {}, alertRows))));
    const c = d.cause;
    const cause = h("section", { class: "card ix-cause", "aria-labelledby": "ix-c-h" },
      h("div", { class: "ix-sec" }, h("h3", { id: "ix-c-h" }, "Nguyên nhân"),
        c && h("span", { class: "small" }, [c.round ? `lượt điều tra ${c.round}` : null, c.at ? hmd(c.at) : null, `độ tin ${(c.confidence || 0).toFixed(2)}`].filter(Boolean).join(" · "))),
      busyNow ? busy(d.queued === "slot" ? "Đang chờ tới lượt điều tra…" : "Agent đang điều tra bằng các lệnh chỉ đọc…") : null,
      c ? [c.parts ? causeBody(c.parts) : h("p", {}, "Chưa rõ nguyên nhân."),
        rootCauseBody(d.rootcause, id, act, (r) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("incidents", undefined, r.id); } }, r.title)),
        reviewBody(d.review, id, act),
        h("details", {}, h("summary", {}, `${c.steps} lệnh đọc và bằng chứng`),
          c.evidence.length ? h("ul", { class: "small" }, c.evidence.map((e) => h("li", {}, e))) : h("p", { class: "small" }, "Không có bằng chứng được ghi."),
          d.steps.some((st) => st.link) && h("div", {}, h("div", { class: "ix-cause-h" }, "Mở lại truy vấn trên công cụ gốc"),
            h("ul", { class: "small ix-links" }, d.steps.filter((st) => st.link).map((st) => h("li", {}, srcLink(st.link), " ",
              h("code", { title: JSON.stringify(st.args) }, (st.args.expr || st.args.query || st.tool).slice(0, 120)))))),
          d.conversation_id && h("a", { href: "#", class: "small", onclick: (e) => { e.preventDefault(); go("chat", d.conversation_id); } }, "Từng lệnh và câu trả lời đầy đủ ở hội thoại điều tra"))]
        : !busyNow && h("p", { class: "small muted" }, "Chưa điều tra."));

    const rel = d.related;
    const incLink = (r) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("incidents", undefined, r.id); } }, r.title);
    const related = h("section", { class: "card ix-rel", "aria-labelledby": "ix-r-h" }, h("div", { class: "ix-sec" }, h("h3", { id: "ix-r-h" }, "Sự cố liên quan")),
      rel.open.map((r) => h("div", { class: "ix-rel-open" }, h("div", {}, incLink(r), h("div", { class: "small" }, `đang mở từ ${hmd(r.created_at)} · ${r.reason}`)),
        d.status === "open" && h("button", { type: "button", class: "ghost small-btn", onclick: act(() => api("POST", `/api/incidents/${r.id}/merge`, { into: id }), "Đã gộp vào sự cố này") }, "Gộp vào đây"))),
      rel.history_count > 0 && h("p", { class: "small" }, `${rel.history_count} sự cố cùng nhóm đã hết hoặc đã đóng trong ${rel.days} ngày:`),
      rel.history.length > 0 && h("ul", { class: "small" }, rel.history.map((r) => h("li", {}, incLink(r), r.count > 1 ? ` · ${r.count} lần, ${dm(r.first)}–${dm(r.created_at)}` : ` · ${dm(r.created_at)}`))),
      !rel.open.length && !rel.history_count && h("p", { class: "small muted" }, `Không có sự cố cùng nhóm trong ${rel.days} ngày.`));
    const recap = d.summary && summaryCard(d.summary, id);
    setKids(box);
    appendKids(box, [head, exported, wait, h("div", { class: "ix-grid" }, h("div", { class: "ix-col" }, recap, topologyGraph(topo, async () => { topo = null; await load(); }), events), h("aside", { class: "ix-col", "aria-label": "Thông tin sự cố" }, times, impactCard, alerts, cause, related))]);
    if (!menuOpen && d.status === "open" && (busyNow || ["new", "awaiting_approval", "verifying"].includes(d.stage))) refreshLater(8000, token, load);
  }
  await load();
}

async function alertSettingsView(main) {
  const info = await api("GET", "/api/alert-settings");
  const cfg = JSON.parse(JSON.stringify(info.settings));
  const dis = !info.can_manage;
  const num = (key, label, step) => h("label", { class: "field" }, label, h("input", { type: "number", step: step || "1", value: cfg[key], disabled: dis,
    oninput: (e) => { cfg[key] = Number(e.target.value); } }));
  const check = (obj, key, label) => h("label", { class: "row small" }, h("input", { type: "checkbox", checked: obj[key], disabled: dis, onchange: (e) => { obj[key] = e.target.checked; } }), label);
  const sources = info.available_sources.length ? info.available_sources.map((src) => h("label", { class: "row small" },
    h("input", { type: "checkbox", checked: cfg.sources.includes(src.name), disabled: dis, onchange: (e) => { cfg.sources = e.target.checked ? [...cfg.sources, src.name] : cfg.sources.filter((x) => x !== src.name); } }),
    `${src.name} (${src.template}, môi trường ${src.envs.join(", ")})`, info.sources && info.sources[src.name] ? h("span", { class: "badge " + (info.sources[src.name].error ? "bad" : "ok") },
      info.sources[src.name].error ? "lỗi: " + info.sources[src.name].error : `${info.sources[src.name].count} cảnh báo`) : null))
    : h("div", { class: "small muted" }, "Chưa có kết nối Alertmanager/Prometheus dùng chung. Tạo ở Công cụ → Kết nối.");
  const labels = h("input", { class: "wide", value: cfg.group_labels.join(", "), disabled: dis, "aria-label": "Nhãn dùng để gom", oninput: (e) => { cfg.group_labels = csv(e.target.value); } });
  const envRows = ["dev", "staging", "prod"].map((env) => h("tr", {}, h("td", {}, env),
    h("td", {}, h("input", { type: "checkbox", checked: cfg.auto[env].enabled, disabled: dis, "aria-label": `Tự xử lý ở ${env}`, onchange: (e) => { cfg.auto[env].enabled = e.target.checked; } }))));
  const autoRules = h("textarea", { class: "code", rows: 5, disabled: dis, "aria-label": "Luật tự xử lý", value: cfg.auto_rules.join("\n"),
    oninput: (e) => { cfg.auto_rules = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean); } });
  const save = guard(async () => { await api("PUT", "/api/alert-settings", cfg); toast("Đã lưu cài đặt cảnh báo"); renderShell(); });
  const pollNow = guard(async () => { const r = await api("POST", "/api/alert-settings/poll"); toast(`Đã lấy: ${r.new} cảnh báo mới, ${r.resolved} đã hết`); renderShell(); });
  addKids(main, h("div", { class: "card" }, h("h3", {}, "Tự xử lý cảnh báo"),
    h("p", { class: "small" }, "kagent định kỳ lấy cảnh báo đang bắn, gom các cảnh báo liên quan thành sự cố (luật nhãn → quan hệ k8s → AI), điều tra bằng lệnh chỉ đọc rồi đề xuất hành động. Hành động chỉ tự chạy khi tự xử lý bật ở môi trường đó và gateway cho tự chạy: ma trận rủi ro, rồi auto mode xét hành động theo luật tự xử lý bên dưới. Auto mode chỉ đọc luật và nhãn cảnh báo, không đọc báo cáo hay log điều tra. Không đạt thì thành yêu cầu duyệt. Lệnh chạy bằng tài khoản kagent-alerts (vai trò member), có audit như mọi lệnh khác."),
    check(cfg, "enabled", "Bật luồng tự xử lý cảnh báo"),
    h("h3", {}, "Nguồn cảnh báo"), sources,
    h("h3", {}, "Gom nhóm"),
    h("div", { class: "row" }, num("poll_seconds", "Chu kỳ lấy cảnh báo (giây)"), num("window_minutes", "Khung thời gian gom (phút)"), num("debounce_seconds", "Chờ trước khi điều tra (giây)")),
    h("label", { class: "field" }, "Nhãn dùng để gom (trùng các nhãn này thì chung sự cố)", labels),
    check(cfg, "ai_grouping", "Dùng AI gom khi luật không quyết được"), num("ai_min_score", "Điểm tối thiểu để AI gộp (0.5–1)", "0.05"),
    h("h3", {}, "Điều tra và tự xử lý"),
    check(cfg, "investigate", "Tự điều tra sự cố mới (agent chỉ dùng công cụ đọc)"),
    h("table", { class: "incident-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Môi trường"), h("th", {}, "Tự xử lý"))), h("tbody", {}, envRows)),
    h("label", { class: "field" }, "Luật tự xử lý (mỗi dòng một luật). Auto mode coi các luật này như lời người dùng khi xét hành động đề xuất: chỉ việc nằm trong luật mới được tự chạy", autoRules),
    h("div", { class: "row" }, num("max_auto_actions_per_hour", "Số lần tự xử lý tối đa mỗi sự cố mỗi giờ"), num("verify_minutes", "Xác minh sau (phút)")),
    h("h3", {}, "Gộp và mở lại"),
    check(cfg, "auto_merge", "Tự gộp sự cố đang mở khớp nhau theo nhãn hoặc cùng node/workload vào sự cố cũ nhất (không gộp sự cố người đã tách tay hoặc đang có việc dở)"),
    num("reopen_hours", "Mở lại sự cố đã hết khi cùng nhóm cảnh báo bắn lại trong (giờ, 0 = tắt)"),
    h("h3", {}, "Tóm tắt sự cố"),
    check(cfg.summary, "enabled", "Tự viết tóm tắt cho lãnh đạo khi sự cố yên sau hoạt động mới (hiện ở cuối hội thoại điều tra và trang sự cố)"),
    h("label", { class: "field" }, "Viết lại sau khi yên (phút)", h("input", { type: "number", min: "1", max: "120", value: cfg.summary.idle_minutes, disabled: dis,
      oninput: (e) => { cfg.summary.idle_minutes = Number(e.target.value); } })),
    info.can_manage ? h("div", { class: "row" }, h("button", { type: "button", onclick: save }, "Lưu"), h("button", { type: "button", class: "ghost", onclick: pollNow }, "Lấy cảnh báo ngay"))
      : h("div", { class: "small muted" }, "Chỉ tenant admin được đổi cài đặt này.")));
  if (info.can_manage) addKids(main, await notifyCard());
  addKids(main, await topologyCard());
}

const TOPO_TYPES = { "k8s.cluster": "cluster", "k8s.node": "node k8s", "k8s.pv": "PV", "k8s.pvc": "PVC", "os.server": "máy ảo", "os.volume": "volume",
  "os.project": "project", "os.hypervisor": "hypervisor", "os.backend": "backend lưu trữ" };

// Trạng thái quét topology (topology.py): nguồn, tầng bị bỏ qua và vì sao, số thực thể; màn đồ thị là giai đoạn B.
async function topologyCard() {
  const st = await api("GET", "/api/topology/status");
  const last = st.last_scan;
  const scanNow = guard(async () => { const r = await api("POST", "/api/topology/scan"); toast(r.started ? "Đã bắt đầu quét" : "Đang quét"); });
  return h("div", { class: "card" }, h("h3", {}, "Topology"),
    h("p", { class: "small" }, `kagent quét k8s và OpenStack của các kết nối dùng chung ${st.full_hours} giờ một lần, và quét lại khi có sự cố mới mà lần quét gần nhất đã cách hơn ${st.incident_minutes} phút. Topology đã lưu chỉ là gợi ý cho điều tra: cạnh cũ hoặc suy từ IP phải được đọc lại trực tiếp trước khi dùng làm bằng chứng.`),
    !st.sources.length ? h("p", { class: "small muted" }, "Chưa có kết nối k8s hoặc OpenStack dùng chung.")
      : h("div", {}, h("p", { class: "small" }, last ? `Quét gần nhất ${hmd(last.started_at)} (${last.reason})${st.running ? " · đang quét" : ""}` : "Chưa quét lần nào."),
        last && h("ul", { class: "small" }, Object.entries(last.sources).map(([name, r]) => h("li", {}, h("strong", {}, name), " ",
          r.status === "error" ? h("span", { class: "warn-text" }, r.error)
            : Object.entries(r.layers || {}).map(([layer, text]) => h("span", { class: "pill " + (text === "ok" ? "ok" : "warn"), title: text }, `${layer}: ${text === "ok" ? "ok" : text.split(":")[0]}`)),
          r.status !== "error" && Object.entries(r.layers || {}).filter(([, text]) => text !== "ok").map(([layer, text]) => h("div", { class: "small muted" }, `${layer}: ${text}`))))),
        Object.keys(st.entities).length > 0 && h("p", { class: "small" }, Object.entries(st.entities).map(([k, n]) => `${n} ${TOPO_TYPES[k] || k}`).join(" · ")),
        st.can_manage && h("button", { type: "button", class: "ghost small-btn", onclick: scanNow }, "Quét ngay")));
}

const TP_W = 150, TP_H = 42, TP_GAP = 22, TP_ROW = 92;

// Xếp nút trong từng tầng theo vị trí trung bình của các nút nối với nó (hai lượt trên → dưới, dưới → trên) để ít cạnh chéo.
function tpLayout(nodes, edges) {
  const rows = [...new Set(nodes.map((n) => n.layer))].sort((a, b) => a - b);
  const byRow = new Map(rows.map((r) => [r, nodes.filter((n) => n.layer === r)]));
  const nb = new Map(nodes.map((n) => [n.id, []]));
  edges.forEach((e) => { if (nb.has(e.from) && nb.has(e.to)) { nb.get(e.from).push(e.to); nb.get(e.to).push(e.from); } });
  const pos = new Map();
  const place = (r) => byRow.get(r).forEach((n, i) => pos.set(n.id, i));
  rows.forEach(place);
  const sweep = (order) => order.forEach((r) => {
    const key = (n) => { const xs = nb.get(n.id).filter((m) => pos.has(m)).map((m) => pos.get(m)); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : pos.get(n.id); };
    byRow.set(r, [...byRow.get(r)].sort((a, b) => key(a) - key(b)));
    place(r);
  });
  sweep(rows); sweep([...rows].reverse()); sweep(rows);
  const widest = Math.max(...rows.map((r) => byRow.get(r).length));
  const W = Math.max(600, widest * (TP_W + TP_GAP) + TP_GAP);
  const xy = new Map();
  rows.forEach((r, ri) => { const list = byRow.get(r); const left = (W - list.length * (TP_W + TP_GAP) + TP_GAP) / 2;
    list.forEach((n, i) => xy.set(n.id, { x: left + i * (TP_W + TP_GAP), y: 34 + ri * TP_ROW })); });
  return { xy, rows, W, H: 34 + rows.length * TP_ROW };
}

const TP_ROLE = { suspect: "nghi gây lỗi", alert: "đang bị cảnh báo", other_alert: "cảnh báo ở sự cố khác", common: "tầng dưới chung", path: "", aggregate: "" };

// Topology của phần bị ảnh hưởng (topology.incident_view): làm nổi thực thể nghi gây lỗi, rồi thực thể bị cảnh báo. Bấm một nút làm nổi
// đường nối của nó, không lọc.
function topologyGraph(v, reload) {
  const box = h("section", { class: "card ix-topo", "aria-labelledby": "ix-tp-h" });
  const head = h("div", { class: "ix-sec" }, h("h3", { id: "ix-tp-h" }, "Topology bị ảnh hưởng"),
    h("button", { type: "button", class: "ghost small-btn", onclick: guard(reload) }, "Làm mới"));
  if (!v.available) { addKids(box, head, h("p", { class: "small muted" }, v.reason)); return box; }
  const real = v.nodes.filter((n) => n.role !== "aggregate");
  if (!real.length) {
    addKids(box, head, h("p", { class: "small muted" }, "Không gắn được cảnh báo nào của sự cố với topology đã quét."),
      v.unmapped.length > 0 && h("p", { class: "small" }, "Chưa gắn được: " + v.unmapped.map((u) => u.alertname).join(", ")));
    return box;
  }
  const { xy, rows, W, H } = tpLayout(v.nodes, v.edges);
  const byId = new Map(v.nodes.map((n) => [n.id, n]));
  const detail = h("div", { class: "small ix-tp-detail" }, "Bấm một nút để xem chi tiết và làm nổi đường nối của nó.");
  const lines = v.edges.filter((e) => xy.has(e.from) && xy.has(e.to)).map((e) => {
    const a = xy.get(e.from), b = xy.get(e.to);
    const down = a.y <= b.y;
    const el = svg("line", { class: "tp-edge " + (e.trust === "tin cậy" ? "ok" : e.trust === "nhóm" ? "group" : "hint"),
      x1: a.x + TP_W / 2, y1: down ? a.y + TP_H : a.y, x2: b.x + TP_W / 2, y2: down ? b.y : b.y + TP_H }, svg("title", {}, `${e.rel} · ${e.trust}${e.why ? " · " + e.why : ""}`));
    return { e, el };
  });
  const related = (id) => {  // tổ tiên và con cháu theo hướng cạnh
    const out = new Set([id]);
    for (const dir of ["from", "to"]) {
      const other = dir === "from" ? "to" : "from";
      let frontier = [id];
      while (frontier.length) { const next = []; v.edges.forEach((e) => { if (frontier.includes(e[dir]) && !out.has(e[other])) { out.add(e[other]); next.push(e[other]); } }); frontier = next; }
    }
    return out;
  };
  const groups = v.nodes.filter((n) => xy.has(n.id)).map((n) => {
    const { x, y } = xy.get(n.id);
    const sub = n.role === "aggregate" ? n.type_label : [n.type_label, TP_ROLE[n.role], n.alerts.length ? `${n.alerts.length} cảnh báo` : "", n.gone ? "đã mất" : ""].filter(Boolean).join(" · ");
    const g = svg("g", { class: `tp-node ${n.role}`, transform: `translate(${x},${y})`, tabindex: 0, role: "button", "aria-label": `${n.label}, ${sub}` },
      svg("rect", { width: TP_W, height: TP_H, rx: 6 }),
      svg("text", { x: 8, y: 17, class: "tp-label" }, n.label.length > 20 ? n.label.slice(0, 19) + "…" : n.label),
      svg("text", { x: 8, y: 33, class: "tp-sub" }, sub.length > 26 ? sub.slice(0, 25) + "…" : sub), svg("title", {}, `${n.label}\n${sub}`));
    const pick = () => {
      const on = related(n.id);
      groups.forEach(({ n: m, g: el }) => el.classList.toggle("dim", !on.has(m.id)));
      lines.forEach(({ e, el }) => el.classList.toggle("dim", !(on.has(e.from) && on.has(e.to))));
      setKids(detail, h("strong", {}, n.label), ` · ${n.type_label}`, TP_ROLE[n.role] && h("span", { class: "pill" }, TP_ROLE[n.role]),
        n.alerts.length > 0 && h("div", {}, "Cảnh báo: " + n.alerts.join(", ")),
        n.note && h("div", { class: "muted" }, n.note),
        n.incident && h("div", {}, "Thuộc sự cố ", h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); go("incidents", undefined, n.incident.id); } }, n.incident.title)),
        n.role !== "aggregate" && h("div", { class: "muted" }, n.id));
    };
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
    return { n, g };
  });
  const rowLabels = rows.map((r, i) => svg("text", { class: "tp-row", x: 6, y: 34 + i * TP_ROW - 8 }, v.layers[r] || ""));
  const chart = svg("svg", { class: "tp-svg", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Topology của phần bị ảnh hưởng theo tầng" },
    rowLabels, lines.map((l) => l.el), groups.map((x) => x.g));
  const s = v.suspect;
  addKids(box, head,
    h("p", { class: "small" }, s.ids.length ? [h("span", { class: "pill bad" }, "Nghi gây lỗi"), " ", s.reason]
      : "Chưa có thực thể nghi gây lỗi: chưa mắt xích 5 Whys có căn cứ nào đọc tới thực thể trong topology, và không tầng dưới chung nào đặc hiệu (xem nút \"tầng dưới chung\")."),
    h("div", { class: "tp-wrap" }, chart),
    h("div", { class: "row small tp-legend" }, h("span", { class: "tp-key suspect" }, "nghi gây lỗi"), h("span", { class: "tp-key alert" }, "bị cảnh báo"),
      h("span", { class: "tp-key other_alert" }, "cảnh báo ở sự cố khác"), h("span", { class: "tp-key common" }, "tầng dưới chung"),
      h("span", {}, "— cạnh tin cậy"), h("span", {}, "- - cạnh gợi ý (cần đọc lại)")),
    detail,
    h("p", { class: "small muted" }, `Theo topology tại lúc cảnh báo bắt đầu (${hmd(v.at)}), quét gần nhất ${hmd(v.last_scan)}. Topology đã lưu là gợi ý, không phải bằng chứng.`),
    v.unmapped.length > 0 && h("p", { class: "small muted" }, "Chưa gắn được với topology: " + [...new Set(v.unmapped.map((u) => u.alertname))].join(", ")),
    Object.keys(v.skipped_layers).length > 0 && h("p", { class: "small muted" }, "Tầng bị bỏ qua: " + Object.entries(v.skipped_layers).map(([k, t]) => `${k} (${t})`).join("; ")));
  return box;
}

const NOTICE_TONE = { sent: "ok", failed: "bad", sending: "", skipped: "" };

// Webhook báo sự cố mới, lệnh chờ duyệt, sự cố cần người (incidents/notify.py). URL đã lưu không trả về: để trống là giữ.
async function notifyCard() {
  const info = await api("GET", "/api/notify-settings");
  const cfg = { ...info.settings };
  const url = h("input", { class: "wide", placeholder: cfg.url_set ? `Đã lưu (${cfg.url_hint}); để trống là giữ` : "https://chat.example.com/hooks/…", "aria-label": "URL webhook" });
  const pub = h("input", { class: "wide", value: cfg.public_url, placeholder: "Mặc định là địa chỉ đang mở kagent", "aria-label": "Địa chỉ kagent trong link" });
  const events = Object.entries(info.events).map(([k, label]) => h("label", { class: "row small" },
    h("input", { type: "checkbox", checked: cfg.events.includes(k), onchange: (e) => { cfg.events = e.target.checked ? [...cfg.events, k] : cfg.events.filter((x) => x !== k); } }), label));
  const body = () => ({ enabled: cfg.enabled, events: cfg.events, allow_private: cfg.allow_private, public_url: pub.value.trim(), ...(url.value.trim() ? { url: url.value.trim() } : {}) });
  const save = guard(async () => { await api("PUT", "/api/notify-settings", body()); toast("Đã lưu webhook"); renderShell(); });
  const test = guard(async () => { await api("PUT", "/api/notify-settings", body()); await api("POST", "/api/notify-settings/test"); toast("Đã gửi tin thử"); renderShell(); });
  return h("div", { class: "card" }, h("h3", {}, "Thông báo qua webhook"),
    h("p", { class: "small" }, "kagent POST JSON có trường text (Slack, Mattermost và webhook vào của ứng dụng chat nhận thẳng) khi có các sự kiện dưới đây, kèm link về trang sự cố. Tin chỉ gồm tiêu đề, môi trường, mức cảnh báo và lệnh chờ duyệt; không gửi log hay kết quả điều tra. Chỉ báo việc xảy ra sau lúc bật."),
    h("label", { class: "row small" }, h("input", { type: "checkbox", checked: cfg.enabled, onchange: (e) => { cfg.enabled = e.target.checked; } }), "Bật thông báo"),
    h("label", { class: "field" }, "URL webhook", url), events,
    h("label", { class: "field" }, "Địa chỉ kagent dùng trong link", pub),
    h("label", { class: "row small" }, h("input", { type: "checkbox", checked: cfg.allow_private, disabled: !info.is_org_admin && !cfg.allow_private,
      onchange: (e) => { cfg.allow_private = e.target.checked; } }), "Cho gửi vào địa chỉ mạng nội bộ (cần org admin bật)"),
    h("div", { class: "row" }, h("button", { type: "button", onclick: save }, "Lưu"), h("button", { type: "button", class: "ghost", onclick: test }, "Lưu và gửi tin thử")),
    info.recent.length > 0 && h("details", {}, h("summary", { class: "small" }, "Tin gửi gần đây"),
      h("ul", { class: "small" }, info.recent.filter((n) => n.status !== "skipped").map((n) => h("li", {}, h("span", { class: "pill " + NOTICE_TONE[n.status] }, n.status), ` ${hmd(n.at)} · ${n.title || n.incident_id} · ${n.key.split(":")[0]}`,
        n.error ? ` · ${n.error}` : "")))));
}

VIEWS.chat = chatView;

VIEWS.episodes = async (main) => {
  const list = await api("GET", "/api/episodes");
  const detail = h("div");
  const open = (id) => guard(async () => { const ep = await api("GET", `/api/episodes/${id}`); setKids(detail, episodeCard(ep, () => renderShell())); detail.scrollIntoView(); });
  addKids(main, h("h2", {}, "Nhật ký (L0)"), h("div", { class: "card scroll" }, list.length === 0 ? "Chưa có episode." : h("table", {},
    h("thead", {}, h("tr", {}, ["Thời gian", "Nhiệm vụ", "Loại", "Môi trường", "Kết quả", "Người chạy", "Đã học"].map((x) => h("th", {}, x)))),
    h("tbody", {}, list.map((e) => h("tr", { class: "click", onclick: open(e.id) }, h("td", {}, fmtTime(e.started_at)), h("td", {}, e.input.task), h("td", {}, e.task_type), h("td", {}, e.env), h("td", {}, badge(e.outcome)), h("td", {}, e.user), h("td", {}, e.processed_at ? "rồi" : "chưa")))))), detail);
};

function kuSources(list) {
  const eps = list.filter((x) => x.type === "episode").length;
  const docs = list.filter((x) => x.type !== "episode");
  const parts = [eps ? `${eps} episode` : null, ...docs.map((d) => `${d.type === "incident" ? "" : "tài liệu "}"${d.title}"${d.section ? ` đoạn ${d.section}` : ""}`)].filter(Boolean);
  return [parts.join(" · ") || "không có"];
}

// Kết quả kiểm tra định kỳ (lint.py): chỉ việc loại ứng viên quá hạn đã tự làm, còn lại là gợi ý để admin bấm.
function lintCard(report, reload) {
  const box = h("div", { class: "card" });
  const run = guard(async () => { render(await api("POST", "/api/knowledge/lint")); toast("Đã kiểm tra kho tri thức"); await reload(); });
  const mergeEntity = (m, li) => guard(async () => {
    const r = await api("POST", "/api/knowledge/entities/merge", { sources: m.sources, target: m.target });
    setKids(li, h("span", { class: "small" }, `Đã gộp ${r.merged.join(", ")} → ${r.target} (${r.kus} KU)`));
  });
  const list = (items, line) => h("ul", { class: "small" }, items.map(line));
  function render(r) {
    if (!r) {
      setKids(box, h("div", { class: "row spread" }, h("b", {}, "Kiểm tra kho tri thức"), h("button", { type: "button", class: "ghost", onclick: run }, "Kiểm tra ngay")),
        h("div", { class: "small muted" }, "Chưa chạy lần nào. Vòng nền tự chạy mỗi ngày một lần."));
      return;
    }
    const b = r.backlog;
    setKids(box, 
      h("div", { class: "row spread" }, h("b", {}, "Kiểm tra kho tri thức"), h("span", { class: "small muted" }, `lần gần nhất ${fmtTime(r.at)}`),
        h("button", { type: "button", class: "ghost", onclick: run }, "Kiểm tra ngay")),
      h("div", { class: "small" }, `${b.candidates} ứng viên chờ duyệt${b.oldest ? `, cũ nhất từ ${fmtTime(b.oldest)}` : ""}; khoảng ${b.new_per_day} KU mới mỗi ngày.` +
        (r.linked ? ` Đã nối thêm ${r.linked} cặp KU cùng chủ đề.` : "")),
      r.expired.length > 0 && h("details", {}, h("summary", { class: "small" }, `Đã loại ${r.expired.length} ứng viên quá ${r.stale_days} ngày chưa được duyệt hay xác nhận (hoàn tác được trong Lịch sử của từng KU)`),
        list(r.expired, (x) => h("li", {}, x.title))),
      r.entity_merges.length > 0 && h("details", { open: true }, h("summary", { class: "small" }, `${r.entity_merges.length} nhóm tên thực thể chỉ khác dấu nối hoặc số nhiều`),
        list(r.entity_merges, (m) => { const li = h("li", {}, `${m.sources.join(", ")} → `, h("b", {}, m.target), ` (${m.kus} KU) `); addKids(li, h("button", { type: "button", class: "ghost small-btn", onclick: mergeEntity(m, li) }, "Gộp")); return li; })),
      r.contradictions.length > 0 && h("details", { open: true }, h("summary", { class: "small" }, `${r.contradictions.length} ứng viên mâu thuẫn với KU đang dùng (cần người chọn bên đúng)`),
        list(r.contradictions, (x) => h("li", {}, `"${x.new_title}" ↔ "${x.old_title}"`))),
      r.unused.length > 0 && h("details", {}, h("summary", { class: "small" }, `${r.unused.length} KU đang dùng nhưng ${r.unused_days} ngày chưa được nạp vào lượt nào`),
        list(r.unused, (x) => h("li", {}, `${x.title} (${x.kind}, lần cuối ${fmtTime(x.last_used)})`))),
      r.dedup && h("div", { class: "small" }, `Tìm KU trùng lúc ${fmtTime(r.dedup.at)}: xét ${r.dedup.clusters} cụm (bỏ qua ${r.dedup.skipped} cụm không đổi), ` +
        `tự gộp ${r.dedup.confirmed} ứng viên vào KU nói cùng điều, gộp ${r.dedup.merged || 0} ứng viên trùng thành bản mới (vẫn là ứng viên), đề xuất ${r.dedup.proposed} bản gộp` +
        (r.dedup.remaining ? `; còn ${r.dedup.remaining} cụm cho lần sau` : "") + (r.dedup.errors.length ? `; ${r.dedup.errors.length} cụm lỗi: ${r.dedup.errors[0]}` : "") + "."),
      !r.expired.length && !r.entity_merges.length && !r.contradictions.length && !r.unused.length && h("div", { class: "small muted" }, "Không có gì cần xử lý."));
  }
  render(report);
  return box;
}

// Đề xuất gộp của dedup.py: gộp KU đang dùng đổi điều agent sẽ thấy nên cần admin; bản gốc hết hiệu lực, vẫn hoàn tác được.
function mergeProposalsCard(reload) {
  const box = h("div");
  const TRUST = { user: "người dùng nói", verified_tool: "kết quả công cụ", internal_doc: "tài liệu", external_untrusted: "nội dung ngoài" };
  const findDups = guard(async () => {
    const r = await api("POST", "/api/knowledge/dedup");
    toast(r.started ? "Đang tìm KU trùng theo từng trang thực thể (gọi LLM, có thể mất vài phút)" : "Đang tìm rồi, chờ lượt hiện tại xong");
    poll();
  });
  const card = (p) => {
    const t = h("input", { class: "wide", value: p.title, "aria-label": "Tiêu đề bản gộp" });
    const c = h("textarea", { rows: 3, value: p.content, "aria-label": "Nội dung bản gộp" });
    const a = h("input", { class: "wide", value: p.applies_when || "", placeholder: "Áp dụng khi", "aria-label": "Điều kiện áp dụng" });
    const el = h("div", { class: "card" });
    const decide = (approve) => guard(async () => {
      await api("POST", `/api/knowledge/merge-proposals/${p.id}/decide`, approve ? { approve, title: t.value, content: c.value, applies_when: a.value } : { approve });
      setKids(el, h("div", { class: "small" }, approve ? `Đã gộp ${p.members.length} KU thành "${t.value}".` : "Đã bỏ qua; nhóm này sẽ không được đề xuất lại."));
      await reload();
    });
    addKids(el, h("div", { class: "row spread" }, h("b", {}, `Gộp ${p.members.length} KU về ${p.entity || "cùng chủ đề"}`),
      h("span", {}, h("span", { class: "small muted" }, fmtTime(p.created_at) + " "), h("span", { class: "badge info" }, p.kind))),
      h("ul", { class: "small" }, p.members.map((m) => h("li", {}, badge(m.status), " ", h("b", {}, m.title), `: ${m.content} `,
        h("span", { class: "muted" }, `(${TRUST[m.source_trust] || m.source_trust} · ✓${m.helpful_cnt} ✗${m.harmful_cnt})`)))),
      p.reason && h("div", { class: "small muted" }, p.reason), h("div", { class: "small" }, "Bản gộp (sửa được):"), t, c, a,
      h("div", { class: "row" }, h("button", { type: "button", onclick: decide(true) }, "Gộp"), h("button", { type: "button", class: "ghost", onclick: decide(false) }, "Bỏ qua")));
    return el;
  };
  async function poll() {
    const r = await api("GET", "/api/knowledge/merge-proposals");
    setKids(box, h("div", { class: "row spread" }, h("h3", {}, `Đề xuất gộp KU (${r.proposals.length})`),
        r.running ? busy("đang tìm KU trùng…") : h("button", { type: "button", class: "ghost", onclick: findDups, title: "Xét từng trang thực thể; cụm không đổi từ lần trước được bỏ qua" }, "Tìm KU trùng ngay")),
      ...(r.proposals.length ? r.proposals.map(card) : [h("div", { class: "card muted small" }, "Không có đề xuất gộp. Vòng nền tìm mỗi ngày; ứng viên nói đúng điều một KU đã có được tự gộp, không cần duyệt.")]));
    if (r.running) setTimeout(() => { if (document.contains(box)) poll().then(reload).catch(() => {}); }, 4000);
  }
  poll().catch((e) => toast(e.message, true));
  return box;
}

async function knowledgeUnitsView(main) {
  const statusSel = persist(h("select", { "aria-label": "Trạng thái", onchange: () => load() }, [["candidate", "Ứng viên"], ["active", "Hiệu lực"], ["deprecated", "Hết hiệu lực"], ["rejected", "Bị loại"], ["", "Tất cả"]].map(([v, l]) => h("option", { value: v }, l))), "ku:status");
  const kindSel = persist(h("select", { "aria-label": "Loại", onchange: () => load() }, [["", "Mọi loại"], ...["rule", "fact", "procedure", "lesson", "insight"].map((k) => [k, k])].map(([v, l]) => h("option", { value: v }, l))), "ku:kind");
  const box = h("div");
  const detail = h("div");
  const consolidated = h("div");
  const selected = new Set();
  const countLabel = h("span", { class: "small" });
  const bulkBar = h("div", { class: "card row bulk-bar hidden" });
  let current = [];
  const showKu = (id) => guard(async () => {
    const ku = await api("GET", `/api/ku/${id}`);
    setKids(detail, h("div", { class: "card" }, h("h3", {}, "Lịch sử: " + ku.title),
      h("div", { class: "small" }, `Nguồn: ${ku.source_trust} · phiên bản ${ku.version} · `, ...kuSources(ku.sources)),
      h("table", {}, h("tbody", {}, ku.events.map((ev) => h("tr", {}, h("td", {}, fmtTime(ev.at)), h("td", {}, ev.op), h("td", {}, ev.actor), h("td", {}, ev.reason || ""),
        h("td", {}, isAdmin() && h("button", { class: "ghost", onclick: guard(async () => { await api("POST", `/api/ku/events/${ev.id}/rollback`); toast("Đã hoàn tác"); await load(); }) }, "Hoàn tác"))))))));
  });
  const review = (id, approve) => guard(async () => { await api("POST", `/api/ku/${id}/review`, { approve }); toast(approve ? "Đã chấp nhận" : "Đã loại"); await load(); });
  const batch = (ids, approve) => api("POST", "/api/ku/review-batch", { ids: [...ids], approve });
  const syncBulk = () => {
    const reviewing = isAdmin() && statusSel.value === "candidate" && current.length > 0;
    bulkBar.classList.toggle("hidden", !reviewing);
    countLabel.textContent = selected.size ? `Đã chọn ${selected.size}/${current.length}` : `${current.length} ứng viên`;
    for (const b of bulkBar.querySelectorAll("button.needs-sel")) b.disabled = selected.size === 0;
  };
  const selectAll = h("input", { type: "checkbox", "aria-label": "Chọn tất cả", onchange: () => {
    selected.clear();
    if (selectAll.checked) current.forEach((k) => selected.add(k.id));
    box.querySelectorAll("input.ku-pick").forEach((c) => { c.checked = selectAll.checked; });
    syncBulk();
  } });
  const doBatch = (approve) => guard(async () => {
    const r = await batch(selected, approve);
    toast(`${approve ? "Đã chấp nhận" : "Đã loại"} ${r.done} KU${r.skipped ? `, bỏ qua ${r.skipped}` : ""}`);
    await load();
  });
  // Tổng hợp chạy nền theo lô (mỗi lô vài chục KU mất khoảng một phút); trang hỏi tiến độ mỗi 3 giây.
  // Job tổng hợp được ghi nhớ: tải lại trang thì theo dõi tiếp hoặc mở lại đề xuất, cho tới khi người dùng đóng nó.
  const consolidate = guard(async () => follow(await api("POST", "/api/ku/consolidate", { kind: kindSel.value || null })));
  async function follow(job) {
    store.set("ku:consolidate", job.id);
    const progress = h("div", { class: "card muted" });
    setKids(consolidated, progress);
    let offline = false;
    while (job.status === "running") {
      setKids(progress, busy(`Đang tổng hợp ${job.considered} ứng viên: xong ${job.done}/${job.total} lô…` +
        (offline ? " (mất kết nối tới máy chủ, đang thử lại)" : " (có thể mất vài phút; chuyển màn hay tải lại trang vẫn theo dõi tiếp)")));
      await new Promise((r) => setTimeout(r, 3000));
      if (!document.contains(progress)) return;  // người dùng đã rời trang hoặc đóng đề xuất; quay lại thì resume() theo dõi tiếp
      try { job = await api("GET", `/api/ku/consolidate/${job.id}`); offline = false; }
      catch (e) { if (e.status === 404) throw e; offline = true; }  // server khởi động lại hay mạng chập chờn: job vẫn còn, thử lại
    }
    if (job.status === "error") { store.set("ku:consolidate", null); setKids(consolidated); throw new Error("Không tổng hợp được: " + job.errors.join("; ")); }
    renderProposal(job);
    if (job.errors.length) toast(`${job.errors.length} lô bị lỗi: ${job.errors[0]}`, true);
  }
  async function resume() {
    const id = store.get("ku:consolidate", null);
    if (!id || !isAdmin()) return;
    try { await follow(await api("GET", `/api/ku/consolidate/${id}`)); }
    catch (e) {
      if (e.status !== 404) return toast(e.message, true);  // lỗi tạm thời: giữ job, lần mở lại màn sẽ thử tiếp
      store.set("ku:consolidate", null);  // job không còn (đã có lần tổng hợp mới hơn)
      setKids(consolidated);
    }
  }
  // Tên nút cho biết phạm vi sẽ xét, để không nhầm chỉ tổng hợp một loại khi bộ lọc đang bật.
  const consolidateBtn = h("button", { type: "button", class: "ghost", onclick: consolidate, title: "LLM gom các KU trùng ý và đề xuất loại KU ít giá trị; chưa thay đổi gì cho tới khi bạn chấp nhận" });
  const syncConsolidateLabel = () => { consolidateBtn.textContent = kindSel.value ? `Tổng hợp trùng lặp (loại ${kindSel.value})` : "Tổng hợp trùng lặp (mọi loại)"; };
  kindSel.addEventListener("change", syncConsolidateLabel);
  syncConsolidateLabel();
  addKids(bulkBar, h("label", { class: "small" }, selectAll, " Chọn tất cả"), countLabel, h("span", { class: "spacer" }),
    h("button", { type: "button", class: "needs-sel", onclick: doBatch(true) }, "Chấp nhận đã chọn"),
    h("button", { type: "button", class: "danger needs-sel", onclick: doBatch(false) }, "Loại đã chọn"),
    consolidateBtn);

  // Đề xuất tổng hợp: mỗi nhóm có bản gộp sửa được; chấp nhận thì bản gốc chuyển "bị loại" kèm liên kết tới bản gộp.
  function renderProposal(p) {
    const title = (id) => (p.items[id] ? p.items[id].title : id);
    const groupCard = (g) => {
      const t = h("input", { class: "wide", value: g.title, "aria-label": "Tiêu đề bản gộp" });
      const c = h("textarea", { rows: 3, value: g.content, "aria-label": "Nội dung bản gộp" });
      const a = h("input", { class: "wide", value: g.applies_when || "", placeholder: "Áp dụng khi", "aria-label": "Điều kiện áp dụng" });
      const card = h("div", { class: "card merge-card" });
      const apply = async (activate) => {
        await api("POST", "/api/ku/merge", { ids: g.ids, kind: g.kind, title: t.value, content: c.value, applies_when: a.value || null, activate });
        card.classList.remove("merge-card");
        setKids(card, h("div", { class: "small" }, `Đã gộp ${g.ids.length} KU thành "${t.value}"${activate ? " và chấp nhận" : " (vẫn là ứng viên)"}.`));
      };
      card.applyMerge = apply;  // dùng cho nút "Gộp và chấp nhận tất cả", giữ nội dung người dùng đã sửa
      const merge = (activate) => guard(async () => { await apply(activate); await load(); });
      addKids(card, h("div", { class: "row spread" }, h("b", {}, `Gộp ${g.ids.length} KU`), h("span", { class: "badge info" }, g.kind)),
        h("ul", { class: "small" }, g.ids.map((id) => h("li", {}, title(id), p.items[id] && p.items[id].source_trust === "external_untrusted" ? h("span", { class: "badge warn" }, "nội dung ngoài") : null))),
        g.reason && h("div", { class: "small muted" }, g.reason), t, c, a,
        h("div", { class: "row" }, h("button", { type: "button", onclick: merge(true) }, "Gộp và chấp nhận"), h("button", { type: "button", class: "ghost", onclick: merge(false) }, "Gộp, giữ là ứng viên"),
          h("button", { type: "button", class: "ghost", onclick: () => card.remove() }, "Bỏ qua (giữ nguyên các KU này)")));
      return card;
    };
    const dropIds = new Set(p.drop.map((d) => d.id));
    const dropCard = p.drop.length > 0 && h("div", { class: "card" }, h("b", {}, `Đề xuất loại ${p.drop.length} KU ít giá trị`),
      h("ul", { class: "small" }, p.drop.map((d) => h("li", {}, h("label", {}, h("input", { type: "checkbox", checked: true, onchange: (e) => (e.target.checked ? dropIds.add(d.id) : dropIds.delete(d.id)) }), " ", title(d.id), h("span", { class: "muted" }, " — " + d.reason))))),
      h("div", { class: "row" }, h("button", { type: "button", class: "danger", onclick: guard(async (e) => {
        const r = await batch(dropIds, false);
        setKids(e.target.closest(".card"), h("div", { class: "small" }, `Đã loại ${r.done} KU.`));
        await load();
      }) }, "Loại các mục đã chọn")));
    const mergedCount = p.groups.reduce((n, g) => n + g.ids.length, 0);
    const acceptAll = guard(async () => {
      let ok = 0, failed = 0;
      for (const card of [...consolidated.querySelectorAll(".merge-card")]) {
        try { await card.applyMerge(true); ok += 1; } catch (e) { failed += 1; }  // nhóm có KU đã bị xử lý ở nơi khác thì bỏ qua
      }
      toast(`Đã gộp và chấp nhận ${ok} nhóm${failed ? `, ${failed} nhóm lỗi` : ""}`, failed > 0);
      await load();
    });
    const acceptSingles = guard(async (e) => {
      const r = await batch(p.singles, true);
      setKids(e.target.closest(".card"), h("div", { class: "small" }, `Đã chấp nhận ${r.done} ứng viên không trùng.`));
      await load();
    });
    const scope = p.kind ? `chỉ loại ${p.kind}` : "mọi loại";
    setKids(consolidated, h("h3", {}, `Đề xuất tổng hợp (${scope}, đã xét ${p.considered} ứng viên)`),
      p.groups.length > 0 && h("div", { class: "card row" }, h("span", { class: "small" }, `${p.groups.length} nhóm gộp ${mergedCount} KU thành ${p.groups.length}; ${p.drop.length} KU đề xuất loại. Sửa hoặc bỏ qua nhóm chưa ổn rồi chấp nhận phần còn lại.`),
        h("span", { class: "spacer" }), h("button", { type: "button", onclick: acceptAll }, `Gộp và chấp nhận tất cả ${p.groups.length} nhóm`)),
      p.singles && p.singles.length > 0 && h("div", { class: "card row" },
        h("span", { class: "small" }, `${p.singles.length} ứng viên trong phạm vi này không trùng với mục nào và không bị đề xuất loại: ${p.singles.slice(0, 3).map(title).join("; ")}${p.singles.length > 3 ? "…" : ""}`),
        h("span", { class: "spacer" }), h("button", { type: "button", class: "ghost", onclick: acceptSingles }, `Chấp nhận ${p.singles.length} ứng viên không trùng`)),
      p.outside > 0 && h("div", { class: "card small muted" }, `Còn ${p.outside} ứng viên ngoài phạm vi đề xuất này (${p.kind ? "loại khác hoặc " : ""}mới sinh sau khi tổng hợp). Chọn "Mọi loại" rồi tổng hợp lại để xét cả những mục đó.`),
      ...(p.groups.length || p.drop.length ? [...p.groups.map(groupCard), dropCard] : [h("div", { class: "card muted" }, "Không thấy KU trùng ý hay ít giá trị.")]),
      h("div", { class: "row" }, h("button", { type: "button", class: "ghost", onclick: () => { store.set("ku:consolidate", null); setKids(consolidated); } }, "Đóng đề xuất")));
  }

  async function load() {
    const q = new URLSearchParams();
    if (statusSel.value) q.set("status", statusSel.value);
    if (kindSel.value) q.set("kind", kindSel.value);
    current = await api("GET", "/api/ku" + (q.toString() ? "?" + q : ""));
    selected.clear();
    selectAll.checked = false;
    const picking = isAdmin() && statusSel.value === "candidate";
    setKids(box, ...(current.length ? current.map((k) => h("div", { class: "card" },
      h("div", { class: "row spread" }, h("div", { class: "row" }, picking && h("input", { type: "checkbox", class: "ku-pick", "aria-label": "Chọn " + k.title,
        onchange: (e) => { e.target.checked ? selected.add(k.id) : selected.delete(k.id); syncBulk(); } }), h("h3", {}, k.title)),
        h("div", {}, badge(k.status), h("span", { class: "badge info" }, k.kind), h("span", { class: "badge" }, k.source_trust), h("span", { class: "badge" }, k.domain))),
      h("div", {}, k.content), k.applies_when && h("div", { class: "small" }, "Áp dụng: " + k.applies_when),
      h("div", { class: "small" }, `✓${k.helpful_cnt} ✗${k.harmful_cnt} · tin cậy ${k.confidence} · ghi nhận ${fmtTime(k.recorded_at)}`),
      h("div", { class: "row" }, k.status === "candidate" && isAdmin() && h("button", { onclick: review(k.id, true) }, "Chấp nhận"), k.status === "candidate" && isAdmin() && h("button", { class: "danger", onclick: review(k.id, false) }, "Loại"), h("button", { class: "ghost", onclick: showKu(k.id) }, "Lịch sử")))) : [h("div", { class: "card muted" }, "Không có KU ở trạng thái này.")]));
    syncBulk();
  }
  const learn = guard(async () => { const r = await api("POST", "/api/learning/run"); toast(`Đã học ${r.processed} episode, thăng hạng ${r.promoted}, chờ duyệt ${r.needs_review}`); await load(); });
  const kind = h("select", { "aria-label": "Loại" }, ["rule", "fact", "procedure", "lesson", "insight"].map((x) => h("option", {}, x)));
  const title = persist(h("input", { placeholder: "Tiêu đề", "aria-label": "Tiêu đề" }), "ku:new-title");
  const content = persist(h("input", { placeholder: "Nội dung một ý", "aria-label": "Nội dung" }), "ku:new-content");
  persist(kind, "ku:new-kind");
  const add = guard(async () => {
    await api("POST", "/api/ku", { kind: kind.value, title: title.value, content: content.value });
    title.value = content.value = ""; store.set("ku:new-title", null); store.set("ku:new-content", null);
    toast("Đã thêm ứng viên"); statusSel.value = "candidate"; store.set("ku:status", "candidate"); await load();
  });
  addKids(main, h("h2", {}, "Tri thức (L1)"), h("div", { class: "row" }, statusSel, kindSel, isAdmin() && h("button", { onclick: learn }, "Chạy học ngay")),
    h("div", { class: "card row" }, kind, title, content, h("button", { class: "ghost", onclick: add }, "Thêm KU")),
    isAdmin() && lintCard(await api("GET", "/api/knowledge/lint"), load), isAdmin() && mergeProposalsCard(load), detail, bulkBar, consolidated, box);
  await load();
  resume().catch((e) => toast(e.message, true));
};

VIEWS.approvals = async (main) => {
  const list = await api("GET", "/api/approvals");
  const pending = list.filter((a) => a.status === "pending");
  const others = list.filter((a) => a.status !== "pending");
  // Cùng thẻ với khung chat, thêm yêu cầu gốc, người yêu cầu và đường về hội thoại.
  const decide = async (a, action, note) => {
    const r = await api("POST", `/api/approvals/${a.id}/decide`, { action, reason: note });
    toast(r.status === "pending" ? "Đã ghi nhận, cần thêm người duyệt" : "Đã xử lý: " + (LABEL[r.status] || r.status));
    renderShell();
  };
  const card = (a) => approvalCard(a, decide, false, h("div", { class: "small" },
    `Yêu cầu của ${a.requester}: `, h("em", {}, a.original_request), ` · hết hạn ${fmtTime(a.expires_at)}`,
    a.conversation_id && a.mine ? [" · ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("chat", a.conversation_id); } }, "Mở hội thoại")] : null));
  addKids(main, h("h2", {}, `Chờ duyệt (${pending.length})`), pending.length ? pending.map(card) : h("div", { class: "card muted" }, "Không có lệnh nào chờ duyệt."),
    others.length > 0 && h("h2", {}, "Đã xử lý"), others.length > 0 && h("div", { class: "card scroll" }, h("table", {}, h("tbody", {}, others.map((a) => h("tr", {}, h("td", {}, fmtTime(a.decided_at)), h("td", {}, a.tool), h("td", {}, a.env), h("td", {}, badge(a.status)), h("td", {}, a.deny_reason || "")))))));
};

// Đề xuất chờ duyệt: chỉ quy ước core và Skill; hiện danh sách quy ước thêm/bỏ bằng lời thay cho git diff.
// Quy ước đã tự publish gần đây: để người duyệt xem lại và gỡ nếu chưa ổn.
function autoPublishedSection(runs) {
  const since = Date.now() - 14 * 86400000;
  const items = runs.filter((r) => r.status === "published" && r.summary && r.summary.rules_auto && new Date(r.created_at).getTime() >= since)
    .flatMap((r) => r.summary.rules_auto.map((x) => ({ ...x, at: r.created_at })));
  if (!items.length) return null;
  const retire = (ku) => guard(async () => { await api("POST", "/api/compile/retire-rule", { ku_id: ku }); toast("Đã gỡ quy ước khỏi core"); renderShell(); });
  return h("div", {}, h("h2", {}, `Đã tự publish 14 ngày qua (${items.length})`), h("div", { class: "card" }, h("ol", {}, items.map((x) => h("li", { class: "rule-item" }, h("div", {}, x.content),
    h("div", { class: "small muted" }, `tự publish lúc ${fmtTime(x.at)}`, isAdmin() && h("button", { type: "button", class: "ghost small-btn", onclick: retire(x.ku) }, "Gỡ khỏi core")))))));
}

function pendingSection(r) {
  if (!r) return h("div", {}, h("h2", {}, "Chờ duyệt"), h("div", { class: "card muted" }, "Không có thay đổi nào chờ duyệt. Playbook và hồ sơ thực thể được publish tự động."));
  const sm = r.summary || { rules_added: [], rules_removed: [], rules_kept: 0, skills: [] };
  const decide = (approve) => guard(async () => { await api("POST", `/api/compile/${r.id}/decide`, { approve }); toast(approve ? "Đã publish" : "Đã từ chối đề xuất"); renderShell(); });
  const retire = (ku) => guard(async () => { await api("POST", "/api/compile/retire-rule", { ku_id: ku }); toast("Đã bỏ quy ước, đề xuất được cập nhật"); renderShell(); });
  const trust = { user: "người dùng nói", verified_tool: "từ kết quả công cụ", internal_doc: "tài liệu nội bộ", external_untrusted: "nội dung ngoài" };
  const ruleItem = (x, removable) => h("li", { class: "rule-item" }, h("div", {}, x.content),
    h("div", { class: "small muted" }, [trust[x.source_trust] || x.source_trust, x.episodes ? `${x.episodes} lượt chat` : null, x.why_review ? `chờ duyệt vì: ${x.why_review}` : null].filter(Boolean).join(" · "),
      removable && isAdmin() && h("button", { type: "button", class: "ghost small-btn", onclick: retire(x.ku), title: "Quy ước này hết hiệu lực và không được nạp vào agent" }, "Bỏ quy ước này")));
  return h("div", {}, h("h2", {}, "Chờ duyệt"), h("div", { class: "card" },
    h("p", { class: "small" }, `Quy ước trong khối core được nạp vào mọi lượt chat của tenant nên cần người duyệt. Đề xuất tạo lúc ${fmtTime(r.created_at)}; chỉ giữ đề xuất mới nhất.`),
    sm.rules_added.length > 0 && h("div", {}, h("b", {}, `Thêm ${sm.rules_added.length} quy ước`), h("ol", {}, sm.rules_added.map((x) => ruleItem(x, true)))),
    (sm.rules_changed || []).length > 0 && h("div", {}, h("b", {}, `Sửa nội dung ${sm.rules_changed.length} quy ước`), h("ol", {}, sm.rules_changed.map((x) => ruleItem(x, true)))),
    sm.rules_removed.length > 0 && h("div", {}, h("b", {}, `Bỏ ${sm.rules_removed.length} quy ước`), h("ol", {}, sm.rules_removed.map((x) => ruleItem(x, false)))),
    sm.rules_kept > 0 && h("div", { class: "small muted" }, `Giữ nguyên ${sm.rules_kept} quy ước đang dùng.`),
    sm.skills.length > 0 && h("div", {}, h("b", {}, "Skill thay đổi: "), sm.skills.join(", ")),
    h("details", {}, h("summary", { class: "small" }, "Xem diff gốc"), h("pre", {}, r.diff || "")),
    isAdmin() && h("div", { class: "row" }, h("button", { onclick: decide(true) }, "Publish"), h("button", { class: "danger", onclick: decide(false) }, "Từ chối cả đề xuất"))));
}

async function compiledView(main) {
  const [runs, files, golden] = await Promise.all([api("GET", "/api/compile/runs"), api("GET", "/api/knowledge/files"), api("GET", "/api/golden")]);
  const viewer = h("pre", {}, "Chọn một file.");
  const show = (path) => guard(async () => { const f = await api("GET", "/api/knowledge/file?path=" + encodeURIComponent(path)); viewer.textContent = f.content; });
  const compile = guard(async () => { const r = await api("POST", "/api/compile"); toast("Biên dịch: " + (LABEL[r.status] || r.status)); renderShell(); });
  const rollback = guard(async () => { await api("POST", "/api/compile/rollback"); toast("Đã hoàn tác lần publish gần nhất"); renderShell(); });
  const query = h("input", { placeholder: "Câu hỏi golden", "aria-label": "Câu hỏi golden" });
  const domain = h("select", { "aria-label": "Loại nhiệm vụ" }, ["k8s_ops", "observability", "docs", "research", "general"].map((x) => h("option", {}, x)));
  const terms = h("input", { placeholder: "Từ khóa kỳ vọng, cách nhau dấu phẩy", "aria-label": "Từ khóa kỳ vọng" });
  const addGolden = guard(async () => { await api("POST", "/api/golden", { query: query.value, expect_domain: domain.value, expect_terms: terms.value.split(",").map((s) => s.trim()).filter(Boolean) }); renderShell(); });
  addKids(main, h("h2", {}, "Runbook (L2)"), isAdmin() && h("div", { class: "row" }, h("button", { onclick: compile }, "Biên dịch ngay"), h("button", { class: "danger", onclick: rollback }, "Hoàn tác publish gần nhất")),
    h("div", { class: "split", style: null }, h("div", { class: "card filelist" }, h("h3", {}, "File"), files.length ? files.map((f) => h("button", { onclick: show(f) }, f)) : "Chưa có file."), h("div", { class: "card" }, h("h3", {}, "Nội dung"), viewer)),
    pendingSection(runs.find((r) => r.status === "pending_review")), autoPublishedSection(runs),
    h("h2", {}, "Lịch sử publish"), runs.filter((r) => r.status !== "pending_review").length ? h("div", { class: "card scroll" }, h("table", {}, h("tbody", {},
      runs.filter((r) => r.status !== "pending_review").map((r) => h("tr", {}, h("td", {}, fmtTime(r.created_at)), h("td", {}, badge(r.status)),
        h("td", {}, r.decided_by === "auto" ? "tự publish" : r.decided_by || r.actor),
        h("td", { class: "small" }, r.changed.join(", ") || r.reason || "", r.summary && r.summary.rules_auto ? ` · ${r.summary.rules_auto.length} quy ước tự publish` : "")))))) : h("div", { class: "card muted" }, "Chưa biên dịch lần nào."),
    h("h2", {}, `Golden task (${golden.length})`), h("div", { class: "card" }, golden.map((g) => h("div", {}, g.query, " ", h("span", { class: "badge" }, g.expect_domain), " ", h("span", { class: "small" }, g.expect_terms.join(", ")))), isAdmin() && h("div", { class: "row" }, query, domain, terms, h("button", { class: "ghost", onclick: addGolden }, "Thêm"))));
};

const ACTION_LABEL = { read: "đọc", write: "ghi", destructive: "phá hủy", exec: "thực thi", external_send: "gửi ra ngoài" };
const ROLE_LABEL = { member: "member", tenant_admin: "tenant admin", "approver:dev": "approver dev", "approver:staging": "approver staging", "approver:prod": "approver prod" };

function toolCatalogSection() {
  const admin = isAdmin();
  const box = h("div", {}, h("div", { class: "card muted" }, busy("Đang tải danh mục…")));
  const search = h("input", { type: "search", placeholder: "Tìm công cụ", "aria-label": "Tìm công cụ" });
  const filter = h("select", { "aria-label": "Lọc công cụ" }, [["all", "Tất cả"], ["enabled", "Đang bật"], ["off", "Đang tắt"], ["personal", "Cá nhân"]].map(([v, l]) => h("option", { value: v }, l)));
  let list = [];
  const patch = (t, body) => guard(async () => {
    const fresh = await api("PATCH", `/api/tools/${encodeURIComponent(t.tool)}`, body);
    list = list.map((x) => (x.tool === t.tool ? fresh : x));
    paint();
  });
  const flip = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  const chip = (label, on, onclick, disabled, title) => h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": on ? "true" : "false", onclick, disabled, title }, label);
  const tierClass = (r) => ({ R3: "warn", R4: "bad" }[r] || "");

  function item(t) {
    const removed = t.status !== "active";
    const locked = !(admin || t.personal) || removed;
    const lastOne = (arr, v) => arr.length === 1 && arr.includes(v);
    const envs = t.catalog_envs.map((e) => chip(e, t.effective_envs.includes(e), patch(t, { envs: flip(t.effective_envs, e) }), locked || lastOne(t.effective_envs, e), "Môi trường được dùng công cụ"));
    const roles = t.role_choices.map((r) => chip(ROLE_LABEL[r] || r, t.effective_roles.includes(r), patch(t, { roles: flip(t.effective_roles, r) }), locked || lastOne(t.effective_roles, r), "Vai trò được gọi công cụ"));
    const tier = h("select", { "aria-label": `Mức rủi ro của ${t.tool}`, disabled: locked, onchange: (e) => patch(t, { risk_tier: e.target.value })() },
      (t.tier_choices.length ? t.tier_choices : [t.risk_tier]).map((r) => h("option", { selected: r === t.risk_tier }, r)));
    const state = removed ? null : (admin || t.personal)
      ? h("button", { type: "button", class: t.enabled ? "ghost small-btn" : "small-btn", onclick: patch(t, { enabled: !t.enabled }) }, t.enabled ? "Tắt" : "Bật")
      : h("span", { class: "small" }, t.enabled ? "đang bật" : "đang tắt");
    return h("div", { class: "tool-item" + (t.enabled && !removed ? "" : " off") },
      h("div", { class: "tool-head" }, h("code", {}, t.tool), h("span", { class: "badge " + tierClass(t.risk_tier) }, t.risk_tier),
        t.risk_tier !== t.catalog_tier && h("span", { class: "small" }, `template: ${t.catalog_tier}`), t.personal && h("span", { class: "badge info" }, "cá nhân"), h("span", { class: "badge" }, ACTION_LABEL[t.action_class] || t.action_class),
        t.untrusted_output ? h("span", { class: "badge warn" }, "nội dung ngoài") : null, t.is_new && !removed && h("span", { class: "badge info" }, "Mới"),
        removed && h("span", { class: "badge bad" }, "Đã gỡ khỏi danh mục"), !t.classified && h("span", { class: "badge warn" }, "Chưa phân loại"), h("span", { class: "spacer" }), state),
      h("div", { class: "small" }, t.description + (t.action_class === "read" ? "" : " · " + undoText(t))),
      !removed && h("div", { class: "tool-perms" }, h("span", { class: "small" }, "Môi trường"), envs, !t.personal && h("span", { class: "small sep" }, "Vai trò"), t.personal ? null : roles, h("span", { class: "small sep" }, "Mức"), tier),
      t.warnings.length > 0 && h("div", { class: "small warn-text" }, "Cảnh báo: " + t.warnings.join("; ")));
  }

  function paint() {
    const q = search.value.trim().toLowerCase();
    const match = { all: () => true, enabled: (t) => t.enabled, off: (t) => !t.enabled, personal: (t) => t.personal }[filter.value];
    const shown = list.filter((t) => match(t) && (!q || t.tool.includes(q) || t.description.toLowerCase().includes(q)));
    const groups = new Map();
    for (const t of shown) { if (!groups.has(t.server)) groups.set(t.server, []); groups.get(t.server).push(t); }
    setKids(box, 
      ...(shown.length ? [...groups].map(([server, items]) => h("div", { class: "card" }, h("h3", {}, server, " ", h("span", { class: "small" }, items[0].server_description)), items.map(item)))
        : [h("div", { class: "card muted" }, list.length ? "Không có công cụ phù hợp." : "Chưa có công cụ nào. Tạo kết nối ở trang Kết nối công cụ.")]));
  }
  search.addEventListener("input", paint);
  filter.addEventListener("change", paint);
  api("GET", "/api/tools").then((r) => { list = r; paint(); }).catch((e) => toast(e.message, true));
  return h("div", {}, h("p", { class: "small" }, "Công cụ sinh ra từ các kết nối (trang Kết nối công cụ). Ở đây chỉ được siết thêm: bật/tắt, nâng mức rủi ro so với template, thu hẹp môi trường trong phạm vi của kết nối và vai trò. Công cụ dùng chung do admin quản lý, công cụ cá nhân do chính chủ quản lý."),
    h("div", { class: "row" }, search, filter), box);
}

const FIELD_HINT = { url: "https://…", secret: "", list: "giá trị 1, giá trị 2" };

function connectionForm(templates, existing, onDone) {
  const admin = isAdmin();
  const tplSelect = h("select", { "aria-label": "Template", disabled: !!existing }, templates.map((t) => h("option", { value: t.name, selected: existing && existing.template === t.name }, t.title)));
  const name = h("input", { placeholder: "tên_kết_nối, ví dụ k8s_prod", "aria-label": "Tên kết nối", value: existing ? existing.name : "", disabled: !!existing });
  const title = h("input", { placeholder: "Tên hiển thị", "aria-label": "Tên hiển thị", value: existing ? existing.title : "", class: "wide" });
  const scope = h("select", { "aria-label": "Phạm vi", disabled: !!existing }, h("option", { value: "user" }, "Cá nhân (chỉ mình tôi)"), admin && h("option", { value: "tenant", selected: existing && existing.scope === "tenant" }, "Dùng chung cho tenant"));
  // Bản nháp của form (trừ secret) được giữ qua refresh, riêng cho tạo mới và cho từng kết nối đang sửa.
  const draft = existing ? `conn:${existing.id}` : "conn:new";
  if (!existing) { persist(tplSelect, `${draft}:template`); persist(name, `${draft}:name`); persist(scope, `${draft}:scope`); }
  persist(title, `${draft}:title`);
  let envs = store.get(`${draft}:envs`, existing ? [...existing.envs] : ["dev"]);
  let roles = store.get(`${draft}:roles`, existing ? [...existing.roles] : ["member"]);
  const chips = h("div", { class: "tool-perms" });
  const allowPrivate = h("input", { type: "checkbox", checked: existing && existing.allow_private, "aria-label": "Cho phép mạng nội bộ" });
  const fieldsBox = h("div", { class: "fields" });
  const inputs = {};
  const desc = h("div", { class: "small" });
  const flip = (arr, v) => (arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);
  function paintChips() {
    const chip = (label, on, onclick) => h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": on ? "true" : "false", onclick }, label);
    setKids(chips, h("span", { class: "small" }, "Môi trường"), ...["dev", "staging", "prod"].map((e) => chip(e, envs.includes(e), () => { envs = flip(envs, e); store.set(`${draft}:envs`, envs); paintChips(); })),
      ...(scope.value === "tenant" ? [h("span", { class: "small sep" }, "Vai trò được dùng"), ...Object.keys(ROLE_LABEL).map((r) => chip(ROLE_LABEL[r], roles.includes(r), () => { roles = flip(roles, r); store.set(`${draft}:roles`, roles); paintChips(); }))] : []));
  }
  function paintFields() {
    const tpl = templates.find((t) => t.name === tplSelect.value);
    desc.textContent = tpl.description + " Công cụ: " + tpl.tools.map((t) => `${t.name} (${t.risk_tier})`).join(", ") + ".";
    for (const k of Object.keys(inputs)) delete inputs[k];
    setKids(fieldsBox, ...tpl.fields.map((f) => {
      const current = existing ? existing.params[f.name] : undefined;
      let input;
      if (f.type === "boolean") input = h("input", { type: "checkbox", checked: current !== undefined ? current : f.default === true });
      else if (f.type === "text") input = h("textarea", { rows: 3, value: current || "" });
      else if (f.type === "secret_text") input = h("textarea", { rows: 6, class: "code", spellcheck: "false", autocomplete: "off",
        placeholder: existing && existing.secrets_set[f.name] ? "đã lưu, để trống để giữ nguyên" : "" });
      else {
        const isSecret = f.type === "secret";
        const set = existing && existing.secrets_set[f.name];
        input = h("input", { type: isSecret ? "password" : f.type === "integer" ? "number" : "text", autocomplete: "off",
          placeholder: isSecret ? (set ? "đã lưu, để trống để giữ nguyên" : "") : (f.default !== null && f.default !== undefined ? String(f.default) : FIELD_HINT[f.type] || ""),
          value: isSecret ? "" : Array.isArray(current) ? current.join(", ") : current !== undefined && current !== null ? String(current) : "" });
      }
      input.setAttribute("aria-label", f.name);
      if (f.type !== "secret" && f.type !== "secret_text") persist(input, `${draft}:${tplSelect.value}:${f.name}`);
      inputs[f.name] = { f, input };
      return h("label", { class: "field" }, `${f.name}${f.required ? " *" : ""}${f.description ? " · " + f.description : ""}`, input);
    }));
  }
  tplSelect.addEventListener("change", paintFields);
  scope.addEventListener("change", paintChips);
  const submit = guard(async (ev) => {
    ev.preventDefault();
    const params = {}, secrets = {};
    for (const [key, { f, input }] of Object.entries(inputs)) {
      if (f.type === "boolean") params[key] = input.checked;
      else if (f.type === "secret" || f.type === "secret_text") { if (input.value.trim()) secrets[key] = input.value; }
      else if (input.value.trim() !== "") params[key] = f.type === "integer" ? Number(input.value) : input.value;
    }
    const body = { title: title.value, envs, params, secrets, ...(scope.value === "tenant" ? { roles } : {}), ...(state.me.is_org_admin ? { allow_private: allowPrivate.checked } : {}) };
    const saved = existing ? await api("PATCH", `/api/connections/${existing.id}`, body)
      : await api("POST", "/api/connections", { ...body, template: tplSelect.value, name: name.value.trim(), scope: scope.value });
    for (const { input, f } of Object.values(inputs)) if (input.type === "password" || (f && f.type === "secret_text")) input.value = "";
    store.clear(draft + ":");
    toast(existing ? "Đã lưu kết nối" : `Đã tạo kết nối ${saved.name}. Công cụ đọc đã bật; công cụ ghi đang tắt, bật ở trang Công cụ & Chính sách.`);
    onDone();
  });
  paintFields();
  paintChips();
  return h("form", { class: "card", onsubmit: submit }, h("h3", {}, existing ? `Sửa kết nối ${existing.name}` : "Tạo kết nối từ template"),
    h("div", { class: "row" }, tplSelect, name, scope), desc, h("div", { class: "row" }, title), chips,
    state.me.is_org_admin && h("label", { class: "row small" }, allowPrivate, "Cho phép kết nối vào mạng nội bộ (chỉ org admin)"),
    fieldsBox, h("div", { class: "row" }, h("button", { type: "submit" }, existing ? "Lưu" : "Tạo kết nối"), existing && h("button", { type: "button", class: "ghost", onclick: onDone }, "Đóng")));
}

// Quyền Kubernetes: 3 nhóm cố định, mỗi nhóm một ServiceAccount do kagent tạo trong cụm, ứng với một công cụ kubectl_*.
const RBAC_GROUPS = [
  ["read", "Đọc", "kubectl_read · R1", ["get", "list", "watch"]],
  ["write", "Ghi", "kubectl_write · R3, chạy thử trước", ["get", "list", "watch", "create", "update", "patch"]],
  ["admin", "Quản trị", "kubectl_admin · R4, luôn cần duyệt", ["get", "list", "watch", "create", "update", "patch", "delete", "deletecollection", "*"]],
];
const csv = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
const groupsIn = (v) => { const out = csv(v).map((g) => (g === "core" ? "" : g)); return out.length ? out : [""]; };
const groupsOut = (gs) => gs.map((g) => (g === "" ? "core" : g)).join(", ");

function k8sRbacPanel(c) {
  const box = h("div", { class: "rbac" }, busy("Đang tải quyền…"));
  const draftKey = `rbac:${c.id}`;
  let info, cfg;
  const load = async () => {
    info = await api("GET", `/api/connections/${c.id}/k8s-rbac`);
    cfg = store.get(draftKey, null) || JSON.parse(JSON.stringify(info.config));
    paint();
  };
  const save = () => store.set(draftKey, cfg);
  const result = h("div");
  function ruleRow(profile, verbs, rule, idx) {
    const field = (label, value, onchange, placeholder) => h("label", { class: "field" }, label,
      h("input", { value, placeholder, disabled: !info.can_manage, oninput: (e) => { onchange(e.target.value); save(); } }));
    const chips = h("div", { class: "tool-perms" }, verbs.map((v) => {
      const on = rule.verbs.includes(v);
      return h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": on ? "true" : "false", disabled: !info.can_manage,
        onclick: () => { rule.verbs = on ? rule.verbs.filter((x) => x !== v) : [...rule.verbs, v]; save(); paint(); } }, v);
    }));
    return h("div", { class: "rbac-rule" },
      h("div", { class: "row" },
        field("Namespace (* = toàn cụm)", rule.namespaces.join(", "), (v) => { rule.namespaces = csv(v); }, "app-a, app-b"),
        field("API group (core = nhóm lõi, * = mọi group)", groupsOut(rule.api_groups), (v) => { rule.api_groups = groupsIn(v); }, "core, apps"),
        info.can_manage && h("button", { type: "button", class: "ghost small-btn", "aria-label": "Xóa quy tắc",
          onclick: () => { cfg.profiles[profile].splice(idx, 1); save(); paint(); } }, "Xóa")),
      field("Resource (* = mọi resource)", rule.resources.join(", "), (v) => { rule.resources = csv(v); }, "pods, pods/log, deployments"),
      info.can_manage && h("div", { class: "row small" },
        h("button", { type: "button", class: "chip" + (rule.api_groups.includes("*") ? " on" : ""), onclick: () => { rule.api_groups = ["*"]; save(); paint(); } }, "* mọi API group"),
        h("button", { type: "button", class: "chip" + (rule.resources.includes("*") ? " on" : ""), onclick: () => { rule.resources = ["*"]; save(); paint(); } }, "* mọi resource"),
        profile !== "admin" && (rule.resources.some((r) => r.includes("*"))) && h("span", {}, "kagent đổi * thành mọi resource đang có trong cụm, bỏ secrets, */exec, */attach, */portforward, */proxy, serviceaccounts/token" + (profile === "write" ? ", pods/eviction" : "") + ". Cài CRD mới thì bấm Áp dụng lại.")),
      h("div", { class: "row small" }, "Verb", chips));
  }
  function paintPlan(r, applied) {
    const cls = { create: "ok", update: "warn", delete: "bad", unchanged: "" };
    const label = { create: "tạo", update: "sửa", delete: "xóa", unchanged: "giữ nguyên" };
    setKids(result, h("div", { class: "card" },
      h("strong", {}, applied ? `Đã áp dụng (${r.changes} thay đổi)` : r.changes ? `Sẽ có ${r.changes} thay đổi trong cụm` : "Cụm đã khớp với cấu hình"),
      r.errors.length ? h("ul", { class: "warn-text small" }, r.errors.map((e) => h("li", {}, e))) : null,
      r.warnings.length ? h("ul", { class: "small" }, r.warnings.map((w) => h("li", {}, "⚠ " + w))) : null,
      h("table", { class: "small" }, h("tbody", {}, r.items.filter((i) => i.action !== "unchanged" || !applied).map((i) => h("tr", {},
        h("td", {}, h("span", { class: "badge " + cls[i.action] }, label[i.action])), h("td", {}, i.kind),
        h("td", {}, h("code", {}, (i.namespace ? i.namespace + "/" : "") + i.name)), h("td", {}, i.summary || ""),
        h("td", { class: "warn-text" }, i.error || "")))))));
  }
  function paint() {
    const ids = RBAC_GROUPS.map(([p, name]) => h("span", { class: "badge " + (info.identities[p] ? "ok" : "") }, `${name}: ${info.identities[p] ? "đã có SA" : "chưa có"}`));
    const preview = guard(async () => {
      setKids(result, busy("Đang so sánh với cụm và chạy thử…"));
      try { paintPlan(await api("POST", `/api/connections/${c.id}/k8s-rbac/plan`, { config: cfg }), false); } catch (e) { setKids(result); throw e; }
    });
    const apply = guard(async () => {
      setKids(result, busy("Đang áp dụng vào cụm…"));
      let r;
      try { r = await api("POST", `/api/connections/${c.id}/k8s-rbac/apply`, { config: cfg }); } catch (e) { setKids(result); throw e; }
      store.set(draftKey, null);
      toast("Đã áp dụng quyền Kubernetes. Bật các công cụ kubectl_* ở trang Danh mục & quyền nếu cần.");
      await load();
      paintPlan(r, true);
    });
    const remove = h("button", { type: "button", class: "danger small-btn", onclick: guard(async () => {
      if (!remove.dataset.confirm) { remove.dataset.confirm = "1"; remove.textContent = "Bấm lần nữa để gỡ khỏi cụm"; setTimeout(() => { delete remove.dataset.confirm; remove.textContent = "Gỡ khỏi cụm"; }, 4000); return; }
      const r = await api("DELETE", `/api/connections/${c.id}/k8s-rbac`);
      store.set(draftKey, null);
      toast(`Đã gỡ ${r.removed} object khỏi cụm`);
      setKids(result);
      await load();
    }) }, "Gỡ khỏi cụm");
    const saNs = h("input", { value: cfg.sa_namespace, disabled: !info.can_manage, "aria-label": "Namespace chứa ServiceAccount",
      oninput: (e) => { cfg.sa_namespace = e.target.value.trim(); save(); } });
    setKids(box);
    appendKids(box, [
      h("p", { class: "small" }, info.applied ? `Đã áp dụng ${fmtTime(info.applied_at)}. ` : "Chưa áp dụng: công cụ kubectl_* chưa chạy được, các công cụ cũ đang dùng tài khoản của kết nối. ",
        "Mỗi nhóm có quy tắc thì được một ServiceAccount riêng, tên ", h("code", {}, info.base_name + "-<nhóm>"), ", nhãn ", h("code", {}, info.label),
        ". Nhóm ghi có thêm quyền của nhóm đọc, nhóm quản trị có thêm quyền của cả hai."),
      h("div", { class: "tool-perms" }, ids),
      h("label", { class: "field" }, "Namespace chứa ServiceAccount (tự tạo nếu chưa có)", saNs),
      RBAC_GROUPS.map(([p, name, tool, verbs]) => h("div", { class: "card" },
        h("div", { class: "tool-head" }, h("strong", {}, `Nhóm ${name}`), h("span", { class: "badge" }, tool), h("span", { class: "spacer" }),
          info.can_manage && h("button", { type: "button", class: "ghost small-btn", onclick: () => {
            cfg.profiles[p].push({ namespaces: [c.params.namespace || "default"], api_groups: [""], resources: [], verbs: verbs.slice(0, 3) }); save(); paint(); } }, "+ Thêm quy tắc")),
        cfg.profiles[p].length ? cfg.profiles[p].map((rule, i) => ruleRow(p, verbs, rule, i)) : h("div", { class: "small muted" }, "Chưa có quy tắc: nhóm này không có ServiceAccount."))),
      info.can_manage && h("div", { class: "row" }, h("button", { type: "button", onclick: preview }, "Xem trước"), h("button", { type: "button", onclick: apply }, "Áp dụng"),
        h("button", { type: "button", class: "ghost", onclick: () => { store.set(draftKey, null); cfg = JSON.parse(JSON.stringify(info.config)); setKids(result); paint(); } }, "Bỏ thay đổi"),
        info.applied && remove),
      result]);
  }
  load().catch((e) => setKids(box, h("div", { class: "warn-text small" }, e.message)));
  return box;
}

async function connectionsView(main) {
  const [templates, list] = await Promise.all([api("GET", "/api/templates"), api("GET", "/api/connections")]);
  const editor = h("div");
  const reload = () => renderShell();
  const action = (c, fn) => guard(async () => { await fn(); reload(); });
  function card(c) {
    const result = h("div", { class: "small" });
    const test = guard(async () => {
      result.className = "small";
      setKids(result, busy("Đang kiểm tra…"));
      let r;
      try { r = await api("POST", `/api/connections/${c.id}/test`); } catch (e) { setKids(result); throw e; }  // không để spinner quay mãi khi request lỗi
      result.className = "small " + (r.ok ? "ok-text" : "warn-text");
      result.textContent = (r.ok ? "Kết nối được: " + JSON.stringify(r.detail) : "Lỗi: " + r.error) + (r.route ? ` · đường đi: ${r.route}` : "");
    });
    const rbacBox = h("div");
    const rbacOpen = `rbac-open:${c.id}`;
    const toggleRbac = () => {
      const open = !rbacBox.childElementCount;
      store.set(rbacOpen, open);
      setKids(rbacBox, ...(open ? [k8sRbacPanel(c)] : []));
    };
    if (c.template === "k8s" && store.get(rbacOpen, false)) addKids(rbacBox, k8sRbacPanel(c));
    const remove = h("button", { type: "button", class: "danger small-btn", onclick: () => {
      if (remove.dataset.confirm) action(c, async () => { const r = await api("DELETE", `/api/connections/${c.id}`); if (r.warning) toast(r.warning, true); })();
      else { remove.dataset.confirm = "1"; remove.textContent = "Bấm lần nữa để gỡ"; setTimeout(() => { delete remove.dataset.confirm; remove.textContent = "Gỡ"; }, 4000); }
    } }, "Gỡ");
    return h("div", { class: "card" },
      h("div", { class: "tool-head" }, h("strong", {}, c.title), h("code", {}, c.name), h("span", { class: "badge" }, c.template_title), h("span", { class: "badge " + (c.personal ? "info" : "") }, c.personal ? "cá nhân" : "dùng chung"),
        badge(c.status === "active" ? "active" : "disabled"), c.allow_private ? h("span", { class: "badge warn" }, "mạng nội bộ") : null, h("span", { class: "spacer" }),
        h("button", { type: "button", class: "ghost small-btn", onclick: test }, "Kiểm tra"),
        c.template === "k8s" && h("button", { type: "button", class: "ghost small-btn", onclick: toggleRbac }, "Quyền Kubernetes"),
        c.can_manage && h("button", { type: "button", class: "ghost small-btn", onclick: () => { openEditor(c); editor.scrollIntoView(); } }, "Sửa"),
        c.can_manage && h("button", { type: "button", class: "ghost small-btn", onclick: action(c, () => api("PATCH", `/api/connections/${c.id}`, { status: c.status === "active" ? "disabled" : "active" })) }, c.status === "active" ? "Tạm tắt" : "Bật lại"),
        c.can_manage && remove),
      h("div", { class: "small" }, `Môi trường: ${c.envs.join(", ")}` + (c.personal ? "" : ` · vai trò: ${c.roles.join(", ")}`)),
      h("div", { class: "tool-perms" }, c.tools.map((t) => h("span", { class: "badge " + (t.enabled ? "ok" : "") }, `${t.tool.split(".")[1]} ${t.risk_tier}${t.enabled ? "" : " · tắt"}`))),
      result, rbacBox);
  }
  const closeEditor = () => { store.set("conn:editing", null); reload(); };
  function openEditor(c) { store.set("conn:editing", c.id); setKids(editor, connectionForm(templates, c, closeEditor)); }
  const shared = list.filter((c) => !c.personal), mine = list.filter((c) => c.personal);
  const reopen = list.find((c) => c.id === store.get("conn:editing", null) && c.can_manage);
  if (reopen) openEditor(reopen);
  addKids(main, h("h2", {}, "Kết nối công cụ"),
    h("p", { class: "small" }, "Tạo công cụ thật từ template bằng tham số của riêng bạn. Kết nối dùng chung do admin tạo và quản lý; kết nối cá nhân chỉ bạn thấy và dùng. Thông tin bí mật được mã hóa và không hiển thị lại. Kết nối vào mạng nội bộ cần org admin cho phép."),
    connectionForm(templates, null, reload), editor,
    h("h2", {}, `Dùng chung (${shared.length})`), ...(shared.length ? shared.map(card) : [h("div", { class: "card muted" }, "Chưa có kết nối dùng chung.")]),
    h("h2", {}, `Của bạn (${mine.length})`), ...(mine.length ? mine.map(card) : [h("div", { class: "card muted" }, "Bạn chưa có kết nối cá nhân.")]));
};

function learnedToolsSection(personalOnly) {
  const box = h("div", {}, h("div", { class: "card muted" }, busy("Đang tải…")));
  const tierClass = (r) => ({ R3: "warn", R4: "bad" }[r] || "");
  const load = async () => {
    const list = (await api("GET", "/api/learned-tools")).filter((t) => (personalOnly ? t.personal : !t.personal));
    setKids(box, ...(list.length ? list.map(item) : [h("div", { class: "card muted" }, personalOnly
      ? "Bạn chưa có công cụ riêng. Trong chat, bấm biểu tượng lưu dưới một câu trả lời có chạy công cụ để tạo."
      : "Chưa có công cụ học được. Hệ thống tự đề xuất khi một chuỗi thao tác giống nhau thành công từ 3 lần.")]));
  };
  const set = (t, status) => guard(async () => { await api("PATCH", `/api/learned-tools/${t.id}`, { status }); toast("Đã cập nhật"); await load(); });
  function item(t) {
    const canEdit = t.personal || isAdmin();
    const actions = !canEdit ? [] : t.status === "proposed" ? [h("button", { type: "button", class: "small-btn", onclick: set(t, "active") }, "Duyệt"), h("button", { type: "button", class: "danger small-btn", onclick: set(t, "rejected") }, "Từ chối")]
      : t.status === "active" ? [h("button", { type: "button", class: "ghost small-btn", onclick: set(t, "disabled") }, "Tắt")]
      : t.status === "disabled" ? [h("button", { type: "button", class: "small-btn", onclick: set(t, "active") }, "Bật")] : [];
    const params = Object.entries(t.params);
    return h("div", { class: "card" },
      h("div", { class: "tool-head" }, h("code", {}, t.name), badge(t.status), h("span", { class: "badge " + tierClass(t.risk_tier) }, t.risk_tier),
        h("span", { class: "badge" }, { auto: "tự đề xuất", agent: "agent đề xuất", manual: "lưu từ chat" }[t.origin] || t.origin),
        t.success_rate !== null && h("span", { class: "small" }, `thành công ${Math.round(t.success_rate * 100)}% trên ${t.uses} lần`), h("span", { class: "spacer" }), actions),
      h("div", { class: "small" }, t.description), h("div", { class: "small" }, "Các bước: ", t.flow),
      params.length > 0 && h("div", { class: "small" }, "Tham số: " + params.map(([k, v]) => `${k} (${v.type}${v.description ? `, ${v.description}` : ""})`).join("; ")),
      t.note && h("div", { class: "small warn-text" }, t.note),
      t.status === "proposed" && t.steps.some((st) => st.tool.endsWith(".script")) && h("div", { class: "small warn-text" },
        "Có bước data.script: sau khi duyệt, code bên dưới chạy mỗi lần dùng công cụ mà không hỏi lại. Hãy đọc kỹ code."),
      h("details", { open: t.status === "proposed" }, h("summary", { class: "small" }, "Chi tiết các bước"),
        t.steps.map((st, i) => h("div", { class: "learned-step" }, h("div", { class: "small" }, `${i + 1}. `, h("code", {}, st.tool)),
          Object.entries(st.args).map(([k, v]) => typeof v === "string" && v.includes("\n")
            ? h("div", {}, h("span", { class: "muted small" }, k + ":"), h("pre", { class: "approval-code" }, v))
            : h("div", { class: "small" }, h("span", { class: "muted" }, k + ": "), h("code", {}, typeof v === "string" ? v : JSON.stringify(v))))))));
  }
  load().catch((e) => toast(e.message, true));
  return h("div", {}, h("p", { class: "small" }, "Mỗi bước của công cụ học được vẫn đi qua gateway với quyền của người gọi, nên không tạo thêm quyền. Tỷ lệ thành công tụt dưới 50% thì công cụ tự tắt."), box);
}

function personalSection() {
  const wrap = h("div");
  (async () => {
    const [p, memories] = await Promise.all([api("GET", "/api/me/profile"), api("GET", "/api/me/memories")]);
    const instructions = h("textarea", { "aria-label": "Chỉ dẫn cá nhân", maxlength: 2000, value: p.instructions, placeholder: "Ví dụ: tôi là kỹ sư SRE, ưu tiên lệnh kubectl, luôn nêu rủi ro trước khi đề xuất thay đổi" });
    const style = h("select", { "aria-label": "Phong cách trả lời" }, [["default", "Mặc định"], ["concise", "Ngắn gọn"], ["detailed", "Chi tiết"]].map(([v, l]) => h("option", { value: v, selected: v === p.style }, l)));
    const env = h("select", { "aria-label": "Môi trường mặc định" }, ["dev", "staging", "prod"].map((e) => h("option", { selected: e === p.default_env }, e)));
    const save = guard(async (ev) => {
      ev.preventDefault();
      const r = await api("PUT", "/api/me/profile", { instructions: instructions.value, style: style.value, default_env: env.value });
      state.me.profile = r;
      state.env = r.default_env;
      toast("Đã lưu cá nhân hóa");
    });
    const forget = (m) => guard(async () => { await api("DELETE", `/api/me/memories/${m.id}`); toast("Agent đã quên ký ức này"); wrap.replaceWith(personalSection()); });
    addKids(wrap, h("h2", {}, "Cá nhân hóa"),
      h("form", { class: "card", onsubmit: save }, h("label", { class: "field" }, "Chỉ dẫn cho agent (chỉ áp dụng khi bạn chat)", instructions),
        h("div", { class: "row" }, h("label", { class: "field" }, "Phong cách trả lời", style), h("label", { class: "field" }, "Môi trường mặc định", env)),
        h("div", { class: "row" }, h("button", { type: "submit" }, "Lưu"))),
      h("h2", {}, `Ký ức của bạn (${memories.length})`),
      h("div", { class: "card" }, memories.length ? memories.map((m) => h("div", { class: "tool-item" },
        h("div", { class: "tool-head" }, h("span", { class: "badge info" }, m.kind), badge(m.status), h("span", { class: "small" }, fmtTime(m.recorded_at)), h("span", { class: "spacer" }),
          h("button", { type: "button", class: "ghost small-btn", onclick: forget(m) }, "Quên")), h("div", {}, m.content)))
        : h("div", { class: "muted" }, "Agent chưa ghi nhớ gì riêng về bạn. Nói với agent kiểu “tôi thích…”, “tôi muốn…” để nó ghi nhớ."),
        h("p", { class: "small" }, "Ký ức cá nhân chỉ bạn thấy, chỉ áp dụng khi bạn chat và được học ở lần chạy học nền tiếp theo.")),
      h("h2", {}, "Công cụ của bạn"), learnedToolsSection(true));
  })().catch((e) => toast(e.message, true));
  return wrap;
}


function ruleAutopublishCard(p) {
  const c = p.rule_autopublish;
  const mode = h("select", { "aria-label": "Chế độ tự publish quy ước", disabled: !isAdmin() },
    [["off", "Tắt: mọi quy ước chờ duyệt"], ["a", "Chỉ khi tenant_admin nói thẳng"], ["ab", "Tenant_admin nói thẳng, hoặc đã kiểm chứng qua sử dụng"]]
      .map(([v, l]) => h("option", { value: v, selected: c.mode === v }, l)));
  const num = (v, label) => h("input", { type: "number", min: 0, value: String(v), "aria-label": label, disabled: !isAdmin(), class: "narrow" });
  const eps = num(c.min_episodes, "Số lượt chat xác nhận"), helpful = num(c.min_helpful, "Số lần hữu ích"), days = num(c.min_days, "Số ngày đã hiệu lực");
  const save = guard(async () => {
    await api("PATCH", "/api/tenant", { rule_autopublish: { mode: mode.value, min_episodes: Number(eps.value), min_helpful: Number(helpful.value), min_days: Number(days.value) } });
    toast("Đã lưu"); renderShell();
  });
  return h("div", { class: "card" },
    h("p", { class: "small" }, "Quy ước tự vào khối core khi: nguồn không phải nội dung ngoài, không mâu thuẫn hay trùng ý với quy ước đang dùng, không nói về cơ chế của agent, khối core còn trong ngân sách, điểm golden task không giảm; và hoặc do tenant_admin nói thẳng, hoặc đã kiểm chứng qua sử dụng theo các ngưỡng dưới đây. Quy ước tự publish bị tính có hại trong 14 ngày đầu sẽ tự rút khỏi core và chờ duyệt. Quy ước hết hiệu lực luôn được gỡ tự động."),
    h("div", { class: "row" }, mode),
    h("div", { class: "row small" }, "Kiểm chứng qua sử dụng: ít nhất", eps, "lượt chat xác nhận,", helpful, "lần hữu ích, 0 lần có hại, đã hiệu lực", days, "ngày"),
    isAdmin() && h("div", { class: "row" }, h("button", { onclick: save }, "Lưu")));
}

// Cột hoàn tác của danh sách công cụ: kagent tự suy ra cách hoàn tác hay không, và đã có bao nhiêu bằng chứng.
function undoText(t) {
  if (!t.undo_supported) return "Không hoàn tác được tự động → lệnh ghi luôn hỏi" + (t.rollback_hint ? ` (gợi ý: ${t.rollback_hint})` : "");
  return "Hoàn tác: kagent tự suy ra từ trạng thái trước khi chạy · " + (t.undo_evidence ? `${t.undo_evidence} lần hoàn tác/diễn tập OK` : "chưa có bằng chứng");
}

// Tự chạy cần hoàn tác được: thời gian kiểm tra, tự hoàn tác, thùng rác, ngưỡng sao lưu, diễn tập, loại hoàn tác tin cậy.
function undoCard(p) {
  const u = p.undo;
  const num = (v, label, min, max) => h("input", { type: "number", min, max, value: v, class: "narrow", "aria-label": label, disabled: !isAdmin() });
  const verify = num(u.verify_minutes, "Thời gian kiểm tra sau khi chạy (phút)", 1, 120), days = num(u.trash_days, "Số ngày giữ trong thùng rác", 1, 90),
    rows = num(u.max_rows, "Số dòng sao lưu tối đa", 0, 10000000);
  const box = (on, label) => h("input", { type: "checkbox", checked: on, disabled: !isAdmin(), "aria-label": label });
  const revert = box(u.auto_revert, "Tự hoàn tác khi kiểm tra không đạt"), dDev = box(u.drill.dev, "Diễn tập ở dev"), dStg = box(u.drill.staging, "Diễn tập ở staging");
  const known = [...new Set([...Object.keys(u.recipes), ...u.trusted])].sort();
  const trusted = new Set(u.trusted);
  const recipeRows = known.map((r) => {
    const n = u.recipes[r] || { revert: 0, drill: 0 };
    const cb = h("input", { type: "checkbox", checked: trusted.has(r), disabled: !isAdmin(), "aria-label": `Tin cậy ${r}`, onchange: (e) => (e.target.checked ? trusted.add(r) : trusted.delete(r)) });
    return h("tr", {}, h("td", {}, h("code", {}, r)), h("td", {}, String(n.revert)), h("td", {}, String(n.drill)), h("td", {}, cb));
  });
  const save = guard(async () => {
    await api("PATCH", "/api/tenant", { undo_settings: { verify_minutes: Number(verify.value), trash_days: Number(days.value), max_rows: Number(rows.value),
      auto_revert: revert.checked, drill: { dev: dDev.checked, staging: dStg.checked, prod: false }, trusted: [...trusted] } });
    toast("Đã lưu"); renderShell();
  });
  const line = (...kids) => h("label", { class: "row small" }, ...kids);
  return h("div", { class: "card" },
    h("p", { class: "small" }, "Ô \"auto mode\" chỉ tự chạy lệnh ghi khi đủ: (a) kagent tự dựng được lệnh hoàn tác từ trạng thái đọc ngay trước khi chạy; (b) lệnh chỉ chạm đúng các đối tượng nêu tên; (c) có bằng chứng lệnh hoàn tác chạy được: chạy thử qua, đã từng hoàn tác hoặc diễn tập thành công, hoặc admin đánh dấu tin cậy bên dưới; và đối tượng không đang rollout dở hay có thay đổi khác chờ kiểm tra. Thiếu điều kiện nào thì hỏi người. Xoá index, xoá bảng thành xoá mềm; xoá/sửa dòng được sao lưu trước."),
    line("Kiểm tra sau khi chạy", verify, "phút: đối tượng phải sẵn sàng và không có cảnh báo mới trên namespace của nó"),
    line(revert, "Tự hoàn tác lệnh đã tự chạy khi kiểm tra không đạt"),
    line("Giữ trong thùng rác", days, "ngày rồi báo đến hạn xoá thật (người có quyền duyệt bấm xoá)"),
    line("Sao lưu tối đa", rows, "dòng; nhiều hơn thì coi là không hoàn tác được"),
    line("Diễn tập lần đầu của mỗi loại hoàn tác ở", dDev, "dev", dStg, "staging", h("span", { class: "muted" }, "(hoàn tác rồi chạy lại ngay sau lệnh; không bao giờ ở prod)")),
    known.length > 0 && h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Loại hoàn tác"), h("th", {}, "Hoàn tác OK"), h("th", {}, "Diễn tập OK"), h("th", {}, "Tin cậy"))),
      h("tbody", {}, recipeRows))),
    isAdmin() && h("div", { class: "row" }, h("button", { onclick: save }, "Lưu")));
}

// Nút đỏ: dừng mọi lệnh ghi tự chạy và việc tự hoàn tác; lệnh đọc vẫn chạy.
function stopBar(p) {
  const u = p.undo;
  const set = (paused) => guard(async () => { await api("PATCH", "/api/tenant", { undo_settings: { paused } }); toast(paused ? "Đã dừng mọi lệnh ghi tự chạy" : "Đã bật lại"); renderShell(); });
  return h("div", { class: "card stop-bar" + (u.paused ? " paused" : "") },
    isAdmin() && (u.paused ? h("button", { onclick: set(false) }, "Bật lại lệnh tự chạy") : h("button", { class: "danger", onclick: set(true) }, "■ Dừng mọi lệnh tự chạy")),
    h("span", { class: "small" }, u.paused ? "Đang dừng: mọi lệnh ghi phải có người duyệt, không tự hoàn tác." : `Đang bật · ${u.verifying} lệnh đang chờ kiểm tra`));
}

async function trashCard() {
  const items = await api("GET", "/api/trash");
  const card = h("div", { class: "card scroll" });
  if (!items.length) { addKids(card, h("div", { class: "small muted" }, "Thùng rác trống.")); return card; }
  const due = (it) => it.purge_at && new Date(it.purge_at) <= new Date();
  addKids(card, h("table", {}, h("thead", {}, h("tr", {}, ["Đối tượng", "Cách giữ", "Lúc", "Môi trường", "Xoá thật", ""].map((x) => h("th", {}, x)))),
    h("tbody", {}, items.map((it) => h("tr", {},
      h("td", {}, it.targets.join(", ")), h("td", { class: "small" }, it.summary || ""), h("td", {}, fmtTime(it.created_at)), h("td", {}, it.env),
      h("td", { class: due(it) ? "warn-text" : "" }, due(it) ? "đến hạn" : `từ ${fmtTime(it.purge_at)}`),
      h("td", {}, it.can_revert && h("button", { class: "ghost small-btn", onclick: guard(async () => { await api("POST", `/api/changes/${it.id}/undo`, {}); toast("Đã khôi phục"); renderShell(); }) }, "Khôi phục"),
        it.can_purge && h("button", { class: "danger small-btn", onclick: guard(async () => {
          if (!window.confirm(`Xoá thật ${it.targets.join(", ")}? Không khôi phục được nữa.`)) return;
          await api("POST", `/api/changes/${it.id}/purge`); toast("Đã xoá thật"); renderShell();
        }) }, "Xoá thật…")))))));
  return card;
}

const AUTO_SECTIONS = [
  ["environment", "Môi trường", "Mô tả hạ tầng: cụm, namespace, project nào là prod, đích nào tin cậy."],
  ["hard_deny", "Cấm luôn", "Chặn kể cả khi người dùng yêu cầu."],
  ["soft_deny", "Chặn trừ khi được yêu cầu đích danh", "Chặn, trừ khi người dùng nêu đúng hành động và đối tượng, hoặc khớp một luật cho phép."],
  ["allow", "Cho phép", "Ngoại lệ của mục trên."],
];

function autoModeCard(p) {
  const am = p.auto_mode;
  const boxes = {};
  const rows = AUTO_SECTIONS.map(([key, title, hint]) => {
    const custom = am.custom[key];
    boxes[key] = h("textarea", { class: "code", rows: 6, disabled: !isAdmin(), "aria-label": title,
      value: (custom || ["$defaults"]).join("\n") });
    return h("div", {}, h("h3", {}, title), h("div", { class: "small" }, hint),
      boxes[key],
      h("details", {}, h("summary", { class: "small" }, `Đang áp dụng (${am.effective[key].length} luật)`), h("ul", { class: "small" }, am.effective[key].map((r) => h("li", {}, r)))));
  });
  const save = guard(async () => {
    const auto_mode = Object.fromEntries(AUTO_SECTIONS.map(([key]) => [key, boxes[key].value.split("\n").map((x) => x.trim()).filter(Boolean)]));
    await api("PATCH", "/api/tenant", { auto_mode });
    toast("Đã lưu");
    renderShell();
  });
  return h("div", { class: "card" },
    h("p", { class: "small" }, "Lệnh rơi vào ô \"auto mode\" của ma trận được một model xét thay người duyệt. Model chỉ đọc lời người dùng, các lệnh họ đã duyệt và các lệnh agent đã gọi; không đọc kết quả công cụ, nên log hay trang web độc hại không lái được nó. Cho qua thì lệnh chạy; chặn thì agent nhận lý do và tìm cách khác; chặn 3 lần liên tiếp (hoặc 20 lần trong một lượt) thì chuyển sang hỏi người. Model lỗi hoặc chưa cấu hình LLM thì hỏi người. Mỗi dòng là một luật viết bằng chữ; dòng $defaults chèn các luật mặc định."),
    rows,
    isAdmin() && h("div", { class: "row" }, h("button", { onclick: save }, "Lưu")));
}

async function policyView(main) {
  const [tools, p] = await Promise.all([api("GET", "/api/tools"), api("GET", "/api/policy")]);
  const cellText = (c) => (typeof c === "string" ? ({ auto: "tự động", judge: "auto mode", ask: "luôn hỏi", block: "chặn" }[c]) : `hỏi, ${c.ask} người`);
  const editor = persist(h("textarea", { class: "code", value: JSON.stringify(p.overlay, null, 2), "aria-label": "Overlay chính sách" }), "policy:overlay");
  const save = guard(async () => { let overlay; try { overlay = JSON.parse(editor.value); } catch { throw new Error("JSON không hợp lệ"); } await api("PUT", "/api/policy", { overlay }); store.set("policy:overlay", null); toast("Đã lưu chính sách"); renderShell(); });
  const setFlag = (key, value) => guard(async () => { await api("PATCH", "/api/tenant", { [key]: value }); renderShell(); });
  const simTool = h("select", { "aria-label": "Công cụ" }, tools.map((t) => h("option", {}, t.tool)));
  const simEnv = h("select", { "aria-label": "Môi trường" }, ["dev", "staging", "prod"].map((x) => h("option", {}, x)));
  const simOut = h("div", { class: "small" });
  const sim = guard(async () => { const r = await api("POST", "/api/policy/simulate", { tool: simTool.value, env: simEnv.value }); simOut.textContent = `${r.risk_tier} → ${r.outcome === "judge" ? "auto mode xét" : LABEL[r.outcome] || r.outcome}${r.required ? `, ${r.required} người duyệt` : ""}. ${r.reasons.join("; ")}`; });
  const tiers = ["R0", "R1", "R2", "R3", "R4"];
  addKids(main, stopBar(p), h("h2", {}, "Chính sách hiệu lực"), h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Mức"), ["dev", "staging", "prod"].map((e) => h("th", {}, `${e} (${p.effective.environments[e].auto_execute})`)))),
    h("tbody", {}, tiers.map((t) => h("tr", {}, h("td", {}, t), ["dev", "staging", "prod"].map((e) => h("td", {}, cellText(p.effective.matrix[t][e]))))))),
    h("div", { class: "row" }, h("span", { class: "small" }, "Luật chặn: " + (p.effective.deny.map((d) => `${d.tool}@${d.env}`).join(", ") || "không"))),
    isAdmin() && h("div", { class: "row" }, h("button", { class: "ghost", onclick: setFlag("change_freeze", !p.change_freeze) }, p.change_freeze ? "Tắt change freeze" : "Bật change freeze"), h("button", { class: "ghost", onclick: setFlag("auto_promote", !p.auto_promote) }, p.auto_promote ? "Tắt tự động thăng hạng" : "Bật tự động thăng hạng"),
      h("button", { class: "ghost", onclick: setFlag("self_approve", !p.self_approve) }, p.self_approve ? "Bắt buộc người khác duyệt lệnh" : "Cho người dùng tự duyệt lệnh")),
    h("div", { class: "small" }, p.self_approve ? "Người dùng tự xác nhận lệnh cần duyệt của chính mình. Thay đổi công cụ dùng chung vẫn cần admin." : "Lệnh cần duyệt phải do người có quyền duyệt khác xác nhận; R3 ở prod cần 2 người.")),
    h("div", { class: "small" }, `"Cho phép trong ${p.grant_ttl_minutes} phút" khi duyệt: cùng công cụ, cùng namespace thì tự chạy, trừ khi có lý do bắt buộc hỏi (change freeze, quota ghi, chạy thử lỗi).`),
    h("h2", {}, "Luật auto mode"), autoModeCard(p),
    h("h2", {}, "Tự chạy cần hoàn tác được"), undoCard(p),
    h("h2", {}, "Thùng rác"), await trashCard(),
    h("h2", {}, "Tự publish quy ước core"), ruleAutopublishCard(p),
    h("h2", {}, "Thử quyết định"), h("div", { class: "card" }, h("div", { class: "row" }, simTool, simEnv, h("button", { class: "ghost", onclick: sim }, "Thử")), simOut),
    isAdmin() && h("h2", {}, "Overlay của tenant (chỉ được siết)"), isAdmin() && h("div", { class: "card" }, editor, h("div", { class: "row" }, h("button", { onclick: save }, "Lưu"))));
}

const KIND_CLASS = { rule: "s1", fact: "s2", procedure: "s3", lesson: "s4", insight: "s5" };
const KIND_LABEL = { rule: "quy ước", fact: "sự thật", procedure: "thủ tục", lesson: "bài học", insight: "chiến lược" };

// Bố cục lực (đẩy nhau, kéo theo cạnh, hút nhẹ về tâm, chống chồng); đủ cho vài trăm nút, không cần thư viện.
// Khởi tạo theo góc vàng để cùng dữ liệu luôn cho cùng bố cục.
function forceLayout(nodes, links, W, H) {
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  nodes.forEach((n, i) => { const a = i * 2.39996, r = 18 * Math.sqrt(i + 1); n.x = W / 2 + Math.cos(a) * r; n.y = H / 2 + Math.sin(a) * r; });
  const L = links.map((l) => [idx.get(l.source), idx.get(l.target)]).filter(([a, b]) => a !== undefined && b !== undefined);
  const N = 300;
  for (let it = 0; it < N; it++) {
    const t = 1 - it / N;
    const fx = new Float64Array(nodes.length), fy = new Float64Array(nodes.length);
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      let dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
      if (!dx && !dy) { dx = 0.5; dy = 0.5; }
      const d2 = dx * dx + dy * dy, d = Math.sqrt(d2), min = nodes[i].r + nodes[j].r + 10;
      let f = (nodes[i].entity || nodes[j].entity ? 1500 : 800) / Math.max(d2, 30);
      if (d < min) f += (min - d) * 0.6 / d;
      fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
    }
    for (const [a, b] of L) {
      const dx = nodes[b].x - nodes[a].x, dy = nodes[b].y - nodes[a].y, d = Math.sqrt(dx * dx + dy * dy) || 1, f = (d - 42) * 0.08;
      fx[a] += (dx / d) * f; fy[a] += (dy / d) * f; fx[b] -= (dx / d) * f; fy[b] -= (dy / d) * f;
    }
    nodes.forEach((n, i) => {
      fx[i] += (W / 2 - n.x) * 0.02; fy[i] += (H / 2 - n.y) * 0.02;
      n.x += Math.max(-25, Math.min(25, fx[i])) * t; n.y += Math.max(-25, Math.min(25, fy[i])) * t;
    });
  }
}

// Gợi ý gộp: tên giống nhau khi bỏ dấu câu, hoặc tên này chứa tên kia (đủ dài để không bắt nhầm).
const bare = (s) => s.replace(/^tool:/, "").replace(/[^a-z0-9à-ỹ]+/g, "");
function similarEntities(name, all) {
  const b = bare(name);
  if (b.length < 3 || name.startsWith("tool:")) return [];
  return all.filter((e) => e !== name && !e.startsWith("tool:")).filter((e) => { const o = bare(e); return o === b || (Math.min(o.length, b.length) >= 4 && (o.includes(b) || b.includes(o))); });
}

function graphCanvas({ nodes, links, W, H, onKu, onEntity, selected, highlight }) {
  const pos = new Map(nodes.map((n) => [n.id, n]));
  const adj = new Map(nodes.map((n) => [n.id, new Set([n.id])]));
  for (const l of links) { adj.get(l.source)?.add(l.target); adj.get(l.target)?.add(l.source); }
  let view = { k: 1, x: 0, y: 0 };
  const layer = svg("g", {});
  const linkEls = links.map((l) => { const a = pos.get(l.source), b = pos.get(l.target); return a && b ? { l, el: svg("line", { class: "kg-link " + l.rel }) } : null; }).filter(Boolean);
  const nodeEls = new Map();
  for (const n of nodes) {
    const g = svg("g", { class: n.entity ? "kg-ent" : "kg-node", tabindex: 0, role: "button",
      "aria-label": n.entity ? "Thực thể " + n.entity : n.ku.title });
    if (n.entity) {
      addKids(g, svg("rect", { x: -n.r, y: -n.r, width: n.r * 2, height: n.r * 2, rx: 3 }),
        svg("text", { class: "kg-label", x: n.r + 5, y: 5 }, n.entity.length > 30 ? n.entity.slice(0, 29) + "…" : n.entity), svg("title", {}, `${n.entity} · ${n.deg} tri thức`));
    } else {
      addKids(g, svg("circle", { class: `kg-ku ${KIND_CLASS[n.ku.kind]}${n.ku.status !== "active" ? " cand" : ""}`, r: n.r }), svg("text", { class: "kg-label kg-ku-label" + (n.pin ? " pin" : ""), x: n.r + 5, y: 5 },
        n.ku.title.length > 40 ? n.ku.title.slice(0, 39) + "…" : n.ku.title), svg("title", {}, n.ku.title));
    }
    nodeEls.set(n.id, g);
  }
  addKids(layer, ...linkEls.map((x) => x.el), ...nodeEls.values());
  const el = svg("svg", { class: "kg-svg", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Đồ thị tri thức, cuộn để phóng to, kéo để di chuyển" }, layer);

  const placeLinks = (only) => linkEls.forEach(({ l, el: line }) => {
    if (only && l.source !== only && l.target !== only) return;
    const a = pos.get(l.source), b = pos.get(l.target);
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y); line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
  });
  const placeNode = (n) => nodeEls.get(n.id).setAttribute("transform", `translate(${n.x},${n.y})`);
  const apply = () => { layer.setAttribute("transform", `translate(${view.x},${view.y}) scale(${view.k})`); el.classList.toggle("zoomed", view.k >= 1.3); };
  nodes.forEach(placeNode); placeLinks();

  function fit() {
    if (!nodes.length) return;
    const xs = nodes.map((n) => n.x), ys = nodes.map((n) => n.y), pad = 40;
    const x0 = Math.min(...xs) - pad, x1 = Math.max(...xs) + pad + 120, y0 = Math.min(...ys) - pad, y1 = Math.max(...ys) + pad;
    const k = Math.min(2.5, W / (x1 - x0), H / (y1 - y0));
    view = { k, x: (W - (x1 - x0) * k) / 2 - x0 * k, y: (H - (y1 - y0) * k) / 2 - y0 * k };
    apply();
  }
  const toSvg = (ev) => { const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(el.getScreenCTM().inverse()); return { x: p.x, y: p.y }; };
  function zoomAt(factor, cx = W / 2, cy = H / 2) {
    const k = Math.max(0.2, Math.min(6, view.k * factor)), r = k / view.k;
    view = { k, x: cx - (cx - view.x) * r, y: cy - (cy - view.y) * r };
    apply();
  }
  el.addEventListener("wheel", (ev) => { ev.preventDefault(); const p = toSvg(ev); zoomAt(Math.exp(-ev.deltaY * 0.0015), p.x, p.y); }, { passive: false });

  let drag = null;
  el.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    const g = ev.target.closest(".kg-node, .kg-ent"), id = g && [...nodeEls].find(([, v]) => v === g)?.[0];
    drag = { id, start: toSvg(ev), view: { ...view }, moved: false, n: id ? { x: pos.get(id).x, y: pos.get(id).y } : null };
    el.setPointerCapture(ev.pointerId);
    el.classList.add(id ? "dragging-node" : "panning");
  });
  el.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const p = toSvg(ev), dx = p.x - drag.start.x, dy = p.y - drag.start.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (!drag.moved) return;
    if (drag.id) {
      const n = pos.get(drag.id);
      n.x = drag.n.x + dx / view.k; n.y = drag.n.y + dy / view.k;
      placeNode(n); placeLinks(drag.id);
    } else {
      view = { ...view, x: drag.view.x + dx, y: drag.view.y + dy }; apply();
    }
  });
  const end = () => {
    if (drag && !drag.moved && drag.id) {
      const n = pos.get(drag.id);
      select(drag.id);
      n.entity ? onEntity(n.entity) : onKu(n.ku);
    }
    drag = null; el.classList.remove("dragging-node", "panning");
  };
  el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  el.addEventListener("dblclick", (ev) => { if (!ev.target.closest(".kg-node, .kg-ent")) fit(); });

  // Rê chuột lên một nút: làm mờ phần không nối với nó để thấy cụm liên quan; rời chuột thì về nút đang chọn (highlight).
  let pinned = highlight || null;
  const focus = (id) => {
    id = id || pinned;
    const near = id ? adj.get(id) : null;
    el.classList.toggle("focus", !!near);
    nodeEls.forEach((g, nid) => g.classList.toggle("near", !!near && near.has(nid)));
    linkEls.forEach(({ l, el: line }) => line.classList.toggle("near", !!near && (l.source === id || l.target === id)));
  };
  function select(id) { nodeEls.forEach((g, nid) => g.classList.toggle("sel", nid === id)); }
  nodeEls.forEach((g, id) => {
    g.addEventListener("pointerenter", () => !drag && focus(id));
    g.addEventListener("pointerleave", () => !drag && focus(null));
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); const n = pos.get(id); select(id); n.entity ? onEntity(n.entity) : onKu(n.ku); } });
  });
  if (selected) select(selected);
  focus(null);

  const tools = h("div", { class: "kg-tools" },
    h("button", { type: "button", class: "icon-btn", title: "Phóng to", "aria-label": "Phóng to", onclick: () => zoomAt(1.3) }, "+"),
    h("button", { type: "button", class: "icon-btn", title: "Thu nhỏ", "aria-label": "Thu nhỏ", onclick: () => zoomAt(1 / 1.3) }, "−"),
    h("button", { type: "button", class: "icon-btn", title: "Vừa khung (nháy đúp nền)", "aria-label": "Vừa khung", onclick: fit }, "⤢"));
  fit();
  const canvas = h("div", { class: "kg-canvas" }, el, tools);
  canvas.has = (id) => pos.has(id);
  canvas.highlight = (id) => { pinned = id; select(id); focus(null); };
  return canvas;
}

async function knowledgeGraphView(main) {
  const q = persist(h("input", { class: "wide", placeholder: "Tra cứu như khi hỏi agent, ví dụ: đếm lỗi 500 của svc-backend", "aria-label": "Tra cứu tri thức" }), "kg:q");
  const withCandidates = persist(h("input", { type: "checkbox", "aria-label": "Gồm cả ứng viên" }), "kg:cand");
  let entity = store.get("kg:entity", "");
  let allEntities = [];
  const chips = h("div", { class: "tool-perms" });
  const graphBox = h("div", { class: "card kg-graph" });
  const hint = () => h("div", { class: "small muted" }, "Chọn một nút để xem chi tiết. Cuộn chuột để phóng to, kéo nền để di chuyển, kéo nút để sắp lại, nháy đúp nền để vừa khung.");
  const detail = h("div", { class: "card kg-detail" }, hint());
  const list = h("div");
  let canvas = null, data = null;
  // Chọn thực thể chỉ làm nổi phần nối với nó, giữ nguyên đồ thị; chỉ tải lại khi thực thể chưa có trên đồ thị.
  let kuSel = null;
  const setEntity = (e) => {
    entity = e; store.set("kg:entity", e); kuSel = null;
    if (e && q.value) { q.value = ""; store.set("kg:q", null); return load(); }
    if (canvas && (!e || canvas.has("e:" + e))) { canvas.highlight(e ? "e:" + e : null); side(); return; }
    load();
  };
  // Bấm lại đúng nút đang chọn thì bỏ chọn: tri thức về lại thực thể đang chọn (nếu có), thực thể về đồ thị bình thường.
  const pickKu = (k) => {
    if (kuSel === k.id) {
      kuSel = null;
      canvas?.highlight(entity ? "e:" + entity : null);
      return entity ? showEntity(entity) : setKids(detail, hint());
    }
    kuSel = k.id;
    canvas?.highlight(k.id);
    showKu(k);
  };
  const pickEntity = (e) => setEntity(e === entity && !kuSel ? "" : e);

  function showKu(k) {
    setKids(detail, h("div", { class: "row" }, h("span", { class: `badge sw-badge ${KIND_CLASS[k.kind]}` }, KIND_LABEL[k.kind] || k.kind), badge(k.status),
      k.score !== undefined && h("span", { class: "badge info" }, `điểm ${k.score}`)),
      h("h3", {}, k.title), h("div", {}, k.content), k.applies_when && h("div", { class: "small" }, "Áp dụng: " + k.applies_when),
      h("div", { class: "small muted" }, `${k.source_trust} · ✓${k.helpful_cnt} ✗${k.harmful_cnt} · ${k.domain}`),
      k.entities.length > 0 && h("div", { class: "tool-perms" }, k.entities.map((e) => h("button", { type: "button", class: "chip", onclick: () => setEntity(e) }, e))));
  }

  function showEntity(name) {
    const n = allEntities.find((e) => e.entity === name)?.n || 0;
    const names = allEntities.map((e) => e.entity);
    const similar = similarEntities(name, names);
    const other = h("input", { list: "kg-entity-names", placeholder: "tên khác cần gộp vào", "aria-label": "Tên thực thể cần gộp" });
    const merge = (sources) => guard(async () => {
      const r = await api("POST", "/api/knowledge/entities/merge", { sources, target: name });
      toast(`Đã gộp ${r.merged.join(", ")} vào ${r.target} (${r.kus} tri thức)`);
      setEntity(r.target);
    });
    setKids(detail, h("div", { class: "row" }, h("span", { class: "badge" }, name.startsWith("tool:") ? "công cụ" : "thực thể"), h("span", { class: "small muted" }, `${n} tri thức`)),
      h("h3", {}, name),
      isAdmin() && !name.startsWith("tool:") && h("div", {},
        similar.length > 0 && h("div", { class: "small" }, "Tên có vẻ cùng một thứ:"),
        similar.length > 0 && h("div", { class: "tool-perms" }, similar.map((s) => h("button", { type: "button", class: "chip", title: `Gộp ${s} vào ${name}`, onclick: merge([s]) }, `+ ${s}`))),
        h("form", { class: "row", onsubmit: (ev) => { ev.preventDefault(); if (other.value.trim()) merge([other.value.trim()])(ev); } }, other, h("button", { type: "submit", class: "ghost" }, "Gộp vào đây")),
        h("datalist", { id: "kg-entity-names" }, names.filter((e) => e !== name && !e.startsWith("tool:")).map((e) => h("option", { value: e }))),
        h("div", { class: "small muted" }, "Gộp xong, tri thức học sau này dùng tên cũ cũng tự đổi sang tên này.")));
  }

  function draw(data) {
    const W = 760, H = 520;
    const deg = new Map();
    for (const k of data.nodes) for (const e of k.entities) deg.set(e, (deg.get(e) || 0) + 1);
    // Thực thể chỉ có một tri thức không giúp thấy liên hệ, trừ khi đang lọc theo nó.
    const ents = [...deg].filter(([e, n]) => n >= 2 || e === entity).map(([e, n]) => ({ id: "e:" + e, entity: e, deg: n, r: 8 + Math.min(8, n) }));
    const entSet = new Set(ents.map((e) => e.entity));
    const top = new Set(data.nodes.filter((k) => k.score !== undefined).slice(0, 5).map((k) => k.id));
    const nodes = [...ents, ...data.nodes.map((k) => ({ id: k.id, ku: k, pin: top.has(k.id),
      r: k.score !== undefined ? 8 + Math.min(7, k.score * 10) : 8 + Math.min(6, Math.sqrt(k.helpful_cnt || 0) * 1.8) }))];
    const links = [...data.nodes.flatMap((k) => k.entities.filter((e) => entSet.has(e)).map((e) => ({ source: k.id, target: "e:" + e, rel: "entity" }))),
      ...data.edges.map((e) => ({ source: e.src, target: e.dst, rel: e.rel }))];
    if (!data.nodes.length) return setKids(graphBox, h("div", { class: "muted" }, "Không có tri thức nào khớp."));
    forceLayout(nodes, links, W, H);
    setKids(graphBox, h("div", { class: "chart-legend" }, Object.entries(KIND_LABEL).map(([k, l]) => h("span", {}, h("span", { class: `sw ${KIND_CLASS[k]}` }), l)),
      h("span", {}, h("span", { class: "sw ent-sw" }), "thực thể"), h("span", { class: "small muted" }, "nét đứt: gộp từ · nét màu: cùng chủ đề · đỏ: mâu thuẫn · viền rỗng: ứng viên · phóng to để xem tên")),
      canvas = graphCanvas({ nodes, links, W, H, onKu: pickKu, onEntity: pickEntity,
        selected: entity ? "e:" + entity : null, highlight: entity ? "e:" + entity : null }));
  }

  async function load() {
    const params = new URLSearchParams({ q: q.value.trim(), entity: entity || "", include_candidates: withCandidates.checked ? "true" : "false" });
    data = await api("GET", "/api/knowledge/graph?" + params);
    allEntities = data.entities;
    canvas = null; kuSel = null;
    draw(data);
    side();
  }

  function side() {
    setKids(chips, h("span", { class: "small" }, "Thực thể:"), ...data.entities.slice(0, 24).map((e) =>
      h("button", { type: "button", class: "chip" + (e.entity === entity ? " on" : ""), onclick: () => setEntity(e.entity === entity ? "" : e.entity) }, `${e.entity} (${e.n})`)));
    if (entity) showEntity(entity); else setKids(detail, hint());
    const shown = entity && !q.value.trim() ? data.nodes.filter((k) => k.entities.includes(entity)) : data.nodes;
    const title = q.value.trim() ? `Agent sẽ nạp ${data.nodes.length} tri thức cho câu hỏi này (xếp theo điểm truy xuất)`
      : entity ? `${shown.length} tri thức về ${entity}` : `${data.nodes.length} tri thức${withCandidates.checked ? " (gồm ứng viên)" : " đang hiệu lực"}`;
    setKids(list, h("h3", {}, title), ...shown.slice(0, 50).map((k) => h("div", { class: "card kg-item", onclick: () => pickKu(k) },
      h("div", { class: "row" }, h("span", { class: `badge sw-badge ${KIND_CLASS[k.kind]}` }, KIND_LABEL[k.kind] || k.kind), k.status !== "active" && badge(k.status),
        k.score !== undefined && h("span", { class: "small muted" }, `điểm ${k.score}`), h("b", {}, k.title)),
      h("div", { class: "small" }, k.content))));
  }
  const search = guard(async (ev) => { ev.preventDefault(); entity = ""; store.set("kg:entity", null); await load(); });
  withCandidates.addEventListener("change", () => load());
  addKids(main, h("form", { class: "row", onsubmit: search }, q, h("button", { type: "submit" }, "Tra cứu"),
    h("label", { class: "small" }, withCandidates, " gồm ứng viên"), (q.value || entity) && h("button", { type: "button", class: "ghost", onclick: () => { q.value = ""; store.set("kg:q", null); setEntity(""); } }, "Xóa lọc")),
    chips, h("div", { class: "kg-split" }, graphBox, detail), list);
  await load();
}

async function toolCatalogView(main) {
  addKids(main, h("h2", {}, "Công cụ học được"), learnedToolsSection(false), h("h2", {}, "Danh mục công cụ"), toolCatalogSection());
}

VIEWS.tools = (main) => subTabs(main, [["connections", "Kết nối", connectionsView], ["catalog", "Danh mục & quyền", toolCatalogView]]);
// Nguồn ngoài episode (sources.py): tài liệu tải lên và bản ghi sự cố đã xử lý xong, mỗi đoạn tách thành KU qua Curator.
async function knowledgeSourcesView(main) {
  const SOURCE_KIND = { doc: "tài liệu", incident: "sự cố" };
  const list = h("div");
  const detail = h("div");
  const title = persist(h("input", { class: "wide", placeholder: "Tiêu đề, vd. Runbook khôi phục etcd", "aria-label": "Tiêu đề tài liệu" }), "src:title");
  const content = persist(h("textarea", { rows: 8, placeholder: "Dán nội dung markdown hoặc chọn file .md/.txt. Mỗi mục # / ## / ### được đọc riêng.", "aria-label": "Nội dung tài liệu" }), "src:content");
  const file = h("input", { type: "file", accept: ".md,.markdown,.txt,text/markdown,text/plain", "aria-label": "Chọn file", onchange: guard(async () => {
    const f = file.files[0];
    if (!f) return;
    content.value = await f.text();
    if (!title.value) title.value = f.name.replace(/\.[^.]+$/, "");
  }) });
  const activate = h("input", { type: "checkbox" });
  const showDoc = (id) => guard(async () => {
    const d = await api("GET", `/api/knowledge/sources/${id}`);
    const bySection = new Map();
    for (const ku of d.kus) for (const n of ku.sections) bySection.set(n, [...(bySection.get(n) || []), ku]);
    setKids(detail, h("div", { class: "card" }, h("div", { class: "row spread" }, h("h3", {}, d.title), h("span", {}, badge(d.status))),
      h("div", { class: "small" }, `${SOURCE_KIND[d.kind] || d.kind} · nguồn ${d.trust} · ${num(d.chars)} ký tự · ${d.chunks.length} đoạn · ${d.kus.length} KU`),
      d.result && d.result.errors && d.result.errors.length > 0 && h("div", { class: "small warn" }, d.result.errors.join("; ")),
      ...d.chunks.map((c, i) => h("details", {}, h("summary", {}, `${i + 1}. ${c.section} `, h("span", { class: "small muted" }, `${(bySection.get(i + 1) || []).length} KU`)),
        h("ul", { class: "small" }, (bySection.get(i + 1) || []).map((ku) => h("li", {}, badge(ku.status), " ", h("span", { class: "badge info" }, ku.kind), " ", ku.title))),
        h("pre", { class: "small" }, c.text)))));
  });
  async function load() {
    const docs = await api("GET", "/api/knowledge/sources");
    setKids(list, docs.length ? h("div", { class: "card scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Thời gian", "Tiêu đề", "Loại", "Trạng thái", "Đoạn", "KU", "Người tải"].map((x) => h("th", {}, x)))),
      h("tbody", {}, docs.map((d) => h("tr", { class: "click", onclick: showDoc(d.id) }, h("td", {}, fmtTime(d.created_at)), h("td", {}, d.title),
        h("td", {}, SOURCE_KIND[d.kind] || d.kind), h("td", {}, d.status === "running" || d.status === "pending" ? busy("đang đọc") : badge(d.status)),
        h("td", {}, d.result ? num(d.result.chunks || 0) : "—"), h("td", {}, d.result ? num(d.result.kus || 0) : "—"), h("td", {}, d.created_by || "")))))) :
      h("div", { class: "card muted" }, "Chưa có tài liệu nào. Sự cố đã xử lý xong cũng tự vào đây."));
    if (docs.some((d) => d.status === "pending" || d.status === "running")) setTimeout(() => { if (document.contains(list)) load().catch(() => {}); }, 3000);
  }
  const upload = guard(async () => {
    const d = await api("POST", "/api/knowledge/sources", { title: title.value, content: content.value, activate: activate.checked });
    title.value = content.value = ""; file.value = ""; store.set("src:title", null); store.set("src:content", null);
    toast(`Đang đọc "${d.title}"; KU tách ra sẽ là ứng viên${activate.checked ? " và được chấp nhận luôn" : ""}`);
    await load();
  });
  addKids(main, h("h2", {}, "Nguồn tri thức"),
    h("div", { class: "card" }, h("p", { class: "small" }, "Tài liệu được lưu nguyên bản; mỗi mục được tách thành KU, đi qua Curator (gộp với KU đã có) và cổng duyệt như tri thức học từ chat."),
      title, content, h("div", { class: "row" }, file, isAdmin() && h("label", { class: "small" }, activate, " Chấp nhận luôn các KU tách ra (tôi bảo đảm tài liệu này)"),
        h("span", { class: "spacer" }), h("button", { type: "button", onclick: upload }, "Tải lên và đọc"))),
    detail, list);
  await load();
}

VIEWS.knowledge = (main) => subTabs(main, [["graph", "Đồ thị (L3)", knowledgeGraphView], ["units", "Tri thức (L1)", knowledgeUnitsView],
  ["compiled", "Runbook (L2)", compiledView], ["sources", "Nguồn", knowledgeSourcesView]]);

const PURPOSE = { agent: "Agent trả lời", subagent: "Agent con", route: "Router", check_plan: "Kiểm tra độc lập", reflect: "Reflector", curate: "Curator", extract: "Đọc tài liệu", consolidate: "Tổng hợp KU", dedup: "Tìm KU trùng", judge: "Chấm kết quả", other: "Khác" };
const num = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("vi-VN"));

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
  kids.flat(Infinity).forEach((k) => k !== null && k !== undefined && k !== false && addKids(el, k));
  return el;
}
const compact = (n) => (n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k" : String(n));
const MAX_SERIES = 6;

// Cột chồng theo ngày, mỗi LLM một màu cố định. Màu gán theo thứ tự LLM xuất hiện lần đầu (không theo thứ hạng),
// nên một LLM giữ nguyên màu khi thứ hạng thay đổi; quá 6 LLM thì phần còn lại gộp vào "Khác".
function llmUsageChart(t) {
  if (!t.by_llm || !t.by_llm.length) return h("div", { class: "card muted" }, "Chưa có lần gọi LLM nào trong 14 ngày qua.");
  const kept = [...t.by_llm].sort((a, b) => b.total - a.total).slice(0, MAX_SERIES).map((r) => r.label);
  const series = t.by_llm.filter((r) => kept.includes(r.label)).sort((a, b) => a.first_at.localeCompare(b.first_at)).map((r, i) => ({ label: r.label, cls: `s${i + 1}` }));
  // Màu gán qua class: CSP của app (default-src 'self') chặn thuộc tính style viết thẳng trong phần tử.
  if (t.by_llm.length > MAX_SERIES) series.push({ label: "Khác", cls: "so" });
  const slot = (label) => (kept.includes(label) ? label : "Khác");
  const byDay = Object.fromEntries(t.llm_days.map((d) => [d, {}]));
  for (const r of t.llm_daily) if (byDay[r.day]) byDay[r.day][slot(r.label)] = (byDay[r.day][slot(r.label)] || 0) + r.total;
  const totals = t.llm_days.map((d) => Object.values(byDay[d]).reduce((a, b) => a + b, 0));
  const W = 720, H = 220, L = 44, R = 8, T = 8, B = 24;
  const max = Math.max(...totals, 1);
  const step = Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.ceil(max / step) * step;
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const slotW = (W - L - R) / t.llm_days.length, barW = Math.max(6, Math.min(28, slotW * 0.6));
  const ticks = [0, top / 2, top];
  const tip = h("div", { class: "chart-tip hidden" });
  const wrap = h("div", { class: "chart" });
  const show = (i, ev) => {
    const d = t.llm_days[i];
    const rows = series.filter((s) => byDay[d][s.label]).map((s) => h("div", { class: "row-tip" }, h("span", {}, h("span", { class: `sw ${s.cls}` }), s.label), h("span", {}, num(byDay[d][s.label]))));
    setKids(tip, h("div", { class: "row-tip" }, h("b", {}, d.split("-").reverse().join("/")), h("b", {}, num(totals[i]) + " token")), ...(rows.length ? rows : [h("div", { class: "small" }, "Không có lần gọi nào")]));
    tip.classList.remove("hidden");
    const box = wrap.getBoundingClientRect();
    const x = ev.clientX - box.left;
    tip.style.left = Math.min(Math.max(0, x + 12), box.width - tip.offsetWidth) + "px";
    tip.style.top = "0px";
  };
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Token theo LLM trong 14 ngày" },
    ticks.map((v) => [svg("line", { class: "grid-line", x1: L, x2: W - R, y1: y(v), y2: y(v) }),
      svg("text", { class: "axis-label", x: L - 6, y: y(v) + 4, "text-anchor": "end" }, compact(v))]),
    t.llm_days.map((d, i) => {
      const cx = L + slotW * i + slotW / 2;
      let acc = 0;
      const segs = series.filter((s) => byDay[d][s.label]).map((s) => {
        const v = byDay[d][s.label];
        const seg = svg("rect", { class: `bar-seg ${s.cls}`, x: cx - barW / 2, y: y(acc + v), width: barW, height: Math.max(1, y(acc) - y(acc + v)), rx: 3 });
        acc += v;
        return seg;
      });
      const label = i % 2 === (t.llm_days.length - 1) % 2 ? svg("text", { class: "axis-label", x: cx, y: H - 6, "text-anchor": "middle" }, d.slice(8) + "/" + d.slice(5, 7)) : null;
      const hit = svg("rect", { class: "hit", x: L + slotW * i, y: T, width: slotW, height: H - T - B });
      hit.addEventListener("mousemove", (ev) => show(i, ev));
      hit.addEventListener("mouseleave", () => tip.classList.add("hidden"));
      return [hit, segs, label];  // vùng rê chuột nằm dưới cột để nền sáng lên mà không làm mờ cột
    }));
  addKids(wrap, chart, tip);
  return h("div", { class: "card" },
    h("div", { class: "chart-legend" }, series.map((s) => h("span", {}, h("span", { class: `sw ${s.cls}` }), s.label))),
    wrap);
}

const COST_NOTE = "Token quy đổi = chi phí tính bằng token input: input ×1, cache đọc ×0,1, ghi cache 5 phút ×1,25, ghi cache 1 giờ ×2, output ×5. Quota ngày dùng số này.";

function tokenSection(t) {
  if (!t) return null;
  // Lời gọi trước khi có cột cache_write_1h có 1h = 0 nên cả phần ghi rơi vào cột 5 phút.
  const w5 = (r) => num(r.cache_write - (r.cache_write_1h || 0)), w1 = (r) => num(r.cache_write_1h || 0);
  // Hàm, không phải phần tử: một node DOM gắn vào bảng sau sẽ bị rút khỏi bảng trước.
  const wHead = () => [h("th", { title: "Ghi cache TTL 5 phút: giá 1,25× input" }, "Cache ghi 5p"), h("th", { title: "Ghi cache TTL 1 giờ: giá 2× input. Dữ liệu trước 19/11 không tách được, nằm ở cột 5p" }, "Cache ghi 1h")];
  const all = t.by_purpose.reduce((a, r) => a + r.input + r.cache_read + r.cache_write + r.output, 0);
  const cost = t.by_purpose.reduce((a, r) => a + (r.cost || 0), 0);
  const costHead = () => h("th", { title: COST_NOTE }, "Quy đổi");
  const table = (head, rows) => h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, head.map((x) => typeof x === "string" ? h("th", {}, x) : x))), h("tbody", {}, rows)));
  return h("div", {},
    h("h2", { class: "row" }, `Token ${t.days} ngày qua`),
    h("div", { class: "grid" }, h("div", { class: "tile", title: COST_NOTE }, h("b", {}, num(cost)), h("span", { class: "small" }, "Token quy đổi")),
      h("div", { class: "tile" }, h("b", {}, num(all)), h("span", { class: "small" }, "Tổng token xử lý (thô)")),
      h("div", { class: "tile" }, h("b", {}, t.cache_hit_pct === null ? "—" : t.cache_hit_pct + "%"), h("span", { class: "small" }, "Input đọc từ cache"),
        t.cache_unmeasured ? h("span", { class: "small muted", title: "Dịch vụ LLM không trả số token cache (vd. vLLM chưa bật --enable-prompt-tokens-details), nên không tính vào tỷ lệ" },
          `chưa tính ${num(t.cache_unmeasured)} token input từ LLM không báo cache`) : null)),
    h("p", { class: "small" }, COST_NOTE),
    table(["Mục đích", "Lần gọi", "Input", "Cache đọc", ...wHead(), "Output", costHead(), "% chi phí"], t.by_purpose.map((r) =>
      h("tr", {}, h("td", {}, PURPOSE[r.purpose] || r.purpose), h("td", {}, num(r.calls)), h("td", {}, num(r.input)), h("td", {}, num(r.cache_read)), h("td", {}, w5(r)), h("td", {}, w1(r)), h("td", {}, num(r.output)),
        h("td", {}, num(r.cost)), h("td", {}, cost ? Math.round(1000 * r.cost / cost) / 10 + "%" : "—")))),
    knowledgeSection(t.knowledge),
    h("h2", { class: "row" }, "Token theo LLM, 14 ngày qua"),
    h("p", { class: "small" }, "Tổng token xử lý mỗi ngày (input, cache đọc/ghi, output), chia theo cấu hình LLM và model. Rê chuột lên cột để xem chi tiết."),
    llmUsageChart(t),
    table(["LLM", "Lần gọi", "Input", "Cache đọc", ...wHead(), "Cache hit", "Output", "Tổng (thô)", costHead()], (t.by_llm || []).map((r) => {
      const inp = r.input + r.cache_read + r.cache_write;
      const hit = r.cache_known === 0 ? h("span", { class: "muted", title: "Dịch vụ không trả số token cache" }, "không đo được")
        : inp ? Math.round(1000 * r.cache_read / inp) / 10 + "%" : "—";
      return h("tr", {}, h("td", {}, r.label), h("td", {}, num(r.calls)), h("td", {}, num(r.input)), h("td", {}, num(r.cache_read)), h("td", {}, w5(r)), h("td", {}, w1(r)),
        h("td", {}, hit), h("td", {}, num(r.output)), h("td", {}, num(r.total)), h("td", {}, num(r.cost)));
    })),
    h("h2", { class: "row" }, "Công cụ trả kết quả lớn nhất"),
    h("p", { class: "small" }, "Kết quả công cụ đi thẳng vào context của agent; công cụ trả nhiều ký tự là chỗ nên lọc trước."),
    table(["Công cụ", "Lần gọi", "Tổng ký tự", "Lớn nhất"], t.tools.map((r) => h("tr", {}, h("td", {}, h("code", {}, r.tool)), h("td", {}, num(r.calls)), h("td", {}, num(r.result_chars)), h("td", {}, num(r.max_chars))))),
    h("h2", { class: "row" }, "Lượt tốn token nhất"),
    table(["Thời gian", "Yêu cầu", "Lần gọi LLM", "Input", "Cache đọc", "Output", costHead(), h("th", { title: "Phần quy đổi của các lời gọi học chạy sau lượt (rút bài học, chấm kết quả)" }, "Trong đó học"), "Tri thức mới", "Bỏ qua do trùng"], t.top_turns.map((r) =>
      h("tr", {}, h("td", {}, fmtTime(r.started_at)), h("td", {}, r.task), h("td", {}, num(r.llm_calls)), h("td", {}, num(r.input + r.cache_write)), h("td", {}, num(r.cache_read)), h("td", {}, num(r.output)),
        h("td", {}, num(r.cost)), h("td", {}, num(r.learn_cost)),
        h("td", {}, r.knowledge_chars == null ? "—" : num(r.knowledge_chars)), h("td", {}, r.skipped_chars == null ? "—" : num(r.skipped_chars))))));
}

function knowledgeSection(k) {
  if (!k || !k.turns) return null;
  const tile = (value, label, title) => h("div", { class: "tile", title }, h("b", {}, value), h("span", { class: "small" }, label));
  return h("div", {},
    h("h2", { class: "row" }, "Tri thức gửi kèm lượt"),
    h("p", { class: "small" }, `${num(k.turns)} lượt. Tri thức đã có trong lịch sử hội thoại không gửi lại; số ký tự tính trên khối tri thức của lượt.`),
    h("div", { class: "grid" },
      tile(num(k.sent_chars), "Ký tự tri thức mới"),
      tile(num(k.skipped_chars), "Ký tự bỏ qua do trùng", "Đã gửi ở lượt trước, model vẫn thấy trong lịch sử"),
      tile(k.skipped_pct === null ? "—" : k.skipped_pct + "%", "Tỷ lệ bỏ qua"),
      tile(k.input_pct === null ? "—" : k.input_pct + "%", "Tri thức trên input agent", "Ước lượng: ký tự/4 × số lần gọi agent của lượt, chia tổng input (gồm cache) của agent")));
}

VIEWS.metrics = async (main) => {
  const m = await api("GET", "/api/metrics");
  const tile = (label, value) => h("div", { class: "tile" }, h("b", {}, value === null || value === undefined ? "—" : value), h("span", { class: "small" }, label));
  addKids(main, h("h2", {}, "Đo lường"), h("div", { class: "grid" }, tile("Episode", m.episodes), tile("Có kết quả (%)", m.labeled_pct), tile("Người phải sửa (%)", m.edited_pct), tile("Thất bại (%)", m.failed_pct), tile("Token quy đổi mỗi nhiệm vụ", m.avg_tokens), tile("Tỷ lệ chấp nhận KU (%)", m.ku_accept_pct)),
    h("h2", { class: "row" }, "Trạng thái KU và quyết định công cụ"), h("div", { class: "card" }, h("div", {}, Object.entries(m.ku_status).map(([k, v]) => h("span", { class: "badge " + (CLASS[k] || "") }, `${LABEL[k] || k}: ${v}`))), h("div", { class: "row" }, Object.entries(m.tool_decisions).map(([k, v]) => h("span", { class: "badge " + (CLASS[k] || "") }, `${LABEL[k] || k}: ${v}`)))),
    tokenSection(m.tokens),
    h("h2", { class: "row" }, "Theo tuần"), h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Tuần", "Nhiệm vụ", "Người sửa", "Thất bại", "Token quy đổi TB"].map((x) => h("th", {}, x)))), h("tbody", {}, m.weekly.map((w) => h("tr", {}, h("td", {}, w.week), h("td", {}, w.tasks), h("td", {}, w.edited), h("td", {}, w.failed), h("td", {}, Math.round(w.tokens))))))));
};

VIEWS.audit = async (main) => {
  const list = await api("GET", "/api/audit");
  addKids(main, h("h2", {}, "Audit gọi công cụ"), h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Thời gian", "Người", "Công cụ", "Môi trường", "Mức", "Quyết định", "Kết quả", "Lý do"].map((x) => h("th", {}, x)))),
    h("tbody", {}, list.map((a) => h("tr", {}, h("td", {}, fmtTime(a.at)), h("td", {}, a.user), h("td", {}, a.tool), h("td", {}, a.env), h("td", {}, a.risk_tier), h("td", {}, badge(a.decision)), h("td", {}, a.result_status), h("td", {}, (a.reasons || []).join("; "))))))));
};

async function refreshMe() {
  const id = state.tenant.id;
  state.me = await api("GET", "/api/me");
  state.tenant = state.me.tenants.find((t) => t.id === id) || state.me.tenants[0];
  renderShell();
}

function quotaMeter(used, limit) {
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 100;
  return h("div", { class: "quota-meter " + (pct >= 100 ? "bad" : pct >= 80 ? "warn" : ""), role: "meter", "aria-valuemin": "0", "aria-valuemax": String(limit), "aria-valuenow": String(used) },
    h("span", { style: `width:${pct}%` }));
}
const fmtNum = (n) => Number(n).toLocaleString("vi-VN");

// Org admin đặt quota cho từng tenant; người khác chỉ xem quota và mức dùng của tenant đang chọn.
async function quotaView(main) {
  if (!state.me.is_org_admin) {
    const q = await api("GET", "/api/quota");
    addKids(main, h("div", { class: "card" },
      h("p", { class: "small" }, `Quota do org admin đặt. Đếm lại từ đầu lúc ${fmtTime(q.resets_at)}. Sự cố đang "Chờ quota" không tự chạy lại: khi đã có quota, mở sự cố và bấm Tiếp tục.`),
      h("div", { class: "quota-row" }, h("strong", { title: COST_NOTE }, "Token quy đổi hôm nay"), h("span", { class: "small" }, `${fmtNum(q.tokens_today)} / ${fmtNum(q.token_quota_daily)}`), quotaMeter(q.tokens_today, q.token_quota_daily)),
      h("div", { class: "quota-row" }, h("strong", {}, "Lệnh ghi hôm nay"), h("span", { class: "small" }, `${fmtNum(q.writes_today)} / ${fmtNum(q.write_quota_daily)}`), quotaMeter(q.writes_today, q.write_quota_daily)),
      h("p", { class: "small" }, `7 ngày qua: ${fmtNum(q.tokens_7d)} token quy đổi.`)));
    return;
  }
  const data = await api("GET", "/api/quotas");
  const row = (q) => {
    const tokens = h("input", { type: "number", min: "0", step: "100000", value: q.token_quota_daily, "aria-label": `Quota token/ngày của ${q.name}` });
    const writes = h("input", { type: "number", min: "0", value: q.write_quota_daily, "aria-label": `Quota lệnh ghi/ngày của ${q.name}` });
    const save = guard(async () => {
      await api("PUT", `/api/quotas/${q.tenant_id}`, { token_quota_daily: Number(tokens.value), write_quota_daily: Number(writes.value) });
      toast(`Đã lưu quota của ${q.name}`); renderShell();
    });
    return h("tr", { class: q.tenant_id === state.tenant.id ? "quota-current" : "" },
      h("td", {}, h("strong", {}, q.name)),
      h("td", {}, h("div", { class: "small" }, `${fmtNum(q.tokens_today)} / ${fmtNum(q.token_quota_daily)}`), quotaMeter(q.tokens_today, q.token_quota_daily)),
      h("td", { class: "small num" }, fmtNum(q.tokens_7d)),
      h("td", {}, h("div", { class: "small" }, `${fmtNum(q.writes_today)} / ${fmtNum(q.write_quota_daily)}`), quotaMeter(q.writes_today, q.write_quota_daily)),
      h("td", {}, tokens), h("td", {}, writes), h("td", {}, h("button", { type: "button", class: "small-btn", onclick: save }, "Lưu")));
  };
  addKids(main, h("div", { class: "card scroll" },
    h("p", { class: "small" }, `Quota theo ngày của từng tenant. Đếm lại từ đầu lúc ${fmtTime(data.resets_at)}. Token: mọi lượt chat và điều tra sự cố của tenant, 0 = chặn mọi lượt gọi model. `
      + "Lệnh ghi: số lệnh ghi chạy thành công, vượt thì mọi lệnh ghi phải hỏi người duyệt. Sự cố đang \"Chờ quota\" không tự chạy lại: sau khi tăng quota, mở sự cố và bấm Tiếp tục."),
    h("table", { class: "quota-table" },
      h("thead", {}, h("tr", {}, ["Tenant", "Token quy đổi hôm nay", "Token quy đổi 7 ngày", "Lệnh ghi hôm nay", "Quota token quy đổi/ngày", "Quota lệnh ghi/ngày", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.items.map(row)))));
}

VIEWS.settings = (main) => subTabs(main, [["policy", "Chính sách", policyView], ["quota", "Quota", quotaView], ["alerts", "Cảnh báo", alertSettingsView], ...(state.me.is_org_admin ? [["llm", "LLM", llmSettingsView], ["proxy", "Proxy", proxySettingsView]] : [])]);

async function proxySettingsView(main) {
  let st = await api("GET", "/api/settings/proxy");
  const url = h("input", { class: "wide", value: st.url || "", placeholder: "https://…/proxy.pac", "aria-label": "URL file PAC" });
  const statusBox = h("div", { class: "small" });
  const testUrl = h("input", { class: "wide", placeholder: "https://alertmanager.staging.corp.test", "aria-label": "URL cần thử" });
  persist(testUrl, "proxy:test-url");
  const testOut = h("div", { class: "small" });
  function paintStatus() {
    const err = st.error;
    statusBox.className = "small " + (err ? "warn-text" : st.loaded ? "ok-text" : "");
    statusBox.textContent = !st.url ? "Chưa khai file PAC: mọi kết nối để proxy = auto đang đi thẳng."
      : (st.loaded ? `Đã nạp ${st.bytes} byte (sha256 ${st.sha256}), tải lúc ${fmtTime(st.fetched_at * 1000)}, tự tải lại sau ${st.ttl_seconds / 60} phút.` : "Chưa nạp được: các kết nối auto đang đi thẳng.")
        + (err ? ` Lỗi lần tải gần nhất: ${err}` + (st.loaded ? " (đang dùng bản đã nạp trước đó)." : "") : "");
  }
  const save = guard(async () => { st = await api("PUT", "/api/settings/proxy", { url: url.value.trim() || null }); paintStatus(); toast("Đã lưu URL file PAC"); });
  const reload = guard(async () => { st = await api("POST", "/api/settings/proxy/reload"); paintStatus(); });
  const test = guard(async () => {
    testOut.className = "small"; setKids(testOut, busy("Đang tra…"));
    try { const r = await api("POST", "/api/settings/proxy/test", { url: testUrl.value.trim() }); testOut.className = "small ok-text"; testOut.textContent = `${r.url}: ${r.text}`; }
    catch (e) { setKids(testOut); throw e; }
  });
  paintStatus();
  addKids(main, h("div", { class: "card" }, h("h3", {}, "Proxy tự động (PAC)"),
    h("p", { class: "small" }, "kagent tải file PAC từ URL này, giữ 5 phút rồi tải lại; tải lỗi thì dùng bản đã nạp trước đó. Kết nối có ô proxy = auto (mặc định) đi theo file này; none là đi thẳng; hoặc điền http://host:port. Postgres, MySQL luôn đi thẳng. Ai sửa được file PAC là đổi được đường đi của mọi kết nối, nên chỉ dùng URL do bên quản trị mạng giữ."),
    h("div", { class: "row" }, url, h("button", { type: "button", onclick: save }, "Lưu"), h("button", { type: "button", class: "ghost", onclick: reload }, "Tải lại ngay")),
    statusBox,
    h("h3", {}, "Thử một địa chỉ"),
    h("div", { class: "row" }, testUrl, h("button", { type: "button", class: "ghost", onclick: test }, "Tra")), testOut));
}

async function llmSettingsView(main) {
  const s = await api("GET", "/api/settings/llm-key");
  const describe = { ui: `Đang dùng cấu hình "${s.profile}"`, env: "Đang dùng key từ biến môi trường của máy chủ", none: "Chưa chọn LLM: agent chạy chế độ heuristic" }[s.source];
  let editing = null; // cấu hình đang sửa; null = thêm mới
  const name = h("input", { "aria-label": "Tên cấu hình", class: "wide", placeholder: "Tên cấu hình (để trống thì tự đặt theo nhà cung cấp và model)", maxlength: 60 });
  const key = h("input", { type: "password", autocomplete: "off", placeholder: "Key hoặc token", "aria-label": "Key hoặc token", class: "wide" });
  const baseUrl = h("input", { type: "url", placeholder: "Base URL https://... (để trống nếu gọi thẳng nhà cung cấp)", "aria-label": "Base URL", class: "wide" });
  const allowPrivate = h("input", { type: "checkbox", "aria-label": "Cho phép địa chỉ mạng nội bộ" });
  const httpWarn = h("div", { class: "small warn-text hidden" }, "Base URL dùng http: key và nội dung chat đi qua mạng nội bộ dưới dạng không mã hóa.");
  const syncHttp = () => httpWarn.classList.toggle("hidden", !/^http:\/\//i.test(baseUrl.value.trim()));
  baseUrl.addEventListener("input", syncHttp);
  const modelList = h("datalist", { id: "llm-model-list" });
  const modelStrong = h("input", { "aria-label": "Model chính", class: "wide", list: "llm-model-list" });
  const modelFast = h("input", { "aria-label": "Model nhanh", class: "wide", list: "llm-model-list", placeholder: "Model nhanh (để trống thì dùng model chính)" });
  const modelInfo = h("div", { class: "small" });
  const provider = h("select", { "aria-label": "Nhà cung cấp" }, h("option", { value: "anthropic" }, "Anthropic hoặc relay Anthropic"), h("option", { value: "openai" }, "OpenAI hoặc tương thích OpenAI"));
  const authType = h("select", { "aria-label": "Loại xác thực" }, h("option", { value: "api_key" }, "API key (ANTHROPIC_API_KEY)"), h("option", { value: "token" }, "Auth token (ANTHROPIC_AUTH_TOKEN)"));
  const formTitle = h("h3", {}, "Thêm cấu hình LLM");
  const cancelEdit = h("button", { type: "button", class: "ghost hidden", onclick: () => { fill(null); store.set("llm:draft", null); } }, "Hủy sửa");
  const syncProvider = () => {
    const isOpenAI = provider.value === "openai";
    authType.classList.toggle("hidden", isOpenAI);
    modelStrong.placeholder = isOpenAI ? "Model chính (bắt buộc)" : "Model chính (để trống để dùng mặc định Claude)";
  };
  provider.addEventListener("change", syncProvider);
  // Đổ cấu hình đã lưu vào form để sửa; key không bao giờ được trả về nên để trống nghĩa là giữ key cũ.
  function fill(p) {
    editing = p;
    name.value = p ? p.name : "";
    provider.value = p ? p.provider : "anthropic";
    authType.value = p && p.auth_type === "token" ? "token" : "api_key";
    baseUrl.value = p ? p.base_url || "" : "";
    allowPrivate.checked = !!(p && p.allow_private);
    modelStrong.value = p ? p.model_strong || "" : "";
    modelFast.value = p ? p.model_fast || "" : "";
    key.value = "";
    key.placeholder = p ? `Để trống để giữ key cũ (…${p.last4})` : "Key hoặc token";
    formTitle.textContent = p ? `Sửa cấu hình "${p.name}"` : "Thêm cấu hình LLM";
    cancelEdit.classList.toggle("hidden", !p);
    syncProvider();
    syncHttp();
  }
  const endpoint = () => ({ base_url: baseUrl.value || null, auth_type: authType.value, provider: provider.value, allow_private: allowPrivate.checked });
  const save = guard(async (ev) => {
    ev.preventDefault();
    if (!editing && !key.value.trim()) throw new Error("Hãy nhập key");
    if (provider.value === "openai" && !modelStrong.value.trim()) throw new Error("OpenAI cần tên model chính");
    await api("POST", "/api/settings/llm-key", { ...endpoint(), key: key.value, name: name.value.trim() || null, profile_id: editing ? editing.id : null,
      model_strong: modelStrong.value || null, model_fast: modelFast.value || null, activate: editing ? editing.active : true });
    clearDraft();
    toast(editing ? "Đã kiểm tra và lưu cấu hình" : "Đã kiểm tra, lưu và chọn cấu hình mới");
    await refreshMe();
  });
  const fetchModels = guard(async () => {
    const r = await api("POST", "/api/settings/llm-models", { ...endpoint(), key: key.value, profile_id: editing ? editing.id : null });
    setKids(modelList, ...r.models.map((m) => h("option", { value: m.id }, m.name !== m.id ? m.name : null)));
    modelInfo.textContent = r.models.length ? `Có ${r.models.length} model; bấm vào ô model để chọn.` : "Endpoint không trả về model nào.";
    toast(`Đã lấy ${r.models.length} model`);
  });
  const use = (p) => guard(async () => { await api("POST", `/api/settings/llm-profiles/${p.id}/activate`); toast(`Đang dùng "${p.name}"`); await refreshMe(); });
  const drop = (p) => guard(async () => { await api("DELETE", `/api/settings/llm-profiles/${p.id}`); toast(`Đã xóa "${p.name}"`); await refreshMe(); });
  const row = (p) => h("div", { class: "tool-item" },
    h("div", { class: "tool-head" }, h("b", {}, p.name), p.active && h("span", { class: "badge ok" }, "đang dùng"), !p.readable && h("span", { class: "badge bad" }, "không giải mã được"),
      h("span", { class: "spacer" }),
      !p.active && p.readable && h("button", { type: "button", class: "small-btn", onclick: use(p) }, "Dùng"),
      p.readable && h("button", { type: "button", class: "ghost small-btn", onclick: () => { fill(p); saveDraft(); name.focus(); } }, "Sửa"),
      h("button", { type: "button", class: "danger small-btn", onclick: drop(p) }, "Xóa")),
    h("div", { class: "small" }, `${p.provider === "openai" ? "OpenAI / tương thích" : "Anthropic"}${p.model_strong ? ` · ${p.model_strong}${p.model_fast ? `, ${p.model_fast}` : ""}` : ""} · key …${p.last4}`),
    p.base_url && h("div", { class: "small" }, "Endpoint: " + p.base_url + (p.allow_private ? " (mạng nội bộ)" : "")),
    h("div", { class: "small muted" }, `Cập nhật ${fmtTime(p.updated_at)} bởi ${p.updated_by}`));
  // Bản nháp form (không gồm key) giữ qua refresh, kể cả đang sửa cấu hình nào.
  const fields = { name, provider, authType, baseUrl, allowPrivate, modelStrong, modelFast };
  const saveDraft = () => store.set("llm:draft", { editing: editing ? editing.id : null,
    ...Object.fromEntries(Object.entries(fields).map(([k, el]) => [k, el.type === "checkbox" ? el.checked : el.value])) });
  const clearDraft = () => store.set("llm:draft", null);
  const draft = store.get("llm:draft", null);
  fill(draft && draft.editing ? s.profiles.find((p) => p.id === draft.editing) || null : null);
  if (draft) for (const [k, el] of Object.entries(fields)) if (k in draft) el[el.type === "checkbox" ? "checked" : "value"] = draft[k];
  syncProvider();
  syncHttp();
  for (const el of Object.values(fields)) { el.addEventListener("input", saveDraft); el.addEventListener("change", saveDraft); }
  addKids(main, h("h2", {}, "LLM"),
    h("div", { class: "card" }, h("div", {}, describe, s.last4 && ` (key kết thúc bằng ${s.last4})`),
      s.profiles.length ? s.profiles.map(row) : h("div", { class: "small muted" }, "Chưa lưu cấu hình nào."),
      h("p", { class: "small" }, "Chọn \"Dùng\" để chuyển agent sang cấu hình khác; mọi lượt chat và học nền sau đó dùng cấu hình này. Trang Đo lường tách token theo từng cấu hình.")),
    h("div", { class: "card" }, formTitle,
      h("form", { onsubmit: save }, h("div", { class: "row" }, name), h("div", { class: "row" }, provider, authType, baseUrl),
        h("div", { class: "row" }, h("label", { class: "small" }, allowPrivate, " Cho phép địa chỉ mạng nội bộ (LLM tự host như vLLM, Ollama, LiteLLM; nhận cả http)")), httpWarn,
        h("div", { class: "row" }, modelStrong, modelFast, h("button", { type: "button", onclick: fetchModels }, "Lấy danh sách model"), modelList), modelInfo,
        h("div", { class: "row" }, key, h("button", { type: "submit" }, "Kiểm tra và lưu"), cancelEdit)),
      h("p", { class: "small" }, "Key dùng chung cho cả tổ chức, được kiểm tra trước khi lưu, mã hóa khi lưu và không hiển thị lại. Với relay và OpenAI, bước kiểm tra gửi một tin nhắn rất ngắn; nội dung nhiệm vụ đi qua máy chủ của dịch vụ đó. Cấu hình chọn ở đây được ưu tiên hơn biến môi trường. Khi sửa, để trống ô key để giữ key cũ, với điều kiện giữ nguyên nhà cung cấp và Base URL.")));
};

VIEWS.account = async (main) => {
  const current = h("input", { type: "password", autocomplete: "current-password", "aria-label": "Mật khẩu hiện tại" });
  const next = h("input", { type: "password", autocomplete: "new-password", minlength: 12, "aria-label": "Mật khẩu mới" });
  const again = h("input", { type: "password", autocomplete: "new-password", "aria-label": "Nhập lại mật khẩu mới" });
  const submit = guard(async (ev) => {
    ev.preventDefault();
    if (next.value.length < 12) throw new Error("Mật khẩu mới cần ít nhất 12 ký tự");
    if (next.value !== again.value) throw new Error("Hai lần nhập mật khẩu mới không khớp");
    await api("POST", "/api/auth/password", { current_password: current.value, new_password: next.value });
    current.value = next.value = again.value = "";
    toast("Đã đổi mật khẩu, các phiên khác đã bị đăng xuất");
  });
  const field = (label, input) => h("label", { class: "field" }, label, input);
  addKids(main, personalSection(), h("h2", {}, "Đổi mật khẩu"), h("form", { class: "card", onsubmit: submit },
    h("div", { class: "row" }, field("Mật khẩu hiện tại", current), field("Mật khẩu mới (ít nhất 12 ký tự)", next), field("Nhập lại mật khẩu mới", again)),
    h("div", { class: "row" }, h("button", { type: "submit" }, "Đổi mật khẩu"))));
};

async function boot() {
  try { state.me = await api("GET", "/api/me"); } catch { return renderLogin(); }
  if (!state.me.tenants.length) { setKids(root, h("div", { class: "card login" }, "Tài khoản chưa thuộc tenant nào.")); return; }
  state.tenant = state.tenant || state.me.tenants[0];
  applyHash();
  state.env = store.get("env", (state.me.profile && state.me.profile.default_env) || "dev");
  syncUrl(true);
  renderShell();
}
boot();
