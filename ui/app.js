"use strict";
// MEMNOS Studio Workspace — Split #Channels & @Handles, Active Filtering, 5 Themes, Real-Time SSE.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ---- Bootstrap Auth & Theme ----
const THEME_KEY = "memnos_ui_theme_v2";
const BOOT_INFO = window.__MEMNOS_BOOT__ || null;
const DEFAULT_LOCAL_URL = (BOOT_INFO && BOOT_INFO.local_url) || "http://127.0.0.1:8900";
const DEFAULT_LOCAL_TOKEN = (BOOT_INFO && BOOT_INFO.local_token) || "";

function isLoopbackUrl(u) {
  const l = String(u || "").toLowerCase();
  return l.includes("://127.0.0.1") || l.includes("://localhost") || l.includes("://[::1]");
}

(function bootstrap() {
  const savedTheme = localStorage.getItem(THEME_KEY) || "aubergine";
  document.documentElement.setAttribute("data-theme", savedTheme);

  if (BOOT_INFO) {
    if (BOOT_INFO.token) localStorage.setItem("memnos_admin_token", BOOT_INFO.token);
    if (BOOT_INFO.url) localStorage.setItem("memnos_server_url", BOOT_INFO.url);
  }
  if (location.hash && location.hash.length > 1) {
    const hp = new URLSearchParams(location.hash.slice(1));
    if (hp.get("token")) localStorage.setItem("memnos_admin_token", hp.get("token"));
    if (hp.get("url")) localStorage.setItem("memnos_server_url", hp.get("url"));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();

const tok = () => localStorage.getItem("memnos_admin_token") || "";
function serverBase() {
  const saved = (localStorage.getItem("memnos_server_url") || "").trim().replace(/\/+$/, "");
  return saved || location.origin;
}

const API_TIMEOUT_MS = 30000;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c]));
const fmt = (t) => t ? new Date(t).toLocaleString() : "—";
const age = (t) => {
  if (!t) return "—";
  const s = Math.max(0, (Date.now() - new Date(t).getTime()) / 1000);
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
};
const fmtBytes = (b) => {
  if (b == null) return "scoped";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KB";
  if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + " MB";
  return (b / (1024 * 1024 * 1024)).toFixed(2) + " GB";
};
const avatarColor = (name) => {
  const palette = ["#6366f1", "#0ea5e9", "#10b981", "#8b5cf6", "#ec4899", "#f59e0b", "#14b8a6", "#f43f5e"];
  let h = 0;
  const s = String(name || "m");
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return palette[Math.abs(h) % palette.length];
};

// Lightweight rich text formatter (fenced code blocks, inline `code`, @handles, #channels)
function formatRichText(raw) {
  const blocks = [];
  let safe = String(raw ?? "").replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_, _lang, code) => {
    const idx = blocks.length;
    blocks.push(`<pre><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000BLOCK_${idx}\u0000`;
  });
  safe = esc(safe);
  safe = safe.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  safe = safe.replace(/(^|\s)(@[a-zA-Z0-9_.-]+|#[a-zA-Z0-9_.-]+)/g, '$1<span class="mention-hl">$2</span>');
  safe = safe.replace(/\u0000BLOCK_(\d+)\u0000/g, (_, idx) => blocks[+idx] || "");
  return safe;
}

async function rawFetch(method, fullPath, body) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), API_TIMEOUT_MS);
  let r;
  try {
    r = await fetch(serverBase() + fullPath, {
      method,
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tok() },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? "request timed out — server may be busy"
      : "cannot reach " + serverBase() + " — check server URL or start memnos");
  } finally {
    clearTimeout(timer);
  }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401) {
    disconnectToLogin("Token invalid or expired for " + serverBase());
    throw new Error("unauthorized");
  }
  if (!r.ok) throw new Error((j.error || ("HTTP " + r.status)) + (j.msg ? ": " + j.msg : ""));
  return j;
}

const wapi = (m, p, b) => rawFetch(m, "/workspace/api/" + p, b);
const api  = (m, p, b) => rawFetch(m, "/admin/api/" + p, b);
const uapi = (m, p, b) => rawFetch(m, p, b);

// ---- App State ----
let wsData = null;
let activeSection = "channels"; // "channels" | "handles" | "live" | "memories" | "telemetry" | "console"
let chFilterMode = "active";    // "active" | "history" | "all"
let hdFilterMode = "online";    // "online" | "all"
let selectedChannel = null;     // { namespace, channel }
let selectedHandle = null;      // { namespace, name }
let inspectorOpen = true;
let wmemType = "";
let wmemOffset = 0;
const WMEM_PAGE = 50;

let liveEvents = [];
let livePaused = false;
let liveNewCount = 0;
let liveFlashTimer = null;
let lastActivityId = 0;
let lastTopbarEvent = null;
let liveDeltaTimer = null;
let liveClockTimer = null;

let allPrincipals = [];
let callerPrincipalId = 0;
let allTokens = [];
let revealedTokenMap = {};

let wsSocket = null;
let wsReconnectTimer = null;
let wsReqSeq = 1;
const wsPending = new Map();
let sseSource = null;
let sseRefreshTimer = null;
let pollFallbackTimer = null;

// ---- Theme Switcher (Slack Aubergine default) ----
const themeSel = $("#theme-select");
themeSel.value = localStorage.getItem(THEME_KEY) || "aubergine";
themeSel.onchange = () => {
  document.documentElement.setAttribute("data-theme", themeSel.value);
  localStorage.setItem(THEME_KEY, themeSel.value);
};

// ---- Table Helpers ----
function cols(tbody) { return $$("th", tbody.closest("table")).length || 1; }
function showLoading(tbody) {
  const n = cols(tbody);
  tbody.innerHTML = `<tr class="state"><td colspan="${n}"><span class="spinner"></span> loading…</td></tr>`;
  return setTimeout(() => {
    const td = $("tr.state td", tbody);
    if (td) td.innerHTML = `<span class="spinner"></span> still loading…`;
  }, 3000);
}
function showError(tbody, msg, retry) {
  const n = cols(tbody);
  tbody.innerHTML = `<tr class="state"><td colspan="${n}"><span class="errbox">${esc(msg)} <button class="retry">retry</button></span></td></tr>`;
  $("button.retry", tbody).onclick = retry;
}
const emptyRow = (n, msg) => `<tr class="state"><td colspan="${n}" class="muted">${msg}</td></tr>`;
async function loadTable(tbody, fetcher, render, self) {
  const t = showLoading(tbody);
  try { render(await fetcher()); }
  catch (e) { if (e.message !== "unauthorized") showError(tbody, e.message, self); }
  finally { clearTimeout(t); }
}

// ---- Auth, Local vs Remote Onboarding, & Server Switcher ----
function setLoginMode(mode) {
  const isLocal = mode !== "remote";
  $$("#login-mode-tabs button").forEach(b => b.classList.toggle("active", (b.dataset.cmode === "local") === isLocal));
  $("#login-local-banner").hidden = !isLocal;
  $("#login-remote-banner").hidden = isLocal;
  const autoBox = $("#login-local-autodetect");
  if (autoBox) autoBox.hidden = !(isLocal && DEFAULT_LOCAL_TOKEN);
  const urlInp = $("#server-url");
  if (isLocal) {
    if (!urlInp.value || !isLoopbackUrl(urlInp.value)) urlInp.value = DEFAULT_LOCAL_URL;
    urlInp.placeholder = DEFAULT_LOCAL_URL;
  } else {
    if (isLoopbackUrl(urlInp.value)) urlInp.value = "";
    urlInp.placeholder = "https://memnos.your-org.com";
  }
}

$$("#login-mode-tabs button").forEach(btn => {
  btn.onclick = () => setLoginMode(btn.dataset.cmode);
});
const autoLocalBtn = $("#btn-connect-local-auto");
if (autoLocalBtn) {
  autoLocalBtn.onclick = () => connectWorkspace(DEFAULT_LOCAL_TOKEN, DEFAULT_LOCAL_URL);
}

function disconnectToLogin(errMsg = "") {
  stopRealtime();
  localStorage.removeItem("memnos_admin_token");
  $("#app").hidden = true;
  $("#server-modal").hidden = true;
  $("#login").hidden = false;
  const cur = serverBase();
  $("#server-url").value = cur;
  setLoginMode(isLoopbackUrl(cur) ? "local" : "remote");
  $("#loginerr").textContent = errMsg;
}

async function connectWorkspace(tokenVal, urlVal) {
  if (urlVal) localStorage.setItem("memnos_server_url", urlVal.replace(/\/+$/, ""));
  if (tokenVal) localStorage.setItem("memnos_admin_token", tokenVal);
  $("#loginerr").textContent = "";
  try {
    await refreshOverview(true);
    $("#login").hidden = true;
    $("#server-modal").hidden = true;
    $("#app").hidden = false;
    loadInitialActivity();
    startRealtime();
  } catch (e) {
    localStorage.removeItem("memnos_admin_token");
    $("#app").hidden = true;
    $("#login").hidden = false;
    setLoginMode(isLoopbackUrl(serverBase()) ? "local" : "remote");
    $("#loginerr").textContent = e.message;
  }
}

$("#signin").onclick = () => {
  const u = ($("#server-url").value || "").trim() || DEFAULT_LOCAL_URL;
  let t = ($("#token").value || "").trim();
  if (!t && isLoopbackUrl(u) && DEFAULT_LOCAL_TOKEN) t = DEFAULT_LOCAL_TOKEN;
  if (!t) { $("#loginerr").textContent = "Paste an admin or role-scoped token (mnk_...)"; return; }
  connectWorkspace(t, u);
};
$("#token").addEventListener("keydown", e => { if (e.key === "Enter") $("#signin").click(); });
$("#logout").onclick = () => disconnectToLogin();

// Server Connection Modal (Local vs Remote while inside the workspace)
function setModalMode(mode) {
  const isLocal = mode !== "remote";
  $$("#modal-mode-tabs button").forEach(b => b.classList.toggle("active", (b.dataset.mmode === "local") === isLocal));
  $("#modal-local-guide").hidden = !isLocal;
  $("#modal-remote-guide").hidden = isLocal;
  const u = $("#modal-url");
  if (isLocal) {
    if (!u.value || !isLoopbackUrl(u.value)) u.value = DEFAULT_LOCAL_URL;
    u.placeholder = DEFAULT_LOCAL_URL;
  } else {
    if (isLoopbackUrl(u.value)) u.value = "";
    u.placeholder = "https://memnos.your-org.com";
  }
  $("#modal-use-local").hidden = !DEFAULT_LOCAL_TOKEN;
}

$$("#modal-mode-tabs button").forEach(btn => {
  btn.onclick = () => setModalMode(btn.dataset.mmode);
});

$("#switch-server").onclick = () => {
  const cur = serverBase();
  $("#modal-url").value = cur;
  $("#modal-token").value = "";
  $("#modal-err").textContent = "";
  setModalMode(isLoopbackUrl(cur) ? "local" : "remote");
  $("#server-modal").hidden = false;
};
$("#modal-close").onclick = () => { $("#server-modal").hidden = true; };
$("#modal-cancel").onclick = () => { $("#server-modal").hidden = true; };
$("#modal-use-local").onclick = async () => {
  if (!DEFAULT_LOCAL_TOKEN) return;
  await connectWorkspace(DEFAULT_LOCAL_TOKEN, DEFAULT_LOCAL_URL);
};
$("#modal-connect").onclick = async () => {
  const nextUrl = ($("#modal-url").value || "").trim().replace(/\/+$/, "");
  if (!nextUrl) { $("#modal-err").textContent = "Enter a MEMNOS server URL."; return; }
  let nextTok = ($("#modal-token").value || "").trim();
  if (!nextTok) {
    nextTok = (isLoopbackUrl(nextUrl) && DEFAULT_LOCAL_TOKEN) ? DEFAULT_LOCAL_TOKEN : tok();
  }
  if (!nextTok) { $("#modal-err").textContent = "Enter an access token (mnk_...) for this server."; return; }
  $("#modal-err").textContent = "";
  try {
    localStorage.setItem("memnos_server_url", nextUrl);
    localStorage.setItem("memnos_admin_token", nextTok);
    await refreshOverview(true);
    $("#server-modal").hidden = true;
    loadInitialActivity();
    startRealtime();
  } catch (e) {
    $("#modal-err").textContent = e.message;
  }
};

$("#live-op-pill").onclick = () => switchSection("live");

$("#refresh-now").onclick = () => {
  refreshOverview(false);
  if (activeSection === "channels" && selectedChannel) loadChannelMessages(false);
  if (activeSection === "handles" && selectedHandle) loadHandleMessages(false);
  if (activeSection === "live") loadInitialActivity();
};

// ---- Real-Time WebSocket (Default) & Live Activity Stream ----
const actorLiveMap = new Map(); // handle -> { ts, ms, op, detail }

function recordActorLive(ev) {
  if (!ev || !ev.actor || !ev.ts) return;
  const clean = String(ev.actor).replace(/^@/, "");
  if (!clean) return;
  const ms = new Date(ev.ts).getTime();
  if (!Number.isFinite(ms)) return;
  const prev = actorLiveMap.get(clean);
  if (!prev || ms >= prev.ms) {
    actorLiveMap.set(clean, { ts: ev.ts, ms, op: ev.op || "", detail: ev.detail || "" });
  }
}

function fmtAgoCompact(iso) {
  if (!iso) return "new";
  const sec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 5) return "just now";
  return age(iso) + " ago";
}

function setLiveStatus(online, label) {
  const badge = $("#sse-badge");
  if (!badge) return;
  // Two-way WebSocket is the product default — hide badge when healthy, show only if offline/reconnecting
  if (online) {
    badge.hidden = true;
  } else {
    badge.hidden = false;
    badge.className = "status-chip offline";
    $("#sse-text").textContent = label || "RECONNECTING";
  }
}

