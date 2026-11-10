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
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
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
    throw new Error(typeof detail === "string" ? detail : `Lỗi ${res.status}`);
  }
  return data;
}

function toast(msg, isErr) {
  const el = h("div", { class: "toast" + (isErr ? " err" : ""), role: "status" }, msg);
  document.body.append(el);
  setTimeout(() => el.remove(), isErr ? 7000 : 3000);
}

const guard = (fn) => async (ev) => {
  const btn = ev && ev.currentTarget instanceof HTMLButtonElement ? ev.currentTarget : null;
  if (btn) btn.disabled = true;
  try { await fn(ev); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
};

const fmtTime = (s) => (s ? new Date(s).toLocaleString("vi-VN") : "—");
const CLASS = { success: "ok", ok: "ok", active: "ok", auto: "ok", approved: "ok", published: "ok", failure: "bad", error: "bad", blocked: "bad", denied: "bad", rejected: "bad", deprecated: "bad", invalid: "bad", pending: "warn", candidate: "warn", pending_review: "warn", partial: "warn", expired: "warn" };
const LABEL = { success: "thành công", failure: "thất bại", partial: "một phần", unknown: "chưa rõ", ok: "ok", error: "lỗi", blocked: "bị chặn", pending: "chờ duyệt", auto: "tự chạy", approved: "đã duyệt", denied: "từ chối", expired: "hết hạn", invalid: "sai tham số", candidate: "ứng viên", active: "hiệu lực", deprecated: "hết hiệu lực", rejected: "bị loại", pending_review: "chờ duyệt", published: "đã publish", noop: "không đổi", stale: "đã cũ", reverted: "đã hoàn tác" };
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
  root.replaceChildren(h("form", { class: "card login", onsubmit: submit }, h("h2", {}, "Knowledge Agent"), h("p", { class: "muted" }, "Agent tự tích lũy tri thức"), user, pass, h("button", { type: "submit" }, "Đăng nhập")));
  user.focus();
}

const TABS = [["episodes", "Nhật ký"], ["knowledge", "Tri thức"], ["approvals", "Duyệt lệnh"], ["compile", "Biên dịch L2"], ["policy", "Công cụ & Chính sách"], ["metrics", "Đo lường"], ["audit", "Audit"], ["settings", "Cài đặt"], ["account", "Tài khoản"]];
const VIEWS = {};
let conversationList = null;

function closeSidebar() { document.querySelector(".sidebar")?.classList.remove("open"); document.querySelector(".scrim")?.classList.remove("open"); }
function go(tab, conversationId) { state.tab = tab; if (conversationId !== undefined) state.conversationId = conversationId; renderShell(); }

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
    item.replaceChildren(input);
    input.select();
  });
  const remove = guard(async () => {
    await api("DELETE", `/api/conversations/${c.id}`);
    toast("Đã xóa hội thoại khỏi danh sách");
    if (state.conversationId === c.id) go("chat", null); else refreshConversations();
  });
  const item = h("div", { class: "conv" + (state.tab === "chat" && state.conversationId === c.id ? " active" : "") },
    h("button", { type: "button", class: "conv-open", title: c.title, onclick: () => { closeSidebar(); go("chat", c.id); } }, c.title),
    iconButton("edit", "Đổi tên", rename), iconButton("trash", "Xóa", remove));
  return item;
}

async function refreshConversations() {
  if (!conversationList) return;
  const list = await api("GET", "/api/conversations");
  const groups = new Map();
  for (const c of list) { const g = dateGroup(c.updated_at); if (!groups.has(g)) groups.set(g, []); groups.get(g).push(c); }
  conversationList.replaceChildren(...(list.length ? [...groups].flatMap(([g, items]) => [h("div", { class: "group" }, g), ...items.map(conversationItem)]) : [h("div", { class: "small pad" }, "Chưa có hội thoại nào.")]));
}

