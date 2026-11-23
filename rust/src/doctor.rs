//! `MEMNOS Doctor` — Diagnostics, Safe Repair & Database Optimization (`doctor.rs`).
//!
//! Shared by both the Workspace App (`/workspace/api/doctor`, `/workspace/api/doctor/run`)
//! and the CLI (`memnos doctor [--fix] [--optimize] [--dry-run] [--namespace <ns>] [--json]`).

use crate::config::{config_dir, BUILD_SHA, VERSION};
use crate::control::Control;
use crate::store::{is_valid_entity_name, BrainStore};
use chrono::{DateTime, Utc};
use deadpool_postgres::Pool;
use serde_json::{json, Value};
use std::fs;
use std::time::{Instant, SystemTime};

const VACUUM_TABLES: &[&str] = &[
    "tenant_memnos.raw_turns",
    "tenant_memnos.episodic",
    "tenant_memnos.semantic",
    "tenant_memnos.entities",
    "tenant_memnos.mentions",
    "tenant_memnos.edges",
    "tenant_memnos.provenance",
    "tenant_memnos.entity_dossiers",
    "memnos_control.namespaces",
    "memnos_control.namespace_links",
    "memnos_control.bindings",
    "memnos_control.agent_sessions",
    "memnos_control.channel_catalog",
    "memnos_control.channel_messages",
    "memnos_control.channel_subscriptions",
    "memnos_control.agent_inbox",
    "memnos_control.leases",
    "memnos_control.constraint_enforcement",
    "memnos_control.audit_log",
];

fn inspect_latest_backup() -> Value {
    let bdir = config_dir().join("backups");
    let mut newest_time: Option<SystemTime> = None;
    let mut newest_size: u64 = 0;
    let mut newest_name = String::new();
    let mut count = 0u64;

    if let Ok(entries) = fs::read_dir(&bdir) {
        for entry in entries.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) != Some("dump") {
                continue;
            }
            if let Ok(meta) = entry.metadata() {
                if meta.len() == 0 {
                    continue;
                }
                count += 1;
                if let Ok(mtime) = meta.modified() {
                    if newest_time.map_or(true, |prev| mtime > prev) {
                        newest_time = Some(mtime);
                        newest_size = meta.len();
                        newest_name = p
                            .file_name()
                            .and_then(|n| n.to_str())
                            .unwrap_or("")
                            .to_string();
                    }
                }
            }
        }
    }

    if let Some(mt) = newest_time {
        let dt: DateTime<Utc> = mt.into();
        let age_hours = SystemTime::now()
            .duration_since(mt)
            .map(|d| (d.as_secs_f64() / 3600.0 * 10.0).round() / 10.0)
            .unwrap_or(0.0);
        json!({
            "exists": true,
            "count": count,
            "latest_file": newest_name,
            "latest_at": dt.to_rfc3339(),
            "size_bytes": newest_size,
            "age_hours": age_hours,
        })
    } else {
        json!({
            "exists": false,
            "count": 0,
            "latest_file": "",
            "latest_at": Value::Null,
            "size_bytes": 0,
            "age_hours": Value::Null,
        })
    }
}

