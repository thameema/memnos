//! Native Rust Desktop Workspace App (`memnos app` / `MEMNOS.app`).
//!
//! Launches a native desktop window for the real-time MEMNOS Workspace (Slack-style `#channels`,
//! `@handles`, role-scoped memory explorer, live activity stream, and server/DB telemetry),
//! connecting to either the local MEMNOS instance or any remote `https://...` MEMNOS server.

use crate::config::{home_dir, server_url, session_token, Config, BUILD_SHA, VERSION};
use serde_json::json;
use std::env;
use std::fs;
use std::process::Command;

const EMBED_HTML: &[u8] = include_bytes!("../../ui/index.html");
const EMBED_CSS: &[u8] = include_bytes!("../../ui/app.css");
const EMBED_JS: &[u8] = include_bytes!("../../ui/app.js");
const EMBED_CLI: &[u8] = include_bytes!("../../ui/cli-reference.json");

fn is_loopback_url(u: &str) -> bool {
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
        raw.push(0u8); // filter type 0 (None)
        let row_start = (y * w * 4) as usize;
        raw.extend_from_slice(&rgba[row_start..row_start + (w * 4) as usize]);
    }

    // Zlib stream using stored (uncompressed) deflate blocks
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
    // Adler-32 checksum
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
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]); // 8-bit RGBA
    push_chunk(&mut png, b"IHDR", &ihdr);
    push_chunk(&mut png, b"IDAT", &z);
    push_chunk(&mut png, b"IEND", &[]);
    png
}

#[cfg(target_os = "macos")]
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

#[cfg(target_os = "macos")]
fn render_memnos_brand_rgba(size: u32) -> Vec<u8> {
    let n = size as usize;
    let mut buf = vec![0u8; n * n * 4];
    let inv = 1.0 / (size as f32);
    let aa = 1.35 * inv;

    for y in 0..size {
        for x in 0..size {
            let u = (x as f32 + 0.5) * inv;
            let v = (y as f32 + 0.5) * inv;

            // macOS Big Sur / Sequoia rounded squircle inset (0.09..0.91 -> 82% footprint)
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

            // Diagonal MEMNOS Aubergine -> Electric Violet -> Magenta brand gradient
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

            // Subtle top-left glass rim highlight
            let rim = (1.0 - ((u - 0.28).powi(2) + (v - 0.18).powi(2)).sqrt() * 2.1).clamp(0.0, 1.0);
            rr = (rr + rim * 22.0).min(255.0);
            gg = (gg + rim * 22.0).min(255.0);
            bb = (bb + rim * 22.0).min(255.0);

            // Bold geometric 'M' monogram matching the inline SVG logo across MEMNOS
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
fn build_macos_icns(resources_dir: &std::path::Path) -> Result<(), String> {
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

#[cfg(target_os = "macos")]
pub fn install_macos_app_bundle() -> Result<std::path::PathBuf, String> {
    let exe = env::current_exe().map_err(|e| e.to_string())?;
    let preferred_bin = home_dir().join(".memnos").join("bin").join("memnos");
    let src_bin = if exe
        .file_name()
        .map(|n| n != "memnos-desktop")
        .unwrap_or(false)
        && exe.is_file()
    {
        fs::canonicalize(&exe).unwrap_or(exe)
    } else if preferred_bin.is_file() {
        fs::canonicalize(&preferred_bin).unwrap_or(preferred_bin)
    } else {
        exe
    };

    let sys_apps = std::path::PathBuf::from("/Applications");
    let user_apps = home_dir().join("Applications");
    let write_bundle = |apps_dir: &std::path::Path| -> Result<std::path::PathBuf, String> {
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

        // Copy native Mach-O binary atomically inside Contents/MacOS/memnos-desktop so macOS
        // NSBundle.mainBundle resolves to MEMNOS.app and renders AppIcon.icns in Dock & Cmd+Tab!
        let launcher_tmp = macos_dir.join("memnos-desktop.new");
        let launcher = macos_dir.join("memnos-desktop");
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
    // Also mirror into /Applications/MEMNOS.app when writable so Launchpad & Finder Applications show MEMNOS natively
    let _ = write_bundle(&sys_apps);
    Ok(primary)
}

fn open_in_browser_or_app_mode(full_url: &str, force_browser: bool) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let _ = force_browser;
        Command::new("open")
            .arg(full_url)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(target_os = "windows")]
    {
        if !force_browser {
            for candidate in ["msedge", "chrome"] {
                if Command::new(candidate)
                    .arg(format!("--app={}", full_url))
                    .spawn()
                    .is_ok()
                {
                    return Ok(());
                }
            }
        }
        Command::new("cmd")
            .args(["/C", "start", "", full_url])
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        if !force_browser {
            for candidate in ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"] {
                if Command::new(candidate)
                    .arg(format!("--app={}", full_url))
                    .spawn()
                    .is_ok()
                {
                    return Ok(());
                }
            }
        }
        Command::new("xdg-open")
            .arg(full_url)
            .spawn()
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
}

pub fn run_desktop_app(
    url_opt: Option<&str>,
    token_opt: Option<&str>,
    install_bundle: bool,
    open_browser: bool,
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
        println!("[memnos] `memnos app --install` creates a `.app` bundle on macOS; run `memnos app` directly on this OS.");
        return Ok(());
    }

    let (base_url, token) = resolve_app_target(url_opt, token_opt, cfg);
    let browser_url = if token.is_empty() {
        format!("{}/app", base_url)
    } else {
        format!(
            "{}/app#token={}&url={}",
            base_url,
            url::form_urlencoded::byte_serialize(token.as_bytes()).collect::<String>(),
            url::form_urlencoded::byte_serialize(base_url.as_bytes()).collect::<String>()
        )
    };

    if open_browser {
        println!("[memnos] opening workspace in browser -> {}/app", base_url);
        return open_in_browser_or_app_mode(&browser_url, true);
    }

    #[cfg(target_os = "macos")]
    {
        let inside_bundle = env::current_exe()
            .ok()
            .and_then(|p| p.file_name().map(|n| n == "memnos-desktop"))
            .unwrap_or(false);
        if !inside_bundle {
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
                    return Ok(());
                }
            }
        }
        return launch_macos_webview(&base_url, &token);
    }

    #[cfg(not(target_os = "macos"))]
    {
        println!("[memnos] launching MEMNOS workspace -> {}/app", base_url);
        open_in_browser_or_app_mode(&browser_url, false)
    }
}