function renderShell() {
  const tenants = h("select", { "aria-label": "Tenant", onchange: (e) => { state.tenant = state.me.tenants.find((t) => t.id === e.target.value); go("chat", null); } },
    state.me.tenants.map((t) => h("option", { value: t.id, selected: t.id === state.tenant.id }, t.name)));
  const logout = guard(async () => { await api("POST", "/api/auth/logout"); state.me = null; state.conversationId = null; state.tab = "chat"; renderLogin(); });
  conversationList = h("div", { class: "conv-list" });
  const pages = TABS.filter(([id]) => id !== "settings" || state.me.is_org_admin);
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
  root.replaceChildren(h("div", { class: "layout" }, scrim, sidebar, h("div", { class: "content" }, top, main)));
  refreshConversations().catch((e) => toast(e.message, true));
  VIEWS[state.tab](main).catch((e) => toast(e.message, true));
}

function traceView(trace) {
  return trace.filter((s) => s.type === "tool_call" || s.type === "error").map((s) => {
    if (s.type === "error") return h("div", { class: "step bad" }, h("b", {}, "Lỗi: "), s.message);
    const cls = s.status === "ok" ? "ok" : s.status === "pending" ? "pending" : "bad";
    return h("div", { class: "step " + cls },
      h("div", {}, h("b", {}, s.tool), " ", s.risk_tier && h("span", { class: "badge" }, s.risk_tier), " ", h("span", { class: "badge" }, s.env), " ", badge(s.decision), " ", badge(s.status),
        s.confidence !== undefined && h("span", { class: "small" }, " tin cậy " + s.confidence), s.untrusted && h("span", { class: "badge warn" }, "untrusted")),
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
    h("div", { class: "small" }, `KU đã nạp: ${ep.ku_used.length} · Token: ${ep.tokens}`), feedbackBox(ep, onDone));
}

VIEWS.chat = chatView;

VIEWS.episodes = async (main) => {
  const list = await api("GET", "/api/episodes");
  const detail = h("div");
  const open = (id) => guard(async () => { const ep = await api("GET", `/api/episodes/${id}`); detail.replaceChildren(episodeCard(ep, () => renderShell())); detail.scrollIntoView(); });
  main.append(h("h2", {}, "Nhật ký (L0)"), h("div", { class: "card scroll" }, list.length === 0 ? "Chưa có episode." : h("table", {},
    h("thead", {}, h("tr", {}, ["Thời gian", "Nhiệm vụ", "Loại", "Môi trường", "Kết quả", "Người chạy", "Đã học"].map((x) => h("th", {}, x)))),
    h("tbody", {}, list.map((e) => h("tr", { class: "click", onclick: open(e.id) }, h("td", {}, fmtTime(e.started_at)), h("td", {}, e.input.task), h("td", {}, e.task_type), h("td", {}, e.env), h("td", {}, badge(e.outcome)), h("td", {}, e.user), h("td", {}, e.processed_at ? "rồi" : "chưa")))))), detail);
};

VIEWS.knowledge = async (main) => {
  const statusSel = h("select", { "aria-label": "Trạng thái", onchange: () => load() }, [["candidate", "Ứng viên"], ["active", "Hiệu lực"], ["deprecated", "Hết hiệu lực"], ["rejected", "Bị loại"], ["", "Tất cả"]].map(([v, l]) => h("option", { value: v }, l)));
  const box = h("div");
  const detail = h("div");
  const showKu = (id) => guard(async () => {
    const ku = await api("GET", `/api/ku/${id}`);
    detail.replaceChildren(h("div", { class: "card" }, h("h3", {}, "Lịch sử: " + ku.title),
      h("div", { class: "small" }, `Nguồn: ${ku.source_trust} · ${ku.source_eps.length} episode · phiên bản ${ku.version}`),
      h("table", {}, h("tbody", {}, ku.events.map((ev) => h("tr", {}, h("td", {}, fmtTime(ev.at)), h("td", {}, ev.op), h("td", {}, ev.actor), h("td", {}, ev.reason || ""),
        h("td", {}, isAdmin() && h("button", { class: "ghost", onclick: guard(async () => { await api("POST", `/api/ku/events/${ev.id}/rollback`); toast("Đã hoàn tác"); await load(); }) }, "Hoàn tác"))))))));
  });
  const review = (id, approve) => guard(async () => { await api("POST", `/api/ku/${id}/review`, { approve }); toast(approve ? "Đã chấp nhận" : "Đã loại"); await load(); });
  async function load() {
    const q = statusSel.value ? `?status=${statusSel.value}` : "";
    const list = await api("GET", "/api/ku" + q);
    box.replaceChildren(...(list.length ? list.map((k) => h("div", { class: "card" },
      h("div", { class: "row spread" }, h("h3", {}, k.title), h("div", {}, badge(k.status), h("span", { class: "badge info" }, k.kind), h("span", { class: "badge" }, k.source_trust), h("span", { class: "badge" }, k.domain))),
      h("div", {}, k.content), k.applies_when && h("div", { class: "small" }, "Áp dụng: " + k.applies_when),
      h("div", { class: "small" }, `✓${k.helpful_cnt} ✗${k.harmful_cnt} · tin cậy ${k.confidence}`),
      h("div", { class: "row" }, k.status === "candidate" && isAdmin() && h("button", { onclick: review(k.id, true) }, "Chấp nhận"), k.status === "candidate" && isAdmin() && h("button", { class: "danger", onclick: review(k.id, false) }, "Loại"), h("button", { class: "ghost", onclick: showKu(k.id) }, "Lịch sử")))) : [h("div", { class: "card muted" }, "Không có KU ở trạng thái này.")]));
  }
  const learn = guard(async () => { const r = await api("POST", "/api/learning/run"); toast(`Đã học ${r.processed} episode, thăng hạng ${r.promoted}, chờ duyệt ${r.needs_review}`); await load(); });
  const kind = h("select", { "aria-label": "Loại" }, ["rule", "fact", "procedure", "lesson", "insight"].map((x) => h("option", {}, x)));
  const title = h("input", { placeholder: "Tiêu đề", "aria-label": "Tiêu đề" });
  const content = h("input", { placeholder: "Nội dung một ý", "aria-label": "Nội dung" });
  const add = guard(async () => { await api("POST", "/api/ku", { kind: kind.value, title: title.value, content: content.value }); title.value = content.value = ""; toast("Đã thêm ứng viên"); statusSel.value = "candidate"; await load(); });
  main.append(h("h2", {}, "Tri thức (L1)"), h("div", { class: "row" }, statusSel, isAdmin() && h("button", { onclick: learn }, "Chạy học ngay")),
    h("div", { class: "card row" }, kind, title, content, h("button", { class: "ghost", onclick: add }, "Thêm KU")), detail, box);
  await load();
};

VIEWS.approvals = async (main) => {
  const list = await api("GET", "/api/approvals");
  const pending = list.filter((a) => a.status === "pending");
  const others = list.filter((a) => a.status !== "pending");
  const card = (a) => {
    const reason = h("input", { placeholder: "Lý do từ chối", "aria-label": "Lý do từ chối" });
    const act = (action) => guard(async () => { const r = await api("POST", `/api/approvals/${a.id}/decide`, { action, reason: reason.value || null }); toast(r.status === "pending" ? "Đã ghi nhận, cần thêm người duyệt" : "Đã xử lý: " + (LABEL[r.status] || r.status)); renderShell(); });
    return h("div", { class: "card" }, h("div", { class: "row spread" }, h("h3", {}, a.tool), h("div", {}, h("span", { class: "badge" }, a.risk_tier), h("span", { class: "badge" }, a.env), h("span", { class: "badge info" }, `tin cậy ${a.confidence}`), h("span", { class: "badge" }, `${a.decided_by.length}/${a.required_approvals} duyệt`))),
      h("div", {}, "Yêu cầu gốc: ", a.original_request), h("div", { class: "small" }, `Người yêu cầu: ${a.requester} · hết hạn ${fmtTime(a.expires_at)}`), h("div", { class: "small" }, "Lý do cần duyệt: " + a.reasons.join("; ")),
      h("pre", {}, JSON.stringify(a.args, null, 1)), a.dry_run && h("pre", {}, "Dry-run: " + JSON.stringify(a.dry_run)), a.rollback_hint && h("div", { class: "small" }, "Cách hoàn tác: " + a.rollback_hint),
      h("div", { class: "row" }, h("button", { onclick: act("approve_once") }, "Duyệt một lần"), h("button", { class: "ghost", onclick: act("approve_session") }, "Duyệt cho phiên (1 giờ)"), reason, h("button", { class: "danger", onclick: act("deny") }, "Từ chối")));
  };
  main.append(h("h2", {}, `Chờ duyệt (${pending.length})`), pending.length ? pending.map(card) : h("div", { class: "card muted" }, "Không có lệnh nào chờ duyệt."),
    others.length > 0 && h("h2", {}, "Đã xử lý"), others.length > 0 && h("div", { class: "card scroll" }, h("table", {}, h("tbody", {}, others.map((a) => h("tr", {}, h("td", {}, fmtTime(a.decided_at)), h("td", {}, a.tool), h("td", {}, a.env), h("td", {}, badge(a.status)), h("td", {}, a.deny_reason || "")))))));
};

VIEWS.compile = async (main) => {
  const [runs, files, golden] = await Promise.all([api("GET", "/api/compile/runs"), api("GET", "/api/knowledge/files"), api("GET", "/api/golden")]);
  const viewer = h("pre", {}, "Chọn một file.");
  const show = (path) => guard(async () => { const f = await api("GET", "/api/knowledge/file?path=" + encodeURIComponent(path)); viewer.textContent = f.content; });
  const compile = guard(async () => { const r = await api("POST", "/api/compile"); toast("Biên dịch: " + (LABEL[r.status] || r.status)); renderShell(); });
  const rollback = guard(async () => { await api("POST", "/api/compile/rollback"); toast("Đã hoàn tác lần publish gần nhất"); renderShell(); });
  const decide = (id, approve) => guard(async () => { await api("POST", `/api/compile/${id}/decide`, { approve }); renderShell(); });
  const query = h("input", { placeholder: "Câu hỏi golden", "aria-label": "Câu hỏi golden" });
  const domain = h("select", { "aria-label": "Loại nhiệm vụ" }, ["k8s_ops", "observability", "docs", "research", "general"].map((x) => h("option", {}, x)));
  const terms = h("input", { placeholder: "Từ khóa kỳ vọng, cách nhau dấu phẩy", "aria-label": "Từ khóa kỳ vọng" });
  const addGolden = guard(async () => { await api("POST", "/api/golden", { query: query.value, expect_domain: domain.value, expect_terms: terms.value.split(",").map((s) => s.trim()).filter(Boolean) }); renderShell(); });
  main.append(h("h2", {}, "Tri thức biên dịch (L2)"), isAdmin() && h("div", { class: "row" }, h("button", { onclick: compile }, "Biên dịch ngay"), h("button", { class: "danger", onclick: rollback }, "Hoàn tác publish gần nhất")),
    h("div", { class: "split", style: null }, h("div", { class: "card filelist" }, h("h3", {}, "File"), files.length ? files.map((f) => h("button", { onclick: show(f) }, f)) : "Chưa có file."), h("div", { class: "card" }, h("h3", {}, "Nội dung"), viewer)),
    h("h2", {}, "Các lần biên dịch"), runs.length ? runs.map((r) => h("div", { class: "card" }, h("div", { class: "row spread" }, h("span", {}, fmtTime(r.created_at), " · ", r.actor), h("div", {}, badge(r.status), r.needs_review ? h("span", { class: "badge warn" }, "cần người duyệt") : "")),
      h("div", { class: "small" }, `Golden: ${r.score_before ?? "—"} → ${r.score_after ?? "—"} · File đổi: ${r.changed.join(", ") || "không"}`), r.diff && h("pre", {}, r.diff),
      r.status === "pending_review" && isAdmin() && h("div", { class: "row" }, h("button", { onclick: decide(r.id, true) }, "Publish"), h("button", { class: "danger", onclick: decide(r.id, false) }, "Từ chối")))) : h("div", { class: "card muted" }, "Chưa biên dịch lần nào."),
    h("h2", {}, `Golden task (${golden.length})`), h("div", { class: "card" }, golden.map((g) => h("div", {}, g.query, " ", h("span", { class: "badge" }, g.expect_domain), " ", h("span", { class: "small" }, g.expect_terms.join(", ")))), isAdmin() && h("div", { class: "row" }, query, domain, terms, h("button", { class: "ghost", onclick: addGolden }, "Thêm"))));
};

const ACTION_LABEL = { read: "đọc", write: "ghi", destructive: "phá hủy", exec: "thực thi", external_send: "gửi ra ngoài" };
const ROLE_LABEL = { member: "member", tenant_admin: "tenant admin", "approver:dev": "approver dev", "approver:staging": "approver staging", "approver:prod": "approver prod" };

function toolCatalogSection() {
  const admin = isAdmin();
  const box = h("div", {}, h("div", { class: "card muted" }, "Đang tải danh mục…"));
  const search = h("input", { type: "search", placeholder: "Tìm công cụ", "aria-label": "Tìm công cụ" });
  const filter = h("select", { "aria-label": "Lọc công cụ" }, [["all", "Tất cả"], ["enabled", "Đang bật"], ["new", "Mới, chưa duyệt"], ["off", "Đang tắt"], ["removed", "Đã gỡ"]].map(([v, l]) => h("option", { value: v }, l)));
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
    const locked = !admin || removed;
    const lastOne = (arr, v) => arr.length === 1 && arr.includes(v);
    const envs = t.catalog_envs.map((e) => chip(e, t.effective_envs.includes(e), patch(t, { envs: flip(t.effective_envs, e) }), locked || lastOne(t.effective_envs, e), "Môi trường được dùng công cụ"));
    const roles = t.role_choices.map((r) => chip(ROLE_LABEL[r] || r, t.effective_roles.includes(r), patch(t, { roles: flip(t.effective_roles, r) }), locked || lastOne(t.effective_roles, r), "Vai trò được gọi công cụ"));
    const tier = h("select", { "aria-label": `Mức rủi ro của ${t.tool}`, disabled: locked, onchange: (e) => patch(t, { risk_tier: e.target.value })() },
      (t.tier_choices.length ? t.tier_choices : [t.risk_tier]).map((r) => h("option", { selected: r === t.risk_tier }, r)));
    const state = removed ? null : admin
      ? h("button", { type: "button", class: t.enabled ? "ghost small-btn" : "small-btn", onclick: patch(t, { enabled: !t.enabled }) }, t.enabled ? "Tắt" : "Bật")
      : h("span", { class: "small" }, t.enabled ? "đang bật" : "đang tắt");
    return h("div", { class: "tool-item" + (t.enabled && !removed ? "" : " off") },
      h("div", { class: "tool-head" }, h("code", {}, t.tool), h("span", { class: "badge " + tierClass(t.risk_tier) }, t.risk_tier),
        t.risk_tier !== t.catalog_tier && h("span", { class: "small" }, `danh mục: ${t.catalog_tier}`), h("span", { class: "badge" }, ACTION_LABEL[t.action_class] || t.action_class),
        t.untrusted_output ? h("span", { class: "badge warn" }, "nội dung ngoài") : null, t.is_new && !removed && h("span", { class: "badge info" }, "Mới"),
        removed && h("span", { class: "badge bad" }, "Đã gỡ khỏi danh mục"), !t.classified && h("span", { class: "badge warn" }, "Chưa phân loại"), h("span", { class: "spacer" }), state),
      h("div", { class: "small" }, t.description + (t.rollback_hint ? ` · Hoàn tác: ${t.rollback_hint}` : "")),
      !removed && h("div", { class: "tool-perms" }, h("span", { class: "small" }, "Môi trường"), envs, h("span", { class: "small sep" }, "Vai trò"), roles, h("span", { class: "small sep" }, "Mức"), tier),
      t.warnings.length > 0 && h("div", { class: "small warn-text" }, "Cảnh báo: " + t.warnings.join("; ")));
  }

  function paint() {
    const q = search.value.trim().toLowerCase();
    const match = { all: () => true, enabled: (t) => t.enabled && t.status === "active", new: (t) => t.is_new && t.status === "active", off: (t) => !t.enabled && t.status === "active", removed: (t) => t.status !== "active" }[filter.value];
    const shown = list.filter((t) => match(t) && (!q || t.tool.includes(q) || t.description.toLowerCase().includes(q)));
    const groups = new Map();
    for (const t of shown) { if (!groups.has(t.server)) groups.set(t.server, []); groups.get(t.server).push(t); }
    const fresh = list.filter((t) => t.is_new && t.status === "active").length;
    box.replaceChildren(
      ...(fresh ? [h("div", { class: "notice" }, `${fresh} công cụ mới từ danh mục đang tắt, chờ admin xem và bật.`)] : []),
      ...(shown.length ? [...groups].map(([server, items]) => h("div", { class: "card" }, h("h3", {}, server, " ", h("span", { class: "small" }, items[0].server_description)), items.map(item)))
        : [h("div", { class: "card muted" }, "Không có công cụ phù hợp.")]));
  }
  search.addEventListener("input", paint);
  filter.addEventListener("change", paint);
  api("GET", "/api/tools").then((r) => { list = r; paint(); }).catch((e) => toast(e.message, true));
  return h("div", {}, h("p", { class: "small" }, "Thêm, gỡ hoặc phân loại công cụ bằng manifest trong kagent/catalog (xem docs/TOOLS.md). Ở đây tenant chỉ được siết thêm: bật/tắt, nâng mức rủi ro, thu hẹp môi trường và vai trò."),
    h("div", { class: "row" }, search, filter), box);
}

VIEWS.policy = async (main) => {
  const [tools, p] = await Promise.all([api("GET", "/api/tools"), api("GET", "/api/policy")]);
  const cellText = (c) => (typeof c === "string" ? ({ auto: "tự động", ask: "luôn hỏi", block: "chặn" }[c]) : c.ask_below !== undefined ? `hỏi nếu < ${c.ask_below}` : `hỏi, ${c.ask} người`);
  const editor = h("textarea", { class: "code", value: JSON.stringify(p.overlay, null, 2), "aria-label": "Overlay chính sách" });
  const save = guard(async () => { let overlay; try { overlay = JSON.parse(editor.value); } catch { throw new Error("JSON không hợp lệ"); } await api("PUT", "/api/policy", { overlay }); toast("Đã lưu chính sách"); renderShell(); });
  const setFlag = (key, value) => guard(async () => { await api("PATCH", "/api/tenant", { [key]: value }); renderShell(); });
  const simTool = h("select", { "aria-label": "Công cụ" }, tools.map((t) => h("option", {}, t.tool)));
  const simEnv = h("select", { "aria-label": "Môi trường" }, ["dev", "staging", "prod"].map((x) => h("option", {}, x)));
  const simConf = h("input", { type: "number", min: 0, max: 1, step: 0.05, value: "0.9", "aria-label": "Điểm tin cậy" });
  const simOut = h("div", { class: "small" });
  const sim = guard(async () => { const r = await api("POST", "/api/policy/simulate", { tool: simTool.value, env: simEnv.value, confidence: Number(simConf.value) }); simOut.textContent = `${r.risk_tier} → ${LABEL[r.outcome] || r.outcome}${r.required ? `, ${r.required} người duyệt` : ""}. ${r.reasons.join("; ")}`; });
  const tiers = ["R0", "R1", "R2", "R3", "R4"];
  main.append(h("h2", {}, "Chính sách hiệu lực"), h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Mức"), ["dev", "staging", "prod"].map((e) => h("th", {}, `${e} (${p.effective.environments[e].auto_execute})`)))),
    h("tbody", {}, tiers.map((t) => h("tr", {}, h("td", {}, t), ["dev", "staging", "prod"].map((e) => h("td", {}, cellText(p.effective.matrix[t][e]))))))),
    h("div", { class: "row" }, h("span", { class: "small" }, "Luật chặn: " + (p.effective.deny.map((d) => `${d.tool}@${d.env}`).join(", ") || "không"))),
    isAdmin() && h("div", { class: "row" }, h("button", { class: "ghost", onclick: setFlag("change_freeze", !p.change_freeze) }, p.change_freeze ? "Tắt change freeze" : "Bật change freeze"), h("button", { class: "ghost", onclick: setFlag("auto_promote", !p.auto_promote) }, p.auto_promote ? "Tắt tự động thăng hạng" : "Bật tự động thăng hạng"))),
    h("h2", {}, "Thử quyết định"), h("div", { class: "card" }, h("div", { class: "row" }, simTool, simEnv, simConf, h("button", { class: "ghost", onclick: sim }, "Thử")), simOut),
    isAdmin() && h("h2", {}, "Overlay của tenant (chỉ được siết)"), isAdmin() && h("div", { class: "card" }, editor, h("div", { class: "row" }, h("button", { onclick: save }, "Lưu"))),
    h("h2", {}, "Danh mục công cụ"), toolCatalogSection());
};

