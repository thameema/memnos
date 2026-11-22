//! Native Desktop Workspace Launcher & Bundle Installer (`memnos app` / `MEMNOS.app`).
//!
//! Launches the single-process native `memnos-desktop` workspace application and builds
//! the macOS `MEMNOS.app` bundle (with Retina `AppIcon.icns`) without any browser or WebView
//! dependencies in `memnos`.

use crate::config::{home_dir, server_url, session_token, Config, BUILD_SHA, VERSION};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub fn is_loopback_url(u: &str) -> bool {
    let l = u.to_ascii_lowercase();
    l.contains("://127.0.0.1") || l.contains("://localhost") || l.contains("://[::1]")
}

pub fn resolve_app_target(
    url_opt: Option<&str>,
    token_opt: Option<&str>,
    cfg: &Config,
) -> (String, String) {
    let base_url = url_opt
        .map(|s| s.trim().trim_end_matches('/').to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            env::var("MEMNOS_URL")
                .ok()
                .map(|s| s.trim().trim_end_matches('/').to_string())
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_else(|| server_url(cfg).trim_end_matches('/').to_string());

    let tok = token_opt
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            if is_loopback_url(&base_url) {
                cfg.admin_token
                    .as_ref()
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
            } else {
                None
            }
        })
        .unwrap_or_else(|| session_token(cfg));

    (base_url, tok)
}

fn seg_dist(px: f32, py: f32, ax: f32, ay: f32, bx: f32, by: f32) -> f32 {
    let vx = bx - ax;
    let vy = by - ay;
    let wx = px - ax;
    let wy = py - ay;
    let len2 = vx * vx + vy * vy;
    let t = if len2 > 0.0 {
        ((wx * vx + wy * vy) / len2).clamp(0.0, 1.0)
    } else {
        0.0
    };
    let dx = px - (ax + t * vx);
    let dy = py - (ay + t * vy);
    (dx * dx + dy * dy).sqrt()
}

/// Renders the MEMNOS rounded-squircle brand icon (`M` monogram over Aubergine -> Violet -> Magenta)
/// into an 8-bit RGBA buffer of `size * size * 4` bytes. Used both for window `IconData` on
/// macOS/Windows/Linux and for generating macOS Retina `AppIcon.icns`.
pub fn render_memnos_brand_rgba(size: u32) -> Vec<u8> {
    let n = size as usize;
    let mut buf = vec![0u8; n * n * 4];
    let inv = 1.0 / (size as f32);
    let aa = 1.35 * inv;

    for y in 0..size {
        for x in 0..size {
            let u = (x as f32 + 0.5) * inv;
            let v = (y as f32 + 0.5) * inv;

            let cx = (u - 0.5).abs();
            let cy = (v - 0.5).abs();
            let half = 0.412_f32;
            let r = 0.185_f32;
            let qx = cx - (half - r);
            let qy = cy - (half - r);
            let outside = (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt();
            let inside = qx.max(qy).min(0.0);
            let d_bg = outside + inside - r;

            let bg_alpha = ((aa - d_bg) / (2.0 * aa)).clamp(0.0, 1.0);
            if bg_alpha <= 0.0 {
                continue;
            }

            let t = ((u - 0.09) * 0.58 + (v - 0.09) * 0.62).clamp(0.0, 1.0);
            let (mut rr, mut gg, mut bb) = if t < 0.52 {
                let k = t / 0.52;
                (
                    124.0 + (184.0 - 124.0) * k,
                    92.0 + (50.0 - 92.0) * k,
                    255.0 + (229.0 - 255.0) * k,
                )
            } else {
                let k = (t - 0.52) / 0.48;
                (
                    184.0 + (236.0 - 184.0) * k,
                    50.0 + (72.0 - 50.0) * k,
                    229.0 + (153.0 - 229.0) * k,
                )
            };

            let rim = (1.0 - ((u - 0.28).powi(2) + (v - 0.18).powi(2)).sqrt() * 2.1).clamp(0.0, 1.0);
            rr = (rr + rim * 22.0).min(255.0);
            gg = (gg + rim * 22.0).min(255.0);
            bb = (bb + rim * 22.0).min(255.0);

            let stroke = 0.052_f32;
            let d1 = seg_dist(u, v, 0.275, 0.285, 0.275, 0.715) - stroke;
            let d2 = seg_dist(u, v, 0.725, 0.285, 0.725, 0.715) - stroke;
            let d3 = seg_dist(u, v, 0.275, 0.285, 0.500, 0.560) - stroke * 0.94;
            let d4 = seg_dist(u, v, 0.725, 0.285, 0.500, 0.560) - stroke * 0.94;
            let d_m = d1.min(d2).min(d3).min(d4);

            let m_alpha = ((aa - d_m) / (2.0 * aa)).clamp(0.0, 1.0);
            rr = rr * (1.0 - m_alpha) + 255.0 * m_alpha;
            gg = gg * (1.0 - m_alpha) + 255.0 * m_alpha;
            bb = bb * (1.0 - m_alpha) + 255.0 * m_alpha;

            let idx = ((y * size + x) * 4) as usize;
            buf[idx] = rr.round() as u8;
            buf[idx + 1] = gg.round() as u8;
            buf[idx + 2] = bb.round() as u8;
            buf[idx + 3] = (bg_alpha * 255.0).round() as u8;
        }
    }
    buf
}

#[cfg(target_os = "macos")]
fn crc32_png(data: &[u8]) -> u32 {
    let mut crc: u32 = 0xffff_ffff;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            if crc & 1 != 0 {
                crc = (crc >> 1) ^ 0xedb8_8320;
            } else {
                crc >>= 1;
            }
        }
    }
    !crc
}

