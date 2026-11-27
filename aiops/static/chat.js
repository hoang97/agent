"use strict";
// Giao diện chat. Markdown được dựng thành nút DOM, không dùng innerHTML, nên nội dung model không thể chèn HTML.

const ICONS = {
  send: "M12 19V5M5 12l7-7 7 7",
  stop: "M7 7h10v10H7z",
  plus: "M12 5v14M5 12h14",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  check: "M5 13l4 4L19 7",
  up: "M7 11l4-8a2 2 0 012 2v4h6l-2 10H7zM7 11H3v9h4",
  down: "M17 13l-4 8a2 2 0 01-2-2v-4H5l2-10h10zM17 13h4V4h-4",
  edit: "M4 20h4L19 9l-4-4L4 16zM13 7l4 4",
  retry: "M4 4v6h6M20 20v-6h-6M20 9A8 8 0 006 5.3L4 10M4 15a8 8 0 0014 3.7l2-4.7",
  menu: "M4 6h16M4 12h16M4 18h16",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  chevron: "M9 6l6 6-6 6",
  down_arrow: "M12 5v14M5 12l7 7 7-7",
  save: "M6 4h12v16l-6-4-6 4z",
  book: "M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3zM5 17a3 3 0 013-3h11",
  alert: "M12 3l9 16H3zM12 10v4M12 17v.01",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
  tool: "M15 4a5 5 0 00-4.6 6.9L4 17.3 6.7 20l6.4-6.4A5 5 0 0020 9l-3 3-3-1-1-3z",
  list: "M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01",
  chart: "M4 4v16h16M8 16v-5M12 16V8M16 16v-3",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 100 6 3 3 0 000-6z",
  gear: "M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4",
  user: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0",
  chat: "M4 5h16v11H9l-5 4z",
  x: "M6 6l12 12M18 6L6 18",
  panel: "M4 4h16v16H4zM10 4v16M7 10l-2 2 2 2",
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  addKids(svg, path);
  return svg;
}

const iconButton = (name, label, onclick, extra) => h("button", { type: "button", class: "icon-btn " + (extra || ""), "aria-label": label, title: label, onclick }, icon(name));

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch { toast("Trình duyệt không cho sao chép", true); return; }
  if (!btn) return;
  setKids(btn, icon("check"));
  setTimeout(() => setKids(btn, icon("copy")), 1500);
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\s][^*\n]*\*)|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>)]+)/g;

function inlineMd(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("code", {}, m[1].slice(1, -1)));
    else if (m[2]) out.push(h("strong", {}, inlineMd(m[2].slice(2, -2))));
    else if (m[3]) out.push(h("em", {}, m[3].slice(1, -1)));
    else if (m[5]) out.push(h("a", { href: m[5], target: "_blank", rel: "noopener noreferrer" }, m[4]));
    else if (m[6]) out.push(h("a", { href: m[6], target: "_blank", rel: "noopener noreferrer" }, m[6]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function codeBlock(code, lang) {
  const copy = iconButton("copy", "Sao chép mã", (e) => copyText(code, e.currentTarget));
  return h("div", { class: "code-block" }, h("div", { class: "code-head" }, h("span", {}, lang || "code"), copy), h("pre", {}, h("code", {}, code)));
}

const splitRow = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

function renderMarkdown(text) {
  const frag = document.createDocumentFragment();
  const lines = text.split("\n");
  let i = 0;
  const isBlockStart = (l) => /^(```|#{1,6}\s|>\s?|\s*[-*+]\s+|\s*\d+\.\s+|---+\s*$)/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```\s*([\w+-]*)/);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      addKids(frag, codeBlock(body.join("\n"), fence[1]));
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)/);
    if (heading) { addKids(frag, h("h" + Math.min(heading[1].length + 2, 6), {}, inlineMd(heading[2]))); i++; continue; }
    if (/^---+\s*$/.test(line)) { addKids(frag, h("hr")); i++; continue; }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
      addKids(frag, h("blockquote", {}, renderMarkdown(quote.join("\n"))));
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const head = splitRow(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) body.push(splitRow(lines[i++]));
      addKids(frag, h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inlineMd(c))))), h("tbody", {}, body.map((r) => h("tr", {}, r.map((c) => h("td", {}, inlineMd(c)))))))));
      continue;
    }
    const list = line.match(/^\s*([-*+]|\d+\.)\s+/);
    if (list) {
      const ordered = /\d/.test(list[1]);
      const pattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/;
      const items = [];
      while (i < lines.length && pattern.test(lines[i])) items.push(h("li", {}, inlineMd(lines[i++].replace(pattern, ""))));
      addKids(frag, h(ordered ? "ol" : "ul", {}, items));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    const p = h("p");
    para.forEach((l, n) => { if (n) addKids(p, h("br")); addKids(p, ...inlineMd(l).map((x) => (x instanceof Node ? x : document.createTextNode(x)))); });
    addKids(frag, p);
  }
  return frag;
}

// ---------- hành động của agent: dòng gọn, xếp xen kẽ với chữ theo thứ tự đã xảy ra (như Claude Code trên VS Code) ----------

const SUMMARY_KEYS = ["command", "query", "expr", "sql", "url", "path", "matchers", "index", "name"];
const STATUS_ICON = { ok: "✓", error: "✕", blocked: "⊘", invalid: "✕", denied: "✕", pending: "…", asked: "?", running: "" };

const TOOL_LABEL = { "agent.delegate": "agent con", "agent.run_background": "chạy nền" };
function shortTool(tool) { return TOOL_LABEL[tool] || String(tool || "").split(".").pop(); }

// Một dòng tóm tắt tham số: lệnh, query, URL... thay cho JSON.
function argSummary(args) {
  if (!args || typeof args !== "object") return "";
  const key = SUMMARY_KEYS.find((k) => typeof args[k] === "string" && args[k].trim());
  const main = key ? args[key] : Object.values(args).find((v) => typeof v === "string") || "";
  const extra = args.service ? `${args.service} ${args.method || "GET"} ` : "";
  return (extra + main).replace(/\s+/g, " ").trim();
}

// CSS cắt dòng dài bằng …; di chuột vào để xem đủ lệnh.
function summaryEl(text) {
  return h("code", { class: "tool-summary", title: text || "" }, text || "");
}