function renderTopbarPillText() {
  const pill = $("#live-op-pill");
  const txt = $("#live-op-text");
  if (!pill || !txt || !lastTopbarEvent) return;
  const ev = lastTopbarEvent;
  const actor = ev.actor ? ("@" + String(ev.actor).replace(/^@/, "")) : "@client";
  const agoSec = ev.ts ? Math.max(0, Math.floor((Date.now() - new Date(ev.ts).getTime()) / 1000)) : 0;
  const agoLbl = agoSec < 3 ? "just now" : (age(ev.ts) + " ago");
  const rawDet = String(ev.detail || "").trim();
  const shortDet = rawDet ? (" · " + (rawDet.length > 26 ? rawDet.slice(0, 26) + "…" : rawDet)) : "";
  txt.textContent = `⚡ ${actor} · ${ev.op}${shortDet} · ${agoLbl}`;
  pill.title = `Live: ${actor} · ${ev.op} (${ev.namespace || "*"}) · ${rawDet || "—"} · ${ev.latency_ms ?? 0}ms — click to open Live stream`;
}

function updateLiveTopbarPill(ev, animate = true) {
  if (!ev) return;
  lastTopbarEvent = ev;
  renderTopbarPillText();
  const pill = $("#live-op-pill");
  if (animate && pill) {
    pill.classList.add("flash");
    if (liveFlashTimer) clearTimeout(liveFlashTimer);
    liveFlashTimer = setTimeout(() => pill.classList.remove("flash"), 1400);
  }
}

function eventDedupKey(ev) {
  return `${ev.op || ""}|${ev.namespace || ""}|${ev.actor || ""}|${(ev.ts || "").slice(0, 19)}`;
}

async function loadInitialActivity() {
  try {
    const { events } = await wapi("GET", "activity?limit=80");
    if (Array.isArray(events)) {
      liveEvents = events;
      for (const ev of events) recordActorLive(ev);
      if (events.length > 0) {
        lastActivityId = Math.max(lastActivityId, Number(events[0].id || 0));
        updateLiveTopbarPill(events[0], false);
      }
      renderLiveTable();
      if (wsData) {
        renderChannelList();
        renderHandleList();
      }
    }
  } catch (_) {}
}

async function pollActivityDelta() {
  if (!tok()) return;
  try {
    const qs = lastActivityId > 0 ? `activity?limit=25&after_id=${lastActivityId}` : "activity?limit=15";
    const { events } = await wapi("GET", qs);
    if (!Array.isArray(events) || !events.length) return;
    const maxId = Math.max(...events.map(e => Number(e.id || 0)));
    if (lastActivityId === 0) {
      lastActivityId = maxId;
      if (!lastTopbarEvent) updateLiveTopbarPill(events[0], false);
      return;
    }
    if (maxId <= lastActivityId) return;
    lastActivityId = maxId;
    const seen = new Set(liveEvents.slice(0, 40).map(eventDedupKey));
    const chronological = [...events].reverse();
    for (const ev of chronological) {
      const k = eventDedupKey(ev);
      if (!seen.has(k)) {
        seen.add(k);
        handleLiveActivity(ev);
      }
    }
  } catch (_) {}
}

function handleLiveActivity(ev) {
  if (ev.id && Number(ev.id) > lastActivityId) {
    lastActivityId = Number(ev.id);
  }
  recordActorLive(ev);
  updateLiveTopbarPill(ev, true);
  if (wsData) {
    if (activeSection === "channels") renderChannelList();
    if (activeSection === "handles") {
      renderHandleList();
      if (selectedHandle && String(ev.actor || "").replace(/^@/, "") === selectedHandle.name) {
        loadHandleMessages(false);
      }
    }
  }
  if (activeSection !== "live") {
    liveNewCount += 1;
    const badge = $("#rail-live-count");
    if (badge) {
      badge.hidden = false;
      badge.textContent = liveNewCount > 99 ? "99+" : String(liveNewCount);
    }
  }
  if (!livePaused) {
    const k = eventDedupKey(ev);
    if (!liveEvents.slice(0, 8).some(x => eventDedupKey(x) === k)) {
      ev._fresh = true;
      liveEvents.unshift(ev);
      if (liveEvents.length > 250) liveEvents.length = 250;
      if (activeSection === "live") renderLiveTable();
    }
  }
  const op = String(ev.op || "");
  if (
    op.includes("remember") ||
    op.startsWith("memory/") ||
    op === "consolidate" ||
    op.startsWith("session/") ||
    op.startsWith("channel/") ||
    op.startsWith("inbox/")
  ) {
    scheduleRealtimeRefresh();
    if (activeSection === "memories" && wmemOffset === 0 && (op.includes("remember") || op.startsWith("memory/"))) {
      loadWorkspaceMemories();
    }
  }
}

function renderLiveTable() {
  const tbody = $("#live-table tbody");
  if (!tbody) return;
  const q = ($("#live-filter")?.value || "").trim().toLowerCase();
  const rows = liveEvents.filter(e => {
    if (!q) return true;
    const hay = `${e.actor || ""} ${e.principal || ""} ${e.op || ""} ${e.namespace || ""} ${e.detail || ""}`.toLowerCase();
    return hay.includes(q);
  });
  const sum = $("#live-summary");
  if (sum) sum.textContent = `${rows.length} event${rows.length === 1 ? "" : "s"}${livePaused ? " · PAUSED" : " · streaming live"}`;
  tbody.innerHTML = rows.map(e => {
    const ok = e.ok !== false && (e.status == null || e.status < 400);
    const actor = e.actor || e.principal || "—";
    const rowCls = e._fresh ? ' class="live-row-new"' : "";
    e._fresh = false;
    return `<tr${rowCls}>
      <td title="${esc(e.ts || "")}">${esc(age(e.ts))} ago</td>
      <td><code>@${esc(String(actor).replace(/^@/, ""))}</code></td>
      <td><b><code>${esc(e.op || "—")}</code></b></td>
      <td><code>${esc(e.namespace || "—")}</code></td>
      <td class="muted" style="word-break:break-word">${esc(e.detail || "—")}</td>
      <td>${e.latency_ms != null ? `${e.latency_ms} ms` : "—"}</td>
      <td><span class="pill ${ok ? "ok" : "no"}">${e.status ?? (ok ? 200 : "ERR")}</span></td>
    </tr>`;
  }).join("") || emptyRow(7, "Waiting for live memnos calls (recall, remember, channel/publish, MCP)…");
}

function scheduleRealtimeRefresh() {
  if (sseRefreshTimer) return;
  sseRefreshTimer = setTimeout(async () => {
    sseRefreshTimer = null;
    try {
      await refreshOverview(false);
      if (activeSection === "channels" && selectedChannel) await loadChannelMessages(false);
      if (activeSection === "handles" && selectedHandle) await loadHandleMessages(false);
    } catch (_) {}
  }, 220);
}

function wsServerBase() {
  const b = serverBase();
  if (b.startsWith("https://")) return "wss://" + b.slice(8);
  if (b.startsWith("http://")) return "ws://" + b.slice(7);
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return proto + location.host;
}

function wsCall(type, payload) {
  return new Promise((resolve, reject) => {
    if (!wsSocket || wsSocket.readyState !== WebSocket.OPEN) {
      return reject(new Error("ws_not_open"));
    }
    const req_id = wsReqSeq++;
    const timer = setTimeout(() => {
      wsPending.delete(req_id);
      reject(new Error("ws_timeout"));
    }, 6000);
    wsPending.set(req_id, { resolve, reject, timer });
    wsSocket.send(JSON.stringify({ type, req_id, ...payload }));
  });
}

function startSseFallback() {
  if (sseSource) return;
  try {
    sseSource = new EventSource(serverBase() + "/workspace/events?token=" + encodeURIComponent(tok()));
    sseSource.onopen = () => {
      if (!wsSocket || wsSocket.readyState !== WebSocket.OPEN) setLiveStatus(true, "SSE LIVE");
    };
    sseSource.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data || "{}");
        if (d.type === "refresh") scheduleRealtimeRefresh();
        else if (d.type === "activity") handleLiveActivity(d);
      } catch (_) {}
    };
    sseSource.onerror = () => {
      if (!wsSocket || wsSocket.readyState !== WebSocket.OPEN) setLiveStatus(false, "SYNCING");
    };
  } catch (_) {}
}

function connectWebSocket() {
  if (!tok()) return;
  if (wsSocket) {
    try { wsSocket.onclose = null; wsSocket.close(); } catch (_) {}
    wsSocket = null;
  }
  try {
    const wsUrl = wsServerBase() + "/workspace/ws?token=" + encodeURIComponent(tok());
    wsSocket = new WebSocket(wsUrl);
    wsSocket.onopen = () => {
      setLiveStatus(true, "WS LIVE");
      if (sseSource) { sseSource.close(); sseSource = null; }
    };
    wsSocket.onmessage = (ev) => {
      setLiveStatus(true, "WS LIVE");
      try {
        const d = JSON.parse(ev.data || "{}");
        if (d.type === "ack" && d.req_id != null && wsPending.has(d.req_id)) {
          const p = wsPending.get(d.req_id);
          wsPending.delete(d.req_id);
          clearTimeout(p.timer);
          if (d.ok) p.resolve(d.result);
          else p.reject(new Error(d.error || "WebSocket command failed"));
          return;
        }
        if (d.type === "refresh") scheduleRealtimeRefresh();
        else if (d.type === "activity") handleLiveActivity(d);
      } catch (_) {}
    };
    wsSocket.onerror = () => {
      startSseFallback();
    };
    wsSocket.onclose = () => {
      wsSocket = null;
      for (const [, p] of wsPending) {
        clearTimeout(p.timer);
        p.reject(new Error("ws_closed"));
      }
      wsPending.clear();
      if (!tok()) return;
      startSseFallback();
      if (!wsReconnectTimer) {
        wsReconnectTimer = setTimeout(() => {
          wsReconnectTimer = null;
          if (tok()) connectWebSocket();
        }, 1800);
      }
    };
  } catch (_) {
    startSseFallback();
  }
}

function startRealtime() {
  stopRealtime();
  // Default: Persistent two-way WebSocket (/workspace/ws), with automatic SSE fallback
  connectWebSocket();
  liveDeltaTimer = setInterval(() => {
    pollActivityDelta();
    if (wsSocket && wsSocket.readyState === WebSocket.OPEN) {
      try { wsSocket.send(JSON.stringify({ type: "ping" })); } catch (_) {}
    }
  }, 3000);
  liveClockTimer = setInterval(() => {
    renderTopbarPillText();
    if (activeSection === "live" && !livePaused && liveEvents.length) renderLiveTable();
    if (activeSection === "channels" && wsData) renderChannelList();
    if (activeSection === "handles" && wsData) renderHandleList();
  }, 2000);
  pollFallbackTimer = setInterval(() => {
    refreshOverview(false).catch(() => {});
    if (activeSection === "channels" && selectedChannel) loadChannelMessages(false).catch(() => {});
    if (activeSection === "handles" && selectedHandle) loadHandleMessages(false).catch(() => {});
  }, 5000);
}

function stopRealtime() {
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  if (wsSocket) {
    try { wsSocket.onclose = null; wsSocket.close(); } catch (_) {}
    wsSocket = null;
  }
  if (sseSource) { sseSource.close(); sseSource = null; }
  if (liveDeltaTimer) { clearInterval(liveDeltaTimer); liveDeltaTimer = null; }
  if (liveClockTimer) { clearInterval(liveClockTimer); liveClockTimer = null; }
  if (pollFallbackTimer) { clearInterval(pollFallbackTimer); pollFallbackTimer = null; }
}

// ---- Primary Far-Left Rail Navigation ----
function switchSection(sec) {
  if (sec === "console" && wsData && !wsData.is_admin) {
    sec = "channels";
  }
  activeSection = sec;
  $$(".rail-btn").forEach(b => b.classList.toggle("active", b.dataset.section === sec));
  ["channels", "handles", "live", "memories", "constraints", "telemetry", "console"].forEach(k => {
    const el = $("#sec-" + k);
    if (el) el.hidden = (k !== sec);
  });

  if (sec === "channels") {
    renderChannelList();
    if (selectedChannel) { renderChannelHeaderAndInspector(); loadChannelMessages(false); }
  } else if (sec === "handles") {
    renderHandleList();
    if (selectedHandle) { renderHandleHero(); loadHandleMessages(false); }
  } else if (sec === "live") {
    liveNewCount = 0;
    const badge = $("#rail-live-count");
    if (badge) badge.hidden = true;
    renderLiveTable();
  } else if (sec === "memories") {
    wmemOffset = 0;
    loadWorkspaceMemories();
  } else if (sec === "constraints") {
    loadWorkspaceConstraints();
  } else if (sec === "telemetry") {
    renderTelemetry();
  } else if (sec === "console") {
    const activeTab = $("#console-tabs button.active")?.dataset.ctab || "access";
    switchConsoleTab(activeTab);
  }
}

$$(".rail-btn").forEach(b => {
  b.onclick = () => switchSection(b.dataset.section);
});

if ($("#live-filter")) $("#live-filter").oninput = renderLiveTable;
if ($("#live-pause-btn")) {
  $("#live-pause-btn").onclick = () => {
    livePaused = !livePaused;
    $("#live-pause-btn").textContent = livePaused ? "Resume Stream" : "Pause Stream";
    renderLiveTable();
  };
}
if ($("#live-clear-btn")) {
  $("#live-clear-btn").onclick = () => {
    liveEvents = [];
    renderLiveTable();
  };
}

