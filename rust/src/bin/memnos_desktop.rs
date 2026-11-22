//! `memnos-desktop` — Native Desktop Workspace Client (`MEMNOS.app`).
//!
//! Renders the full Slack-Aubergine Studio Workspace UI over the internal `memnos://localhost`
//! custom protocol (never loading an `http://` URL into the view, eliminating browser web
//! consoles, `http://127.0.0.1:8900` process labels, `ScreenTimeWebExtension`, and `AutoFill`)
//! while keeping the `memnos` CLI/server binary 100% headless.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use clap::Parser;
use memnos_core::{
    config::{load_config, server_url, session_token, BUILD_SHA, VERSION},
    desktop::resolve_app_target,
};

#[cfg(any(target_os = "macos", target_os = "windows"))]
const EMBED_HTML: &[u8] = include_bytes!("../../desktop_assets/index.html");
#[cfg(any(target_os = "macos", target_os = "windows"))]
const EMBED_CSS: &[u8] = include_bytes!("../../desktop_assets/app.css");
#[cfg(any(target_os = "macos", target_os = "windows"))]
const EMBED_JS: &[u8] = include_bytes!("../../desktop_assets/app.js");
#[cfg(any(target_os = "macos", target_os = "windows"))]
const EMBED_CLI: &[u8] = include_bytes!("../../../docs/cli-reference.json");

#[derive(Parser, Debug)]
#[command(
    name = "memnos-desktop",
    version = VERSION,
    about = "MEMNOS Workspace — native desktop client for local and remote MEMNOS servers"
)]
struct DesktopArgs {
    /// MEMNOS server URL (defaults to local ~/.memnos/config.json or MEMNOS_URL)
    #[arg(long)]
    url: Option<String>,

    /// Access token (`mnk_...`; defaults to local config admin_token on loopback)
    #[arg(long)]
    token: Option<String>,
}

#[cfg(any(target_os = "macos", target_os = "windows"))]
fn launch_studio_window(base_url: &str, token: &str) -> Result<(), String> {
    use muda::{Menu, PredefinedMenuItem, Submenu};
    use serde_json::json;
    use tao::{
        dpi::LogicalSize,
        event::{Event, WindowEvent},
        event_loop::{ControlFlow, EventLoop},
        window::{Icon, WindowBuilder},
    };
    use wry::{
        dpi::{LogicalPosition as WryLogicalPos, LogicalSize as WryLogicalSize},
        http::Response as HttpResponse,
        Rect, WebViewBuilder,
    };

    let local_cfg = load_config();
    let local_url = server_url(&local_cfg).trim_end_matches('/').to_string();
    let local_tok = local_cfg
        .admin_token
        .as_ref()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| session_token(&local_cfg));

    let boot_script = format!(
        "window.__MEMNOS_UI__ = {}; window.__MEMNOS_BOOT__ = {};",
        json!({
            "version": VERSION,
            "build": BUILD_SHA,
        }),
        json!({
            "url": base_url,
            "token": token,
            "local_url": local_url,
            "local_token": local_tok,
            "desktop": true,
            "app_version": VERSION,
            "app_build": BUILD_SHA,
        })
    );

    let event_loop = EventLoop::new();

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
    #[cfg(target_os = "macos")]
    menu_bar.init_for_nsapp();

    let title_ver = if BUILD_SHA.is_empty() {
        format!("v{}", VERSION)
    } else {
        format!("v{} ({})", VERSION, BUILD_SHA)
    };

    let rgba64 = memnos_core::desktop::render_memnos_brand_rgba(64);
    let win_icon = Icon::from_rgba(rgba64, 64, 64).ok();

    let mut wb = WindowBuilder::new()
        .with_title(format!(
            "MEMNOS {} · {}",
            title_ver,
            base_url
                .trim_start_matches("http://")
                .trim_start_matches("https://")
        ))
        .with_inner_size(LogicalSize::new(1420.0, 880.0))
        .with_min_inner_size(LogicalSize::new(920.0, 580.0));
    if let Some(ic) = win_icon {
        wb = wb.with_window_icon(Some(ic));
    }

    let window = wb
        .build(&event_loop)
        .map_err(|e| format!("window creation failed: {}", e))?;

    #[cfg(target_os = "windows")]
    unsafe {
        use tao::platform::windows::WindowExtWindows;
        let _ = menu_bar.init_for_hwnd(window.hwnd() as isize);
    }

    let sf = window.scale_factor();
    let init_logical = window.inner_size().to_logical::<f64>(sf);
    let init_bounds = Rect {
        position: WryLogicalPos::new(0.0, 0.0).into(),
        size: WryLogicalSize::new(init_logical.width, init_logical.height).into(),
    };

    // Always boot via custom `memnos://localhost/` protocol (never `http://...`), preventing
    // `http://127.0.0.1:8900` process labels and `ScreenTimeWebExtension` in Activity Monitor.
    let start_url = if cfg!(target_os = "windows") {
        "http://memnos.localhost/index.html"
    } else {
        "memnos://localhost/index.html"
    };

    let webview = WebViewBuilder::new()
        .with_bounds(init_bounds)
        .with_background_color((20, 14, 24, 255))
        .with_autoplay(false)
        .with_back_forward_navigation_gestures(false)
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
                .header("Access-Control-Allow-Origin", "*")
                .header("Cache-Control", "no-store")
                .body(std::borrow::Cow::Borrowed(bytes))
                .unwrap()
        })
        .with_url(start_url)
        .build_as_child(&window)
        .map_err(|e| format!("desktop renderer creation failed: {}", e))?;

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

#[cfg(not(any(target_os = "macos", target_os = "windows")))]
fn launch_studio_window(base_url: &str, _token: &str) -> Result<(), String> {
    eprintln!(
        "[memnos-desktop] native windowing on Linux requires desktop display libraries; use `memnos` CLI against {}",
        base_url
    );
    Ok(())
}

fn main() {
    let cleaned_argv: Vec<String> = std::env::args()
        .filter(|a| !a.starts_with("-psn_"))
        .collect();
    let args = DesktopArgs::parse_from(cleaned_argv);

    let cfg = load_config();
    let (base_url, token) = resolve_app_target(args.url.as_deref(), args.token.as_deref(), &cfg);

    if let Err(e) = launch_studio_window(&base_url, &token) {
        eprintln!("error: {}", e);
        std::process::exit(1);
    }
}