function fmtMs(ms) { return ms === undefined || ms === null ? "" : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)}s`; }

// Kết quả: chữ dài cắt 20 dòng, danh sách object thành bảng nhỏ, còn lại JSON gọn.
function resultView(result) {
  if (result === undefined || result === null) return null;
  const text = typeof result === "string" ? result : typeof result.stdout === "string" ? result.stdout : typeof result.text === "string" ? result.text : null;
  if (text !== null) return clippedPre(text, result.stderr);
  const list = Array.isArray(result) ? result : Object.values(result).find((v) => Array.isArray(v) && v.length && v.every((x) => x && typeof x === "object" && !Array.isArray(x)));
  if (list && list.length) {
    const cols = [...new Set(list.slice(0, 20).flatMap((r) => Object.keys(r)))].slice(0, 6);
    const cell = (v) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v)).slice(0, 120);
    return h("div", { class: "result-table" },
      h("table", {}, h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, c)))), h("tbody", {}, list.slice(0, 20).map((r) => h("tr", {}, cols.map((c) => h("td", {}, cell(r[c]))))))),
      list.length > 20 && h("div", { class: "small muted" }, `… còn ${list.length - 20} dòng`),
      h("details", { class: "raw" }, h("summary", { class: "small muted" }, "JSON đầy đủ"), h("pre", {}, JSON.stringify(result, null, 1))));
  }
  return clippedPre(JSON.stringify(result, null, 1));
}

function clippedPre(text, stderr) {
  const lines = String(text).split("\n");
  const pre = h("pre", { class: "tool-output" }, lines.slice(0, 20).join("\n"));
  const more = lines.length > 20 && h("button", { type: "button", class: "ghost small-btn", onclick: (e) => { pre.textContent = text; e.currentTarget.remove(); } }, `Xem đủ ${lines.length} dòng`);
  return h("div", {}, pre, more, stderr && stderr.trim() && h("pre", { class: "tool-output warn-text" }, stderr.trim().split("\n").slice(-10).join("\n")));
}

// Dòng của một lệnh: ký hiệu trạng thái, tên công cụ, tóm tắt, thời gian; bấm để mở kết quả.
function toolRow(step) {
  const status = step.status || "running";
  const kind = step.action_class && step.action_class !== "read" ? (step.action_class === "write" ? "k-write" : "k-danger") : "k-read";
  const stopped = status === "blocked" || status === "invalid";
  const line = h("button", { type: "button", class: "tool-line", "aria-expanded": "false" },
    h("span", { class: "tool-icon s-" + status, "aria-label": LABEL[status] || status }, status === "running" ? h("span", { class: "spinner" }) : STATUS_ICON[status] || "•"),
    h("span", { class: "tool-name" }, step.sub ? `#${step.sub} ` : "", shortTool(step.tool), step.sub_id ? ` #${step.sub_id}` : ""),
    summaryEl(argSummary(step.args)),
    h("span", { class: "tool-meta" }, status === "running" ? h("span", { class: "elapsed" }, "0s") : fmtMs(step.ms)),
    step.background && h("span", { class: "badge", title: "Chạy nền: agent làm việc khác trong lúc chờ" }, `nền #${step.background}`),
    step.untrusted && h("span", { class: "badge warn", title: "Kết quả chứa nội dung người ngoài ghi được (log, trang web...)" }, "nội dung ngoài"),
    status !== "running" && icon("chevron"));
  const detail = h("div", { class: "tool-detail", hidden: "" });
  const row = h("div", { class: `tool-row ${kind} s-${status}${step.sub ? " sub" : ""}` }, line,
    stopped && step.reasons && step.reasons.length > 0 && h("div", { class: "tool-note" }, step.reasons.join("; ")),
    status === "error" && step.result && step.result.error && h("div", { class: "tool-note" }, String(step.result.error).split("\n")[0].slice(0, 300)),
    step.change_id && changeLine({ id: step.change_id, status: "loading" }),
    detail);
  let filled = false;
  line.addEventListener("click", () => {
    if (status === "running") return;
    if (!filled) {
      filled = true;
      appendKids(detail, [h("div", { class: "small muted" }, `${step.tool} · ${step.env || ""}${step.risk_tier ? " · " + step.risk_tier : ""}${step.parallel ? " · chạy song song" : ""}`),
        h("pre", { class: "tool-args" }, JSON.stringify(step.args, null, 1)),
        !stopped && step.reasons && step.reasons.length > 0 && h("div", { class: "small muted" }, step.reasons.join("; ")),
        step.status === "ok" && step.undo && step.undo.checks.length > 0 && undoChecks(step.undo, "Vì sao tự chạy"),
        step.steps && h("div", { class: "sub-steps" }, step.steps.map((sub) => h("div", { class: "small" }, "↳ ", h("code", {}, sub.tool), " ", badge(sub.status)))),
        resultView(step.result)]);
    }
    detail.hidden = !detail.hidden;
    line.setAttribute("aria-expanded", String(!detail.hidden));
    row.classList.toggle("open", !detail.hidden);
  });
  return row;
}

// Phạm vi của "Cho phép trong 1 giờ" phải hiện rõ trên nút, như Claude Code: người dùng thấy hết những gì quyền đó sẽ cho phép.
function grantScope(a) {
  const cmd = a.args && typeof a.args.command === "string" ? a.args.command : "";
  const m = cmd.match(/(?:^|\s)(?:-n|--namespace)[= ](\S+)/);
  const ns = (a.args && a.args.namespace) || (m && m[1]);
  return `${shortTool(a.tool)} ở ${a.env}${ns ? `, namespace ${ns}` : ""}`;
}

// Lệnh kiểu kubectl hiện thành một dòng shell; tham số khác hiện theo từng trường.
function commandView(a) {
  const args = a.args || {};
  if (typeof args.command === "string") {
    const rest = Object.entries(args).filter(([k]) => k !== "command");
    return h("div", { class: "approval-args" }, h("pre", { class: "approval-code" }, "$ " + args.command),
      rest.map(([k, v]) => h("div", {}, h("span", { class: "muted" }, k + ": "), typeof v === "string" && v.includes("\n") ? h("pre", { class: "approval-code" }, v) : h("code", {}, typeof v === "string" ? v : JSON.stringify(v)))));
  }
  const entries = Object.entries(args);
  if (!entries.length) return null;
  return h("div", { class: "approval-args" }, entries.map(([k, v]) => typeof v === "string" && v.includes("\n")
    ? h("div", {}, h("span", { class: "muted" }, k + ":"), h("pre", { class: "approval-code" }, v))  // code của data.script: giữ xuống dòng để người duyệt đọc được
    : h("div", {}, h("span", { class: "muted" }, k + ": "), h("code", {}, typeof v === "string" ? v : JSON.stringify(v)))));
}

// Tóm tắt cho lãnh đạo của sự cố, máy chủ viết lại khi sự cố yên sau hoạt động mới (như session recap của Claude Code).
function summaryCard(s, incidentId) {
  const card = h("div", { class: "card incident-summary", role: "region", "aria-label": "Tóm tắt sự cố" });
  let cur = s || {};
  const paint = (next) => {
    cur = next || {};
    const meta = cur.running ? "đang viết lại…" : cur.at ? `cập nhật ${fmtTime(cur.at)}` + (cur.stale ? " · có hoạt động mới, sẽ viết lại khi sự cố yên" : "")
      : "sẽ tự viết khi sự cố yên sau lượt điều tra";
    setKids(card, 
      h("div", { class: "tool-head" }, h("strong", {}, "Tóm tắt sự cố"), h("span", { class: "small muted" }, meta), h("span", { class: "spacer" }),
        jobBtn(job, { label: "Tóm tắt ngay", busyLabel: "Đang tóm tắt…", onDone: () => { job = null; card.refresh(); },
          run: async () => { const r = await api("POST", `/api/incidents/${incidentId}/summary`); job = null; paint(r); } })),
      cur.text && h("p", { class: "summary-text" }, cur.text));
  };
  let job = null;  // lượt "Tóm tắt ngay" đang chạy hoặc vừa lỗi (jobs.py)
  paint(cur);
  jobsOf(`incident:${incidentId}`).then((jobs) => { job = jobs.summary || null; if (job) paint(cur); });
  card.refresh = async () => { paint(await api("GET", `/api/incidents/${incidentId}/summary`)); };
  return card;
}