// ---- Harness Identity Helpers ----
function normalizeHarness(raw, meta) {
  let h = String(raw || "").trim().toLowerCase();
  if (!h && meta && typeof meta.display_label === "string") {
    const dl = meta.display_label.toLowerCase();
    if (dl.includes("claude")) h = "claude-code";
    else if (dl.includes("antigravity")) h = "antigravity";
    else if (dl.includes("cursor")) h = "cursor";
    else if (dl.includes("codex")) h = "codex";
    else if (dl.includes("gemini")) h = "gemini-cli";
  }
  if (!h) return null;
  if (h.includes("claude")) return { key: "claude-code", label: "Claude Code", cls: "claude" };
  if (h.includes("antigravity") || h === "agy") return { key: "antigravity", label: "Antigravity", cls: "agy" };
  if (h.includes("cursor")) return { key: "cursor", label: "Cursor", cls: "cursor" };
  if (h.includes("codex")) return { key: "codex", label: "Codex", cls: "codex" };
  if (h.includes("gemini")) return { key: "gemini-cli", label: "Gemini CLI", cls: "gemini" };
  return { key: h, label: raw, cls: "mcp" };
}

function harnessBadgeForSession(s) {
  const info = normalizeHarness(s?.harness, s?.metadata);
  if (!info) return "";
  return `<span class="harness-pill ${info.cls}" title="Attached harness: ${esc(info.label)}">${esc(info.label)}</span>`;
}

function harnessBadgeByName(handleName) {
  const clean = String(handleName || "").replace(/^@/, "");
  const s = (wsData?.sessions || []).find(x => x.name === clean && x.alive)
    || (wsData?.sessions || []).find(x => x.name === clean);
  return s ? harnessBadgeForSession(s) : "";
}

// ---- Active / Alive Helpers ----
function aliveSessionSet() {
  const s = new Set();
  for (const x of (wsData?.sessions || [])) {
    if (x.alive) s.add(x.namespace + "::" + x.name);
  }
  return s;
}

function liveSubscribersFor(ch) {
  if (Array.isArray(ch.live_subscribers)) return ch.live_subscribers;
  const aliveNames = new Set((wsData?.sessions || []).filter(x => x.alive).map(x => x.name));
  return (ch.subscribers || []).filter(h => aliveNames.has(h));
}

function isChannelActive(ch) {
  if (!ch.channel || !ch.channel.startsWith("#")) return false;
  const nss = ch.namespaces || (ch.namespace ? [ch.namespace] : []);
  if (nss.length > 0 && nss.every(n => String(n).startsWith("test:"))) return false;
  const liveSubs = liveSubscribersFor(ch);
  if (liveSubs.length > 0) return true;
  if ((ch.open_tasks || 0) > 0) return true;
  if (ch.last_message_at) {
    const hrs = (Date.now() - new Date(ch.last_message_at).getTime()) / 3600000;
    if (hrs <= 48) return true;
  }
  return false;
}

function canWriteNs(ns) {
  const found = (wsData?.namespaces || []).find(n => n.name === ns);
  return found ? !!found.can_write : !!wsData?.is_admin;
}

