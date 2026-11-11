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
const CLASS = { proposed: "warn", disabled: "bad", success: "ok", ok: "ok", active: "ok", auto: "ok", approved: "ok", published: "ok", failure: "bad", error: "bad", blocked: "bad", denied: "bad", rejected: "bad", deprecated: "bad", invalid: "bad", pending: "warn", candidate: "warn", pending_review: "warn", partial: "warn", expired: "warn" };
const LABEL = { success: "thành công", failure: "thất bại", partial: "một phần", unknown: "chưa rõ", ok: "ok", error: "lỗi", blocked: "bị chặn", pending: "chờ duyệt", auto: "tự chạy", approved: "đã duyệt", denied: "từ chối", expired: "hết hạn", invalid: "sai tham số", candidate: "ứng viên", active: "hiệu lực", deprecated: "hết hiệu lực", rejected: "bị loại", pending_review: "chờ duyệt", proposed: "đề xuất", disabled: "đã tắt", published: "đã publish", noop: "không đổi", stale: "đã cũ", reverted: "đã hoàn tác" };
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

const TABS = [["connections", "Kết nối công cụ"], ["episodes", "Nhật ký"], ["knowledge", "Tri thức"], ["approvals", "Duyệt lệnh"], ["compile", "Biên dịch L2"], ["policy", "Công cụ & Chính sách"], ["metrics", "Đo lường"], ["audit", "Audit"], ["settings", "Cài đặt"], ["account", "Tài khoản"]];
const VIEWS = {};
let conversationList = null;