// Thẻ duyệt: chờ duyệt thì đầy đủ (mô tả, lệnh, nút, ghi chú, chi tiết gập); đã quyết thì gọn như một dòng lệnh.
function approvalCard(a, onDecide, streaming, context) {
  const done = a.status !== "pending";
  const result = a.result && a.result.result;
  const failed = a.result && a.result.status === "error";
  if (done) {
    const icon_ = failed ? "✕" : a.status === "approved" ? "✓" : a.status === "denied" ? "✕" : "…";
    const head = failed ? "Đã duyệt nhưng chạy lỗi" : a.status === "approved" ? "Đã duyệt và chạy" : a.status === "denied" ? "Bạn đã từ chối"
      : a.status === "skipped" ? "Bỏ qua, chưa chạy" : "Hết hạn, chưa ai duyệt";
    const detail = h("div", { class: "tool-detail", hidden: "" }, commandView(a), result && resultView(result));
    const line = h("button", { type: "button", class: "tool-line" }, h("span", { class: "tool-icon s-" + (failed ? "error" : a.status) }, icon_),
      h("span", { class: "tool-name" }, head), summaryEl(a.why || argSummary(a.args)), icon("chevron"));
    line.addEventListener("click", () => { detail.hidden = !detail.hidden; });
    return h("div", { class: `tool-row decided s-${failed ? "error" : a.status}` }, line,
      a.status === "denied" && h("div", { class: "tool-note" }, a.deny_reason ? "Ghi chú cho agent: " + a.deny_reason : "Không kèm ghi chú, agent dừng ở đây."),
      a.status === "expired" && h("div", { class: "tool-note" }, (a.reasons && a.reasons.length ? "Cần duyệt vì: " + a.reasons.join("; ").replace(/[.\s]+$/, "") + ". " : "")
        + `Hết hạn lúc ${fmtTime(a.expires_at)}; thẻ hết hạn không duyệt được nữa, nhắn agent nếu vẫn cần lệnh này.`),
      a.status === "skipped" && h("div", { class: "tool-note" }, "Hội thoại có tin nhắn mới trước khi lệnh được duyệt; agent sẽ đề xuất lại nếu vẫn cần."),
      a.note && h("div", { class: "tool-note" }, "Ghi chú cho agent: " + a.note),
      a.change && changeLine(a.change), detail);
  }
  const dryError = a.dry_run && a.dry_run.error;
  const details = h("details", { class: "approval-more", open: dryError ? "" : null }, h("summary", { class: "small muted" }, dryError ? "Chạy thử báo lỗi" : "Chi tiết"),
    h("div", { class: "small muted" }, `${a.tool} · ${a.env} · ${a.risk_tier}`),
    a.reasons.length > 0 && h("div", { class: "small" }, "Cần duyệt vì: " + a.reasons.join("; ")),
    a.dry_run && h("div", { class: "small" + (dryError ? " warn-text" : "") }, (dryError ? "Lệnh nhiều khả năng sẽ thất bại: " : "Chạy thử: ") + (dryError || JSON.stringify(a.dry_run))),
    !a.undo && a.rollback_hint && h("div", { class: "small" }, "Cách hoàn tác: " + a.rollback_hint));
  const decidable = !streaming && a.can_decide && onDecide;
  const card = h("div", { class: "approval" + (decidable ? " waiting" : ""), tabindex: decidable ? "0" : null, "data-approval": a.id, "aria-label": "Lệnh chờ duyệt: " + (a.why || a.tool) },
    h("div", { class: "approval-head" }, h("strong", {}, a.why || `Agent muốn chạy ${shortTool(a.tool)}`), h("span", { class: "badge " + (a.risk_tier >= "R3" ? "bad" : "warn") }, a.risk_tier)),
    context, commandView(a),
    a.undo && undoChecks(a.undo, a.undo.soft ? "Xoá mềm" : "Hoàn tác"),
    streaming ? h("div", { class: "small muted" }, "Nút duyệt sẽ hiện khi agent trả lời xong.")
      : decidable ? approvalActions(a, onDecide)
      : h("div", { class: "small" }, `Đã có ${a.decided_by.length}/${a.required_approvals} người duyệt. Đang chờ người có quyền duyệt.`),
    details);
  if (decidable) {
    // Như Claude Code: 1 chạy, 2 chạy và cho phép trong 1 giờ, 3 từ chối; chỉ khi đang chọn thẻ, không phải khi gõ ghi chú.
    card.addEventListener("keydown", (e) => {
      if (e.target !== card) return;
      const action = { 1: "approve_once", 2: "approve_session", 3: "deny" }[e.key];
      if (action) { e.preventDefault(); card.querySelector(`[data-action="${action}"]`).click(); }
    });
  }
  return card;
}

// Điều kiện hoàn tác gateway đã xét: ✓ đạt, ✕ chưa đạt. Trên thẻ duyệt cho biết thiếu gì; trên lệnh đã tự chạy cho biết vì sao được chạy.
function undoChecks(u, title) {
  const list = u.checks && u.checks.length ? u.checks : [{ ok: u.reversible, text: (u.reversible ? "Hoàn tác được: " : "Không hoàn tác được: ") + (u.summary || "") }];
  return h("div", { class: "undo-checks" }, h("div", { class: "small muted" }, title),
    list.map((c) => h("div", { class: "undo-check " + (c.ok ? "ok-text" : "warn-text") }, h("span", { "aria-hidden": "true" }, c.ok ? "✓ " : "✕ "),
      h("span", { class: "sr-only" }, c.ok ? "đạt: " : "chưa đạt: "), c.text)));
}

const CHANGE_FINAL = ["ok", "reverted", "purged", "applied", "check_failed", "conflict", "revert_failed"];
// Một hội thoại có nhiều dòng lệnh ghi: các dòng vẽ cùng lúc dùng chung một lần đọc /api/jobs.
let changeJobs = null;
const changeJobsNow = () => {
  if (!changeJobs || Date.now() - changeJobs.at > 5000) changeJobs = { at: Date.now(), p: jobsOf("changes") };
  return changeJobs.p;
};

// Trạng thái của lệnh ghi sau khi chạy: đang kiểm tra, ổn, tự hoàn tác, kiểm tra không đạt; kèm nút Hoàn tác.
// Đang kiểm tra thì hỏi lại máy chủ mỗi 10 giây cho tới khi có kết quả hoặc dòng bị gỡ khỏi trang.
function changeLine(change) {
  const box = h("div", { class: "change-line", role: "status" });
  let cur = change;
  const paint = () => {
    const c = cur;
    if (c.status === "loading") { setKids(box, h("span", { class: "small muted" }, "…")); return; }
    const check = c.check || {};
    const left = c.verify_until ? Math.max(0, Math.ceil((new Date(c.verify_until) - Date.now()) / 60000)) : 0;
    const by = c.reverted_by === "auto" ? "tự hoàn tác" : c.reverted_by === "drill" ? "hoàn tác khi diễn tập" : `${c.reverted_by || ""} hoàn tác`;
    const [mark, cls, text] = {
      verifying: ["◷", "muted", `Đang kiểm tra · còn ${left} phút` + (check.detail ? ` · ${check.detail}` : "")],
      ok: ["✓", "ok-text", "Ổn sau kiểm tra" + (check.detail ? ` · ${check.detail}` : "")],
      applied: c.soft ? ["🗑", "muted", `Xoá mềm · khôi phục được tới ${c.purge_at ? fmtTime(c.purge_at) : "—"}`] : ["✓", "muted", "Đã chạy · " + (c.summary ? "hoàn tác: " + c.summary : "")],
      check_failed: ["⚠", "warn-text", "Kiểm tra không đạt: " + (check.detail || "")],
      conflict: ["⚠", "warn-text", "Kiểm tra không đạt, không tự hoàn tác được: đối tượng đã bị sửa sau lệnh"],
      reverted: ["↺", "muted", `Đã ${by}${c.reverted_at ? " lúc " + fmtTime(c.reverted_at) : ""}` + (c.reverted_by === "auto" && check.detail ? ` vì ${check.detail}` : "")],
      revert_failed: ["✕", "warn-text", "Hoàn tác lỗi: " + ((c.revert && c.revert.error) || "")],
      purged: ["✕", "muted", "Đã xoá thật"],
    }[c.status] || ["•", "muted", c.status];
    const label = c.status === "conflict" ? "Hoàn tác…" : c.soft ? "Khôi phục" : "Hoàn tác";
    const btn = c.can_undo && c.can_revert !== false && jobBtn(job, { label, busyLabel: c.soft ? "Đang khôi phục…" : "Đang hoàn tác…",
      run: () => runUndo(c, box, (next) => { job = null; cur = next; paint(); }), onDone: () => { job = null; poll(); } });
    setKids(box, h("span", { class: cls }, h("span", { "aria-hidden": "true" }, mark + " "), text), btn || null);
  };
  let job = null;  // lượt hoàn tác đang chạy hoặc vừa lỗi, kể cả bấm trước khi tải lại trang
  if (change.can_undo) changeJobsNow().then((jobs) => { job = jobs[`undo:${change.id}`] || null; if (job) paint(); });
  const poll = async () => {
    try {
      const [fresh] = await api("GET", `/api/changes?ids=${encodeURIComponent(change.id)}`);
      if (fresh) { cur = fresh; paint(); }
    } catch { /* lỗi mạng tạm thời: thử lại ở nhịp sau */ }
    if (box.isConnected && !CHANGE_FINAL.includes(cur.status)) setTimeout(poll, 10000);
  };
  paint();
  if (!CHANGE_FINAL.includes(cur.status)) setTimeout(poll, cur.status === "loading" ? 0 : 10000);
  return box;
}

