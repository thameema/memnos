//! Verified PostgreSQL database backup helpers (`pg_dump -Fc` + `pg_restore --list`)
//! enforced before upgrades, migrations, namespace moves/merges/purges, and `doctor --fix`.

use crate::config::{config_dir, dsn, Config};
use serde_json::Value;
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn find_pg_tool_bin(tool: &str) -> Option<PathBuf> {
    let exe_name = if cfg!(windows) {
        format!("{}.exe", tool)
    } else {
        tool.to_string()
    };

    if let Ok(explicit_env) = env::var(format!("MEMNOS_{}", tool.to_ascii_uppercase())) {
        let trimmed = explicit_env.trim();
        if !trimmed.is_empty() {
            let p = PathBuf::from(trimmed);
            return if p.is_file() { Some(p) } else { None };
        }
    }
    if let Ok(pg_bin_dir) = env::var("MEMNOS_PG_BIN") {
        let trimmed = pg_bin_dir.trim();
        if !trimmed.is_empty() {
            let cand = PathBuf::from(trimmed).join(&exe_name);
            return if cand.is_file() { Some(cand) } else { None };
        }
    }

    // 1. Embedded PostgreSQL bundle (`~/.memnos/embedded_pg/state.json` `pg_dir/bin/<tool>`)
    let emb_root = config_dir().join("embedded_pg");
    if let Ok(raw_state) = fs::read_to_string(emb_root.join("state.json")) {
        if let Ok(st_json) = serde_json::from_str::<Value>(&raw_state) {
            if let Some(pg_dir) = st_json.get("pg_dir").and_then(|v| v.as_str()) {
                let cand = PathBuf::from(pg_dir).join("bin").join(&exe_name);
                if cand.is_file() {
                    return Some(cand);
                }
            }
        }
    }
    if let Ok(entries) = fs::read_dir(&emb_root) {
        for entry in entries.flatten() {
            let p1 = entry.path().join("bin").join(&exe_name);
            if p1.is_file() {
                return Some(p1);
            }
            if let Ok(sub_entries) = fs::read_dir(entry.path()) {
                for sub in sub_entries.flatten() {
                    let p2 = sub.path().join("bin").join(&exe_name);
                    if p2.is_file() {
                        return Some(p2);
                    }
                }
            }
        }
    }

    // 2. Active PATH
    if let Ok(path_var) = env::var("PATH") {
        for dir in env::split_paths(&path_var) {
            let cand = dir.join(&exe_name);
            if cand.is_file() {
                return Some(cand);
            }
        }
    }

    // 3. Common OS PostgreSQL client install paths
    #[allow(unused_mut)]
    let mut candidates: Vec<PathBuf> = vec![
        emb_root.join("bin").join(&exe_name),
        PathBuf::from(format!("/opt/homebrew/opt/libpq/bin/{}", exe_name)),
        PathBuf::from(format!("/opt/homebrew/opt/postgresql@18/bin/{}", exe_name)),
        PathBuf::from(format!("/opt/homebrew/opt/postgresql@17/bin/{}", exe_name)),
        PathBuf::from(format!("/opt/homebrew/opt/postgresql@16/bin/{}", exe_name)),
        PathBuf::from(format!("/opt/homebrew/opt/postgresql@15/bin/{}", exe_name)),
        PathBuf::from(format!("/opt/homebrew/bin/{}", exe_name)),
        PathBuf::from(format!("/usr/local/opt/libpq/bin/{}", exe_name)),
        PathBuf::from(format!("/usr/local/opt/postgresql@17/bin/{}", exe_name)),
        PathBuf::from(format!("/usr/local/opt/postgresql@16/bin/{}", exe_name)),
        PathBuf::from(format!("/usr/local/bin/{}", exe_name)),
        PathBuf::from(format!(
            "/Applications/Postgres.app/Contents/Versions/latest/bin/{}",
            exe_name
        )),
        PathBuf::from(format!("/usr/lib/postgresql/17/bin/{}", exe_name)),
        PathBuf::from(format!("/usr/lib/postgresql/16/bin/{}", exe_name)),
        PathBuf::from(format!("/usr/lib/postgresql/15/bin/{}", exe_name)),
    ];
    #[cfg(windows)]
    {
        for v in ["18", "17", "16", "15"] {
            candidates.push(PathBuf::from(format!(
                r"C:\Program Files\PostgreSQL\{}\bin\{}",
                v, exe_name
            )));
        }
    }
    candidates.into_iter().find(|p| p.is_file())
}

