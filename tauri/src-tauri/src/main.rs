// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod accessibility;
mod audio_capture;
mod audio_output;
mod audio_pcm;
mod audio_stream;
mod clipboard;
mod focus_capture;
#[cfg(desktop)]
mod hotkey_monitor;
mod input_monitoring;
#[cfg(desktop)]
mod key_codes;
mod keyboard_layout;
mod paste_pipeline;
mod publication;
mod publication_commands;
mod server_process;
mod server_startup;
mod speak_monitor;
mod sse_frames;
mod synthetic_keys;
mod window_close;

use std::sync::Mutex;
use tauri::{command, State, Manager, WindowEvent, Emitter, Listener, RunEvent, WebviewUrl, WebviewWindowBuilder, PhysicalPosition};
use tauri_plugin_shell::ShellExt;
use tokio::sync::mpsc;

pub const DICTATE_WINDOW_LABEL: &str = "dictate";
const DICTATE_WINDOW_WIDTH: f64 = 420.0;
const DICTATE_WINDOW_HEIGHT: f64 = 64.0;

/// Create the floating dictate webview hidden. The HotkeyMonitor shows it on
/// chord-start; the frontend hides it when the capture pipeline finishes.
/// Building it at setup avoids a race where the first chord or agent-speech
/// event fires before the webview subscribes to the `dictate:*` events.
#[cfg(desktop)]
fn build_dictate_window(app: &tauri::AppHandle) -> tauri::Result<tauri::WebviewWindow> {
    let window = WebviewWindowBuilder::new(
        app,
        DICTATE_WINDOW_LABEL,
        WebviewUrl::App("?view=dictate".into()),
    )
    .title("Voicebox DS Dictate")
    .inner_size(DICTATE_WINDOW_WIDTH, DICTATE_WINDOW_HEIGHT)
    .decorations(false)
    .transparent(true)
    .always_on_top(true)
    // Follow the user across macOS Spaces / virtual desktops instead of
    // being pinned to the Space where the window was first created.
    .visible_on_all_workspaces(true)
    .skip_taskbar(true)
    .resizable(false)
    .shadow(false)
    .visible(false)
    .build()?;

    if let Some(monitor) = window.current_monitor()? {
        let monitor_size = monitor.size();
        let win_size = window.outer_size()?;
        let x = (monitor_size.width as i32 - win_size.width as i32) / 2;
        let y = (monitor_size.height as f64 * 0.04) as i32;
        window.set_position(PhysicalPosition::new(x, y))?;
    }

    Ok(window)
}

/// Position, undo click-through, and show the dictate pill window.
///
/// The hide path parks the window at (-10_000, -10_000) and toggles
/// `ignore_cursor_events(true)` so invisible click targets don't leak; we
/// undo both here. Mirrors the logic the hotkey_monitor's
/// `Effect::StartRecording` path runs, minus the focus snapshot — this is
/// for agent-initiated speech, not dictation, so there's no focused text
/// field to paste into.
/// Build the pill webview if it doesn't exist yet. Idempotent — used by
/// agent-speech to prime the webview on speak-start so its listeners can
/// register before the actual show arrives from `audio.onplaying`.
#[cfg(desktop)]
pub fn ensure_dictate_window(app: &tauri::AppHandle) {
    if app.get_webview_window(DICTATE_WINDOW_LABEL).is_none() {
        if let Err(e) = build_dictate_window(app) {
            eprintln!("ensure_dictate_window: failed to build pill: {e}");
        }
    }
}

#[cfg(desktop)]
pub fn show_dictate_window(app: &tauri::AppHandle) {
    // Build on demand so agent-initiated speech works before the user has
    // enabled the global hotkey (the hotkey path is the other place this
    // window gets built, see `enable_hotkey`).
    let window = match app.get_webview_window(DICTATE_WINDOW_LABEL) {
        Some(w) => w,
        None => match build_dictate_window(app) {
            Ok(w) => w,
            Err(e) => {
                eprintln!("show_dictate_window: failed to build pill window: {e}");
                return;
            }
        },
    };
    // current_monitor() returns None when the window has been parked
    // off any display by the hide path; fall back to the primary.
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten());
    if let Some(monitor) = monitor {
        let monitor_pos = monitor.position();
        let monitor_size = monitor.size();
        if let Ok(win_size) = window.outer_size() {
            let x = monitor_pos.x
                + (monitor_size.width as i32 - win_size.width as i32) / 2;
            let y = monitor_pos.y + (monitor_size.height as f64 * 0.04) as i32;
            let _ = window.set_position(PhysicalPosition::new(x, y));
        }
    }
    // Skip on Linux: tao's CursorIgnoreEvents handler unwraps the GdkWindow,
    // which is None until the window is first shown, aborting the process.
    // The click-through toggle is a macOS workaround and is never set on Linux.
    #[cfg(not(target_os = "linux"))]
    let _ = window.set_ignore_cursor_events(false);
    let _ = window.show();
}

const LEGACY_PORT: u16 = 8000;
pub(crate) const SERVER_PORT: u16 = 17493;

/// Find a voicebox-server process listening on a given port (Windows only).
///
/// Uses PowerShell `Get-NetTCPConnection` to look up the PID owning the port,
/// then verifies via `tasklist` that it's a voicebox process. The caller is
/// responsible for checking port occupancy first (e.g. `TcpStream::connect_timeout`).
/// Replaces the previous `netstat -ano` approach which failed on systems with
/// corrupted system DLLs (see #277).
#[cfg(windows)]
fn find_voicebox_pid_on_port(port: u16) -> Option<u32> {
    use std::process::Command;

    // Use PowerShell's Get-NetTCPConnection to find the PID listening on the port.
    // This is a built-in cmdlet that doesn't depend on netstat.exe.
    let ps_script = format!(
        "Get-NetTCPConnection -LocalPort {} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess",
        port
    );
    if let Ok(output) = Command::new("powershell")
        .args(["-NoProfile", "-Command", &ps_script])
        .output()
    {
        let output_str = String::from_utf8_lossy(&output.stdout);
        for line in output_str.lines() {
            if let Ok(pid) = line.trim().parse::<u32>() {
                if pid <= 1 {
                    continue;
                }
                // Verify this PID is a voicebox process
                if let Ok(tasklist_output) = Command::new("tasklist")
                    .args(["/FI", &format!("PID eq {}", pid), "/FO", "CSV", "/NH"])
                    .output()
                {
                    let tasklist_str = String::from_utf8_lossy(&tasklist_output.stdout);
                    if server_process::is_windows_server_image(&tasklist_str) {
                        return Some(pid);
                    }
                }
            }
        }
    }

    None
}

/// Check if a Voicebox server is responding on the given port.
///
/// Sends an HTTP GET to `/health` and returns `true` only if the response
/// is valid JSON matching the Voicebox `HealthResponse` schema — specifically
/// `status` must be `"healthy"`, and both `model_loaded` and `gpu_available`
/// must be present as booleans. This prevents misidentifying an unrelated
/// service that happens to expose a `/health` endpoint.
#[allow(dead_code)] // Used in platform-specific cfg blocks
fn check_health(port: u16) -> bool {
    let url = format!("http://127.0.0.1:{}/health", port);
    match reqwest::blocking::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(3))
        .build()
    {
        Ok(client) => match client.get(&url).send() {
            Ok(resp) => {
                if resp.status() != reqwest::StatusCode::OK {
                    return false;
                }
                // Parse as JSON and validate Voicebox-specific fields
                match resp.json::<serde_json::Value>() {
                    Ok(body) => {
                        body.get("status").and_then(|v| v.as_str()) == Some("healthy")
                            && body.get("model_loaded").map(|v| v.is_boolean()).unwrap_or(false)
                            && body.get("gpu_available").map(|v| v.is_boolean()).unwrap_or(false)
                    }
                    Err(_) => false,
                }
            }
            Err(_) => false,
        },
        Err(_) => false,
    }
}

#[cfg(debug_assertions)]
async fn manual_server_available(port: u16) -> bool {
    tokio::task::spawn_blocking(move || check_health(port))
        .await
        .unwrap_or(false)
}

fn eligible_gpu_backends(
    pin: Option<&str>,
    rocm: Option<std::path::PathBuf>,
    cuda: Option<std::path::PathBuf>,
) -> (Option<std::path::PathBuf>, Option<std::path::PathBuf>) {
    let pin_cuda = pin == Some("cuda") && cuda.is_some();
    let pin_rocm = pin == Some("rocm") && rocm.is_some();
    (if pin_cuda { None } else { rocm }, if pin_rocm { None } else { cuda })
}

fn server_arguments(data_dir: &str, port: u16, parent_pid: u32, remote: bool) -> Vec<String> {
    let mut args = vec![
        "--data-dir".into(), data_dir.into(), "--port".into(), port.to_string(),
        "--parent-pid".into(), parent_pid.to_string(),
    ];
    if remote { args.extend(["--host".into(), "0.0.0.0".into()]); }
    args
}

#[cfg(any(target_os = "linux", test))]
fn trusted_microphone_origin(uri: &str) -> bool {
    let Ok(url) = reqwest::Url::parse(uri) else {
        return false;
    };
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    match (url.scheme(), url.host_str(), url.port()) {
        ("tauri", Some("localhost"), None) => true,
        ("https", Some("tauri.localhost"), None) => true,
        ("http", Some("localhost" | "127.0.0.1"), _) => true,
        _ => false,
    }
}

#[cfg(any(target_os = "linux", test))]
fn allow_media_request(uri: &str, audio: bool, video: bool, display: bool) -> bool {
    audio && !video && !display && trusted_microphone_origin(uri)
}

