"""
engram_api.routers.learning_admin — Learning admin dashboard API.

REST endpoints for inspecting and managing the learning subsystem
(heuristics, episodes, quality records). A browser dashboard is served
at GET /learning/dashboard.

Endpoints
---------
GET  /learning/dashboard          — HTML admin dashboard
GET  /learning/stats              — summary statistics
GET  /learning/heuristics         — list heuristics for a namespace
DELETE /learning/heuristics/{id}  — delete a heuristic
GET  /learning/episodes/recent    — recent episodic records
POST /learning/reflect            — trigger reflection cycle
"""

from __future__ import annotations

import logging
import pathlib
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from engram_api.auth import check_namespace_access, require_api_key_entry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/learning", tags=["learning"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class HeuristicOut(BaseModel):
    id: str
    namespace: str
    rule: str
    rationale: str
    confidence: float
    triggered_count: int
    overridden_count: int
    applies_to_tags: list[str]
    created_at: datetime
    last_triggered_at: datetime | None = None


class EpisodeOut(BaseModel):
    id: str
    namespace: str
    original_prompt: str
    agent_used: str | None
    outcome: str
    quality_score: float | None
    duration_s: float
    token_cost: int
    created_at: datetime


class LearningStats(BaseModel):
    namespace: str
    heuristic_count: int
    episode_count_7d: int
    avg_quality_7d: float | None
    success_rate_7d: float | None
    top_agents: list[dict[str, Any]]


class ReflectRequest(BaseModel):
    namespace: str
    lookback_days: int = 7


class ReflectResponse(BaseModel):
    namespace: str
    heuristics_added: int
    episodes_analysed: int


# ---------------------------------------------------------------------------
# Store helpers (lazy, one connection per request)
# ---------------------------------------------------------------------------

def _get_heuristic_store():
    try:
        from engram_learning.heuristic_store import HeuristicStore  # type: ignore
        return HeuristicStore()
    except ImportError:
        return None


def _get_episode_store():
    try:
        from engram_learning.episode_store import EpisodeStore  # type: ignore
        return EpisodeStore()
    except ImportError:
        return None


def _get_quality_store():
    try:
        from engram_learning.quality_store import QualityStore  # type: ignore
        return QualityStore()
    except ImportError:
        return None


def _require_learning():
    store = _get_heuristic_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Learning subsystem is not available. "
                "Install engram-learning: pip install engram-learning"
            ),
        )
    return store


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/dashboard", include_in_schema=False, response_class=HTMLResponse)
async def learning_dashboard():
    """Serve the learning admin HTML dashboard."""
    static = pathlib.Path(__file__).parent.parent / "static" / "learning_dashboard.html"
    if static.exists():
        return HTMLResponse(content=static.read_text(encoding="utf-8"))
    return HTMLResponse(content=_INLINE_DASHBOARD_HTML)


@router.get("/stats", response_model=LearningStats)
async def learning_stats(
    ns: str = Query(..., description="Namespace to summarise"),
    key_entry=Depends(require_api_key_entry),
) -> LearningStats:
    """Return summary statistics for the learning subsystem in *ns*."""
    await check_namespace_access(key_entry, ns)

    heuristic_count = 0
    h_store = _get_heuristic_store()
    if h_store is not None:
        try:
            await h_store.init()
            heuristics = await h_store.get_all(ns)
            heuristic_count = len(heuristics)
        except Exception as exc:
            logger.debug("heuristic stats failed: %s", exc)

    episode_count = 0
    avg_quality: float | None = None
    success_rate: float | None = None
    top_agents: list[dict] = []

    e_store = _get_episode_store()
    if e_store is not None:
        try:
            await e_store.init()
            recent = await e_store.get_recent(ns, days=7)
            episode_count = len(recent)
            if recent:
                quality_scores = [ep.quality_score for ep in recent if ep.quality_score is not None]
                avg_quality = round(sum(quality_scores) / len(quality_scores), 3) if quality_scores else None
                successes = sum(1 for ep in recent if str(ep.outcome) in ("Outcome.SUCCESS", "SUCCESS"))
                success_rate = round(successes / len(recent), 3)

                # Top 3 agents by episode count
                agent_counts: dict[str, int] = {}
                for ep in recent:
                    a = ep.agent_used or "default"
                    agent_counts[a] = agent_counts.get(a, 0) + 1
                top_agents = [
                    {"agent": a, "episodes": c}
                    for a, c in sorted(agent_counts.items(), key=lambda x: -x[1])[:3]
                ]
        except Exception as exc:
            logger.debug("episode stats failed: %s", exc)

    return LearningStats(
        namespace=ns,
        heuristic_count=heuristic_count,
        episode_count_7d=episode_count,
        avg_quality_7d=avg_quality,
        success_rate_7d=success_rate,
        top_agents=top_agents,
    )