pub fn missing_pg_client_tools_message(missing_tool: &str) -> String {
    let hint = if cfg!(target_os = "macos") {
        "Install PostgreSQL client tools via `brew install libpq` (or set MEMNOS_PG_BIN=/path/to/pg/bin)."
    } else if cfg!(windows) {
        r#"Install PostgreSQL client tools via `winget install PostgreSQL.PostgreSQL` (or set MEMNOS_PG_BIN="C:\Program Files\PostgreSQL\17\bin"). Embedded PostgreSQL installs (`memnos setup`) bundle `pg_dump` and `pg_restore` automatically."#
    } else {
        "Install PostgreSQL client tools via `sudo apt-get install -y postgresql-client` (or set MEMNOS_PG_BIN=/path/to/pg/bin)."
    };
    format!(
        "refusing operation without a verified PostgreSQL backup: `{}` was not found on PATH, in ~/.memnos/embedded_pg, or in MEMNOS_PG_BIN. {}",
        missing_tool, hint
    )
}

pub fn precreate_private_file(p: &Path) -> Result<fs::File, String> {
    let mut opts = fs::OpenOptions::new();
    opts.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    opts.open(p)
        .map_err(|e| format!("failed to pre-create private file {}: {}", p.display(), e))
}

pub fn copy_private_file(src: &Path, dst: &Path) -> Result<(), String> {
    let mut in_f = fs::File::open(src).map_err(|e| e.to_string())?;
    let mut out_f = precreate_private_file(dst)?;
    io::copy(&mut in_f, &mut out_f).map_err(|e| e.to_string())?;
    out_f.sync_all().ok();
    Ok(())
}

pub fn prune_old_backups(backups_dir: &Path, keep: usize, protect_path: Option<&Path>) -> usize {
    if keep == 0 {
        return 0;
    }
    let Ok(entries) = fs::read_dir(backups_dir) else {
        return 0;
    };
    let protect_canon = protect_path.and_then(|p| fs::canonicalize(p).ok());
    let mut valid_dumps: Vec<(bool, std::time::SystemTime, String, PathBuf)> = Vec::new();
    let mut removed = 0usize;

    for entry in entries.flatten() {
        let p = entry.path();
        let name = p
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_string();
        if !name.ends_with(".dump") {
            continue;
        }
        let is_protected = protect_path.map(|pp| pp == p).unwrap_or(false)
            || (protect_canon.is_some() && fs::canonicalize(&p).ok() == protect_canon);
        let Ok(meta) = fs::metadata(&p) else {
            continue;
        };
        if !meta.is_file() {
            continue;
        }
        // Never allow an empty/corrupt 0-byte dump file to count toward `keep` and evict a valid backup
        if meta.len() == 0 && !is_protected {
            let _ = fs::remove_file(&p);
            let _ = fs::remove_file(p.with_extension("config.json"));
            removed += 1;
            continue;
        }
        let mtime = meta.modified().unwrap_or(std::time::UNIX_EPOCH);
        valid_dumps.push((is_protected, mtime, name, p));
    }

    // Protected (freshly verified) dump always sorts first, then newest mtime, then lexicographically newest name
    valid_dumps.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| b.1.cmp(&a.1))
            .then_with(|| b.2.cmp(&a.2))
    });

    let mut kept_stems: std::collections::HashSet<String> = std::collections::HashSet::new();
    for (idx, (_, _, _, dump_path)) in valid_dumps.into_iter().enumerate() {
        if idx < keep {
            if let Some(stem) = dump_path.file_stem().and_then(|s| s.to_str()) {
                kept_stems.insert(stem.to_string());
            }
        } else {
            let cfg_snap = dump_path.with_extension("config.json");
            if fs::remove_file(&dump_path).is_ok() {
                removed += 1;
            }
            let _ = fs::remove_file(&cfg_snap);
        }
    }

    // Also remove any orphaned `memnos-*.config.json` snapshots whose `.dump` was already deleted
    if let Ok(entries2) = fs::read_dir(backups_dir) {
        for entry in entries2.flatten() {
            let p = entry.path();
            let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
            if name.starts_with("memnos-") && name.ends_with(".config.json") {
                let stem = name.trim_end_matches(".config.json");
                if !kept_stems.contains(stem) {
                    let _ = fs::remove_file(&p);
                }
            }
        }
    }

    removed
}

