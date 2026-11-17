const state = { ventureId: null, modelReady: false, config: null,
                cy: null, pollTimer: null, readyForBrief: false,
                etaTimer: null, etaStart: 0, etaTotal: 0,
                analyzedCache: null, askHistory: [] };
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const STORAGE_KEY = "ventureos.state.v1";
const STAGE_ETAS = { discover: 90, validate: 30, build: 45,
                     commercial: 30, launch: 25, operate: 40 };

/* ── Persistence ─────────────────────────────────────────────────────── */
function saveState() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ ventureId: state.ventureId })); } catch {}
}
function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const d = JSON.parse(raw);
    if (d && typeof d.ventureId === "string") state.ventureId = d.ventureId;
  } catch {}
}
function clearState() {
  try { localStorage.removeItem(STORAGE_KEY); } catch {}
  state.ventureId = null;
  state.analyzedCache = null;
  state.askHistory = [];
  state.mapAutoTried = false;
}

/* ── Start over ──────────────────────────────────────────────────── */
function confirmStartOver() {
  return new Promise((resolve) => {
    const bd = document.createElement("div");
    bd.className = "vo-modal-backdrop";
    const m = document.createElement("div");
    m.className = "vo-modal";
    m.innerHTML = `
      <button class="vo-close" aria-label="Close">×</button>
      <h3>Start over?</h3>
      <p style="line-height:1.6;color:var(--muted);margin:0 0 20px">
        This returns you to the landing page so you can describe a new idea.
        Your current venture stays saved on the server — nothing is deleted.
      </p>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="ghost" id="cancel-start-over">Cancel</button>
        <button class="primary" id="confirm-start-over">Yes, start over</button>
      </div>
    `;
    bd.appendChild(m);
    document.body.appendChild(bd);

    const close = (result) => { bd.remove(); resolve(result); };
    m.querySelector(".vo-close").onclick = () => close(false);
    m.querySelector("#cancel-start-over").onclick = () => close(false);
    m.querySelector("#confirm-start-over").onclick = () => close(true);
    bd.onclick = (e) => { if (e.target === bd) close(false); };
    const esc = (e) => {
      if (e.key === "Escape") {
        close(false);
        document.removeEventListener("keydown", esc);
      }
    };
    document.addEventListener("keydown", esc);
  });
}

async function startOver() {
  const ok = await confirmStartOver();
  if (!ok) return;

  // Forget the current venture selection
  clearState();

  // Tear down any running client-side state
  if (state.cy) { try { state.cy.destroy(); } catch {} state.cy = null; }
  stopPoll(); stopETA();

  // Reset UI to landing
  $("#workspace").classList.add("hidden");
  $("#discovery").classList.add("hidden");
  const chat = $("#chat"); if (chat) chat.innerHTML = "";
  const idea = $("#idea-input"); if (idea) idea.value = "";
  const ans = $("#answer-input"); if (ans) ans.value = "";
  $("#landing").classList.remove("hidden");
  state.readyForBrief = false;
  state.analyzedCache = null;

  toastOk("Ready for a fresh venture");
}

function ensureStartOverButton() {
  const sidebar = document.querySelector(".sidebar");
  if (!sidebar || document.getElementById("start-over-btn")) return;
  const btn = document.createElement("button");
  btn.id = "start-over-btn";
  btn.className = "ghost start-over-btn";
  btn.textContent = "Start over";
  btn.title = "Start a new venture";
  btn.onclick = startOver;
  sidebar.appendChild(btn);
}

function ensureDiscoveryStartOver() {
  const head = document.querySelector(".discovery-head");
  if (!head || document.getElementById("discovery-start-over")) return;
  const btn = document.createElement("button");
  btn.id = "discovery-start-over";
  btn.className = "ghost";
  btn.style.cssText = "margin-left:auto;font-size:13px;padding:6px 12px";
  btn.textContent = "Start over";
  btn.onclick = startOver;
  head.appendChild(btn);
}

/* ── Arrow-button transform ─────────────────────────────────────────── */
(function() {
  const ARROW_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M5 12h14M13 6l6 6-6 6"/></svg>';
  function transform() {
    const ids = ["submit-idea", "send-answer", "ask-send"];
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el && !el.classList.contains("arrow-btn")) {
        el.classList.add("arrow-btn");
        el.innerHTML = ARROW_SVG;
      }
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", transform);
  } else {
    transform();
  }
})();

function toast(m, kind) {
  kind = kind || "info";
  const el = document.createElement("div");
  el.className = "vo-toast vo-toast-" + kind;
  el.textContent = m;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add("in"));
  setTimeout(() => { el.classList.remove("in"); setTimeout(() => el.remove(), 300); }, 4000);
}
function toastError(m) { toast(m, "error"); }
function toastOk(m) { toast(m, "success"); }

async function api(path, opts) {
  opts = opts || {};
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json();
}