fn resolve_remote_setting(stored: &Mutex<bool>, requested: Option<bool>) -> bool {
    let mut remote = stored.lock().unwrap();
    if let Some(requested) = requested {
        *remote = requested;
    }
    *remote
}

fn update_models_dir(stored: &Mutex<Option<String>>, requested: Option<&str>) {
    if let Some(dir) = requested {
        *stored.lock().unwrap() = (!dir.is_empty()).then(|| dir.to_string());
    }
}

struct ServerState {
    start_gate: server_startup::StartGate,
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
    server_pid: Mutex<Option<u32>>,
    keep_running_on_close: Mutex<bool>,
    remote: Mutex<bool>,
    models_dir: Mutex<Option<String>>,
    /// Override the backend selection: Some("cpu") forces the CPU sidecar even
    /// when GPU binaries exist (solving the Windows catch-22 where an active
    /// .exe cannot be deleted), while Some("cuda")/Some("rocm") pin a specific
    /// GPU variant when more than one is installed. None uses the on-disk
    /// default (ROCm preferred, then CUDA). Persisted to disk so the choice
    /// survives an app restart.
    backend_override: Mutex<Option<String>>,
}

fn backend_override_file(data_dir: &std::path::Path) -> std::path::PathBuf {
    data_dir.join("backend_override")
}