pub fn take_verified_db_backup_sync(
    pg_dsn: &str,
    reason: &str,
    custom_out: Option<&str>,
    keep: usize,
) -> Result<PathBuf, String> {
    let parsed_url =
        url::Url::parse(pg_dsn).map_err(|e| format!("invalid PostgreSQL DSN for backup: {}", e))?;
    let pg_dump_bin =
        find_pg_tool_bin("pg_dump").ok_or_else(|| missing_pg_client_tools_message("pg_dump"))?;
    let pg_restore_bin = find_pg_tool_bin("pg_restore")
        .ok_or_else(|| missing_pg_client_tools_message("pg_restore"))?;

    let backups_dir = config_dir().join("backups");
    fs::create_dir_all(&backups_dir)
        .map_err(|e| format!("create backups dir failed: {}", e))?;
    let now = chrono::Utc::now();
    let base_ts = now.format("%Y%m%dT%H%M%SZ").to_string();

    let target_path = match custom_out {
        Some(raw) => PathBuf::from(raw),
        None => {
            let p0 = backups_dir.join(format!("memnos-{}-{}.dump", reason, base_ts));
            if p0.exists() {
                let ms_ts = now.format("%Y%m%dT%H%M%S_%3fZ").to_string();
                backups_dir.join(format!("memnos-{}-{}.dump", reason, ms_ts))
            } else {
                p0
            }
        }
    };
    if let Some(parent) = target_path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
    }

    // Pre-create with 0600 permissions BEFORE pg_dump writes a single byte (zero world-readable window)
    drop(precreate_private_file(&target_path)?);

    eprintln!(
        "[memnos] taking mandatory database backup ({}) -> {} ...",
        reason,
        target_path.display()
    );

    // Pass connection parameters & password via PG* environment variables (never in `ps` argv)
    let mut cmd = Command::new(&pg_dump_bin);
    cmd.args([
        "-Fc",
        "--no-owner",
        "--no-acl",
        "-f",
        &target_path.to_string_lossy(),
    ]);
    if let Some(host) = parsed_url.host_str() {
        cmd.env("PGHOST", host);
    }
    if let Some(port) = parsed_url.port() {
        cmd.env("PGPORT", port.to_string());
    }
    let user = parsed_url.username();
    if !user.is_empty() {
        cmd.env("PGUSER", user);
    }
    if let Some(pw) = parsed_url.password() {
        cmd.env("PGPASSWORD", pw);
    }
    let dbname = parsed_url.path().trim_start_matches('/');
    if !dbname.is_empty() {
        cmd.env("PGDATABASE", dbname);
    }

    let out = cmd
        .output()
        .map_err(|e| format!("failed to run {}: {}", pg_dump_bin.display(), e))?;
    if !out.status.success() {
        let _ = fs::remove_file(&target_path);
        let stderr = String::from_utf8_lossy(&out.stderr);
        return Err(format!(
            "pre-operation database backup failed via pg_dump (aborting to protect data): {}",
            stderr.trim()
        ));
    }

    let meta = fs::metadata(&target_path).map_err(|e| {
        format!(
            "backup verification failed on {}: {}",
            target_path.display(),
            e
        )
    })?;
    if meta.len() == 0 {
        let _ = fs::remove_file(&target_path);
        return Err(format!(
            "backup verification failed: {} is 0 bytes (aborting)",
            target_path.display()
        ));
    }

    // Verify archive integrity and table-of-contents via `pg_restore --list`
    let verify_out = Command::new(&pg_restore_bin)
        .args(["--list", &target_path.to_string_lossy()])
        .output()
        .map_err(|e| format!("failed running pg_restore --list: {}", e))?;
    let toc_lines = String::from_utf8_lossy(&verify_out.stdout)
        .lines()
        .filter(|l| !l.trim().is_empty() && !l.trim_start().starts_with(';'))
        .count();
    if !verify_out.status.success() || toc_lines == 0 {
        let _ = fs::remove_file(&target_path);
        let stderr = String::from_utf8_lossy(&verify_out.stderr);
        return Err(format!(
            "backup archive verification (`pg_restore --list`) failed on {}: {}",
            target_path.display(),
            stderr.trim()
        ));
    }

    let src_cfg = config_dir().join("config.json");
    if src_cfg.is_file() {
        let cfg_snap = target_path.with_extension("config.json");
        let _ = copy_private_file(&src_cfg, &cfg_snap);
    }

    if custom_out.is_none() {
        let pruned = prune_old_backups(&backups_dir, keep.max(1), Some(&target_path));
        if pruned > 0 {
            eprintln!(
                "[memnos] pruned {} older database backup(s) (keeping latest {} verified dump).",
                pruned,
                keep.max(1)
            );
        }
    }

    let mb = meta.len() as f64 / (1024.0 * 1024.0);
    eprintln!(
        "[memnos] ✓ verified DB backup saved ({:.2} MB, {} archive objects verified via pg_restore): {}",
        mb,
        toc_lines,
        target_path.display()
    );
    Ok(target_path)
}

