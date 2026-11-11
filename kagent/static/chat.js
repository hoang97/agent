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
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

const iconButton = (name, label, onclick, extra) => h("button", { type: "button", class: "icon-btn " + (extra || ""), "aria-label": label, title: label, onclick }, icon(name));

async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); } catch { toast("Trình duyệt không cho sao chép", true); return; }
  if (!btn) return;
  btn.replaceChildren(icon("check"));
  setTimeout(() => btn.replaceChildren(icon("copy")), 1500);
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
      frag.append(codeBlock(body.join("\n"), fence[1]));
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)/);
    if (heading) { frag.append(h("h" + Math.min(heading[1].length + 2, 6), {}, inlineMd(heading[2]))); i++; continue; }
    if (/^---+\s*$/.test(line)) { frag.append(h("hr")); i++; continue; }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
      frag.append(h("blockquote", {}, renderMarkdown(quote.join("\n"))));
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      const head = splitRow(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) body.push(splitRow(lines[i++]));
      frag.append(h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inlineMd(c))))), h("tbody", {}, body.map((r) => h("tr", {}, r.map((c) => h("td", {}, inlineMd(c)))))))));
      continue;
    }
    const list = line.match(/^\s*([-*+]|\d+\.)\s+/);
    if (list) {
      const ordered = /\d/.test(list[1]);
      const pattern = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*+]\s+/;
      const items = [];
      while (i < lines.length && pattern.test(lines[i])) items.push(h("li", {}, inlineMd(lines[i++].replace(pattern, ""))));
      frag.append(h(ordered ? "ol" : "ul", {}, items));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    const p = h("p");
    para.forEach((l, n) => { if (n) p.append(h("br")); p.append(...inlineMd(l).map((x) => (x instanceof Node ? x : document.createTextNode(x)))); });
    frag.append(p);
  }
  return frag;
}

function activityView(steps, running) {
  running = running || [];
  if (!steps.length && !running.length) return null;
  const parallel = steps.filter((s) => s.parallel).length;
  const summary = running.length ? `Đang chạy ${running.join(", ")}…`
    : `Đã dùng ${steps.length} công cụ` + (parallel ? ` · ${parallel} chạy song song` : "");
  const rows = steps.map((s) => h("div", { class: "tool-row" },
    h("div", { class: "tool-title" }, h("code", {}, s.tool), s.risk_tier && h("span", { class: "badge" }, s.risk_tier), badge(s.status),
      s.parallel && h("span", { class: "badge info" }, "song song"), s.learned && h("span", { class: "badge info" }, "công cụ học được"),
      s.untrusted && h("span", { class: "badge warn" }, "nội dung ngoài")),
    s.reasons && s.reasons.length > 0 && h("div", { class: "small" }, s.reasons.join("; ")),
    s.steps && h("div", { class: "sub-steps" }, s.steps.map((sub) => h("div", { class: "small" }, "↳ ", h("code", {}, sub.tool), " ", badge(sub.status)))),
    h("pre", {}, JSON.stringify(s.result || s.args, null, 1))));
  return h("details", { class: "activity" + (running ? " running" : "") }, h("summary", {}, icon("chevron"), summary), rows);
}