fn read_persisted_backend_override(data_dir: &std::path::Path) -> Option<String> {
    std::fs::read_to_string(backend_override_file(data_dir))
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn write_persisted_backend_override(data_dir: &std::path::Path, value: Option<&str>) {
    let path = backend_override_file(data_dir);
    match value {
        Some(v) => {
            let _ = std::fs::create_dir_all(data_dir);
            if let Err(e) = std::fs::write(&path, v) {
                println!("Failed to persist backend override: {}", e);
            }
        }
        None => {
            let _ = std::fs::remove_file(&path);
        }
    }
}

/// Run `<exe> --version` with a 10-second timeout to avoid hanging Tauri startup.
/// Returns a version only for a successful process with one Voicebox version line.
async fn probe_binary_version(exe: &std::path::Path, cwd: &std::path::Path) -> Option<String> {
    let mut cmd = tokio::process::Command::new(exe);
    cmd.arg("--version")
        .current_dir(cwd)
        .kill_on_drop(true);

    match tokio::time::timeout(std::time::Duration::from_secs(10), cmd.output()).await {
        Ok(Ok(output)) => {
            if !output.status.success() {
                return None;
            }
            let stdout = std::str::from_utf8(&output.stdout).ok()?;
            let mut versions = stdout.lines().filter_map(|line| {
                let mut words = line.split_whitespace();
                match (words.next(), words.next(), words.next()) {
                    (Some("voicebox-server"), Some(version), None) => Some(version),
                    _ => None,
                }
            });
            let version = versions.next()?;
            if versions.next().is_some() {
                return None;
            }
            Some(version.to_owned())
        }
        Ok(Err(e)) => {
            println!("Version probe failed: {}", e);
            None
        }
        Err(_) => {
            println!("Version probe timed out after 10s");
            None
        }
    }
}

#[command]
async fn start_server(
    app: tauri::AppHandle,
    state: State<'_, ServerState>,
    remote: Option<bool>,
    models_dir: Option<String>,
) -> Result<String, String> {
    let _start_permit = state.start_gate.try_begin().ok_or("The bundled server is still starting. Please try again shortly.")?;
    let is_remote = resolve_remote_setting(&state.remote, remote);
    // Store models_dir for use on restart (empty string means reset to default)
    update_models_dir(&state.models_dir, models_dir.as_deref());
    // Check if server is already running (managed by this app instance)
    let has_managed_child = state.child.lock().unwrap().is_some();
    if has_managed_child {
        let ready = tokio::task::spawn_blocking(|| check_health(SERVER_PORT))
            .await
            .unwrap_or(false);
        if ready {
            _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
            return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
        }
        return Err("The bundled server is still starting. Please try again shortly.".to_string());
    }

    // Check if a voicebox server is already running on our port (from previous session with keep_running=true,
    // or an externally started server e.g. via `python`, `uvicorn`, Docker, etc.)
    #[cfg(unix)]
    {
        use std::process::Command;
        if let Ok(output) = Command::new("lsof")
            .args(["+c", "0", "-i", &format!(":{}", SERVER_PORT), "-sTCP:LISTEN"])
            .output()
        {
            let output_str = String::from_utf8_lossy(&output.stdout);
            let healthy = output_str.lines().skip(1).any(|line| !line.trim().is_empty())
                && check_health(SERVER_PORT);
            match server_process::classify_listener(&output_str, healthy) {
                server_process::ListenerDecision::Unoccupied => {}
                server_process::ListenerDecision::ReuseOwned(pid) => {
                    #[cfg(target_os = "linux")]
                    if !server_process::linux_server_executable(pid) {
                        println!("Healthy server has an unverified executable; reusing it as external");
                        _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
                        return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
                    }
                    println!("Found healthy voicebox-server on port {} (PID: {}), reusing it", SERVER_PORT, pid);
                    _start_permit.commit(|| *state.server_pid.lock().unwrap() = Some(pid))
                        .ok_or("Server startup cancelled")?;
                    return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
                }
                server_process::ListenerDecision::ReuseExternal => {
                    println!("Health check passed — reusing external server on port {}", SERVER_PORT);
                    _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
                    return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
                }
                server_process::ListenerDecision::Conflict(command) => {
                    return Err(format!(
                        "Port {} is already in use by another application ({}). \
                         Close it or change the Voicebox server port.",
                        SERVER_PORT, command
                    ));
                }
            }
        }
    }
    
    #[cfg(windows)]
    {
        use std::net::TcpStream;
        if TcpStream::connect_timeout(
            &format!("127.0.0.1:{}", SERVER_PORT).parse().unwrap(),
            std::time::Duration::from_secs(1),
        ).is_ok() {
            if check_health(SERVER_PORT) {
                if let Some(pid) = find_voicebox_pid_on_port(SERVER_PORT) {
                    println!("Found healthy voicebox-server on port {} (PID: {}), reusing it", SERVER_PORT, pid);
                    _start_permit.commit(|| *state.server_pid.lock().unwrap() = Some(pid))
                        .ok_or("Server startup cancelled")?;
                } else {
                    println!("Health check passed — reusing external server on port {}", SERVER_PORT);
                    _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
                }
                return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
            }
            return Err(format!(
                "Port {} is already in use by another application. \
                 Close the other application or change the Voicebox port.",
                SERVER_PORT
            ));
        }
    }

    // Kill any orphaned voicebox-server from previous session on legacy port 8000
    // This handles upgrades from older versions that used a fixed port
    #[cfg(unix)]
    {
        use std::process::Command;
        if let Ok(output) = Command::new("lsof")
            .args(["+c", "0", "-i", &format!(":{}", LEGACY_PORT), "-sTCP:LISTEN"])
            .output()
        {
            let output_str = String::from_utf8_lossy(&output.stdout);
            let healthy = output_str.lines().skip(1).any(|line| !line.trim().is_empty())
                && check_health(LEGACY_PORT);
            for pid in server_process::legacy_pids(&output_str, healthy) {
                #[cfg(target_os = "linux")]
                if !server_process::linux_server_executable(pid) {
                    continue;
                }
                _start_permit.commit(|| {
                    println!("Found healthy voicebox-server on legacy port {} (PID: {}), stopping it...", LEGACY_PORT, pid);
                    let _ = Command::new("kill")
                        .args(["-9", "--", &format!("-{}", pid)])
                        .output();
                    let _ = Command::new("kill")
                        .args(["-9", &pid.to_string()])
                        .output();
                }).ok_or("Server startup cancelled")?;
            }
        }
    }
    
    #[cfg(windows)]
    {
        use std::net::TcpStream;
        if TcpStream::connect_timeout(
            &format!("127.0.0.1:{}", LEGACY_PORT).parse().unwrap(),
            std::time::Duration::from_secs(1),
        ).is_ok() && check_health(LEGACY_PORT) {
            if let Some(pid) = find_voicebox_pid_on_port(LEGACY_PORT) {
                _start_permit.commit(|| {
                    println!("Found orphaned voicebox-server on legacy port {} (PID: {}), killing it...", LEGACY_PORT, pid);
                    let _ = std::process::Command::new("taskkill")
                        .args(["/PID", &pid.to_string(), "/T", "/F"])
                        .output();
                }).ok_or("Server startup cancelled")?;
            }
        }
    }
    
    // Brief wait for port to be released
    std::thread::sleep(std::time::Duration::from_millis(200));

    // Get app data directory
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("Failed to get app data dir: {}", e))?;

    // Ensure data directory exists
    std::fs::create_dir_all(&data_dir)
        .map_err(|e| format!("Failed to create data dir: {}", e))?;

    println!("=================================================================");
    println!("Starting voicebox-server sidecar");
    println!("Data directory: {:?}", data_dir);
    println!("Remote mode: {}", is_remote);

    // Check for ROCm backend in data directory (onedir layout: backends/rocm/)
    let rocm_binary = {
        let rocm_dir = data_dir.join("backends").join("rocm");
        let rocm_name = if cfg!(windows) {
            "voicebox-server-rocm.exe"
        } else {
            "voicebox-server-rocm"
        };
        let exe_path = rocm_dir.join(rocm_name);
        if exe_path.exists() {
            println!("Found ROCm backend at {:?}", rocm_dir);

            let app_version = app.config().version.clone().unwrap_or_default();
            let binary_version = probe_binary_version(&exe_path, &rocm_dir).await;
            let version_ok = if !app_version.is_empty()
                && binary_version.as_deref() == Some(app_version.as_str())
            {
                println!("ROCm binary version {} matches app version", app_version);
                true
            } else {
                println!(
                    "ROCm binary version mismatch: binary={}, app={}. Falling back to CPU.",
                    binary_version.as_deref().unwrap_or("<unknown>"),
                    app_version
                );
                false
            };

            if version_ok {
                Some(exe_path)
            } else {
                None
            }
        } else {
            println!("No ROCm backend found");
            None
        }
    };

    // Check for CUDA backend in data directory (onedir layout: backends/cuda/)
    let cuda_binary = {
        let cuda_dir = data_dir.join("backends").join("cuda");
        let cuda_name = if cfg!(windows) {
            "voicebox-server-cuda.exe"
        } else {
            "voicebox-server-cuda"
        };
        let exe_path = cuda_dir.join(cuda_name);
        if exe_path.exists() {
            println!("Found CUDA backend at {:?}", cuda_dir);

            // Version check: run --version from the onedir directory so
            // PyInstaller can find its support files for the fast --version path
            let app_version = app.config().version.clone().unwrap_or_default();
            let binary_version = probe_binary_version(&exe_path, &cuda_dir).await;
            let version_ok = if !app_version.is_empty()
                && binary_version.as_deref() == Some(app_version.as_str())
            {
                println!("CUDA binary version {} matches app version", app_version);
                true
            } else {
                println!(
                    "CUDA binary version mismatch: binary={}, app={}. Falling back to CPU.",
                    binary_version.as_deref().unwrap_or("<unknown>"),
                    app_version
                );
                false
            };

            if version_ok {
                Some(exe_path)
            } else {
                None
            }
        } else {
            println!("No CUDA backend found, using bundled CPU binary");
            None
        }
    };

    let sidecar_result = app.shell().sidecar("voicebox-server");

    let mut sidecar = match sidecar_result {
        Ok(s) => s,
        Err(e) => {
            eprintln!("Failed to get sidecar: {}", e);

            // In dev mode, check if the server is already running (started manually)
            #[cfg(debug_assertions)]
            {
                eprintln!("Dev mode: Checking if server is already running on port {}...", SERVER_PORT);

                // Try to connect to the server port
                if manual_server_available(SERVER_PORT).await {
                    println!("Found server already running on port {}", SERVER_PORT);
                    _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
                    return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
                }

                eprintln!("");
                eprintln!("=================================================================");
                eprintln!("DEV MODE: No server found on port {}", SERVER_PORT);
                eprintln!("");
                eprintln!("Start the Python server in a separate terminal:");
                eprintln!("  bun run dev:server");
                eprintln!("=================================================================");
                eprintln!("");
            }

            return Err(format!("Failed to start server. In dev mode, run 'bun run dev:server' in a separate terminal."));
        }
    };

    // macOS ships the large Python runtime as an onedir resource. Launch it
    // beside its support files instead of extracting a onefile archive on
    // every launch. Older bundles and development stubs retain the sidecar.
    #[cfg(target_os = "macos")]
    {
        let backend_dir = app
            .path()
            .resource_dir()
            .map_err(|e| format!("Failed to locate bundled backend: {}", e))?
            .join("backend");
        let backend_exe = backend_dir.join("voicebox-server");
        if backend_exe.is_file() {
            println!("Using unpacked macOS backend: {:?}", backend_exe);
            sidecar = app.shell().command(&backend_exe).current_dir(&backend_dir);
        }
    }

    println!("Sidecar command created successfully");

    // Build common args
    let data_dir_str = data_dir
        .to_str()
        .ok_or_else(|| "Invalid data dir path".to_string())?
        .to_string();
    let server_args = server_arguments(&data_dir_str, SERVER_PORT, std::process::id(), is_remote);

    // Resolve the custom models directory from the parameter or stored state
    let effective_models_dir = models_dir.or_else(|| state.models_dir.lock().unwrap().clone());
    if let Some(ref dir) = effective_models_dir {
        println!("Custom models directory: {}", dir);
    }

    // Respect backend override (e.g., user wants CPU even though a GPU binary
    // exists, or pinned a specific GPU variant). The in-memory value resets to
    // None on app launch, so fall back to the persisted choice on disk.
    let backend_override = {
        let in_memory = state.backend_override.lock().unwrap().clone();
        in_memory.or_else(|| read_persisted_backend_override(&data_dir))
    };

    // Honor a pinned GPU variant by ignoring the other one — but only when the
    // pinned variant is actually installed, so a stale pin to a deleted backend
    // self-heals to the default order instead of forcing CPU. With no pin, both
    // stay eligible and the launch order below prefers ROCm, then CUDA.
    let (rocm_binary, cuda_binary) = eligible_gpu_backends(
        backend_override.as_deref(), rocm_binary, cuda_binary,
    );

    // If ROCm binary exists, launch it from the onedir directory.
    // If CUDA binary exists, launch it from the onedir directory.
    // .current_dir() is critical: PyInstaller onedir expects all DLLs and
    // support files relative to the exe.
    let spawn_result = _start_permit.commit(|| {
        let result = if backend_override.as_deref() != Some("cpu") {
            let mut gpu_spawn = None;

            if let Some(ref rocm_path) = rocm_binary {
                let rocm_dir = rocm_path.parent().unwrap();
                println!("Launching ROCm backend: {:?} (cwd: {:?})", rocm_path, rocm_dir);
                let mut cmd = app.shell().command(rocm_path.to_str().unwrap());
                cmd = cmd.current_dir(rocm_dir);
                cmd = cmd.args(&server_args);
                if let Some(ref dir) = effective_models_dir { cmd = cmd.env("VOICEBOX_MODELS_DIR", dir); }
                match cmd.spawn() {
                    Ok(r) => { gpu_spawn = Some(Ok(r)); }
                    Err(e) => { println!("ROCm spawn failed ({}), trying CUDA/CPU fallback", e); }
                }
            }

            if gpu_spawn.is_none() {
                if let Some(ref cuda_path) = cuda_binary {
                    let cuda_dir = cuda_path.parent().unwrap();
                    println!("Launching CUDA backend: {:?} (cwd: {:?})", cuda_path, cuda_dir);
                    let mut cmd = app.shell().command(cuda_path.to_str().unwrap());
                    cmd = cmd.current_dir(cuda_dir);
                    cmd = cmd.args(&server_args);
                    if let Some(ref dir) = effective_models_dir { cmd = cmd.env("VOICEBOX_MODELS_DIR", dir); }
                    match cmd.spawn() {
                        Ok(r) => { gpu_spawn = Some(Ok(r)); }
                        Err(e) => { println!("CUDA spawn failed ({}), falling back to CPU", e); }
                    }
                }
            }

            if let Some(result) = gpu_spawn {
                result
            } else {
                // Fall back to bundled CPU sidecar
                sidecar = sidecar.args(&server_args);
                if let Some(ref dir) = effective_models_dir { sidecar = sidecar.env("VOICEBOX_MODELS_DIR", dir); }
                println!("Spawning bundled CPU server process...");
                sidecar.spawn()
            }
        } else {
            // Override forces CPU — use bundled sidecar, GPU binary stays on disk
            println!("Backend override=cpu: using bundled CPU sidecar");
            sidecar = sidecar.args(&server_args);
            if let Some(ref dir) = effective_models_dir {
                sidecar = sidecar.env("VOICEBOX_MODELS_DIR", dir);
            }
            sidecar.spawn()
        };
        result.map(|(rx, child)| {
            let process_pid = child.pid();
            let mut pid = state.server_pid.lock().unwrap();
            let mut managed_child = state.child.lock().unwrap();
            *pid = Some(process_pid);
            *managed_child = Some(child);
            (rx, process_pid)
        })
    }).ok_or("Server startup cancelled")?;

    let (mut rx, process_pid) = match spawn_result {
        Ok(result) => result,
        Err(e) => {
            eprintln!("Failed to spawn server process: {}", e);

            // In dev mode, check if a manually-started server is available
            #[cfg(debug_assertions)]
            {
                if manual_server_available(SERVER_PORT).await {
                    println!("Found manually-started server on port {}", SERVER_PORT);
                    _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
                    return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
                }

                eprintln!("");
                eprintln!("=================================================================");
                eprintln!("DEV MODE: Server binary failed to start");
                eprintln!("");
                eprintln!("Start the Python server in a separate terminal:");
                eprintln!("  bun run dev:server");
                eprintln!("=================================================================");
                eprintln!("");
                return Err("Dev mode: Start server manually with 'bun run dev:server'".to_string());
            }

            #[cfg(not(debug_assertions))]
            {
                eprintln!("This could be due to:");
                eprintln!("  - Missing or corrupted binary");
                eprintln!("  - Missing execute permissions");
                eprintln!("  - Code signing issues on macOS");
                eprintln!("  - Missing dependencies");
                return Err(format!("Failed to spawn: {}", e));
            }
        }
    };

    println!("Server process spawned, waiting for ready signal...");
    println!("=================================================================");

    let startup = server_startup::wait_for_startup(&mut rx, std::time::Duration::from_secs(120), |stream, line| {
        if stream == "stderr" { eprintln!("Server: {line}"); } else { println!("Server output: {line}"); }
        let _ = app.emit("server-log", serde_json::json!({ "stream": stream, "line": line }));
    }).await;
    match startup {
        server_startup::Outcome::Ready => println!("Server is ready!"),
        server_startup::Outcome::TimedOut(errors) => {
            eprintln!("Server startup timeout after 120 seconds");
            for line in errors { eprintln!("  {line}"); }
            #[cfg(debug_assertions)]
            let healthy = manual_server_available(SERVER_PORT).await;
            #[cfg(not(debug_assertions))]
            let healthy = false;
            if healthy {
                println!("Found healthy server on port {}", SERVER_PORT);
            } else {
                stop_server_if_owned(state.clone(), Some(process_pid)).await?;
                return Err("Server startup timed out. Please retry; check Console.app if the problem persists.".to_string());
            }
        }
        server_startup::Outcome::Exited => {
            server_process::take_process(&state.server_pid, &state.child, Some(process_pid));
            #[cfg(debug_assertions)]
            {
                if manual_server_available(SERVER_PORT).await {
                    println!("Found manually-started server on port {}", SERVER_PORT);
                    _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
                    return Ok(format!("http://127.0.0.1:{}", SERVER_PORT));
                }
                return Err("Dev mode: Start server manually with 'bun run dev:server'".to_string());
            }
            #[cfg(not(debug_assertions))]
            return Err("Server process ended unexpectedly".to_string());
        }
    }

    _start_permit.commit(|| ()).ok_or("Server startup cancelled")?;
    // Spawn task to continue reading output and emit to frontend
    let app_handle = app.clone();
    tokio::spawn(async move {
        server_startup::forward_events(&mut rx, |stream, line| {
            if stream == "stderr" { eprintln!("Server error: {line}"); } else { println!("Server: {line}"); }
            let _ = app_handle.emit("server-log", serde_json::json!({ "stream": stream, "line": line }));
        }, || {
            let state = app_handle.state::<ServerState>();
            server_process::take_process(&state.server_pid, &state.child, Some(process_pid));
        }).await;
    });

    Ok(format!("http://127.0.0.1:{}", SERVER_PORT))
}

