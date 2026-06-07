"use strict";
// memnos console — zero-build vanilla JS. Token in localStorage, sent as Bearer.
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const tok = () => localStorage.getItem("memnos_admin_token") || "";

async function api(method, path, body) {
  const r = await fetch("/admin/api/" + path, {
    method,
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + tok() },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP " + r.status) + (j.msg ? ": " + j.msg : ""));
  return j;
}
const esc = (s) => String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const fmt = (t) => t ? new Date(t).toLocaleString() : "—";

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
  const { namespaces } = await api("GET", "namespaces");
  $("#ns-table tbody").innerHTML = namespaces.map(n => `<tr>
    <td><code>${esc(n.name)}</code></td><td>${esc(n.description || "")}</td>
    <td>${n.turns}</td><td>${n.facts}</td><td>${fmt(n.created_at)}</td>
    <td><button class="danger" data-del="${esc(n.name)}">delete</button></td></tr>`).join("")
    || `<tr><td colspan=6 class=muted>No namespaces yet — create one above.</td></tr>`;
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
}
$("#ns-create").onclick = async () => {
  const name = $("#ns-name").value.trim(); if (!name) return;
  try { await api("POST", "namespaces", { name, description: $("#ns-desc").value.trim() });
    $("#ns-name").value = ""; $("#ns-desc").value = ""; loadNamespaces(); }
  catch (e) { alert(e.message); }
};

// ---- principals + grants ----
async function loadPrincipals() {
  const { principals } = await api("GET", "principals");
  $("#pr-table tbody").innerHTML = principals.map(p => `<tr>
    <td>${p.id}</td><td>${esc(p.name)}</td><td>${esc(p.kind)}</td><td>${p.active_tokens}</td></tr>`).join("");
  const opts = principals.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $("#tok-principal").innerHTML = opts; $("#gr-principal").innerHTML = opts;
  loadTokens(); loadGrants();
}
$("#pr-create").onclick = async () => {
  const name = $("#pr-name").value.trim(); if (!name) return;
  try { await api("POST", "principals", { name, kind: $("#pr-kind").value }); $("#pr-name").value = ""; loadPrincipals(); }
  catch (e) { alert(e.message); }
};
async function loadTokens() {
  const pid = $("#tok-principal").value; if (!pid) return;
  $("#tok-who").textContent = $("#tok-principal").selectedOptions[0].textContent;
  const { tokens } = await api("GET", "tokens?principal=" + pid);
  $("#tok-table tbody").innerHTML = tokens.map(t => `<tr>
    <td>${t.id}</td><td><code>${esc(t.hint || "—")}</code></td><td>${esc(t.label || "")}</td><td>${fmt(t.created_at)}</td><td>${fmt(t.expires_at)}</td>
    <td><span class="pill ${t.revoked ? "no" : "ok"}">${t.revoked ? "revoked" : "active"}</span></td>
    <td>${t.revoked ? "" : `<button class="danger" data-rv="${t.id}">revoke</button>`}</td></tr>`).join("");
  $$("#tok-table [data-rv]").forEach(b => b.onclick = async () => {
    await api("POST", "tokens/revoke", { id: +b.dataset.rv }); loadTokens();
  });
}
$("#tok-principal").onchange = loadTokens;
$("#tok-mint").onclick = async () => {
  const ttl = $("#tok-ttl").value;
  const { token } = await api("POST", "tokens", {
    principal_id: +$("#tok-principal").value, label: $("#tok-label").value.trim() || null,
    ttl_days: ttl ? +ttl : null });
  const b = $("#tok-show"); b.hidden = false;
  b.innerHTML = `Copy this token now — it is shown <b>once</b>:<br><code>${esc(token)}</code>`;
  $("#tok-label").value = ""; $("#tok-ttl").value = ""; loadTokens(); loadPrincipals();
};
async function loadGrants() {
  const pid = $("#gr-principal").value; if (!pid) return;
  const { grants } = await api("GET", "grants?principal=" + pid);
  $("#gr-table tbody").innerHTML = grants.map(g => `<tr>
    <td><code>${esc(g.namespace)}</code></td>
    <td>${g.can_read ? "✓" : ""}</td><td>${g.can_write ? "✓" : ""}</td>
    <td><button class="danger" data-rg="${esc(g.namespace)}">revoke</button></td></tr>`).join("")
    || `<tr><td colspan=4 class=muted>No grants.</td></tr>`;
  $$("#gr-table [data-rg]").forEach(b => b.onclick = async () => {
    await api("DELETE", "grants?principal=" + pid + "&namespace=" + encodeURIComponent(b.dataset.rg)); loadGrants();
  });
}
$("#gr-principal").onchange = loadGrants;
$("#gr-add").onclick = async () => {
  await api("POST", "grants", { principal_id: +$("#gr-principal").value, namespace: $("#gr-namespace").value.trim(),
    can_read: $("#gr-read").checked, can_write: $("#gr-write").checked });
  $("#gr-namespace").value = ""; loadGrants();
};