function formatStatus(s) {
  if (!s) return "Unknown";
  const map = {
    "not_started": "Not started",
    "in_progress": "Running",
    "awaiting_approval": "Awaiting approval",
    "awaiting_input": "Awaiting input",
    "completed": "Completed",
    "stopped": "Stopped",
  };
  return map[s] || s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/* ── Plain-English helpers ───────────────────────────────────────────── */
const DECISION_TEXT = { GO: "Go ahead", REPEAT: "Repeat this stage", PIVOT: "Pivot", STOP: "Stop",
                        APPROVED: "Approved by you", REJECTED: "Rejected by you" };
const AGENT_NAMES = { founder: "You (founder)", founder_understanding: "Founder interview",
  feasibility: "Feasibility analyst", validation: "Validation analyst", development: "Prototype builder",
  testing: "Prototype tester", strategy: "Product strategist", financial: "Financial analyst",
  commercial: "Pricing analyst", marketing: "Marketing writer", operations: "Operations planner",
  research: "Market researcher", case_research: "Case researcher", business_map: "Business mapper" };
function humanize(s) {
  s = String(s ?? "");
  if (s.includes("://")) return s;
  return s.replace(/\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b/g, (m) => m.replace(/_/g, " "));
}
function sentenceCase(s) { s = humanize(s).trim(); return s.charAt(0).toUpperCase() + s.slice(1); }
function stageLabel(s) { return (state.config && state.config.stage_labels[s]) || sentenceCase(s); }
function decisionBadge(code, text) {
  const c = String(code || "").toUpperCase();
  return `<span class="decision-badge d-${escapeHtml(c.toLowerCase())}">${escapeHtml(text || DECISION_TEXT[c] || sentenceCase(c))}</span>`;
}
function renderReport(r, compact) {
  if (!r) return "";
  const hl = (r.highlights || []).map((h) =>
    `<div class="hl"><div class="hl-k">${escapeHtml(h.label)}</div><div class="hl-v">${escapeHtml(h.value)}</div></div>`).join("");
  const secs = (r.sections || []).map((sec) =>
    `<details class="report-sec"${compact ? "" : " open"}><summary>${escapeHtml(sec.title)} <span class="muted">(${sec.items.length})</span></summary>
     <ul>${sec.items.map((i) => `<li>${escapeHtml(i)}</li>`).join("")}</ul></details>`).join("");
  const links = (r.links || []).map((l) => l.url.startsWith("#")
    ? `<button class="primary" data-goto="${escapeHtml(l.url.slice(1))}">${escapeHtml(l.label)}</button>`
    : `<a class="report-link" href="${escapeHtml(l.url)}" target="_blank" rel="noopener">${escapeHtml(l.label)} ↗</a>`).join("");
  return `<div class="card report-card">
    <div class="report-head"><h3>${escapeHtml(r.stage_label)} — summary</h3>${decisionBadge(r.decision, r.decision_text)}</div>
    <p class="report-summary">${escapeHtml(r.summary)}</p>
    ${hl ? `<div class="hl-grid">${hl}</div>` : ""}
    ${links ? `<div class="report-links">${links}</div>` : ""}
    ${secs}
  </div>`;
}
function bindReportLinks(root) {
  root.querySelectorAll("[data-goto]").forEach((b) => { b.onclick = () => switchPanel(b.dataset.goto); });
}

/* ── Rename project ─────────────────────────────────────────────────── */
function beginRename() {
  const el = $("#venture-name");
  if (!el || el.querySelector("input") || !state.ventureId) return;
  const current = el.textContent.trim();
  el.innerHTML = `<input class="rename-input" maxlength="80" value="${escapeHtml(current)}" aria-label="Project name">`;
  const inp = el.querySelector("input");
  inp.focus(); inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const name = inp.value.trim();
    if (save && name && name !== current) {
      try {
        await api(`/api/ventures/${state.ventureId}/rename`, { method: "POST", body: JSON.stringify({ name }) });
        el.textContent = name; toastOk("Renamed");
        return;
      } catch (e) { toastError("Rename failed: " + e.message); }
    }
    el.textContent = current;
  };
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") finish(false);
  });
  inp.addEventListener("blur", () => finish(true));
}

const THEME_PRESETS = {
  midnight: { accent: "#6366f1", accent_2: "#a855f7", bg: "#07070f", surface: "#0f0f1a" },
  aurora:   { accent: "#14b8a6", accent_2: "#8b5cf6", bg: "#060d0d", surface: "#0c1618" },
  sunset:   { accent: "#f97316", accent_2: "#ec4899", bg: "#120508", surface: "#1a0a10" },
  forest:   { accent: "#22c55e", accent_2: "#06b6d4", bg: "#060a08", surface: "#0a1410" },
  ocean:    { accent: "#3b82f6", accent_2: "#06b6d4", bg: "#050a14", surface: "#0a1220" },
  mono:     { accent: "#e5e7eb", accent_2: "#a1a1aa", bg: "#050505", surface: "#0e0e0e" },
};

function _hexLum(hex) {
  try {
    const h = (hex || "").replace("#", "");
    if (h.length < 6) return 1;
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    return (0.2126*r + 0.7152*g + 0.0722*b) / 255;
  } catch { return 1; }
}
function _lighten(hex, amount) {
  try {
    const h = hex.replace("#", "");
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    const add = (c) => Math.min(255, Math.round(c + amount));
    return "#" + [add(r), add(g), add(b)].map(c => c.toString(16).padStart(2, "0")).join("");
  } catch { return hex; }
}
function _applyPreset(p) {
  if (!p) return;
  const r = document.documentElement.style;
  r.setProperty("--bg", p.bg);
  r.setProperty("--surface", p.surface || _lighten(p.bg, 8));
  r.setProperty("--accent", p.accent);
  r.setProperty("--accent-2", p.accent_2 || _lighten(p.accent, 30));
}
function applyVenturePalette(palette, fallbackTheme) {
  if (!palette || !palette.accent) {
    const a = themeToColor(fallbackTheme);
    _applyPreset({ bg: "#07070f", surface: "#0f0f1a", accent: a, accent_2: hexToAccent2(a) });
    return;
  }
  let bg = palette.background || "#07070f";
  if (_hexLum(bg) > 0.20) bg = "#07070f";
  let surf = palette.surface || _lighten(bg, 10);
  if (_hexLum(surf) > 0.30 || _hexLum(surf) < _hexLum(bg)) surf = _lighten(bg, 10);
  _applyPreset({ bg, surface: surf, accent: palette.accent, accent_2: palette.accent_2 || hexToAccent2(palette.accent) });
}
function renderThemePicker() {
  const box = document.getElementById("theme-swatches");
  if (!box) return;
  box.innerHTML = "";
  Object.entries(THEME_PRESETS).forEach(([name, p]) => {
    const btn = document.createElement("button");
    btn.className = "theme-swatch";
    btn.style.setProperty("--swatch-a", p.accent);
    btn.style.setProperty("--swatch-b", p.accent_2);
    btn.title = name;
    btn.onclick = async () => {
      _applyPreset(p);
      document.querySelectorAll(".theme-swatch").forEach((x) => x.classList.remove("active"));
      btn.classList.add("active");
      try {
        await api(`/api/ventures/${state.ventureId}/theme`, {
          method: "POST",
          body: JSON.stringify({ theme: { accent: p.accent, accent_2: p.accent_2,
                                         background: p.bg, surface: p.surface, mood: name } }),
        });
        toastOk("Theme saved: " + name);
      } catch (e) { toastError("Theme save failed: " + e.message); }
    };
    box.appendChild(btn);
  });
}

/* ── Bootstrap + refresh-restore ────────────────────────────────────── */
async function bootstrap() {
  try {
    const [health, cfg] = await Promise.all([
      api("/api/health").catch((e) => ({ ok: false, error: e.message })),
      api("/api/config"),
    ]);
    state.config = cfg;
    if (!health.ok) {
      const b = $("#ollama-banner");
      b.textContent = "AI service not ready.\n\n" + (health.error || "") + "\n\nFix your provider and restart.";
      b.classList.remove("hidden");
    } else state.modelReady = true;
  } catch (e) {
    const b = $("#ollama-banner");
    b.textContent = "Backend unreachable: " + e.message;
    b.classList.remove("hidden");
  }

  loadState();
  if (state.ventureId && state.config) {
    try {
      const data = await api(`/api/ventures/${state.ventureId}`);
      if (data.venture.brief) {
        $("#landing").classList.add("hidden");
        $("#discovery").classList.add("hidden");
        $("#workspace").classList.remove("hidden");
        await refresh();
        renderThemePicker();
        ensureAskPanel();
      } else if (data.messages && data.messages.length > 0) {
        $("#landing").classList.add("hidden");
        $("#discovery").classList.remove("hidden");
        for (const m of data.messages) addChat(m.role, m.content);
        $("#answer-input").placeholder = "Answer the agent… (or start with ? to ask anything)";
        ensureDiscoveryStartOver();
        const sum = await api(`/api/ventures/${state.ventureId}/summary`).catch(() => null);
        if (sum && Object.keys(sum.answers || {}).filter((k) => sum.answers[k]).length >= 4
            && !state.readyForBrief) {
          state.readyForBrief = true;
          await showSummaryConfirm();
        }
      }
    } catch (e) {
      clearState();
    }
  }
}