#[command]
async fn stop_server(state: State<'_, ServerState>) -> Result<(), String> {
    stop_server_if_owned(state, None).await
}

async fn stop_server_if_owned(
    state: State<'_, ServerState>,
    expected: Option<u32>,
) -> Result<(), String> {
    let process = if expected.is_none() {
        state.start_gate.cancel_and(|| server_process::take_process(&state.server_pid, &state.child, expected))
    } else {
        server_process::take_process(&state.server_pid, &state.child, expected)
    };
    if let Some((pid, child)) = process {
        tokio::task::spawn_blocking(move || {
            let _child = child;
            println!("stop_server: Stopping server with PID: {}", pid);

            #[cfg(unix)]
            {
                use std::process::Command;
                // Kill process group with SIGTERM first
                let _ = Command::new("kill")
                    .args(["-TERM", "--", &format!("-{}", pid)])
                    .output();

                // Brief wait then force kill
                std::thread::sleep(std::time::Duration::from_millis(100));

                let _ = Command::new("kill")
                    .args(["-9", "--", &format!("-{}", pid)])
                    .output();
                let _ = Command::new("kill").args(["-9", &pid.to_string()]).output();

                println!("stop_server: Process group kill completed");
            }

            #[cfg(windows)]
            {
                // Terminate the captured process tree, never a replacement
                // server that may have acquired the fixed HTTP port.
                let _ = std::process::Command::new("taskkill")
                    .args(["/PID", &pid.to_string(), "/T", "/F"])
                    .output();
            }
        })
        .await
        .map_err(|error| format!("Failed to stop server: {}", error))?;
    }
    Ok(())
}

#[command]
async fn restart_server(
    app: tauri::AppHandle,
    state: State<'_, ServerState>,
    models_dir: Option<String>,
) -> Result<String, String> {
    println!("restart_server: stopping current server...");

    // Update stored models_dir: empty string means reset to default, non-empty means set
    update_models_dir(&state.models_dir, models_dir.as_deref());

    // Stop the current server
    stop_server(state.clone()).await?;

    // Wait for port to be released
    println!("restart_server: waiting for port release...");
    tokio::time::sleep(tokio::time::Duration::from_millis(1000)).await;

    // Start server again (will auto-detect GPU binary and use stored models_dir)
    println!("restart_server: starting server...");
    start_server(app, state.clone(), None, None).await
}

#[command]
fn set_keep_server_running(state: State<'_, ServerState>, keep_running: bool) {
    println!("set_keep_server_running called with: {}", keep_running);
    *state.keep_running_on_close.lock().unwrap() = keep_running;
}

#[command]
fn set_backend_override(
    app: tauri::AppHandle,
    state: State<'_, ServerState>,
    backend: Option<String>,
) {
    println!("set_backend_override called with: {:?}", backend);
    if let Ok(data_dir) = app.path().app_data_dir() {
        write_persisted_backend_override(&data_dir, backend.as_deref());
    }
    *state.backend_override.lock().unwrap() = backend;
}

#[command]
async fn start_system_audio_capture(
    state: State<'_, audio_capture::AudioCaptureState>,
    max_duration_secs: u32,
) -> Result<(), String> {
    audio_capture::start_capture(&state, max_duration_secs).await
}

#[command]
async fn stop_system_audio_capture(
    state: State<'_, audio_capture::AudioCaptureState>,
) -> Result<String, String> {
    audio_capture::stop_capture(&state).await
}

#[command]
fn is_system_audio_supported() -> bool {
    audio_capture::is_supported()
}

#[command]
fn list_audio_output_devices(
    state: State<'_, audio_output::AudioOutputState>,
) -> Result<Vec<audio_output::AudioOutputDevice>, String> {
    state.list_output_devices()
}

#[command]
async fn play_audio_to_devices(
    app: tauri::AppHandle,
    state: State<'_, audio_output::AudioOutputState>,
    request: tauri::ipc::Request<'_>,
) -> Result<audio_output::PlaybackSession, String> {
    let (audio_data, device_ids, options) = audio_output::read_playback_request(
        request.body(),
        request
            .headers()
            .get("x-voicebox-playback")
            .and_then(|value| value.to_str().ok()),
    )?;
    state
        .play_audio_to_devices(app, audio_data, device_ids, options)
        .await
}

#[command]
async fn start_audio_pcm_stream(
    app: tauri::AppHandle,
    state: State<'_, audio_output::AudioOutputState>,
    sample_rate: u32,
    device_ids: Vec<String>,
) -> Result<audio_output::PlaybackSession, String> {
    state.start_pcm_stream(app, sample_rate, device_ids).await
}

#[command]
async fn append_audio_pcm_stream(
    state: State<'_, audio_output::AudioOutputState>,
    request: tauri::ipc::Request<'_>,
) -> Result<(), String> {
    let id = request
        .headers()
        .get("x-voicebox-session")
        .and_then(|value| value.to_str().ok())
        .filter(|value| value.len() <= 128)
        .ok_or("Missing preview session")?
        .to_owned();
    let tauri::ipc::InvokeBody::Raw(pcm) = request.body() else {
        return Err("Preview requires binary PCM".into());
    };
    state.append_pcm_stream(&id, pcm.clone()).await
}

#[command]
fn finish_audio_pcm_stream(
    state: State<'_, audio_output::AudioOutputState>,
    session_id: String,
) -> Result<(), String> {
    state.finish_pcm_stream(&session_id)
}

#[command]
fn control_audio_playback(
    state: State<'_, audio_output::AudioOutputState>,
    session_id: String,
    control: audio_output::PlaybackControl,
) -> Result<(), String> {
    state.control_playback(&session_id, control)
}

#[command]
fn stop_audio_playback(
    state: State<'_, audio_output::AudioOutputState>,
    session_id: Option<String>,
) -> Result<(), String> {
    state.stop_playback(session_id.as_deref())
}

/// Identifier of the Voicebox app itself — used to short-circuit auto-paste
/// when the user fires a chord while focus was inside one of our own
/// windows. Paste into Voicebox-internal targets is step 6 territory and
/// goes through a different (JS-side) injection path.
///
/// Value matches what `focus_capture::capture_focus` writes into
/// `FocusSnapshot::bundle_id` on the current platform — reverse-DNS bundle
/// id on macOS, lowercased exe basename on Windows/Linux.
#[cfg(target_os = "macos")]
const VOICEBOX_BUNDLE_ID: &str = "sh.voicebox.app";
#[cfg(target_os = "windows")]
const VOICEBOX_BUNDLE_ID: &str = "voicebox.exe";
#[cfg(not(any(target_os = "macos", target_os = "windows")))]
const VOICEBOX_BUNDLE_ID: &str = "voicebox";

/// Milliseconds to wait between activating the target app and firing the
/// synthetic ⌘V, giving AppKit time to finish re-ordering windows and
/// restoring its last-focused field.
const POST_ACTIVATE_SETTLE_MS: u64 = 120;

/// Milliseconds the staged text lives on the clipboard after the paste
/// keystroke, before we restore the user's original clipboard contents.
/// Too short and slow apps haven't consumed the paste yet; too long and
/// the user sees our text if they look at their clipboard manager.
const PASTE_CONSUME_MS: u64 = 400;

/// Reports whether the process currently has macOS Accessibility trust.
/// Used by the settings UI and the paste debug harness to decide whether
/// synthetic key events will actually land.
#[command]
fn check_accessibility_permission() -> bool {
    accessibility::is_trusted()
}

/// Reports whether the process can observe global keyboard events. Read by
/// the Captures settings UI to surface a "missing — open Settings" hint
/// beside the hotkey toggle. No prompt side-effect.
#[command]
fn check_input_monitoring_permission() -> bool {
    input_monitoring::is_trusted()
}

