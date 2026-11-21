"use strict";
// MEMNOS Studio Workspace — Split #Channels & @Handles, Active Filtering, 5 Themes, Real-Time SSE.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ---- Bootstrap Auth & Theme ----
const THEME_KEY = "memnos_ui_theme_v2";
const BOOT_INFO = window.__MEMNOS_BOOT__ || null;
const UI_INFO = window.__MEMNOS_UI__ || null;
const DEFAULT_LOCAL_URL = (BOOT_INFO && BOOT_INFO.local_url) || "http://127.0.0.1:8900";
const DEFAULT_LOCAL_TOKEN = (BOOT_INFO && BOOT_INFO.local_token) || "";

let serverVerState = { version: "", build: "" };

function getClientVersionInfo() {
  if (BOOT_INFO && BOOT_INFO.desktop) {
    return {
      kind: "App",
      fullLabel: "MEMNOS.app Desktop",
      version: String(BOOT_INFO.app_version || (UI_INFO && UI_INFO.version) || "").trim(),
      build: String(BOOT_INFO.app_build || (UI_INFO && UI_INFO.build) || "").trim(),
    };
  }
  return {
    kind: "UI",
    fullLabel: "Browser Web UI",
    version: String((UI_INFO && UI_INFO.version) || "").trim(),
    build: String((UI_INFO && UI_INFO.build) || "").trim(),
  };
}

function fmtVerTag(version, build, includeBuild = true) {
  const v = String(version || "").trim();
  const b = String(build || "").trim();
  if (!v) return "—";
  return includeBuild && b ? `v${v} (${b})` : `v${v}`;
}

function hasVersionMismatch(client, srv) {
  if (!client.version || !srv.version) return false;
  if (client.version !== srv.version) return true;
  if (client.build && srv.build && client.build !== srv.build) return true;
  return false;
}

function updateServerVersion(version, build) {
  const v = String(version || "").trim();
  const b = String(build || "").trim();
  if (!v && !b) return;
  if (v) serverVerState.version = v;
  if (build !== undefined && build !== null) serverVerState.build = b;
  renderVersionSurfaces();
}

function renderVersionSurfaces() {
  const client = getClientVersionInfo();
  const srv = serverVerState;
  const mismatch = hasVersionMismatch(client, srv);
  const hostShort = serverBase().replace(/^https?:\/\//, "");

  // 1. Topbar App version pill + Server switcher button
  const topBadge = $("#topbar-ver-badge");
  if (topBadge) {
    const cShort = fmtVerTag(client.version, client.build, false);
    const sShort = fmtVerTag(srv.version, srv.build, false);
    const label = mismatch
      ? (client.version !== srv.version
          ? `⚠ ${client.kind} ${cShort} ≠ Server ${sShort}`
          : `⚠ ${client.kind} #${client.build} ≠ Server #${srv.build}`)
      : `${client.kind} ${fmtVerTag(client.version || srv.version, client.build || srv.build, true)}`;
    if (topBadge.textContent !== label) topBadge.textContent = label;
    topBadge.classList.toggle("mismatch", mismatch);
    topBadge.title = `${client.fullLabel}: ${fmtVerTag(client.version, client.build, true)} · Server (${hostShort}): ${fmtVerTag(srv.version, srv.build, true)}${mismatch ? " — version mismatch! Restart MEMNOS.app or server so both match." : " — click for details"}`;
  }

  const swBtn = $("#switch-server");
  if (swBtn) {
    const srvSuffix = srv.version ? ` · Server v${srv.version}` : "";
    const swText = `${hostShort}${srvSuffix}`;
    if (swBtn.textContent !== swText) swBtn.textContent = swText;
    swBtn.title = `Connected server: ${serverBase()} (${fmtVerTag(srv.version, srv.build, true)}) — click to switch Local or Remote server`;
  }

  // 2. Persistent Bottom Bar right-edge version readout
  const footVer = $("#footer-version");
  if (footVer) {
    const cFull = fmtVerTag(client.version, client.build, true);
    const sFull = fmtVerTag(srv.version, srv.build, true);
    const footText = `${mismatch ? "⚠ " : ""}${client.kind} ${cFull} · Server ${sFull}`;
    if (footVer.textContent !== footText) footVer.textContent = footText;
    footVer.classList.toggle("mismatch", mismatch);
    footVer.title = mismatch
      ? `Version mismatch between ${client.fullLabel} (${cFull}) and Server (${sFull}). Click to inspect connection.`
      : `${client.fullLabel}: ${cFull} · Connected Server (${hostShort}): ${sFull}`;
  }

  // 3. Login / Onboarding screen version line
  const loginVer = $("#login-version-line");
  if (loginVer) {
    const cFull = fmtVerTag(client.version, client.build, true);
    const sPart = srv.version ? ` · Server ${fmtVerTag(srv.version, srv.build, true)}` : "";
    const lText = `${client.kind} ${cFull}${sPart}`;
    if (loginVer.textContent !== lText) loginVer.textContent = lText;
  }

  // 4. Server Connection Modal detailed side-by-side box
  const modalBox = $("#modal-version-box");
  if (modalBox) {
    const cFull = fmtVerTag(client.version, client.build, true);
    const sFull = fmtVerTag(srv.version, srv.build, true);
    const sig = `${client.kind}:${cFull}|${hostShort}:${sFull}|${mismatch}`;
    const statusPill = (!client.version || !srv.version)
      ? `<span class="pill">Checking…</span>`
      : (mismatch
          ? `<span class="pill warn">⚠ Out of sync — restart ${client.kind === "App" ? "MEMNOS.app" : "UI"} or server</span>`
          : `<span class="pill ok">✓ Versions in sync</span>`);
    setHtmlBySig(modalBox, sig, `
      <div class="ver-info-row"><span class="muted">${esc(client.fullLabel)}</span><code>${esc(cFull)}</code></div>
      <div class="ver-info-row"><span class="muted">Server (<code>${esc(hostShort)}</code>)</span><code>${esc(sFull)}</code></div>
      <div class="ver-info-row"><span class="muted">Sync Status</span>${statusPill}</div>
    `);
  }

  const docVer = srv.version || client.version;
  if (docVer) {
    const nextTitle = `MEMNOS v${docVer} · Workspace`;
    if (document.title !== nextTitle) document.title = nextTitle;
  }
}

async function probeServerHealthVersion(targetUrl) {
  const base = String(targetUrl || serverBase()).trim().replace(/\/+$/, "");
  if (!base) return;
  try {
    const r = await fetch(base + "/healthz", { cache: "no-store" });
    if (r.ok) {
      const j = await r.json();
      if (j && j.version) updateServerVersion(j.version, j.build || "");
    }
  } catch (_) {}
}

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

// ---- In-App Confirm & Toast (macOS WKWebView suppresses window.confirm / window.alert) ----
let toastTimer = null;
function showToast(msg, isError = false) {
  const el = $("#app-toast");
  if (!el) return;
  el.textContent = String(msg || "");
  el.className = "app-toast" + (isError ? " err-toast" : "");
  el.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 4500 : 2800);
}

function askRenameHandle(currentName) {
  return new Promise(resolve => {
    const modal = $("#rename-modal");
    const inp = $("#rename-input");
    const okBtn = $("#rename-save");
    const cancelBtn = $("#rename-cancel");
    if (!modal || !inp || !okBtn || !cancelBtn) { resolve(""); return; }
    $("#rename-title").textContent = `Rename @${currentName}`;
    inp.value = currentName;
    modal.hidden = false;
    const finish = (val) => {
      modal.hidden = true;
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); finish(""); }
      else if (ev.key === "Enter") { ev.preventDefault(); finish(inp.value.trim().replace(/^@/, "")); }
    };
    okBtn.onclick = () => finish(inp.value.trim().replace(/^@/, ""));
    cancelBtn.onclick = () => finish("");
    modal.onclick = (ev) => { if (ev.target === modal) finish(""); };
    document.addEventListener("keydown", onKey);
    inp.focus();
    inp.select();
  });
}

function askConfirm({ title = "Confirm Action", message = "", confirmLabel = "Delete", checkboxLabel = "", defaultChecked = false }) {
  return new Promise(resolve => {
    const modal = $("#confirm-modal");
    const okBtn = $("#confirm-ok");
    const cancelBtn = $("#confirm-cancel");
    const chkWrap = $("#confirm-extra-wrap");
    const chk = $("#confirm-extra-chk");
    const chkLbl = $("#confirm-extra-lbl");
    if (!modal || !okBtn || !cancelBtn) { resolve({ ok: true, checked: defaultChecked }); return; }
    $("#confirm-title").textContent = title;
    $("#confirm-body").textContent = message;
    okBtn.textContent = confirmLabel;
    if (checkboxLabel) {
      chkWrap.hidden = false;
      chk.checked = !!defaultChecked;
      chkLbl.textContent = checkboxLabel;
    } else {
      chkWrap.hidden = true;
      chk.checked = false;
    }
    modal.hidden = false;
    const finish = (ok) => {
      modal.hidden = true;
      document.removeEventListener("keydown", onKey);
      resolve({ ok, checked: !!chk.checked });
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
      else if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
    };
    okBtn.onclick = () => finish(true);
    cancelBtn.onclick = () => finish(false);
    modal.onclick = (ev) => { if (ev.target === modal) finish(false); };
    document.addEventListener("keydown", onKey);
    okBtn.focus();
  });
}
const age = (t) => {
  if (!t) return "—";
  const s = Math.max(0, (Date.now() - new Date(t).getTime()) / 1000);
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
};