{
  const vn = $("#venture-name");
  if (vn) { vn.title = "Click to rename"; vn.classList.add("renamable"); vn.addEventListener("click", beginRename); }
}

$$(".chip").forEach((c) => c.addEventListener("click", () => {
  $("#idea-input").value = c.textContent.trim();
  $("#idea-input").focus();
}));

$("#submit-idea").addEventListener("click", async () => {
  const idea = $("#idea-input").value.trim();
  if (!idea) return;
  try {
    const v = await api("/api/ventures", { method: "POST", body: JSON.stringify({ idea }) });
    state.ventureId = v.id;
    saveState();
    $("#landing").classList.add("hidden");
    $("#discovery").classList.remove("hidden");
    $("#answer-input").placeholder = "Answer the agent… (or start with ? to ask anything)";
    addChat("user", idea);
    ensureDiscoveryStartOver();
    await askNext(idea);
  } catch (e) { toastError("Create failed: " + e.message); }
});

function addChat(role, text) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.textContent = text;
  $("#chat").appendChild(el);
  $("#chat").scrollTop = $("#chat").scrollHeight;
}
function addChatNode(node) {
  $("#chat").appendChild(node);
  $("#chat").scrollTop = $("#chat").scrollHeight;
}
function showTyping() {
  hideTyping();
  const el = document.createElement("div");
  el.className = "msg typing";
  el.id = "typing-indicator";
  el.innerHTML = "<span></span><span></span><span></span>";
  $("#chat").appendChild(el);
  $("#chat").scrollTop = $("#chat").scrollHeight;
}
function hideTyping() {
  const el = document.getElementById("typing-indicator");
  if (el) el.remove();
}

/* ── Ask flow — `?` prefix in discovery routes to /ask ───────────────── */
async function askNext(message) {
  const trimmed = message.trim();
  if (trimmed.startsWith("?")) {
    showTyping();
    try {
      const res = await api(`/api/ventures/${state.ventureId}/ask`, {
        method: "POST",
        body: JSON.stringify({ question: trimmed.slice(1).trim(), history: [] })
      });
      hideTyping();
      addChat("assistant", res.answer);
    } catch (e) {
      hideTyping();
      addChat("assistant", "⚠️ " + e.message);
    }
    return;
  }
  showTyping();
  try {
    const res = await api("/api/founder/next-question", {
      method: "POST", body: JSON.stringify({ venture_id: state.ventureId, message }) });
    hideTyping();
    if (res.question) addChat("assistant", res.question);
    if (res.ready_for_brief && !state.readyForBrief) {
      state.readyForBrief = true;
      await showSummaryConfirm();
    }
  } catch (e) { hideTyping(); addChat("assistant", "⚠️ " + e.message); }
}

async function showSummaryConfirm() {
  document.querySelectorAll(".summary-card").forEach((el) => el.remove());
  let summary;
  try { summary = await api(`/api/ventures/${state.ventureId}/summary`); }
  catch (e) { toastError("Summary failed: " + e.message); return; }

  const card = document.createElement("div");
  card.className = "summary-card";
  card.innerHTML = `<h4>Confirm before drafting the plan</h4><div class="muted" style="padding:6px 0">Analyzing your answers…</div>`;
  addChatNode(card);

  let analyzed = null;
  try {
    const r = await api(`/api/ventures/${state.ventureId}/analyze-summary`, { method: "POST" });
    analyzed = r.analyzed || null;
    if (analyzed) state.analyzedCache = analyzed;
  } catch {}

  const fields = { problem: "Problem", differentiation: "Unique angle", target_market: "Target market",
                   budget: "Budget", skills: "Skills", education: "Education" };
  const rows = Object.entries(fields).map(([k, lbl]) => {
    const raw = (summary.answers || {})[k];
    const pretty = analyzed && analyzed[k] && String(analyzed[k]).trim() ? String(analyzed[k]).trim() : null;
    const v = pretty || raw || "";
    const cls = v && v !== "Not specified" ? "" : " empty";
    const tip = pretty && raw && pretty !== raw ? ` title="You said: ${escapeHtml(raw)}"` : "";
    return `<li><span class="k">${lbl}</span><span class="v${cls}"${tip}>${v && v !== "Not specified" ? escapeHtml(v) : "not provided"}</span></li>`;
  }).join("");

  card.innerHTML = `
    <h4>Confirm before drafting the plan</h4>
    <ul>
      <li><span class="k">Idea</span><span class="v">${escapeHtml(summary.idea)}</span></li>
      ${rows}
    </ul>
    <div class="actions">
      <button class="primary" id="confirm-draft">Confirm — draft plan</button>
      <button class="ghost" id="edit-answers">Edit — add more</button>
    </div>`;
  card.querySelector("#confirm-draft").onclick = () => draftBrief(card);
  card.querySelector("#edit-answers").onclick = () => {
    card.remove(); state.readyForBrief = false;
    addChat("assistant", "Sure — add context, then I'll re-check when ready.");
    $("#answer-input").focus();
  };
}

async function draftBrief(card) {
  if (card) card.remove();
  showTyping();
  try {
    const res = await api("/api/founder/draft-brief", {
      method: "POST", body: JSON.stringify({ venture_id: state.ventureId, message: "Draft" }) });
    hideTyping();
    const b = res.brief;
    addChat("assistant", `Plan drafted.\n\nName: ${b.name}\nAudience: ${b.target_audience}\n\nEntering workspace…`);
    setTimeout(enterWorkspace, 800);
  } catch (e) { hideTyping(); addChat("assistant", "⚠️ " + e.message); }
}

$("#send-answer").addEventListener("click", async () => {
  const t = $("#answer-input").value.trim();
  if (!t) return;
  $("#answer-input").value = "";
  addChat("user", t);
  await askNext(t);
});
$("#answer-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#send-answer").click(); }
});

async function enterWorkspace() {
  $("#discovery").classList.add("hidden");
  $("#workspace").classList.remove("hidden");
  ensureAskPanel();
  ensureStartOverButton();
  await refresh();
  renderThemePicker();
}