pub async fn take_verified_db_backup_dsn(
    pg_dsn: &str,
    reason: &str,
    custom_out: Option<&str>,
    keep: usize,
) -> Result<PathBuf, String> {
    let dsn_owned = pg_dsn.to_string();
    let reason_owned = reason.to_string();
    let out_owned = custom_out.map(|s| s.to_string());
    tokio::task::spawn_blocking(move || {
        take_verified_db_backup_sync(&dsn_owned, &reason_owned, out_owned.as_deref(), keep)
    })
    .await
    .map_err(|e| format!("backup worker failed: {e}"))?
}

pub async fn take_verified_db_backup_cfg(
    cfg: &Config,
    reason: &str,
    custom_out: Option<&str>,
    keep: usize,
) -> Result<PathBuf, String> {
    let pg_dsn = dsn(cfg);
    take_verified_db_backup_dsn(&pg_dsn, reason, custom_out, keep).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prune_keeps_exactly_one_verified_backup_across_all_reasons_and_ignores_zero_byte_dumps() {
        let tmp = env::temp_dir().join(format!("memnos-backup-prune-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&tmp);
        fs::create_dir_all(&tmp).unwrap();

        let b1 = tmp.join("memnos-manual-20261120T010000Z.dump");
        let b2 = tmp.join("memnos-pre-upgrade-20261121T010000Z.dump");
        let b3 = tmp.join("memnos-pre-doctor-20261122T010000Z.dump");
        let b4_latest = tmp.join("memnos-pre-upgrade-20261123T010000Z.dump");
        let orphan_cfg = tmp.join("memnos-pre-cutover-20261119.config.json");
        let broken_zero = tmp.join("memnos-pre-upgrade-20990101T000000Z.dump");

        for p in [&b1, &b2, &b3, &b4_latest] {
            fs::write(p, b"PGDMP-valid-payload").unwrap();
            fs::write(p.with_extension("config.json"), b"{\"port\":8900}").unwrap();
        }
        fs::write(&orphan_cfg, b"{}").unwrap();
        // 0-byte failed/partial dump with a newer filename must NEVER evict b4_latest!
        fs::write(&broken_zero, b"").unwrap();

        let pruned = prune_old_backups(&tmp, 1, Some(&b4_latest));
        assert_eq!(pruned, 4); // b1, b2, b3, and 0-byte broken_zero removed

        assert!(b4_latest.is_file(), "newest verified dump must be retained");
        assert!(
            b4_latest.with_extension("config.json").is_file(),
            "newest companion config.json must be retained"
        );
        for gone in [&b1, &b2, &b3, &broken_zero] {
            assert!(!gone.exists(), "{} should be pruned", gone.display());
            assert!(!gone.with_extension("config.json").exists());
        }
        assert!(
            !orphan_cfg.exists(),
            "orphaned memnos-*.config.json must be cleaned up"
        );

        let _ = fs::remove_dir_all(&tmp);
    }
}