// ---- Anti-Flicker & Focus-Preserving DOM Helpers ----
function hasActiveControl(root) {
  const act = document.activeElement;
  if (!act || !root || !root.contains(act)) return false;
  const tag = act.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function tickRelativeClocks(root = document) {
  $$("[data-ago-iso]", root).forEach(el => {
    const iso = el.getAttribute("data-ago-iso");
    if (iso) {
      const next = fmtAgoCompact(iso);
      if (el.textContent !== next) el.textContent = next;
    }
  });
  $$("[data-age-iso]", root).forEach(el => {
    const iso = el.getAttribute("data-age-iso");
    if (iso) {
      const next = age(iso) + " ago";
      if (el.textContent !== next) el.textContent = next;
    }
  });
}

function setHtmlBySig(el, sig, html) {
  if (!el) return false;
  if (el._renderSig === sig) {
    tickRelativeClocks(el);
    return false;
  }
  if (hasActiveControl(el)) return false;
  el._renderSig = sig;
  el.innerHTML = html;
  return true;
}

function syncSelectOptions(sel, optionItems, preferredValue) {
  if (!sel || document.activeElement === sel) return;
  const sig = optionItems.map(o => `${o.value}\u0001${o.label}`).join("\u0002");
  if (sel._optSig !== sig) {
    sel._optSig = sig;
    sel.innerHTML = optionItems
      .map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`)
      .join("");
  }
  if (preferredValue !== undefined && [...sel.options].some(o => o.value === preferredValue)) {
    if (sel.value !== preferredValue) sel.value = preferredValue;
  }
}
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
  renderVersionSurfaces();
  probeServerHealthVersion(cur);
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

function openServerModal() {
  const cur = serverBase();
  $("#modal-url").value = cur;
  $("#modal-token").value = "";
  $("#modal-err").textContent = "";
  setModalMode(isLoopbackUrl(cur) ? "local" : "remote");
  renderVersionSurfaces();
  probeServerHealthVersion(cur);
  $("#server-modal").hidden = false;
}
$("#switch-server").onclick = openServerModal;
if ($("#topbar-ver-badge")) $("#topbar-ver-badge").onclick = openServerModal;
if ($("#footer-version")) {
  $("#footer-version").onclick = (ev) => {
    ev.stopPropagation();
    openServerModal();
  };
}
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
  if (!pill || !txt) return;
  const recent = liveEvents.length ? liveEvents.slice(0, 3) : (lastTopbarEvent ? [lastTopbarEvent] : []);
  if (!recent.length) return;
  txt.innerHTML = recent.map((ev, idx) => {
    const actor = ev.actor ? ("@" + String(ev.actor).replace(/^@/, "")) : "@client";
    const agoLbl = fmtAgoCompact(ev.ts);
    const rawDet = String(ev.detail || "").trim();
    const maxLen = idx === 0 ? 64 : 28;
    const shortDet = rawDet ? (rawDet.length > maxLen ? rawDet.slice(0, maxLen) + "…" : rawDet) : "";
    const nsPart = ev.namespace ? ` <span class="muted">(${esc(ev.namespace)})</span>` : "";
    const detPart = shortDet ? ` · <span class="${idx === 0 ? "" : "muted"}">${esc(shortDet)}</span>` : "";
    const msPart = (idx === 0 && ev.latency_ms != null) ? ` · <span class="muted">${ev.latency_ms}ms</span>` : "";
    return `<span class="ticker-item ${idx === 0 ? "primary" : ""}">` +
      `${idx === 0 ? "⚡ " : ""}<b>${esc(actor)}</b> <code>${esc(ev.op || "op")}</code>${idx === 0 ? nsPart : ""}${detPart}${msPart} · <span class="muted">${esc(agoLbl)}</span>` +
      `</span>`;
  }).join(`<span class="ticker-sep">│</span>`);
  const lead = recent[0];
  const leadActor = lead.actor ? ("@" + String(lead.actor).replace(/^@/, "")) : "@client";
  pill.title = `Latest: ${leadActor} · ${lead.op} (${lead.namespace || "*"}) · ${lead.detail || "—"} · ${lead.latency_ms ?? 0}ms — click to open full Live stream`;
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

function isWsOpen() {
  return !!(wsSocket && wsSocket.readyState === WebSocket.OPEN);
}

function isSseOpen() {
  return !!(sseSource && sseSource.readyState === EventSource.OPEN);
}

function handleLiveActivity(ev) {
  if (ev.id && Number(ev.id) > lastActivityId) {
    lastActivityId = Number(ev.id);
  }
  recordActorLive(ev);
  if (!livePaused) {
    const k = eventDedupKey(ev);
    if (!liveEvents.slice(0, 8).some(x => eventDedupKey(x) === k)) {
      ev._fresh = true;
      liveEvents.unshift(ev);
      if (liveEvents.length > 250) liveEvents.length = 250;
      if (activeSection === "live") renderLiveTable();
    }
  }
  updateLiveTopbarPill(ev, true);
  const op = String(ev.op || "");
  if (wsData && activeSection === "handles" && selectedHandle && String(ev.actor || "").replace(/^@/, "") === selectedHandle.name) {
    if (op === "headless/step") {
      // Append live step line directly in-place without re-fetching or rebuilding the DOM!
      const cScroll = $("#hd-console-scroll");
      if (cScroll && ev.detail) {
        const tsHms = new Date(ev.ts || Date.now()).toISOString().slice(11, 19);
        const lineDiv = document.createElement("div");
        lineDiv.className = "headless-log-line";
        lineDiv.textContent = `[${tsHms}] ${ev.detail}`;
        cScroll.appendChild(lineDiv);
        while (cScroll.children.length > 50) cScroll.removeChild(cScroll.firstChild);
        cScroll.scrollTop = cScroll.scrollHeight;
      }
    } else if (!op.startsWith("session/heartbeat")) {
      loadHandleMessages(false);
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
  // Only refresh overview/messages on real state-changing mutations (NEVER on heartbeats, polls, or recalls!)
  const isStateMutation =
    op.includes("remember") ||
    op.startsWith("memory/") ||
    op === "consolidate" ||
    op === "session/register" ||
    op === "session/unregister" ||
    op === "session/rename" ||
    op === "session/quota" ||
    op === "channel/publish" ||
    op === "channel/ack" ||
    op === "channel/subscribe" ||
    op === "channel/unsubscribe" ||
    op === "channel/create" ||
    op === "channel/delete" ||
    op === "channel/registry" ||
    op.startsWith("inbox/") ||
    (op.startsWith("headless/") && op !== "headless/step");

  if (isStateMutation) {
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

let refreshInFlight = false;
let refreshQueued = false;

function scheduleRealtimeRefresh() {
  if (sseRefreshTimer) return;
  sseRefreshTimer = setTimeout(async () => {
    sseRefreshTimer = null;
    if (refreshInFlight) {
      refreshQueued = true;
      return;
    }
    refreshInFlight = true;
    try {
      await refreshOverview(false);
      if (activeSection === "channels" && selectedChannel) await loadChannelMessages(false);
      if (activeSection === "handles" && selectedHandle) await loadHandleMessages(false);
    } catch (_) {
    } finally {
      refreshInFlight = false;
      if (refreshQueued) {
        refreshQueued = false;
        scheduleRealtimeRefresh();
      }
    }
  }, 450);
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
        if (d.type === "connected" && d.version) updateServerVersion(d.version, d.build || "");
        else if (d.type === "refresh") scheduleRealtimeRefresh();
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
        if (d.type === "connected" && d.version) updateServerVersion(d.version, d.build || "");
        else if (d.type === "refresh") scheduleRealtimeRefresh();
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
  // Default: Persistent two-way WebSocket (/workspace/ws) — ZERO HTTP polling while connected!
  connectWebSocket();
  liveDeltaTimer = setInterval(() => {
    if (isWsOpen()) {
      try { wsSocket.send(JSON.stringify({ type: "ping" })); } catch (_) {}
    } else if (!isSseOpen()) {
      pollActivityDelta();
    }
  }, 15000);
  liveClockTimer = setInterval(() => {
    renderTopbarPillText();
    tickRelativeClocks(document);
  }, 15000);
  pollFallbackTimer = setInterval(() => {
    // Fallback HTTP poll runs ONLY if both WebSocket and SSE streams are disconnected
    if (isWsOpen() || isSseOpen()) return;
    refreshOverview(false).catch(() => {});
    if (activeSection === "channels" && selectedChannel) loadChannelMessages(false).catch(() => {});
    if (activeSection === "handles" && selectedHandle) loadHandleMessages(false).catch(() => {});
  }, 10000);
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

// ---- Harness & Attached Model Identity Helpers ----
function normalizeHarness(raw, meta) {
  let h = String(raw || "").trim().toLowerCase();
  if (!h && meta && typeof meta.display_label === "string") {
    const dl = meta.display_label.toLowerCase();
    if (dl.includes("claude desktop")) h = "claude-desktop";
    else if (dl.includes("claude")) h = "claude-code";
    else if (dl.includes("antigravity")) h = "antigravity";
    else if (dl.includes("cursor")) h = "cursor";
    else if (dl.includes("windsurf")) h = "windsurf";
    else if (dl.includes("codex")) h = "codex";
    else if (dl.includes("gemini")) h = "gemini";
  }
  if (!h) return null;
  if (h === "claude-desktop" || h.includes("claude desktop")) return { key: "claude-desktop", label: "Claude Desktop", cls: "claude" };
  if (h.includes("claude")) return { key: "claude-code", label: "Claude Code", cls: "claude" };
  if (h.includes("antigravity") || h === "agy") return { key: "antigravity", label: "Antigravity", cls: "agy" };
  if (h.includes("cursor")) return { key: "cursor", label: "Cursor", cls: "cursor" };
  if (h.includes("windsurf")) return { key: "windsurf", label: "Windsurf", cls: "cursor" };
  if (h.includes("codex")) return { key: "codex", label: "Codex", cls: "codex" };
  if (h.includes("gemini")) return { key: "gemini", label: "Gemini", cls: "gemini" };
  return { key: h, label: raw, cls: "mcp" };
}

function formatModelShort(raw) {
  const m = String(raw || "").trim();
  if (!m || m === "<synthetic>") return "";
  // Trim trailing date suffixes like -20250929 while keeping clean family+version
  return m.replace(/-\d{8}$/, "");
}

function modelBadgeForSession(s) {
  const m = formatModelShort(s?.model);
  if (!m) return "";
  return `<span class="model-pill" title="Currently attached model: ${esc(s.model)}">${esc(m)}</span>`;
}

function harnessBadgeForSession(s, includeModel = true) {
  const info = normalizeHarness(s?.harness, s?.metadata);
  const hPill = info ? `<span class="harness-pill ${info.cls}" title="Attached harness: ${esc(info.label)}">${esc(info.label)}</span>` : "";
  const mPill = includeModel ? modelBadgeForSession(s) : "";
  const hlPill = s?.metadata?.headless ? `<span class="headless-pill" title="Non-TTY background worker launched by MEMNOS">HEADLESS</span>` : "";
  const qPill = s?.status === "quota_paused" ? `<span class="quota-pill" title="Subscription quota paused — open tasks released for failover">QUOTA PAUSED</span>` : "";
  const parts = [hPill, mPill, hlPill, qPill].filter(Boolean);
  if (parts.length > 1) return `<span style="display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap;min-width:0">${parts.join("")}</span>`;
  return parts[0] || "";
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

  const v = data.vitals || {};
  if (v.version) updateServerVersion(v.version, v.build || "");
  else renderVersionSurfaces();
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

  const memStr = v.footprint_mb != null ? `${v.footprint_mb} MB` : (v.rss_mb != null ? `${v.rss_mb} MB` : "—");
  const dbStr = v.db_size_bytes != null ? fmtBytes(v.db_size_bytes) : "scoped";
  const vitalsHtml =
    `<span>RAM <b>${esc(memStr)}</b></span>` +
    `<span>DB <b>${esc(dbStr)}</b></span>` +
    `<span>Facts <b>${(v.total_facts || 0).toLocaleString()}</b></span>` +
    `<span>Turns <b>${(v.total_turns || 0).toLocaleString()}</b></span>`;
  setHtmlBySig($("#vitals-strip"), vitalsHtml, vitalsHtml);

  // Smart Namespace Selector — sync only when option list changes & never overwrite while focused
  const prevNs = $("#ws-ns-filter").value || "*";
  const allNs = (data.namespaces || []).filter(n => !n.name.startsWith("test:"));
  const activeNs = allNs.filter(n => (n.facts || 0) > 0 || (n.turns || 0) > 0
    || (data.sessions || []).some(s => s.namespace === n.name && s.alive)
    || (data.channels || []).some(c => c.namespace === n.name && (c.message_count || 0) > 0));
  const nsItems = [{ value: "*", label: `All namespaces (${activeNs.length} active)` }]
    .concat((activeNs.length ? activeNs : allNs).map(n => ({
      value: n.name,
      label: `${n.name} · ${(n.facts || 0).toLocaleString()} facts`,
    })));
  syncSelectOptions($("#ws-ns-filter"), nsItems, prevNs);

  // Sync Constraints namespace selector
  const csSel = $("#cs-ns-select");
  if (csSel) {
    const prevCs = csSel.value || "*";
    const csItems = [{ value: "*", label: "All authorized namespaces (*)" }]
      .concat(allNs.map(n => ({ value: n.name, label: `${n.name} (+ inherited)` })));
    syncSelectOptions(csSel, csItems, prevCs);
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
    const hfItems = [{ value: "", label: "All harnesses" }]
      .concat([...seenH.entries()].map(([k, lbl]) => ({ value: k, label: lbl })));
    syncSelectOptions(hfSel, hfItems, prevHf);
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

// ---- Sidebar Sort & Custom Order Helpers (Name A-Z default — zero jumping!) ----
let chSortMode = localStorage.getItem("memnos_ch_sort_v1") || "name";
let hdSortMode = localStorage.getItem("memnos_hd_sort_v1") || "name";
let draggedChKey = null;
let draggedHdKey = null;

function loadCustomOrder(storageKey) {
  try {
    const raw = JSON.parse(localStorage.getItem(storageKey) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch (_) { return []; }
}
function saveCustomOrder(storageKey, arr) {
  localStorage.setItem(storageKey, JSON.stringify(arr));
}
function reorderArrayItem(order, keysPresent, targetKey, deltaOrDestKey, isDropBefore = false) {
  const merged = [...order.filter(k => keysPresent.includes(k)), ...keysPresent.filter(k => !order.includes(k))];
  const fromIdx = merged.indexOf(targetKey);
  if (fromIdx < 0) return merged;
  if (typeof deltaOrDestKey === "number") {
    const toIdx = Math.max(0, Math.min(merged.length - 1, fromIdx + deltaOrDestKey));
    if (toIdx === fromIdx) return merged;
    merged.splice(fromIdx, 1);
    merged.splice(toIdx, 0, targetKey);
    return merged;
  }
  const destIdx = merged.indexOf(deltaOrDestKey);
  if (destIdx < 0 || destIdx === fromIdx) return merged;
  merged.splice(fromIdx, 1);
  const newDest = merged.indexOf(deltaOrDestKey);
  merged.splice(isDropBefore ? newDest : newDest + 1, 0, targetKey);
  return merged;
}

if ($("#ch-sort-mode")) {
  $("#ch-sort-mode").value = chSortMode;
  $("#ch-sort-mode").onchange = (e) => {
    const next = e.target.value || "name";
    if (next === "custom" && loadCustomOrder("memnos_ch_custom_order").length === 0 && wsData?.channels) {
      saveCustomOrder("memnos_ch_custom_order", sortedChannels(filteredChannels()).map(c => c.channel));
    }
    chSortMode = next;
    localStorage.setItem("memnos_ch_sort_v1", chSortMode);
    renderChannelList();
  };
}

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

function sortedChannels(list) {
  const arr = [...list];
  if (chSortMode === "activity") {
    return arr.sort((a, b) => {
      const am = a.last_message_at ? new Date(a.last_message_at).getTime() : 0;
      const bm = b.last_message_at ? new Date(b.last_message_at).getTime() : 0;
      if (bm !== am) return bm - am;
      return String(a.channel).localeCompare(String(b.channel));
    });
  }
  if (chSortMode === "custom") {
    const custom = loadCustomOrder("memnos_ch_custom_order");
    const rank = new Map(custom.map((k, i) => [k, i]));
    return arr.sort((a, b) => {
      const ra = rank.has(a.channel) ? rank.get(a.channel) : 999999;
      const rb = rank.has(b.channel) ? rank.get(b.channel) : 999999;
      if (ra !== rb) return ra - rb;
      return String(a.channel).localeCompare(String(b.channel));
    });
  }
  // Default: Name (A–Z) — rock-solid sidebar, zero jumping on live traffic
  return arr.sort((a, b) => String(a.channel).localeCompare(String(b.channel)));
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

  const list = sortedChannels(filteredChannels());
  $("#ch-pane-sub").textContent = `${list.length} shown · ${actCnt} active`;
  const box = $("#channel-list");

  if (!list.length) {
    const emptySig = `empty:${chFilterMode}:${all.length}`;
    if (setHtmlBySig(box, emptySig, `<div class="empty-state" style="padding:28px 12px">
      <div class="muted">No channels match this filter.</div>
      ${chFilterMode !== "all" ? `<button class="ghost sm" id="ch-show-all-btn" style="margin-top:8px">Show all (${all.length})</button>` : ""}
    </div>`)) {
      const btn = $("#ch-show-all-btn");
      if (btn) btn.onclick = () => { $$("#ch-mode-tabs button")[2].click(); };
    }
    return;
  }

  const isCustom = chSortMode === "custom";
  const sig = `${chSortMode}|` + list.map(c => {
    const isSel = selectedChannel && selectedChannel.channel === c.channel ? 1 : 0;
    return `${c.channel}:${c.namespace}:${isSel}:${c.open_tasks || 0}:${liveSubscribersFor(c).length}:${c.message_count || 0}:${(c.subscribers || []).length}:${c.last_message_at || ""}`;
  }).join("|");
  let html = "";
  for (const c of list) {
    const isSel = selectedChannel && selectedChannel.channel === c.channel;
    const liveSubs = liveSubscribersFor(c);
    const tasks = c.open_tasks || 0;
    const msgTimeSpan = c.last_message_at
      ? `<span data-ago-iso="${esc(c.last_message_at)}">${esc(fmtAgoCompact(c.last_message_at))}</span>`
      : `<span>${(c.message_count || 0) > 0 ? "recent" : "no msgs"}</span>`;
    html += `<button class="list-card ${isSel ? "active" : ""}" data-ns="${esc(c.namespace)}" data-ch="${esc(c.channel)}" ${isCustom ? 'draggable="true"' : ""}>
      <div class="list-card-top">
        <span class="list-card-name">${isCustom ? '<span class="drag-grip" title="Drag to reorder">⠿</span>' : ""}${esc(c.channel)}</span>
        <span style="display:inline-flex;align-items:center;gap:4px;flex-shrink:0">
          ${tasks > 0
            ? `<span class="tiny-pill warn">${tasks} task${tasks > 1 ? "s" : ""}</span>`
            : (liveSubs.length > 0 ? `<span class="tiny-pill live">● ${liveSubs.length} live</span>` : `<span class="tiny-pill">${c.message_count || 0} msgs</span>`)}
          ${isCustom ? `<span class="reorder-btns"><span class="reorder-btn" data-ch-move="-1" title="Move up">↑</span><span class="reorder-btn" data-ch-move="1" title="Move down">↓</span></span>` : ""}
        </span>
      </div>
      <div class="list-card-sub">
        <span>${c.message_count || 0} msgs · ${(c.subscribers || []).length} subs</span>
        ${msgTimeSpan}
      </div>
    </button>`;
  }
  if (!setHtmlBySig(box, sig, html)) return;

  const visibleKeys = list.map(x => x.channel);
  $$("#channel-list .list-card").forEach(card => {
    card.onclick = async (ev) => {
      const mv = ev.target.closest("[data-ch-move]");
      if (mv) {
        ev.stopPropagation();
        const nextOrder = reorderArrayItem(loadCustomOrder("memnos_ch_custom_order"), visibleKeys, card.dataset.ch, Number(mv.dataset.chMove));
        saveCustomOrder("memnos_ch_custom_order", nextOrder);
        renderChannelList();
        return;
      }
      selectedChannel = { namespace: card.dataset.ns, channel: card.dataset.ch };
      renderChannelList();
      renderChannelHeaderAndInspector();
      await loadChannelMessages(true);
    };
    if (isCustom) {
      card.ondragstart = (ev) => { draggedChKey = card.dataset.ch; ev.dataTransfer.effectAllowed = "move"; };
      card.ondragend = () => { draggedChKey = null; card.classList.remove("drag-over"); };
      card.ondragover = (ev) => { ev.preventDefault(); card.classList.add("drag-over"); };
      card.ondragleave = () => card.classList.remove("drag-over");
      card.ondrop = (ev) => {
        ev.preventDefault();
        card.classList.remove("drag-over");
        if (draggedChKey && draggedChKey !== card.dataset.ch) {
          const rect = card.getBoundingClientRect();
          const before = (ev.clientY - rect.top) < rect.height / 2;
          const nextOrder = reorderArrayItem(loadCustomOrder("memnos_ch_custom_order"), visibleKeys, draggedChKey, card.dataset.ch, before);
          saveCustomOrder("memnos_ch_custom_order", nextOrder);
          renderChannelList();
        }
        draggedChKey = null;
      };
    }
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
  } catch (e) { showToast(e.message, true); }
};

// ---- Headless Harness Spawner Modal ----
let spawnerStatusCache = null;

async function refreshSpawnerModalRuns() {
  const box = $("#spawn-active-runs");
  const sel = $("#spawn-harness");
  if (!sel) return;
  try {
    const st = await wapi("GET", "spawner/status");
    spawnerStatusCache = st;
    const prevVal = sel.value;
    const harnesses = st.harnesses || [];
    const sorted = [...harnesses].sort((a, b) => (b.installed ? 1 : 0) - (a.installed ? 1 : 0));
    sel.innerHTML = sorted.map(h =>
      `<option value="${esc(h.key)}" ${h.installed ? "" : "disabled"}>${h.installed ? "✓ " : ""}${esc(h.label)} (${esc(h.binary_name)})${h.installed ? "" : " — not installed"}</option>`
    ).join("");
    if (prevVal && [...sel.options].some(o => o.value === prevVal && !o.disabled)) {
      sel.value = prevVal;
    } else {
      const firstInst = sorted.find(h => h.installed);
      if (firstInst) sel.value = firstInst.key;
    }

    const running = (st.runs || []).filter(r => r.status === "running");
    if (box) {
      if (!running.length) {
        box.hidden = true;
        box.innerHTML = "";
      } else {
        box.hidden = false;
        box.innerHTML = `<div class="row" style="justify-content:space-between">
          <b>Running Headless Workers (${running.length}/${st.max_concurrent || 4})</b>
        </div>` + running.map(r => `<div class="row" style="justify-content:space-between;gap:8px">
          <span><code>@${esc(r.handle)}</code> · <span class="muted">${esc(r.harness)}${r.channel ? " · " + esc(r.channel) : ""}</span></span>
          <button type="button" class="danger sm" style="padding:2px 8px;font-size:11px" data-stop-run="${r.run_id}">⏹ Stop</button>
        </div>`).join("");
        $$("[data-stop-run]", box).forEach(b => {
          b.onclick = async () => {
            try {
              await wapi("POST", "spawner/stop", { run_id: Number(b.dataset.stopRun) });
              showToast("Stopped headless worker");
              await refreshSpawnerModalRuns();
              await refreshOverview(false);
            } catch (e) { showToast(e.message, true); }
          };
        });
      }
    }
  } catch (e) {
    if ($("#spawn-err")) $("#spawn-err").textContent = e.message;
  }
}

async function openSpawnModal(defaults = {}) {
  const modal = $("#spawn-modal");
  if (!modal) return;
  $("#spawn-err").textContent = "";
  const curNs = $("#ws-ns-filter")?.value;
  const fallbackNs = (curNs && curNs !== "*")
    ? curNs
    : (selectedChannel?.namespace || selectedHandle?.namespace || wsData?.namespaces?.find(n => !n.name.startsWith("test:"))?.name || "proj:default");
  $("#spawn-ns").value = defaults.namespace || fallbackNs;
  $("#spawn-ch").value = defaults.channel !== undefined ? defaults.channel : (selectedChannel?.channel || "#general");
  $("#spawn-role").value = defaults.role || "";
  $("#spawn-cwd").value = defaults.cwd || "";
  $("#spawn-prompt").value = defaults.prompt || "";
  $("#spawn-sub-only").checked = true;
  $("#spawn-failover").checked = true;
  if ($("#spawn-skip-perms")) $("#spawn-skip-perms").checked = defaults.skipPermissions !== undefined ? !!defaults.skipPermissions : true;
  $("#spawn-worktree").checked = false;
  modal.hidden = false;
  await refreshSpawnerModalRuns();
  if (defaults.preferredHarness && [...$("#spawn-harness").options].some(o => o.value === defaults.preferredHarness && !o.disabled)) {
    $("#spawn-harness").value = defaults.preferredHarness;
  }
  $("#spawn-prompt").focus();
}

if ($("#btn-ch-spawn")) {
  $("#btn-ch-spawn").onclick = () => openSpawnModal({
    namespace: selectedChannel?.namespace,
    channel: selectedChannel?.channel || "",
  });
}
if ($("#btn-hd-spawn")) {
  $("#btn-hd-spawn").onclick = () => openSpawnModal({
    namespace: selectedHandle?.namespace,
    channel: selectedChannel?.channel || "#general",
  });
}
if ($("#spawn-close")) $("#spawn-close").onclick = () => { $("#spawn-modal").hidden = true; };
if ($("#spawn-cancel")) $("#spawn-cancel").onclick = () => { $("#spawn-modal").hidden = true; };
if ($("#spawn-modal")) {
  $("#spawn-modal").onclick = (ev) => { if (ev.target === $("#spawn-modal")) $("#spawn-modal").hidden = true; };
}
if ($("#spawn-submit")) {
  $("#spawn-submit").onclick = async () => {
    const ns = ($("#spawn-ns").value || "").trim();
    const harness = $("#spawn-harness").value;
    if (!ns || ns === "*") { $("#spawn-err").textContent = "Enter a concrete namespace (e.g. proj:payments)."; return; }
    if (!harness) { $("#spawn-err").textContent = "No installed CLI harness selected."; return; }
    $("#spawn-err").textContent = "";
    $("#spawn-submit").disabled = true;
    try {
      const res = await wapi("POST", "spawner/launch", {
        namespace: ns,
        harness,
        model: ($("#spawn-model").value || "").trim(),
        role: $("#spawn-role").value || "",
        channel: ($("#spawn-ch").value || "").trim(),
        cwd: ($("#spawn-cwd").value || "").trim(),
        prompt: ($("#spawn-prompt").value || "").trim(),
        subscription_only: $("#spawn-sub-only").checked,
        skip_permissions: !!$("#spawn-skip-perms")?.checked,
        auto_failover: $("#spawn-failover").checked,
        isolate_worktree: $("#spawn-worktree").checked,
      });
      const w = res.worker || {};
      $("#spawn-modal").hidden = true;
      showToast(`⚡ Launched @${w.handle || "worker"} (${w.harness || harness})`);
      await refreshOverview(false);
      if (activeSection === "channels" && selectedChannel) renderChannelHeaderAndInspector();
    } catch (e) {
      $("#spawn-err").textContent = e.message;
    } finally {
      $("#spawn-submit").disabled = false;
    }
  };
}

$("#toggle-ch-inspector").onclick = () => {
  inspectorOpen = !inspectorOpen;
  $("#ch-inspector").hidden = !inspectorOpen;
};

$("#delete-channel-btn").onclick = async () => {
  if (!selectedChannel) return;
  const { namespace, channel } = selectedChannel;
  const { ok } = await askConfirm({
    title: `Delete ${channel}?`,
    message: `Permanently delete channel "${channel}" across authorized namespaces?\n\nThis removes the channel, all handle subscriptions on ${channel}, and its message history.`,
    confirmLabel: "Delete Channel",
  });
  if (!ok) return;
  try {
    await wapi("DELETE", `channel?namespace=${encodeURIComponent(namespace)}&channel=${encodeURIComponent(channel)}`);
    selectedChannel = null;
    showToast(`Deleted ${channel}`);
    await refreshOverview(true);
  } catch (e) { showToast(e.message, true); }
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
  const avHtml = liveSubs.slice(0, 5).map(h =>
    `<span class="mini-av" style="background:${avatarColor(h)}" title="@${esc(h)} (online)">${esc(h.slice(0, 2).toUpperCase())}</span>`
  ).join("");
  setHtmlBySig($("#ch-live-avatars"), liveSubs.slice(0, 5).join(","), avHtml);

  // Right Inspector: separate Online vs Offline subscribers; attach inputs stay permanent in DOM!
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

  const inspSig = `${namespace}:${channel}:${writable}|${liveSubs.join(",")}|${offlineSubs.join(",")}`;
  const inspHtml = `
    <div class="insp-block">
      <div class="insp-hdr">Online Subscribers (${liveSubs.length})</div>
      ${liveSubs.length ? liveSubs.map(h => renderMemberRow(h, true)).join("") : `<div class="muted">No online handles subscribed right now.</div>`}
    </div>

    ${offlineSubs.length ? `
    <div class="insp-block">
      <div class="insp-hdr">Inactive / Offline Subscribers (${offlineSubs.length})</div>
      ${offlineSubs.map(h => renderMemberRow(h, false)).join("")}
    </div>` : ""}
  `;
  if (setHtmlBySig($("#ch-inspector-body"), inspSig, inspHtml)) {
    $$("#ch-inspector-body [data-unsub]").forEach(btn => {
      btn.onclick = async () => {
        try {
          await wapi("POST", "channel/unsubscribe", { namespace, channel, session_name: btn.dataset.unsub });
          await refreshOverview(false);
        } catch (e) { showToast(e.message, true); }
      };
    });
    $$("#ch-inspector-body [data-jump-handle]").forEach(el => {
      el.onclick = () => jumpToHandle(el.dataset.jumpNs, el.dataset.jumpHandle);
    });
  }

  // Update permanent attach box without ever destroying `#ch-insp-custom` or `#ch-insp-pick`!
  const attachBox = $("#ch-insp-attach-box");
  if (attachBox) {
    attachBox.hidden = !writable;
    if (writable) {
      if ($("#ch-insp-attach-hdr")) $("#ch-insp-attach-hdr").textContent = `Attach @Handle to ${channel}`;
      const pickSel = $("#ch-insp-pick");
      if (pickSel) {
        pickSel.hidden = !candidateHandles.length;
        if (candidateHandles.length) {
          const prevPick = pickSel.value || "";
          const pickItems = [{ value: "", label: "Choose online @handle…" }]
            .concat(candidateHandles.map(h => ({ value: h, label: `@${h}` })));
          syncSelectOptions(pickSel, pickItems, prevPick);
        }
      }
    }
  }
}

async function submitChannelInspectorSubscribe() {
  if (!selectedChannel) return;
  const { namespace, channel } = selectedChannel;
  const picked = $("#ch-insp-pick")?.value || "";
  const typed = ($("#ch-insp-custom")?.value || "").trim().replace(/^@/, "");
  const target = typed || picked;
  if (!target) return;
  try {
    await wapi("POST", "channel/subscribe", { namespace, channel, session_name: target });
    if ($("#ch-insp-custom")) $("#ch-insp-custom").value = "";
    if ($("#ch-insp-pick")) $("#ch-insp-pick").value = "";
    showToast(`Subscribed @${target} to ${channel}`);
    await refreshOverview(false);
  } catch (e) { showToast(e.message, true); }
}
if ($("#ch-insp-add")) $("#ch-insp-add").onclick = submitChannelInspectorSubscribe;
if ($("#ch-insp-custom")) {
  $("#ch-insp-custom").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); submitChannelInspectorSubscribe(); }
  });
}