/* ── Ask AI panel injection ──────────────────────────────────────────── */
function ensureAskPanel() {
  const nav = $(".sidebar nav");
  if (nav && !nav.querySelector('[data-section="ask"]')) {
    const a = document.createElement("a");
    a.dataset.section = "ask";
    a.textContent = "Ask AI";
    a.addEventListener("click", () => switchPanel("ask"));
    nav.appendChild(a);
  }
  const mp = $(".main-panel");
  if (mp && !$("#panel-ask")) {
    const panel = document.createElement("div");
    panel.className = "panel hidden";
    panel.id = "panel-ask";
    panel.innerHTML = `<h2>Ask AI</h2>
      <p class="muted" style="margin-top:-12px;margin-bottom:16px">
        Ask anything about your venture, the market, or general business questions.
      </p>
      <div id="ask-log" class="ask-log"></div>
      <div class="composer composer-inline" style="margin-top:8px">
        <textarea id="ask-input" rows="2" placeholder="Ask anything…"></textarea>
        <button id="ask-send" class="primary">Send</button>
      </div>`;
    mp.appendChild(panel);
    const askBtn = $("#ask-send");
    if (askBtn) {
      askBtn.classList.add("arrow-btn");
      askBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
    }
    $("#ask-send").addEventListener("click", sendAsk);
    $("#ask-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendAsk(); }
    });
  }
}

function switchPanel(section) {
  $$(".sidebar nav a").forEach((x) => x.classList.remove("active"));
  const link = document.querySelector(`.sidebar nav a[data-section="${section}"]`);
  if (link) link.classList.add("active");
  $$(".panel").forEach((p) => p.classList.add("hidden"));
  const panel = $("#panel-" + section);
  if (!panel) return;
  panel.classList.remove("hidden");
  panel.classList.remove("entering");
  void panel.offsetHeight;
  panel.classList.add("entering");
  if (section === "mindmap") renderMindMap();
  if (section === "decisions") renderDecisions();
  if (section === "ask") { const i = $("#ask-input"); if (i) i.focus(); }
}

async function sendAsk() {
  const input = $("#ask-input");
  const log = $("#ask-log");
  if (!input || !log) return;
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  appendAskMsg(log, "user", q);
  const pending = appendAskMsg(log, "assistant", "Thinking…", "pending");
  try {
    const res = await api(`/api/ventures/${state.ventureId}/ask`, {
      method: "POST",
      body: JSON.stringify({ question: q, history: state.askHistory.slice(-10) })
    });
    pending.remove();
    appendAskMsg(log, "assistant", res.answer);
    state.askHistory.push({ role: "user", content: q }, { role: "assistant", content: res.answer });
  } catch (e) {
    pending.remove();
    appendAskMsg(log, "assistant", "⚠️ " + e.message);
  }
}