/// Holds the lazily-spawned global hotkey monitor. The monitor is `None`
/// until the user opts in via the Captures settings toggle — that opt-in is
/// what triggers the macOS Input Monitoring TCC prompt, so a fresh-install
/// user who never enables the hotkey never sees the prompt.
///
/// Disabling the hotkey clears the monitor's internal `ChordMatcher` so
/// keytap's event tap is released while Tauri still owns this `HotkeyState`
/// for the rest of the process. A subsequent enable re-arms without
/// re-prompting for the Input Monitoring permission.
#[cfg(desktop)]
#[derive(Default)]
pub struct HotkeyState {
    monitor: Mutex<Option<hotkey_monitor::HotkeyMonitor>>,
}

#[cfg(desktop)]
fn build_chord_bindings(
    push_to_talk: &[String],
    toggle_to_talk: &[String],
) -> Result<hotkey_monitor::Bindings, String> {
    use hotkey_monitor::{Bindings, ChordAction};
    use keytap::Key;
    use std::collections::HashSet;

    fn build_chord(name: &str, names: &[String]) -> Result<HashSet<Key>, String> {
        if names.is_empty() {
            return Err(format!("{name} chord must have at least one key"));
        }
        let mut chord = HashSet::new();
        for raw in names {
            let key = key_codes::key_from_str(raw)
                .ok_or_else(|| format!("Unsupported key in {name} chord: {raw}"))?;
            chord.insert(key);
        }
        Ok(chord)
    }

    let push_chord = build_chord("push-to-talk", push_to_talk)?;
    let toggle_chord = build_chord("toggle-to-talk", toggle_to_talk)?;
    if push_chord == toggle_chord {
        return Err("Push-to-talk and toggle-to-talk chords must be different".to_string());
    }

    let mut bindings = Bindings::new();
    bindings.insert(ChordAction::PushToTalk, push_chord);
    bindings.insert(ChordAction::ToggleToTalk, toggle_chord);
    Ok(bindings)
}

/// Spawn the global hotkey monitor on first call; subsequent calls just push
/// the new bindings into the existing monitor. Idempotent on purpose — the
/// frontend invokes this both at startup (when `capture_settings.hotkey_enabled`
/// is true) and from the settings toggle.
///
/// On macOS this is the call that triggers the "Voicebox would like to receive
/// keystrokes from any application" TCC prompt, since keytap's `Tap` creates
/// the CGEventTap inside `HotkeyMonitor::spawn`.
#[cfg(desktop)]
#[command]
fn enable_hotkey(
    app: tauri::AppHandle,
    state: State<'_, HotkeyState>,
    push_to_talk: Vec<String>,
    toggle_to_talk: Vec<String>,
) -> Result<(), String> {
    let bindings = build_chord_bindings(&push_to_talk, &toggle_to_talk)?;

    // Fire the Input Monitoring TCC prompt explicitly from the user's
    // toggle click, before keytap's Tap would do it implicitly via
    // CGEventTap creation. Two reasons: (1) the prompt timing becomes
    // deterministic — it appears in response to a click instead of as a
    // mysterious side-effect of "the app started"; (2) on subsequent
    // launches we can short-circuit the spawn entirely if the user
    // revoked the grant, instead of relying on the tap silently failing.
    // The call returns the current grant state; we ignore it because
    // keytap surfaces its own error via stderr, and the settings UI
    // polls `check_input_monitoring_permission` separately.
    let _ = input_monitoring::request();

    // The dictate pill webview must exist before the first chord fires so it
    // can subscribe to `dictate:start`. Build it here (idempotent — Tauri
    // returns the existing window when one with this label already exists).
    if app.get_webview_window(DICTATE_WINDOW_LABEL).is_none() {
        if let Err(e) = build_dictate_window(&app) {
            eprintln!("Failed to build dictate window: {}", e);
        }
    }

    let mut slot = state.monitor.lock().map_err(|e| e.to_string())?;
    match slot.as_mut() {
        Some(monitor) => monitor.update_bindings(bindings),
        None => {
            *slot = Some(hotkey_monitor::HotkeyMonitor::spawn(app, bindings));
        }
    }
    Ok(())
}

/// Quiet the global hotkey. Tears down the `ChordMatcher` (which stops
/// keytap's chord worker and closes the OS event tap) but keeps the
/// `HotkeyMonitor` handle around so a subsequent `enable_hotkey` re-arms
/// without re-prompting for Input Monitoring permission.
#[cfg(desktop)]
#[command]
fn disable_hotkey(state: State<'_, HotkeyState>) -> Result<(), String> {
    let mut slot = state.monitor.lock().map_err(|e| e.to_string())?;
    if let Some(monitor) = slot.as_mut() {
        monitor.update_bindings(hotkey_monitor::Bindings::new());
    }
    Ok(())
}

/// Push a new chord configuration into the running `HotkeyMonitor`. Called
/// by the chord-picker UI when the user edits the chord. No-ops when the
/// monitor isn't spawned — the picker is gated behind the enable toggle, so
/// this can only happen if the frontend races; the next `enable_hotkey` will
/// pick up the saved chords.
///
/// Returns an error when a key name doesn't map to a `keytap::Key`, so the
/// picker UI can surface "this key isn't supported" instead of silently
/// dropping it from the chord.
#[cfg(desktop)]
#[command]
fn update_chord_bindings(
    state: State<'_, HotkeyState>,
    push_to_talk: Vec<String>,
    toggle_to_talk: Vec<String>,
) -> Result<(), String> {
    let bindings = build_chord_bindings(&push_to_talk, &toggle_to_talk)?;
    let mut slot = state.monitor.lock().map_err(|e| e.to_string())?;
    if let Some(monitor) = slot.as_mut() {
        monitor.update_bindings(bindings);
    }
    Ok(())
}

/// Open the Privacy & Security → Accessibility pane in System Settings so
/// the user can grant the permission. The URL scheme is stable across
/// macOS 10.14–15; no-op on other platforms.
#[command]
fn open_accessibility_settings(app: tauri::AppHandle) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let url = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility";
        app.shell()
            .open(url, None)
            .map_err(|e| format!("Failed to open Accessibility settings: {e}"))?;
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err("Accessibility settings pane is only implemented on macOS".into())
    }
}

/// Open the Privacy & Security → Input Monitoring pane in System Settings.
/// Used by the Captures settings UI when the toggle is on but the grant
/// is missing, so the user can flip the system toggle without hunting.
#[command]
fn open_input_monitoring_settings(app: tauri::AppHandle) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let url = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent";
        app.shell()
            .open(url, None)
            .map_err(|e| format!("Failed to open Input Monitoring settings: {e}"))?;
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err("Input Monitoring settings pane is only implemented on macOS".into())
    }
}

/// Deliver `text` into the UI that had focus when the chord fired.
///
/// Pipeline: activate the captured PID → settle → save the user's
/// clipboard → write `text` → fire ⌘V → wait for the target to consume it
/// → conditionally restore the original clipboard.
///
/// The restore is conditional on `NSPasteboard.changeCount` (or the
/// Windows sequence number) matching the value captured right after
/// `write_text`: if something else wrote to the clipboard during the
/// paste-consume window — the user's own ⌘C in the target app, a
/// clipboard history tool (Paste, Pastebot, Maccy), Universal Clipboard
/// sync, 1Password inserting a secret — their newer content takes
/// priority over our snapshot and is preserved. A
/// [`clipboard::current_change_count`] read failure is treated the same
/// way: unknown state is safer than an unconditional overwrite.
///
/// `send_paste` failure is isolated from the restore decision: we always
/// attempt the conditional restore before propagating the paste error,
/// so a failed `CGEventPost` / `SendInput` never leaves the user's
/// clipboard stuck on the transcript.
///
/// Skips (returns `false`) without touching anything when:
/// - `focus.bundle_id` is Voicebox itself — step 6 will inject directly
///   into our own webview; pasting would just double-insert or miss the
///   real target.
/// - Accessibility is not trusted — `CGEventPost` would silently drop the
///   keystroke, leaving the user's clipboard clobbered with nothing to
///   show for it.
///
/// Returns `true` when the paste sequence completed end-to-end.
#[command]
async fn paste_final_text(
    text: String,
    focus: focus_capture::FocusSnapshot,
) -> Result<bool, String> {
    paste_pipeline::paste_final_text(&text, &focus, &paste_pipeline::NativePaste, tokio::time::sleep).await
}

/// Inspect the currently focused UI element. Returns the owning app's PID,
/// bundle id, and AX role. Useful for sanity-checking the focus pipeline
/// before committing to a paste.
#[command]
fn debug_capture_focus() -> Result<focus_capture::FocusSnapshot, String> {
    focus_capture::capture_focus()
}

/// Full auto-paste rehearsal: snapshot the focus target now, sleep
/// `drift_ms` so the user can deliberately switch to a different app
/// (proving we don't paste into whichever window is frontmost when the
/// transcribe finishes), then activate the captured PID, stage `text`,
/// fire ⌘V, and restore the clipboard.
#[command]
async fn debug_focus_roundtrip(
    text: String,
    drift_ms: u64,
    post_paste_delay_ms: u64,
) -> Result<serde_json::Value, String> {
    if !accessibility::is_trusted() {
        return Err(
            "Accessibility permission not granted. Open System Settings → Privacy & Security → Accessibility and enable Voicebox."
                .into(),
        );
    }

    let snapshot = focus_capture::capture_focus()?;

    tokio::time::sleep(std::time::Duration::from_millis(drift_ms)).await;

    focus_capture::activate_pid(snapshot.pid)?;
    // Give AppKit a beat to process the activation before the synthetic
    // Cmd+V arrives — without this the paste sometimes races ahead of the
    // window-ordering animation and lands in the previous frontmost app.
    tokio::time::sleep(std::time::Duration::from_millis(120)).await;

    let clip = clipboard::save_clipboard()?;
    let after_write = clipboard::write_text(&text)?;
    synthetic_keys::send_paste()?;
    tokio::time::sleep(std::time::Duration::from_millis(post_paste_delay_ms)).await;
    let before_restore = clipboard::current_change_count()?;
    clipboard::restore_clipboard(&clip)?;

    Ok(serde_json::json!({
        "focus": snapshot,
        "change_count_after_write": after_write,
        "change_count_before_restore": before_restore,
        "clobbered_during_paste": before_restore != after_write,
    }))
}