@router.get("/heuristics", response_model=list[HeuristicOut])
async def list_heuristics(
    ns: str = Query(..., description="Namespace to query"),
    limit: int = Query(50, ge=1, le=500),
    key_entry=Depends(require_api_key_entry),
) -> list[HeuristicOut]:
    """List heuristic rules for *ns*, ordered by confidence descending."""
    await check_namespace_access(key_entry, ns)
    store = _require_learning()
    try:
        await store.init()
        heuristics = await store.get_all(ns)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    heuristics.sort(key=lambda h: h.confidence, reverse=True)
    return [
        HeuristicOut(
            id=h.id,
            namespace=h.namespace,
            rule=h.rule,
            rationale=h.rationale,
            confidence=h.confidence,
            triggered_count=h.triggered_count,
            overridden_count=h.overridden_count,
            applies_to_tags=list(h.applies_to_tags or []),
            created_at=h.created_at,
            last_triggered_at=h.last_triggered_at,
        )
        for h in heuristics[:limit]
    ]


@router.delete("/heuristics/{heuristic_id}", status_code=200)
async def delete_heuristic(
    heuristic_id: str,
    ns: str = Query(..., description="Namespace that owns the heuristic"),
    key_entry=Depends(require_api_key_entry),
):
    """Delete a heuristic rule by ID."""
    await check_namespace_access(key_entry, ns)
    store = _require_learning()
    try:
        await store.init()
        await store.delete(heuristic_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"deleted": heuristic_id}


@router.get("/episodes/recent", response_model=list[EpisodeOut])
async def recent_episodes(
    ns: str = Query(..., description="Namespace to query"),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
    key_entry=Depends(require_api_key_entry),
) -> list[EpisodeOut]:
    """Return the most recent episodic records for *ns*."""
    await check_namespace_access(key_entry, ns)
    e_store = _get_episode_store()
    if e_store is None:
        raise HTTPException(status_code=503, detail="Learning subsystem not installed")
    try:
        await e_store.init()
        episodes = await e_store.get_recent(ns, days=days)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    episodes.sort(key=lambda ep: ep.created_at, reverse=True)
    return [
        EpisodeOut(
            id=ep.id,
            namespace=ep.namespace,
            original_prompt=ep.original_prompt[:300],
            agent_used=ep.agent_used,
            outcome=str(ep.outcome.value if hasattr(ep.outcome, "value") else ep.outcome),
            quality_score=ep.quality_score,
            duration_s=ep.duration_s,
            token_cost=ep.token_cost,
            created_at=ep.created_at,
        )
        for ep in episodes[:limit]
    ]