function appendAskMsg(log, role, text, extra) {
  const el = document.createElement("div");
  el.className = "ask-msg " + role + (extra ? " " + extra : "");
  el.textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

/* ── Refresh (workspace) ────────────────────────────────────────────── */
async function refresh() {
  const analyzedPromise = state.analyzedCache
    ? Promise.resolve({ analyzed: state.analyzedCache })
    : api(`/api/ventures/${state.ventureId}/analyze-summary`, { method: "POST" }).catch(() => null);

  const [data, summary, analyzedResp] = await Promise.all([
    api(`/api/ventures/${state.ventureId}`),
    api(`/api/ventures/${state.ventureId}/summary`).catch(() => null),
    analyzedPromise,
  ]);
  if (analyzedResp && analyzedResp.analyzed) state.analyzedCache = analyzedResp.analyzed;

  if (!$("#venture-name").querySelector("input")) $("#venture-name").textContent = data.venture.name;
  applyVenturePalette(data.venture.brief && data.venture.brief.theme, data.venture.theme);

  // Stage list
  const sl = $("#stage-list");
  sl.innerHTML = "";
  for (const s of state.config.stages) {
    const st = data.stages[s] || { status: "not_started" };
    const row = document.createElement("div");
    row.className = "stage-row" + (s === data.venture.current_stage ? " current" : "");
    row.innerHTML = `<span>${state.config.stage_labels[s]}</span><span class="status">${formatStatus(st.status)}</span>`;
    sl.appendChild(row);
  }

  const currentStage = data.venture.current_stage;
  const currentStatus = ((data.stages[currentStage] || {}).status) || "not_started";
  const stagesAll = state.config.stages;
  const allDone = currentStage === stagesAll[stagesAll.length - 1] && currentStatus === "completed";
  const runnable = ["not_started", "awaiting_input", "stopped"].includes(currentStatus);
  const runLabel = allDone ? "All six stages complete"
    : currentStatus === "stopped" ? "Re-run stage (after pivoting)"
    : currentStatus === "awaiting_input" ? "Run stage again"
    : runnable ? "Run current stage"
    : currentStatus === "in_progress" ? "Stage running…"
    : currentStatus === "awaiting_approval" ? "Awaiting your approval below"
    : currentStatus === "completed" ? "Stage completed"
    : "Not runnable";

  const warn = data.venture.gate === "STOP"
    ? `<div style="margin-top:12px;padding:12px;border-radius:10px;background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.35);line-height:1.5"><strong>⚠️ Stop recommended.</strong> The agents found this venture not feasible as described. Review the summary below — approve to continue anyway, or reject and re-run after adjusting the idea.</div>`
    : "";
  const vid = state.ventureId;
  const doneBanner = allDone ? `<div class="done-banner"><strong>🎉 Your venture plan is complete.</strong>
      <div class="report-links" style="margin-top:10px">
        <a class="report-link" href="/api/ventures/${vid}/artifacts/report" target="_blank" rel="noopener">Full venture report ↗</a>
        <a class="report-link" href="/api/ventures/${vid}/artifacts/prototype" target="_blank" rel="noopener">Prototype ↗</a>
        <a class="report-link" href="/api/ventures/${vid}/artifacts/validation-plan" target="_blank" rel="noopener">Validation plan ↗</a>
        <button class="ghost" data-goto="mindmap">Business mind map</button>
      </div></div>` : "";
  $("#overview-content").innerHTML = `
    <div style="line-height:1.6"><strong>Idea:</strong> ${escapeHtml(data.venture.idea)}</div>
    <div style="margin-top:8px"><strong>Stage:</strong> ${state.config.stage_labels[currentStage]}</div>
    <div style="margin-top:8px"><strong>Latest decision:</strong> ${data.venture.gate && data.venture.gate !== "pending" ? decisionBadge(data.venture.gate) : "None yet"}</div>
    ${warn}${doneBanner}
    <button id="run-current" class="primary${currentStatus === "in_progress" ? " running" : ""}" style="margin-top:14px"${runnable ? "" : " disabled"}>${escapeHtml(runLabel)}</button>
    ${currentStatus === "in_progress" ? '<div id="run-eta" class="run-eta"></div>' : ""}
    <div id="stage-feedback" style="margin-top:14px"></div>`;
  if (runnable) $("#run-current").onclick = runStage;

  // Stage reports: current stage first, then earlier ones (collapsed)
  const reportOf = (s) => ((data.stages[s] || {}).notes || {}).report;
  const fbBox = $("#stage-feedback");
  if (currentStatus !== "in_progress" && reportOf(currentStage)) fbBox.innerHTML = renderReport(reportOf(currentStage));
  const earlier = stagesAll.filter((s) => s !== currentStage && reportOf(s)).reverse();
  if (earlier.length) {
    fbBox.insertAdjacentHTML("beforeend", `<div class="earlier-reports"><h4>Earlier stages</h4>${earlier.map((s) => {
      const r = reportOf(s);
      return `<details class="earlier"><summary>${escapeHtml(r.stage_label)} ${decisionBadge(r.decision, r.decision_text)}</summary>${renderReport(r, true)}</details>`;
    }).join("")}</div>`);
  }
  bindReportLinks($("#overview-content"));

  // Polling + ETA for in-progress stages (survives refresh)
  if (currentStatus === "in_progress") {
    startPoll(currentStage);
    const cps = ((data.stages[currentStage] || {}).notes || {}).checkpoints || [];
    let fromTime = Date.now();
    if (cps.length > 0 && cps[0].time) {
      try { fromTime = new Date(cps[0].time.replace(" ", "T") + "Z").getTime(); } catch {}
    }
    startETA(currentStage, fromTime);
  } else {
    stopPoll();
    stopETA();
  }

  // Knowledge card with analyzed values + edit buttons
  if (summary) {
    const old = document.getElementById("knowledge-card");
    if (old) old.remove();
    const kc = document.createElement("div");
    kc.id = "knowledge-card";
    kc.className = "card knowledge-card";
    const fields = { budget: "Budget", skills: "Skills", education: "Education", target_market: "Target market" };
    const analyzed = state.analyzedCache || {};
    kc.innerHTML = `<h3>Knowledge provided</h3><ul class="knowledge-list">
      <li data-field="idea"><span class="k">Idea</span><span class="v">${escapeHtml(summary.idea)}</span><span></span><span></span></li>
      ${Object.entries(fields).map(([k, lbl]) => {
        const pretty = (analyzed[k] || (summary.answers || {})[k] || "").toString().trim();
        const has = pretty && pretty !== "Not specified";
        const display = has ? escapeHtml(pretty) : '<span style="color:var(--muted);font-style:italic">Not provided</span>';
        return `<li data-field="${k}">
          <span class="k">${lbl}</span>
          <span class="v">${display}</span>
          <button class="edit-field-btn" data-field="${k}" title="Edit">✎</button>
          <span></span>
        </li>`;
      }).join("")}
    </ul>`;
    $("#overview-content").insertAdjacentElement("afterend", kc);
    kc.querySelectorAll(".edit-field-btn").forEach((btn) => {
      btn.onclick = () => beginEditField(kc, btn.dataset.field);
    });
  }

  // Approvals
  const ap = $("#pending-approvals");
  ap.innerHTML = "<h3 style='margin-top:0'>Pending approvals</h3>";
  if (data.approvals.length === 0) {
    ap.innerHTML += "<div class='muted'>None.</div>";
  } else {
    for (const a of data.approvals) {
      const d = document.createElement("div");
      d.className = "card";
      const sources = (a.payload.sources_used || []);
      d.innerHTML = `
        <div style="font-weight:600;margin-bottom:8px">${escapeHtml(a.payload.stage_label || sentenceCase(String(a.action || "").replace(/_gate$/, "")))} decision: ${decisionBadge(a.payload.recommendation, a.payload.decision_text)}</div>
        <div style="color:var(--muted);line-height:1.6;white-space:normal;word-wrap:break-word">${escapeHtml(humanize(a.payload.rationale || "No rationale."))}</div>
        ${a.payload.classification ? `<div style="margin-top:8px;font-size:13px;color:var(--muted)">Result: ${escapeHtml(sentenceCase(String(a.payload.classification).toLowerCase()))}</div>` : ""}
        ${sources.length > 0 ? `<div class="approval-source-list"><div class="label">Sources used</div>${sources.map((s) => `<div><a href="${escapeHtml(s)}" target="_blank" rel="noopener">${escapeHtml(s)}</a></div>`).join("")}</div>` : ""}
        ${(a.payload.unsupported_claims || []).length > 0 ? `<div style="margin-top:12px;padding:10px;border-radius:8px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#fca5a5;font-size:13px"><strong>⚠️ Unsupported claims:</strong><ul style="margin:6px 0 0 16px;padding:0">${(a.payload.unsupported_claims || []).map((c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul></div>` : ""}
        <div style="margin-top:12px;display:flex;gap:8px">
          <button class="primary" data-approve="${a.id}">${currentStage === stagesAll[stagesAll.length - 1] ? "Approve & finish" : "Approve & continue"}</button>
          <button class="ghost" data-reject="${a.id}">Reject</button>
        </div>`;
      d.querySelector("[data-approve]").onclick = async () => {
        try { const r = await api("/api/approve", { method: "POST", body: JSON.stringify({ venture_id: state.ventureId, approval_id: a.id }) });
          toastOk(r.finished ? "Venture plan complete 🎉" : "Approved — next: " + stageLabel(r.advanced_to)); await refresh();
        } catch (e) { toastError(e.message); }
      };
      d.querySelector("[data-reject]").onclick = async () => {
        try { await api("/api/reject", { method: "POST", body: JSON.stringify({ venture_id: state.ventureId, approval_id: a.id }) });
          toastError("Rejected"); await refresh();
        } catch (e) { toastError(e.message); }
      };
      ap.appendChild(d);
    }
  }

  // Stages panel
  const cards = $("#stage-cards");
  cards.innerHTML = "";
  for (const s of state.config.stages) {
    const st = data.stages[s] || { status: "not_started" };
    const c = document.createElement("div");
    c.className = "stage-card";
    const r = (st.notes || {}).report;
    c.innerHTML = `<h3>${state.config.stage_labels[s]}</h3><span class="status-tag">${formatStatus(st.status)}</span>
      ${r ? `<div style="margin-top:10px">${decisionBadge(r.decision, r.decision_text)}</div><p class="muted" style="line-height:1.55;margin:8px 0 0">${escapeHtml(r.summary)}</p>` : ""}`;
    cards.appendChild(c);
  }

  // Evidence
  const es = $("#evidence-summary");
  es.innerHTML = "<h3 style='margin-top:0'>Summary</h3>";
  if (Object.keys(data.evidence_summary).length === 0) es.innerHTML += "<div class='muted'>No evidence yet.</div>";
  else for (const [k, v] of Object.entries(data.evidence_summary)) {
    const row = document.createElement("div");
    row.textContent = `${k}: ${v}`;
    es.appendChild(row);
  }
  const el = $("#evidence-list");
  el.innerHTML = "";
  for (const e of data.evidence) {
    const d = document.createElement("div");
    d.className = "evidence-item";
    const src = e.source ? `<div class="src">↗ <a href="${escapeHtml(e.source)}" target="_blank" rel="noopener">${escapeHtml(e.source)}</a></div>` : "";
    d.innerHTML = `<div class="tag">${escapeHtml(e.type)} · ${escapeHtml(e.agent || "")}</div><div>${escapeHtml(e.claim)}</div>${src}`;
    el.appendChild(d);
  }
}

/* ── Edit field ──────────────────────────────────────────────────────── */
function beginEditField(card, field) {
  const li = card.querySelector(`li[data-field="${field}"]`);
  if (!li) return;
  const labels = { budget: "Budget", skills: "Skills", education: "Education", target_market: "Target market" };
  const currentText = li.querySelector(".v").textContent.trim();
  const current = currentText === "Not provided" ? "" : currentText;
  li.innerHTML = `
    <span class="k">${labels[field]}</span>
    <span class="v"><input class="edit-field-input" value="${escapeHtml(current)}" /></span>
    <button class="save-field-btn" title="Save">✓</button>
    <button class="cancel-field-btn" title="Cancel">✕</button>`;
  const input = li.querySelector(".edit-field-input");
  input.focus(); input.select();
  const save = async () => {
    const val = input.value.trim();
    try {
      await api(`/api/ventures/${state.ventureId}/update-field`, {
        method: "POST",
        body: JSON.stringify({ field, value: val })
      });
      state.analyzedCache = null;
      toastOk("Saved: " + labels[field]);
      await refresh();
    } catch (e) { toastError("Save failed: " + e.message); }
  };
  li.querySelector(".save-field-btn").onclick = save;
  li.querySelector(".cancel-field-btn").onclick = () => refresh();
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    if (e.key === "Escape") refresh();
  });
}

/* ── Run stage with blur + ETA ───────────────────────────────────────── */
async function runStage() {
  const data = await api(`/api/ventures/${state.ventureId}`);
  const stage = data.venture.current_stage;
  const btn = $("#run-current");
  if (!btn) return;
  btn.disabled = true;
  btn.classList.add("running");
  btn.innerHTML = '<span class="spinner"></span>Running…';

  let eta = document.getElementById("run-eta");
  if (!eta) {
    eta = document.createElement("div");
    eta.id = "run-eta";
    eta.className = "run-eta";
    btn.insertAdjacentElement("afterend", eta);
  }
  startETA(stage);
  startPoll(stage);

  state.stageRequestActive = true;
  try {
    const res = await api(`/api/ventures/${state.ventureId}/run-stage/${stage}`, { method: "POST" });
    state.stageRequestActive = false;
    stopPoll();
    stopETA();
    if (res.status === "not_implemented" || res.status === "wrong_stage" || res.status === "already_ran") {
      toastError(res.message);
      btn.classList.remove("running");
      btn.disabled = false; btn.textContent = "Run current stage"; return;
    }
    await refresh();
    if (res.stage_status === "stopped") toastError("Stop recommended — see the summary.");
    else toastOk(stageLabel(stage) + " finished — decision ready");
  } catch (e) {
    state.stageRequestActive = false;
    stopPoll(); stopETA();
    toastError("Stage failed: " + e.message);
    await refresh();
    btn.classList.remove("running");
    btn.disabled = false; btn.textContent = "Run current stage";
  }
}

/* ── ETA timer ───────────────────────────────────────────────────────── */
function startETA(stage, fromTime) {
  stopETA();
  state.etaStart = fromTime || Date.now();
  state.etaTotal = (STAGE_ETAS[stage] || 60) * 1000;
  updateETA(stage);
  state.etaTimer = setInterval(() => updateETA(stage), 1000);
}
function stopETA() { if (state.etaTimer) { clearInterval(state.etaTimer); state.etaTimer = null; } }
function updateETA(stage) {
  const box = document.getElementById("run-eta");
  if (!box) return;
  const elapsed = Math.max(0, Math.floor((Date.now() - state.etaStart) / 1000));
  const totalSec = Math.floor(state.etaTotal / 1000);
  const remaining = Math.max(0, totalSec - elapsed);
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (s) => `${Math.floor(s / 60)}:${pad(s % 60)}`;
  box.textContent = remaining === 0
    ? `${fmt(elapsed)} elapsed · finishing up…`
    : `${fmt(elapsed)} elapsed · ETA ~${fmt(remaining)}`;
}

/* ── Polling + checkpoint render ─────────────────────────────────────── */
function startPoll(stage) {
  stopPoll();
  const tick = async () => {
    try {
      const data = await api(`/api/ventures/${state.ventureId}`);
      const st = data.stages[stage] || {};
      const cps = (st.notes && st.notes.checkpoints) || [];
      renderCheckpoints(stage, cps, st.status);
      if (st.status && st.status !== "in_progress" && !state.stageRequestActive) {
        stopPoll(); stopETA(); await refresh();
      }
    } catch (e) {}
  };
  tick();
  state.pollTimer = setInterval(tick, 750);
}
function stopPoll() { if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; } }

function renderCheckpoints(stage, cps, status) {
  const box = $("#stage-feedback");
  if (!box) return;
  let panel = document.getElementById("checkpoint-panel");
  const rendered = panel ? parseInt(panel.dataset.rendered || "0", 10) : 0;
  if (!panel || panel.dataset.stage !== stage || cps.length < rendered) {
    if (panel) panel.remove();
    panel = document.createElement("div");
    panel.id = "checkpoint-panel";
    panel.className = "checkpoint-panel";
    panel.dataset.stage = stage;
    panel.dataset.rendered = "0";
    panel.innerHTML = `<h4>${escapeHtml(stageLabel(stage))} — live</h4><div class="checkpoint-list"></div>`;
    box.appendChild(panel);
  }
  const list = panel.querySelector(".checkpoint-list");
  if (!list) return;
  const already = parseInt(panel.dataset.rendered || "0", 10);
  if (cps.length === 0 && already === 0) return;
  for (let i = already; i < cps.length; i++) {
    const c = cps[i];
    const t = (c.time || "").slice(11, 19);
    const item = document.createElement("div");
    item.className = "checkpoint-item";
    item.innerHTML = `<span class="bullet"></span><span class="text">${escapeHtml(c.message)}</span><span class="time">${escapeHtml(t)}</span>`;
    item.addEventListener("click", () => openCheckpointDetail(c, stage));
    list.appendChild(item);
  }
  panel.dataset.rendered = String(cps.length);

  // Pulse ONLY the last bullet, and ONLY while the stage is running.
  const isRunning = status === "in_progress";
  const items = list.querySelectorAll(".checkpoint-item");
  items.forEach((el, i) => {
    el.classList.toggle("active", isRunning && i === items.length - 1);
  });
}

/* ── Checkpoint detail — prettified ──────────────────────────────────── */
function openCheckpointDetail(c, stage) {
  const fullTime = (c.time || "").replace("T", " ").replace("Z", " UTC").slice(0, 19);
  const stageName = stage.charAt(0).toUpperCase() + stage.slice(1);
  let body = `<div><strong>Message</strong>
    <div style="margin-top:4px;white-space:pre-wrap;line-height:1.55">${escapeHtml(c.message)}</div></div>`;
  body += `<div style="margin-top:14px"><strong>Time</strong>
    <div style="margin-top:4px;color:var(--muted);font-variant-numeric:tabular-nums">${escapeHtml(fullTime)}</div></div>`;
  if (c.extra && typeof c.extra === "object" && Object.keys(c.extra).length > 0) {
    body += `<div style="margin-top:14px"><strong>Details</strong>
      <div style="margin-top:8px">${renderExtra(c.extra)}</div></div>`;
  }
  showModalHtml(body, `${stageName} — checkpoint`);
}

function renderExtra(obj) {
  if (!obj || typeof obj !== "object") return "";
  let html = '<div style="display:grid;grid-template-columns:140px 1fr;gap:8px 14px;font-size:13px;line-height:1.55">';
  for (const [k, v] of Object.entries(obj)) {
    const label = k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    html += `<div style="color:var(--muted);font-weight:600">${escapeHtml(label)}</div>`;
    html += `<div>${formatValue(v)}</div>`;
  }
  html += "</div>";
  return html;
}

function formatValue(v) {
  if (v === null || v === undefined) return '<span style="color:var(--muted)">—</span>';
  if (Array.isArray(v)) {
    if (v.length === 0) return '<span style="color:var(--muted)">empty</span>';
    return '<ul style="margin:0;padding-left:16px">' +
      v.map((x) => `<li>${formatValue(x)}</li>`).join("") + "</ul>";
  }
  if (typeof v === "object") return renderExtra(v);
  return escapeHtml(String(v));
}

function showModalHtml(html, title) {
  document.querySelectorAll(".vo-modal-backdrop").forEach((el) => el.remove());
  const bd = document.createElement("div");
  bd.className = "vo-modal-backdrop";
  const m = document.createElement("div");
  m.className = "vo-modal";
  m.innerHTML = `<button class="vo-close">×</button><h3>${escapeHtml(title || "")}</h3>${html}`;
  bd.appendChild(m);
  document.body.appendChild(bd);
  const close = () => { bd.remove(); };
  m.querySelector(".vo-close").onclick = close;
  bd.onclick = (e) => { if (e.target === bd) close(); };
  const esc = (e) => { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } };
  document.addEventListener("keydown", esc);
}