// Hoàn tác: đối tượng đã bị sửa sau lệnh thì máy chủ trả danh sách khác biệt; người dùng đọc rồi mới chọn đè.
async function runUndo(c, box, done) {
  try {
    done(await api("POST", `/api/changes/${c.id}/undo`, {}));
    toast(c.soft ? "Đã khôi phục" : "Đã hoàn tác");
  } catch (err) {
    if (err.status !== 409 || !err.data || !err.data.drift) throw err;
    const confirm = h("div", { class: "undo-drift warn-text" }, h("div", {}, "Đối tượng đã bị sửa sau lệnh; hoàn tác lúc này sẽ đè lên các thay đổi:"),
      h("ul", {}, err.data.drift.map((d) => h("li", {}, d))),
      h("div", { class: "row" },
        h("button", { type: "button", class: "danger small-btn", onclick: guard(async () => { done(await api("POST", `/api/changes/${c.id}/undo`, { force: true })); toast("Đã hoàn tác"); }) }, "Vẫn hoàn tác"),
        h("button", { type: "button", class: "ghost small-btn", onclick: () => confirm.remove() }, "Thôi")));
    box.after(confirm);
  }
}

function approvalActions(a, onDecide) {
  const note = h("input", { placeholder: "Ghi chú cho agent (tùy chọn)", "aria-label": "Ghi chú cho agent", class: "wide" });
  const act = (action) => guard(async () => { await onDecide(a, action, note.value.trim() || null); });
  note.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); act(note.value.trim() ? "deny" : "approve_once")(e); } });
  return h("div", { class: "approval-actions" },
    h("div", { class: "choice-list", role: "group", "aria-label": "Quyết định" },
      h("button", { type: "button", class: "choice primary", "data-action": "approve_once", onclick: act("approve_once") }, h("kbd", {}, "1"), "Chạy"),
      h("button", { type: "button", class: "choice", "data-action": "approve_session", onclick: act("approve_session") }, h("kbd", {}, "2"), `Chạy, và cho phép ${grantScope(a)} trong 1 giờ`),
      h("button", { type: "button", class: "choice deny", "data-action": "deny", onclick: act("deny") }, h("kbd", {}, "3"), "Từ chối")),
    note,
    h("div", { class: "small muted" }, "Từ chối kèm ghi chú thì agent làm tiếp theo ghi chú; không ghi chú thì agent dừng. Chạy kèm ghi chú thì agent nhận ghi chú sau kết quả."));
}

// Dựng các mục của một lượt theo thứ tự: chữ, dòng lệnh, thẻ duyệt, câu hỏi. Lượt cũ chưa lưu thứ tự thì lệnh trước, chữ sau.
function timelineItems(m) {
  if (m.timeline) return m.timeline;
  return [...m.steps.map((_, i) => ({ step: i })), ...(m.output ? [{ text: m.output }] : [])];
}

// Câu hỏi của agent (công cụ ask_user), như AskUserQuestion của Claude Code: chọn nút, hoặc tự gõ ở ô "Khác".
function questionCard(step, answerable, onAnswer) {
  const picks = step.questions.map(() => new Set());
  const others = step.questions.map(() => h("input", { placeholder: "Khác…", "aria-label": "Câu trả lời khác", class: "wide", disabled: !answerable }));
  const submit = guard(async () => {
    const lines = step.questions.map((q, i) => {
      const chosen = [...picks[i], ...(others[i].value.trim() ? [others[i].value.trim()] : [])];
      return chosen.length ? `${q.header || q.question}: ${chosen.join(", ")}` : null;
    });
    if (lines.some((l) => l === null)) throw new Error("Hãy trả lời mọi câu hỏi");
    await onAnswer(lines.length === 1 ? lines[0] : lines.map((l) => "- " + l).join("\n"));
  });
  const single = step.questions.length === 1 && !step.questions[0].multi_select;
  const blocks = step.questions.map((q, i) => h("div", { class: "question-item" },
    h("div", {}, q.header && h("span", { class: "badge info" }, q.header), " ", h("strong", {}, q.question)),
    h("div", { class: "question-options" }, q.options.map((o) => {
      const btn = h("button", { type: "button", class: "option", disabled: !answerable, title: o.description || "" },
        h("span", {}, o.label), o.description && h("span", { class: "small muted" }, o.description));
      btn.addEventListener("click", (e) => {
        if (single) { picks[i] = new Set([o.label]); others[i].value = ""; submit(e); return; }
        if (!q.multi_select) { picks[i].clear(); btn.parentElement.querySelectorAll(".option").forEach((b) => b.classList.remove("on")); }
        if (picks[i].has(o.label)) { picks[i].delete(o.label); btn.classList.remove("on"); } else { picks[i].add(o.label); btn.classList.add("on"); }
      });
      return btn;
    })),
    others[i]));
  if (single) others[0].addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.isComposing && others[0].value.trim()) { e.preventDefault(); submit(e); } });
  return h("div", { class: "question" + (answerable ? "" : " done") }, h("div", { class: "approval-head" }, h("strong", {}, answerable ? "Agent hỏi bạn" : "Agent đã hỏi")),
    blocks, answerable && !single && h("div", { class: "row" }, h("button", { type: "button", onclick: submit }, "Gửi trả lời")),
    answerable && single && h("div", { class: "small muted" }, "Bấm một lựa chọn, hoặc gõ câu trả lời khác rồi Enter."));
}