// ---- Overview Refresh ----
async function refreshOverview(pickDefaults = false) {
  const nsSel = $("#ws-ns-filter").value || "*";
  const data = await wapi("GET", "overview?namespace=" + encodeURIComponent(nsSel));
  // Strictly ensure channels only carry `#...`
  data.channels = (data.channels || []).filter(c => String(c.channel || "").startsWith("#"));
  wsData = data;

  // Topbar pills & Admin-only Settings visibility
  $("#switch-server").textContent = serverBase().replace(/^https?:\/\//, "");
  const pName = data.principal?.name || "user";
  $("#who-badge").textContent = `@${pName} · ${data.is_admin ? "ADMIN" : "SCOPED"}`;
  $$(".admin-only").forEach(el => { el.hidden = !data.is_admin; });
  if (!data.is_admin && activeSection === "console") {
    switchSection("channels");
  }
  ["channels", "handles", "live", "memories", "constraints", "telemetry", "console"].forEach(k => {
    const el = $("#sec-" + k);
    if (el) el.hidden = (k !== activeSection);
  });

  const v = data.vitals || {};
  const memStr = v.footprint_mb != null ? `${v.footprint_mb} MB` : (v.rss_mb != null ? `${v.rss_mb} MB` : "—");
  const dbStr = v.db_size_bytes != null ? fmtBytes(v.db_size_bytes) : "scoped";
  $("#vitals-strip").innerHTML =
    `<span>RAM <b>${esc(memStr)}</b></span>` +
    `<span>DB <b>${esc(dbStr)}</b></span>` +
    `<span>Facts <b>${(v.total_facts || 0).toLocaleString()}</b></span>` +
    `<span>Turns <b>${(v.total_turns || 0).toLocaleString()}</b></span>`;

  // Smart Namespace Selector (prioritize namespaces that actually have data/sessions/channels, exclude test:* noise at bottom)
  const prevNs = $("#ws-ns-filter").value || "*";
  const allNs = (data.namespaces || []).filter(n => !n.name.startsWith("test:"));
  const activeNs = allNs.filter(n => (n.facts || 0) > 0 || (n.turns || 0) > 0
    || (data.sessions || []).some(s => s.namespace === n.name && s.alive)
    || (data.channels || []).some(c => c.namespace === n.name && (c.message_count || 0) > 0));
  const opts = [`<option value="*">All namespaces (${activeNs.length} active)</option>`]
    .concat((activeNs.length ? activeNs : allNs).map(n =>
      `<option value="${esc(n.name)}">${esc(n.name)} · ${(n.facts || 0).toLocaleString()} facts</option>`
    ));
  $("#ws-ns-filter").innerHTML = opts.join("");
  if ([...$("#ws-ns-filter").options].some(o => o.value === prevNs)) {
    $("#ws-ns-filter").value = prevNs;
  }

  // Sync Constraints namespace selector
  const csSel = $("#cs-ns-select");
  if (csSel) {
    const prevCs = csSel.value || "*";
    csSel.innerHTML = `<option value="*">All authorized namespaces (*)</option>`
      + allNs.map(n => `<option value="${esc(n.name)}">${esc(n.name)} (+ inherited)</option>`).join("");
    if ([...csSel.options].some(o => o.value === prevCs)) csSel.value = prevCs;
  }

  // Sync Harness filter dropdown in @Handles
  const hfSel = $("#hd-harness-filter");
  if (hfSel) {
    const prevHf = hfSel.value || "";
    const seenH = new Map();
    for (const s of (data.sessions || [])) {
      const info = normalizeHarness(s.harness, s.metadata);
      if (info && !seenH.has(info.key)) seenH.set(info.key, info.label);
    }
    hfSel.innerHTML = `<option value="">All harnesses</option>`
      + [...seenH.entries()].map(([k, lbl]) => `<option value="${esc(k)}">${esc(lbl)}</option>`).join("");
    if ([...hfSel.options].some(o => o.value === prevHf)) hfSel.value = prevHf;
  }

  // Rail badges
  const activeChs = (data.channels || []).filter(isChannelActive);
  const onlineHds = (data.sessions || []).filter(s => s.alive && !s.namespace.startsWith("test:"));
  $("#rail-ch-count").textContent = activeChs.length;
  $("#rail-hd-count").textContent = onlineHds.length;
  if ($("#rail-cs-count")) $("#rail-cs-count").textContent = v.total_constraints ?? 0;

  // Pick sensible initial selections
  if (pickDefaults || !selectedChannel) {
    // Prefer a channel that has BOTH messages and live subscribers, else first active channel, else first channel
    const bestCh = activeChs.find(c => (c.message_count || 0) > 0 && liveSubscribersFor(c).length > 0)
      || activeChs[0]
      || (data.channels || [])[0];
    if (bestCh) selectedChannel = { namespace: bestCh.namespace, channel: bestCh.channel };
  }
  if (pickDefaults || !selectedHandle) {
    const bestHd = onlineHds[0] || (data.sessions || [])[0];
    if (bestHd) selectedHandle = { namespace: bestHd.namespace, name: bestHd.name };
  }

  renderChannelList();
  renderHandleList();
  if (activeSection === "channels" && selectedChannel) {
    renderChannelHeaderAndInspector();
    if (pickDefaults) await loadChannelMessages(true);
  }
  if (activeSection === "handles" && selectedHandle) {
    renderHandleHero();
    if (pickDefaults) await loadHandleMessages(true);
  }
  if (activeSection === "telemetry") renderTelemetry();
}

$("#ws-ns-filter").onchange = () => refreshOverview(true);

// ============================================================================
// SECTION 1: CHANNELS (#) — Active Filtering, Search, Stream & Management
// ============================================================================

$$("#ch-mode-tabs button").forEach(btn => {
  btn.onclick = () => {
    chFilterMode = btn.dataset.mode;
    $$("#ch-mode-tabs button").forEach(x => x.classList.toggle("active", x === btn));
    renderChannelList();
  };
});
$("#ch-search").oninput = renderChannelList;

function filteredChannels() {
  const all = wsData?.channels || [];
  const q = ($("#ch-search").value || "").trim().toLowerCase();
  return all.filter(c => {
    if (chFilterMode === "active" && !isChannelActive(c)) return false;
    if (chFilterMode === "history" && (c.message_count || 0) === 0) return false;
    if (q) {
      const hay = `${c.channel} ${c.namespace} ${c.description || ""} ${(c.subscribers || []).join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderChannelList() {
  if (!wsData) return;
  const all = wsData.channels || [];
  const actCnt = all.filter(isChannelActive).length;
  const histCnt = all.filter(c => (c.message_count || 0) > 0).length;
  const tabs = $$("#ch-mode-tabs button");
  if (tabs[0]) tabs[0].textContent = `Active (${actCnt})`;
  if (tabs[1]) tabs[1].textContent = `With Msgs (${histCnt})`;
  if (tabs[2]) tabs[2].textContent = `All (${all.length})`;

  const list = filteredChannels();
  $("#ch-pane-sub").textContent = `${list.length} shown · ${actCnt} active`;
  const box = $("#channel-list");

  if (!list.length) {
    box.innerHTML = `<div class="empty-state" style="padding:28px 12px">
      <div class="muted">No channels match this filter.</div>
      ${chFilterMode !== "all" ? `<button class="ghost sm" id="ch-show-all-btn" style="margin-top:8px">Show all (${all.length})</button>` : ""}
    </div>`;
    const btn = $("#ch-show-all-btn");
    if (btn) btn.onclick = () => { $$("#ch-mode-tabs button")[2].click(); };
    return;
  }

  // Flat Slack-style list of unique #channels across the server (sorted by freshest activity)
  const enriched = list.map(c => {
    let bestTs = c.last_active_at || c.last_message_at || null;
    let bestMs = bestTs ? new Date(bestTs).getTime() : 0;
    let liveHandle = c.last_active_handle || "";
    let liveOp = c.last_active_op || "";
    for (const sub of (c.subscribers || [])) {
      const a = actorLiveMap.get(sub);
      if (a && a.ms > bestMs) {
        bestMs = a.ms;
        bestTs = a.ts;
        liveHandle = sub;
        liveOp = a.op;
      }
    }
    const msgMs = c.last_message_at ? new Date(c.last_message_at).getTime() : 0;
    const isSubOpNewer = bestMs > msgMs && liveHandle;
    return { c, bestTs, bestMs, liveHandle, liveOp, isSubOpNewer };
  }).sort((a, b) => b.bestMs - a.bestMs);

  let html = "";
  for (const { c, bestTs, liveHandle, liveOp, isSubOpNewer } of enriched) {
    const isSel = selectedChannel && selectedChannel.channel === c.channel;
    const liveSubs = liveSubscribersFor(c);
    const tasks = c.open_tasks || 0;
    const rightTime = isSubOpNewer
      ? `<span title="Latest memnos op on @${esc(liveHandle)}: ${esc(liveOp || "active")}" style="color:var(--ok);font-weight:600">⚡ @${esc(liveHandle)} · ${esc(fmtAgoCompact(bestTs))}</span>`
      : `<span>${c.last_message_at ? esc(fmtAgoCompact(c.last_message_at)) : (bestTs ? esc(fmtAgoCompact(bestTs)) : "new")}</span>`;
    html += `<button class="list-card ${isSel ? "active" : ""}" data-ns="${esc(c.namespace)}" data-ch="${esc(c.channel)}">
      <div class="list-card-top">
        <span class="list-card-name">${esc(c.channel)}</span>
        ${tasks > 0
          ? `<span class="tiny-pill warn">${tasks} task${tasks > 1 ? "s" : ""}</span>`
          : (liveSubs.length > 0 ? `<span class="tiny-pill live">● ${liveSubs.length} live</span>` : `<span class="tiny-pill">${c.message_count || 0} msgs</span>`)}
      </div>
      <div class="list-card-sub">
        <span>${c.message_count || 0} msgs · ${(c.subscribers || []).length} subs</span>
        ${rightTime}
      </div>
    </button>`;
  }
  box.innerHTML = html;

  $$("#channel-list .list-card").forEach(card => {
    card.onclick = async () => {
      selectedChannel = { namespace: card.dataset.ns, channel: card.dataset.ch };
      renderChannelList();
      renderChannelHeaderAndInspector();
      await loadChannelMessages(true);
    };
  });
}

// Create Channel
$("#btn-new-channel").onclick = () => {
  const box = $("#new-channel-box");
  box.hidden = !box.hidden;
  if (!box.hidden) {
    const curNs = $("#ws-ns-filter").value;
    $("#new-ch-ns").value = (curNs && curNs !== "*") ? curNs : (selectedChannel?.namespace || wsData?.namespaces?.[0]?.name || "");
    $("#new-ch-name").focus();
  }
};
$("#new-ch-cancel").onclick = () => { $("#new-channel-box").hidden = true; };
$("#new-ch-save").onclick = async () => {
  const ns = $("#new-ch-ns").value.trim();
  const rawName = $("#new-ch-name").value.trim();
  if (!ns || !rawName) return;
  const ch = rawName.startsWith("#") ? rawName : "#" + rawName;
  try {
    await wapi("POST", "channel/create", { namespace: ns, channel: ch, description: $("#new-ch-desc").value.trim() });
    $("#new-ch-name").value = "";
    $("#new-ch-desc").value = "";
    $("#new-channel-box").hidden = true;
    selectedChannel = { namespace: ns, channel: ch };
    if (chFilterMode === "active") chFilterMode = "all";
    await refreshOverview(false);
    renderChannelHeaderAndInspector();
    await loadChannelMessages(true);
  } catch (e) { alert(e.message); }
};

$("#toggle-ch-inspector").onclick = () => {
  inspectorOpen = !inspectorOpen;
  $("#ch-inspector").hidden = !inspectorOpen;
};

$("#delete-channel-btn").onclick = async () => {
  if (!selectedChannel) return;
  const { namespace, channel } = selectedChannel;
  if (!confirm(`Delete channel "${channel}" from namespace "${namespace}"?\n\nThis removes the channel, all handle subscriptions on ${channel}, and its message history.`)) return;
  try {
    await wapi("DELETE", `channel?namespace=${encodeURIComponent(namespace)}&channel=${encodeURIComponent(channel)}`);
    selectedChannel = null;
    await refreshOverview(true);
  } catch (e) { alert(e.message); }
};

function renderChannelHeaderAndInspector() {
  if (!selectedChannel || !wsData) return;
  const chObj = (wsData.channels || []).find(c => c.channel === selectedChannel.channel)
    || { namespace: selectedChannel.namespace, namespaces: [selectedChannel.namespace], subscribers: [], publishers: [], message_count: 0, open_tasks: 0, description: "" };
  selectedChannel.namespace = chObj.namespace || selectedChannel.namespace;
  const { namespace, channel } = selectedChannel;
  const nss = chObj.namespaces || [namespace];
  const writable = nss.some(canWriteNs) || canWriteNs(namespace);
  const liveSubs = liveSubscribersFor(chObj);
  const allSubs = chObj.subscribers || [];

  $("#ch-stage-title").textContent = channel.replace(/^#/, "");
  $("#ch-stage-ns").textContent = nss.length > 1 ? `${nss.length} namespaces` : namespace;
  $("#ch-stage-ns").title = nss.join(", ");
  $("#ch-stage-meta").textContent =
    `${liveSubs.length} online / ${allSubs.length} subscribed · ${chObj.message_count || 0} messages` +
    (chObj.description ? ` · ${chObj.description}` : "");
  $("#ch-comp-input").placeholder = `Message ${channel}… (Enter to send, Shift+Enter for newline)`;
  $("#delete-channel-btn").hidden = !writable;

  // Stacked live avatars
  $("#ch-live-avatars").innerHTML = liveSubs.slice(0, 5).map(h =>
    `<span class="mini-av" style="background:${avatarColor(h)}" title="@${esc(h)} (online)">${esc(h.slice(0, 2).toUpperCase())}</span>`
  ).join("");

  // Right Inspector: separate Online vs Offline subscribers + Subscribe control
  const offlineSubs = allSubs.filter(h => !liveSubs.includes(h));
  const candidateHandles = [...new Set((wsData.sessions || [])
    .filter(s => s.alive && !allSubs.includes(s.name))
    .map(s => s.name))];

  const findHandleNs = (h) => {
    const s = (wsData.sessions || []).find(x => x.name === h && x.alive)
      || (wsData.sessions || []).find(x => x.name === h);
    return s ? s.namespace : namespace;
  };

  const renderMemberRow = (h, isLive) => `
    <div class="member-row">
      <div class="member-row-left" data-jump-handle="${esc(h)}" data-jump-ns="${esc(findHandleNs(h))}" title="Open @${esc(h)} in Handles">
        <span class="dot ${isLive ? "alive" : "idle"}"></span>
        <code>@${esc(h)}</code>
        ${harnessBadgeByName(h)}
      </div>
      ${writable ? `<button class="danger sm" style="padding:2px 8px;font-size:11px" data-unsub="${esc(h)}">Unregister</button>` : ""}
    </div>`;

  $("#ch-inspector-body").innerHTML = `
    <div class="insp-block">
      <div class="insp-hdr">Online Subscribers (${liveSubs.length})</div>
      ${liveSubs.length ? liveSubs.map(h => renderMemberRow(h, true)).join("") : `<div class="muted">No online handles subscribed right now.</div>`}
    </div>

    ${offlineSubs.length ? `
    <div class="insp-block">
      <div class="insp-hdr">Inactive / Offline Subscribers (${offlineSubs.length})</div>
      ${offlineSubs.map(h => renderMemberRow(h, false)).join("")}
    </div>` : ""}

    ${writable ? `
    <div class="insp-block">
      <div class="insp-hdr">Attach @Handle to ${esc(channel)}</div>
      <div style="display:flex;flex-direction:column;gap:7px">
        ${candidateHandles.length ? `<select id="ch-insp-pick">
          <option value="">Choose online @handle…</option>
          ${candidateHandles.map(h => `<option value="${esc(h)}">@${esc(h)}</option>`).join("")}
        </select>` : ""}
        <input id="ch-insp-custom" placeholder="or type @handle…">
        <button id="ch-insp-add" class="btn-primary sm">Subscribe Handle</button>
      </div>
    </div>` : ""}
  `;

  $$("#ch-inspector-body [data-unsub]").forEach(btn => {
    btn.onclick = async () => {
      try {
        await wapi("POST", "channel/unsubscribe", { namespace, channel, session_name: btn.dataset.unsub });
        await refreshOverview(false);
      } catch (e) { alert(e.message); }
    };
  });
  $$("#ch-inspector-body [data-jump-handle]").forEach(el => {
    el.onclick = () => jumpToHandle(el.dataset.jumpNs, el.dataset.jumpHandle);
  });
  const addBtn = $("#ch-insp-add");
  if (addBtn) {
    addBtn.onclick = async () => {
      const picked = $("#ch-insp-pick")?.value || "";
      const typed = ($("#ch-insp-custom")?.value || "").trim().replace(/^@/, "");
      const target = typed || picked;
      if (!target) return;
      try {
        await wapi("POST", "channel/subscribe", { namespace, channel, session_name: target });
        await refreshOverview(false);
      } catch (e) { alert(e.message); }
    };
  }
}

async function loadChannelMessages(forceScrollBottom = false) {
  if (!selectedChannel) return;
  const box = $("#ch-messages");
  const nearBottom = forceScrollBottom || (box.scrollHeight - box.scrollTop - box.clientHeight < 100);
  const { namespace, channel } = selectedChannel;
  try {
    const { messages } = await wapi("GET", `channel/messages?namespace=${encodeURIComponent(namespace)}&channel=${encodeURIComponent(channel)}&limit=150`);
    if (!messages || !messages.length) {
      box.innerHTML = `<div class="empty-state">
        <div class="empty-ico">#</div>
        <h3 style="margin:0 0 6px;color:var(--fg)">${esc(channel)}</h3>
        <p>No messages in <code>${esc(namespace)}</code> · <b>${esc(channel)}</b> yet.<br>Post below to broadcast and wake subscribed handles.</p>
      </div>`;
      return;
    }
    box.innerHTML = messages.map(m => {
      const sender = m.sender_session || "agent";
      const initials = sender.replace(/^@/, "").slice(0, 2).toUpperCase();
      const isDone = m.claimable && ["acked", "completed", "done", "acted"].includes(String(m.claim_status || "").toLowerCase());
      const cls = m.claimable ? (isDone ? "msg-row is-done" : "msg-row is-task") : "msg-row";
      const isPeerDm = m.source === "peer_dm" && m.to_session;
      let taskStrip = "";
      if (m.claimable) {
        const st = (m.claim_status || "pending").toUpperCase();
        const who = m.claimed_by ? ` · handled by <b>@${esc(m.claimed_by)}</b>` : (m.to_session ? ` · assigned to <b>@${esc(m.to_session)}</b>` : " · waiting for subscriber");
        const res = m.resolution ? ` · <code>${esc(m.resolution)}</code>` : "";
        taskStrip = `<div class="task-strip">
          <span class="pill ${isDone ? "ok" : "warn"}">TASK · ${esc(st)}</span>
          <span class="muted">#${m.id}${who}${res}</span>
        </div>`;
      }
      return `<div class="${cls}">
        <div class="msg-av" style="background:${avatarColor(sender)}">${esc(initials)}</div>
        <div class="msg-content">
          <div class="msg-meta">
            <span class="msg-author" data-author="${esc(sender)}" data-ns="${esc(m.namespace || namespace)}">@${esc(sender)}${isPeerDm ? ` → @${esc(m.to_session)}` : ""}</span>
            ${harnessBadgeByName(sender)}
            ${isPeerDm ? `<span class="tiny-pill warn" title="Direct peer message between subscribed handles of this channel">peer dm</span>` : ""}
            <span class="tiny-pill">${esc(m.intent || "message")}</span>
            ${m.namespace ? `<span class="tiny-pill tag">${esc(m.namespace)}</span>` : ""}
            <span class="msg-ts" title="${esc(m.created_at)}">${fmt(m.created_at)} (${age(m.created_at)} ago) · #${m.id}</span>
          </div>
          <div class="msg-body-text">${formatRichText(m.content)}</div>
          ${taskStrip}
        </div>
      </div>`;
    }).join("");
    $$("#ch-messages [data-author]").forEach(el => {
      el.onclick = () => jumpToHandle(el.dataset.ns, el.dataset.author);
    });
    if (nearBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    box.innerHTML = `<div class="empty-state"><span class="err">${esc(e.message)}</span></div>`;
  }
}

async function sendChannelMessage() {
  if (!selectedChannel) return;
  const text = $("#ch-comp-input").value.trim();
  if (!text) return;
  const sender = ($("#ch-comp-sender").value.trim().replace(/^@/, "")) || "owner";
  const claimable = $("#ch-comp-claimable").checked;
  $("#ch-comp-err").textContent = "";
  $("#ch-comp-send").disabled = true;
  try {
    const payload = {
      namespace: selectedChannel.namespace,
      channel: selectedChannel.channel,
      sender_session: sender,
      content: text,
      claimable,
      intent: claimable ? "task" : "message",
    };
    try {
      await wsCall("publish_channel", payload);
    } catch (_) {
      await wapi("POST", "channel/publish", payload);
    }
    $("#ch-comp-input").value = "";
    $("#ch-comp-claimable").checked = false;
    await loadChannelMessages(true);
    await refreshOverview(false);
  } catch (e) {
    $("#ch-comp-err").textContent = e.message;
  } finally {
    $("#ch-comp-send").disabled = false;
  }
}
$("#ch-comp-send").onclick = sendChannelMessage;
$("#ch-comp-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChannelMessage(); }
});

// ============================================================================
// SECTION 2: HANDLES (@) — Online-Only Default, Filter, Hero & Direct Inbox
// ============================================================================

$$("#hd-mode-tabs button").forEach(btn => {
  btn.onclick = () => {
    hdFilterMode = btn.dataset.mode;
    $$("#hd-mode-tabs button").forEach(x => x.classList.toggle("active", x === btn));
    renderHandleList();
  };
});
$("#hd-search").oninput = renderHandleList;
if ($("#hd-harness-filter")) $("#hd-harness-filter").onchange = renderHandleList;

function jumpToHandle(namespace, name) {
  selectedHandle = { namespace, name: name.replace(/^@/, "") };
  switchSection("handles");
}

function jumpToChannel(namespace, channel) {
  selectedChannel = { namespace, channel };
  switchSection("channels");
}

function filteredHandles() {
  const all = (wsData?.sessions || []).filter(s => !s.namespace.startsWith("test:"));
  const q = ($("#hd-search").value || "").trim().toLowerCase();
  const hf = ($("#hd-harness-filter")?.value || "").trim();
  return all.filter(s => {
    if (hdFilterMode === "online" && !s.alive) return false;
    const hInfo = normalizeHarness(s.harness, s.metadata);
    if (hf && hInfo?.key !== hf) return false;
    if (q) {
      const hay = `@${s.name} ${hInfo?.label || ""} ${s.harness || ""} ${s.namespace} ${s.cwd || ""} ${s.host || ""} ${(s.channels || []).join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderHandleList() {
  if (!wsData) return;
  const all = (wsData.sessions || []).filter(s => !s.namespace.startsWith("test:"));
  const onlineCnt = all.filter(s => s.alive).length;
  const tabs = $$("#hd-mode-tabs button");
  if (tabs[0]) tabs[0].textContent = `Online (${onlineCnt})`;
  if (tabs[1]) tabs[1].textContent = `All (${all.length})`;

  const list = filteredHandles();
  $("#hd-pane-sub").textContent = `${onlineCnt} online · ${list.length} shown`;
  const box = $("#handle-list");

  if (!list.length) {
    box.innerHTML = `<div class="empty-state" style="padding:28px 12px">
      <div class="muted">No handles match this filter.</div>
      ${hdFilterMode !== "all" ? `<button class="ghost sm" id="hd-show-all-btn" style="margin-top:8px">Show offline (${all.length})</button>` : ""}
    </div>`;
    const btn = $("#hd-show-all-btn");
    if (btn) btn.onclick = () => { $$("#hd-mode-tabs button")[1].click(); };
    return;
  }

  box.innerHTML = list.map(s => {
    const isSel = selectedHandle && selectedHandle.namespace === s.namespace && selectedHandle.name === s.name;
    const liveAct = actorLiveMap.get(s.name);
    const hbMs = s.last_heartbeat_at ? new Date(s.last_heartbeat_at).getTime() : 0;
    const bestIso = (liveAct && liveAct.ms > hbMs) ? liveAct.ts : s.last_heartbeat_at;
    const dotCls = !s.alive ? "idle" : (s.run_state === "running" || s.run_state === "busy" ? "busy" : "alive");
    const chs = s.channels || [];
    const unread = s.unread_inbox || 0;
    const shortCwd = s.cwd ? s.cwd.split("/").slice(-2).join("/") : s.namespace;
    return `<button class="list-card ${isSel ? "active" : ""}" data-ns="${esc(s.namespace)}" data-hd="${esc(s.name)}">
      <div class="list-card-top">
        <span class="list-card-name"><span class="dot ${dotCls}"></span> @${esc(s.name)}</span>
        ${unread > 0 ? `<span class="tiny-pill warn">${unread} unread</span>` : `<span class="tiny-pill">${esc(fmtAgoCompact(bestIso))}</span>`}
      </div>
      <div class="list-card-sub">
        <span>${harnessBadgeForSession(s) || esc(s.namespace)}</span>
        <span title="${esc(s.cwd || "")}">${esc(shortCwd)}</span>
      </div>
      ${chs.length ? `<div class="list-tags"><span class="tiny-pill">${esc(s.namespace)}</span>${chs.slice(0, 2).map(c => `<span class="tiny-pill tag">${esc(c)}</span>`).join("")}${chs.length > 2 ? `<span class="tiny-pill">+${chs.length - 2}</span>` : ""}</div>` : `<div class="list-tags"><span class="tiny-pill">${esc(s.namespace)}</span></div>`}
    </button>`;
  }).join("");

  $$("#handle-list .list-card").forEach(card => {
    card.onclick = async () => {
      selectedHandle = { namespace: card.dataset.ns, name: card.dataset.hd };
      renderHandleList();
      renderHandleHero();
      await loadHandleMessages(true);
    };
  });
}

function renderHandleHero() {
  const box = $("#hd-hero");
  if (!selectedHandle || !wsData) { box.innerHTML = ""; return; }
  const { namespace, name } = selectedHandle;
  const writable = canWriteNs(namespace);
  const sObj = (wsData.sessions || []).find(s => s.namespace === namespace && s.name === name);
  const chs = sObj?.channels || [];
  const initials = name.slice(0, 2).toUpperCase();

  $("#hd-comp-input").placeholder = `Send direct @inbox message or task to @${name}… (Enter to send)`;

  box.innerHTML = `
    <div class="hero-top">
      <div class="hero-id">
        <div class="hero-av" style="background:${avatarColor(name)}">${esc(initials)}</div>
        <div>
          <div class="chat-title-row">
            <h2 style="margin:0;font-size:17px">@${esc(name)}</h2>
            ${sObj ? harnessBadgeForSession(sObj) : ""}
            <span class="ns-badge">${esc(namespace)}</span>
            ${sObj
              ? `<span class="pill ${sObj.alive ? "ok" : "no"}">${sObj.alive ? "ONLINE · " + esc(sObj.run_state || "idle") : "OFFLINE"}</span>`
              : `<span class="pill warn">UNREGISTERED HANDLE</span>`}
          </div>
          ${sObj ? `<div class="hero-meta-grid" style="margin-top:5px">
            ${sObj.harness || sObj.metadata?.display_label ? `<span>Harness: <b>${esc(normalizeHarness(sObj.harness, sObj.metadata)?.label || sObj.harness)}</b></span>` : ""}
            <span>Heartbeat: <b>${age(sObj.last_heartbeat_at)} ago</b></span>
            ${sObj.pid ? `<span>PID: <code>${sObj.pid}</code> ${esc(sObj.tty || "")}</span>` : ""}
            ${sObj.cwd ? `<span>Folder: <code>${esc(sObj.cwd)}</code></span>` : ""}
            ${sObj.host ? `<span>Host: <code>${esc(sObj.host)}</code></span>` : ""}
          </div>` : ""}
        </div>
      </div>

      ${writable && sObj ? `<button id="unreg-handle-btn" class="danger sm">Unregister Handle</button>` : ""}
    </div>

    <div class="hero-ch-bar">
      <span class="muted" style="font-weight:600;margin-right:4px">Channels:</span>
      ${chs.length ? chs.map(c => `
        <span class="ch-chip">
          <span style="cursor:pointer" data-open-ch="${esc(c)}">${esc(c)}</span>
          ${writable ? `<button data-leave-ch="${esc(c)}" title="Unregister @${esc(name)} from ${esc(c)}">×</button>` : ""}
        </span>
      `).join("") : `<span class="muted">None</span>`}
      ${writable ? `
        <input id="hd-join-input" placeholder="+ #channel" style="max-width:140px;padding:3px 9px;font-size:12px;border-radius:99px">
        <button id="hd-join-btn" class="ghost sm" style="border-radius:99px">Subscribe</button>
      ` : ""}
    </div>
  `;

  $$("#hd-hero [data-open-ch]").forEach(el => {
    el.onclick = () => jumpToChannel(namespace, el.dataset.openCh);
  });
  $$("#hd-hero [data-leave-ch]").forEach(btn => {
    btn.onclick = async () => {
      try {
        await wapi("POST", "channel/unsubscribe", { namespace, channel: btn.dataset.leaveCh, session_name: name });
        await refreshOverview(false);
      } catch (e) { alert(e.message); }
    };
  });
  const joinBtn = $("#hd-join-btn");
  if (joinBtn) {
    joinBtn.onclick = async () => {
      const raw = ($("#hd-join-input")?.value || "").trim();
      if (!raw) return;
      const ch = raw.startsWith("#") ? raw : "#" + raw;
      try {
        await wapi("POST", "channel/subscribe", { namespace, channel: ch, session_name: name });
        await refreshOverview(false);
      } catch (e) { alert(e.message); }
    };
  }
  const unregBtn = $("#unreg-handle-btn");
  if (unregBtn) {
    unregBtn.onclick = async () => {
      if (!confirm(`Unregister handle "@${name}" in "${namespace}" and remove all of its channel subscriptions?`)) return;
      try {
        await wapi("DELETE", `session?namespace=${encodeURIComponent(namespace)}&session=${encodeURIComponent(name)}`);
        selectedHandle = null;
        await refreshOverview(true);
      } catch (e) { alert(e.message); }
    };
  }
}

async function loadHandleMessages(forceScrollBottom = false) {
  if (!selectedHandle) return;
  const box = $("#hd-messages");
  const nearBottom = forceScrollBottom || (box.scrollHeight - box.scrollTop - box.clientHeight < 100);
  const { namespace, name } = selectedHandle;
  try {
    const { messages } = await wapi("GET", `session/messages?namespace=${encodeURIComponent(namespace)}&session=${encodeURIComponent(name)}&limit=150`);
    const handleOps = liveEvents.filter(e => String(e.actor || "").replace(/^@/, "") === name).slice(0, 12);
    const opsStrip = handleOps.length ? `<div class="surface-card" style="margin:8px 0 14px;padding:12px 14px">
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <b style="font-size:12.5px">⚡ Recent MEMNOS Calls by @${esc(name)}</b>
        <span class="muted" style="font-size:11.5px">${handleOps.length} recent</span>
      </div>
      <div style="display:flex;flex-direction:column;gap:5px;font-size:12px">
        ${handleOps.map(e => `<div class="row" style="justify-content:space-between;gap:10px">
          <span><code><b>${esc(e.op)}</b></code> <span class="muted">${esc(e.detail || "—")}</span></span>
          <span class="muted" style="white-space:nowrap">${esc(fmtAgoCompact(e.ts))} · ${e.latency_ms ?? 0}ms</span>
        </div>`).join("")}
      </div>
    </div>` : "";
    if (!messages || !messages.length) {
      box.innerHTML = opsStrip + `<div class="empty-state">
        <div class="empty-ico">@</div>
        <h3 style="margin:0 0 6px;color:var(--fg)">@${esc(name)}</h3>
        <p>No direct <code>@inbox</code> messages for <b>@${esc(name)}</b> in <code>${esc(namespace)}</code>.<br>Send a DM below to wake this handle directly.</p>
      </div>`;
      return;
    }
    box.innerHTML = opsStrip + messages.map(m => {
      const sender = m.from_session || "agent";
      const initials = sender.slice(0, 2).toUpperCase();
      const isActed = m.status === "acted";
      return `<div class="msg-row ${isActed ? "is-done" : ""}">
        <div class="msg-av" style="background:${avatarColor(sender)}">${esc(initials)}</div>
        <div class="msg-content">
          <div class="msg-meta">
            <span class="msg-author">@${esc(sender)} → @${esc(m.to_session)}</span>
            <span class="tiny-pill">${esc(m.intent || "message")} · ${esc(m.status || "pending")}</span>
            <span class="msg-ts">${fmt(m.created_at)} (${age(m.created_at)} ago) · #${m.id}</span>
          </div>
          ${m.subject ? `<div style="font-weight:700;margin-bottom:4px">${esc(m.subject)}</div>` : ""}
          <div class="msg-body-text">${formatRichText(m.content)}</div>
          ${m.resolution ? `<div class="task-strip"><span class="pill ok">ACTED</span> <span>${esc(m.resolution)}</span></div>` : ""}
        </div>
      </div>`;
    }).join("");
    if (nearBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    box.innerHTML = `<div class="empty-state"><span class="err">${esc(e.message)}</span></div>`;
  }
}

async function sendHandleMessage() {
  if (!selectedHandle) return;
  const text = $("#hd-comp-input").value.trim();
  if (!text) return;
  const sender = ($("#hd-comp-sender").value.trim().replace(/^@/, "")) || "owner";
  const intent = $("#hd-comp-intent").value || "message";
  $("#hd-comp-err").textContent = "";
  $("#hd-comp-send").disabled = true;
  try {
    const payload = {
      namespace: selectedHandle.namespace,
      to_session: selectedHandle.name,
      from_session: sender,
      content: text,
      intent,
    };
    try {
      await wsCall("send_dm", payload);
    } catch (_) {
      await wapi("POST", "session/send", payload);
    }
    $("#hd-comp-input").value = "";
    await loadHandleMessages(true);
    await refreshOverview(false);
  } catch (e) {
    $("#hd-comp-err").textContent = e.message;
  } finally {
    $("#hd-comp-send").disabled = false;
  }
}
$("#hd-comp-send").onclick = sendHandleMessage;
$("#hd-comp-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendHandleMessage(); }
});

// ============================================================================
// SECTION 3: MEMORY EXPLORER
// ============================================================================

$$("#wmem-type-pills button").forEach(btn => {
  btn.onclick = () => {
    wmemType = btn.dataset.type;
    $$("#wmem-type-pills button").forEach(x => x.classList.toggle("active", x === btn));
    wmemOffset = 0;
    loadWorkspaceMemories();
  };
});

async function loadWorkspaceMemories() {
  const tbody = $("#wmem-table tbody");
  const ns = $("#ws-ns-filter").value || "*";
  const q = $("#wmem-q").value.trim();
  const qs = `memories?limit=${WMEM_PAGE}&offset=${wmemOffset}&namespace=${encodeURIComponent(ns)}`
    + (wmemType ? `&type=${encodeURIComponent(wmemType)}` : "")
    + (q ? `&q=${encodeURIComponent(q)}` : "");
  await loadTable(tbody, () => wapi("GET", qs), ({ memories }) => {
    tbody.innerHTML = (memories || []).map(m => `<tr>
      <td title="${esc(m.observed_at || "")}">${age(m.observed_at)}</td>
      <td><code>${esc(m.namespace)}</code></td>
      <td>${m.type ? `<span class="pill ${m.type === "constraint" ? "no" : (m.type === "decision" ? "warn" : "ok")}">${esc(m.type)}</span>` : ""}</td>
      <td>${esc(m.author || m.speaker || "—")}</td>
      <td style="white-space:pre-wrap;word-break:break-word">${formatRichText(m.content)}</td>
    </tr>`).join("") || emptyRow(5, "No matching memories in your authorized scope.");
    $("#wmem-info").textContent = `page ${Math.floor(wmemOffset / WMEM_PAGE) + 1}`;
    $("#wmem-prev").disabled = wmemOffset === 0;
    $("#wmem-next").disabled = (memories || []).length < WMEM_PAGE;
  }, loadWorkspaceMemories);
}
$("#wmem-search").onclick = () => { wmemOffset = 0; loadWorkspaceMemories(); };
$("#wmem-q").addEventListener("keydown", e => { if (e.key === "Enter") { wmemOffset = 0; loadWorkspaceMemories(); } });
$("#wmem-prev").onclick = () => { wmemOffset = Math.max(0, wmemOffset - WMEM_PAGE); loadWorkspaceMemories(); };
$("#wmem-next").onclick = () => { wmemOffset += WMEM_PAGE; loadWorkspaceMemories(); };

// ============================================================================
// SECTION 3B: CONSTRAINTS & CROSS-NAMESPACE VALIDATOR
// ============================================================================

async function loadWorkspaceConstraints() {
  const nsSel = $("#cs-ns-select")?.value || $("#ws-ns-filter")?.value || "*";
  const defaultWriteNs = (nsSel && nsSel !== "*")
    ? nsSel
    : ((wsData?.namespaces || []).find(n => n.can_write && !n.name.startsWith("test:"))?.name || "");
  if ($("#cs-add-enf-ns") && !$("#cs-add-enf-ns").value) $("#cs-add-enf-ns").value = defaultWriteNs;
  if ($("#cs-add-pin-ns") && !$("#cs-add-pin-ns").value) $("#cs-add-pin-ns").value = defaultWriteNs;

  const enfTbody = $("#cs-enf-table tbody");
  const pinTbody = $("#cs-pin-table tbody");
  if (!enfTbody || !pinTbody) return;

  enfTbody.innerHTML = `<tr class="state"><td colspan="6"><span class="spinner"></span> loading…</td></tr>`;
  pinTbody.innerHTML = `<tr class="state"><td colspan="6"><span class="spinner"></span> loading…</td></tr>`;

  try {
    const data = await wapi("GET", "constraints?namespace=" + encodeURIComponent(nsSel));
    const enf = data.enforcement_rules || [];
    const pin = data.pinned_constraints || [];
    $("#cs-enf-cnt").textContent = enf.length;
    $("#cs-pin-cnt").textContent = pin.length;

    enfTbody.innerHTML = enf.map(r => `<tr>
      <td><code>${esc(r.namespace)}</code>${r.inherited ? ` <span class="tiny-pill tag">inherited</span>` : ""}</td>
      <td><span class="pill ${r.enforce_level === "block" ? "no" : "warn"}">${esc((r.enforce_level || "ask").toUpperCase())}</span></td>
      <td><code>${esc(r.tool_matcher)}</code></td>
      <td>${formatRichText(r.rule_text)}</td>
      <td title="${esc(r.created_at || "")}">${age(r.created_at)}</td>
      <td>${r.can_write ? `<button class="danger sm" data-del-enf="${r.id}">Remove</button>` : ""}</td>
    </tr>`).join("") || emptyRow(6, "No active PreToolUse tool guardrails in this scope.");

    pinTbody.innerHTML = pin.map(p => `<tr>
      <td><code>${esc(p.namespace)}</code>${p.inherited ? ` <span class="tiny-pill tag">inherited</span>` : ""}</td>
      <td>${p.subject ? `<code>${esc(p.subject)}</code>` : `<span class="muted">—</span>`}</td>
      <td style="white-space:pre-wrap;word-break:break-word">${formatRichText(p.content)}</td>
      <td>${esc(p.author || "—")}</td>
      <td title="${esc(p.observed_at || "")}">${age(p.observed_at)}</td>
      <td>${p.can_write ? `<button class="danger sm" data-retire-pin="${p.id}" data-retire-ns="${esc(p.namespace)}">Retire</button>` : ""}</td>
    </tr>`).join("") || emptyRow(6, "No pinned semantic constraints in this scope.");

    $$("#cs-enf-table [data-del-enf]").forEach(btn => {
      btn.onclick = async () => {
        if (!confirm("Deactivate this PreToolUse guardrail rule?")) return;
        try {
          await wapi("DELETE", "constraints/enforcement?id=" + btn.dataset.delEnf);
          await loadWorkspaceConstraints();
          refreshOverview(false);
        } catch (e) { alert(e.message); }
      };
    });

    $$("#cs-pin-table [data-retire-pin]").forEach(btn => {
      btn.onclick = async () => {
        if (!confirm("Retire this pinned constraint?")) return;
        try {
          await wapi("DELETE", `constraints/pin?id=${btn.dataset.retirePin}&namespace=${encodeURIComponent(btn.dataset.retireNs)}`);
          await loadWorkspaceConstraints();
          refreshOverview(false);
        } catch (e) { alert(e.message); }
      };
    });
  } catch (e) {
    enfTbody.innerHTML = emptyRow(6, e.message);
    pinTbody.innerHTML = emptyRow(6, e.message);
  }
}

if ($("#cs-ns-select")) {
  $("#cs-ns-select").onchange = () => {
    const v = $("#cs-ns-select").value;
    if (v && v !== "*") {
      if ($("#cs-add-enf-ns")) $("#cs-add-enf-ns").value = v;
      if ($("#cs-add-pin-ns")) $("#cs-add-pin-ns").value = v;
    }
    loadWorkspaceConstraints();
  };
}

if ($("#cs-val-tool-btn")) {
  $("#cs-val-tool-btn").onclick = async () => {
    const subj = ($("#cs-val-tool")?.value || "").trim();
    const out = $("#cs-val-tool-out");
    if (!subj) { out.innerHTML = `<span class="err">Enter a tool call subject, e.g. <code>Bash(rm -rf /tmp/x)</code></span>`; return; }
    out.innerHTML = `<span class="muted"><span class="spinner"></span> evaluating guardrails…</span>`;
    try {
      const ns = $("#cs-ns-select")?.value || "*";
      const res = await wapi("POST", "constraints/validate", { namespace: ns, tool_subject: subj });
      const tv = res.tool_validation || {};
      const dec = (tv.decision || "allow").toUpperCase();
      const cls = dec === "BLOCK" ? "no" : (dec === "ASK" ? "warn" : "ok");
      const matches = tv.matched_rules || [];
      out.innerHTML = `<div class="val-box">
        <div class="row" style="justify-content:space-between;margin-bottom:${matches.length ? 8 : 0}px">
          <span>Verdict: <span class="pill ${cls}">${esc(dec)}</span></span>
          <span class="muted">Checked ${tv.checked_rules ?? 0} active guardrail(s) in <code>${esc(ns)}</code></span>
        </div>
        ${matches.map(m => `<div style="padding-top:6px;border-top:1px solid var(--border)">
          <code>${esc(m.namespace)}</code> · <code>${esc(m.tool_matcher)}</code> — ${esc(m.rule_text)}
        </div>`).join("")}
      </div>`;
    } catch (e) {
      out.innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  };
}

if ($("#cs-val-stmt-btn")) {
  $("#cs-val-stmt-btn").onclick = async () => {
    const stmt = ($("#cs-val-stmt")?.value || "").trim();
    const out = $("#cs-val-stmt-out");
    if (!stmt) { out.innerHTML = `<span class="err">Enter a statement, plan, or snippet to validate.</span>`; return; }
    out.innerHTML = `<span class="muted"><span class="spinner"></span> checking pinned constraints &amp; reconcile…</span>`;
    try {
      const ns = $("#cs-ns-select")?.value || "*";
      const res = await wapi("POST", "constraints/validate", { namespace: ns, statement: stmt });
      const sv = res.statement_validation || {};
      const pm = sv.pinned_matches || [];
      const rec = sv.reconcile;
      const recStatus = rec?.status ? `<span class="pill ${rec.status === "stale" ? "no" : "ok"}">reconcile: ${esc(rec.status)}</span>` : "";
      out.innerHTML = `<div class="val-box">
        <div class="row" style="justify-content:space-between;margin-bottom:6px">
          <span><b>${pm.length}</b> relevant pinned constraint(s) matched</span>
          ${recStatus}
        </div>
        ${pm.slice(0, 5).map(p => `<div style="padding-top:6px;border-top:1px solid var(--border)">
          <code>${esc(p.namespace)}</code>${p.subject ? ` · <b>${esc(p.subject)}</b>` : ""}: ${esc(String(p.content || "").slice(0, 220))}${String(p.content || "").length > 220 ? "…" : ""}
        </div>`).join("") || `<div class="muted">No conflicting or matching pinned constraints found for these terms.</div>`}
      </div>`;
    } catch (e) {
      out.innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  };
}

if ($("#cs-add-enf-btn")) {
  $("#cs-add-enf-btn").onclick = async () => {
    const ns = ($("#cs-add-enf-ns")?.value || "").trim();
    const lvl = $("#cs-add-enf-lvl")?.value || "block";
    const matcher = ($("#cs-add-enf-matcher")?.value || "").trim();
    const text = ($("#cs-add-enf-text")?.value || "").trim();
    if (!ns || !matcher || !text) { alert("Namespace, Tool Matcher, and Rule Reason are required."); return; }
    try {
      await wapi("POST", "constraints/enforcement", { namespace: ns, enforce_level: lvl, tool_matcher: matcher, rule_text: text });
      $("#cs-add-enf-matcher").value = "";
      $("#cs-add-enf-text").value = "";
      await loadWorkspaceConstraints();
      refreshOverview(false);
    } catch (e) { alert(e.message); }
  };
}

if ($("#cs-add-pin-btn")) {
  $("#cs-add-pin-btn").onclick = async () => {
    const ns = ($("#cs-add-pin-ns")?.value || "").trim();
    const subj = ($("#cs-add-pin-subj")?.value || "").trim();
    const text = ($("#cs-add-pin-text")?.value || "").trim();
    if (!ns || !text) { alert("Namespace and Constraint Text are required."); return; }
    try {
      await wapi("POST", "constraints/pin", { namespace: ns, subject: subj || undefined, text });
      $("#cs-add-pin-subj").value = "";
      $("#cs-add-pin-text").value = "";
      await loadWorkspaceConstraints();
      refreshOverview(false);
    } catch (e) { alert(e.message); }
  };
}

// ============================================================================
// SECTION 4: ENGINE & DB TELEMETRY
// ============================================================================

function renderTelemetry() {
  if (!wsData) return;
  const v = wsData.vitals || {};
  const u = v.usage_30d || {};
  const onlineHds = (wsData.sessions || []).filter(s => s.alive && !s.namespace.startsWith("test:")).length;
  const activeChs = (wsData.channels || []).filter(isChannelActive).length;
  const upHrs = v.uptime_secs != null ? (v.uptime_secs / 3600).toFixed(1) + "h" : "—";
  const ramVal = v.footprint_mb != null ? `${v.footprint_mb} MB` : (v.rss_mb != null ? `${v.rss_mb} MB` : "remote");
  const ramSub = (v.rss_mb != null && v.footprint_mb != null) ? `RSS ${v.rss_mb} MB · v${v.version || ""} · up ${upHrs}` : `v${v.version || ""} · up ${upHrs}`;

  $("#tel-cards").innerHTML = `
    <div class="kpi-card"><div class="kpi-lbl">Rust Engine Footprint</div><div class="kpi-val">${esc(ramVal)}</div><div class="kpi-sub">${esc(ramSub)}</div></div>
    <div class="kpi-card"><div class="kpi-lbl">PostgreSQL Storage</div><div class="kpi-val">${esc(fmtBytes(v.db_size_bytes))}</div><div class="kpi-sub">${esc(v.embedding_mode || "—")}</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Semantic Facts</div><div class="kpi-val">${(v.total_facts || 0).toLocaleString()}</div><div class="kpi-sub">${(v.total_entities || 0).toLocaleString()} graph entities</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Raw Turns</div><div class="kpi-val">${(v.total_turns || 0).toLocaleString()}</div><div class="kpi-sub">across authorized scope</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Active Mesh</div><div class="kpi-val">${onlineHds} @ · ${activeChs} #</div><div class="kpi-sub">online handles &amp; active channels</div></div>
    <div class="kpi-card"><div class="kpi-lbl">30d Ledger</div><div class="kpi-val">$${Number(u.cost_usd || 0).toFixed(4)}</div><div class="kpi-sub">${Number((u.tokens_in || 0) + (u.tokens_out || 0)).toLocaleString()} tokens</div></div>
  `;

  $("#tel-ops-table tbody").innerHTML = (v.ops_24h || []).map(o => `<tr>
    <td><code>${esc(o.op)}</code></td>
    <td>${(o.calls || 0).toLocaleString()}</td>
    <td><b>${Math.round(o.p50_ms || 0)} ms</b></td>
    <td>${Math.round(o.p95_ms || 0)} ms</td>
    <td><span class="pill ${(o.err_pct || 0) > 0 ? "no" : "ok"}">${o.err_pct || 0}%</span></td>
  </tr>`).join("") || emptyRow(5, "No operations recorded in the last 24h.");

  renderTelemetryNamespaces();
}

function renderTelemetryNamespaces() {
  if (!wsData) return;
  const q = ($("#tel-ns-search")?.value || "").trim().toLowerCase();
  const rows = (wsData.namespaces || [])
    .filter(n => !n.name.startsWith("test:"))
    .filter(n => !q || n.name.toLowerCase().includes(q) || (n.description || "").toLowerCase().includes(q))
    .filter(n => q || (n.facts || 0) > 0 || (n.turns || 0) > 0 || n.registered);

  $("#tel-ns-table tbody").innerHTML = rows.map(n => `<tr>
    <td><code>${esc(n.name)}</code></td>
    <td><b>${(n.facts || 0).toLocaleString()}</b></td>
    <td>${(n.turns || 0).toLocaleString()}</td>
    <td>${(n.entities || 0).toLocaleString()}</td>
    <td><span class="pill ok">${n.can_write ? "RW" : "RO"}</span></td>
  </tr>`).join("") || emptyRow(5, "No matching namespaces with data.");
}
$("#tel-ns-search").oninput = renderTelemetryNamespaces;

// ============================================================================
// SECTION 5: GOVERNANCE & CONSOLE SUBTABS
// ============================================================================

function switchConsoleTab(ctab) {
  $$("#console-tabs button").forEach(b => b.classList.toggle("active", b.dataset.ctab === ctab));
  $$(".ctab-pane").forEach(p => { p.hidden = (p.id !== "ctab-" + ctab); });
  if (ctab === "routing") loadRouting();
  if (ctab === "cli") loadCliRef();
  if (ctab === "namespaces") loadNamespaces();
  if (ctab === "access") loadPrincipals();
  if (ctab === "tokens") loadPrincipals();
  if (ctab === "usage") loadUsagePanel();
  if (ctab === "secrets") loadSecrets();
  if (ctab === "dashboard") loadDashboard();
  if (ctab === "settings") loadSettings();
}
$$("#console-tabs button").forEach(b => {
  b.onclick = () => switchConsoleTab(b.dataset.ctab);
});

// ---- Namespaces ----
async function loadNamespaces() {
  const tbody = $("#ns-table tbody");
  await loadTable(tbody, () => api("GET", "namespaces"), ({ namespaces }) => {
    tbody.innerHTML = namespaces.map(n => `<tr>
      <td><code>${esc(n.name)}</code>${n.registered ? "" : ' <span class="pill warn">discovered</span>'}</td>
      <td>${esc(n.description || "")}</td>
      <td>${n.turns}</td><td>${n.facts}</td><td>${fmt(n.created_at)}</td>
      <td><button class="danger" data-del="${esc(n.name)}">delete</button></td></tr>`).join("")
      || emptyRow(6, `No namespaces yet.`);
    $$("#ns-table [data-del]").forEach(b => b.onclick = async () => {
      if (!confirm(`Delete namespace "${b.dataset.del}"? Also purge its stored memory?\n\nOK = delete + purge data · Cancel = keep`)) {
        if (!confirm(`Delete namespace "${b.dataset.del}" but KEEP its data?`)) return;
        await api("DELETE", "namespaces?name=" + encodeURIComponent(b.dataset.del));
      } else {
        await api("DELETE", "namespaces?purge=1&name=" + encodeURIComponent(b.dataset.del));
      }
      loadNamespaces(); refreshOverview(false);
    });
  }, loadNamespaces);
}
$("#ns-create").onclick = async () => {
  const name = $("#ns-name").value.trim(); if (!name) return;
  try {
    await api("POST", "namespaces", { name, description: $("#ns-desc").value.trim() });
    $("#ns-name").value = ""; $("#ns-desc").value = "";
    loadNamespaces(); refreshOverview(false);
  } catch (e) { alert(e.message); }
};

// ---- Principals, Grants & Tokens ----
function renderPrincipalsRows() {
  const tbody = $("#pr-table tbody");
  if (!tbody) return;
  const q = ($("#pr-filter")?.value || "").trim().toLowerCase();
  const filtered = allPrincipals.filter(p => {
    if (!q) return true;
    return `${p.id} ${p.name} ${p.kind}`.toLowerCase().includes(q);
  });

  const staleTests = allPrincipals.filter(p =>
    p.id !== callerPrincipalId &&
    (p.active_tokens || 0) === 0 &&
    String(p.name || "").toLowerCase().startsWith("test-")
  );
  const pruneBtn = $("#pr-prune-test");
  if (pruneBtn) {
    pruneBtn.hidden = staleTests.length === 0;
    pruneBtn.textContent = `Prune 0-token test-* (${staleTests.length})`;
    pruneBtn.onclick = async () => {
      if (!confirm(`Delete ${staleTests.length} stale "test-*" principals that have 0 active tokens?`)) return;
      pruneBtn.disabled = true;
      try {
        for (const tp of staleTests) {
          await api("DELETE", "principals?id=" + tp.id);
        }
        await loadPrincipals();
        refreshOverview(false);
      } catch (e) {
        alert(e.message);
      } finally {
        pruneBtn.disabled = false;
      }
    };
  }

  tbody.innerHTML = filtered.map(p => {
    const isSelf = p.id === callerPrincipalId;
    return `<tr>
      <td>${p.id}</td>
      <td><code>${esc(p.name)}</code></td>
      <td><span class="pill ${p.kind === "service" ? "warn" : "ok"}">${esc(p.kind)}</span></td>
      <td>${p.active_tokens}</td>
      <td>
        <div class="row" style="gap:6px">
          <button class="ghost sm" data-manage-gr="${p.id}">Grants</button>
          ${isSelf
            ? `<span class="pill ok" title="Currently signed-in principal">current</span>`
            : `<button class="danger sm" data-del-pr="${p.id}" data-del-name="${esc(p.name)}">Delete</button>`}
        </div>
      </td>
    </tr>`;
  }).join("") || emptyRow(5, "No matching principals.");

  $$("#pr-table [data-manage-gr]").forEach(b => {
    b.onclick = () => {
      $("#gr-principal").value = b.dataset.manageGr;
      loadGrants();
      $("#grants-card")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
  });

  $$("#pr-table [data-del-pr]").forEach(b => {
    b.onclick = async () => {
      const id = +b.dataset.delPr;
      const nm = b.dataset.delName || String(id);
      if (!confirm(`Delete principal "${nm}" (ID ${id})?\n\nThis permanently revokes and deletes all grants, tokens, and sessions belonging to "${nm}".`)) return;
      try {
        await api("DELETE", "principals?id=" + id);
        await loadPrincipals();
        refreshOverview(false);
      } catch (e) {
        alert(e.message);
      }
    };
  });
}

if ($("#pr-filter")) $("#pr-filter").oninput = renderPrincipalsRows;

async function loadPrincipals() {
  const tbody = $("#pr-table tbody");
  const prevTokP = $("#tok-principal").value;
  const prevViewP = $("#tok-view-principal")?.value || "0";
  const prevGrP = $("#gr-principal").value;
  await loadTable(tbody, () => api("GET", "principals"), ({ principals, caller_id }) => {
    allPrincipals = principals || [];
    callerPrincipalId = caller_id || wsData?.principal?.id || 0;
    renderPrincipalsRows();
    // Order non-test principals with active tokens first in mint dropdown
    const sortedForMint = [...allPrincipals].sort((a, b) => {
      const aTest = String(a.name).startsWith("test-") ? 1 : 0;
      const bTest = String(b.name).startsWith("test-") ? 1 : 0;
      if (aTest !== bTest) return aTest - bTest;
      return (b.active_tokens || 0) - (a.active_tokens || 0);
    });
    const opts = sortedForMint.map(p => `<option value="${p.id}">${esc(p.name)} (${esc(p.kind)})</option>`).join("");
    $("#tok-principal").innerHTML = opts;
    $("#gr-principal").innerHTML = opts;
    if ($("#tok-view-principal")) {
      $("#tok-view-principal").innerHTML = `<option value="0">All principals</option>` + opts;
      if ([...$("#tok-view-principal").options].some(o => o.value === prevViewP)) {
        $("#tok-view-principal").value = prevViewP;
      }
    }
    if (prevTokP && [...$("#tok-principal").options].some(o => o.value === prevTokP)) {
      $("#tok-principal").value = prevTokP;
    }
    if (prevGrP && [...$("#gr-principal").options].some(o => o.value === prevGrP)) {
      $("#gr-principal").value = prevGrP;
    }
    loadTokens();
    loadGrants();
  }, loadPrincipals);
}
$("#pr-create").onclick = async () => {
  const name = $("#pr-name").value.trim(); if (!name) return;
  try { await api("POST", "principals", { name, kind: $("#pr-kind").value }); $("#pr-name").value = ""; loadPrincipals(); }
  catch (e) { alert(e.message); }
};

function renderTokensRows() {
  const tbody = $("#tok-table tbody");
  if (!tbody) return;
  const stMode = $("#tok-status-filter")?.value || "active";
  const hideTest = $("#tok-hide-test") ? $("#tok-hide-test").checked : true;
  const q = ($("#tok-search")?.value || "").trim().toLowerCase();

  const rows = allTokens.filter(t => {
    if (stMode === "active" && t.revoked) return false;
    if (stMode === "revoked" && !t.revoked) return false;
    if (hideTest && String(t.principal_name || "").toLowerCase().startsWith("test-")) return false;
    if (q) {
      const hay = `${t.id} ${t.principal_name || ""} ${t.principal_kind || ""} ${t.label || ""} ${t.hint || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if ($("#tok-who")) {
    const viewLbl = $("#tok-view-principal")?.selectedOptions[0]?.textContent || "All principals";
    $("#tok-who").textContent = `· ${rows.length} shown (${viewLbl})`;
  }

  tbody.innerHTML = rows.map(t => {
    const plain = revealedTokenMap[t.id];
    const pName = t.principal_name || ("#" + t.principal_id);
    return `<tr>
      <td>${t.id}</td>
      <td><code>${esc(pName)}</code>${t.principal_kind ? ` <span class="tiny-pill">${esc(t.principal_kind)}</span>` : ""}</td>
      <td><code style="word-break:break-all">${esc(plain || t.hint || "mnk_…")}</code></td>
      <td>${esc(t.label || "—")}</td>
      <td>${fmt(t.created_at)}</td>
      <td>${fmt(t.expires_at)}</td>
      <td><span class="pill ${t.revoked ? "no" : "ok"}">${t.revoked ? "revoked" : "active"}</span></td>
      <td>
        <div class="row" style="gap:5px">
          ${t.recoverable
            ? `<button class="ghost sm" data-reveal-tok="${t.id}">${plain ? "Copy" : "Reveal &amp; Copy"}</button>`
            : `<button class="ghost sm" data-rot-pid="${t.principal_id}" data-rot-lbl="${esc(t.label || "")}" title="Legacy pre-vault token stores SHA-256 hash only; click to mint &amp; copy a fresh token">Mint Copy</button>`}
          ${t.revoked ? "" : `<button class="danger sm" data-rv="${t.id}">Revoke</button>`}
        </div>
      </td>
    </tr>`;
  }).join("") || emptyRow(8, "No matching minted tokens.");

  $$("#tok-table [data-reveal-tok]").forEach(btn => {
    btn.onclick = async () => {
      const id = +btn.dataset.revealTok;
      try {
        if (!revealedTokenMap[id]) {
          const r = await api("GET", "tokens/reveal?id=" + id);
          revealedTokenMap[id] = r.token;
        }
        try { await navigator.clipboard.writeText(revealedTokenMap[id]); } catch (_) {}
        renderTokensRows();
      } catch (e) {
        alert(e.message);
      }
    };
  });

  $$("#tok-table [data-rot-pid]").forEach(btn => {
    btn.onclick = async () => {
      const pid = +btn.dataset.rotPid;
      const lbl = btn.dataset.rotLbl || "desktop-mint";
      try {
        const { token } = await api("POST", "tokens", { principal_id: pid, label: lbl, ttl_days: null });
        try { await navigator.clipboard.writeText(token); } catch (_) {}
        const b = $("#tok-show");
        b.hidden = false;
        b.innerHTML = `Minted &amp; copied recoverable token:<br><code>${esc(token)}</code>`;
        await loadTokens();
        loadPrincipals();
      } catch (e) { alert(e.message); }
    };
  });

  $$("#tok-table [data-rv]").forEach(b => {
    b.onclick = async () => {
      if (!confirm(`Revoke token #${b.dataset.rv}? Any agent using this token will immediately lose access.`)) return;
      await api("POST", "tokens/revoke", { id: +b.dataset.rv });
      loadTokens();
      loadPrincipals();
    };
  });
}

async function loadTokens() {
  const tbody = $("#tok-table tbody");
  const viewPid = +($("#tok-view-principal")?.value || 0);
  await loadTable(tbody, () => api("GET", "tokens?principal=" + viewPid), ({ tokens }) => {
    allTokens = tokens || [];
    renderTokensRows();
  }, loadTokens);
}
if ($("#tok-view-principal")) $("#tok-view-principal").onchange = loadTokens;
if ($("#tok-status-filter")) $("#tok-status-filter").onchange = renderTokensRows;
if ($("#tok-search")) $("#tok-search").oninput = renderTokensRows;
if ($("#tok-hide-test")) $("#tok-hide-test").onchange = renderTokensRows;
$("#tok-mint").onclick = async () => {
  const ttl = $("#tok-ttl").value;
  try {
    const { token } = await api("POST", "tokens", {
      principal_id: +$("#tok-principal").value, label: $("#tok-label").value.trim() || null,
      ttl_days: ttl ? +ttl : null });
    try { await navigator.clipboard.writeText(token); } catch (_) {}
    const b = $("#tok-show"); b.hidden = false;
    b.innerHTML = `Minted &amp; copied to clipboard (also recoverable anytime via <b>Reveal &amp; Copy</b> below):<br><code>${esc(token)}</code>`;
    $("#tok-label").value = ""; $("#tok-ttl").value = ""; loadTokens(); loadPrincipals();
  } catch (e) { alert(e.message); }
};
async function loadGrants() {
  const tbody = $("#gr-table tbody");
  const pid = $("#gr-principal").value;
  if (!pid) { tbody.innerHTML = emptyRow(4, `Create a principal first.`); return; }
  if ($("#gr-who")) $("#gr-who").textContent = $("#gr-principal").selectedOptions[0]?.textContent || "";
  await loadTable(tbody, () => api("GET", "grants?principal=" + pid), ({ grants }) => {
    tbody.innerHTML = grants.map(g => `<tr>
      <td><code>${esc(g.namespace)}</code></td>
      <td>${g.can_read ? "✓" : "—"}</td><td>${g.can_write ? "✓" : "—"}</td>
      <td><button class="danger sm" data-rg="${esc(g.namespace)}">Revoke</button></td></tr>`).join("")
      || emptyRow(4, `No grants configured for this principal.`);
    $$("#gr-table [data-rg]").forEach(b => b.onclick = async () => {
      try {
        await api("DELETE", "grants?principal=" + pid + "&namespace=" + encodeURIComponent(b.dataset.rg));
        loadGrants();
      } catch (e) { alert(e.message); }
    });
  }, loadGrants);
}
$("#gr-principal").onchange = loadGrants;
$("#gr-add").onclick = async () => {
  try {
    await api("POST", "grants", { principal_id: +$("#gr-principal").value, namespace: $("#gr-namespace").value.trim(),
      can_read: $("#gr-read").checked, can_write: $("#gr-write").checked });
    $("#gr-namespace").value = ""; loadGrants();
  } catch (e) { alert(e.message); }
};

// ---- Routing ----
const KEY_LABEL = { repo: "repo", host_repo: "repo", host_path: "path" };
async function loadRouting() {
  const groups = $("#bind-groups");
  groups.innerHTML = `<span class="muted"><span class="spinner"></span> loading…</span>`;
  try {
    const { bindings, hosts } = await uapi("GET", "/bindings");
    renderBindings(bindings, hosts);
    renderHosts(hosts);
    populateScope(hosts);
  } catch (e) {
    if (e.message === "unauthorized") return;
    groups.innerHTML = `<span class="errbox">${esc(e.message)} <button class="retry">retry</button></span>`;
    $("button.retry", groups).onclick = loadRouting;
  }
}
function populateScope(hosts) {
  const sel = $("#bind-scope");
  const cur = sel.value;
  sel.innerHTML = `<option value="repo">All hosts (follow the repo)</option>`
    + hosts.map(h => `<option value="${esc(h.machine_id)}">${esc(h.friendly_name || h.machine_id)}</option>`).join("");
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  syncBindForm();
}
function syncBindForm() {
  const hostScoped = $("#bind-scope").value !== "repo";
  $("#bind-keytype").hidden = !hostScoped;
  $("#bind-key").placeholder = hostScoped && $("#bind-keytype").value === "host_path"
    ? "absolute path (e.g. /srv/acme/payments-sync)"
    : "repo (e.g. github.com/acme/payments-sync)";
}
$("#bind-scope").onchange = syncBindForm;
$("#bind-keytype").onchange = syncBindForm;

function bindingRow(b) {
  return `<tr>
    <td><span class="muted">${esc(KEY_LABEL[b.key_type] || b.key_type)}</span> <code>${esc(b.key)}</code></td>
    <td><input class="bind-ns-edit" data-id="${b.id}" value="${esc(b.namespace)}"
         data-keytype="${esc(b.key_type)}" data-key="${esc(b.key)}" data-host="${esc(b.host_id ?? "")}"></td>
    <td>
      <button class="bind-save sm" data-id="${b.id}">save</button>
      <button class="danger bind-del" data-id="${b.id}" data-key="${esc(b.key)}">remove</button>
    </td></tr>`;
}
function renderBindings(bindings, hosts) {
  const box = $("#bind-groups");
  const allHost = bindings.filter(b => b.key_type === "repo");
  const byHost = {};
  for (const h of hosts) byHost[h.machine_id] = [];
  for (const b of bindings) {
    if (b.key_type === "repo") continue;
    (byHost[b.host_id] = byHost[b.host_id] || []).push(b);
  }
  const hostName = (mid) => {
    const h = hosts.find(x => x.machine_id === mid);
    return h ? (h.friendly_name || h.machine_id) : mid;
  };
  const tbl = (rows) => `<table><thead><tr><th>Key</th><th>Namespace</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  let html = `<div class="bind-group"><h4>All hosts <span class="muted">· follow-the-repo</span></h4>`
    + (allHost.length ? tbl(allHost.map(bindingRow).join("")) : `<p class="muted">No host-agnostic bindings yet.</p>`) + `</div>`;
  for (const mid of Object.keys(byHost)) {
    html += `<div class="bind-group"><h4>${esc(hostName(mid))} <span class="muted">· ${esc(mid)}</span></h4>`
      + (byHost[mid].length ? tbl(byHost[mid].map(bindingRow).join("")) : `<p class="muted">No bindings for this host.</p>`) + `</div>`;
  }
  box.innerHTML = html;
  $$(".bind-save").forEach(b => b.onclick = async () => {
    const inp = $(`.bind-ns-edit[data-id="${b.dataset.id}"]`);
    const ns = inp.value.trim(); if (!ns) return;
    try {
      await uapi("POST", "/bindings", { key_type: inp.dataset.keytype, key: inp.dataset.key, namespace: ns, host_id: inp.dataset.host || undefined });
      loadRouting();
    } catch (e) { $("#bind-err").textContent = e.message; }
  });
  $$(".bind-del").forEach(b => b.onclick = async () => {
    if (!confirm(`Remove binding for "${b.dataset.key}"?`)) return;
    try { await uapi("DELETE", "/bindings/" + b.dataset.id); loadRouting(); }
    catch (e) { $("#bind-err").textContent = e.message; }
  });
}
function renderHosts(hosts) {
  const tbody = $("#host-table tbody");
  tbody.innerHTML = hosts.map(h => `<tr>
    <td><input class="host-name-edit" data-mid="${esc(h.machine_id)}" value="${esc(h.friendly_name || "")}" placeholder="(unnamed)"></td>
    <td><code>${esc(h.machine_id)}</code></td>
    <td title="${esc(h.last_seen || "")}">${age(h.last_seen)}</td>
    <td><button class="host-save sm" data-mid="${esc(h.machine_id)}">rename</button></td></tr>`).join("")
    || emptyRow(4, `No machines registered yet.`);
  $$(".host-save").forEach(b => b.onclick = async () => {
    const inp = $(`.host-name-edit[data-mid="${CSS.escape(b.dataset.mid)}"]`);
    try {
      await uapi("POST", "/hosts", { machine_id: b.dataset.mid, friendly_name: inp.value.trim() || null });
      loadRouting();
    } catch (e) { $("#bind-err").textContent = e.message; }
  });
}
$("#bind-add").onclick = async () => {
  const scope = $("#bind-scope").value;
  const key = $("#bind-key").value.trim(), ns = $("#bind-ns").value.trim();
  if (!key || !ns) { $("#bind-err").textContent = "key and namespace are required"; return; }
  const body = (scope === "repo")
    ? { key_type: "repo", key, namespace: ns }
    : { key_type: $("#bind-keytype").value, key, namespace: ns, host_id: scope };
  try {
    await uapi("POST", "/bindings", body);
    $("#bind-key").value = ""; $("#bind-ns").value = ""; $("#bind-err").textContent = "";
    loadRouting();
  } catch (e) { $("#bind-err").textContent = e.message; }
};

// ---- Audit, Health, Secrets, Usage, CLI, Settings ----
async function loadHealth() {
  const box = $("#dash-health");
  box.innerHTML = `<span class="muted"><span class="spinner"></span> loading…</span>`;
  try {
    const h = await api("GET", "health");
    box.innerHTML = h.findings.map(f => `<div><span class="lvl ${esc(f.level)}">${esc(f.level)}</span>${esc(f.msg)}</div>`).join("")
      || `<span class="muted">OK — no findings.</span>`;
  } catch (e) { box.innerHTML = `<span class="errbox">${esc(e.message)}</span>`; }
}
async function loadStats() {
  const tbody = $("#dash-stats tbody");
  await loadTable(tbody, () => api("GET", "stats"), ({ ops }) => {
    tbody.innerHTML = ops.map(o => `<tr><td>${esc(o.action)}</td><td>${o.calls}</td>
      <td>${o.error_pct ?? 0}</td><td>${o.p50_ms ?? "—"}</td><td>${o.p95_ms ?? "—"}</td>
      <td>${o.recall_empty_pct ?? ""}</td></tr>`).join("") || emptyRow(6, `No API calls in last 24h.`);
  }, loadStats);
}
async function loadUsage() {
  const tbody = $("#dash-usage tbody");
  await loadTable(tbody, () => api("GET", "usage"), ({ usage }) => {
    tbody.innerHTML = (usage || []).map(o => `<tr><td>${esc(o.op)}</td><td>${o.n}</td>
      <td>${o.tin ?? 0}</td><td>${o.tout ?? 0}</td><td>${o.cost ?? 0}</td></tr>`).join("") || emptyRow(5, `No usage recorded yet.`);
  }, loadUsage);
}
const AUDIT_PAGE = 100;
let auditOffset = 0;
async function loadAudit() {
  const tbody = $("#dash-audit tbody");
  await loadTable(tbody, () => api("GET", `audit?limit=${AUDIT_PAGE}&offset=${auditOffset}`), (j) => {
    tbody.innerHTML = j.audit.map(r => `<tr><td>${fmt(r.ts)}</td><td>${r.principal_id ?? "—"}</td>
      <td>${esc(r.action)}</td><td>${esc(r.namespace || "")}</td>
      <td>${r.status ?? (r.ok ? 200 : "—")}</td><td>${r.latency_ms ?? ""}</td></tr>`).join("") || emptyRow(6, `No activity yet.`);
    const total = j.total ?? j.audit.length;
    const page = Math.floor(auditOffset / AUDIT_PAGE) + 1;
    const pages = Math.max(1, Math.ceil(total / AUDIT_PAGE));
    $("#audit-info").textContent = `page ${page} of ${pages} · ~${total.toLocaleString()} events`;
    $("#audit-prev").disabled = auditOffset === 0;
    $("#audit-next").disabled = j.audit.length < AUDIT_PAGE;
  }, loadAudit);
}
$("#audit-prev").onclick = () => { auditOffset = Math.max(0, auditOffset - AUDIT_PAGE); loadAudit(); };
$("#audit-next").onclick = () => { auditOffset += AUDIT_PAGE; loadAudit(); };
function loadDashboard() { loadHealth(); loadStats(); loadUsage(); auditOffset = 0; loadAudit(); }

async function loadSecrets() {
  const tbody = $("#sec-table tbody");
  await loadTable(tbody, () => api("GET", "secrets"), (j) => {
    $("#vault-state").textContent = j.unlocked ? "· unlocked" : "· LOCKED";
    tbody.innerHTML = (j.secrets || []).map(s => `<tr>
      <td><code>${esc(s.name)}</code></td><td>${esc(s.description || "")}</td><td>${fmt(s.updated_at)}</td>
      <td><button class="danger" data-ds="${esc(s.name)}">delete</button></td></tr>`).join("") || emptyRow(4, `No secrets stored.`);
    $$("#sec-table [data-ds]").forEach(b => b.onclick = async () => {
      if (!confirm(`Delete secret "${b.dataset.ds}"?`)) return;
      await api("DELETE", "secrets?name=" + encodeURIComponent(b.dataset.ds)); loadSecrets();
    });
  }, loadSecrets);
}
$("#sec-set").onclick = async () => {
  const name = $("#sec-name").value.trim(), value = $("#sec-value").value;
  if (!name || !value) return;
  try {
    await api("POST", "secrets", { name, value, description: $("#sec-desc").value.trim() || null });
    $("#sec-name").value = ""; $("#sec-value").value = ""; $("#sec-desc").value = ""; $("#sec-err").textContent = "";
    loadSecrets();
  } catch (e) { $("#sec-err").textContent = e.message; }
};

async function loadSettings() {
  const box = $("#set-provider");
  box.innerHTML = `<span class="muted"><span class="spinner"></span> loading…</span>`;
  try {
    const p = await api("GET", "provider");
    box.innerHTML = `Mode: <b>${esc(p.mode)}</b> · embedding dim <b>${p.dim}</b> ·
      extract model <code>${esc(p.extract_model)}</code> · OpenAI key ${p.key_present ? '<span class="pill ok">present</span>' : '<span class="pill no">missing</span>'}`;
  } catch (e) { box.innerHTML = `<span class="errbox">${esc(e.message)}</span>`; }
}

let cliLoaded = false;
async function loadCliRef() {
  if (cliLoaded) return;
  const box = $("#cli-ref");
  box.innerHTML = `<span class="muted"><span class="spinner"></span> loading…</span>`;
  try {
    const r = await fetch("/admin/cli-reference.json");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    box.innerHTML = (j.groups || []).map(g => `
      <h4>${esc(g.title)}</h4>
      ${g.commands.map(c => `
        <details class="cli-cmd">
          <summary><code>memnos ${esc(c.command)}</code> <span class="muted">— ${esc(c.help)}</span></summary>
          ${c.args && c.args.length ? `<table><tbody>${c.args.map(a =>
            `<tr><td><code>${esc(a.arg)}</code>${a.positional && !a.required ? ' <span class="muted">(optional)</span>' : ""}</td>
             <td>${esc(a.help || "")}${a.choices ? ' <span class="muted">[' + a.choices.map(esc).join(" | ") + "]</span>" : ""}</td></tr>`).join("")}</tbody></table>` : ""}
          ${c.example ? `<pre><code>${esc(c.example)}</code></pre>` : ""}
        </details>`).join("")}`).join("");
    cliLoaded = true;
  } catch (e) {
    box.innerHTML = `<span class="errbox">couldn't load CLI reference (${esc(e.message)})</span>`;
  }
}

async function loadUsagePanel() {
  const days = $("#usage-days").value;
  const ns   = $("#usage-ns").value.trim();
  const qs   = `days=${days}` + (ns ? `&namespace=${encodeURIComponent(ns)}` : "");
  const banner = $("#usage-budget-banner");
  banner.hidden = true;
  const [usageData, budgetData] = await Promise.all([
    api("GET", `usage?${qs}`),
    api("GET", "budget").catch(() => null),
  ]);
  if (usageData && !usageData.error) {
    const d = usageData;
    $("#usage-totals").textContent = `Total: $${(d.total_usd ?? 0).toFixed(4)} | ${(d.total_tokens ?? 0).toLocaleString()} tokens`;
    $("#usage-op-table tbody").innerHTML = Object.entries(d.by_op ?? {}).map(([op, v]) =>
      `<tr><td>${esc(op)}</td><td>${v.calls ?? 0}</td><td>${v.tokens_in ?? 0}</td><td>${v.tokens_out ?? 0}</td><td>${(v.usd ?? 0).toFixed(4)}</td></tr>`
    ).join("") || emptyRow(5, "No usage recorded yet.");
    $("#usage-ns-table tbody").innerHTML = Object.entries(d.by_namespace ?? {}).map(([n, v]) =>
      `<tr><td>${esc(n)}</td><td>${v.calls ?? 0}</td><td>${v.tokens ?? 0}</td><td>${(v.usd ?? 0).toFixed(4)}</td></tr>`
    ).join("") || emptyRow(4, "No namespace breakdown yet.");
    $("#usage-day-table tbody").innerHTML = (d.by_day ?? []).slice(-7).map(r =>
      `<tr><td>${esc(r.date)}</td><td>${r.tokens ?? 0}</td><td>${(r.usd ?? 0).toFixed(4)}</td></tr>`
    ).join("") || emptyRow(3, "No daily data yet.");
  }
  if (budgetData && !budgetData.error && budgetData.exceeded) {
    const msgs = [];
    if (!budgetData.daily_ok)   msgs.push(`Daily budget exceeded ($${(budgetData.daily_spend_usd ?? 0).toFixed(4)} / $${budgetData.daily_limit_usd})`);
    if (!budgetData.monthly_ok) msgs.push(`Monthly budget exceeded ($${(budgetData.monthly_spend_usd ?? 0).toFixed(4)} / $${budgetData.monthly_limit_usd})`);
    banner.textContent = msgs.join(" | ");
    banner.hidden = false;
    banner.style.cssText = "background:var(--warn-bg);border:1px solid var(--warn);padding:8px 12px;border-radius:8px;margin-bottom:12px;";
  }
}
$("#usage-refresh").addEventListener("click", loadUsagePanel);
$("#usage-days").addEventListener("change", loadUsagePanel);

// ---- Boot ----
$("#server-url").value = serverBase();
if (tok()) {
  connectWorkspace(tok(), serverBase());
} else {
  disconnectToLogin();
}