#[cfg(target_os = "macos")]
fn launch_macos_webview(base_url: &str, token: &str) -> Result<(), String> {
    use muda::{Menu, PredefinedMenuItem, Submenu};
    use tao::{
        dpi::LogicalSize,
        event::{Event, WindowEvent},
        event_loop::{ControlFlow, EventLoop},
        window::WindowBuilder,
    };
    use wry::{
        dpi::{LogicalPosition as WryLogicalPos, LogicalSize as WryLogicalSize},
        http::Response as HttpResponse,
        Rect, WebViewBuilder,
    };

    let server_has_app = ureq::get(&format!("{}/app", base_url))
        .timeout(std::time::Duration::from_millis(1200))
        .call()
        .map(|r| r.status() == 200)
        .unwrap_or(false);

    let start_url = if server_has_app {
        format!("{}/app", base_url)
    } else {
        "memnos://localhost/index.html".to_string()
    };

    let local_cfg = crate::config::load_config();
    let local_url = server_url(&local_cfg).trim_end_matches('/').to_string();
    let local_tok = local_cfg
        .admin_token
        .as_ref()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| session_token(&local_cfg));

    let boot_script = format!(
        "window.__MEMNOS_BOOT__ = {};",
        json!({
            "url": base_url,
            "token": token,
            "local_url": local_url,
            "local_token": local_tok,
            "desktop": true,
            "app_version": VERSION,
            "app_build": BUILD_SHA
        })
    );

    let event_loop = EventLoop::new();

    // Native macOS Menu bar so Cmd+Q, Cmd+W, Cmd+C, Cmd+V, Cmd+X, Cmd+A work natively
    let menu_bar = Menu::new();
    let app_m = Submenu::new("MEMNOS", true);
    let _ = app_m.append_items(&[
        &PredefinedMenuItem::about(None, None),
        &PredefinedMenuItem::separator(),
        &PredefinedMenuItem::services(None),
        &PredefinedMenuItem::separator(),
        &PredefinedMenuItem::hide(None),
        &PredefinedMenuItem::hide_others(None),
        &PredefinedMenuItem::show_all(None),
        &PredefinedMenuItem::separator(),
        &PredefinedMenuItem::quit(None),
    ]);
    let edit_m = Submenu::new("Edit", true);
    let _ = edit_m.append_items(&[
        &PredefinedMenuItem::undo(None),
        &PredefinedMenuItem::redo(None),
        &PredefinedMenuItem::separator(),
        &PredefinedMenuItem::cut(None),
        &PredefinedMenuItem::copy(None),
        &PredefinedMenuItem::paste(None),
        &PredefinedMenuItem::select_all(None),
    ]);
    let win_m = Submenu::new("Window", true);
    let _ = win_m.append_items(&[
        &PredefinedMenuItem::minimize(None),
        &PredefinedMenuItem::maximize(None),
        &PredefinedMenuItem::separator(),
        &PredefinedMenuItem::close_window(None),
    ]);
    let _ = menu_bar.append_items(&[&app_m, &edit_m, &win_m]);
    menu_bar.init_for_nsapp();

    let title_ver = if BUILD_SHA.is_empty() {
        format!("v{}", VERSION)
    } else {
        format!("v{} ({})", VERSION, BUILD_SHA)
    };
    let window = WindowBuilder::new()
        .with_title(format!(
            "MEMNOS {} · {}",
            title_ver,
            base_url
                .trim_start_matches("http://")
                .trim_start_matches("https://")
        ))
        .with_inner_size(LogicalSize::new(1380.0, 860.0))
        .with_min_inner_size(LogicalSize::new(900.0, 560.0))
        .build(&event_loop)
        .map_err(|e| format!("window creation failed: {}", e))?;

    let sf = window.scale_factor();
    let init_logical = window.inner_size().to_logical::<f64>(sf);
    let init_bounds = Rect {
        position: WryLogicalPos::new(0.0, 0.0).into(),
        size: WryLogicalSize::new(init_logical.width, init_logical.height).into(),
    };

    let webview = WebViewBuilder::new()
        .with_bounds(init_bounds)
        .with_background_color((20, 14, 24, 255))
        .with_initialization_script(&boot_script)
        .with_custom_protocol("memnos".into(), |_id, req| {
            let path = req.uri().path();
            let (ctype, bytes): (&str, &[u8]) = match path {
                "/admin/app.css" | "/app.css" => ("text/css; charset=utf-8", EMBED_CSS),
                "/admin/app.js" | "/app.js" => ("application/javascript; charset=utf-8", EMBED_JS),
                "/admin/cli-reference.json" | "/cli-reference.json" => {
                    ("application/json; charset=utf-8", EMBED_CLI)
                }
                _ => ("text/html; charset=utf-8", EMBED_HTML),
            };
            HttpResponse::builder()
                .header("Content-Type", ctype)
                .body(std::borrow::Cow::Borrowed(bytes))
                .unwrap()
        })
        .with_url(&start_url)
        .build_as_child(&window)
        .map_err(|e| format!("webview creation failed: {}", e))?;

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                *control_flow = ControlFlow::Exit;
            }
            Event::WindowEvent {
                event: WindowEvent::Resized(phys_size),
                ..
            } => {
                let scale = window.scale_factor();
                let logical = phys_size.to_logical::<f64>(scale);
                let _ = webview.set_bounds(Rect {
                    position: WryLogicalPos::new(0.0, 0.0).into(),
                    size: WryLogicalSize::new(logical.width, logical.height).into(),
                });
            }
            Event::WindowEvent {
                event:
                    WindowEvent::ScaleFactorChanged {
                        scale_factor,
                        new_inner_size,
                    },
                ..
            } => {
                let logical = new_inner_size.to_logical::<f64>(scale_factor);
                let _ = webview.set_bounds(Rect {
                    position: WryLogicalPos::new(0.0, 0.0).into(),
                    size: WryLogicalSize::new(logical.width, logical.height).into(),
                });
            }
            _ => {}
        }
    });
}
