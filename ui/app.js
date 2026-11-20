"use strict";
// MEMNOS Workspace & Console — zero-build vanilla JS (browser + native Rust desktop app).

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// Boot injection from `memnos app` native WebView or `#token=...&url=...` hash
(function bootstrapAuth() {
  if (window.__MEMNOS_BOOT__) {
    if (window.__MEMNOS_BOOT__.token) localStorage.setItem("memnos_admin_token", window.__MEMNOS_BOOT__.token);
    if (window.__MEMNOS_BOOT__.url) localStorage.setItem("memnos_server_url", window.__MEMNOS_BOOT__.url);
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
  if (saved) return saved;
  return location.origin;
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
  const palette = ["#5c6cff", "#2ca58d", "#e06d53", "#845ec2", "#0089ba", "#d65db1", "#ff9671", "#3a86ff"];
  let h = 0;
  const s = String(name || "m");
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return palette[Math.abs(h) % palette.length];
};

async function rawFetch(method, fullPath, body, dropOn403 = false) {
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
  if (r.status === 401 || (dropOn403 && r.status === 403)) {
    disconnectToLogin("Token invalid, expired, or unauthorized for " + serverBase());
    throw new Error("unauthorized");
  }
  if (!r.ok) throw new Error((j.error || ("HTTP " + r.status)) + (j.msg ? ": " + j.msg : ""));
  return j;
}

const wapi = (m, p, b) => rawFetch(m, "/workspace/api/" + p, b, false);
const api  = (m, p, b) => rawFetch(m, "/admin/api/" + p, b, false);
const uapi = (m, p, b) => rawFetch(m, p, b, false);

// ---- Workspace State ----
let wsData = null; // latest /workspace/api/overview payload
let activeView = "stream";
let activeTarget = null; // { kind: "channel"|"session", namespace, name }
let sseSource = null;
let sseRefreshTimer = null;
let pollFallbackTimer = null;
let inspectorOpen = true;

// ---- Table helpers ----
function cols(tbody) { return $$("th", tbody.closest("table")).length || 1; }
function showLoading(tbody) {
  const n = cols(tbody);
  tbody.innerHTML = `<tr class="state"><td colspan="${n}"><span class="spinner"></span> loading…</td></tr>`;
  return setTimeout(() => {
    const td = $("tr.state td", tbody);
    if (td) td.innerHTML = `<span class="spinner"></span> still loading… dataset is large`;
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

// ---- Auth & Connection ----
function disconnectToLogin(errMsg = "") {
  stopRealtime();
  localStorage.removeItem("memnos_admin_token");
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#server-url").value = serverBase();
  $("#loginerr").textContent = errMsg;
}

async function connectWorkspace(tokenVal, urlVal) {
  if (urlVal) localStorage.setItem("memnos_server_url", urlVal.replace(/\/+$/, ""));
  if (tokenVal) localStorage.setItem("memnos_admin_token", tokenVal);
  $("#loginerr").textContent = "";
  try {
    await refreshOverview(true);
    $("#login").hidden = true;
    $("#app").hidden = false;
    startRealtime();
  } catch (e) {
    localStorage.removeItem("memnos_admin_token");
    $("#app").hidden = true;
    $("#login").hidden = false;
    $("#loginerr").textContent = e.message;
  }
}

$("#signin").onclick = () => {
  const t = $("#token").value.trim();
  const u = $("#server-url").value.trim();
  if (!t) { $("#loginerr").textContent = "Paste an admin or role-scoped token (mnk_...)"; return; }
  connectWorkspace(t, u || location.origin);
};
$("#token").addEventListener("keydown", e => { if (e.key === "Enter") $("#signin").click(); });
$("#logout").onclick = () => disconnectToLogin();
$("#switch-server").onclick = () => {
  const next = prompt("Enter MEMNOS Server URL (local or remote):", serverBase());
  if (!next || !next.trim()) return;
  localStorage.setItem("memnos_server_url", next.trim().replace(/\/+$/, ""));
  const nextTok = prompt("Enter token for " + next.trim() + " (leave blank to keep current token):", "");
  connectWorkspace(nextTok ? nextTok.trim() : tok(), next.trim());
};
$("#refresh-now").onclick = () => {
  refreshOverview(false);
  if (activeView === "stream" && activeTarget) loadStreamMessages(false);
};

// ---- Real-time SSE (LISTEN/NOTIFY) ----
function setLiveStatus(online, label) {
  const b = $("#sse-badge");
  b.className = "live-pill " + (online ? "online" : "offline");
  b.textContent = "● " + label;
}

function scheduleRealtimeRefresh() {
  if (sseRefreshTimer) return;
  sseRefreshTimer = setTimeout(async () => {
    sseRefreshTimer = null;
    try {
      await refreshOverview(false);
      if (activeView === "stream" && activeTarget) await loadStreamMessages(false);
    } catch (_) {}
  }, 220);
}

function startRealtime() {
  stopRealtime();
  try {
    const evUrl = serverBase() + "/workspace/events?token=" + encodeURIComponent(tok());
    sseSource = new EventSource(evUrl);
    sseSource.onopen = () => setLiveStatus(true, "LIVE");
    sseSource.onmessage = (ev) => {
      setLiveStatus(true, "LIVE");
      try {
        const d = JSON.parse(ev.data || "{}");
        if (d.type === "refresh") scheduleRealtimeRefresh();
      } catch (_) {}
    };
    sseSource.onerror = () => setLiveStatus(false, "RECONNECTING");
  } catch (_) {
    setLiveStatus(false, "POLLING");
  }
  pollFallbackTimer = setInterval(() => {
    refreshOverview(false).catch(() => {});
    if (activeView === "stream" && activeTarget) loadStreamMessages(false).catch(() => {});
  }, 12000);
}

function stopRealtime() {
  if (sseSource) { sseSource.close(); sseSource = null; }
  if (pollFallbackTimer) { clearInterval(pollFallbackTimer); pollFallbackTimer = null; }
}

// ---- Workspace Overview, Sidebar & Topbar ----
async function refreshOverview(pickInitialTarget = false) {
  const nsSel = $("#ws-ns-filter").value || "*";
  const data = await wapi("GET", "overview?namespace=" + encodeURIComponent(nsSel));
  wsData = data;

  // Header pills
  const hostShort = serverBase().replace(/^https?:\/\//, "");
  $("#switch-server").textContent = hostShort;
  const pName = data.principal?.name || "token";
  const roleLabel = data.is_admin ? "ADMIN (*)" : `SCOPED (${(data.namespaces || []).length} ns)`;
  $("#who-badge").textContent = `@${pName} · ${roleLabel}`;
  $("#admin-nav-group").hidden = !data.is_admin;

  // Vitals strip
  const v = data.vitals || {};
  const memStr = v.footprint_mb != null ? `${v.footprint_mb} MB` : (v.rss_mb != null ? `${v.rss_mb} MB` : "—");
  const dbStr = v.db_size_bytes != null ? fmtBytes(v.db_size_bytes) : "role-scoped";
  $("#vitals-strip").innerHTML =
    `<span>RAM <b>${esc(memStr)}</b></span>` +
    `<span>DB <b>${esc(dbStr)}</b></span>` +
    `<span>Facts <b>${(v.total_facts || 0).toLocaleString()}</b></span>` +
    `<span>Turns <b>${(v.total_turns || 0).toLocaleString()}</b></span>` +
    `<span>Mode <b>${esc(v.embedding_mode || "—")}</b></span>`;

  // Namespace dropdown (preserve selection)
  const prevNs = $("#ws-ns-filter").value || "*";
  const nsOpts = [`<option value="*">All authorized (${(data.namespaces || []).length})</option>`]
    .concat((data.namespaces || []).map(n =>
      `<option value="${esc(n.name)}">${esc(n.name)} (${n.facts} facts)</option>`
    ));
  $("#ws-ns-filter").innerHTML = nsOpts.join("");
  if ([...$("#ws-ns-filter").options].some(o => o.value === prevNs)) {
    $("#ws-ns-filter").value = prevNs;
  }

  renderSidebarChannels(data.channels || []);
  renderSidebarSessions(data.sessions || []);

  if (pickInitialTarget || !activeTarget) {
    const chs = data.channels || [];
    const sss = data.sessions || [];
    if (chs.length) {
      selectStreamTarget("channel", chs[0].namespace, chs[0].channel);
    } else if (sss.length) {
      selectStreamTarget("session", sss[0].namespace, sss[0].name);
    } else {
      const defNs = (data.namespaces && data.namespaces[0]?.name) || "default";
      selectStreamTarget("channel", defNs, "#general");
    }
  } else if (activeView === "stream" && activeTarget) {
    renderInspector();
  }
  if (activeView === "telemetry") renderTelemetry();
}

$("#ws-ns-filter").onchange = () => refreshOverview(true);

function renderSidebarChannels(channels) {
  const multiNs = ($("#ws-ns-filter").value || "*") === "*";
  const box = $("#sidebar-channels");
  if (!channels.length) {
    box.innerHTML = `<div class="muted" style="padding:6px 10px">No channels yet — click <b>+</b> above.</div>`;
    return;
  }
  box.innerHTML = channels.map(c => {
    const isAct = activeView === "stream" && activeTarget?.kind === "channel"
      && activeTarget.namespace === c.namespace && activeTarget.name === c.channel;
    const tasks = c.open_tasks || 0;
    const badge = tasks > 0
      ? `<span class="mini-badge warn" title="Open claimable tasks">${tasks} task${tasks > 1 ? "s" : ""}</span>`
      : `<span class="mini-badge">${c.message_count || 0}</span>`;
    return `<button class="nav-item ${isAct ? "active" : ""}" data-ch="${esc(c.channel)}" data-ns="${esc(c.namespace)}">
      <span class="nav-item-main">
        <span>${esc(c.channel)}</span>
        ${multiNs ? `<span class="nav-sub-ns">${esc(c.namespace)}</span>` : ""}
      </span>
      ${badge}
    </button>`;
  }).join("");
  $$("#sidebar-channels .nav-item").forEach(btn => {
    btn.onclick = () => selectStreamTarget("channel", btn.dataset.ns, btn.dataset.ch);
  });
}

function renderSidebarSessions(sessions) {
  const multiNs = ($("#ws-ns-filter").value || "*") === "*";
  const aliveCount = sessions.filter(s => s.alive).length;
  $("#sessions-count").textContent = `${aliveCount}/${sessions.length}`;
  const box = $("#sidebar-sessions");
  if (!sessions.length) {
    box.innerHTML = `<div class="muted" style="padding:6px 10px">No registered @handles yet.</div>`;
    return;
  }
  box.innerHTML = sessions.map(s => {
    const isAct = activeView === "stream" && activeTarget?.kind === "session"
      && activeTarget.namespace === s.namespace && activeTarget.name === s.name;
    const dotCls = !s.alive ? "idle" : (s.run_state === "running" || s.run_state === "busy" ? "busy" : "alive");
    const unread = s.unread_inbox || 0;
    return `<button class="nav-item ${isAct ? "active" : ""}" data-sess="${esc(s.name)}" data-ns="${esc(s.namespace)}">
      <span class="nav-item-main">
        <span class="dot ${dotCls}"></span>
        <span>@${esc(s.name)}</span>
        ${multiNs ? `<span class="nav-sub-ns">${esc(s.namespace)}</span>` : ""}
      </span>
      ${unread > 0 ? `<span class="mini-badge warn">${unread}</span>` : `<span class="mini-badge">${(s.channels || []).length}#</span>`}
    </button>`;
  }).join("");
  $$("#sidebar-sessions .nav-item").forEach(btn => {
    btn.onclick = () => selectStreamTarget("session", btn.dataset.ns, btn.dataset.sess);
  });
}

// ---- Create Channel ----
$("#btn-new-channel").onclick = () => {
  const box = $("#new-channel-box");
  box.hidden = !box.hidden;
  if (!box.hidden) {
    const curNs = $("#ws-ns-filter").value;
    $("#new-ch-ns").value = (curNs && curNs !== "*") ? curNs : (wsData?.namespaces?.[0]?.name || "");
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
    await wapi("POST", "channel/create", {
      namespace: ns,
      channel: ch,
      description: $("#new-ch-desc").value.trim(),
    });
    $("#new-ch-name").value = "";
    $("#new-ch-desc").value = "";
    $("#new-channel-box").hidden = true;
    await refreshOverview(false);
    selectStreamTarget("channel", ns, ch);
  } catch (e) { alert(e.message); }
};

// ---- View Switching ----
function switchStageView(viewName) {
  activeView = viewName;
  $$(".stage-view").forEach(sec => sec.hidden = true);
  const targetSec = $("#view-" + viewName);
  if (targetSec) targetSec.hidden = false;

  $$(".sidebar [data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === viewName));
  if (viewName !== "stream") {
    $$("#sidebar-channels .nav-item, #sidebar-sessions .nav-item").forEach(b => b.classList.remove("active"));
  }

  if (viewName === "memories") { wmemOffset = 0; loadWorkspaceMemories(); }
  if (viewName === "telemetry") renderTelemetry();
  if (viewName === "routing") loadRouting();
  if (viewName === "namespaces") loadNamespaces();
  if (viewName === "access") loadPrincipals();
  if (viewName === "tokens") loadPrincipals();
  if (viewName === "usage") loadUsagePanel();
  if (viewName === "secrets") loadSecrets();
  if (viewName === "dashboard") loadDashboard();
  if (viewName === "cli") loadCliRef();
  if (viewName === "settings") loadSettings();
}

$$(".sidebar [data-view]").forEach(btn => {
  btn.onclick = () => switchStageView(btn.dataset.view);
});

// ---- Stream Target Selection, Messages & Inspector ----
function canWriteNs(ns) {
  const found = (wsData?.namespaces || []).find(n => n.name === ns);
  return found ? !!found.can_write : !!wsData?.is_admin;
}

async function selectStreamTarget(kind, namespace, name) {
  activeTarget = { kind, namespace, name };
  switchStageView("stream");
  renderSidebarChannels(wsData?.channels || []);
  renderSidebarSessions(wsData?.sessions || []);

  const writable = canWriteNs(namespace);
  $("#stream-ns").textContent = namespace;
  if (kind === "channel") {
    const chObj = (wsData?.channels || []).find(c => c.namespace === namespace && c.channel === name);
    $("#stream-title").textContent = name;
    const subCnt = (chObj?.subscribers || []).length;
    $("#stream-sub").textContent = `${subCnt} subscribed handle${subCnt === 1 ? "" : "s"}` + (chObj?.description ? ` · ${chObj.description}` : "");
    $("#comp-claim-wrap").hidden = false;
    $("#comp-input").placeholder = `Message ${name} in ${namespace}… (Enter to send, Shift+Enter for newline)`;
    const delBtn = $("#delete-target-btn");
    delBtn.hidden = !writable;
    delBtn.textContent = "Delete Channel";
  } else {
    const sObj = (wsData?.sessions || []).find(s => s.namespace === namespace && s.name === name);
    $("#stream-title").textContent = "@" + name;
    $("#stream-sub").textContent = sObj
      ? `${sObj.alive ? "online" : "offline"} · last heartbeat ${age(sObj.last_heartbeat_at)} ago`
      : "direct @inbox";
    $("#comp-claim-wrap").hidden = true;
    $("#comp-input").placeholder = `Send direct @inbox message to @${name}…`;
    const delBtn = $("#delete-target-btn");
    delBtn.hidden = !writable;
    delBtn.textContent = "Unregister Handle";
  }

  renderInspector();
  await loadStreamMessages(true);
}

$("#toggle-inspector").onclick = () => {
  inspectorOpen = !inspectorOpen;
  $("#stream-inspector").hidden = !inspectorOpen;
};

$("#delete-target-btn").onclick = async () => {
  if (!activeTarget) return;
  const { kind, namespace, name } = activeTarget;
  if (kind === "channel") {
    if (!confirm(`Delete channel "${name}" in namespace "${namespace}"?\n\nThis removes the channel catalog entry, all handle subscriptions on ${name}, and its message history.`)) return;
    try {
      await wapi("DELETE", `channel?namespace=${encodeURIComponent(namespace)}&channel=${encodeURIComponent(name)}`);
      activeTarget = null;
      await refreshOverview(true);
    } catch (e) { alert(e.message); }
  } else {
    if (!confirm(`Unregister handle "@${name}" from namespace "${namespace}" and remove all of its channel subscriptions?`)) return;
    try {
      await wapi("DELETE", `session?namespace=${encodeURIComponent(namespace)}&session=${encodeURIComponent(name)}`);
      activeTarget = null;
      await refreshOverview(true);
    } catch (e) { alert(e.message); }
  }
};

async function loadStreamMessages(forceScrollBottom = false) {
  if (!activeTarget) return;
  const box = $("#stream-messages");
  const nearBottom = forceScrollBottom || (box.scrollHeight - box.scrollTop - box.clientHeight < 90);
  const { kind, namespace, name } = activeTarget;

  try {
    if (kind === "channel") {
      const { messages } = await wapi("GET", `channel/messages?namespace=${encodeURIComponent(namespace)}&channel=${encodeURIComponent(name)}&limit=150`);
      if (!messages || !messages.length) {
        box.innerHTML = `<div class="empty-stream">
          <h3>${esc(name)}</h3>
          <p>No messages in <code>${esc(namespace)}</code> · <b>${esc(name)}</b> yet.<br>Send a message below or have your agents publish to <code>${esc(name)}</code>.</p>
        </div>`;
        return;
      }
      box.innerHTML = messages.map(renderChannelMessage).join("");
    } else {
      const { messages } = await wapi("GET", `session/messages?namespace=${encodeURIComponent(namespace)}&session=${encodeURIComponent(name)}&limit=150`);
      if (!messages || !messages.length) {
        box.innerHTML = `<div class="empty-stream">
          <h3>@${esc(name)}</h3>
          <p>No direct <code>@inbox</code> messages for <b>@${esc(name)}</b> in <code>${esc(namespace)}</code> yet.</p>
        </div>`;
        return;
      }
      box.innerHTML = messages.map(renderSessionMessage).join("");
    }
    if (nearBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    box.innerHTML = `<div class="empty-stream"><span class="err">${esc(e.message)}</span></div>`;
  }
}

function renderChannelMessage(m) {
  const sender = m.sender_session || "agent";
  const initials = sender.replace(/^@/, "").slice(0, 2).toUpperCase();
  const isDone = m.claimable && (m.claim_status === "acked" || m.claim_status === "completed");
  const cardCls = m.claimable ? (isDone ? "msg-card is-done" : "msg-card is-task") : "msg-card";
  let taskHtml = "";
  if (m.claimable) {
    const st = m.claim_status || "pending";
    const who = m.claimed_by ? ` · claimed by <b>@${esc(m.claimed_by)}</b>` : " · waiting for subscriber to claim";
    const res = m.resolution ? ` · resolution: <code>${esc(m.resolution)}</code>` : "";
    taskHtml = `<div class="task-banner">
      <span class="pill ${isDone ? "ok" : "no"}">TASK · ${esc(st.toUpperCase())}</span>
      <span class="muted">#${m.id}${who}${res}</span>
    </div>`;
  }
  return `<div class="${cardCls}">
    <div class="avatar" style="background:${avatarColor(sender)}">${esc(initials)}</div>
    <div class="msg-body">
      <div class="msg-hdr">
        <span class="msg-sender">@${esc(sender)}</span>
        <span class="intent-pill">${esc(m.intent || "message")}</span>
        <span class="msg-time" title="${esc(m.created_at)}">${fmt(m.created_at)} (${age(m.created_at)} ago) · #${m.id}</span>
      </div>
      <div class="msg-text">${esc(m.content)}</div>
      ${taskHtml}
    </div>
  </div>`;
}

function renderSessionMessage(m) {
  const sender = m.from_session || "agent";
  const initials = sender.replace(/^@/, "").slice(0, 2).toUpperCase();
  const isActed = m.status === "acted";
  return `<div class="msg-card ${isActed ? "is-done" : ""}">
    <div class="avatar" style="background:${avatarColor(sender)}">${esc(initials)}</div>
    <div class="msg-body">
      <div class="msg-hdr">
        <span class="msg-sender">@${esc(sender)} → @${esc(m.to_session)}</span>
        <span class="intent-pill">${esc(m.intent || "message")} · ${esc(m.status || "pending")}</span>
        <span class="msg-time">${fmt(m.created_at)} · #${m.id}</span>
      </div>
      ${m.subject ? `<div style="font-weight:600;margin-bottom:3px">${esc(m.subject)}</div>` : ""}
      <div class="msg-text">${esc(m.content)}</div>
      ${m.resolution ? `<div class="task-banner"><span class="pill ok">ACTED</span> <span>${esc(m.resolution)}</span></div>` : ""}
    </div>
  </div>`;
}

// ---- Composer Send ----
async function sendComposerMessage() {
  if (!activeTarget) return;
  const text = $("#comp-input").value.trim();
  if (!text) return;
  const sender = ($("#comp-sender").value.trim().replace(/^@/, "")) || "owner";
  $("#comp-err").textContent = "";
  $("#comp-send").disabled = true;
  try {
    if (activeTarget.kind === "channel") {
      const claimable = $("#comp-claimable").checked;
      await wapi("POST", "channel/publish", {
        namespace: activeTarget.namespace,
        channel: activeTarget.name,
        sender_session: sender,
        content: text,
        claimable,
        intent: claimable ? "task" : "message",
      });
      $("#comp-claimable").checked = false;
    } else {
      await wapi("POST", "session/send", {
        namespace: activeTarget.namespace,
        to_session: activeTarget.name,
        from_session: sender,
        content: text,
        intent: "message",
      });
    }
    $("#comp-input").value = "";
    await loadStreamMessages(true);
    await refreshOverview(false);
  } catch (e) {
    $("#comp-err").textContent = e.message;
  } finally {
    $("#comp-send").disabled = false;
  }
}

$("#comp-send").onclick = sendComposerMessage;
$("#comp-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendComposerMessage();
  }
});

// ---- Right Inspector: Subscribe / Unregister Handles & Channel Info ----
function renderInspector() {
  const box = $("#inspector-content");
  if (!activeTarget || !wsData) { box.innerHTML = ""; return; }
  const { kind, namespace, name } = activeTarget;
  const writable = canWriteNs(namespace);

  if (kind === "channel") {
    const chObj = (wsData.channels || []).find(c => c.namespace === namespace && c.channel === name) || {
      subscribers: [], publishers: [], message_count: 0, open_tasks: 0, description: ""
    };
    const subs = chObj.subscribers || [];
    const nsSessions = (wsData.sessions || [])
      .filter(s => s.namespace === namespace && !subs.includes(s.name))
      .map(s => s.name);

    box.innerHTML = `
      <div class="insp-section">
        <h4>Channel Details</h4>
        <div class="kv-list">
          <span>Channel</span><span>${esc(name)}</span>
          <span>Namespace</span><span>${esc(namespace)}</span>
          <span>Messages</span><span>${chObj.message_count || 0}</span>
          <span>Open Tasks</span><span>${chObj.open_tasks || 0}</span>
        </div>
      </div>

      <div class="insp-section">
        <h4>Subscribed Handles (${subs.length})</h4>
        <div class="chip-list">
          ${subs.length ? subs.map(h => `
            <div class="handle-chip">
              <code>@${esc(h)}</code>
              ${writable ? `<button class="unsub-btn" data-unsub="${esc(h)}" title="Unregister @${esc(h)} from ${esc(name)}">Unregister</button>` : ""}
            </div>
          `).join("") : `<div class="muted">No handles subscribed yet.</div>`}
        </div>
      </div>

      ${writable ? `
      <div class="insp-section">
        <h4>Register @Handle to Channel</h4>
        <div class="inline-form" style="margin:0">
          ${nsSessions.length ? `<select id="insp-pick-handle">
            <option value="">Pick active @handle…</option>
            ${nsSessions.map(h => `<option value="${esc(h)}">@${esc(h)}</option>`).join("")}
          </select>` : ""}
          <input id="insp-custom-handle" placeholder="or type @handle (e.g. @architect)">
          <button id="insp-add-sub" class="sm">Subscribe Handle</button>
        </div>
      </div>` : ""}
    `;

    $$("#inspector-content [data-unsub]").forEach(btn => {
      btn.onclick = async () => {
        const h = btn.dataset.unsub;
        try {
          await wapi("POST", "channel/unsubscribe", { namespace, channel: name, session_name: h });
          await refreshOverview(false);
        } catch (e) { alert(e.message); }
      };
    });
    const addBtn = $("#insp-add-sub");
    if (addBtn) {
      addBtn.onclick = async () => {
        const picked = $("#insp-pick-handle")?.value || "";
        const typed = ($("#insp-custom-handle")?.value || "").trim().replace(/^@/, "");
        const handle = typed || picked;
        if (!handle) return;
        try {
          await wapi("POST", "channel/subscribe", { namespace, channel: name, session_name: handle });
          await refreshOverview(false);
        } catch (e) { alert(e.message); }
      };
    }
  } else {
    const sObj = (wsData.sessions || []).find(s => s.namespace === namespace && s.name === name);
    const chs = sObj?.channels || [];
    box.innerHTML = `
      <div class="insp-section">
        <h4>Session @${esc(name)}</h4>
        ${sObj ? `<div class="kv-list">
          <span>Status</span><span>${sObj.alive ? "ONLINE" : "OFFLINE"} (${esc(sObj.run_state || "idle")})</span>
          <span>Namespace</span><span>${esc(namespace)}</span>
          <span>Host</span><span>${esc(sObj.host || "—")}</span>
          <span>PID / TTY</span><span>${sObj.pid || "—"} ${esc(sObj.tty || "")}</span>
          <span>Workspace</span><span>${esc(sObj.cwd || "—")}</span>
          <span>Heartbeat</span><span>${age(sObj.last_heartbeat_at)} ago</span>
        </div>` : `<div class="muted">Handle not currently registered as a live process.</div>`}
      </div>

      <div class="insp-section">
        <h4>Subscribed Channels (${chs.length})</h4>
        <div class="chip-list">
          ${chs.length ? chs.map(c => `
            <div class="handle-chip">
              <code>${esc(c)}</code>
              ${writable ? `<button class="unsub-btn" data-leave="${esc(c)}">Leave</button>` : ""}
            </div>
          `).join("") : `<div class="muted">Not subscribed to any #channels.</div>`}
        </div>
      </div>

      ${writable ? `
      <div class="insp-section">
        <h4>Join #Channel</h4>
        <div class="inline-form" style="margin:0">
          <input id="insp-join-ch" placeholder="#channel (e.g. #arch-review)">
          <button id="insp-join-btn" class="sm">Subscribe @${esc(name)}</button>
        </div>
      </div>` : ""}
    `;

    $$("#inspector-content [data-leave]").forEach(btn => {
      btn.onclick = async () => {
        try {
          await wapi("POST", "channel/unsubscribe", { namespace, channel: btn.dataset.leave, session_name: name });
          await refreshOverview(false);
        } catch (e) { alert(e.message); }
      };
    });
    const joinBtn = $("#insp-join-btn");
    if (joinBtn) {
      joinBtn.onclick = async () => {
        const raw = ($("#insp-join-ch")?.value || "").trim();
        if (!raw) return;
        const ch = raw.startsWith("#") ? raw : "#" + raw;
        try {
          await wapi("POST", "channel/subscribe", { namespace, channel: ch, session_name: name });
          await refreshOverview(false);
        } catch (e) { alert(e.message); }
      };
    }
  }
}

// ---- Role-Scoped Memory Explorer ----
const WMEM_PAGE = 50;
let wmemOffset = 0;
async function loadWorkspaceMemories() {
  const tbody = $("#wmem-table tbody");
  const ns = $("#ws-ns-filter").value || "*";
  const ty = $("#wmem-type").value;
  const q = $("#wmem-q").value.trim();
  const qs = `memories?limit=${WMEM_PAGE}&offset=${wmemOffset}&namespace=${encodeURIComponent(ns)}`
    + (ty ? `&type=${encodeURIComponent(ty)}` : "")
    + (q ? `&q=${encodeURIComponent(q)}` : "");
  await loadTable(tbody, () => wapi("GET", qs), ({ memories }) => {
    tbody.innerHTML = (memories || []).map(m => `<tr>
      <td title="${esc(m.observed_at || "")}">${age(m.observed_at)}</td>
      <td><code>${esc(m.namespace)}</code></td>
      <td>${m.type ? `<span class="pill ${m.type === "constraint" ? "no" : "ok"}">${esc(m.type)}</span>` : ""}</td>
      <td>${esc(m.author || m.speaker || "—")}</td>
      <td style="white-space:pre-wrap;word-break:break-word">${esc(m.content)}</td>
    </tr>`).join("") || emptyRow(5, "No matching memories in your authorized scope.");
    $("#wmem-info").textContent = `page ${Math.floor(wmemOffset / WMEM_PAGE) + 1}`;
    $("#wmem-prev").disabled = wmemOffset === 0;
    $("#wmem-next").disabled = (memories || []).length < WMEM_PAGE;
  }, loadWorkspaceMemories);
}
$("#wmem-search").onclick = () => { wmemOffset = 0; loadWorkspaceMemories(); };
$("#wmem-type").onchange = () => { wmemOffset = 0; loadWorkspaceMemories(); };
$("#wmem-q").addEventListener("keydown", e => { if (e.key === "Enter") { wmemOffset = 0; loadWorkspaceMemories(); } });
$("#wmem-prev").onclick = () => { wmemOffset = Math.max(0, wmemOffset - WMEM_PAGE); loadWorkspaceMemories(); };
$("#wmem-next").onclick = () => { wmemOffset += WMEM_PAGE; loadWorkspaceMemories(); };

// ---- Telemetry & DB Overview ----
function renderTelemetry() {
  if (!wsData) return;
  const v = wsData.vitals || {};
  const u = v.usage_30d || {};
  const aliveSess = (wsData.sessions || []).filter(s => s.alive).length;
  const upMin = v.uptime_secs != null ? Math.floor(v.uptime_secs / 60) + "m" : "—";
  const ramVal = v.footprint_mb != null ? `${v.footprint_mb} MB` : (v.rss_mb != null ? `${v.rss_mb} MB` : "remote");
  const ramSub = (v.rss_mb != null && v.footprint_mb != null) ? `RSS ${v.rss_mb} MB · v${v.version || ""} · up ${upMin}` : `v${v.version || ""} · up ${upMin}`;

  $("#tel-cards").innerHTML = `
    <div class="kpi-card"><div class="kpi-lbl">Server Footprint</div><div class="kpi-val">${esc(ramVal)}</div><div class="kpi-sub">${esc(ramSub)}</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Database Size</div><div class="kpi-val">${esc(fmtBytes(v.db_size_bytes))}</div><div class="kpi-sub">${esc(v.embedding_mode || "—")}</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Semantic Facts</div><div class="kpi-val">${(v.total_facts || 0).toLocaleString()}</div><div class="kpi-sub">${(v.total_entities || 0).toLocaleString()} graph entities</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Raw Turns</div><div class="kpi-val">${(v.total_turns || 0).toLocaleString()}</div><div class="kpi-sub">across ${(wsData.namespaces || []).length} authorized ns</div></div>
    <div class="kpi-card"><div class="kpi-lbl">Active Mesh</div><div class="kpi-val">${aliveSess} @ / ${(wsData.channels || []).length} #</div><div class="kpi-sub">live sessions &amp; channels</div></div>
    <div class="kpi-card"><div class="kpi-lbl">30d Usage</div><div class="kpi-val">$${Number(u.cost_usd || 0).toFixed(4)}</div><div class="kpi-sub">${Number((u.tokens_in || 0) + (u.tokens_out || 0)).toLocaleString()} tokens</div></div>
  `;

  $("#tel-ns-table tbody").innerHTML = (wsData.namespaces || []).map(n => `<tr>
    <td><code>${esc(n.name)}</code>${n.registered ? "" : ' <span class="pill no">discovered</span>'}</td>
    <td><span class="pill ok">${n.can_write ? "read + write" : "read-only"}</span></td>
    <td><b>${(n.facts || 0).toLocaleString()}</b></td>
    <td>${(n.turns || 0).toLocaleString()}</td>
    <td>${(n.entities || 0).toLocaleString()}</td>
    <td class="muted">${esc(n.description || "—")}</td>
  </tr>`).join("") || emptyRow(6, "No authorized namespaces.");

  $("#tel-ops-table tbody").innerHTML = (v.ops_24h || []).map(o => `<tr>
    <td><code>${esc(o.op)}</code></td>
    <td>${(o.calls || 0).toLocaleString()}</td>
    <td><b>${Math.round(o.p50_ms || 0)} ms</b></td>
    <td>${Math.round(o.p95_ms || 0)} ms</td>
    <td>${o.err_pct || 0}%</td>
  </tr>`).join("") || emptyRow(5, "No operations recorded in the last 24h.");
}

// ================================================================ Existing Admin & Routing Panels

// ---- namespaces ----
async function loadNamespaces() {
  const tbody = $("#ns-table tbody");
  await loadTable(tbody, () => api("GET", "namespaces"), ({ namespaces }) => {
    tbody.innerHTML = namespaces.map(n => `<tr>
      <td><code>${esc(n.name)}</code>${n.registered ? "" : ' <span class="pill no">discovered</span>'}</td>
      <td>${esc(n.description || "")}</td>
      <td>${n.turns}</td><td>${n.facts}</td><td>${fmt(n.created_at)}</td>
      <td><button class="danger" data-del="${esc(n.name)}">delete</button></td></tr>`).join("")
      || emptyRow(6, `No namespaces yet — create one above or run: <code>memnos namespace add &lt;name&gt;</code>`);
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

// ---- principals + grants + tokens ----
async function loadPrincipals() {
  const tbody = $("#pr-table tbody");
  await loadTable(tbody, () => api("GET", "principals"), ({ principals }) => {
    tbody.innerHTML = principals.map(p => `<tr>
      <td>${p.id}</td><td>${esc(p.name)}</td><td>${esc(p.kind)}</td><td>${p.active_tokens}</td></tr>`).join("")
      || emptyRow(4, `No principals yet.`);
    const opts = principals.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
    $("#tok-principal").innerHTML = opts; $("#gr-principal").innerHTML = opts;
    loadTokens(); loadGrants();
  }, loadPrincipals);
}
$("#pr-create").onclick = async () => {
  const name = $("#pr-name").value.trim(); if (!name) return;
  try { await api("POST", "principals", { name, kind: $("#pr-kind").value }); $("#pr-name").value = ""; loadPrincipals(); }
  catch (e) { alert(e.message); }
};
async function loadTokens() {
  const tbody = $("#tok-table tbody");
  const pid = $("#tok-principal").value;
  if (!pid) { tbody.innerHTML = emptyRow(7, `Create a principal first.`); return; }
  $("#tok-who").textContent = $("#tok-principal").selectedOptions[0]?.textContent || "";
  await loadTable(tbody, () => api("GET", "tokens?principal=" + pid), ({ tokens }) => {
    tbody.innerHTML = tokens.map(t => `<tr>
      <td>${t.id}</td><td><code>${esc(t.hint || "—")}</code></td><td>${esc(t.label || "")}</td><td>${fmt(t.created_at)}</td><td>${fmt(t.expires_at)}</td>
      <td><span class="pill ${t.revoked ? "no" : "ok"}">${t.revoked ? "revoked" : "active"}</span></td>
      <td>${t.revoked ? "" : `<button class="danger" data-rv="${t.id}">revoke</button>`}</td></tr>`).join("")
      || emptyRow(7, `No tokens for this principal.`);
    $$("#tok-table [data-rv]").forEach(b => b.onclick = async () => {
      await api("POST", "tokens/revoke", { id: +b.dataset.rv }); loadTokens();
    });
  }, loadTokens);
}
$("#tok-principal").onchange = loadTokens;
$("#tok-mint").onclick = async () => {
  const ttl = $("#tok-ttl").value;
  try {
    const { token } = await api("POST", "tokens", {
      principal_id: +$("#tok-principal").value, label: $("#tok-label").value.trim() || null,
      ttl_days: ttl ? +ttl : null });
    const b = $("#tok-show"); b.hidden = false;
    b.innerHTML = `Copy this token now — it is shown <b>once</b>:<br><code>${esc(token)}</code>`;
    $("#tok-label").value = ""; $("#tok-ttl").value = ""; loadTokens(); loadPrincipals();
  } catch (e) { alert(e.message); }
};
async function loadGrants() {
  const tbody = $("#gr-table tbody");
  const pid = $("#gr-principal").value;
  if (!pid) { tbody.innerHTML = emptyRow(4, `Create a principal first.`); return; }
  await loadTable(tbody, () => api("GET", "grants?principal=" + pid), ({ grants }) => {
    tbody.innerHTML = grants.map(g => `<tr>
      <td><code>${esc(g.namespace)}</code></td>
      <td>${g.can_read ? "✓" : ""}</td><td>${g.can_write ? "✓" : ""}</td>
      <td><button class="danger" data-rg="${esc(g.namespace)}">revoke</button></td></tr>`).join("")
      || emptyRow(4, `No grants.`);
    $$("#gr-table [data-rg]").forEach(b => b.onclick = async () => {
      await api("DELETE", "grants?principal=" + pid + "&namespace=" + encodeURIComponent(b.dataset.rg)); loadGrants();
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

// ---- routing ----
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
      <button class="bind-save" data-id="${b.id}">save</button>
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
  const hostIds = Object.keys(byHost);
  for (const mid of hostIds) {
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
    <td><button class="host-save" data-mid="${esc(h.machine_id)}">rename</button></td></tr>`).join("")
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

// ---- audit, health, secrets, usage, cli, settings ----
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
    const groups = j.groups || [];
    box.innerHTML = groups.map(g => `
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
    banner.style.cssText = "background:#2a1f0d;border:1px solid var(--warn);padding:8px 12px;border-radius:8px;margin-bottom:12px;";
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