function chatView(main) {
  const thread = h("div", { class: "thread" });
  const scroller = h("div", { class: "scroller" }, thread);
  const toBottom = iconButton("down_arrow", "Xuống cuối", () => { scroller.scrollTop = scroller.scrollHeight; }, "to-bottom hidden");
  let stick = true;
  const scroll = () => { if (stick) scroller.scrollTop = scroller.scrollHeight; };
  scroller.addEventListener("scroll", () => {
    stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    toBottom.classList.toggle("hidden", stick);
  });

  const blocks = new Map();
  let lastTask = null;
  let activeRun = null;
  // Tin gửi lúc agent đang chạy mà agent không còn đọc được (lượt vừa kết thúc): gửi thành lượt kế tiếp. {text, el}
  const afterRun = [];
  let queued = false;  // điều tra sự cố đã nhận nhưng chưa chạy: chưa cho gửi để lượt chat không chen vào trước

  // Thanh "đang chờ bạn" trên ô nhập khi thẻ duyệt hay câu hỏi đã trôi khỏi màn hình; bấm để tới đó.
  const pendingBar = h("button", { type: "button", class: "pending-bar hidden" });
  const waitingEls = () => [...thread.querySelectorAll(".approval.waiting, .question:not(.done)")];
  pendingBar.addEventListener("click", () => {
    const el = waitingEls()[0];
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.focus({ preventScroll: true });
  });
  function updatePending() {
    const els = waitingEls();
    const box = scroller.getBoundingClientRect();
    const visible = els.some((el) => { const r = el.getBoundingClientRect(); return r.bottom > box.top && r.top < box.bottom; });
    const approvals = els.filter((el) => el.classList.contains("approval")).length;
    pendingBar.textContent = approvals ? `${approvals} lệnh đang chờ bạn duyệt · Xem` : "Agent đang hỏi bạn · Xem";
    pendingBar.classList.toggle("hidden", !els.length || visible);
  }
  scroller.addEventListener("scroll", () => requestAnimationFrame(updatePending));

  // Focus view: ẩn các dòng lệnh, chỉ giữ chữ, thẻ chờ duyệt và câu hỏi. Mỗi người tự chọn, được nhớ lại.
  const focusBtn = h("button", { type: "button", class: "ghost small-btn focus-toggle" });
  const syncFocus = (on) => {
    thread.classList.toggle("focus", on);
    focusBtn.textContent = on ? "Hiện hành động" : "Ẩn hành động";
    focusBtn.setAttribute("aria-pressed", String(on));
    focusBtn.title = on ? "Hiện các lệnh agent đã chạy" : "Chỉ giữ câu trả lời, thẻ duyệt, hành động khắc phục và câu hỏi";
  };
  syncFocus(Boolean(store.get("focusView", false)));
  focusBtn.addEventListener("click", () => { const on = !thread.classList.contains("focus"); store.set("focusView", on || null); syncFocus(on); });

  const input = h("textarea", { rows: 1, placeholder: "Nhắn cho agent…", "aria-label": "Tin nhắn" });
  const env = h("select", { "aria-label": "Môi trường", title: "Môi trường chạy công cụ" }, ["dev", "staging", "prod"].map((e) => h("option", { selected: e === state.env }, e)));
  env.addEventListener("change", () => { state.env = env.value; store.set("env", env.value); });
  // Nội dung đang gõ dở được giữ qua refresh, riêng cho từng hội thoại.
  const draftKey = () => "draft:" + (state.conversationId || "new");
  input.value = store.get(draftKey(), "");
  input.addEventListener("input", () => store.set(draftKey(), input.value));
  const sendBtn = h("button", { type: "button", class: "send", "aria-label": "Gửi", title: "Gửi", disabled: true }, icon("send"));

  const resize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; };
  // Đang chạy: ô nhập trống thì nút là Dừng, có chữ thì là Gửi thêm (agent đọc ngay sau lệnh đang chạy, như Claude Code).
  const syncSend = () => {
    const stopping = activeRun && !input.value.trim();
    const label = stopping ? "Dừng" : activeRun ? "Gửi thêm: agent đọc ngay sau lệnh đang chạy" : "Gửi";
    setKids(sendBtn, icon(stopping ? "stop" : "send"));
    sendBtn.setAttribute("aria-label", label);
    sendBtn.title = label;
    sendBtn.disabled = !activeRun && (queued || !input.value.trim());
  };
  input.addEventListener("input", () => { resize(); syncSend(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!queued) submit(); }
  });
  sendBtn.addEventListener("click", () => { if (activeRun && !input.value.trim()) stop(); else submit(); });

  function userBubble(text, fresh) {
    return h("div", { class: "msg user" + (fresh ? " fresh" : "") }, h("div", { class: "bubble" }, text));
  }

  function continuationNote() {
    return h("div", { class: "continuation-note small" }, "↻ Agent làm tiếp theo quyết định của bạn");
  }

  function actionsRow(m) {
    const row = h("div", { class: "actions" });
    const feedback = (outcome, final) => guard(async () => {
      await api("POST", `/api/episodes/${m.id}/feedback`, { outcome, final_text: final === undefined ? null : final });
      m.outcome = outcome; m.outcome_src = "user_feedback";
      toast("Đã ghi phản hồi, agent sẽ học từ đánh giá này");
      replaceBlock(m);
    });
    const copyBtn = iconButton("copy", "Sao chép", (e) => copyText(m.output || "", e.currentTarget));
    const up = iconButton("up", "Câu trả lời tốt", feedback("success"), m.outcome_src === "user_feedback" && m.outcome === "success" ? "on" : "");
    const down = iconButton("down", "Câu trả lời sai", feedback("failure"), m.outcome_src === "user_feedback" && m.outcome === "failure" ? "on" : "");
    const edit = iconButton("edit", "Sửa câu trả lời để agent học", () => {
      const area = h("textarea", { class: "edit-area", value: m.output || "", "aria-label": "Bản đã sửa" });
      const box = h("div", { class: "edit-box" }, area, h("div", { class: "row" },
        h("button", { type: "button", class: "ghost", onclick: () => box.remove() }, "Hủy"),
        h("button", { type: "button", onclick: (e) => { m.output = area.value; m.edited = true; feedback("partial", area.value)(e); } }, "Lưu bản sửa")));
      row.after(box);
      area.focus();
    });
    const savable = m.steps.some((s) => s.status === "ok" && !s.learned);
    const saveTool = savable && iconButton("save", "Lưu thành công cụ", () => {
      if (row.nextElementSibling && row.nextElementSibling.classList.contains("save-box")) return;
      const name = h("input", { placeholder: "Tên công cụ, ví dụ: kiem tra don loi", "aria-label": "Tên công cụ", class: "wide" });
      const scope = h("select", { "aria-label": "Phạm vi" }, h("option", { value: "user" }, "Chỉ mình tôi"), h("option", { value: "tenant" }, "Cả nhóm (cần admin duyệt)"));
      const note = h("span");  // lượt lưu đang chạy hoặc vừa lỗi, kể cả bấm trước khi tải lại trang
      const box = h("div", { class: "edit-box save-box" },
        h("div", { class: "small" }, "Lưu các bước công cụ đã chạy thành công ở lượt này thành một công cụ dùng lại được. Giá trị bạn đã gõ trong yêu cầu sẽ thành tham số."),
        h("div", { class: "row" }, name, scope,
          h("button", { type: "button", class: "ghost", onclick: () => box.remove() }, "Hủy"),
          jobBtn(null, { label: "Lưu", busyLabel: "Đang lưu…", cls: "", run: async () => {
            const tool = await api("POST", "/api/learned-tools/from-episode", { episode_id: m.id, name: name.value || null, scope: scope.value });
            box.remove();
            toast(tool.status === "active" ? `Đã tạo ${tool.name}. Nhắc tên này trong chat để agent dùng.` : `Đã gửi ${tool.name} cho admin duyệt.`);
          } }), note));
      row.after(box);
      name.focus();
      jobsOf("learned-tools").then((jobs) => setKids(note, jobNote(jobs[`from-episode:${m.id}`], () => box.remove(), note)));
    });
    const remember = m.output && iconButton("book", "Lưu vào tri thức", () => {
      if (row.nextElementSibling && row.nextElementSibling.classList.contains("ku-box")) return;
      const text = (m.output || "").trim();
      const kind = h("select", { "aria-label": "Loại" }, [["fact", "Sự thật"], ["procedure", "Thủ tục"], ["lesson", "Bài học"], ["insight", "Chiến lược"], ["rule", "Quy ước"]]
        .map(([v, l]) => h("option", { value: v }, l)));
      const title = h("input", { class: "wide", value: text.split("\n")[0].replace(/^[#*\s-]+/, "").slice(0, 120), "aria-label": "Tiêu đề" });
      const content = h("textarea", { class: "edit-area", value: text.slice(0, 2000), "aria-label": "Nội dung" });
      const when = h("input", { class: "wide", placeholder: "Áp dụng khi… (không bắt buộc)", "aria-label": "Điều kiện áp dụng" });
      const scope = h("select", { "aria-label": "Phạm vi" }, h("option", { value: "tenant" }, "Cả nhóm (qua cổng duyệt)"), h("option", { value: "user" }, "Chỉ mình tôi"));
      const box = h("div", { class: "edit-box ku-box" },
        h("div", { class: "small" }, "Giữ đúng một ý dùng lại được; bỏ chi tiết riêng của lần này. Agent sẽ thấy tri thức này ở các lượt sau."),
        h("div", { class: "row" }, kind, scope), title, content, when,
        h("div", { class: "row" }, h("button", { type: "button", class: "ghost", onclick: () => box.remove() }, "Hủy"),
          h("button", { type: "button", onclick: guard(async () => {
            const ku = await api("POST", `/api/episodes/${m.id}/knowledge`, { kind: kind.value, title: title.value, content: content.value, applies_when: when.value || null, scope: scope.value });
            box.remove();
            toast(ku.status === "active" ? "Đã lưu vào ký ức của bạn" : ku.source_trust === "external_untrusted"
              ? "Đã lưu thành ứng viên; câu trả lời dựa trên nội dung ngoài (log, web) nên cần admin duyệt" : "Đã lưu thành ứng viên của nhóm");
          }) }, "Lưu")));
      row.after(box);
      content.focus();
    });
    addKids(row, copyBtn, up, down, edit);
    if (remember) addKids(row, remember);
    if (saveTool) addKids(row, saveTool);
    if (m.task === lastTask) addKids(row, iconButton("retry", "Gửi lại", () => send(m.task)));
    addKids(row, h("span", { class: "meta" }, `${m.env} · ${m.ku_used} KU${m.tokens ? ` · ${m.tokens} token quy đổi` : ""}`));
    return row;
  }

  function assistantBlock(m) {
    const byId = new Map(m.approvals.map((a) => [a.id, a]));
    const latest = m.id === latestId();
    const flow = h("div", { class: "flow" });
    // Lệnh của agent con được ghi trước dòng giao việc (giao việc xong mới ghi); gom chúng xuống ngay dưới dòng đó. Lệnh chờ duyệt vẫn ở chỗ cũ.
    const delegated = new Set(m.steps.filter((s) => s.tool === "agent.delegate" && s.sub_id).map((s) => s.sub_id));
    const grouped = (s) => s.sub && delegated.has(s.sub) && !(s.approval_id && byId.has(s.approval_id));
    for (const item of timelineItems(m)) {
      if (item.text !== undefined) { addKids(flow, h("div", { class: "prose" }, renderMarkdown(item.text))); continue; }
      if (item.user !== undefined) { addKids(flow, h("div", { class: "msg user queued" }, h("div", { class: "bubble" }, item.user))); continue; }
      const st = m.steps[item.step];
      if (!st || grouped(st)) continue;
      if (st.tool === "agent.delegate" && st.sub_id) { addKids(flow, toolRow(st), m.steps.filter((s) => s.sub === st.sub_id && grouped(s)).map(toolRow)); continue; }
      if (st.tool === "ask_user" && st.status === "asked" && st.questions) addKids(flow, questionCard(st, latest, (answer) => send(answer)));
      else if (st.approval_id && byId.has(st.approval_id)) { addKids(flow, approvalCard(byId.get(st.approval_id), decide)); byId.delete(st.approval_id); }
      else addKids(flow, toolRow(st));
    }
    // Hành động sự cố đề xuất sau khi điều tra xong đi thẳng qua gateway, không nằm trong trace của lượt.
    const fixes = m.incident_actions || [];
    if (fixes.length) {
      // Câu trả lời ở trên viết trong lượt chỉ đọc ("chưa chạy lệnh ghi nào"); lệnh khắc phục đến sau, phải nói rõ để không bị đọc là chưa có đề xuất.
      addKids(flow, h("div", { class: "small muted incident-fix-head" }, "Hành động khắc phục của sự cố"),
        h("div", { class: "small muted incident-fix-lead" }, `Sau khi điều tra (chỉ đọc), aiops đưa ${fixes.length} lệnh khắc phục qua gateway:`));
      for (const a of fixes) {
        const label = a.why || a.summary;
        if (a.approval_id && byId.has(a.approval_id)) {
          const req = byId.get(a.approval_id);
          byId.delete(a.approval_id);
          addKids(flow, h("div", { class: "incident-fix" }, approvalCard({ ...req, why: req.why || label }, decide)));
          continue;
        }
        addKids(flow, h("div", { class: "incident-fix" }, label && h("div", { class: "small" }, label), toolRow({ ...a, env: m.env }),
          a.status === "ok" && h("div", { class: "small muted" }, (a.auto ? "Tự chạy" : "Đã chạy")
            + (a.verified === true ? " · đã hết cảnh báo" : a.verified === false ? " · cảnh báo vẫn còn" : ""))));
      }
    }
    for (const a of byId.values()) addKids(flow, approvalCard(a, decide));
    if (m.error) addKids(flow, h("div", { class: "msg-error" }, m.output));
    const open = m.approvals.filter((a) => a.status === "pending" && a.can_decide);
    const bulk = open.length > 1 && h("div", { class: "row approval-bulk" }, h("span", { class: "small" }, `${open.length} lệnh đang chờ duyệt`),
      h("button", { type: "button", onclick: guard(() => decideAll(m, open, "approve_once")) }, "Chạy tất cả"),
      h("button", { type: "button", class: "ghost", onclick: guard(() => decideAll(m, open, "approve_session")), title: "Mỗi lệnh: cùng công cụ, cùng namespace, trong 1 giờ" }, "Chạy tất cả, cho phép từng lệnh trong 1 giờ"));
    const canContinue = !activeRun && !m.continued && continuable(m);
    const resume = canContinue && h("div", { class: "row resume" }, h("button", { type: "button", class: "ghost", onclick: () => send(null, m.id) }, "Để agent làm tiếp"));
    return h("div", { class: "msg assistant", "data-ep": m.id }, bulk, flow, resume, m.cancelled && h("div", { class: "small muted" }, "Đã dừng"), actionsRow(m));
  }

  async function decide(a, action, reason) {
    const r = await api("POST", `/api/approvals/${a.id}/decide`, { action, reason });
    const failed = r.result && r.result.status === "error";
    const extra = r.also_approved && r.also_approved.length ? ` (và ${r.also_approved.length} lệnh cùng loại)` : "";
    toast(r.status === "pending" ? "Đã ghi nhận, cần thêm người duyệt" : r.status === "denied" ? (r.deny_reason ? "Đã từ chối, agent làm tiếp theo ghi chú" : "Đã từ chối, agent dừng")
      : failed ? "Lệnh đã chạy nhưng báo lỗi" : "Đã chạy lệnh" + extra, failed);
    await refreshAndContinue();
    refreshConversations();
  }

  async function decideAll(m, list, action) {
    let done = 0;
    for (const a of list) {
      try { const r = await api("POST", `/api/approvals/${a.id}/decide`, { action }); done += 1 + (r.also_approved ? r.also_approved.length : 0); }
      catch (e) { if (!/đã ở trạng thái/.test(e.message)) { toast(e.message, true); break; } }  // lệnh đã được grant phủ thì bỏ qua
    }
    toast(`Đã chạy ${done} lệnh`);
    await refreshAndContinue();
  }

  // Khi lượt mới nhất không còn lệnh chờ và đã có lệnh chạy, agent tự viết tiếp với kết quả đó. Server từ chối nếu đã chạy tiếp rồi.
  async function refreshBlocks() {
    const data = await api("GET", `/api/conversations/${state.conversationId}`);
    for (const m of data.messages) if (blocks.has(m.id)) replaceBlock(m);
    return data;
  }

  async function refreshAndContinue() {
    maybeContinue((await refreshBlocks()).messages);
  }

  // Như Claude Code: lệnh đã chạy, hoặc bị từ chối kèm ghi chú, thì agent làm tiếp; từ chối không ghi chú thì dừng.
  function continuable(m) {
    if (!m.approvals.length || m.approvals.some((a) => a.status === "pending")) return false;
    return m.approvals.some((a) => a.status === "approved" || (a.status === "denied" && a.deny_reason));
  }

  function latestId() {
    let last = null;
    for (const id of blocks.keys()) last = id;
    return last;
  }

  function maybeContinue(messages) {
    const last = messages[messages.length - 1];
    if (!last || activeRun || last.continued || !continuable(last)) return;
    send(null, last.id);
  }

  function replaceBlock(m) {
    const old = blocks.get(m.id);
    const fresh = assistantBlock(m);
    blocks.set(m.id, fresh);
    if (old) old.replaceWith(fresh);
    requestAnimationFrame(updatePending);
    return fresh;
  }

  function addMessage(m) {
    if (!m.continuation) lastTask = m.task;
    addKids(thread, m.continuation ? continuationNote() : userBubble(m.task));
    blocks.set(m.id, null);  // giữ chỗ để assistantBlock biết đây là tin nhắn mới nhất
    const el = assistantBlock(m);
    blocks.set(m.id, el);
    addKids(thread, el);
    requestAnimationFrame(updatePending);
  }

  function emptyState() {
    const fill = (text) => () => { input.value = text; resize(); syncSend(); input.focus(); };
    const ideas = ["Xem pod trong namespace default", "Tìm log lỗi trong 1 giờ qua", "Xem các cảnh báo đang kích hoạt", "Đọc trang https://example.com"];
    return h("div", { class: "empty" }, h("h2", {}, `Xin chào, ${state.me.username}`), h("p", { class: "muted" }, "Hôm nay agent có thể giúp gì cho bạn?"),
      h("div", { class: "ideas" }, ideas.map((t) => h("button", { type: "button", class: "idea", onclick: fill(t) }, t))));
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    store.set(draftKey(), null);
    resize();
    syncSend();
    if (activeRun) return queueMessage(text);
    await send(text);
  }

  async function queueMessage(text) {
    const el = activeRun.turn ? activeRun.turn.queuedBubble(text) : null;
    try {
      if (!state.conversationId) throw new Error("chưa có hội thoại");
      await api("POST", `/api/conversations/${state.conversationId}/queue`, { message: text });
    } catch {
      afterRun.push({ text, el });
      if (el) setKids(el.querySelector(".queued-note"), "sẽ gửi khi agent trả lời xong");
    }
  }

  function flushAfterRun() {
    if (activeRun || !afterRun.length) return;
    const items = afterRun.splice(0);
    items.forEach((i) => i.el && i.el.remove());
    send(items.map((i) => i.text).join("\n\n"));
  }

  // Dừng theo hội thoại: dùng được cả cho lượt đang xem tiếp sau khi tải lại, và lượt của người khác trong hội thoại sự cố.
  async function stop() {
    if (!activeRun || !state.conversationId) return;
    sendBtn.disabled = true;
    try { await api("POST", `/api/conversations/${state.conversationId}/stop`); } catch (e) { toast(e.message, true); sendBtn.disabled = false; }
  }

  // Dựng khung của một lượt (câu hỏi + vùng trả lời đang chạy) và trả về hàm xử lý sự kiện stream; dùng cho lượt mới gửi
  // và cho lượt đang chạy khi trang vừa tải lại (xem tiếp qua /live).
  function beginTurn(text, continueEpisode, continuation) {
    const empty = thread.querySelector(".empty");
    if (empty) empty.remove();
    // Câu hỏi cũ chỉ trả lời được trước khi có lượt mới.
    thread.querySelectorAll(".question:not(.done)").forEach((q) => { q.classList.add("done"); q.querySelectorAll("button, input").forEach((b) => { b.disabled = true; }); });
    if (continueEpisode || continuation) {
      if (continueEpisode) blocks.get(continueEpisode)?.querySelector(".resume")?.remove();
      addKids(thread, continuationNote());
    }
    else { lastTask = text; addKids(thread, userBubble(text, true)); }
    // Lượt đang chạy dựng dần theo sự kiện: chữ mới nối vào đoạn cuối, mỗi lệnh thành một dòng ở đúng chỗ; dòng trạng thái ở cuối đếm thời gian.
    const flow = h("div", { class: "flow" });
    const status = h("div", { class: "live-status small muted", role: "status" });
    const liveEl = h("div", { class: "msg assistant live" }, flow, status);
    addKids(thread, liveEl);
    stick = true;
    scroll();

    let cur = null;  // đoạn chữ đang được viết
    const waiting = [];  // bong bóng tin gửi thêm chưa được agent đọc
    const queuedBubble = (text, read) => {
      const el = h("div", { class: "msg user queued fresh" }, h("div", { class: "bubble" }, text), !read && h("div", { class: "small muted queued-note" }, "chờ agent đọc…"));
      if (!read) waiting.push({ text, el });
      addKids(flow, el);
      cur = null;
      scroll();
      return el;
    };
    const takeWaiting = (text) => { const i = waiting.findIndex((w) => w.text === text); return i === -1 ? null : waiting.splice(i, 1)[0].el; };
    const running = [];  // {tool, args, row, at}
    let lastEvent = Date.now();
    let frame = 0;
    const paint = () => { frame = 0; if (cur) setKids(cur.el, renderMarkdown(cur.text)); scroll(); };
    const queuePaint = () => { if (!frame) frame = requestAnimationFrame(paint); };
    const secs = (t) => `${Math.max(0, Math.round((Date.now() - t) / 1000))}s`;
    const tickStatus = () => {
      for (const r of running) { const el = r.row.querySelector(".elapsed"); if (el) el.textContent = secs(r.at); }
      setKids(status, running.length ? busy(`Đang chạy ${running.map((r) => shortTool(r.tool)).join(", ")}… ${secs(Math.min(...running.map((r) => r.at)))}`)
        : busy(`${cur ? "Đang viết" : "Đang suy nghĩ"}… ${secs(lastEvent)}`));
    };
    tickStatus();
    const ticker = setInterval(tickStatus, 1000);
    const stopTicker = () => { clearInterval(ticker); setKids(status); };
    const fail = (message) => { stopTicker(); addKids(flow, h("div", { class: "msg-error" }, message)); };
    const sameCall = (r, step) => r.tool === step.tool && JSON.stringify(r.args || {}) === JSON.stringify(step.args || {});

    const handle = (ev) => {
      lastEvent = Date.now();
      if (ev.type === "start") {
        activeRun.id = ev.run_id;
        const isNew = state.conversationId !== ev.conversation_id;
        adoptConversation(ev.conversation_id);
        if (isNew) refreshConversations();
      } else if (ev.type === "delta") {
        if (!cur) { cur = { text: "", el: h("div", { class: "prose" }) }; addKids(flow, cur.el); }
        cur.text += ev.text;
        queuePaint();
      } else if (ev.type === "tool_start") {
        if (frame) { cancelAnimationFrame(frame); paint(); }
        cur = null;
        const row = toolRow({ tool: ev.tool, args: ev.args, status: "running", sub: ev.sub, background: ev.background });
        running.push({ tool: ev.tool, args: ev.args, row, at: Date.now() });
        addKids(flow, row);
        tickStatus();
        scroll();
      } else if (ev.type === "tool_end") {
        const step = ev.step;
        let i = running.findIndex((r) => sameCall(r, step));
        if (i === -1) i = running.findIndex((r) => r.tool === step.tool);
        const slot = i === -1 ? null : running.splice(i, 1)[0].row;
        const view = step.tool === "ask_user" && step.status === "asked" && step.questions ? questionCard(step, false, () => {})
          : step.status === "pending" ? approvalCard({ ...step, id: step.approval_id, status: "pending", decided_by: [], required_approvals: step.required_approvals || 1,
            reasons: step.reasons || [], can_decide: false }, null, true)
          : toolRow(step);
        if (slot) slot.replaceWith(view); else addKids(flow, view);
        tickStatus();
        scroll();
      } else if (ev.type === "done") {
        if (frame) cancelAnimationFrame(frame);
        stopTicker();
        blocks.set(ev.message.id, liveEl);
        replaceBlock(ev.message);
        if (continueEpisode) refreshBlocks().catch(() => {});  // lượt gốc giờ đã "continued": bỏ nút Tiếp tục
        scroll();
        refreshConversations();
        updatePending();
        if (ev.message.approvals.some((a) => a.status === "pending")) pollApprovals();
      } else if (ev.type === "user_message") {
        // Agent vừa đọc tin gửi thêm: dời bong bóng xuống đúng chỗ trong luồng.
        const el = takeWaiting(ev.text);
        if (el) el.remove();
        queuedBubble(ev.text, true);
      } else if (ev.type === "leftover") {
        for (const text of ev.texts) afterRun.push({ text, el: takeWaiting(text) });
      } else if (ev.type === "stopping") {
        // Lệnh ghi đã gửi đi thì ngắt giữa chừng không hoàn tác được: đợi nó xong rồi mới dừng.
        addKids(flow, h("div", { class: "small warn-text" }, `Đang đợi ${ev.waiting_for.join(", ")} chạy xong rồi dừng…`));
        scroll();
      } else if (ev.type === "error") fail(ev.message);
    };
    return { handle, fail, liveEl, queuedBubble, stopPaint: () => { if (frame) cancelAnimationFrame(frame); stopTicker(); } };
  }

  async function readSSE(res, handle) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        if (chunk.startsWith("data: ")) handle(JSON.parse(chunk.slice(6)));
      }
    }
  }

  async function send(text, continueEpisode) {
    if (activeRun) return;
    const turn = beginTurn(text, continueEpisode);
    const { handle, fail, stopPaint } = turn;
    activeRun = { id: null, turn };
    syncSend();
    try {
      const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json", "X-Tenant-Id": state.tenant.id }, credentials: "same-origin", body: JSON.stringify({ ...(continueEpisode ? { continue_episode: continueEpisode } : { message: text }), conversation_id: state.conversationId, env: env.value, page: pageContext() }) });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        fail(typeof (data && data.detail) === "string" ? data.detail : `Lỗi ${res.status}`);
      } else await readSSE(res, handle);
    } catch (e) {
      fail("Mất kết nối. Câu trả lời vẫn có thể được lưu, hãy mở lại hội thoại sau ít phút.");
    } finally {
      stopPaint();
      activeRun = null;
      syncSend();
      input.focus();
    }
    flushAfterRun();
  }

  // Trang được tải lại khi agent đang trả lời: phát lại câu hỏi và phần đã sinh của lượt đó, rồi stream tiếp tới khi xong.
  async function attachLive(known) {
    const conv = state.conversationId;
    let turn = null;
    let episodeId = null;
    activeRun = { id: null };  // không gửi lượt mới chồng lên; nút Dừng dừng theo hội thoại nên vẫn dùng được
    syncSend();
    try {
      const res = await fetch(`/api/conversations/${conv}/live`, { headers: { "X-Tenant-Id": state.tenant.id }, credentials: "same-origin" });
      if (res.ok) {
        await readSSE(res, (ev) => {
          if (state.conversationId !== conv) return;
          if (ev.type === "live_start") { turn = beginTurn(ev.task, null, ev.continuation); if (activeRun) activeRun.turn = turn; }
          else if (ev.type === "end") episodeId = ev.episode_id;
          else if (turn) turn.handle(ev);
        });
      }
    } catch { /* mất kết nối: vẫn tải lại hội thoại bên dưới */ }
    activeRun = null;
    syncSend();
    if (state.conversationId !== conv) return;
    if (turn) turn.stopPaint();
    const data = await api("GET", `/api/conversations/${conv}`);
    for (const m of data.messages.slice(known)) {
      if (turn && m.id === episodeId) { blocks.set(m.id, turn.liveEl); replaceBlock(m); }
      else if (!blocks.has(m.id)) addMessage(m);
    }
    if (data.messages.some((m) => m.approvals.some((a) => a.status === "pending"))) pollApprovals();
    if (data.running) return attachLive(data.messages.length);  // lượt kế tiếp đã bắt đầu (vd. chạy tiếp sau khi duyệt)
    scroll();
    flushAfterRun();
  }

  // Điều tra sự cố chưa chạy (chờ lượt điều tra khác xong): thẻ chờ có đồng hồ, hỏi lại tới khi lượt bắt đầu rồi stream như thường.
  async function waitQueued(kind) {
    const conv = state.conversationId;
    const since = Date.now();
    const text = (k) => (k === "slot" ? "Đang chờ tới lượt điều tra: các cuộc điều tra khác đang chạy, agent sẽ tự bắt đầu khi có chỗ trống" : "Agent đang bắt đầu điều tra");
    const status = h("div", { class: "small muted", role: "status" });
    const card = h("div", { class: "msg assistant queued-card" }, status);
    const paint = () => setKids(status, busy(`${text(kind)}… ${Math.round((Date.now() - since) / 1000)}s`));
    paint();
    const ticker = setInterval(paint, 1000);
    addKids(thread, card);
    queued = true;
    syncSend();
    scroll();
    let data = null;
    try {
      while (kind) {
        await new Promise((r) => setTimeout(r, 2000));
        if (!document.contains(thread) || state.conversationId !== conv) return;
        data = await api("GET", `/api/conversations/${conv}`);
        kind = data.queued;
      }
    } catch (e) {
      toast(e.message, true);
    } finally {
      clearInterval(ticker);
      card.remove();
      queued = false;
      syncSend();
    }
    if (!data || state.conversationId !== conv) return;
    for (const m of data.messages) if (!blocks.has(m.id)) addMessage(m);
    if (data.running) await attachLive(data.messages.length);
    else scroll();
  }

  let polling = false;
  function pollApprovals() {
    if (polling) return;
    polling = true;
    const conv = state.conversationId;
    const tick = async () => {
      if (!document.contains(thread) || state.conversationId !== conv) { polling = false; return; }
      let pending = false;
      try {
        const data = await api("GET", `/api/conversations/${conv}`);
        for (const m of data.messages) {
          const el = blocks.get(m.id);
          if (el && el.dataset.approvals !== JSON.stringify(m.approvals.map((a) => [a.status, a.decided_by.length]))) {
            replaceBlock(m).dataset.approvals = JSON.stringify(m.approvals.map((a) => [a.status, a.decided_by.length]));
          }
          pending = pending || m.approvals.some((a) => a.status === "pending");
        }
        if (!pending) maybeContinue(data.messages);
      } catch { pending = true; }
      if (pending) setTimeout(tick, 5000); else polling = false;
    };
    setTimeout(tick, 5000);
  }

  const notice = state.me.llm_provider === "heuristic"
    ? "Chế độ heuristic: chưa có LLM key nên agent chỉ làm theo từ khóa." + (state.me.is_org_admin ? " Nhập key ở mục Cài đặt." : "") + " "
    : "";
  const composer = h("div", { class: "composer-wrap" },
    pendingBar,
    h("div", { class: "composer" }, input, h("div", { class: "composer-bar" }, env, focusBtn, h("span", { class: "spacer" }), sendBtn)),
    h("div", { class: "hint" }, notice + "Agent dùng công cụ thật qua các kết nối bạn được phép. Enter để gửi, Shift+Enter xuống dòng."));
  addKids(main, scroller, toBottom, composer);

  return (async () => {
    if (state.conversationId) {
      try {
        const data = await api("GET", `/api/conversations/${state.conversationId}`);
        if (data.incident_id) {
          // Hội thoại điều tra của sự cố: dùng chung trong tenant, lượt đầu do aiops-alerts chạy.
          addKids(thread, h("div", { class: "card incident-banner" },
            h("div", { class: "incident-banner-text", title: data.title }, h("strong", {}, data.title), " ",
              h("span", { class: "small" }, "Hội thoại điều tra dùng chung của sự cố; hỏi thêm hoặc yêu cầu khắc phục ngay tại đây.")),
            h("button", { type: "button", class: "ghost small-btn", onclick: () => go("incidents", undefined, data.incident_id) }, "Xem sự cố")));
        }
        data.messages.forEach(addMessage);  // tóm tắt sự cố nằm trên trang sự cố, mở cạnh chat, nên không lặp lại ở đây
        if (data.messages.some((m) => m.approvals.some((a) => a.status === "pending"))) pollApprovals();
        if (data.running) attachLive(data.messages.length).catch((e) => toast(e.message, true));
        else if (data.queued) waitQueued(data.queued);
        requestAnimationFrame(scroll);
      } catch (e) {
        adoptConversation(null);
        toast(e.message, true);
      }
    }
    if (!thread.children.length) addKids(thread, emptyState());
    resize();
    syncSend();
    input.focus();
  })();
}