/// End-to-end smoke test for the auto-paste pipeline: save the user's
/// clipboard, stage `text`, optionally wait `pre_paste_delay_ms` so the
/// caller has time to focus the target app, synthesise ⌘V, wait
/// `post_paste_delay_ms` for the target app to consume the event, and put
/// the original clipboard back.
///
/// Short-circuits when Accessibility permission is missing — without it
/// `CGEventPost` silently drops events, so running the full sequence
/// would just clobber the clipboard with nothing to show for it.
#[command]
async fn debug_paste_text(
    text: String,
    pre_paste_delay_ms: u64,
    post_paste_delay_ms: u64,
) -> Result<serde_json::Value, String> {
    if !accessibility::is_trusted() {
        return Err(
            "Accessibility permission not granted. Open System Settings → Privacy & Security → Accessibility and enable Voicebox, then try again."
                .into(),
        );
    }

    let snapshot = clipboard::save_clipboard()?;
    let before = snapshot.change_count();
    let after_write = clipboard::write_text(&text)?;

    tokio::time::sleep(std::time::Duration::from_millis(pre_paste_delay_ms)).await;

    synthetic_keys::send_paste()?;

    tokio::time::sleep(std::time::Duration::from_millis(post_paste_delay_ms)).await;

    let before_restore = clipboard::current_change_count()?;
    clipboard::restore_clipboard(&snapshot)?;
    let after_restore = clipboard::current_change_count()?;

    Ok(serde_json::json!({
        "change_count_before": before,
        "change_count_after_write": after_write,
        "change_count_before_restore": before_restore,
        "change_count_after_restore": after_restore,
        "clobbered_during_paste": before_restore != after_write,
    }))
}

/// Manual smoke test for the clipboard snapshot/restore primitives used by
/// the auto-paste pipeline. Stages `text` on the pasteboard, waits
/// `hold_ms` so the caller can ⌘V into another app, then puts the original
/// clipboard contents back. The return value reports the change-count deltas
/// so the harness can verify no third party mutated the clipboard mid-paste.
#[command]
async fn debug_clipboard_roundtrip(
    text: String,
    hold_ms: u64,
) -> Result<serde_json::Value, String> {
    let snapshot = clipboard::save_clipboard()?;
    let before = snapshot.change_count();
    let item_count = snapshot.item_count();
    let after_write = clipboard::write_text(&text)?;

    tokio::time::sleep(std::time::Duration::from_millis(hold_ms)).await;

    let before_restore = clipboard::current_change_count()?;
    clipboard::restore_clipboard(&snapshot)?;
    let after_restore = clipboard::current_change_count()?;

    Ok(serde_json::json!({
        "saved_items": item_count,
        "change_count_before": before,
        "change_count_after_write": after_write,
        "change_count_before_restore": before_restore,
        "change_count_after_restore": after_restore,
        "clobbered_during_hold": before_restore != after_write,
    }))
}

/// Final step of the main-window close flow, after the frontend has had its
/// chance to stop the server.
///
/// On Linux, closing the main window must exit the process: the hidden dictate
/// pill webview is still a window, so Tauri would otherwise keep running with
/// no UI, the backend already stopped, and `speak_monitor` retrying
/// `/events/speak` every 30 s (#1040). On macOS and Windows the window just
/// closes, as before, so "Keep server running" plus the global hotkey keep
/// dictation available without the main window.
fn finish_main_window_close(window: &tauri::Window) {
    if cfg!(target_os = "linux") {
        window.app_handle().exit(0);
    } else {
        window.close().ok();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_shell::init())
        .manage(ServerState {
            start_gate: server_startup::StartGate::default(),
            child: Mutex::new(None),
            server_pid: Mutex::new(None),
            keep_running_on_close: Mutex::new(false),
            remote: Mutex::new(false),
            models_dir: Mutex::new(None),
            backend_override: Mutex::new(None),
        })
        .manage(audio_capture::AudioCaptureState::new())
        .manage(audio_output::AudioOutputState::new())
        .setup(|app| {
            #[cfg(desktop)]
            {
                app.handle().plugin(tauri_plugin_updater::Builder::new().build())?;
                app.handle().plugin(tauri_plugin_process::init())?;

                // Resolve the active keyboard layout's V keycode now, on
                // the main thread, and register an observer for layout
                // changes. The synthetic-paste hot path then only reads an
                // atomic. See keyboard_layout.rs for why this matters
                // (Cmd+V is matched by translated character, not keycode,
                // so QWERTY keycode 9 produces Cmd+. on Dvorak).
                keyboard_layout::init();

                // HotkeyMonitor is spawned lazily via the `enable_hotkey`
                // command — see HotkeyState. The hidden dictate webview is
                // safe to build up front because it does not create the global
                // keyboard tap or trigger the macOS Input Monitoring prompt.
                app.manage(HotkeyState::default());

                // The frontend emits `dictate:hide` whenever the pill cycle
                // finishes (rest-fade → hidden). `hide()` alone has been
                // unreliable for transparent always-on-top windows on macOS
                // — the NSWindow lingers as an invisible click target that
                // steals focus to the Voicebox app when the user clicks
                // where it used to be. Park the window off-screen and mark
                // it click-through as well, so even if `hide()` no-ops the
                // user sees and interacts with nothing.
                let handle_for_hide = app.handle().clone();
                app.handle().listen("dictate:hide", move |_event| {
                    if let Some(window) = handle_for_hide.get_webview_window(DICTATE_WINDOW_LABEL) {
                        // Skip on Linux: aborts if the window was never realized
                        // (see show_dictate_window).
                        #[cfg(not(target_os = "linux"))]
                        let _ = window.set_ignore_cursor_events(true);
                        let _ = window.set_position(PhysicalPosition::new(-10_000, -10_000));
                        let _ = window.hide();
                    }
                });

                // Agent-initiated speech (voicebox_speak over MCP or POST /speak)
                // pops the pill up so the user can see what's coming out of their
                // machine. The `dictate:show` listener is kept for any frontend
                // caller that wants to force-surface the pill directly, but the
                // primary source is `speak_monitor` below — Rust subscribes to
                // the backend /events/speak SSE stream so the pill surfaces even
                // when no JS window is active.
                let handle_for_show = app.handle().clone();
                app.handle().listen("dictate:show", move |_event| {
                    show_dictate_window(&handle_for_show);
                });

                ensure_dictate_window(app.handle());
                speak_monitor::spawn_speak_monitor(app.handle().clone());
            }

            // Hide title bar icon on Windows
            #[cfg(windows)]
            {
                use windows::Win32::Foundation::HWND;
                use windows::Win32::UI::WindowsAndMessaging::{SetClassLongPtrW, GCLP_HICON, GCLP_HICONSM};
                
                if let Some((_, window)) = app.webview_windows().iter().next() {
                    if let Ok(hwnd) = window.hwnd() {
                        let hwnd = HWND(hwnd.0);
                        unsafe {
                            // Set both small and regular icons to NULL to hide the title bar icon
                            SetClassLongPtrW(hwnd, GCLP_HICON, 0);
                            SetClassLongPtrW(hwnd, GCLP_HICONSM, 0);
                        }
                    }
                }
            }

            // Enable microphone access on Linux (WebKitGTK denies getUserMedia by default)
            #[cfg(target_os = "linux")]
            {
                use tauri::Manager;
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.with_webview(|webview| {
                        use webkit2gtk::{WebViewExt, SettingsExt, PermissionRequestExt, UserMediaPermissionRequestExt};
                        use webkit2gtk::glib::prelude::Cast;
                        use webkit2gtk::glib::translate::ToGlibPtr;
                        let wk_webview = webview.inner();

                        // Enable media stream support in WebKitGTK settings
                        if let Some(settings) = WebViewExt::settings(&wk_webview) {
                            settings.set_enable_media_stream(true);
                        }

                        // Auto-grant UserMediaPermissionRequest (microphone access)
                        // Only for trusted local origins (Tauri dev server or custom protocol)
                        wk_webview.connect_permission_request(move |webview, request: &webkit2gtk::PermissionRequest| {
                            if let Some(media) = request.downcast_ref::<webkit2gtk::UserMediaPermissionRequest>() {
                                let uri = WebViewExt::uri(webview).unwrap_or_default();
                                // The high-level bindings do not expose this WebKit query.
                                let display = unsafe {
                                    webkit2gtk::ffi::webkit_user_media_permission_is_for_display_device(media.to_glib_none().0) != 0
                                };
                                if allow_media_request(&uri, media.is_for_audio_device(), media.is_for_video_device(), display) {
                                    request.allow();
                                    return true;
                                }
                                request.deny();
                                return true;
                            }
                            false
                        });
                    });
                }
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            publication_commands::publish_file_bytes,
            publication_commands::publish_file_url,
            start_server,
            stop_server,
            restart_server,
            set_keep_server_running,
            set_backend_override,
            start_system_audio_capture,
            stop_system_audio_capture,
            is_system_audio_supported,
            list_audio_output_devices,
            play_audio_to_devices,
            start_audio_pcm_stream,
            append_audio_pcm_stream,
            finish_audio_pcm_stream,
            stop_audio_playback,
            control_audio_playback,
            debug_clipboard_roundtrip,
            debug_paste_text,
            debug_capture_focus,
            debug_focus_roundtrip,
            check_accessibility_permission,
            check_input_monitoring_permission,
            open_accessibility_settings,
            open_input_monitoring_settings,
            paste_final_text,
            enable_hotkey,
            disable_hotkey,
            update_chord_bindings
        ])
        .on_window_event({
            let closing = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            move |window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // If we're already in the close flow, let it proceed
                if closing.load(std::sync::atomic::Ordering::SeqCst) {
                    return;
                }
                closing.store(true, std::sync::atomic::Ordering::SeqCst);

                // Prevent automatic close so frontend can clean up
                api.prevent_close();

                // Emit event to frontend to check setting and stop server if needed
                let app_handle = window.app_handle();

                let window_for_close = window.clone();
                let closing_for_timeout = closing.clone();
                let (tx, mut rx) = mpsc::unbounded_channel::<()>();
                let listener_id = match window_close::request_cleanup(
                    || window.listen("window-close-allowed", move |_| { let _ = tx.send(()); }),
                    || app_handle.emit("window-close-requested", ()),
                    |id| window.unlisten(id),
                ) {
                    Ok(id) => id,
                    Err(e) => {
                        eprintln!("Failed to emit window-close-requested event: {}", e);
                        finish_main_window_close(window);
                        return;
                    }
                };

                tauri::async_runtime::spawn(async move {
                    tokio::select! {
                        _ = rx.recv() => {
                            println!("Frontend cleanup complete, closing");
                            finish_main_window_close(&window_for_close);
                        }
                        _ = tokio::time::sleep(tokio::time::Duration::from_secs(5)) => {
                            eprintln!("Window close timeout, closing anyway");
                            finish_main_window_close(&window_for_close);
                        }
                    }
                    window_for_close.unlisten(listener_id);
                    closing_for_timeout.store(false, std::sync::atomic::Ordering::SeqCst);
                });
            }
        }})
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            let _ = &app; // used on unix
            match &event {
                RunEvent::Exit => {
                    let state = app.state::<ServerState>();
                    let keep_running = *state.keep_running_on_close.lock().unwrap();
                    let has_pid = state.server_pid.lock().unwrap().is_some();
                    println!("RunEvent::Exit — keep_running={}, has_pid={}", keep_running, has_pid);

                    if keep_running {
                        // Tell the server to disable its watchdog so it survives
                        // after this process exits.
                        println!("Keep server running: disabling watchdog...");

                        // Write a sentinel file as a reliable fallback. On Windows
                        // the HTTP request below can race with process exit, leaving
                        // the watchdog unaware it should stay alive. The sentinel
                        // file is checked during the watchdog grace period.
                        let data_dir = app
                            .path()
                            .app_data_dir()
                            .unwrap_or_default();
                        let sentinel = data_dir.join(".keep-running");
                        if let Err(e) = std::fs::write(&sentinel, b"1") {
                            eprintln!("Failed to write keep-running sentinel: {}", e);
                        } else {
                            println!("Wrote keep-running sentinel to {:?}", sentinel);
                        }

                        let client = reqwest::blocking::Client::builder()
                            .timeout(std::time::Duration::from_secs(2))
                            .build()
                            .unwrap();
                        match client
                            .post(&format!("http://127.0.0.1:{}/watchdog/disable", SERVER_PORT))
                            .send()
                        {
                            Ok(resp) => println!("Watchdog disable response: {}", resp.status()),
                            Err(e) => eprintln!("Failed to disable watchdog: {}", e),
                        }
                    } else {
                        // Server will self-terminate via parent-pid watchdog when
                        // this process exits. On Unix, also send SIGTERM for
                        // immediate cleanup.
                        println!("RunEvent::Exit - server will self-terminate via watchdog");

                        #[cfg(unix)]
                        {
                            if let Some(pid) = state.server_pid.lock().unwrap().take() {
                                use std::process::Command;
                                let _ = Command::new("kill")
                                    .args(["-TERM", "--", &format!("-{}", pid)])
                                    .output();
                                std::thread::sleep(std::time::Duration::from_millis(100));
                                let _ = Command::new("kill")
                                    .args(["-9", "--", &format!("-{}", pid)])
                                    .output();
                                let _ = Command::new("kill")
                                    .args(["-9", &pid.to_string()])
                                    .output();
                            }
                        }
                    }
                }
                RunEvent::ExitRequested { api, .. } => {
                    println!("RunEvent::ExitRequested received");
                    // Don't prevent exit, just log it
                    let _ = api;
                }
                _ => {}
            }
        });
}

