//! Native Rust Desktop Workspace App (`memnos app` / `MEMNOS.app`).
//!
//! Launches a native desktop window for the real-time MEMNOS Workspace (Slack-style `#channels`,
//! `@handles`, role-scoped memory explorer, and live server/DB telemetry), connecting to either
//! the local MEMNOS instance or any remote `https://...` MEMNOS server.

use crate::config::{home_dir, server_url, session_token, Config, VERSION};
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
pub fn install_macos_app_bundle() -> Result<std::path::PathBuf, String> {
    let exe = env::current_exe()
        .ok()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| {
            home_dir()
                .join(".local")
                .join("bin")
                .join("memnos")
                .to_string_lossy()
                .to_string()
        });
    let preferred_bin = home_dir().join(".local").join("bin").join("memnos");
    let target_bin = if preferred_bin.exists() {
        preferred_bin.to_string_lossy().to_string()
    } else {
        exe
    };

    let apps_dir = home_dir().join("Applications");
    let bundle = apps_dir.join("MEMNOS.app");
    let contents = bundle.join("Contents");
    let macos_dir = contents.join("MacOS");
    fs::create_dir_all(&macos_dir).map_err(|e| e.to_string())?;

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
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"#,
        VERSION, VERSION
    );
    fs::write(contents.join("Info.plist"), plist).map_err(|e| e.to_string())?;

    let launcher = macos_dir.join("memnos-desktop");
    let script = format!(
        "#!/bin/sh\nexport PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH\"\nexec \"{}\" app \"$@\"\n",
        target_bin
    );
    fs::write(&launcher, script).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&launcher, fs::Permissions::from_mode(0o755));
    }
    let _ = Command::new("codesign")
        .args(["-s", "-", "-f", &bundle.to_string_lossy()])
        .output();
    Ok(bundle)
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
    use wry::{http::Response as HttpResponse, WebViewBuilder};

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

    let boot_script = format!(
        "window.__MEMNOS_BOOT__ = {};",
        json!({ "url": base_url, "token": token })
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

    let window = WindowBuilder::new()
        .with_title(format!("MEMNOS · {}", base_url.trim_start_matches("http://").trim_start_matches("https://")))
        .with_inner_size(LogicalSize::new(1360.0, 850.0))
        .with_min_inner_size(LogicalSize::new(920.0, 580.0))
        .build(&event_loop)
        .map_err(|e| format!("window creation failed: {}", e))?;

    let _webview = WebViewBuilder::new()
        .with_background_color((11, 14, 23, 255))
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
        .build(&window)
        .map_err(|e| format!("webview creation failed: {}", e))?;

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        if let Event::WindowEvent {
            event: WindowEvent::CloseRequested,
            ..
        } = event
        {
            *control_flow = ControlFlow::Exit;
        }
    });
}