@router.post("/reflect", response_model=ReflectResponse, status_code=202)
async def trigger_reflection(
    req: ReflectRequest,
    key_entry=Depends(require_api_key_entry),
) -> ReflectResponse:
    """Trigger a reflection cycle to distil heuristics from recent episodes."""
    await check_namespace_access(key_entry, req.namespace)

    try:
        import os
        from engram_learning.episode_store import EpisodeStore  # type: ignore
        from engram_learning.heuristic_store import HeuristicStore  # type: ignore
        from engram_learning.reflection import ReflectionService  # type: ignore

        episode_store = EpisodeStore()
        await episode_store.init()
        heuristic_store = HeuristicStore()
        await heuristic_store.init()

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key or api_key.startswith("sk-ant-placeholder"):
            raise HTTPException(
                status_code=503,
                detail="Reflection requires ANTHROPIC_API_KEY to be configured",
            )

        reflection = ReflectionService(
            api_key=api_key,
            model="claude-haiku-4-5-20251001",
            episode_store=episode_store,
            heuristic_store=heuristic_store,
            namespace=req.namespace,
            engram_client=None,
        )

        before_count = len(await heuristic_store.get_all(req.namespace))
        episodes_before = await episode_store.get_recent(req.namespace, days=req.lookback_days)
        await reflection.run(lookback_days=req.lookback_days)
        after_count = len(await heuristic_store.get_all(req.namespace))

        return ReflectResponse(
            namespace=req.namespace,
            heuristics_added=max(0, after_count - before_count),
            episodes_analysed=len(episodes_before),
        )
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="engram-learning package is not installed",
        )
    except Exception as exc:
        logger.exception("Reflection failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Inline dashboard HTML (fallback if static file not found)
# ---------------------------------------------------------------------------

_INLINE_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>engram — Learning Admin</title>
<style>
  body{font-family:system-ui,sans-serif;margin:0;background:#0f1117;color:#e2e8f0}
  nav{background:#1a1f2e;padding:12px 24px;border-bottom:1px solid #2d3748;display:flex;align-items:center;gap:16px}
  nav a{color:#63b3ed;text-decoration:none;font-size:14px}
  h1{font-size:20px;margin:0;color:#90cdf4}
  .container{max-width:1100px;margin:0 auto;padding:24px}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:24px}
  .card{background:#1a1f2e;border:1px solid #2d3748;border-radius:8px;padding:16px}
  .card-val{font-size:28px;font-weight:700;color:#63b3ed}
  .card-label{font-size:12px;color:#718096;margin-top:4px}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{background:#1a1f2e;color:#a0aec0;text-align:left;padding:8px 12px;border-bottom:1px solid #2d3748}
  td{padding:8px 12px;border-bottom:1px solid #1a1f2e;vertical-align:top}
  tr:hover td{background:#1a1f2e}
  .badge{display:inline-block;border-radius:4px;padding:2px 6px;font-size:11px}
  .badge-ok{background:#22543d;color:#68d391}
  .badge-fail{background:#742a2a;color:#fc8181}
  .badge-corr{background:#44337a;color:#d6bcfa}
  input,select{background:#1a1f2e;border:1px solid #2d3748;color:#e2e8f0;padding:6px 10px;border-radius:4px;font-size:14px}
  button{background:#3182ce;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;font-size:14px}
  button:hover{background:#2b6cb0}
  button.danger{background:#e53e3e}
  button.danger:hover{background:#c53030}
  .section{margin-bottom:32px}
  .section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
  .section-title{font-size:16px;font-weight:600;color:#e2e8f0}
  .tag{background:#2d3748;border-radius:3px;padding:1px 5px;font-size:11px;margin-right:4px}
  .conf-bar{background:#2d3748;border-radius:2px;height:6px;width:80px;display:inline-block;vertical-align:middle}
  .conf-fill{background:#63b3ed;border-radius:2px;height:6px}
  #status{font-size:13px;color:#a0aec0;padding:4px 0}
</style>
</head>
<body>
<nav>
  <h1>engram</h1>
  <a href="/dashboard">Graph</a>
  <a href="/learning/dashboard" style="color:#e2e8f0">Learning</a>
  <a href="/docs">API</a>
</nav>
<div class="container">
  <div style="display:flex;gap:12px;align-items:center;margin-bottom:20px">
    <input id="nsInput" placeholder="Namespace (e.g. org:acme)" style="width:260px">
    <input id="apiKeyInput" placeholder="API Key" type="password" style="width:180px">
    <button onclick="loadAll()">Load</button>
    <span id="status"></span>
  </div>

  <div class="cards" id="statsCards">
    <div class="card"><div class="card-val" id="stat-heuristics">—</div><div class="card-label">Heuristics</div></div>
    <div class="card"><div class="card-val" id="stat-episodes">—</div><div class="card-label">Episodes (7d)</div></div>
    <div class="card"><div class="card-val" id="stat-quality">—</div><div class="card-label">Avg Quality</div></div>
    <div class="card"><div class="card-val" id="stat-success">—</div><div class="card-label">Success Rate</div></div>
  </div>

  <div class="section">
    <div class="section-header">
      <span class="section-title">Heuristic Rules</span>
      <button onclick="triggerReflect()">Run Reflection</button>
    </div>
    <table>
      <thead><tr><th>Rule</th><th>Confidence</th><th>Triggered</th><th>Tags</th><th></th></tr></thead>
      <tbody id="heuristicsBody"><tr><td colspan="5" style="color:#718096">Load a namespace above</td></tr></tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-header">
      <span class="section-title">Recent Episodes (7d)</span>
      <span></span>
    </div>
    <table>
      <thead><tr><th>Prompt</th><th>Agent</th><th>Outcome</th><th>Quality</th><th>Duration</th><th>Created</th></tr></thead>
      <tbody id="episodesBody"><tr><td colspan="6" style="color:#718096">Load a namespace above</td></tr></tbody>
    </table>
  </div>
</div>

<script>
const BASE = window.location.origin;
let NS = '', KEY = '';

function apiHeaders() {
  return {'Content-Type':'application/json','X-API-Key': KEY};
}

async function apiFetch(path, opts={}) {
  const r = await fetch(BASE + path, {...opts, headers: apiHeaders()});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function setStatus(msg, err=false) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.style.color = err ? '#fc8181' : '#68d391';
}

async function loadAll() {
  NS = document.getElementById('nsInput').value.trim();
  KEY = document.getElementById('apiKeyInput').value.trim();
  if (!NS) { setStatus('Enter a namespace', true); return; }
  setStatus('Loading...');
  try {
    const [stats, heuristics, episodes] = await Promise.all([
      apiFetch('/api/v1/learning/stats?ns=' + encodeURIComponent(NS)),
      apiFetch('/api/v1/learning/heuristics?ns=' + encodeURIComponent(NS) + '&limit=100'),
      apiFetch('/api/v1/learning/episodes/recent?ns=' + encodeURIComponent(NS) + '&days=7&limit=100'),
    ]);
    renderStats(stats);
    renderHeuristics(heuristics);
    renderEpisodes(episodes);
    setStatus('Loaded');
  } catch(e) { setStatus('Error: ' + e.message, true); }
}

function renderStats(s) {
  document.getElementById('stat-heuristics').textContent = s.heuristic_count;
  document.getElementById('stat-episodes').textContent = s.episode_count_7d;
  document.getElementById('stat-quality').textContent = s.avg_quality_7d != null ? s.avg_quality_7d.toFixed(2) : '—';
  document.getElementById('stat-success').textContent = s.success_rate_7d != null ? (s.success_rate_7d * 100).toFixed(0) + '%' : '—';
}

function renderHeuristics(rows) {
  const tb = document.getElementById('heuristicsBody');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="5" style="color:#718096">No heuristics found</td></tr>'; return; }
  tb.innerHTML = rows.map(h => `
    <tr>
      <td style="max-width:400px">${esc(h.rule)}<br><small style="color:#718096">${esc(h.rationale||'')}</small></td>
      <td>
        <div class="conf-bar"><div class="conf-fill" style="width:${(h.confidence*80).toFixed(0)}px"></div></div>
        <small>${(h.confidence*100).toFixed(0)}%</small>
      </td>
      <td>${h.triggered_count} / ${h.overridden_count} overrides</td>
      <td>${(h.applies_to_tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</td>
      <td><button class="danger" style="padding:4px 8px;font-size:12px" onclick="deleteHeuristic('${esc(h.id)}')">Delete</button></td>
    </tr>`).join('');
}

function renderEpisodes(rows) {
  const tb = document.getElementById('episodesBody');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="6" style="color:#718096">No episodes found</td></tr>'; return; }
  tb.innerHTML = rows.map(ep => {
    const oc = (ep.outcome||'').toUpperCase();
    const cls = oc.includes('SUCCESS') ? 'badge-ok' : oc.includes('FAIL') ? 'badge-fail' : 'badge-corr';
    return `<tr>
      <td style="max-width:300px">${esc(ep.original_prompt.substring(0,120))}${ep.original_prompt.length>120?'…':''}</td>
      <td>${esc(ep.agent_used||'default')}</td>
      <td><span class="badge ${cls}">${oc}</span></td>
      <td>${ep.quality_score != null ? ep.quality_score.toFixed(2) : '—'}</td>
      <td>${ep.duration_s.toFixed(1)}s</td>
      <td style="white-space:nowrap">${new Date(ep.created_at).toLocaleString()}</td>
    </tr>`;
  }).join('');
}

async function deleteHeuristic(id) {
  if (!confirm('Delete this heuristic?')) return;
  try {
    await fetch(BASE + '/api/v1/learning/heuristics/' + id + '?ns=' + encodeURIComponent(NS), {method:'DELETE', headers:apiHeaders()});
    setStatus('Deleted');
    await loadAll();
  } catch(e) { setStatus('Delete failed: ' + e.message, true); }
}

async function triggerReflect() {
  if (!NS) { setStatus('Enter a namespace first', true); return; }
  setStatus('Running reflection...');
  try {
    const r = await apiFetch('/api/v1/learning/reflect', {
      method: 'POST',
      body: JSON.stringify({namespace: NS, lookback_days: 7}),
    });
    setStatus(`Reflection done: +${r.heuristics_added} heuristics from ${r.episodes_analysed} episodes`);
    await loadAll();
  } catch(e) { setStatus('Reflection failed: ' + e.message, true); }
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Pre-populate from sessionStorage if available
window.onload = () => {
  const k = sessionStorage.getItem('engram_key');
  if (k) document.getElementById('apiKeyInput').value = k;
};
</script>
</body>
</html>
"""