fn main() {
    run();
}

#[cfg(all(test, unix))]
mod backend_probe_tests {
    use super::probe_binary_version;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new(script: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("voicebox-probe-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            fs::create_dir(&dir).unwrap();
            let exe = dir.join("backend");
            fs::write(&exe, format!("#!/bin/sh\n{script}\n")).unwrap();
            fs::set_permissions(&exe, fs::Permissions::from_mode(0o700)).unwrap();
            Self(dir)
        }
        async fn probe(&self) -> Option<String> {
            probe_binary_version(&self.0.join("backend"), &self.0).await
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }

    #[tokio::test]
    async fn valid_server_version_is_read_from_successful_process() {
        let f = Fixture::new("printf 'voicebox-server 0.6.0\\n'");
        assert_eq!(f.probe().await.as_deref(), Some("0.6.0"));
    }
    #[tokio::test]
    async fn unsuccessful_process_cannot_report_a_usable_version() {
        let f = Fixture::new("printf 'voicebox-server 0.6.0\\n'; exit 3");
        assert_eq!(f.probe().await, None);
    }
    #[tokio::test]
    async fn unrelated_stdout_is_not_a_server_version() {
        let f = Fixture::new("printf 'not-the-server 0.6.0\\n'");
        assert_eq!(f.probe().await, None);
    }
    #[tokio::test]
    async fn version_line_can_be_followed_by_unrelated_output() {
        let f = Fixture::new("printf 'voicebox-server 0.6.0\\nwarning from startup\\n'");
        assert_eq!(f.probe().await.as_deref(), Some("0.6.0"));
    }
    #[tokio::test]
    async fn invalid_utf8_cannot_masquerade_as_version_output() {
        let f = Fixture::new("printf 'voicebox-server 0.6.0\\377\\n'");
        assert_eq!(f.probe().await, None);
    }
    #[tokio::test]
    async fn ambiguous_version_lines_are_rejected() {
        let f = Fixture::new("printf 'voicebox-server 0.6.0\\nvoicebox-server 0.7.0\\n'");
        assert_eq!(f.probe().await, None);
    }
    #[tokio::test]
    async fn missing_executable_and_empty_output_are_not_versions() {
        let f = Fixture::new("exit 0");
        assert_eq!(f.probe().await, None);
        assert_eq!(probe_binary_version(&f.0.join("missing"), &f.0).await, None);
    }
}

#[cfg(all(test, desktop))]
mod chord_validation_tests {
    use super::build_chord_bindings;
    use crate::hotkey_monitor::ChordAction;
    use keytap::Key;
    use std::collections::HashSet;