function closeSidebar() { document.querySelector(".sidebar")?.classList.remove("open"); document.querySelector(".scrim")?.classList.remove("open"); }
// Vị trí hiện tại nằm trên URL (#/t/<tenant>/<màn>[/<hội thoại>]) để refresh, Back/Forward và link mở đúng chỗ.
function routeHash() {
  return `#/t/${state.tenant.id}/${state.tab}${state.tab === "chat" && state.conversationId ? "/" + state.conversationId : ""}`;
}
function syncUrl(replace) {
  if (!state.tenant || location.hash === routeHash()) return;
  history[replace ? "replaceState" : "pushState"](null, "", routeHash());
}
function applyHash() {
  const [, t, tenantId, tab, conv] = location.hash.split("/");
  if (t !== "t") return;
  const tenant = state.me.tenants.find((x) => x.id === tenantId);
  if (tenant) state.tenant = tenant;
  const allowed = tab === "chat" || TABS.some(([id]) => id === tab && (id !== "settings" || state.me.is_org_admin));
  state.tab = allowed ? tab : "chat";
  state.conversationId = state.tab === "chat" && conv ? conv : null;
}
window.addEventListener("popstate", () => { if (state.me) { applyHash(); renderShell(); } });
function go(tab, conversationId) { state.tab = tab; if (conversationId !== undefined) state.conversationId = conversationId; syncUrl(); renderShell(); }

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
  const tenants = h("select", { "aria-label": "Tenant", onchange: (e) => { state.tenant = state.me.tenants.find((t) => t.id === e.target.value); state.env = store.get("env", state.env); go("chat", null); } },
    state.me.tenants.map((t) => h("option", { value: t.id, selected: t.id === state.tenant.id }, t.name)));
  const logout = guard(async () => { await api("POST", "/api/auth/logout"); state.me = null; state.conversationId = null; state.tab = "chat"; history.replaceState(null, "", location.pathname); renderLogin(); });
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
  main.append = (...kids) => appendKids(main, kids);
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
    detail.replaceChildren(h("div", { class: "card" }, h("h3", {}, "Lịch sử: " + ku.title),
      h("div", { class: "small" }, `Nguồn: ${ku.source_trust} · ${ku.source_eps.length} episode · phiên bản ${ku.version}`),
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
    consolidated.replaceChildren(progress);
    while (job.status === "running") {
      progress.textContent = `Đang tổng hợp ${job.considered} ứng viên: xong ${job.done}/${job.total} lô… (có thể mất vài phút, cứ để trang mở)`;
      await new Promise((r) => setTimeout(r, 3000));
      if (!document.contains(progress)) return;  // người dùng đã rời trang hoặc đóng đề xuất
      job = await api("GET", `/api/ku/consolidate/${job.id}`);
    }
    if (job.status === "error") { store.set("ku:consolidate", null); consolidated.replaceChildren(); throw new Error("Không tổng hợp được: " + job.errors.join("; ")); }
    renderProposal(job);
    if (job.errors.length) toast(`${job.errors.length} lô bị lỗi: ${job.errors[0]}`, true);
  }
  async function resume() {
    const id = store.get("ku:consolidate", null);
    if (!id || !isAdmin()) return;
    try { await follow(await api("GET", `/api/ku/consolidate/${id}`)); }
    catch { store.set("ku:consolidate", null); consolidated.replaceChildren(); }  // máy chủ đã khởi động lại: đề xuất không còn
  }
  bulkBar.append(h("label", { class: "small" }, selectAll, " Chọn tất cả"), countLabel, h("span", { class: "spacer" }),
    h("button", { type: "button", class: "needs-sel", onclick: doBatch(true) }, "Chấp nhận đã chọn"),
    h("button", { type: "button", class: "danger needs-sel", onclick: doBatch(false) }, "Loại đã chọn"),
    h("button", { type: "button", class: "ghost", onclick: consolidate, title: "LLM gom các KU trùng ý và đề xuất loại KU ít giá trị; chưa thay đổi gì cho tới khi bạn chấp nhận" }, "Tổng hợp trùng lặp"));

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
        card.replaceChildren(h("div", { class: "small" }, `Đã gộp ${g.ids.length} KU thành "${t.value}"${activate ? " và chấp nhận" : " (vẫn là ứng viên)"}.`));
      };
      card.applyMerge = apply;  // dùng cho nút "Gộp và chấp nhận tất cả", giữ nội dung người dùng đã sửa
      const merge = (activate) => guard(async () => { await apply(activate); await load(); });
      card.append(h("div", { class: "row spread" }, h("b", {}, `Gộp ${g.ids.length} KU`), h("span", { class: "badge info" }, g.kind)),
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
        e.target.closest(".card").replaceChildren(h("div", { class: "small" }, `Đã loại ${r.done} KU.`));
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
    consolidated.replaceChildren(h("h3", {}, `Đề xuất tổng hợp (đã xét ${p.considered} ứng viên)`),
      p.groups.length > 0 && h("div", { class: "card row" }, h("span", { class: "small" }, `${p.groups.length} nhóm gộp ${mergedCount} KU thành ${p.groups.length}; ${p.drop.length} KU đề xuất loại. Sửa hoặc bỏ qua nhóm chưa ổn rồi chấp nhận phần còn lại.`),
        h("span", { class: "spacer" }), h("button", { type: "button", onclick: acceptAll }, `Gộp và chấp nhận tất cả ${p.groups.length} nhóm`)),
      ...(p.groups.length || p.drop.length ? [...p.groups.map(groupCard), dropCard] : [h("div", { class: "card muted" }, "Không thấy KU trùng ý hay ít giá trị.")]),
      h("div", { class: "row" }, h("button", { type: "button", class: "ghost", onclick: () => { store.set("ku:consolidate", null); consolidated.replaceChildren(); } }, "Đóng đề xuất")));
  }

  async function load() {
    const q = new URLSearchParams();
    if (statusSel.value) q.set("status", statusSel.value);
    if (kindSel.value) q.set("kind", kindSel.value);
    current = await api("GET", "/api/ku" + (q.toString() ? "?" + q : ""));
    selected.clear();
    selectAll.checked = false;
    const picking = isAdmin() && statusSel.value === "candidate";
    box.replaceChildren(...(current.length ? current.map((k) => h("div", { class: "card" },
      h("div", { class: "row spread" }, h("div", { class: "row" }, picking && h("input", { type: "checkbox", class: "ku-pick", "aria-label": "Chọn " + k.title,
        onchange: (e) => { e.target.checked ? selected.add(k.id) : selected.delete(k.id); syncBulk(); } }), h("h3", {}, k.title)),
        h("div", {}, badge(k.status), h("span", { class: "badge info" }, k.kind), h("span", { class: "badge" }, k.source_trust), h("span", { class: "badge" }, k.domain))),
      h("div", {}, k.content), k.applies_when && h("div", { class: "small" }, "Áp dụng: " + k.applies_when),
      h("div", { class: "small" }, `✓${k.helpful_cnt} ✗${k.harmful_cnt} · tin cậy ${k.confidence}`),
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
  main.append(h("h2", {}, "Tri thức (L1)"), h("div", { class: "row" }, statusSel, kindSel, isAdmin() && h("button", { onclick: learn }, "Chạy học ngay")),
    h("div", { class: "card row" }, kind, title, content, h("button", { class: "ghost", onclick: add }, "Thêm KU")), detail, bulkBar, consolidated, box);
  await load();
  resume().catch((e) => toast(e.message, true));
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
      a.can_decide ? h("div", { class: "row" }, h("button", { onclick: act("approve_once") }, a.mine ? "Chạy lệnh" : "Duyệt một lần"), h("button", { class: "ghost", onclick: act("approve_session") }, "Cho phép trong 1 giờ"), reason, h("button", { class: "danger", onclick: act("deny") }, "Từ chối"))
        : h("div", { class: "small" }, a.mine ? "Tenant này yêu cầu người khác duyệt lệnh của bạn." : "Bạn không có quyền duyệt yêu cầu này."));
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
      h("div", { class: "small" }, t.description + (t.rollback_hint ? ` · Hoàn tác: ${t.rollback_hint}` : "")),
      !removed && h("div", { class: "tool-perms" }, h("span", { class: "small" }, "Môi trường"), envs, !t.personal && h("span", { class: "small sep" }, "Vai trò"), t.personal ? null : roles, h("span", { class: "small sep" }, "Mức"), tier),
      t.warnings.length > 0 && h("div", { class: "small warn-text" }, "Cảnh báo: " + t.warnings.join("; ")));
  }

  function paint() {
    const q = search.value.trim().toLowerCase();
    const match = { all: () => true, enabled: (t) => t.enabled, off: (t) => !t.enabled, personal: (t) => t.personal }[filter.value];
    const shown = list.filter((t) => match(t) && (!q || t.tool.includes(q) || t.description.toLowerCase().includes(q)));
    const groups = new Map();
    for (const t of shown) { if (!groups.has(t.server)) groups.set(t.server, []); groups.get(t.server).push(t); }
    box.replaceChildren(
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
    chips.replaceChildren(h("span", { class: "small" }, "Môi trường"), ...["dev", "staging", "prod"].map((e) => chip(e, envs.includes(e), () => { envs = flip(envs, e); store.set(`${draft}:envs`, envs); paintChips(); })),
      ...(scope.value === "tenant" ? [h("span", { class: "small sep" }, "Vai trò được dùng"), ...Object.keys(ROLE_LABEL).map((r) => chip(ROLE_LABEL[r], roles.includes(r), () => { roles = flip(roles, r); store.set(`${draft}:roles`, roles); paintChips(); }))] : []));
  }
  function paintFields() {
    const tpl = templates.find((t) => t.name === tplSelect.value);
    desc.textContent = tpl.description + " Công cụ: " + tpl.tools.map((t) => `${t.name} (${t.risk_tier})`).join(", ") + ".";
    for (const k of Object.keys(inputs)) delete inputs[k];
    fieldsBox.replaceChildren(...tpl.fields.map((f) => {
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

VIEWS.connections = async (main) => {
  const [templates, list] = await Promise.all([api("GET", "/api/templates"), api("GET", "/api/connections")]);
  const editor = h("div");
  const reload = () => { state.tab = "connections"; renderShell(); };
  const action = (c, fn) => guard(async () => { await fn(); reload(); });
  function card(c) {
    const result = h("div", { class: "small" });
    const test = guard(async () => { result.textContent = "Đang kiểm tra…"; const r = await api("POST", `/api/connections/${c.id}/test`); result.className = "small " + (r.ok ? "ok-text" : "warn-text"); result.textContent = r.ok ? "Kết nối được: " + JSON.stringify(r.detail) : "Lỗi: " + r.error; });
    const remove = h("button", { type: "button", class: "danger small-btn", onclick: () => {
      if (remove.dataset.confirm) action(c, () => api("DELETE", `/api/connections/${c.id}`))();
      else { remove.dataset.confirm = "1"; remove.textContent = "Bấm lần nữa để gỡ"; setTimeout(() => { delete remove.dataset.confirm; remove.textContent = "Gỡ"; }, 4000); }
    } }, "Gỡ");
    return h("div", { class: "card" },
      h("div", { class: "tool-head" }, h("strong", {}, c.title), h("code", {}, c.name), h("span", { class: "badge" }, c.template_title), h("span", { class: "badge " + (c.personal ? "info" : "") }, c.personal ? "cá nhân" : "dùng chung"),
        badge(c.status === "active" ? "active" : "disabled"), c.allow_private ? h("span", { class: "badge warn" }, "mạng nội bộ") : null, h("span", { class: "spacer" }),
        h("button", { type: "button", class: "ghost small-btn", onclick: test }, "Kiểm tra"),
        c.can_manage && h("button", { type: "button", class: "ghost small-btn", onclick: () => { openEditor(c); editor.scrollIntoView(); } }, "Sửa"),
        c.can_manage && h("button", { type: "button", class: "ghost small-btn", onclick: action(c, () => api("PATCH", `/api/connections/${c.id}`, { status: c.status === "active" ? "disabled" : "active" })) }, c.status === "active" ? "Tạm tắt" : "Bật lại"),
        c.can_manage && remove),
      h("div", { class: "small" }, `Môi trường: ${c.envs.join(", ")}` + (c.personal ? "" : ` · vai trò: ${c.roles.join(", ")}`)),
      h("div", { class: "tool-perms" }, c.tools.map((t) => h("span", { class: "badge " + (t.enabled ? "ok" : "") }, `${t.tool.split(".")[1]} ${t.risk_tier}${t.enabled ? "" : " · tắt"}`))),
      result);
  }
  const closeEditor = () => { store.set("conn:editing", null); reload(); };
  function openEditor(c) { store.set("conn:editing", c.id); editor.replaceChildren(connectionForm(templates, c, closeEditor)); }
  const shared = list.filter((c) => !c.personal), mine = list.filter((c) => c.personal);
  const reopen = list.find((c) => c.id === store.get("conn:editing", null) && c.can_manage);
  if (reopen) openEditor(reopen);
  main.append(h("h2", {}, "Kết nối công cụ"),
    h("p", { class: "small" }, "Tạo công cụ thật từ template bằng tham số của riêng bạn. Kết nối dùng chung do admin tạo và quản lý; kết nối cá nhân chỉ bạn thấy và dùng. Thông tin bí mật được mã hóa và không hiển thị lại. Kết nối vào mạng nội bộ cần org admin cho phép."),
    connectionForm(templates, null, reload), editor,
    h("h2", {}, `Dùng chung (${shared.length})`), ...(shared.length ? shared.map(card) : [h("div", { class: "card muted" }, "Chưa có kết nối dùng chung.")]),
    h("h2", {}, `Của bạn (${mine.length})`), ...(mine.length ? mine.map(card) : [h("div", { class: "card muted" }, "Bạn chưa có kết nối cá nhân.")]));
};

function learnedToolsSection(personalOnly) {
  const box = h("div", {}, h("div", { class: "card muted" }, "Đang tải…"));
  const tierClass = (r) => ({ R3: "warn", R4: "bad" }[r] || "");
  const load = async () => {
    const list = (await api("GET", "/api/learned-tools")).filter((t) => (personalOnly ? t.personal : !t.personal));
    box.replaceChildren(...(list.length ? list.map(item) : [h("div", { class: "card muted" }, personalOnly
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
    wrap.append(h("h2", {}, "Cá nhân hóa"),
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

const GRANT_MODE = { off: "không phủ", read: "chỉ lệnh đọc", all: "mọi lệnh" };

function grantPolicyCard(p) {
  const envs = ["dev", "staging", "prod"];
  const controls = {};
  const rows = envs.map((e) => {
    const cur = p.grant_policy[e];
    const mode = h("select", { "aria-label": `Phạm vi ở ${e}`, disabled: !isAdmin() },
      Object.entries(GRANT_MODE).filter(([k]) => !(e === "prod" && k === "all")).map(([k, label]) => h("option", { value: k, selected: cur.untrusted === k }, label)));
    const conf = h("input", { type: "number", min: 0, max: 1, step: 0.05, value: String(cur.min_confidence), "aria-label": `Tin cậy tối thiểu ở ${e}`, disabled: !isAdmin() });
    const auto = h("select", { "aria-label": `Lệnh đọc tự chạy ở ${e}`, disabled: !isAdmin() },
      [["none", "không"], ["R0", "R0 (đọc thường)"], ["R1", "R0, R1 (cả đọc nhạy cảm)"]].filter(([k]) => !(e === "prod" && k === "R1"))
        .map(([k, label]) => h("option", { value: k, selected: cur.untrusted_auto === k }, label)));
    controls[e] = { mode, conf, auto };
    return h("tr", {}, h("td", {}, e), h("td", {}, auto), h("td", {}, mode), h("td", {}, conf));
  });
  const save = guard(async () => {
    const grant_policy = Object.fromEntries(envs.map((e) => [e, { untrusted: controls[e].mode.value, min_confidence: Number(controls[e].conf.value), untrusted_auto: controls[e].auto.value }]));
    await api("PATCH", "/api/tenant", { grant_policy });
    toast("Đã lưu");
    renderShell();
  });
  return h("div", { class: "card" },
    h("p", { class: "small" }, "Khi kế hoạch của agent chịu ảnh hưởng từ nội dung untrusted (log, trang web), mọi lệnh đều phải hỏi. Bảng này quy định, ở từng môi trường: lệnh đọc mức nào vẫn tự chạy; và quyền \"Cho phép trong 1 giờ\" có được thay cho việc hỏi lại không, với điểm tin cậy tối thiểu của lệnh. Không có nội dung untrusted thì quyền này luôn có hiệu lực. Ở prod không cho phủ lệnh ghi."),
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Môi trường"), h("th", {}, "Lệnh đọc tự chạy, không cần grant"), h("th", {}, "Grant phủ thêm"), h("th", {}, "Tin cậy tối thiểu của grant"))), h("tbody", {}, rows)),
    isAdmin() && h("div", { class: "row" }, h("button", { onclick: save }, "Lưu")));
}

VIEWS.policy = async (main) => {
  const [tools, p] = await Promise.all([api("GET", "/api/tools"), api("GET", "/api/policy")]);
  const cellText = (c) => (typeof c === "string" ? ({ auto: "tự động", ask: "luôn hỏi", block: "chặn" }[c]) : c.ask_below !== undefined ? `hỏi nếu < ${c.ask_below}` : `hỏi, ${c.ask} người`);
  const editor = persist(h("textarea", { class: "code", value: JSON.stringify(p.overlay, null, 2), "aria-label": "Overlay chính sách" }), "policy:overlay");
  const save = guard(async () => { let overlay; try { overlay = JSON.parse(editor.value); } catch { throw new Error("JSON không hợp lệ"); } await api("PUT", "/api/policy", { overlay }); store.set("policy:overlay", null); toast("Đã lưu chính sách"); renderShell(); });
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
    isAdmin() && h("div", { class: "row" }, h("button", { class: "ghost", onclick: setFlag("change_freeze", !p.change_freeze) }, p.change_freeze ? "Tắt change freeze" : "Bật change freeze"), h("button", { class: "ghost", onclick: setFlag("auto_promote", !p.auto_promote) }, p.auto_promote ? "Tắt tự động thăng hạng" : "Bật tự động thăng hạng"),
      h("button", { class: "ghost", onclick: setFlag("self_approve", !p.self_approve) }, p.self_approve ? "Bắt buộc người khác duyệt lệnh" : "Cho người dùng tự duyệt lệnh")),
    h("div", { class: "small" }, p.self_approve ? "Người dùng tự xác nhận lệnh cần duyệt của chính mình. Thay đổi công cụ dùng chung vẫn cần admin." : "Lệnh cần duyệt phải do người có quyền duyệt khác xác nhận; R3 ở prod cần 2 người.")),
    h("h2", {}, `Cho phép trong ${p.grant_ttl_minutes} phút`), grantPolicyCard(p),
    h("h2", {}, "Thử quyết định"), h("div", { class: "card" }, h("div", { class: "row" }, simTool, simEnv, simConf, h("button", { class: "ghost", onclick: sim }, "Thử")), simOut),
    isAdmin() && h("h2", {}, "Overlay của tenant (chỉ được siết)"), isAdmin() && h("div", { class: "card" }, editor, h("div", { class: "row" }, h("button", { onclick: save }, "Lưu"))),
    h("h2", {}, "Công cụ học được"), learnedToolsSection(false),
    h("h2", {}, "Danh mục công cụ"), toolCatalogSection());
};

const PURPOSE = { agent: "Agent trả lời", route: "Router", check_plan: "Kiểm tra độc lập", reflect: "Reflector", curate: "Curator", judge: "Chấm kết quả", other: "Khác" };
const num = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("vi-VN"));

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
  kids.flat(Infinity).forEach((k) => k !== null && k !== undefined && k !== false && el.append(k));
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
    tip.replaceChildren(h("div", { class: "row-tip" }, h("b", {}, d.split("-").reverse().join("/")), h("b", {}, num(totals[i]) + " token")), ...(rows.length ? rows : [h("div", { class: "small" }, "Không có lần gọi nào")]));
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
  wrap.append(chart, tip);
  return h("div", { class: "card" },
    h("div", { class: "chart-legend" }, series.map((s) => h("span", {}, h("span", { class: `sw ${s.cls}` }), s.label))),
    wrap);
}

function tokenSection(t) {
  if (!t) return null;
  const all = t.by_purpose.reduce((a, r) => a + r.input + r.cache_read + r.cache_write + r.output, 0);
  const table = (head, rows) => h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, head.map((x) => h("th", {}, x)))), h("tbody", {}, rows)));
  return h("div", {},
    h("h2", { class: "row" }, `Token ${t.days} ngày qua`),
    h("div", { class: "grid" }, h("div", { class: "tile" }, h("b", {}, num(all)), h("span", { class: "small" }, "Tổng token xử lý")),
      h("div", { class: "tile" }, h("b", {}, t.cache_hit_pct === null ? "—" : t.cache_hit_pct + "%"), h("span", { class: "small" }, "Input đọc từ cache"))),
    table(["Mục đích", "Lần gọi", "Input", "Cache đọc", "Cache ghi", "Output"], t.by_purpose.map((r) =>
      h("tr", {}, h("td", {}, PURPOSE[r.purpose] || r.purpose), h("td", {}, num(r.calls)), h("td", {}, num(r.input)), h("td", {}, num(r.cache_read)), h("td", {}, num(r.cache_write)), h("td", {}, num(r.output))))),
    h("h2", { class: "row" }, "Token theo LLM, 14 ngày qua"),
    h("p", { class: "small" }, "Tổng token xử lý mỗi ngày (input, cache đọc/ghi, output), chia theo cấu hình LLM và model. Rê chuột lên cột để xem chi tiết."),
    llmUsageChart(t),
    table(["LLM", "Lần gọi", "Input", "Cache đọc", "Cache ghi", "Output", "Tổng"], (t.by_llm || []).map((r) =>
      h("tr", {}, h("td", {}, r.label), h("td", {}, num(r.calls)), h("td", {}, num(r.input)), h("td", {}, num(r.cache_read)), h("td", {}, num(r.cache_write)), h("td", {}, num(r.output)), h("td", {}, num(r.total))))),
    h("h2", { class: "row" }, "Công cụ trả kết quả lớn nhất"),
    h("p", { class: "small" }, "Kết quả công cụ đi thẳng vào context của agent; công cụ trả nhiều ký tự là chỗ nên lọc trước."),
    table(["Công cụ", "Lần gọi", "Tổng ký tự", "Lớn nhất"], t.tools.map((r) => h("tr", {}, h("td", {}, h("code", {}, r.tool)), h("td", {}, num(r.calls)), h("td", {}, num(r.result_chars)), h("td", {}, num(r.max_chars))))),
    h("h2", { class: "row" }, "Lượt tốn token nhất"),
    table(["Thời gian", "Yêu cầu", "Lần gọi LLM", "Input", "Cache đọc", "Output"], t.top_turns.map((r) =>
      h("tr", {}, h("td", {}, fmtTime(r.started_at)), h("td", {}, r.task), h("td", {}, num(r.llm_calls)), h("td", {}, num(r.input + r.cache_write)), h("td", {}, num(r.cache_read)), h("td", {}, num(r.output))))));
}

VIEWS.metrics = async (main) => {
  const m = await api("GET", "/api/metrics");
  const tile = (label, value) => h("div", { class: "tile" }, h("b", {}, value === null || value === undefined ? "—" : value), h("span", { class: "small" }, label));
  main.append(h("h2", {}, "Đo lường"), h("div", { class: "grid" }, tile("Episode", m.episodes), tile("Có kết quả (%)", m.labeled_pct), tile("Người phải sửa (%)", m.edited_pct), tile("Thất bại (%)", m.failed_pct), tile("Token mỗi nhiệm vụ", m.avg_tokens), tile("Tỷ lệ chấp nhận KU (%)", m.ku_accept_pct)),
    h("h2", { class: "row" }, "Trạng thái KU và quyết định công cụ"), h("div", { class: "card" }, h("div", {}, Object.entries(m.ku_status).map(([k, v]) => h("span", { class: "badge " + (CLASS[k] || "") }, `${LABEL[k] || k}: ${v}`))), h("div", { class: "row" }, Object.entries(m.tool_decisions).map(([k, v]) => h("span", { class: "badge " + (CLASS[k] || "") }, `${LABEL[k] || k}: ${v}`)))),
    tokenSection(m.tokens),
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
    modelList.replaceChildren(...r.models.map((m) => h("option", { value: m.id }, m.name !== m.id ? m.name : null)));
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
  main.append(h("h2", {}, "LLM"),
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
  main.append(personalSection(), h("h2", {}, "Đổi mật khẩu"), h("form", { class: "card", onsubmit: submit },
    h("div", { class: "row" }, field("Mật khẩu hiện tại", current), field("Mật khẩu mới (ít nhất 12 ký tự)", next), field("Nhập lại mật khẩu mới", again)),
    h("div", { class: "row" }, h("button", { type: "submit" }, "Đổi mật khẩu"))));
};

async function boot() {
  try { state.me = await api("GET", "/api/me"); } catch { return renderLogin(); }
  if (!state.me.tenants.length) { root.replaceChildren(h("div", { class: "card login" }, "Tài khoản chưa thuộc tenant nào.")); return; }
  state.tenant = state.tenant || state.me.tenants[0];
  applyHash();
  state.env = store.get("env", (state.me.profile && state.me.profile.default_env) || "dev");
  syncUrl(true);
  renderShell();
}
boot();