function approvalCard(a, onDecide) {
  const done = a.status !== "pending";
  const result = a.result && a.result.result;
  const failed = a.result && a.result.status === "error";
  const heading = failed ? "Lệnh đã duyệt nhưng chạy lỗi" : done ? `Lệnh ${LABEL[a.status] || a.status}` : "Lệnh đang chờ duyệt";
  return h("div", { class: "approval " + (failed ? "bad" : CLASS[a.status] || "") },
    h("div", { class: "approval-head" }, h("strong", {}, heading), badge(failed ? "error" : a.status)),
    h("div", {}, h("code", {}, a.tool), ` ở ${a.env} · ${a.risk_tier} · tin cậy ${a.confidence}`),
    a.args && Object.keys(a.args).length > 0 && h("div", { class: "approval-args" },
      Object.entries(a.args).map(([k, v]) => h("div", {}, h("span", { class: "muted" }, k + ": "), h("code", {}, typeof v === "string" ? v : JSON.stringify(v))))),
    a.reasons.length > 0 && h("div", { class: "small" }, a.reasons.join("; ")),
    a.status === "pending" && a.dry_run && h("div", { class: "small" + (a.dry_run.error ? " warn-text" : "") }, (a.dry_run.error ? "Chạy thử báo lỗi, lệnh nhiều khả năng sẽ thất bại: " : "Chạy thử: ") + (a.dry_run.error || JSON.stringify(a.dry_run))),
    a.status === "pending" && a.rollback_hint && h("div", { class: "small" }, "Cách hoàn tác: " + a.rollback_hint),
    a.status === "pending" && (a.can_decide && onDecide ? approvalActions(a, onDecide)
      : h("div", { class: "small" }, `Đã có ${a.decided_by.length}/${a.required_approvals} người duyệt. Đang chờ người có quyền duyệt tại trang Duyệt lệnh.`)),
    a.deny_reason && h("div", { class: "small" }, "Lý do từ chối: " + a.deny_reason),
    result && h("details", { class: "approval-result" }, h("summary", {}, "Kết quả"), h("pre", {}, JSON.stringify(result, null, 1))));
}

