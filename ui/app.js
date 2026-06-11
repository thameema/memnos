"use strict";
// memnos console — zero-build vanilla JS. Token in localStorage, sent as Bearer.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const tok = () => localStorage.getItem("memnos_admin_token") || "";

const API_TIMEOUT_MS = 30000;   // per-request guard: a wedged query must not hang the UI forever

async function api(method, path, body) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), API_TIMEOUT_MS);
  let r;
  try {
    r = await fetch("/admin/api/" + path, {
      method,
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tok() },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? "request timed out — the dataset may be large or the server busy"
      : "server unreachable — is memnos running?");
  } finally { clearTimeout(timer); }
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 || r.status === 403) {   // revoked/expired/invalid token → back to login
    localStorage.removeItem("memnos_admin_token");
    show(false);
    $("#loginerr").textContent = "Session expired or token revoked — sign in again with a valid admin token.";
    throw new Error("unauthorized");
  }
  if (!r.ok) throw new Error((j.error || ("HTTP " + r.status)) + (j.msg ? ": " + j.msg : ""));
  return j;
}
const esc = (s) => String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const fmt = (t) => t ? new Date(t).toLocaleString() : "—";

// ---- loading / error / empty states (never show a silently blank panel) ----
function cols(tbody) { return $$("th", tbody.closest("table")).length || 1; }
function showLoading(tbody) {
  const n = cols(tbody);
  tbody.innerHTML = `<tr class="state"><td colspan="${n}"><span class="spinner"></span> loading…</td></tr>`;
  // slow-query reassurance: after 3s tell the user it's the data, not a hang
  return setTimeout(() => {
    const td = $("tr.state td", tbody);
    if (td) td.innerHTML = `<span class="spinner"></span> still loading… the dataset is large`;
  }, 3000);
}
function showError(tbody, msg, retry) {
  const n = cols(tbody);
  tbody.innerHTML = `<tr class="state"><td colspan="${n}">
    <span class="errbox">${esc(msg)} <button class="retry">retry</button></span></td></tr>`;
  $("button.retry", tbody).onclick = retry;
}
const emptyRow = (n, msg) => `<tr class="state"><td colspan="${n}" class="muted">${msg}</td></tr>`;
// wrap a table loader with loading → render | error(+retry). render(data) fills the tbody.
async function loadTable(tbody, fetcher, render, self) {
  const t = showLoading(tbody);
  try { render(await fetcher()); }
  catch (e) { if (e.message !== "unauthorized") showError(tbody, e.message, self); }
  finally { clearTimeout(t); }
}

// ---- auth ----
async function signIn(t) {
  localStorage.setItem("memnos_admin_token", t);
  try { await api("GET", "provider"); show(true); } // any admin call validates
  catch (e) { localStorage.removeItem("memnos_admin_token"); $("#loginerr").textContent = e.message; }
}
function show(on) {
  $("#login").hidden = on; $("#app").hidden = !on;
  $("#logout").hidden = !on; $("#who").textContent = on ? "admin" : "";
  if (on) loadAll();
}
$("#signin").onclick = () => signIn($("#token").value.trim());
$("#token").addEventListener("keydown", e => { if (e.key === "Enter") $("#signin").click(); });
$("#logout").onclick = () => { localStorage.removeItem("memnos_admin_token"); show(false); };

// ---- tabs ----
$$(".tabs button").forEach(b => b.onclick = () => {
  $$(".tabs button").forEach(x => x.classList.remove("active")); b.classList.add("active");
  $$(".tab").forEach(t => t.hidden = true); $("#tab-" + b.dataset.tab).hidden = false;
});

function loadAll() { loadNamespaces(); loadPrincipals(); }

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
        // Cancel still offers delete-without-purge:
        if (!confirm(`Delete namespace "${b.dataset.del}" but KEEP its data?`)) return;
        await api("DELETE", "namespaces?name=" + encodeURIComponent(b.dataset.del));
      } else {
        await api("DELETE", "namespaces?purge=1&name=" + encodeURIComponent(b.dataset.del));
      }
      loadNamespaces();
    });
  }, loadNamespaces);
}
$("#ns-create").onclick = async () => {
  const name = $("#ns-name").value.trim(); if (!name) return;
  try { await api("POST", "namespaces", { name, description: $("#ns-desc").value.trim() });
    $("#ns-name").value = ""; $("#ns-desc").value = ""; loadNamespaces(); }
  catch (e) { alert(e.message); }
};