// ---- dashboard ----
async function loadDashboard() {
  const h = await api("GET", "health");
  $("#dash-health").innerHTML = h.findings.map(f =>
    `<div><span class="lvl ${esc(f.level)}">${esc(f.level)}</span>${esc(f.msg)}</div>`).join("") || "<span class=muted>OK — no findings.</span>";
  const s = await api("GET", "stats");
  $("#dash-stats tbody").innerHTML = s.ops.map(o => `<tr><td>${esc(o.action)}</td><td>${o.calls}</td>
    <td>${o.error_pct ?? 0}</td><td>${o.p50_ms ?? "—"}</td><td>${o.p95_ms ?? "—"}</td>
    <td>${o.recall_empty_pct ?? ""}</td></tr>`).join("");
  const u = await api("GET", "usage");
  $("#dash-usage tbody").innerHTML = u.usage.map(o => `<tr><td>${esc(o.op)}</td><td>${o.n}</td>
    <td>${o.tin ?? 0}</td><td>${o.tout ?? 0}</td><td>${o.cost ?? 0}</td></tr>`).join("");
  const a = await api("GET", "audit?limit=40");
  $("#dash-audit tbody").innerHTML = a.audit.map(r => `<tr><td>${fmt(r.ts)}</td><td>${r.principal_id ?? "—"}</td>
    <td>${esc(r.action)}</td><td>${esc(r.namespace || "")}</td>
    <td>${r.status ?? (r.ok ? 200 : "—")}</td><td>${r.latency_ms ?? ""}</td></tr>`).join("");
}
async function loadSecrets() {
  const j = await api("GET", "secrets");
  $("#vault-state").textContent = j.unlocked ? "· unlocked" : "· LOCKED (set MEMNOS_SECRET_KEY in .env)";
  $("#sec-table tbody").innerHTML = (j.secrets || []).map(s => `<tr>
    <td><code>${esc(s.name)}</code></td><td>${esc(s.description || "")}</td><td>${fmt(s.updated_at)}</td>
    <td><button class="danger" data-ds="${esc(s.name)}">delete</button></td></tr>`).join("")
    || `<tr><td colspan=4 class=muted>No secrets stored.</td></tr>`;
  $$("#sec-table [data-ds]").forEach(b => b.onclick = async () => {
    if (!confirm(`Delete secret "${b.dataset.ds}"?`)) return;
    await api("DELETE", "secrets?name=" + encodeURIComponent(b.dataset.ds)); loadSecrets();
  });
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
  const p = await api("GET", "provider");
  $("#set-provider").innerHTML = `Mode: <b>${esc(p.mode)}</b> · embedding dim <b>${p.dim}</b> ·
    extract model <code>${esc(p.extract_model)}</code> · OpenAI key ${p.key_present ? '<span class="pill ok">present</span>' : '<span class="pill no">missing</span>'}`;
}
$$(".tabs button").forEach(b => b.addEventListener("click", () => {
  if (b.dataset.tab === "dashboard") loadDashboard();
  if (b.dataset.tab === "secrets") loadSecrets();
  if (b.dataset.tab === "settings") loadSettings();
}));

// ---- boot ----
if (tok()) show(true); else show(false);