pub async fn run_doctor_scan(
    pool: &Pool,
    principal_id: Option<i64>,
    ns_filter: Option<&str>,
) -> Result<Value, String> {
    let t0 = Instant::now();
    let c = pool.get().await.map_err(|e| format!("db connection failed: {e}"))?;
    let (is_admin, ns_map) = if let Some(pid) = principal_id {
        let adm = Control::is_admin(pool, pid).await.map_err(|e| e.to_string())?;
        let m = Control::workspace_authorized_ns_map(&**c, pid)
            .await
            .map_err(|e| e.to_string())?;
        (adm, m)
    } else {
        let list = Control::list_namespaces(pool)
            .await
            .map_err(|e| e.to_string())?;
        let m: Vec<(String, bool)> = list
            .into_iter()
            .filter_map(|v| v.get("name").and_then(|s| s.as_str()).map(|s| (s.to_string(), true)))
            .collect();
        (true, m)
    };

    let target_ns_opt = ns_filter
        .map(|s| s.trim())
        .filter(|s| !s.is_empty() && *s != "*");
    if let (Some(pid), Some(tgt)) = (principal_id, target_ns_opt) {
        let ok = Control::authorize(pool, pid, tgt, false)
            .await
            .map_err(|e| e.to_string())?;
        if !ok {
            return Err(format!("Forbidden: no read grant for namespace '{}'", tgt));
        }
    }

    let scope_ns: Vec<String> = match target_ns_opt {
        Some(tgt) => vec![tgt.to_string()],
        None => ns_map
            .iter()
            .map(|(n, _)| n.clone())
            .filter(|n| !n.starts_with("secret:"))
            .collect(),
    };
    let non_test_ns: Vec<String> = scope_ns
        .iter()
        .filter(|n| !n.starts_with("test:"))
        .cloned()
        .collect();

    let has_tenant: bool = c
        .query_one(
            "SELECT to_regclass('tenant_memnos.raw_turns') IS NOT NULL
                    AND to_regclass('tenant_memnos.semantic') IS NOT NULL
                    AND to_regclass('tenant_memnos.entities') IS NOT NULL",
            &[],
        )
        .await
        .map(|r| r.get(0))
        .unwrap_or(false);

    let mut checks: Vec<Value> = Vec::new();
    let mut score: i64 = 100;

    // 1. Backup & Snapshot Readiness (Admin only)
    if is_admin {
        let binfo = inspect_latest_backup();
        let exists = binfo["exists"].as_bool().unwrap_or(false);
        let age_h = binfo["age_hours"].as_f64().unwrap_or(9999.0);
        let st = if exists && age_h <= 168.0 {
            "ok"
        } else {
            score -= 6;
            "warn"
        };
        let summary = if exists {
            format!(
                "Latest verified dump: {} ({:.1}h ago, {} backups stored)",
                binfo["latest_file"].as_str().unwrap_or("backup.dump"),
                age_h,
                binfo["count"].as_u64().unwrap_or(1)
            )
        } else {
            "No PostgreSQL backup found in ~/.memnos/backups yet.".to_string()
        };
        checks.push(json!({
            "id": "backup_status",
            "title": "Database Backup & Recovery",
            "status": st,
            "summary": summary,
            "action": "backup_db",
            "action_label": "Snapshot Backup Now",
            "metrics": binfo,
        }));
    }

    // 2. Knowledge Graph & Entity Integrity
    let (mut junk_entities, mut unemb_entities, mut orphan_mentions, mut orphan_edges, mut orphan_dossiers) =
        (0u64, 0u64, 0u64, 0u64, 0u64);
    if has_tenant && !scope_ns.is_empty() {
        let ent_rows = c
            .query(
                "SELECT name, embedding IS NOT NULL AS has_emb FROM tenant_memnos.entities WHERE namespace = ANY($1)",
                &[&scope_ns],
            )
            .await
            .unwrap_or_default();
        for r in &ent_rows {
            let nm: String = r.get("name");
            let has_emb: bool = r.get("has_emb");
            if !is_valid_entity_name(&nm, true) {
                junk_entities += 1;
            } else if !has_emb {
                unemb_entities += 1;
            }
        }
        if let Ok(orow) = c
            .query_one(
                "SELECT
                    (SELECT count(*) FROM tenant_memnos.mentions m WHERE NOT EXISTS (SELECT 1 FROM tenant_memnos.entities e WHERE e.id = m.entity_id)
                       OR (m.memory_kind = 'semantic' AND NOT EXISTS (SELECT 1 FROM tenant_memnos.semantic f WHERE f.id = m.memory_id))) AS om,
                    (SELECT count(*) FROM tenant_memnos.edges ed WHERE ed.namespace = ANY($1)
                       AND (NOT EXISTS (SELECT 1 FROM tenant_memnos.entities e1 WHERE e1.id = ed.src_entity)
                         OR NOT EXISTS (SELECT 1 FROM tenant_memnos.entities e2 WHERE e2.id = ed.dst_entity))) AS oe,
                    (SELECT count(*) FROM tenant_memnos.entity_dossiers d WHERE d.namespace = ANY($1)
                       AND NOT EXISTS (SELECT 1 FROM tenant_memnos.entities e WHERE e.id = d.entity_id)) AS od",
                &[&scope_ns],
            )
            .await
        {
            orphan_mentions = orow.get::<_, i64>("om") as u64;
            orphan_edges = orow.get::<_, i64>("oe") as u64;
            orphan_dossiers = orow.get::<_, i64>("od") as u64;
        }
    }
    let graph_issues = junk_entities + unemb_entities + orphan_mentions + orphan_edges + orphan_dossiers;
    let graph_status = if graph_issues > 0 {
        score -= 8;
        "warn"
    } else {
        "ok"
    };
    checks.push(json!({
        "id": "graph_integrity",
        "title": "Knowledge Graph & Entity Hygiene",
        "status": graph_status,
        "summary": if graph_issues == 0 {
            "Entity graph is clean — zero junk nodes, orphan edges, or missing entity vectors.".to_string()
        } else {
            format!(
                "{} junk entities · {} unembedded entities · {} orphan mentions/edges/dossiers",
                junk_entities,
                unemb_entities,
                orphan_mentions + orphan_edges + orphan_dossiers
            )
        },
        "action": "repair_graph",
        "action_label": "Clean Graph & Embed Entities",
        "metrics": {
            "junk_entities": junk_entities,
            "unembedded_entities": unemb_entities,
            "orphan_mentions": orphan_mentions,
            "orphan_edges": orphan_edges,
            "orphan_dossiers": orphan_dossiers,
        }
    }));

    // 3. Memory Quality, Duplicates & Embeddings
    let (mut exact_dupes, mut unemb_facts, mut unemb_turns, mut contra_groups) = (0u64, 0u64, 0u64, 0u64);
    if has_tenant && !scope_ns.is_empty() {
        if let Ok(mrow) = c
            .query_one(
                "SELECT
                    (SELECT count(*) FROM (
                        SELECT id, row_number() OVER (PARTITION BY namespace, lower(statement) ORDER BY id ASC) AS rn
                        FROM tenant_memnos.semantic
                        WHERE namespace = ANY($1) AND expired_at IS NULL AND valid_to IS NULL AND constraint_retired_at IS NULL
                    ) sub WHERE rn > 1) AS dupes,
                    (SELECT count(*) FROM tenant_memnos.semantic WHERE namespace = ANY($1) AND expired_at IS NULL AND embedding IS NULL) AS ufacts,
                    (SELECT count(*) FROM tenant_memnos.raw_turns WHERE namespace = ANY($1) AND embedding IS NULL) AS uturns,
                    (SELECT count(*) FROM (
                        SELECT namespace, lower(subject_entity), lower(predicate)
                        FROM tenant_memnos.semantic
                        WHERE namespace = ANY($1) AND expired_at IS NULL AND valid_to IS NULL
                          AND subject_entity IS NOT NULL AND subject_entity <> ''
                          AND predicate IS NOT NULL AND predicate <> ''
                          AND object IS NOT NULL AND object <> ''
                        GROUP BY namespace, lower(subject_entity), lower(predicate)
                        HAVING count(DISTINCT lower(object)) > 1
                    ) cg) AS contras",
                &[&scope_ns],
            )
            .await
        {
            exact_dupes = mrow.get::<_, i64>("dupes") as u64;
            unemb_facts = mrow.get::<_, i64>("ufacts") as u64;
            unemb_turns = mrow.get::<_, i64>("uturns") as u64;
            contra_groups = mrow.get::<_, i64>("contras") as u64;
        }
    }
    let mem_repairable = exact_dupes + unemb_facts + unemb_turns;
    let mem_status = if mem_repairable > 0 {
        score -= 8;
        "warn"
    } else if contra_groups > 0 {
        score -= 3;
        "info"
    } else {
        "ok"
    };
    checks.push(json!({
        "id": "memory_quality",
        "title": "Memory Deduplication & Vector Coverage",
        "status": mem_status,
        "summary": if mem_repairable == 0 && contra_groups == 0 {
            "All live facts & turns are embedded with zero duplicate statements.".to_string()
        } else {
            format!(
                "{} duplicate live facts · {} unembedded facts · {} unembedded turns · {} contested predicates",
                exact_dupes, unemb_facts, unemb_turns, contra_groups
            )
        },
        "action": "optimize_memories",
        "action_label": "Consolidate & Backfill Vectors",
        "metrics": {
            "exact_duplicate_facts": exact_dupes,
            "unembedded_facts": unemb_facts,
            "unembedded_turns": unemb_turns,
            "contradiction_groups": contra_groups,
        }
    }));

    // 4. Multi-Agent Coordination, Claims & Leases
    let mut stuck_claims = 0u64;
    let mut expired_leases = 0u64;
    let mut stale_sessions = 0u64;
    let mut expired_quotas = 0u64;
    if !scope_ns.is_empty() {
        if let Ok(crow) = c
            .query_one(
                "SELECT
                    (SELECT count(*) FROM memnos_control.channel_messages
                     WHERE namespace = ANY($1) AND recalled_at IS NULL AND claimable
                       AND claim_status = 'claimed' AND claim_lease_until IS NOT NULL AND claim_lease_until < now()) AS sc,
                    (SELECT count(*) FROM memnos_control.leases
                     WHERE namespace = ANY($1) AND released_at IS NULL AND expires_at < now()) AS el,
                    (SELECT count(*) FROM memnos_control.agent_sessions s
                     WHERE s.namespace = ANY($1) AND s.expires_at < now() - interval '48 hours'
                       AND NOT EXISTS (
                           SELECT 1 FROM memnos_control.agent_inbox i
                           WHERE i.namespace = s.namespace AND i.to_session = s.name
                             AND i.recalled_at IS NULL AND i.status IN ('pending', 'read', 'in_progress')
                       )) AS ss",
                &[&scope_ns],
            )
            .await
        {
            stuck_claims = crow.get::<_, i64>("sc") as u64;
            expired_leases = crow.get::<_, i64>("el") as u64;
            stale_sessions = crow.get::<_, i64>("ss") as u64;
        }
        let qrows = c
            .query(
                "SELECT metadata->>'quota_until' AS qu FROM memnos_control.agent_sessions WHERE namespace = ANY($1) AND status = 'quota_paused'",
                &[&scope_ns],
            )
            .await
            .unwrap_or_default();
        for qr in qrows {
            if let Some(qu_str) = qr.get::<_, Option<String>>("qu") {
                if let Ok(dt) = DateTime::parse_from_rfc3339(&qu_str) {
                    if dt.with_timezone(&Utc) <= Utc::now() {
                        expired_quotas += 1;
                    }
                }
            }
        }
    }
    let coord_issues = stuck_claims + expired_leases + stale_sessions + expired_quotas;
    let coord_status = if stuck_claims > 0 || expired_leases > 0 {
        score -= 10;
        "warn"
    } else if stale_sessions > 0 || expired_quotas > 0 {
        score -= 4;
        "info"
    } else {
        "ok"
    };
    checks.push(json!({
        "id": "coordination_hygiene",
        "title": "Multi-Agent Claims, Leases & Sessions",
        "status": coord_status,
        "summary": if coord_issues == 0 {
            "No stuck task claims, expired file leases, or stale dead @handles.".to_string()
        } else {
            format!(
                "{} stuck task claims · {} expired file leases · {} expired quota caps · {} stale (>48h) handles",
                stuck_claims, expired_leases, expired_quotas, stale_sessions
            )
        },
        "action": "repair_coordination",
        "action_label": "Release Locks & Prune Stale Handles",
        "metrics": {
            "stuck_claims": stuck_claims,
            "expired_leases": expired_leases,
            "expired_quota_pauses": expired_quotas,
            "stale_dead_sessions": stale_sessions,
        }
    }));

    // 5. Namespace & Scratch Hygiene
    let mut unreg_ns = 0u64;
    if !non_test_ns.is_empty() {
        if let Ok(nr) = c
            .query_one(
                "SELECT count(*) AS n
                 FROM UNNEST($1::text[]) AS u(ns)
                 LEFT JOIN memnos_control.namespaces reg ON reg.name = u.ns
                 WHERE reg.name IS NULL OR COALESCE(reg.auto_registered, false)",
                &[&non_test_ns],
            )
            .await
        {
            unreg_ns = nr.get::<_, i64>("n") as u64;
        }
    }
    let scratch_test_ns = if is_admin {
        scope_ns.iter().filter(|n| n.starts_with("test:")).count() as u64
    } else {
        0
    };
    let scratch_test_principals: u64 = if is_admin {
        c.query_one(
            "SELECT count(*) AS n FROM memnos_control.principals p
             WHERE p.name LIKE 'test-%'
               AND NOT EXISTS (SELECT 1 FROM memnos_control.api_tokens t WHERE t.principal_id = p.id AND t.revoked_at IS NULL)",
            &[],
        )
        .await
        .map(|r| r.get::<_, i64>("n") as u64)
        .unwrap_or(0)
    } else {
        0
    };
    let ns_issues = unreg_ns + scratch_test_ns + scratch_test_principals;
    let ns_status = if ns_issues > 0 {
        score -= 4;
        "info"
    } else {
        "ok"
    };
    checks.push(json!({
        "id": "namespace_hygiene",
        "title": "Namespace Registry & Scratch Cleanup",
        "status": ns_status,
        "summary": if ns_issues == 0 {
            "All active namespaces are explicitly registered with zero leftover test artifacts.".to_string()
        } else {
            format!(
                "{} auto-discovered unregistered namespace(s) · {} test:* scope(s) · {} 0-token test-* principal(s)",
                unreg_ns, scratch_test_ns, scratch_test_principals
            )
        },
        "action": "repair_namespaces",
        "action_label": "Register Scopes & Prune test:*",
        "metrics": {
            "unregistered_namespaces": unreg_ns,
            "scratch_test_namespaces": scratch_test_ns,
            "scratch_test_principals": scratch_test_principals,
        }
    }));

    // 6. PostgreSQL Storage, Dead Tuples & Index Health (Admin)
    if is_admin {
        let srow = c
            .query_one(
                "SELECT
                    COALESCE(sum(n_dead_tup), 0)::bigint AS dead_tup,
                    COALESCE(sum(n_live_tup), 0)::bigint AS live_tup,
                    count(*) FILTER (
                        WHERE n_live_tup > 50
                          AND ((last_analyze IS NULL AND last_autoanalyze IS NULL)
                               OR n_mod_since_analyze > GREATEST(250, n_live_tup / 4))
                    )::bigint AS need_analyze,
                    (SELECT count(*) FROM pg_index i
                     JOIN pg_class cl ON cl.oid = i.indexrelid
                     JOIN pg_namespace ns ON ns.oid = cl.relnamespace
                     WHERE ns.nspname IN ('tenant_memnos', 'memnos_control') AND NOT i.indisvalid)::bigint AS invalid_idx,
                    pg_database_size(current_database())::bigint AS db_bytes
                 FROM pg_stat_user_tables
                 WHERE schemaname IN ('tenant_memnos', 'memnos_control')",
                &[],
            )
            .await
            .map_err(|e| e.to_string())?;

        let dead_tup: i64 = srow.get("dead_tup");
        let live_tup: i64 = srow.get("live_tup");
        let need_analyze: i64 = srow.get("need_analyze");
        let invalid_idx: i64 = srow.get("invalid_idx");
        let db_bytes: i64 = srow.get("db_bytes");

        let top_tables: Vec<Value> = c
            .query(
                "SELECT schemaname || '.' || relname AS table_name,
                        pg_total_relation_size(relid)::bigint AS total_bytes,
                        n_live_tup::bigint AS live_rows,
                        n_dead_tup::bigint AS dead_rows
                 FROM pg_stat_user_tables
                 WHERE schemaname IN ('tenant_memnos', 'memnos_control')
                 ORDER BY pg_total_relation_size(relid) DESC
                 LIMIT 6",
                &[],
            )
            .await
            .unwrap_or_default()
            .iter()
            .map(|r| {
                json!({
                    "table": r.get::<_, String>("table_name"),
                    "bytes": r.get::<_, i64>("total_bytes"),
                    "live_rows": r.get::<_, i64>("live_rows"),
                    "dead_rows": r.get::<_, i64>("dead_rows"),
                })
            })
            .collect();

        let db_st = if invalid_idx > 0 {
            score -= 25;
            "critical"
        } else if dead_tup > 2000 || need_analyze > 0 {
            score -= 6;
            "warn"
        } else {
            "ok"
        };

        checks.push(json!({
            "id": "db_storage",
            "title": "PostgreSQL Storage, Indexes & Planner Stats",
            "status": db_st,
            "summary": format!(
                "{} dead tuples across {} live rows · {} table(s) need ANALYZE · {} invalid index(es)",
                dead_tup, live_tup, need_analyze, invalid_idx
            ),
            "action": "optimize_db",
            "action_label": "Run VACUUM ANALYZE",
            "metrics": {
                "db_size_bytes": db_bytes,
                "live_tuples": live_tup,
                "dead_tuples": dead_tup,
                "tables_needing_analyze": need_analyze,
                "invalid_indexes": invalid_idx,
                "top_tables": top_tables,
            }
        }));

        // 7. 24h Operational Latency & Error Findings
        if let Ok(hval) = Control::health(pool, 24).await {
            let arr = hval.as_array().cloned().unwrap_or_default();
            let mut has_crit = false;
            let mut has_warn = false;
            let mut msgs: Vec<String> = Vec::new();
            for item in &arr {
                if let Some(pair) = item.as_array() {
                    let lvl = pair.first().and_then(|v| v.as_str()).unwrap_or("INFO");
                    let msg = pair.get(1).and_then(|v| v.as_str()).unwrap_or("");
                    if lvl == "CRITICAL" {
                        has_crit = true;
                    } else if lvl == "WARN" {
                        has_warn = true;
                    }
                    if lvl != "OK" && !msg.is_empty() {
                        msgs.push(format!("[{}] {}", lvl, msg));
                    }
                }
            }
            let op_st = if has_crit {
                score -= 10;
                "critical"
            } else if has_warn {
                score -= 4;
                "warn"
            } else {
                "ok"
            };
            checks.push(json!({
                "id": "operational_reliability",
                "title": "24h Operational Latency & Error Rate",
                "status": op_st,
                "summary": if msgs.is_empty() {
                    "All 24h recall, remember, and coordination operations within healthy latency & error thresholds.".to_string()
                } else {
                    msgs.join(" · ")
                },
                "action": Value::Null,
                "action_label": Value::Null,
                "metrics": { "findings": arr }
            }));
        }
    }

    let final_score = score.clamp(0, 100);
    let overall_status = if final_score >= 92 {
        "healthy"
    } else if final_score >= 75 {
        "attention"
    } else {
        "action_required"
    };

    Ok(json!({
        "ok": true,
        "version": VERSION,
        "build": BUILD_SHA,
        "is_admin": is_admin,
        "scope": target_ns_opt.unwrap_or("*"),
        "score": final_score,
        "status": overall_status,
        "scan_ms": t0.elapsed().as_millis() as u64,
        "checks": checks,
    }))
}

