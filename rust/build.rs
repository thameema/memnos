use std::path::{Path, PathBuf};
use std::process::Command;

fn resolve_git_dir(manifest_dir: &Path) -> Option<PathBuf> {
    let dot_git = manifest_dir.join(".git");
    if dot_git.is_dir() {
        return Some(dot_git);
    }
    if dot_git.is_file() {
        let content = std::fs::read_to_string(&dot_git).ok()?;
        let raw = content.strip_prefix("gitdir:")?.trim();
        let p = PathBuf::from(raw);
        return Some(if p.is_absolute() {
            p
        } else {
            manifest_dir.join(p)
        });
    }
    None
}

fn main() {
    println!("cargo:rerun-if-changed=rust/build.rs");
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        let mpath = PathBuf::from(&manifest);
        let dot_git = mpath.join(".git");
        if dot_git.exists() {
            println!("cargo:rerun-if-changed={}", dot_git.display());
        }
        if let Some(git_dir) = resolve_git_dir(&mpath) {
            let head = git_dir.join("HEAD");
            if head.exists() {
                println!("cargo:rerun-if-changed={}", head.display());
                if let Ok(h) = std::fs::read_to_string(&head) {
                    if let Some(r) = h.strip_prefix("ref:") {
                        let ref_rel = r.trim();
                        let local_ref = git_dir.join(ref_rel);
                        if local_ref.exists() {
                            println!("cargo:rerun-if-changed={}", local_ref.display());
                        }
                        let commondir_file = git_dir.join("commondir");
                        if let Ok(cd) = std::fs::read_to_string(&commondir_file) {
                            let common_ref = git_dir.join(cd.trim()).join(ref_rel);
                            if common_ref.exists() {
                                println!("cargo:rerun-if-changed={}", common_ref.display());
                            }
                        }
                    }
                }
            }
        }
    }

    let sha = Command::new("git")
        .args(["rev-parse", "--short=7", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| {
            s.trim()
                .chars()
                .filter(|c| c.is_ascii_hexdigit())
                .take(12)
                .collect::<String>()
                .to_ascii_lowercase()
        })
        .unwrap_or_default();

    println!("cargo:rustc-env=MEMNOS_BUILD_SHA={}", sha);
}