#[cfg(target_os = "macos")]
fn encode_uncompressed_rgba_png(w: u32, h: u32, rgba: &[u8]) -> Vec<u8> {
    let mut raw = Vec::with_capacity(((w * 4 + 1) * h) as usize);
    for y in 0..h {
        raw.push(0u8);
        let row_start = (y * w * 4) as usize;
        raw.extend_from_slice(&rgba[row_start..row_start + (w * 4) as usize]);
    }

    let mut z = Vec::with_capacity(raw.len() + 64);
    z.extend_from_slice(&[0x78, 0x01]);
    let chunks: Vec<&[u8]> = raw.chunks(65_535).collect();
    for (i, ch) in chunks.iter().enumerate() {
        let last = if i + 1 == chunks.len() { 1u8 } else { 0u8 };
        z.push(last);
        let len = ch.len() as u16;
        z.extend_from_slice(&len.to_le_bytes());
        z.extend_from_slice(&(!len).to_le_bytes());
        z.extend_from_slice(ch);
    }
    let mut s1: u32 = 1;
    let mut s2: u32 = 0;
    for &b in &raw {
        s1 = (s1 + b as u32) % 65_521;
        s2 = (s2 + s1) % 65_521;
    }
    let adler = (s2 << 16) | s1;
    z.extend_from_slice(&adler.to_be_bytes());

    fn push_chunk(out: &mut Vec<u8>, tag: &[u8; 4], payload: &[u8]) {
        out.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        let start = out.len();
        out.extend_from_slice(tag);
        out.extend_from_slice(payload);
        let c = crc32_png(&out[start..]);
        out.extend_from_slice(&c.to_be_bytes());
    }

    let mut png = Vec::with_capacity(z.len() + 128);
    png.extend_from_slice(&[137, 80, 78, 71, 13, 10, 26, 10]);
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&w.to_be_bytes());
    ihdr.extend_from_slice(&h.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
    push_chunk(&mut png, b"IHDR", &ihdr);
    push_chunk(&mut png, b"IDAT", &z);
    push_chunk(&mut png, b"IEND", &[]);
    png
}