async function loadChannelMessages(forceScrollBottom = false) {
  if (!selectedChannel) return;
  const box = $("#ch-messages");
  const pipeEl = $("#ch-pipeline-strip");
  const nearBottom = forceScrollBottom || (box.scrollHeight - box.scrollTop - box.clientHeight < 100);
  const { namespace, channel } = selectedChannel;
  try {
    const { messages } = await wapi("GET", `channel/messages?namespace=${encodeURIComponent(namespace)}&channel=${encodeURIComponent(channel)}&limit=150`);
    if (!messages || !messages.length) {
      if (pipeEl) { pipeEl.hidden = true; pipeEl._renderSig = ""; pipeEl.innerHTML = ""; }
      setHtmlBySig(box, `empty:${namespace}:${channel}`, `<div class="empty-state">
        <div class="empty-ico">#</div>
        <h3 style="margin:0 0 6px;color:var(--fg)">${esc(channel)}</h3>
        <p>No messages in <code>${esc(namespace)}</code> · <b>${esc(channel)}</b> yet.<br>Post below to broadcast and wake subscribed handles.</p>
      </div>`);
      return;
    }

    const isTaskDone = (m) => m.claimable && ["acked", "completed", "done", "acted"].includes(String(m.claim_status || "").toLowerCase());
    const doneIds = new Set(messages.filter(isTaskDone).map(m => Number(m.id)));
    const allTasks = messages.filter(m => m.claimable);
    if (pipeEl) {
      if (!allTasks.length) {
        pipeEl.hidden = true;
        pipeEl._renderSig = "";
        pipeEl.innerHTML = "";
      } else {
        let readyCnt = 0, blockedCnt = 0, claimedCnt = 0, doneCnt = 0;
        let prefHint = "";
        for (const t of allTasks) {
          if (isTaskDone(t)) { doneCnt++; continue; }
          const st = String(t.claim_status || "pending").toLowerCase();
          if (st === "claimed" || st === "read") { claimedCnt++; continue; }
          const deps = Array.isArray(t.metadata?.depends_on) ? t.metadata.depends_on.map(Number).filter(Boolean) : [];
          if (deps.some(id => !doneIds.has(id))) blockedCnt++;
          else {
            readyCnt++;
            if (!prefHint && typeof t.metadata?.preferred_harness === "string") prefHint = t.metadata.preferred_harness;
          }
        }
        pipeEl.hidden = false;
        const pipeSig = `${allTasks.length}:${readyCnt}:${claimedCnt}:${blockedCnt}:${doneCnt}:${!!wsData?.is_admin}`;
        const pipeHtml = `
          <div class="pipeline-counts">
            <b>Task Pipeline (${allTasks.length})</b>
            ${readyCnt > 0 ? `<span class="pill ok">${readyCnt} ready</span>` : ""}
            ${claimedCnt > 0 ? `<span class="pill warn">${claimedCnt} in progress</span>` : ""}
            ${blockedCnt > 0 ? `<span class="tiny-pill warn">⛓ ${blockedCnt} blocked</span>` : ""}
            <span class="tiny-pill">${doneCnt}/${allTasks.length} done</span>
          </div>
          ${readyCnt > 0 && wsData?.is_admin ? `<button type="button" id="btn-pipe-run-ready" class="btn-primary sm" style="padding:3px 10px;font-size:11.5px">⚡ Run ${readyCnt} Ready Headlessly</button>` : ""}
        `;
        if (setHtmlBySig(pipeEl, pipeSig, pipeHtml)) {
          const runBtn = $("#btn-pipe-run-ready");
          if (runBtn) {
            runBtn.onclick = () => openSpawnModal({
              namespace,
              channel,
              preferredHarness: prefHint,
              prompt: "",
            });
          }
        }
      }
    }

    const msgSig = `${namespace}:${channel}|` + messages.map(m =>
      `${m.id}:${m.claim_status || ""}:${m.claimed_by || ""}:${m.resolution || ""}`
    ).join("|");
    const msgHtml = messages.map(m => {
      const sender = m.sender_session || "agent";
      const initials = sender.replace(/^@/, "").slice(0, 2).toUpperCase();
      const isDone = isTaskDone(m);
      const cls = m.claimable ? (isDone ? "msg-row is-done" : "msg-row is-task") : "msg-row";
      const isPeerDm = m.source === "peer_dm" && m.to_session;
      let taskStrip = "";
      if (m.claimable) {
        const meta = (m.metadata && typeof m.metadata === "object") ? m.metadata : {};
        const deps = Array.isArray(meta.depends_on) ? meta.depends_on.map(Number).filter(Boolean) : [];
        const unmetDeps = deps.filter(id => !doneIds.has(id));
        const stRaw = String(m.claim_status || "pending").toLowerCase();
        const st = (!isDone && stRaw === "pending" && unmetDeps.length > 0) ? "BLOCKED" : stRaw.toUpperCase();
        const who = m.claimed_by ? ` · handled by <b>@${esc(m.claimed_by)}</b>` : (m.to_session ? ` · assigned to <b>@${esc(m.to_session)}</b>` : (unmetDeps.length ? ` · waiting on #${unmetDeps.join(", #")}` : " · ready for subscriber"));
        const res = m.resolution ? ` · <code>${esc(m.resolution)}</code>` : "";
        const routePills = [
          deps.length ? `<span class="tiny-pill ${unmetDeps.length ? "warn" : ""}" title="Task DAG dependencies">⛓ deps: #${deps.join(", #")}</span>` : "",
          meta.different_harness ? `<span class="tiny-pill tag" title="Must be claimed by a different CLI harness than the author">⇄ cross-harness</span>` : "",
          meta.preferred_harness ? `<span class="tiny-pill tag" title="Preferred harness (auto-falls back if offline or quota-paused)">🎯 ${esc(meta.preferred_harness)}</span>` : "",
          meta.handoff_from ? `<span class="tiny-pill warn" title="Auto-released when @${esc(meta.handoff_from)} hit subscription quota cap">↻ failover from @${esc(meta.handoff_from)}</span>` : "",
        ].filter(Boolean).join(" ");
        taskStrip = `<div class="task-strip">
          <span class="pill ${isDone ? "ok" : "warn"}">TASK · ${esc(st)}</span>
          ${routePills}
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
            <span class="msg-ts" title="${esc(m.created_at)}">${fmt(m.created_at)} (<span data-age-iso="${esc(m.created_at)}">${age(m.created_at)} ago</span>) · #${m.id}</span>
          </div>
          <div class="msg-body-text">${formatRichText(m.content)}</div>
          ${taskStrip}
        </div>
      </div>`;
    }).join("");
    if (setHtmlBySig(box, msgSig, msgHtml)) {
      $$("#ch-messages [data-author]").forEach(el => {
        el.onclick = () => jumpToHandle(el.dataset.ns, el.dataset.author);
      });
      if (nearBottom) box.scrollTop = box.scrollHeight;
    } else if (forceScrollBottom) {
      box.scrollTop = box.scrollHeight;
    }
  } catch (e) {
    box.innerHTML = `<div class="empty-state"><span class="err">${esc(e.message)}</span></div>`;
  }
}

if ($("#ch-comp-claimable")) {
  $("#ch-comp-claimable").onchange = () => {
    const opts = $("#ch-task-opts");
    if (opts) opts.hidden = !$("#ch-comp-claimable").checked;
  };
}

async function sendChannelMessage() {
  if (!selectedChannel) return;
  const text = $("#ch-comp-input").value.trim();
  if (!text) return;
  const sender = ($("#ch-comp-sender").value.trim().replace(/^@/, "")) || "owner";
  const claimable = $("#ch-comp-claimable").checked;
  const metadata = {};
  if (claimable) {
    const routeVal = ($("#ch-comp-route")?.value || "").trim();
    if (routeVal === "cross") metadata.different_harness = true;
    else if (routeVal) metadata.preferred_harness = routeVal;
    const rawDeps = ($("#ch-comp-deps")?.value || "").trim();
    if (rawDeps) {
      const deps = rawDeps.split(/[\s,]+/).map(x => Number(x.replace(/^#/, ""))).filter(n => Number.isFinite(n) && n > 0);
      if (deps.length) metadata.depends_on = deps;
    }
  }
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
      metadata,
    };
    try {
      await wsCall("publish_channel", payload);
    } catch (_) {
      await wapi("POST", "channel/publish", payload);
    }
    $("#ch-comp-input").value = "";
    $("#ch-comp-claimable").checked = false;
    if ($("#ch-task-opts")) $("#ch-task-opts").hidden = true;
    if ($("#ch-comp-route")) $("#ch-comp-route").value = "";
    if ($("#ch-comp-deps")) $("#ch-comp-deps").value = "";
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
if ($("#hd-sort-mode")) {
  $("#hd-sort-mode").value = hdSortMode;
  $("#hd-sort-mode").onchange = (e) => {
    const next = e.target.value || "name";
    if (next === "custom" && loadCustomOrder("memnos_hd_custom_order").length === 0 && wsData?.sessions) {
      saveCustomOrder("memnos_hd_custom_order", sortedHandles(filteredHandles()).map(s => `${s.namespace}::${s.name}`));
    }
    hdSortMode = next;
    localStorage.setItem("memnos_hd_sort_v1", hdSortMode);
    renderHandleList();
  };
}

function jumpToHandle(namespace, name) {
  selectedHandle = { namespace, name: name.replace(/^@/, "") };
  switchSection("handles");
}

function jumpToChannel(namespace, channel) {
  selectedChannel = { namespace, channel };
  switchSection("channels");
}

function isRecentHeadless(s) {
  if (!s?.metadata?.headless) return false;
  const hb = s.last_heartbeat_at ? new Date(s.last_heartbeat_at).getTime() : 0;
  return hb > 0 && (Date.now() - hb) < 30 * 60 * 1000;
}

function filteredHandles() {
  const all = (wsData?.sessions || []).filter(s => !s.namespace.startsWith("test:"));
  const q = ($("#hd-search").value || "").trim().toLowerCase();
  const hf = ($("#hd-harness-filter")?.value || "").trim();
  return all.filter(s => {
    const isSel = selectedHandle && selectedHandle.namespace === s.namespace && selectedHandle.name === s.name;
    if (hdFilterMode === "online" && !s.alive && !isSel && !isRecentHeadless(s)) return false;
    const hInfo = normalizeHarness(s.harness, s.metadata);
    if (hf && hInfo?.key !== hf) return false;
    if (q) {
      const hay = `@${s.name} ${hInfo?.label || ""} ${s.harness || ""} ${s.model || ""} ${s.namespace} ${s.cwd || ""} ${s.host || ""} ${(s.channels || []).join(" ")}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function handleBestMs(s) {
  const liveAct = actorLiveMap.get(s.name);
  const hbMs = s.last_heartbeat_at ? new Date(s.last_heartbeat_at).getTime() : 0;
  return (liveAct && liveAct.ms > hbMs) ? liveAct.ms : hbMs;
}

function sortedHandles(list) {
  const arr = [...list];
  if (hdSortMode === "activity") {
    return arr.sort((a, b) => {
      if (!!b.alive !== !!a.alive) return (b.alive ? 1 : 0) - (a.alive ? 1 : 0);
      const d = handleBestMs(b) - handleBestMs(a);
      if (d !== 0) return d;
      return String(a.name).localeCompare(String(b.name));
    });
  }
  if (hdSortMode === "custom") {
    const custom = loadCustomOrder("memnos_hd_custom_order");
    const rank = new Map(custom.map((k, i) => [k, i]));
    return arr.sort((a, b) => {
      const ka = `${a.namespace}::${a.name}`;
      const kb = `${b.namespace}::${b.name}`;
      const ra = rank.has(ka) ? rank.get(ka) : 999999;
      const rb = rank.has(kb) ? rank.get(kb) : 999999;
      if (ra !== rb) return ra - rb;
      return String(a.name).localeCompare(String(b.name));
    });
  }
  // Default: Name (A–Z) — prevents sidebar jumping
  return arr.sort((a, b) => {
    const c = String(a.name).localeCompare(String(b.name));
    return c !== 0 ? c : String(a.namespace).localeCompare(String(b.namespace));
  });
}

function renderHandleList() {
  if (!wsData) return;
  const all = (wsData.sessions || []).filter(s => !s.namespace.startsWith("test:"));
  const onlineCnt = all.filter(s => s.alive).length;
  const tabs = $$("#hd-mode-tabs button");
  if (tabs[0]) tabs[0].textContent = `Online (${onlineCnt})`;
  if (tabs[1]) tabs[1].textContent = `All (${all.length})`;

  const list = sortedHandles(filteredHandles());
  $("#hd-pane-sub").textContent = `${onlineCnt} online · ${list.length} shown`;
  const box = $("#handle-list");

  if (!list.length) {
    const emptySig = `empty:${hdFilterMode}:${all.length}`;
    if (setHtmlBySig(box, emptySig, `<div class="empty-state" style="padding:28px 12px">
      <div class="muted">No handles match this filter.</div>
      ${hdFilterMode !== "all" ? `<button class="ghost sm" id="hd-show-all-btn" style="margin-top:8px">Show offline (${all.length})</button>` : ""}
    </div>`)) {
      const btn = $("#hd-show-all-btn");
      if (btn) btn.onclick = () => { $$("#hd-mode-tabs button")[1].click(); };
    }
    return;
  }

  const isCustom = hdSortMode === "custom";
  const sig = `${hdSortMode}|` + list.map(s => {
    const isSel = selectedHandle && selectedHandle.namespace === s.namespace && selectedHandle.name === s.name ? 1 : 0;
    return `${s.namespace}::${s.name}:${isSel}:${s.alive ? 1 : 0}:${s.status || ""}:${s.run_state || ""}:${s.harness || ""}:${s.model || ""}:${s.unread_inbox || 0}:${(s.channels || []).join(",")}`;
  }).join("|");
  const html = list.map(s => {
    const isSel = selectedHandle && selectedHandle.namespace === s.namespace && selectedHandle.name === s.name;
    const liveAct = actorLiveMap.get(s.name);
    const hbMs = s.last_heartbeat_at ? new Date(s.last_heartbeat_at).getTime() : 0;
    const bestIso = (liveAct && liveAct.ms > hbMs) ? liveAct.ts : s.last_heartbeat_at;
    const dotCls = !s.alive ? "idle" : (s.status === "quota_paused" ? "paused" : (s.run_state === "running" || s.run_state === "busy" ? "busy" : "alive"));
    const chs = s.channels || [];
    const unread = s.unread_inbox || 0;
    const shortCwd = s.cwd ? s.cwd.split("/").slice(-2).join("/") : s.namespace;
    const hKey = `${s.namespace}::${s.name}`;
    return `<button class="list-card ${isSel ? "active" : ""}" data-ns="${esc(s.namespace)}" data-hd="${esc(s.name)}" data-hkey="${esc(hKey)}" ${isCustom ? 'draggable="true"' : ""}>
      <div class="list-card-top">
        <span class="list-card-name">${isCustom ? '<span class="drag-grip" title="Drag to reorder">⠿</span>' : ""}<span class="dot ${dotCls}"></span> @${esc(s.name)}</span>
        <span style="display:inline-flex;align-items:center;gap:4px;flex-shrink:0">
          ${unread > 0 ? `<span class="tiny-pill warn">${unread} unread</span>` : `<span class="tiny-pill" ${bestIso ? `data-ago-iso="${esc(bestIso)}"` : ""}>${esc(fmtAgoCompact(bestIso))}</span>`}
          ${isCustom ? `<span class="reorder-btns"><span class="reorder-btn" data-hd-move="-1" title="Move up">↑</span><span class="reorder-btn" data-hd-move="1" title="Move down">↓</span></span>` : ""}
        </span>
      </div>
      <div class="list-card-sub">
        <span>${harnessBadgeForSession(s) || esc(s.namespace)}</span>
        <span title="${esc(s.cwd || "")}">${esc(shortCwd)}</span>
      </div>
      ${chs.length ? `<div class="list-tags"><span class="tiny-pill">${esc(s.namespace)}</span>${chs.slice(0, 2).map(c => `<span class="tiny-pill tag">${esc(c)}</span>`).join("")}${chs.length > 2 ? `<span class="tiny-pill">+${chs.length - 2}</span>` : ""}</div>` : `<div class="list-tags"><span class="tiny-pill">${esc(s.namespace)}</span></div>`}
    </button>`;
  }).join("");
  if (!setHtmlBySig(box, sig, html)) return;

  const visibleKeys = list.map(s => `${s.namespace}::${s.name}`);
  $$("#handle-list .list-card").forEach(card => {
    card.onclick = async (ev) => {
      const mv = ev.target.closest("[data-hd-move]");
      if (mv) {
        ev.stopPropagation();
        const nextOrder = reorderArrayItem(loadCustomOrder("memnos_hd_custom_order"), visibleKeys, card.dataset.hkey, Number(mv.dataset.hdMove));
        saveCustomOrder("memnos_hd_custom_order", nextOrder);
        renderHandleList();
        return;
      }
      selectedHandle = { namespace: card.dataset.ns, name: card.dataset.hd };
      renderHandleList();
      renderHandleHero();
      await loadHandleMessages(true);
    };
    if (isCustom) {
      card.ondragstart = (ev) => { draggedHdKey = card.dataset.hkey; ev.dataTransfer.effectAllowed = "move"; };
      card.ondragend = () => { draggedHdKey = null; card.classList.remove("drag-over"); };
      card.ondragover = (ev) => { ev.preventDefault(); card.classList.add("drag-over"); };
      card.ondragleave = () => card.classList.remove("drag-over");
      card.ondrop = (ev) => {
        ev.preventDefault();
        card.classList.remove("drag-over");
        if (draggedHdKey && draggedHdKey !== card.dataset.hkey) {
          const rect = card.getBoundingClientRect();
          const before = (ev.clientY - rect.top) < rect.height / 2;
          const nextOrder = reorderArrayItem(loadCustomOrder("memnos_hd_custom_order"), visibleKeys, draggedHdKey, card.dataset.hkey, before);
          saveCustomOrder("memnos_hd_custom_order", nextOrder);
          renderHandleList();
        }
        draggedHdKey = null;
      };
    }
  });
}

function renderHandleHero() {
  const box = $("#hd-hero");
  const infoBox = $("#hd-hero-info");
  const chipsBox = $("#hd-hero-ch-chips");
  const joinWrap = $("#hd-join-wrap");
  if (!selectedHandle || !wsData) {
    if (box) box.hidden = true;
    return;
  }
  if (box) box.hidden = false;
  const { namespace, name } = selectedHandle;
  const writable = canWriteNs(namespace);
  const sObj = (wsData.sessions || []).find(s => s.namespace === namespace && s.name === name);
  const chs = sObj?.channels || [];
  const initials = name.slice(0, 2).toUpperCase();

  const isHeadless = !!sObj?.metadata?.headless;
  const nextPh = isHeadless
    ? (sObj?.alive
        ? `Ask @${name} a question or send follow-up instructions (will reply right as current step completes)…`
        : `Send a DM to wake @${name} headlessly and continue this conversation…`)
    : `Send direct @inbox message or task to @${name}… (Enter to send)`;
  if ($("#hd-comp-input") && $("#hd-comp-input").placeholder !== nextPh) {
    $("#hd-comp-input").placeholder = nextPh;
  }

  const infoSig = `${namespace}::${name}:${writable}:${!!wsData.is_admin}:${sObj ? `${sObj.alive}:${sObj.status || ""}:${sObj.run_state || ""}:${sObj.harness || ""}:${sObj.model || ""}:${sObj.role || ""}:${sObj.pid || ""}:${sObj.tty || ""}:${sObj.cwd || ""}:${sObj.host || ""}` : "unreg"}`;
  const infoHtml = `
    <div class="hero-top">
      <div class="hero-id">
        <div class="hero-av" style="background:${avatarColor(name)}">${esc(initials)}</div>
        <div>
          <div class="chat-title-row">
            <h2 style="margin:0;font-size:17px">@${esc(name)}</h2>
            ${sObj ? harnessBadgeForSession(sObj) : ""}
            <span class="ns-badge">${esc(namespace)}</span>
            ${sObj
              ? `<span class="pill ${sObj.status === "quota_paused" ? "warn" : (sObj.alive ? "ok" : (isHeadless ? "warn" : "no"))}">${sObj.status === "quota_paused" ? "QUOTA PAUSED" : (sObj.alive ? "ONLINE · " + esc(sObj.run_state || "idle") : (isHeadless ? "IDLE · DM TO WAKE" : "OFFLINE"))}</span>`
              : `<span class="pill warn">UNREGISTERED HANDLE</span>`}
          </div>
          ${sObj ? `<div class="hero-meta-grid" style="margin-top:5px">
            ${sObj.harness || sObj.metadata?.display_label ? `<span>Harness: <b>${esc(normalizeHarness(sObj.harness, sObj.metadata)?.label || sObj.harness)}</b></span>` : ""}
            ${sObj.model ? `<span>Model: <code>${esc(sObj.model)}</code></span>` : ""}
            ${sObj.role ? `<span>Role: <code>${esc(sObj.role)}</code></span>` : ""}
            <span>Heartbeat: <b ${sObj.last_heartbeat_at ? `data-age-iso="${esc(sObj.last_heartbeat_at)}"` : ""}>${age(sObj.last_heartbeat_at)} ago</b></span>
            ${sObj.pid ? `<span>PID: <code>${sObj.pid}</code> ${esc(sObj.tty || "")}</span>` : ""}
            ${sObj.cwd ? `<span>Folder: <code>${esc(sObj.cwd)}</code></span>` : ""}
            ${sObj.host ? `<span>Host: <code>${esc(sObj.host)}</code></span>` : ""}
          </div>` : ""}
        </div>
      </div>

      ${writable && sObj ? `<div class="row" style="gap:6px">
        ${wsData?.is_admin && sObj.metadata?.headless && sObj.alive ? `<button id="stop-worker-btn" class="danger sm" title="Stop running headless worker process">⏹ Stop Worker</button>` : ""}
        <button id="quota-handle-btn" class="ghost sm" title="${sObj.status === "quota_paused" ? "Resume handle so it can claim channel tasks again" : "Mark subscription quota reached (1h) and release in-progress tasks + file leases for failover"}">${sObj.status === "quota_paused" ? "▶ Resume Quota" : "⏸ Quota Cap"}</button>
        <button id="rename-handle-btn" class="ghost sm">Rename</button>
        <button id="unreg-handle-btn" class="danger sm">Unregister Handle</button>
      </div>` : ""}
    </div>
  `;

  if (setHtmlBySig(infoBox, infoSig, infoHtml)) {
    const stopWorkerBtn = $("#stop-worker-btn");
    if (stopWorkerBtn) {
      stopWorkerBtn.onclick = async () => {
        try {
          await wapi("POST", "spawner/stop", { handle: name, namespace });
          showToast(`Stopped headless worker @${name}`);
          await refreshOverview(false);
          renderHandleHero();
        } catch (e) { showToast(e.message, true); }
      };
    }
    const quotaBtn = $("#quota-handle-btn");
    if (quotaBtn && sObj) {
      quotaBtn.onclick = async () => {
        const pause = sObj.status !== "quota_paused";
        try {
          const res = await wapi("POST", "session/quota", {
            namespace,
            session: name,
            paused: pause,
            cooldown_minutes: 60,
            reason: pause ? "manual UI quota cap" : "",
          });
          if (pause) {
            const rel = res.released_tasks || 0;
            showToast(`Paused @${name} (1h quota cap)${rel ? ` · released ${rel} task(s) for failover` : ""}`);
          } else {
            showToast(`Resumed @${name}`);
          }
          await refreshOverview(false);
          renderHandleHero();
        } catch (e) { showToast(e.message, true); }
      };
    }
    const renameBtn = $("#rename-handle-btn");
    if (renameBtn) {
      renameBtn.onclick = async () => {
        const nextName = await askRenameHandle(name);
        if (!nextName || nextName === name) return;
        try {
          const res = await wapi("POST", "session/rename", { namespace, old_name: name, new_name: nextName });
          const finalName = res.new_name || nextName;
          selectedHandle = { namespace, name: finalName };
          showToast(`Renamed @${name} → @${finalName}`);
          await refreshOverview(false);
          renderHandleHero();
          await loadHandleMessages(true);
        } catch (e) { showToast(e.message, true); }
      };
    }
    const unregBtn = $("#unreg-handle-btn");
    if (unregBtn) {
      unregBtn.onclick = async () => {
        const { ok } = await askConfirm({
          title: `Unregister @${name}?`,
          message: `Unregister handle "@${name}" in "${namespace}" and remove all of its channel subscriptions?`,
          confirmLabel: "Unregister Handle",
        });
        if (!ok) return;
        try {
          await wapi("DELETE", `session?namespace=${encodeURIComponent(namespace)}&session=${encodeURIComponent(name)}`);
          selectedHandle = null;
          showToast(`Unregistered @${name}`);
          await refreshOverview(true);
        } catch (e) { showToast(e.message, true); }
      };
    }
  } else if (sObj?.last_heartbeat_at && infoBox) {
    const hbEl = $("[data-age-iso]", infoBox);
    if (hbEl) hbEl.setAttribute("data-age-iso", sObj.last_heartbeat_at);
    tickRelativeClocks(infoBox);
  }

  // Update channel chips separately so `#hd-join-input` stays permanently untouched in the DOM!
  if (joinWrap) joinWrap.hidden = !writable;
  const chipsSig = `${namespace}::${name}:${writable}|${chs.join(",")}`;
  const chipsHtml = chs.length ? chs.map(c => `
    <span class="ch-chip">
      <span style="cursor:pointer" data-open-ch="${esc(c)}">${esc(c)}</span>
      ${writable ? `<button type="button" data-leave-ch="${esc(c)}" title="Unregister @${esc(name)} from ${esc(c)}">×</button>` : ""}
    </span>
  `).join("") : `<span class="muted">None</span>`;

  if (setHtmlBySig(chipsBox, chipsSig, chipsHtml)) {
    $$("#hd-hero-ch-chips [data-open-ch]").forEach(el => {
      el.onclick = () => jumpToChannel(namespace, el.dataset.openCh);
    });
    $$("#hd-hero-ch-chips [data-leave-ch]").forEach(btn => {
      btn.onclick = async () => {
        try {
          await wapi("POST", "channel/unsubscribe", { namespace, channel: btn.dataset.leaveCh, session_name: name });
          await refreshOverview(false);
        } catch (e) { showToast(e.message, true); }
      };
    });
  }
}

async function submitHandleChannelJoin() {
  if (!selectedHandle) return;
  const { namespace, name } = selectedHandle;
  const inp = $("#hd-join-input");
  const raw = (inp?.value || "").trim();
  if (!raw) return;
  const ch = raw.startsWith("#") ? raw : "#" + raw;
  try {
    await wapi("POST", "channel/subscribe", { namespace, channel: ch, session_name: name });
    if (inp) inp.value = "";
    showToast(`Subscribed @${name} to ${ch}`);
    await refreshOverview(false);
  } catch (e) { showToast(e.message, true); }
}
if ($("#hd-join-btn")) $("#hd-join-btn").onclick = submitHandleChannelJoin;
if ($("#hd-join-input")) {
  $("#hd-join-input").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); submitHandleChannelJoin(); }
  });
}

async function loadHandleMessages(forceScrollBottom = false) {
  if (!selectedHandle) return;
  const box = $("#hd-messages");
  const nearBottom = forceScrollBottom || (box.scrollHeight - box.scrollTop - box.clientHeight < 100);
  const { namespace, name } = selectedHandle;
  const sObj = (wsData?.sessions || []).find(s => s.namespace === namespace && s.name === name);
  const isHeadless = !!sObj?.metadata?.headless;
  try {
    const [{ messages }, spStatus] = await Promise.all([
      wapi("GET", `session/messages?namespace=${encodeURIComponent(namespace)}&session=${encodeURIComponent(name)}&limit=150`),
      isHeadless ? wapi("GET", "spawner/status").catch(() => null) : Promise.resolve(null),
    ]);

    let consoleHtml = "";
    const matchingRun = (spStatus?.runs || []).find(r => r.namespace === namespace && r.handle === name);
    if (isHeadless || matchingRun) {
      const rStatus = matchingRun?.status || (sObj?.alive ? "running" : "completed");
      const isRunning = rStatus === "running" || rStatus === "starting";
      const logLines = matchingRun?.log_lines?.length
        ? matchingRun.log_lines
        : liveEvents
            .filter(e => String(e.actor || "").replace(/^@/, "") === name)
            .slice(0, 15)
            .reverse()
            .map(e => `${e.op}: ${e.detail || "—"}`);
      consoleHtml = `<div class="headless-console">
        <div class="headless-console-hdr">
          <span><b>🖥 Headless Worker Live Stream</b> · <code>@${esc(name)}</code></span>
          <span class="pill ${isRunning ? "ok" : "warn"}">${isRunning ? "● RUNNING LIVE" : ("IDLE (" + esc(rStatus.toUpperCase()) + ") · DM BELOW TO WAKE")}</span>
        </div>
        <div class="headless-console-body" id="hd-console-scroll">
          ${logLines.length
            ? logLines.map(l => `<div class="headless-log-line">${esc(l)}</div>`).join("")
            : `<div class="muted">${isRunning ? "Waiting for first stream output…" : "Last turn auto-closed cleanly. Send a DM below to wake @" + esc(name) + " and continue chatting."}</div>`}
        </div>
      </div>`;
    }

    const handleOps = liveEvents.filter(e => String(e.actor || "").replace(/^@/, "") === name && !String(e.op || "").startsWith("headless/")).slice(0, 10);
    const opsStrip = handleOps.length ? `<div class="surface-card" style="margin:8px 0 14px;padding:12px 14px">
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <b style="font-size:12.5px">⚡ Recent MEMNOS Calls by @${esc(name)}</b>
        <span class="muted" style="font-size:11.5px">${handleOps.length} recent</span>
      </div>
      <div style="display:flex;flex-direction:column;gap:5px;font-size:12px">
        ${handleOps.map(e => `<div class="row" style="justify-content:space-between;gap:10px">
          <span><code><b>${esc(e.op)}</b></code> <span class="muted">${esc(e.detail || "—")}</span></span>
          <span class="muted" style="white-space:nowrap"><span ${e.ts ? `data-ago-iso="${esc(e.ts)}"` : ""}>${esc(fmtAgoCompact(e.ts))}</span> · ${e.latency_ms ?? 0}ms</span>
        </div>`).join("")}
      </div>
    </div>` : "";
    const hdFeedSig = `${namespace}::${name}|run:${matchingRun?.status || ""}:${(matchingRun?.log_lines || []).length}:${matchingRun?.last_line || ""}|ops:${handleOps.map(e => e.ts + ":" + e.op).join(",")}|msgs:` +
      (messages || []).map(m => `${m.id}:${m.status || ""}:${m.resolution || ""}`).join("|");

    if (!messages || !messages.length) {
      if (setHtmlBySig(box, hdFeedSig + "|empty", consoleHtml + opsStrip + `<div class="empty-state">
        <div class="empty-ico">@</div>
        <h3 style="margin:0 0 6px;color:var(--fg)">@${esc(name)}</h3>
        <p>${isHeadless
          ? `Headless worker <b>@${esc(name)}</b> automatically posts its turn summary here when done — or send a DM below anytime to ask questions or assign follow-up work.`
          : `No direct <code>@inbox</code> messages for <b>@${esc(name)}</b> in <code>${esc(namespace)}</code>.<br>Send a DM below to wake this handle directly.`}</p>
      </div>`)) {
        const cScroll = $("#hd-console-scroll");
        if (cScroll) cScroll.scrollTop = cScroll.scrollHeight;
      }
      return;
    }
    const feedHtml = consoleHtml + opsStrip + messages.map(m => {
      const sender = m.from_session || "agent";
      const initials = sender.slice(0, 2).toUpperCase();
      const isActed = m.status === "acted";
      return `<div class="msg-row ${isActed ? "is-done" : ""}">
        <div class="msg-av" style="background:${avatarColor(sender)}">${esc(initials)}</div>
        <div class="msg-content">
          <div class="msg-meta">
            <span class="msg-author">@${esc(sender)} → @${esc(m.to_session)}</span>
            <span class="tiny-pill">${esc(m.intent || "message")} · ${esc(m.status || "pending")}</span>
            <span class="msg-ts">${fmt(m.created_at)} (<span data-age-iso="${esc(m.created_at)}">${age(m.created_at)} ago</span>) · #${m.id}</span>
          </div>
          ${m.subject ? `<div style="font-weight:700;margin-bottom:4px">${esc(m.subject)}</div>` : ""}
          <div class="msg-body-text">${formatRichText(m.content)}</div>
          ${m.resolution ? `<div class="task-strip"><span class="pill ok">ACTED</span> <span>${esc(m.resolution)}</span></div>` : ""}
        </div>
      </div>`;
    }).join("");
    if (setHtmlBySig(box, hdFeedSig, feedHtml)) {
      const cScroll = $("#hd-console-scroll");
      if (cScroll) cScroll.scrollTop = cScroll.scrollHeight;
      if (nearBottom) box.scrollTop = box.scrollHeight;
    } else if (forceScrollBottom) {
      box.scrollTop = box.scrollHeight;
    }
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
    let sendRes = null;
    try {
      sendRes = await wsCall("send_dm", payload);
    } catch (_) {
      sendRes = await wapi("POST", "session/send", payload);
    }
    if (sendRes?.headless_delivery === "woke_headless_worker") {
      showToast(`⚡ Woke @${selectedHandle.name} headlessly — streaming reply…`);
    } else if (sendRes?.headless_delivery === "queued_for_running_worker") {
      showToast(`⏳ Queued for @${selectedHandle.name} — will reply right after current step`);
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
        const { ok } = await askConfirm({
          title: "Deactivate Guardrail?",
          message: "Deactivate this PreToolUse guardrail rule?",
          confirmLabel: "Deactivate",
        });
        if (!ok) return;
        try {
          await wapi("DELETE", "constraints/enforcement?id=" + btn.dataset.delEnf);
          showToast("Guardrail deactivated");
          await loadWorkspaceConstraints();
          refreshOverview(false);
        } catch (e) { showToast(e.message, true); }
      };
    });

    $$("#cs-pin-table [data-retire-pin]").forEach(btn => {
      btn.onclick = async () => {
        const { ok } = await askConfirm({
          title: "Retire Pinned Constraint?",
          message: "Retire this pinned constraint from active recall?",
          confirmLabel: "Retire",
        });
        if (!ok) return;
        try {
          await wapi("DELETE", `constraints/pin?id=${btn.dataset.retirePin}&namespace=${encodeURIComponent(btn.dataset.retireNs)}`);
          showToast("Pinned constraint retired");
          await loadWorkspaceConstraints();
          refreshOverview(false);
        } catch (e) { showToast(e.message, true); }
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
    if (!ns || !matcher || !text) { showToast("Namespace, Tool Matcher, and Rule Reason are required.", true); return; }
    try {
      await wapi("POST", "constraints/enforcement", { namespace: ns, enforce_level: lvl, tool_matcher: matcher, rule_text: text });
      $("#cs-add-enf-matcher").value = "";
      $("#cs-add-enf-text").value = "";
      showToast("Guardrail added");
      await loadWorkspaceConstraints();
      refreshOverview(false);
    } catch (e) { showToast(e.message, true); }
  };
}

if ($("#cs-add-pin-btn")) {
  $("#cs-add-pin-btn").onclick = async () => {
    const ns = ($("#cs-add-pin-ns")?.value || "").trim();
    const subj = ($("#cs-add-pin-subj")?.value || "").trim();
    const text = ($("#cs-add-pin-text")?.value || "").trim();
    if (!ns || !text) { showToast("Namespace and Constraint Text are required.", true); return; }
    try {
      await wapi("POST", "constraints/pin", { namespace: ns, subject: subj || undefined, text });
      $("#cs-add-pin-subj").value = "";
      $("#cs-add-pin-text").value = "";
      showToast("Constraint pinned");
      await loadWorkspaceConstraints();
      refreshOverview(false);
    } catch (e) { showToast(e.message, true); }
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
  const cInfo = getClientVersionInfo();
  const srvTag = fmtVerTag(v.version || serverVerState.version, v.build || serverVerState.build, true);
  const cliTag = fmtVerTag(cInfo.version, cInfo.build, true);
  const ramSub = `${cInfo.kind} ${cliTag} · Server ${srvTag} · up ${upHrs}`;

  $("#tel-cards").innerHTML = `
    <div class="kpi-card"><div class="kpi-lbl">Engine Footprint</div><div class="kpi-val">${esc(ramVal)}</div><div class="kpi-sub">${esc(ramSub)}</div></div>
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
      const nsName = b.dataset.del;
      const { ok, checked } = await askConfirm({
        title: `Delete namespace "${nsName}"?`,
        message: `Remove "${nsName}" from registered namespaces?`,
        confirmLabel: "Delete Namespace",
        checkboxLabel: "Also purge all stored memory turns & facts in this namespace",
        defaultChecked: false,
      });
      if (!ok) return;
      try {
        await api("DELETE", (checked ? "namespaces?purge=1&name=" : "namespaces?name=") + encodeURIComponent(nsName));
        showToast(`Deleted namespace ${nsName}`);
        loadNamespaces(); refreshOverview(false);
      } catch (e) { showToast(e.message, true); }
    });
  }, loadNamespaces);
}
$("#ns-create").onclick = async () => {
  const name = $("#ns-name").value.trim(); if (!name) return;
  try {
    await api("POST", "namespaces", { name, description: $("#ns-desc").value.trim() });
    $("#ns-name").value = ""; $("#ns-desc").value = "";
    loadNamespaces(); refreshOverview(false);
  } catch (e) { showToast(e.message, true); }
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
      const { ok } = await askConfirm({
        title: "Prune Stale Test Principals?",
        message: `Delete ${staleTests.length} stale "test-*" principals that have 0 active tokens?`,
        confirmLabel: `Prune ${staleTests.length} Principals`,
      });
      if (!ok) return;
      pruneBtn.disabled = true;
      try {
        for (const tp of staleTests) {
          await api("DELETE", "principals?id=" + tp.id);
        }
        showToast(`Pruned ${staleTests.length} test-* principals`);
        await loadPrincipals();
        refreshOverview(false);
      } catch (e) {
        showToast(e.message, true);
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
      const { ok } = await askConfirm({
        title: `Delete Principal "${nm}"?`,
        message: `Permanently delete principal "${nm}" (ID ${id})?\n\nThis revokes and deletes all grants, tokens, and sessions belonging to "${nm}".`,
        confirmLabel: "Delete Principal",
      });
      if (!ok) return;
      try {
        await api("DELETE", "principals?id=" + id);
        showToast(`Deleted principal "${nm}"`);
        await loadPrincipals();
        refreshOverview(false);
      } catch (e) {
        showToast(e.message, true);
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
  catch (e) { showToast(e.message, true); }
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
        try { await navigator.clipboard.writeText(revealedTokenMap[id]); showToast("Token copied to clipboard"); } catch (_) {}
        renderTokensRows();
      } catch (e) {
        showToast(e.message, true);
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
        showToast("Minted & copied token");
        await loadTokens();
        loadPrincipals();
      } catch (e) { showToast(e.message, true); }
    };
  });

  $$("#tok-table [data-rv]").forEach(b => {
    b.onclick = async () => {
      const { ok } = await askConfirm({
        title: `Revoke Token #${b.dataset.rv}?`,
        message: `Revoke token #${b.dataset.rv}? Any agent using this token will immediately lose access.`,
        confirmLabel: "Revoke Token",
      });
      if (!ok) return;
      try {
        await api("POST", "tokens/revoke", { id: +b.dataset.rv });
        showToast(`Revoked token #${b.dataset.rv}`);
        loadTokens();
        loadPrincipals();
      } catch (e) { showToast(e.message, true); }
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
    showToast("Minted & copied token");
    $("#tok-label").value = ""; $("#tok-ttl").value = ""; loadTokens(); loadPrincipals();
  } catch (e) { showToast(e.message, true); }
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
        showToast(`Revoked grant ${b.dataset.rg}`);
        loadGrants();
      } catch (e) { showToast(e.message, true); }
    });
  }, loadGrants);
}
$("#gr-principal").onchange = loadGrants;
$("#gr-add").onclick = async () => {
  try {
    await api("POST", "grants", { principal_id: +$("#gr-principal").value, namespace: $("#gr-namespace").value.trim(),
      can_read: $("#gr-read").checked, can_write: $("#gr-write").checked });
    $("#gr-namespace").value = ""; loadGrants();
  } catch (e) { showToast(e.message, true); }
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
    const { ok } = await askConfirm({
      title: "Remove Repo Binding?",
      message: `Remove binding for "${b.dataset.key}"?`,
      confirmLabel: "Remove Binding",
    });
    if (!ok) return;
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
      const { ok } = await askConfirm({
        title: `Delete Secret "${b.dataset.ds}"?`,
        message: `Permanently delete secret "${b.dataset.ds}" from the encrypted vault?`,
        confirmLabel: "Delete Secret",
      });
      if (!ok) return;
      try {
        await api("DELETE", "secrets?name=" + encodeURIComponent(b.dataset.ds));
        showToast(`Deleted secret ${b.dataset.ds}`);
        loadSecrets();
      } catch (e) { showToast(e.message, true); }
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
renderVersionSurfaces();
$("#server-url").value = serverBase();
if (tok()) {
  connectWorkspace(tok(), serverBase());
} else {
  disconnectToLogin();
}