VIEWS.metrics = async (main) => {
  const m = await api("GET", "/api/metrics");
  const tile = (label, value) => h("div", { class: "tile" }, h("b", {}, value === null || value === undefined ? "—" : value), h("span", { class: "small" }, label));
  main.append(h("h2", {}, "Đo lường"), h("div", { class: "grid" }, tile("Episode", m.episodes), tile("Có kết quả (%)", m.labeled_pct), tile("Người phải sửa (%)", m.edited_pct), tile("Thất bại (%)", m.failed_pct), tile("Token mỗi nhiệm vụ", m.avg_tokens), tile("Tỷ lệ chấp nhận KU (%)", m.ku_accept_pct)),
    h("h2", { class: "row" }, "Trạng thái KU và quyết định công cụ"), h("div", { class: "card" }, h("div", {}, Object.entries(m.ku_status).map(([k, v]) => h("span", { class: "badge " + (CLASS[k] || "") }, `${LABEL[k] || k}: ${v}`))), h("div", { class: "row" }, Object.entries(m.tool_decisions).map(([k, v]) => h("span", { class: "badge " + (CLASS[k] || "") }, `${LABEL[k] || k}: ${v}`)))),
    h("h2", { class: "row" }, "Theo tuần"), h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Tuần", "Nhiệm vụ", "Người sửa", "Thất bại", "Token TB"].map((x) => h("th", {}, x)))), h("tbody", {}, m.weekly.map((w) => h("tr", {}, h("td", {}, w.week), h("td", {}, w.tasks), h("td", {}, w.edited), h("td", {}, w.failed), h("td", {}, Math.round(w.tokens))))))));
};

VIEWS.audit = async (main) => {
  const list = await api("GET", "/api/audit");
  main.append(h("h2", {}, "Audit gọi công cụ"), h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Thời gian", "Người", "Công cụ", "Môi trường", "Mức", "Quyết định", "Kết quả", "Lý do"].map((x) => h("th", {}, x)))),
    h("tbody", {}, list.map((a) => h("tr", {}, h("td", {}, fmtTime(a.at)), h("td", {}, a.user), h("td", {}, a.tool), h("td", {}, a.env), h("td", {}, a.risk_tier), h("td", {}, badge(a.decision)), h("td", {}, a.result_status), h("td", {}, (a.reasons || []).join("; "))))))));
};