#[cfg(target_os = "macos")]
fn build_macos_icns(resources_dir: &Path) -> Result<(), String> {
    fs::create_dir_all(resources_dir).map_err(|e| e.to_string())?;
    let tmp_root = env::temp_dir().join(format!("memnos-icon-{}", std::process::id()));
    let iconset = tmp_root.join("AppIcon.iconset");
    let _ = fs::remove_dir_all(&tmp_root);
    fs::create_dir_all(&iconset).map_err(|e| e.to_string())?;

    let master_rgba = render_memnos_brand_rgba(1024);
    let master_png = encode_uncompressed_rgba_png(1024, 1024, &master_rgba);
    let master_path = iconset.join("icon_512x512@2x.png");
    fs::write(&master_path, &master_png).map_err(|e| e.to_string())?;

    for (px, filename) in [
        (16u32, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
    ] {
        let out = iconset.join(filename);
        let status = Command::new("/usr/bin/sips")
            .args([
                "-z",
                &px.to_string(),
                &px.to_string(),
                &master_path.to_string_lossy(),
                "--out",
                &out.to_string_lossy(),
            ])
            .output();
        if status.map(|o| !o.status.success()).unwrap_or(true) {
            let rgba = render_memnos_brand_rgba(px);
            let png = encode_uncompressed_rgba_png(px, px, &rgba);
            let _ = fs::write(&out, png);
        }
    }

    let icns_out = resources_dir.join("AppIcon.icns");
    let res = Command::new("/usr/bin/iconutil")
        .args([
            "-c",
            "icns",
            &iconset.to_string_lossy(),
            "-o",
            &icns_out.to_string_lossy(),
        ])
        .output()
        .map_err(|e| e.to_string())?;
    let _ = fs::remove_dir_all(&tmp_root);
    if !res.status.success() {
        return Err(format!(
            "iconutil failed: {}",
            String::from_utf8_lossy(&res.stderr)
        ));
    }
    Ok(())
}

pub fn find_desktop_binary() -> Result<PathBuf, String> {
    let bin_name = if cfg!(windows) {
        "memnos-desktop.exe"
    } else {
        "memnos-desktop"
    };

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = env::current_exe() {
        if let Ok(canon) = fs::canonicalize(&exe) {
            if let Some(dir) = canon.parent() {
                candidates.push(dir.join(bin_name));
            }
        }
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join(bin_name));
        }
    }
    let home = home_dir();
    candidates.push(home.join(".memnos").join("bin").join(bin_name));
    candidates.push(home.join(".local").join("bin").join(bin_name));

    #[cfg(target_os = "macos")]
    {
        candidates.push(
            home.join("Applications")
                .join("MEMNOS.app")
                .join("Contents")
                .join("MacOS")
                .join("memnos-desktop"),
        );
        candidates.push(PathBuf::from(
            "/Applications/MEMNOS.app/Contents/MacOS/memnos-desktop",
        ));
    }

    if let Ok(path_var) = env::var("PATH") {
        for dir in env::split_paths(&path_var) {
            candidates.push(dir.join(bin_name));
        }
    }

    for cand in candidates {
        if cand.is_file() {
            return Ok(fs::canonicalize(&cand).unwrap_or(cand));
        }
    }

    Err(format!(
        "`{}` binary not found alongside `memnos` or in ~/.memnos/bin — install or build the desktop package first",
        bin_name
    ))
}