pub async fn execute_doctor_actions(
    pool: &Pool,
    store: &BrainStore,
    pg_dsn: &str,
    principal_id: Option<i64>,
    ns_filter: Option<&str>,
    requested_actions: &[String],
    dry_run: bool,
    event_tx: Option<&tokio::sync::broadcast::Sender<Value>>,
) -> Result<Value, String> {
    let t0 = Instant::now();
    let c = pool.get().await.map_err(|e| format!("db connection failed: {e}"))?;
    let (is_admin, ns_map) = if let Some(pid) = principal_id {
        let adm = Control::is_admin(pool, pid).await.map_err(|e| e.to_string())?;
        let m = Control::workspace_authorized_ns_map(&**c, pid)
            .await
            .map_err(|e| e.to_string())?;
        (adm, m)
    } else {
        let list = Control::list_namespaces(pool)
            .await
            .map_err(|e| e.to_string())?;
        let m: Vec<(String, bool)> = list
            .into_iter()
            .filter_map(|v| v.get("name").and_then(|s| s.as_str()).map(|s| (s.to_string(), true)))
            .collect();
        (true, m)
    };

    let target_ns_opt = ns_filter
        .map(|s| s.trim())
        .filter(|s| !s.is_empty() && *s != "*");
    if let (Some(pid), Some(tgt)) = (principal_id, target_ns_opt) {
        let can_w = Control::authorize(pool, pid, tgt, true)
            .await
            .map_err(|e| e.to_string())?;
        if !can_w {
            return Err(format!("Forbidden: write grant required on namespace '{}'", tgt));
        }
    }

    let write_ns: Vec<String> = match target_ns_opt {
        Some(tgt) => vec![tgt.to_string()],
        None => ns_map
            .into_iter()
            .filter(|(n, w)| *w && !n.starts_with("secret:"))
            .map(|(n, _)| n)
            .collect(),
    };

    let expanded: Vec<String> = if requested_actions.is_empty()
        || requested_actions.iter().any(|a| a == "all")
    {
        let mut v = vec![
            "repair_graph".to_string(),
            "optimize_memories".to_string(),
            "repair_coordination".to_string(),
            "repair_namespaces".to_string(),
        ];
        if is_admin {
            v.push("optimize_db".to_string());
        }
        v
    } else {
        requested_actions.to_vec()
    };

    for act in &expanded {
        if matches!(act.as_str(), "optimize_db" | "backup_db") && !is_admin {
            return Err(format!("Forbidden: admin role required for '{}'", act));
        }
    }

    let has_tenant: bool = c
        .query_one(
            "SELECT to_regclass('tenant_memnos.raw_turns') IS NOT NULL
                    AND to_regclass('tenant_memnos.semantic') IS NOT NULL
                    AND to_regclass('tenant_memnos.entities') IS NOT NULL",
            &[],
        )
        .await
        .map(|r| r.get(0))
        .unwrap_or(false);
    drop(c);

    let mut results = serde_json::Map::new();
    let total_steps = expanded.len().max(1);
    let emit_step = |idx: usize, msg: &str| {
        if let Some(tx) = event_tx {
            let _ = tx.send(json!({
                "type": "activity",
                "op": "doctor/step",
                "namespace": target_ns_opt.unwrap_or("*"),
                "actor": "doctor",
                "detail": format!("[{}/{}] {}", idx + 1, total_steps, msg),
                "ok": true,
            }));
        }
    };

    if !dry_run && !expanded.is_empty() {
        if let Some(tx) = event_tx {
            let _ = tx.send(json!({
                "type": "activity",
                "op": "doctor/step",
                "namespace": target_ns_opt.unwrap_or("*"),
                "actor": "doctor",
                "detail": "Taking mandatory verified PostgreSQL backup before repair…",
                "ok": true,
            }));
        }
        let bpath = crate::backup::take_verified_db_backup_dsn(pg_dsn, "pre-doctor", None, 10).await?;
        results.insert(
            "pre_backup".into(),
            json!(bpath.to_string_lossy().to_string()),
        );
    }

    for (step_idx, act) in expanded.iter().enumerate() {
        match act.as_str() {
            "backup_db" => {
                emit_step(step_idx, "Snapshotting verified PostgreSQL database backup…");
                if dry_run {
                    results.insert("backup_db".into(), json!({ "dry_run": true, "would_backup": true }));
                } else {
                    results.insert("backup_db".into(), inspect_latest_backup());
                }
            }
            "repair_graph" => {
                emit_step(step_idx, "Cleaning junk entities, orphan edges & batch-embedding entities…");
                if has_tenant {
                    let r = store
                        .doctor_clean_orphans_and_graph(&write_ns, dry_run)
                        .await?;
                    results.insert("repair_graph".into(), r);
                } else {
                    results.insert("repair_graph".into(), json!({ "skipped": "no tenant schema" }));
                }
            }
            "optimize_memories" => {
                emit_step(step_idx, "Deduplicating live facts & batch-backfilling missing embeddings…");
                if has_tenant {
                    let r = store
                        .doctor_optimize_memories(&write_ns, 256, dry_run)
                        .await?;
                    results.insert("optimize_memories".into(), r);
                } else {
                    results.insert("optimize_memories".into(), json!({ "skipped": "no tenant schema" }));
                }
            }
            "repair_coordination" => {
                emit_step(step_idx, "Releasing stuck claims, expired file leases & stale sessions…");
                let c = pool.get().await.map_err(|e| e.to_string())?;
                if dry_run || write_ns.is_empty() {
                    results.insert(
                        "repair_coordination".into(),
                        json!({ "dry_run": dry_run, "released_claims": 0, "released_leases": 0, "resumed_quotas": 0, "pruned_sessions": 0 }),
                    );
                } else {
                    let rc = c
                        .execute(
                            "UPDATE memnos_control.channel_messages
                             SET claim_status = 'pending', claimed_by = NULL, claimed_by_ns = NULL, claim_lease_until = NULL
                             WHERE namespace = ANY($1) AND recalled_at IS NULL AND claimable
                               AND claim_status = 'claimed' AND claim_lease_until IS NOT NULL AND claim_lease_until < now()",
                            &[&write_ns],
                        )
                        .await
                        .unwrap_or(0);
                    let rl = c
                        .execute(
                            "UPDATE memnos_control.leases
                             SET released_at = now()
                             WHERE namespace = ANY($1) AND released_at IS NULL AND expires_at < now()",
                            &[&write_ns],
                        )
                        .await
                        .unwrap_or(0);
                    let qrows = c
                        .query(
                            "SELECT id, metadata->>'quota_until' AS qu FROM memnos_control.agent_sessions WHERE namespace = ANY($1) AND status = 'quota_paused'",
                            &[&write_ns],
                        )
                        .await
                        .unwrap_or_default();
                    let mut expired_ids: Vec<i64> = Vec::new();
                    for qr in qrows {
                        if let Some(qu_str) = qr.get::<_, Option<String>>("qu") {
                            if let Ok(dt) = DateTime::parse_from_rfc3339(&qu_str) {
                                if dt.with_timezone(&Utc) <= Utc::now() {
                                    expired_ids.push(qr.get("id"));
                                }
                            }
                        }
                    }
                    let rq = if expired_ids.is_empty() {
                        0
                    } else {
                        c.execute(
                            "UPDATE memnos_control.agent_sessions SET status = 'active' WHERE id = ANY($1)",
                            &[&expired_ids],
                        )
                        .await
                        .unwrap_or(0)
                    };
                    let ps = c
                        .execute(
                            "DELETE FROM memnos_control.agent_sessions s
                             WHERE s.namespace = ANY($1) AND s.expires_at < now() - interval '48 hours'
                               AND NOT EXISTS (
                                   SELECT 1 FROM memnos_control.agent_inbox i
                                   WHERE i.namespace = s.namespace AND i.to_session = s.name
                                     AND i.recalled_at IS NULL AND i.status IN ('pending', 'read', 'in_progress')
                               )",
                            &[&write_ns],
                        )
                        .await
                        .unwrap_or(0);
                    results.insert(
                        "repair_coordination".into(),
                        json!({
                            "dry_run": false,
                            "released_claims": rc,
                            "released_leases": rl,
                            "resumed_quotas": rq,
                            "pruned_sessions": ps,
                        }),
                    );
                }
            }
            "repair_namespaces" => {
                emit_step(step_idx, "Registering active scopes & pruning leftover test:* artifacts…");
                let c = pool.get().await.map_err(|e| e.to_string())?;
                let mut registered_cnt = 0u64;
                let mut pruned_test_ns = 0u64;
                let mut pruned_test_principals = 0u64;
                if !dry_run {
                    let non_test: Vec<String> = write_ns
                        .iter()
                        .filter(|n| !n.starts_with("test:"))
                        .cloned()
                        .collect();
                    for ns in &non_test {
                        let n = c
                            .execute(
                                "INSERT INTO memnos_control.namespaces(name, created_by, auto_registered)
                                 VALUES($1, $2, false)
                                 ON CONFLICT (name) DO UPDATE SET auto_registered = false
                                 WHERE memnos_control.namespaces.auto_registered = true",
                                &[ns, &principal_id],
                            )
                            .await
                            .unwrap_or(0);
                        registered_cnt += n;
                    }
                    if is_admin && target_ns_opt.is_none() {
                        let test_nss: Vec<String> = write_ns
                            .iter()
                            .filter(|n| n.starts_with("test:"))
                            .cloned()
                            .collect();
                        for tns in test_nss {
                            if Control::delete_namespace(pool, &tns, true).await.is_ok() {
                                pruned_test_ns += 1;
                            }
                        }
                        let stale_pids = c
                            .query(
                                "SELECT p.id FROM memnos_control.principals p
                                 WHERE p.name LIKE 'test-%' AND ($1::bigint IS NULL OR p.id <> $1)
                                   AND NOT EXISTS (SELECT 1 FROM memnos_control.api_tokens t WHERE t.principal_id = p.id AND t.revoked_at IS NULL)",
                                &[&principal_id],
                            )
                            .await
                            .unwrap_or_default();
                        for r in stale_pids {
                            let spid: i64 = r.get(0);
                            if Control::delete_principal(pool, spid, principal_id.unwrap_or(0)).await.is_ok() {
                                pruned_test_principals += 1;
                            }
                        }
                    }
                }
                results.insert(
                    "repair_namespaces".into(),
                    json!({
                        "dry_run": dry_run,
                        "registered_namespaces": registered_cnt,
                        "pruned_test_namespaces": pruned_test_ns,
                        "pruned_test_principals": pruned_test_principals,
                    }),
                );
            }
            "optimize_db" => {
                emit_step(step_idx, "Refreshing PostgreSQL planner statistics & reclaiming dead tuples…");
                if dry_run {
                    results.insert(
                        "optimize_db".into(),
                        json!({ "dry_run": true, "tables_targeted": VACUUM_TABLES.len() }),
                    );
                } else {
                    let c = pool.get().await.map_err(|e| e.to_string())?;
                    // Check which tables actually have >= 100 dead tuples so multi-GB clean tables
                    // only run sub-100ms ANALYZE instead of full-page VACUUM scans
                    let dead_rows = c
                        .query(
                            "SELECT schemaname || '.' || relname AS fq, n_dead_tup::bigint AS dead
                             FROM pg_stat_user_tables
                             WHERE schemaname IN ('tenant_memnos', 'memnos_control')",
                            &[],
                        )
                        .await
                        .unwrap_or_default();
                    let mut dead_map = std::collections::HashMap::new();
                    for dr in dead_rows {
                        dead_map.insert(dr.get::<_, String>("fq"), dr.get::<_, i64>("dead"));
                    }

                    let mut processed = 0u64;
                    let mut vacuumed = 0u64;
                    for tbl in VACUUM_TABLES {
                        let exists: bool = c
                            .query_one("SELECT to_regclass($1) IS NOT NULL", &[tbl])
                            .await
                            .map(|r| r.get(0))
                            .unwrap_or(false);
                        if !exists {
                            continue;
                        }
                        let dead_cnt = dead_map.get(*tbl).copied().unwrap_or(0);
                        let sql = if dead_cnt >= 100 {
                            vacuumed += 1;
                            format!("VACUUM (ANALYZE) {};", tbl)
                        } else {
                            format!("ANALYZE {};", tbl)
                        };
                        if c.batch_execute(&sql).await.is_ok() {
                            processed += 1;
                        }
                    }
                    results.insert(
                        "optimize_db".into(),
                        json!({
                            "dry_run": false,
                            "tables_vacuum_analyzed": processed,
                            "tables_vacuumed": vacuumed,
                        }),
                    );
                }
            }
            _ => {}
        }
    }

    let post_scan = run_doctor_scan(pool, principal_id, ns_filter).await?;
    Ok(json!({
        "ok": true,
        "dry_run": dry_run,
        "elapsed_ms": t0.elapsed().as_millis() as u64,
        "results": results,
        "scan": post_scan,
    }))
}