$$(".sidebar nav a").forEach((a) => a.addEventListener("click", async () => {
  $$(".sidebar nav a").forEach((x) => x.classList.remove("active"));
  a.classList.add("active");
  $$(".panel").forEach((p) => p.classList.add("hidden"));
  const panel = $("#panel-" + a.dataset.section);
  if (!panel) return;
  panel.classList.remove("hidden");
  panel.classList.remove("entering");
  void panel.offsetHeight;
  panel.classList.add("entering");
  if (a.dataset.section === "mindmap") await renderMindMap();
  if (a.dataset.section === "decisions") await renderDecisions();
  if (a.dataset.section === "ask") { const i = $("#ask-input"); if (i) i.focus(); }
}));

async function renderDecisions() {
  const box = document.getElementById("decisions-list");
  if (!box) return;
  box.innerHTML = "<div class='muted'>Loading…</div>";
  let decisions;
  try { decisions = await api(`/api/ventures/${state.ventureId}/decisions`); }
  catch (e) { box.innerHTML = `<div style="color:#fca5a5;padding:16px">Failed: ${escapeHtml(e.message)}</div>`; return; }
  if (!decisions || decisions.length === 0) { box.innerHTML = "<div class='muted'>No decisions yet.</div>"; return; }
  box.innerHTML = "";
  for (const d of decisions) {
    const card = document.createElement("div");
    card.className = "decision-card";
    const time = (d.created_at || "").replace("T", " ").slice(0, 19);
    const badge = d.approval_status === "approved" ? '<span class="badge approved">approved</span>'
                : d.approval_status === "rejected" ? '<span class="badge rejected">rejected</span>' : "";
    card.innerHTML = `
      <div class="head"><div class="action">${escapeHtml(sentenceCase(d.action || "Decision"))}${badge}</div><div class="meta">${escapeHtml(time)}</div></div>
      <div><span class="badge stage">${escapeHtml(stageLabel(d.stage || ""))}</span><span class="badge model">${escapeHtml(d.model || "unknown")}</span>${d.agent ? `<span class="badge stage">${escapeHtml(AGENT_NAMES[d.agent] || sentenceCase(d.agent))}</span>` : ""}</div>
      <div class="rationale"><strong>Decision:</strong> ${decisionBadge(d.decision)}<br>${escapeHtml(humanize(d.rationale || ""))}</div>`;
    box.appendChild(card);
  }
}