// ---- memory feed (recent memories across namespaces, paginated) ----
const FEED_PAGE = 50;
let feedOffset = 0;
const age = (t) => {
  if (!t) return "—";
  const s = Math.max(0, (Date.now() - new Date(t).getTime()) / 1000);
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
};
async function loadFeed() {
  const tbody = $("#feed-table tbody");
  const ns = $("#feed-ns").value.trim(), ty = $("#feed-type").value;
  const qs = `memory/feed?limit=${FEED_PAGE}&offset=${feedOffset}`
    + (ns ? "&namespace=" + encodeURIComponent(ns) : "") + (ty ? "&type=" + ty : "");
  await loadTable(tbody, () => api("GET", qs), ({ memories }) => {
    tbody.innerHTML = memories.map(m => `<tr>
      <td title="${esc(m.observed_at || "")}">${age(m.observed_at)}</td>
      <td><code>${esc(m.namespace)}</code></td>
      <td>${m.type ? `<span class="pill ${m.type === "constraint" ? "no" : "ok"}">${esc(m.type)}</span>` : ""}</td>
      <td>${esc(m.author || "—")}</td>
      <td>${esc(m.content)}</td></tr>`).join("")
      || emptyRow(5, `No memories yet — they appear here as clients call <code>/remember</code>.`);
    const page = Math.floor(feedOffset / FEED_PAGE) + 1;
    $("#feed-info").textContent = `page ${page}`;
    $("#feed-prev").disabled = feedOffset === 0;
    $("#feed-next").disabled = memories.length < FEED_PAGE;
  }, loadFeed);
}
$("#feed-prev").onclick = () => { feedOffset = Math.max(0, feedOffset - FEED_PAGE); loadFeed(); };
$("#feed-next").onclick = () => { feedOffset += FEED_PAGE; loadFeed(); };
$("#feed-refresh").onclick = () => { feedOffset = 0; loadFeed(); };
$("#feed-type").onchange = () => { feedOffset = 0; loadFeed(); };
$("#feed-ns").addEventListener("keydown", e => { if (e.key === "Enter") { feedOffset = 0; loadFeed(); } });