function approvalActions(a, onDecide) {
  const reason = h("input", { placeholder: "Lý do (khi từ chối)", "aria-label": "Lý do từ chối", class: "wide" });
  const act = (action) => guard(async () => {
    if (action === "deny" && !reason.value.trim()) throw new Error("Hãy nêu lý do từ chối để agent học");
    await onDecide(a, action, reason.value.trim() || null);
  });
  return h("div", { class: "row" }, h("button", { type: "button", onclick: act("approve_once") }, "Chạy lệnh"),
    h("button", { type: "button", class: "ghost", onclick: act("approve_session"), title: "Cùng công cụ và namespace, trong 1 giờ" }, "Cho phép trong 1 giờ"),
    reason, h("button", { type: "button", class: "danger", onclick: act("deny") }, "Từ chối"));
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

  const input = h("textarea", { rows: 1, placeholder: "Nhắn cho agent…", "aria-label": "Tin nhắn" });
  const env = h("select", { "aria-label": "Môi trường", title: "Môi trường chạy công cụ" }, ["dev", "staging", "prod"].map((e) => h("option", { selected: e === state.env }, e)));
  env.addEventListener("change", () => { state.env = env.value; });
  const sendBtn = h("button", { type: "button", class: "send", "aria-label": "Gửi", title: "Gửi", disabled: true }, icon("send"));

  const resize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 200) + "px"; };
  const syncSend = () => {
    sendBtn.replaceChildren(icon(activeRun ? "stop" : "send"));
    sendBtn.setAttribute("aria-label", activeRun ? "Dừng" : "Gửi");
    sendBtn.title = activeRun ? "Dừng" : "Gửi";
    sendBtn.disabled = !activeRun && !input.value.trim();
  };
  input.addEventListener("input", () => { resize(); syncSend(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!activeRun) submit(); }
  });
  sendBtn.addEventListener("click", () => { if (activeRun) stop(); else submit(); });

  function userBubble(text) {
    return h("div", { class: "msg user" }, h("div", { class: "bubble" }, text));
  }

  function continuationNote() {
    return h("div", { class: "continuation-note small" }, "↻ Agent tiếp tục với kết quả các lệnh vừa duyệt");
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
      const box = h("div", { class: "edit-box save-box" },
        h("div", { class: "small" }, "Lưu các bước công cụ đã chạy thành công ở lượt này thành một công cụ dùng lại được. Giá trị bạn đã gõ trong yêu cầu sẽ thành tham số."),
        h("div", { class: "row" }, name, scope,
          h("button", { type: "button", class: "ghost", onclick: () => box.remove() }, "Hủy"),
          h("button", { type: "button", onclick: guard(async () => {
            const tool = await api("POST", "/api/learned-tools/from-episode", { episode_id: m.id, name: name.value || null, scope: scope.value });
            box.remove();
            toast(tool.status === "active" ? `Đã tạo ${tool.name}. Nhắc tên này trong chat để agent dùng.` : `Đã gửi ${tool.name} cho admin duyệt.`);
          }) }, "Lưu")));
      row.after(box);
      name.focus();
    });
    row.append(copyBtn, up, down, edit);
    if (saveTool) row.append(saveTool);
    if (m.task === lastTask) row.append(iconButton("retry", "Gửi lại", () => send(m.task)));
    row.append(h("span", { class: "meta" }, `${m.env} · ${m.ku_used} KU${m.tokens ? ` · ${m.tokens} token` : ""}`));
    return row;
  }

  function assistantBlock(m) {
    const body = h("div", { class: "prose" });
    if (m.error) body.append(h("div", { class: "msg-error" }, m.output));
    else body.append(renderMarkdown(m.output || ""));
    const open = m.approvals.filter((a) => a.status === "pending" && a.can_decide);
    const bulk = open.length > 1 && h("div", { class: "row approval-bulk" }, h("span", { class: "small" }, `${open.length} lệnh đang chờ duyệt`),
      h("button", { type: "button", onclick: guard(() => decideAll(m, open, "approve_once")) }, "Chạy tất cả"),
      h("button", { type: "button", class: "ghost", onclick: guard(() => decideAll(m, open, "approve_session")), title: "Cùng công cụ và namespace, trong 1 giờ" }, "Cho phép tất cả trong 1 giờ"));
    const canContinue = !m.continued && m.approvals.length > 0 && !m.approvals.some((a) => a.status === "pending") && m.approvals.some((a) => a.status === "approved");
    const resume = canContinue && h("div", { class: "row" }, h("button", { type: "button", class: "ghost", onclick: () => send(null, m.id) }, "Tiếp tục với kết quả các lệnh đã duyệt"));
    const el = h("div", { class: "msg assistant", "data-ep": m.id }, activityView(m.steps, null), bulk, m.approvals.map((a) => approvalCard(a, decide)), resume, body,
      m.cancelled && h("div", { class: "small" }, "Đã dừng"), actionsRow(m));
    return el;
  }

  async function decide(a, action, reason) {
    const r = await api("POST", `/api/approvals/${a.id}/decide`, { action, reason });
    const failed = r.result && r.result.status === "error";
    const extra = r.also_approved && r.also_approved.length ? ` (và ${r.also_approved.length} lệnh cùng loại)` : "";
    toast(r.status === "pending" ? "Đã ghi nhận, cần thêm người duyệt" : r.status === "denied" ? "Đã từ chối lệnh" : failed ? "Lệnh đã chạy nhưng báo lỗi" : "Đã chạy lệnh" + extra, failed);
    await refreshAndContinue();
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
  async function refreshAndContinue() {
    const data = await api("GET", `/api/conversations/${state.conversationId}`);
    for (const m of data.messages) if (blocks.has(m.id)) replaceBlock(m);
    maybeContinue(data.messages);
  }

  function maybeContinue(messages) {
    const last = messages[messages.length - 1];
    if (!last || activeRun || last.continued || !last.approvals.length) return;
    if (last.approvals.some((a) => a.status === "pending") || !last.approvals.some((a) => a.status === "approved")) return;
    send(null, last.id);
  }

  function replaceBlock(m) {
    const old = blocks.get(m.id);
    const fresh = assistantBlock(m);
    blocks.set(m.id, fresh);
    if (old) old.replaceWith(fresh);
    return fresh;
  }

  function addMessage(m) {
    if (!m.continuation) lastTask = m.task;
    thread.append(m.continuation ? continuationNote() : userBubble(m.task));
    const el = assistantBlock(m);
    blocks.set(m.id, el);
    thread.append(el);
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
    resize();
    await send(text);
  }

  async function stop() {
    if (!activeRun || !activeRun.id) return;
    sendBtn.disabled = true;
    try { await api("POST", `/api/chat/${activeRun.id}/cancel`); } catch (e) { toast(e.message, true); sendBtn.disabled = false; }
  }

  async function send(text, continueEpisode) {
    if (activeRun) return;
    const empty = thread.querySelector(".empty");
    if (empty) empty.remove();
    if (continueEpisode) thread.append(continuationNote());
    else { lastTask = text; thread.append(userBubble(text)); }
    const typing = h("div", { class: "typing", "aria-label": "Agent đang trả lời" }, h("i"), h("i"), h("i"));
    const live = { text: "", steps: [], running: [] };
    const activitySlot = h("div");
    const approvalsSlot = h("div");
    const body = h("div", { class: "prose" }, typing);
    const liveEl = h("div", { class: "msg assistant" }, activitySlot, approvalsSlot, body);
    thread.append(liveEl);
    stick = true;
    scroll();
    activeRun = { id: null };
    syncSend();

    let frame = 0;
    const paint = () => {
      frame = 0;
      if (live.text) body.replaceChildren(renderMarkdown(live.text));
      scroll();
    };
    const queuePaint = () => { if (!frame) frame = requestAnimationFrame(paint); };
    const paintActivity = () => {
      const open = activitySlot.querySelector("details")?.open;
      const view = activityView(live.steps, live.running);
      if (view && open) view.open = true;
      activitySlot.replaceChildren(...(view ? [view] : []));
      scroll();
    };
    const fail = (message) => { body.replaceChildren(h("div", { class: "msg-error" }, message)); };

    const handle = (ev) => {
      if (ev.type === "start") {
        activeRun.id = ev.run_id;
        const isNew = state.conversationId !== ev.conversation_id;
        state.conversationId = ev.conversation_id;
        if (isNew) refreshConversations();
      } else if (ev.type === "delta") { live.text += ev.text; queuePaint(); }
      else if (ev.type === "tool_start") { live.running.push(ev.tool); paintActivity(); }
      else if (ev.type === "tool_end") {
        const i = live.running.indexOf(ev.step.tool);
        if (i !== -1) live.running.splice(i, 1);
        live.steps.push(ev.step);
        paintActivity();
      }
      else if (ev.type === "done") {
        if (frame) cancelAnimationFrame(frame);
        blocks.set(ev.message.id, liveEl);
        replaceBlock(ev.message);
        scroll();
        refreshConversations();
        if (ev.message.approvals.some((a) => a.status === "pending")) pollApprovals();
      } else if (ev.type === "error") fail(ev.message);
    };

    try {
      const res = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json", "X-Tenant-Id": state.tenant.id }, credentials: "same-origin", body: JSON.stringify(continueEpisode ? { continue_episode: continueEpisode, conversation_id: state.conversationId, env: env.value }
        : { message: text, conversation_id: state.conversationId, env: env.value }) });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        fail(typeof (data && data.detail) === "string" ? data.detail : `Lỗi ${res.status}`);
      } else {
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
    } catch (e) {
      fail("Mất kết nối. Câu trả lời vẫn có thể được lưu, hãy mở lại hội thoại sau ít phút.");
    } finally {
      activeRun = null;
      syncSend();
      input.focus();
    }
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
    h("div", { class: "composer" }, input, h("div", { class: "composer-bar" }, env, h("span", { class: "spacer" }), sendBtn)),
    h("div", { class: "hint" }, notice + "Agent dùng công cụ thật qua các kết nối bạn được phép. Enter để gửi, Shift+Enter xuống dòng."));
  main.append(scroller, toBottom, composer);

  return (async () => {
    if (state.conversationId) {
      try {
        const data = await api("GET", `/api/conversations/${state.conversationId}`);
        data.messages.forEach(addMessage);
        if (data.messages.some((m) => m.approvals.some((a) => a.status === "pending"))) pollApprovals();
        requestAnimationFrame(scroll);
      } catch (e) {
        state.conversationId = null;
        toast(e.message, true);
      }
    }
    if (!thread.children.length) thread.append(emptyState());
    input.focus();
  })();
}