// Cytoscape only accepts #rgb/#rrggbb/rgb()/rgba() — never 8-digit hex (that crashed the map).
function toRgba(color, alpha) {
  const c = String(color || "").trim();
  let m = c.match(/^#([0-9a-f]{3})$/i);
  if (m) return toRgba("#" + m[1].split("").map((x) => x + x).join(""), alpha);
  m = c.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i);
  if (m) return `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${alpha})`;
  m = c.match(/^rgba?\(([^,]+),([^,]+),([^,)]+)/i);
  if (m) return `rgba(${m[1].trim()},${m[2].trim()},${m[3].trim()},${alpha})`;
  return `rgba(99,102,241,${alpha})`;
}

async function renderMindMap() {
  const container = document.getElementById("cy");
  if (!container) return;
  if (typeof cytoscape === "undefined") { container.innerHTML = "<div style='padding:32px;color:var(--muted)'>Mind map lib failed to load.</div>"; return; }
  let data;
  try { data = await api(`/api/ventures/${state.ventureId}/mindmap`); }
  catch (e) { container.innerHTML = `<div style='padding:32px;color:#fca5a5'>Error: ${escapeHtml(e.message)}</div>`; return; }
  if (state.cy) { state.cy.destroy(); state.cy = null; }
  if (data.pending) {
    container.innerHTML = `<div style="padding:32px;color:var(--muted);line-height:1.6;max-width:520px">
      ${escapeHtml(data.message || "Business map not generated yet.")}
      ${data.can_generate ? '<div style="margin-top:14px"><button class="primary" id="gen-map">Generate business map</button></div>' : ""}
    </div>`;
    const gb = document.getElementById("gen-map");
    if (gb && !state.mapAutoTried) { state.mapAutoTried = true; setTimeout(() => gb.click(), 0); }
    if (gb) gb.onclick = async () => {
      gb.disabled = true; gb.textContent = "Generating…";
      try { await api(`/api/ventures/${state.ventureId}/mindmap/generate`, { method: "POST" }); await renderMindMap(); }
      catch (e) { toastError("Map failed: " + e.message); gb.disabled = false; gb.textContent = "Generate business map"; }
    };
    return;
  }
  container.innerHTML = "";
  const css = getComputedStyle(document.documentElement);
  const a1 = css.getPropertyValue("--accent").trim() || "#6366f1";
  const a2 = css.getPropertyValue("--accent-2").trim() || "#a855f7";
  const a3 = css.getPropertyValue("--accent-3").trim() || "#06b6d4";
  try { state.cy = cytoscape({
    container, elements: [...data.nodes, ...data.edges], wheelSensitivity: 0.25,
    style: [
      { selector: 'node[type="center"]', style: {
        "background-fill": "linear-gradient", "background-gradient-stop-colors": `${a1} ${a2} ${a3}`,
        "background-gradient-stop-positions": "0% 50% 100%", "background-gradient-direction": "to-bottom-right",
        label: "data(label)", color: "#fff", "font-weight": 800, "font-size": 19,
        "text-valign": "center", "text-halign": "center", "text-wrap": "wrap", "text-max-width": 170,
        width: 210, height: 210, padding: 20, "border-width": 3, "border-color": "rgba(255,255,255,.85)" }},
      { selector: 'node[type="aspect"]', style: {
        "background-fill": "linear-gradient", "background-gradient-stop-colors": `${toRgba(a2, 0.22)} ${toRgba(a3, 0.22)}`,
        "background-gradient-stop-positions": "0% 100%", "background-gradient-direction": "to-bottom-right",
        "border-width": 2, "border-color": a1, label: "data(label)", color: "#f5f5f7",
        "font-weight": 700, "font-size": 13, "text-valign": "center", "text-halign": "center",
        "text-wrap": "wrap", "text-max-width": 140, width: 170, height: 60, padding: 10 }},
      { selector: 'node[type="aspect"][status="strong"]', style: { "border-color": "#22c55e" }},
      { selector: 'node[type="aspect"][status="gap"]', style: { "border-color": "#f87171", "border-style": "dashed" }},
      { selector: 'node[type="leaf"]', style: {
        "background-color": "rgba(255,255,255,.055)", "border-width": 1, "border-color": "rgba(255,255,255,.14)",
        label: "data(label)", color: "#c8c8d8", "font-size": 11, "text-wrap": "wrap", "text-max-width": 130,
        "text-valign": "center", "text-halign": "center", width: 160, height: 62, padding: 8 }},
      { selector: "edge", style: { width: 1.5, "line-color": "rgba(160,160,220,.20)", "curve-style": "bezier" }},
    ],
    layout: { name: "concentric",
      concentric: (n) => n.data("type") === "center" ? 10 : (n.data("type") === "aspect" ? 5 : 1),
      levelWidth: () => 3, minNodeSpacing: 30, spacingFactor: 1.1, avoidOverlap: true,
      animate: false, padding: 40 },
  }); } catch (e) {
    container.innerHTML = `<div style='padding:32px;color:#fca5a5'>Mind map failed to draw: ${escapeHtml(e.message)}</div>`;
    return;
  }
  state.cy.nodes().style({ opacity: 0 });
  state.cy.nodes().forEach((n, i) => setTimeout(() => {
    if (state.cy) n.animate({ style: { opacity: 1 } }, { duration: 380, easing: "ease-out-cubic" });
  }, 80 + i * 25));
  setTimeout(() => { if (state.cy) state.cy.fit(undefined, 50); }, 1200);
  state.cy.on("tap", 'node[type="center"]', (e) => showModal(e.target.data("items") || [], "Actionable Steps"));
  const statusText = { strong: "Backed by evidence", partial: "Stated, not yet verified", gap: "Gap — needs work" };
  state.cy.on("tap", 'node[type="aspect"]', (e) => {
    const d = e.target.data();
    showModal([...(d.items || []), statusText[d.status] ? `Status: ${statusText[d.status]}` : ""].filter(Boolean), d.full_label || d.label);
  });
  state.cy.on("tap", 'node[type="leaf"]', (e) => {
    const parent = e.target.incomers("node")[0];
    showModal([e.target.data("full") || e.target.data("label")], parent ? (parent.data("full_label") || parent.data("label")) : "Detail");
  });
}