    fn keys(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn defaults_allow_superset_and_preserve_key_identity() {
        let bindings = build_chord_bindings(
            &keys(&["MetaRight", "AltGr"]),
            &keys(&["MetaRight", "AltRight", "Space"]),
        ).unwrap();
        assert_eq!(bindings[&ChordAction::PushToTalk], HashSet::from([Key::MetaRight, Key::AltRight]));
        assert_eq!(bindings[&ChordAction::ToggleToTalk], HashSet::from([Key::MetaRight, Key::AltRight, Key::Space]));
    }

    #[test]
    fn empty_and_unsupported_chords_are_rejected() {
        assert!(build_chord_bindings(&[], &keys(&["Space"])).is_err());
        assert!(build_chord_bindings(&keys(&["Space"]), &[]).is_err());
        assert!(build_chord_bindings(&keys(&["Unknown"]), &keys(&["Space"])).is_err());
    }

    #[test]
    fn identical_chords_are_rejected_even_with_aliases_and_reordered_keys() {
        let error = build_chord_bindings(
            &keys(&["MetaRight", "AltGr"]),
            &keys(&["AltRight", "MetaRight"]),
        ).unwrap_err();
        assert!(error.contains("different"), "{error}");
    }
}

#[cfg(test)]
mod server_health_tests {
    use super::check_health;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    fn serve(status: &str, headers: &str, body: &str) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let response = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n{headers}\r\n{body}", body.len());
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(std::time::Duration::from_secs(3))).unwrap();
            let mut buf = [0; 1024];
            let _ = stream.read(&mut buf);
            let _ = stream.write_all(response.as_bytes());
        });
        (port, handle)
    }

    #[test]
    fn valid_health_requires_expected_fields_and_success_status() {
        for (status, body, expected) in [
            ("200 OK", r#"{"status":"healthy","model_loaded":false,"gpu_available":true}"#, true),
            ("200 OK", r#"{"status":"healthy","model_loaded":true}"#, false),
            ("200 OK", r#"{"status":"healthy","model_loaded":1,"gpu_available":true}"#, false),
            ("200 OK", r#"{"status":"degraded","model_loaded":true,"gpu_available":false}"#, false),
            ("200 OK", "not json", false),
            ("503 Service Unavailable", r#"{"status":"healthy","model_loaded":true,"gpu_available":true}"#, false),
        ] {
            let (port, handle) = serve(status, "", body);
            assert_eq!(check_health(port), expected, "{status}: {body}");
            handle.join().unwrap();
        }
    }

    #[test]
    fn non_ok_success_statuses_do_not_identify_a_voicebox_server() {
        let body = r#"{"status":"healthy","model_loaded":true,"gpu_available":false}"#;
        for status in ["201 Created", "202 Accepted", "206 Partial Content"] {
            let (port, handle) = serve(status, "", body);
            assert!(!check_health(port), "unexpectedly accepted {status}");
            handle.join().unwrap();
        }
    }

    #[test]
    fn redirect_must_not_turn_an_unrelated_service_into_a_healthy_backend() {
        let (target, target_handle) = serve("200 OK", "", r#"{"status":"healthy","model_loaded":true,"gpu_available":false}"#);
        let (source, source_handle) = serve("302 Found", &format!("Location: http://127.0.0.1:{target}/health\r\n"), "");
        let healthy = check_health(source);
        source_handle.join().unwrap();
        // Release the target listener whether or not the client followed the redirect.
        let _ = std::net::TcpStream::connect(("127.0.0.1", target));
        target_handle.join().unwrap();
        assert!(!healthy);
    }
}

#[cfg(test)]
mod backend_override_tests {
    use super::{read_persisted_backend_override, write_persisted_backend_override};
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn backend_override_survives_restart_and_can_be_cleared() {
        let dir = std::env::temp_dir().join(format!("voicebox-override-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        assert_eq!(read_persisted_backend_override(&dir), None);
        write_persisted_backend_override(&dir, Some("cuda"));
        assert_eq!(read_persisted_backend_override(&dir).as_deref(), Some("cuda"));
        write_persisted_backend_override(&dir, Some("  rocm\n"));
        assert_eq!(read_persisted_backend_override(&dir).as_deref(), Some("rocm"));
        write_persisted_backend_override(&dir, Some(" \n"));
        assert_eq!(read_persisted_backend_override(&dir), None);
        write_persisted_backend_override(&dir, None);
        assert_eq!(read_persisted_backend_override(&dir), None);
        std::fs::remove_dir_all(dir).unwrap();
    }
}

#[cfg(all(test, debug_assertions))]
mod manual_server_tests {
    use super::manual_server_available;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    fn serve(status: &str, body: &str) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let response = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(std::time::Duration::from_secs(2))).unwrap();
            let mut request = [0; 1024];
            if stream.read(&mut request).unwrap_or(0) > 0 {
                let _ = stream.write_all(response.as_bytes());
            }
        });
        (port, server)
    }

    #[tokio::test]
    async fn only_a_healthy_voicebox_server_is_a_usable_manual_server() {
        for (status, body, expected) in [
            ("200 OK", r#"{"status":"healthy","model_loaded":false,"gpu_available":true}"#, true),
            ("200 OK", "unrelated service", false),
            ("200 OK", r#"{"status":"healthy"}"#, false),
            ("503 Service Unavailable", r#"{"status":"healthy","model_loaded":true,"gpu_available":true}"#, false),
        ] {
            let (port, server) = serve(status, body);
            assert_eq!(manual_server_available(port).await, expected, "{status}: {body}");
            server.join().unwrap();
        }
    }

    #[tokio::test]
    async fn absent_listener_is_not_a_usable_manual_server() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        assert!(!manual_server_available(port).await);
    }
}

#[cfg(test)]
mod backend_selection_tests {
    use super::eligible_gpu_backends;
    use std::path::PathBuf;

    #[test]
    fn installed_and_pinned_backends_keep_the_expected_fallbacks() {
        for (pin, rocm, cuda, expected) in [
            (None, true, true, (true, true)),
            (None, true, false, (true, false)),
            (None, false, true, (false, true)),
            (None, false, false, (false, false)),
            (Some("cuda"), true, true, (false, true)),
            (Some("rocm"), true, true, (true, false)),
            (Some("cuda"), true, false, (true, false)),
            (Some("rocm"), false, true, (false, true)),
            (Some("cuda"), false, false, (false, false)),
            (Some("rocm"), false, false, (false, false)),
            (Some("cpu"), true, true, (true, true)),
            (Some("unknown"), true, true, (true, true)),
        ] {
            let actual = eligible_gpu_backends(pin, rocm.then(|| PathBuf::from("rocm")), cuda.then(|| PathBuf::from("cuda")));
            assert_eq!((actual.0.is_some(), actual.1.is_some()), expected, "pin={pin:?}, rocm={rocm}, cuda={cuda}");
            if let Some(path) = actual.0 { assert_eq!(path, PathBuf::from("rocm")); }
            if let Some(path) = actual.1 { assert_eq!(path, PathBuf::from("cuda")); }
        }
    }
}

#[cfg(test)]
mod server_arguments_tests {
    use super::server_arguments;

    #[test]
    fn local_launch_preserves_data_path_as_one_argument_and_uses_loopback_default() {
        assert_eq!(server_arguments("/data/voice box", 17493, 123, false),
            ["--data-dir", "/data/voice box", "--port", "17493", "--parent-pid", "123"]);
    }

    #[test]
    fn remote_launch_adds_public_host_without_changing_other_arguments() {
        assert_eq!(server_arguments("/data/voice box", 17493, 123, true),
            ["--data-dir", "/data/voice box", "--port", "17493", "--parent-pid", "123", "--host", "0.0.0.0"]);
    }
}

#[cfg(test)]
mod microphone_origin_tests {
    use super::trusted_microphone_origin;

    #[test]
    fn permits_the_desktop_origin_and_local_development_servers() {
        for uri in [
            "tauri://localhost/", "tauri://localhost/index.html",
            "https://tauri.localhost/", "https://tauri.localhost/index.html",
            "http://localhost:5173/", "http://127.0.0.1:5173/index.html",
        ] {
            assert!(trusted_microphone_origin(uri), "{uri}");
        }
    }

    #[test]
    fn rejects_prefix_spoofs_and_nonlocal_origins() {
        for uri in [
            "http://localhost.evil.example/", "http://localhost@evil.example/",
            "http://127.0.0.1.evil.example/", "http://127.0.0.1@evil.example/",
            "https://tauri.localhost.evil.example/", "https://tauri.localhost@evil.example/",
            "tauri://evil.example/", "tauri://localhost:444/", "https://tauri.localhost:444/", "http://user@localhost/", "https://evil.example/", "file:///example/index.html",
        ] {
            assert!(!trusted_microphone_origin(uri), "{uri}");
        }
    }

    #[test]
    fn rejects_malformed_and_unsafe_schemes() {
        for uri in ["", "http://localhost:invalid/", "http://localhost:99999/", "https://localhost/", "http://tauri.localhost/", "javascript:alert(1)"] {
            assert!(!trusted_microphone_origin(uri), "{uri}");
        }
    }
}

#[cfg(test)]
mod media_permission_tests {
    use super::allow_media_request;

    #[test]
    fn trusted_origins_only_allow_microphone_requests() {
        for uri in ["tauri://localhost/", "https://tauri.localhost/", "http://localhost:5173/"] {
            assert!(allow_media_request(uri, true, false, false), "{uri}");
            for (audio, video, display) in [(false, true, false), (true, true, false), (false, false, true), (true, false, true), (true, true, true), (false, false, false)] {
                assert!(!allow_media_request(uri, audio, video, display), "{uri}: {audio} {video} {display}");
            }
        }
    }

    #[test]
    fn untrusted_origins_cannot_receive_any_media_permission() {
        for uri in ["http://localhost.evil.example/", "https://evil.example/", "file:///example/index.html", ""] {
            for audio in [false, true] {
                for video in [false, true] {
                    for display in [false, true] {
                        assert!(!allow_media_request(uri, audio, video, display), "{uri}: {audio} {video} {display}");
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod restart_configuration_tests {
    use super::{resolve_remote_setting, server_arguments, update_models_dir};
    use std::sync::Mutex;

    fn args(remote: bool) -> Vec<String> { server_arguments("/data", 17493, 123, remote) }

    #[test]
    fn remote_binding_survives_restart_without_an_explicit_override() {
        let state = Mutex::new(false);
        assert!(args(resolve_remote_setting(&state, Some(true))).contains(&"0.0.0.0".into()));
        let restarted = args(resolve_remote_setting(&state, None));
        assert!(restarted.contains(&"0.0.0.0".into()));
        assert_eq!(restarted.last().unwrap(), "0.0.0.0");
    }

    #[test]
    fn explicit_local_mode_replaces_the_previous_remote_choice() {
        let state = Mutex::new(false);
        resolve_remote_setting(&state, Some(true));
        assert!(!args(resolve_remote_setting(&state, Some(false))).contains(&"--host".into()));
        assert!(!args(resolve_remote_setting(&state, None)).contains(&"--host".into()));
    }

    #[test]
    fn models_directory_is_retained_replaced_and_reset_across_restarts() {
        let models = Mutex::new(None);
        update_models_dir(&models, Some("/Volumes/Models With Spaces"));
        assert_eq!(models.lock().unwrap().as_deref(), Some("/Volumes/Models With Spaces"));
        update_models_dir(&models, None);
        assert_eq!(models.lock().unwrap().as_deref(), Some("/Volumes/Models With Spaces"));
        update_models_dir(&models, Some("/Volumes/別の場所"));
        assert_eq!(models.lock().unwrap().as_deref(), Some("/Volumes/別の場所"));
        update_models_dir(&models, Some(""));
        assert_eq!(*models.lock().unwrap(), None);
        update_models_dir(&models, None);
        assert_eq!(*models.lock().unwrap(), None);
    }

    #[test]
    fn omitted_setting_defaults_to_local_and_each_instance_is_independent() {
        let first = Mutex::new(false);
        let second = Mutex::new(false);
        assert!(!resolve_remote_setting(&first, None));
        assert!(resolve_remote_setting(&first, Some(true)));
        assert!(!resolve_remote_setting(&second, None));
        assert!(resolve_remote_setting(&first, None));
    }
}