// ---- principals + grants ----
async function loadPrincipals() {
  const tbody = $("#pr-table tbody");
  await loadTable(tbody, () => api("GET", "principals"), ({ principals }) => {
    tbody.innerHTML = principals.map(p => `<tr>
      <td>${p.id}</td><td>${esc(p.name)}</td><td>${esc(p.kind)}</td><td>${p.active_tokens}</td></tr>`).join("")
      || emptyRow(4, `No principals yet — create one above or run: <code>memnos principal &lt;name&gt;</code>`);
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
  if (!pid) { tbody.innerHTML = emptyRow(7, `No principals yet — create one in <b>Principals &amp; Grants</b> first.`); return; }
  $("#tok-who").textContent = $("#tok-principal").selectedOptions[0].textContent;
  await loadTable(tbody, () => api("GET", "tokens?principal=" + pid), ({ tokens }) => {
    tbody.innerHTML = tokens.map(t => `<tr>
      <td>${t.id}</td><td><code>${esc(t.hint || "—")}</code></td><td>${esc(t.label || "")}</td><td>${fmt(t.created_at)}</td><td>${fmt(t.expires_at)}</td>
      <td><span class="pill ${t.revoked ? "no" : "ok"}">${t.revoked ? "revoked" : "active"}</span></td>
      <td>${t.revoked ? "" : `<button class="danger" data-rv="${t.id}">revoke</button>`}</td></tr>`).join("")
      || emptyRow(7, `No tokens for this principal — mint one above or run: <code>memnos token &lt;principal&gt;</code>`);
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
  if (!pid) { tbody.innerHTML = emptyRow(4, `No principals yet — create one above first.`); return; }
  await loadTable(tbody, () => api("GET", "grants?principal=" + pid), ({ grants }) => {
    tbody.innerHTML = grants.map(g => `<tr>
      <td><code>${esc(g.namespace)}</code></td>
      <td>${g.can_read ? "✓" : ""}</td><td>${g.can_write ? "✓" : ""}</td>
      <td><button class="danger" data-rg="${esc(g.namespace)}">revoke</button></td></tr>`).join("")
      || emptyRow(4, `No grants — add one above or run: <code>memnos grant &lt;principal&gt; &lt;namespace&gt;</code>`);
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

// ---- dashboard (each panel loads + fails independently — one bad call can't blank the page) ----
async function loadHealth() {
  const box = $("#dash-health");
  box.innerHTML = `<span class="muted"><span class="spinner"></span> loading…</span>`;
  try {
    const h = await api("GET", "health");
    box.innerHTML = h.findings.map(f =>
      `<div><span class="lvl ${esc(f.level)}">${esc(f.level)}</span>${esc(f.msg)}</div>`).join("")
      || `<span class="muted">OK — no findings.</span>`;
  } catch (e) {
    if (e.message === "unauthorized") return;
    box.innerHTML = `<span class="errbox">${esc(e.message)} <button class="retry">retry</button></span>`;
    $("button.retry", box).onclick = loadHealth;
  }
}
async function loadStats() {
  const tbody = $("#dash-stats tbody");
  await loadTable(tbody, () => api("GET", "stats"), ({ ops }) => {
    tbody.innerHTML = ops.map(o => `<tr><td>${esc(o.action)}</td><td>${o.calls}</td>
      <td>${o.error_pct ?? 0}</td><td>${o.p50_ms ?? "—"}</td><td>${o.p95_ms ?? "—"}</td>
      <td>${o.recall_empty_pct ?? ""}</td></tr>`).join("")
      || emptyRow(6, `No API calls in the last 24h — traffic stats appear here once clients call the API.`);
  }, loadStats);
}
async function loadUsage() {
  const tbody = $("#dash-usage tbody");
  await loadTable(tbody, () => api("GET", "usage"), ({ usage }) => {
    tbody.innerHTML = usage.map(o => `<tr><td>${esc(o.op)}</td><td>${o.n}</td>
      <td>${o.tin ?? 0}</td><td>${o.tout ?? 0}</td><td>${o.cost ?? 0}</td></tr>`).join("")
      || emptyRow(5, `No usage recorded yet — token/cost accounting appears after the first remember/recall.`);
  }, loadUsage);
}
// audit: server-side pagination (limit/offset), total is the server's planner estimate
const AUDIT_PAGE = 100;
let auditOffset = 0;
async function loadAudit() {
  const tbody = $("#dash-audit tbody");
  await loadTable(tbody,
    () => api("GET", `audit?limit=${AUDIT_PAGE}&offset=${auditOffset}`),
    (j) => {
      tbody.innerHTML = j.audit.map(r => `<tr><td>${fmt(r.ts)}</td><td>${r.principal_id ?? "—"}</td>
        <td>${esc(r.action)}</td><td>${esc(r.namespace || "")}</td>
        <td>${r.status ?? (r.ok ? 200 : "—")}</td><td>${r.latency_ms ?? ""}</td></tr>`).join("")
        || emptyRow(6, `No activity yet — every API call is audited and appears here.`);
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
    $("#vault-state").textContent = j.unlocked ? "· unlocked" : "· LOCKED (set MEMNOS_SECRET_KEY in .env)";
    tbody.innerHTML = (j.secrets || []).map(s => `<tr>
      <td><code>${esc(s.name)}</code></td><td>${esc(s.description || "")}</td><td>${fmt(s.updated_at)}</td>
      <td><button class="danger" data-ds="${esc(s.name)}">delete</button></td></tr>`).join("")
      || emptyRow(4, `No secrets stored — add one above or run: <code>memnos secret set &lt;name&gt; --value …</code>`);
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
  } catch (e) {
    if (e.message === "unauthorized") return;
    box.innerHTML = `<span class="errbox">${esc(e.message)} <button class="retry">retry</button></span>`;
    $("button.retry", box).onclick = loadSettings;
  }
}
// ---- CLI reference (bundled static JSON generated by `memnos docs-gen`) ----
let cliLoaded = false;
async function loadCliRef() {
  if (cliLoaded) return;                       // static content — load once
  const box = $("#cli-ref");
  box.innerHTML = `<span class="muted"><span class="spinner"></span> loading…</span>`;
  try {
    const r = await fetch("/admin/cli-reference.json");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    const groups = j.groups || [];
    if (!groups.length) { box.innerHTML = `<span class="muted">No CLI reference bundled with this build.</span>`; return; }
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
    box.innerHTML = `<span class="errbox">couldn't load the CLI reference (${esc(e.message)}) <button class="retry">retry</button></span>`;
    $("button.retry", box).onclick = loadCliRef;
  }
}

$$(".tabs button").forEach(b => b.addEventListener("click", () => {
  if (b.dataset.tab === "feed") { feedOffset = 0; loadFeed(); }
  if (b.dataset.tab === "dashboard") loadDashboard();
  if (b.dataset.tab === "secrets") loadSecrets();
  if (b.dataset.tab === "cli") loadCliRef();
  if (b.dataset.tab === "settings") loadSettings();
}));

// ---- boot ----
if (tok()) show(true); else show(false);