function showModal(items, title) {
  document.querySelectorAll(".vo-modal-backdrop").forEach((el) => el.remove());
  const bd = document.createElement("div");
  bd.className = "vo-modal-backdrop";
  const m = document.createElement("div");
  m.className = "vo-modal";
  m.innerHTML = `<button class="vo-close">×</button><h3>${escapeHtml(title || "Actionable Steps")}</h3>${items.length === 0 ? "<div class='muted'>Nothing yet.</div>" : `<ol>${items.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ol>`}`;
  bd.appendChild(m);
  document.body.appendChild(bd);
  const close = () => { bd.remove(); };
  m.querySelector(".vo-close").onclick = close;
  bd.onclick = (e) => { if (e.target === bd) close(); };
  const esc = (e) => { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } };
  document.addEventListener("keydown", esc);
}

function escapeHtml(s) { return String(s || "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function themeToColor(t) { return { blue:"#2563eb", cyan:"#0891b2", teal:"#0d9488", coral:"#f97316", amber:"#d97706", indigo:"#4f46e5" }[t] || "#4f46e5"; }
function hexToAccent2(hex) {
  try {
    const h = hex.replace("#", "");
    const r = parseInt(h.substring(0, 2), 16), g = parseInt(h.substring(2, 4), 16), b = parseInt(h.substring(4, 6), 16);
    const sh = (c, d) => Math.max(0, Math.min(255, Math.round(c + d)));
    return "rgb(" + sh(r, 30) + ", " + sh(g, -40) + ", " + sh(b, 40) + ")";
  } catch { return "#a855f7"; }
}

bootstrap();