async function refreshMe() {
  const id = state.tenant.id;
  state.me = await api("GET", "/api/me");
  state.tenant = state.me.tenants.find((t) => t.id === id) || state.me.tenants[0];
  renderShell();
}

VIEWS.settings = async (main) => {
  const s = await api("GET", "/api/settings/llm-key");
  const describe = { ui: "Đang dùng key nhập trên giao diện", env: "Đang dùng key từ biến môi trường của máy chủ", none: "Chưa có key: agent chạy chế độ heuristic" }[s.source];
  const key = h("input", { type: "password", autocomplete: "off", placeholder: "Key hoặc token", "aria-label": "Key hoặc token", class: "wide" });
  const baseUrl = h("input", { type: "url", placeholder: "Base URL https://... (để trống nếu gọi thẳng nhà cung cấp)", "aria-label": "Base URL", class: "wide", value: s.base_url || "" });
  const modelStrong = h("input", { "aria-label": "Model chính", class: "wide", value: s.model_strong || "" });
  const modelFast = h("input", { "aria-label": "Model nhanh", class: "wide", placeholder: "Model nhanh (để trống thì dùng model chính)", value: s.model_fast || "" });
  const provider = h("select", { "aria-label": "Nhà cung cấp" }, h("option", { value: "anthropic", selected: s.provider !== "openai" }, "Anthropic hoặc relay Anthropic"), h("option", { value: "openai", selected: s.provider === "openai" }, "OpenAI hoặc tương thích OpenAI"));
  const authType = h("select", { "aria-label": "Loại xác thực" }, h("option", { value: "api_key", selected: s.auth_type !== "token" }, "API key (ANTHROPIC_API_KEY)"), h("option", { value: "token", selected: s.auth_type === "token" }, "Auth token (ANTHROPIC_AUTH_TOKEN)"));
  const save = guard(async (ev) => {
    ev.preventDefault();
    if (!key.value.trim()) throw new Error("Hãy nhập key");
    if (provider.value === "openai" && !modelStrong.value.trim()) throw new Error("OpenAI cần tên model chính");
    await api("POST", "/api/settings/llm-key", { key: key.value, base_url: baseUrl.value || null, auth_type: authType.value, provider: provider.value, model_strong: modelStrong.value || null, model_fast: modelFast.value || null });
    key.value = "";
    toast("Đã kiểm tra và lưu key");
    await refreshMe();
  });
  const remove = guard(async () => { await api("DELETE", "/api/settings/llm-key"); toast("Đã xóa key"); await refreshMe(); });
  const syncProvider = () => {
    const isOpenAI = provider.value === "openai";
    authType.classList.toggle("hidden", isOpenAI);
    modelStrong.placeholder = isOpenAI ? "Model chính (bắt buộc)" : "Model chính (để trống để dùng mặc định Claude)";
  };
  provider.addEventListener("change", syncProvider);
  syncProvider();
  main.append(h("h2", {}, "Khóa LLM"),
    h("div", { class: "card" }, h("div", {}, describe, s.last4 && ` (kết thúc bằng ${s.last4})`),
      s.provider && h("div", { class: "small" }, `Nhà cung cấp: ${s.provider === "openai" ? "OpenAI" : "Anthropic"}${s.model_strong ? ` · Model: ${s.model_strong}${s.model_fast ? `, ${s.model_fast}` : ""}` : ""}`),
      s.base_url && h("div", { class: "small" }, "Endpoint: " + s.base_url),
      s.updated_at && h("div", { class: "small" }, `Cập nhật ${fmtTime(s.updated_at)} bởi ${s.updated_by}`),
      h("form", { onsubmit: save }, h("div", { class: "row" }, provider, authType, baseUrl), h("div", { class: "row" }, modelStrong, modelFast), h("div", { class: "row" }, key, h("button", { type: "submit" }, "Kiểm tra và lưu"), s.source === "ui" && h("button", { type: "button", class: "danger", onclick: remove }, "Xóa key"))),
      h("p", { class: "small" }, "Key dùng chung cho cả tổ chức, được kiểm tra trước khi lưu, mã hóa khi lưu và không hiển thị lại. Với relay và OpenAI, bước kiểm tra gửi một tin nhắn rất ngắn; nội dung nhiệm vụ đi qua máy chủ của dịch vụ đó. Giá trị nhập ở đây được ưu tiên hơn biến môi trường.")));
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
  main.append(h("h2", {}, "Đổi mật khẩu"), h("form", { class: "card", onsubmit: submit },
    h("div", { class: "row" }, field("Mật khẩu hiện tại", current), field("Mật khẩu mới (ít nhất 12 ký tự)", next), field("Nhập lại mật khẩu mới", again)),
    h("div", { class: "row" }, h("button", { type: "submit" }, "Đổi mật khẩu"))));
};

async function boot() {
  try { state.me = await api("GET", "/api/me"); } catch { return renderLogin(); }
  if (!state.me.tenants.length) { root.replaceChildren(h("div", { class: "card login" }, "Tài khoản chưa thuộc tenant nào.")); return; }
  state.tenant = state.tenant || state.me.tenants[0];
  renderShell();
}
boot();