#[cfg(target_os = "macos")]
pub fn install_macos_app_bundle() -> Result<PathBuf, String> {
    let src_bin = find_desktop_binary()?;
    let sys_apps = PathBuf::from("/Applications");
    let user_apps = home_dir().join("Applications");

    let write_bundle = |apps_dir: &Path| -> Result<PathBuf, String> {
        let bundle = apps_dir.join("MEMNOS.app");
        let contents = bundle.join("Contents");
        let macos_dir = contents.join("MacOS");
        let resources_dir = contents.join("Resources");
        fs::create_dir_all(&macos_dir).map_err(|e| e.to_string())?;
        build_macos_icns(&resources_dir)?;

        let bundle_build_ver = if BUILD_SHA.is_empty() {
            VERSION.to_string()
        } else {
            format!("{}.{}", VERSION, BUILD_SHA)
        };
        let plist = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDisplayName</key>
    <string>MEMNOS</string>
    <key>CFBundleName</key>
    <string>MEMNOS</string>
    <key>CFBundleIdentifier</key>
    <string>ai.memnos.desktop</string>
    <key>CFBundleVersion</key>
    <string>{}</string>
    <key>CFBundleShortVersionString</key>
    <string>{}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>memnos-desktop</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIconName</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"#,
            bundle_build_ver, VERSION
        );
        fs::write(contents.join("Info.plist"), plist).map_err(|e| e.to_string())?;

        let launcher = macos_dir.join("memnos-desktop");
        let same_inode = fs::canonicalize(&launcher)
            .ok()
            .map(|p| p == src_bin)
            .unwrap_or(false);

        if !same_inode {
            let launcher_tmp = macos_dir.join("memnos-desktop.new");
            fs::copy(&src_bin, &launcher_tmp).map_err(|e| e.to_string())?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&launcher_tmp, fs::Permissions::from_mode(0o755));
            }
            let _ = Command::new("codesign")
                .args(["-s", "-", "-f", &launcher_tmp.to_string_lossy()])
                .output();
            fs::rename(&launcher_tmp, &launcher).map_err(|e| e.to_string())?;
        }

        let _ = Command::new("codesign")
            .args(["-s", "-", "-f", "--deep", &bundle.to_string_lossy()])
            .output();
        let _ = Command::new("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister")
            .args(["-f", &bundle.to_string_lossy()])
            .output();
        let _ = Command::new("touch")
            .arg(&bundle.to_string_lossy().to_string())
            .output();
        Ok(bundle)
    };

    let primary = write_bundle(&user_apps)?;
    let _ = write_bundle(&sys_apps);
    Ok(primary)
}

pub fn run_desktop_app(
    url_opt: Option<&str>,
    token_opt: Option<&str>,
    install_bundle: bool,
    cfg: &Config,
) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    if install_bundle {
        let bundle = install_macos_app_bundle()?;
        println!(
            "[memnos] installed macOS desktop bundle -> {}",
            bundle.display()
        );
        return Ok(());
    }
    #[cfg(not(target_os = "macos"))]
    if install_bundle {
        let bin = find_desktop_binary()?;
        println!(
            "[memnos] desktop binary ready at {} (run `memnos app` to launch)",
            bin.display()
        );
        return Ok(());
    }

    let (base_url, token) = resolve_app_target(url_opt, token_opt, cfg);

    #[cfg(target_os = "macos")]
    {
        if let Ok(bundle) = install_macos_app_bundle() {
            let mut cmd = Command::new("open");
            cmd.arg("-a").arg(&bundle);
            if url_opt.is_some() || token_opt.is_some() {
                cmd.arg("--args");
                if let Some(u) = url_opt {
                    cmd.args(["--url", u]);
                }
                if let Some(t) = token_opt {
                    cmd.args(["--token", t]);
                }
            }
            if cmd.status().map(|s| s.success()).unwrap_or(false) {
                println!(
                    "[memnos] launched MEMNOS.app -> {} ({})",
                    base_url,
                    if token.is_empty() {
                        "connect screen"
                    } else {
                        "authenticated"
                    }
                );
                return Ok(());
            }
        }
    }

    let desktop_bin = find_desktop_binary()?;
    let mut cmd = Command::new(&desktop_bin);
    if let Some(u) = url_opt {
        cmd.args(["--url", u]);
    }
    if let Some(t) = token_opt {
        cmd.args(["--token", t]);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("failed to launch {}: {}", desktop_bin.display(), e))?;
    println!("[memnos] launched MEMNOS desktop -> {}", base_url);
    Ok(())
}
