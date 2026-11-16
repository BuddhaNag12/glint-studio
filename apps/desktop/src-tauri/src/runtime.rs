//! First-run install of the core runtime (uv, Python 3.12, a venv, the host's dependencies), the
//! optional local engine (PyTorch for this machine's GPU, ComfyUI), and starting them. Every install step is safe to re-run: finished work is skipped or is a fast no-op.
//! The host's own code ships inside the app (bundle resources); only its dependencies come from PyPI.

use std::fs;
use std::io;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::async_runtime::Mutex;
use tauri::ipc::Channel;
use tauri::{AppHandle, Manager, State};

use crate::paths::{Paths, exe};
use crate::scan::{self, Backend, Vendor};
use crate::supervisor::{ProcessSpec, ProcessStatus, Supervisor, free_port};

const UV_VERSION: &str = "0.13.8";
/// (target triple, archive extension, sha256), from the `.sha256` files on the uv release.
const UV_ARCHIVES: &[(&str, &str, &str)] = &[
    ("aarch64-apple-darwin", "tar.gz", "6b71028a6b904f2ae3340ef52e595512de807d06c0a20eaef2f692581b246558"),
    ("x86_64-apple-darwin", "tar.gz", "a04c2ffeac28b2003a2c13caf538ab425fcd7def3e4a41571f4a8fb370a66af3"),
    ("x86_64-unknown-linux-gnu", "tar.gz", "5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91"),
    ("aarch64-unknown-linux-gnu", "tar.gz", "74f2f5c6024d397481db228db8fc25eed420db192243f88438c118e7e013e585"),
    ("x86_64-pc-windows-msvc", "zip", "529e78a9cec6cd7e3e014f7ecd8bda0c113969d4ed0f2efeedb85c30943325b8"),
    ("aarch64-pc-windows-msvc", "zip", "59e6d919bed1f72035e622a7c930058f08a8931c4c78ceaf9f56e7fb74dd2c48"),
];
const PYTHON: &str = "3.12";
const HOST: &str = "host";
/// A broken host restarts with backoff before the supervisor gives up (~35 s), so wait a bit longer.
const HOST_START_TIMEOUT: Duration = Duration::from_secs(45);

const COMFYUI: &str = "comfyui";
const COMFYUI_VERSION: &str = "0.45.0";
/// sha256 of GitHub's tag tarball for COMFYUI_VERSION.
const COMFYUI_SHA: &str = "408670ede5da641b30a2b3f1e39e5b84d75c12357e75695416a6e6cc5bcbc4bb";
/// Importing torch is slow on a first, cold start.
const COMFYUI_START_TIMEOUT: Duration = Duration::from_secs(180);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Step {
    Uv,
    Python,
    Venv,
    Host,
    Torch,
    Comfyui,
}

/// Sent to the UI as each step starts (`done: false`) and finishes (`done: true`).
/// A failure is the command's error; the step that failed is the last one not done.
#[derive(Clone, Debug, Serialize)]
pub struct Progress {
    pub step: Step,
    pub done: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct HostInfo {
    pub url: String,
    /// New every launch; the UI keeps it in memory only.
    pub api_key: String,
    /// Bound to every address (the LAN), not just 127.0.0.1.
    #[serde(skip)]
    pub(crate) lan: bool,
    /// The ComfyUI this host talks to, kept when only the host restarts.
    #[serde(skip)]
    comfy_port: Option<u16>,
}

/// Managed state. The locks stop a second call (React StrictMode runs effects twice in dev)
/// from racing the first.
#[derive(Default)]
pub struct Runtime {
    pub supervisor: Supervisor,
    installing: Mutex<()>,
    host: Mutex<Option<HostInfo>>,
}

#[tauri::command]
pub async fn install_core(app: AppHandle, state: State<'_, Runtime>, on_progress: Channel<Progress>) -> Result<(), String> {
    let _guard = state.installing.lock().await;
    let paths = app_paths(&app)?;
    let host_dir = resource_dir(&app)?.join("host");
    tauri::async_runtime::spawn_blocking(move || {
        install(&paths, &host_dir, |p| {
            let _ = on_progress.send(p); // the window may be gone; the install carries on
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

/// PyTorch for this machine's GPU, then ComfyUI and its dependencies. Needs `install_core` first.
#[tauri::command]
pub async fn install_engine(app: AppHandle, state: State<'_, Runtime>, on_progress: Channel<Progress>) -> Result<(), String> {
    let _guard = state.installing.lock().await;
    let paths = app_paths(&app)?;
    tauri::async_runtime::spawn_blocking(move || {
        let index = torch_index_for(&scan::scan());
        setup_engine(&paths, index, |p| {
            let _ = on_progress.send(p); // the window may be gone; the install carries on
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn start_host(app: AppHandle) -> Result<HostInfo, String> {
    ensure_host(&app).await
}

/// Starts the host, and ComfyUI too when the engine is installed (the host is pointed at it).
/// Returns the running host as is when both are already up.
pub async fn ensure_host(app: &AppHandle) -> Result<HostInfo, String> {
    let state = app.state::<Runtime>();
    let mut host = state.host.lock().await;
    let supervisor = state.supervisor.clone();
    let paths = app_paths(app)?;
    let engine = engine_installed(&paths);
    let lan = crate::network::wanted(&paths).lan;
    let running = |name| supervisor.status(name) == ProcessStatus::Running;
    if let Some(info) = host.as_ref() {
        if running(HOST) && info.lan == lan && (!engine || running(COMFYUI)) {
            return Ok(info.clone());
        }
    }
    // A LAN switch restarts the running host; free its port first so it keeps the same one.
    supervisor.stop(HOST);
    if !paths.venv_python().exists() {
        return Err("The runtime isn't installed yet.".into());
    }
    let mut dirs = vec![paths.outputs(), paths.styles(), paths.workflows()];
    if engine {
        // ComfyUI exits if custom_nodes is missing, and --models-directory must exist.
        dirs.extend([paths.comfyui_data().join("custom_nodes"), paths.models()]);
    }
    for dir in dirs {
        fs::create_dir_all(&dir).map_err(|e| format!("Couldn't create {}: {e}", dir.display()))?;
    }
    // The same address and key as last time, so apps set up with them and open dashboard tabs keep working.
    let port = host_port(host.as_ref(), lan).map_err(|e| e.to_string())?;
    let api_key = match host.as_ref() {
        Some(info) => info.api_key.clone(),
        None => crate::secrets::host_api_key().or_else(|e| {
            eprintln!("host key: {e}; using a key for this launch only");
            new_key()
        })?,
    };
    let mut info = HostInfo { url: format!("http://127.0.0.1:{port}"), api_key, lan, comfy_port: None };
    let mut spec = host_spec(&paths, &resource_dir(app)?, port, &info.api_key, lan);
    let mut waits = vec![(HOST, HOST_START_TIMEOUT, spec.log_file.clone())];
    if engine {
        // A ComfyUI that's up (or on its way) stays: only the host needs restarting.
        let comfy_up = matches!(supervisor.status(COMFYUI), ProcessStatus::Running | ProcessStatus::Starting);
        let reuse = host.as_ref().and_then(|i| i.comfy_port).filter(|_| comfy_up);
        let comfy_port = match reuse {
            Some(port) => port,
            None => free_port().map_err(|e| e.to_string())?,
        };
        point_host_at_comfyui(&mut spec, &paths, comfy_port);
        info.comfy_port = Some(comfy_port);
        let comfy = comfyui_spec(&paths, comfy_port);
        waits.push((COMFYUI, COMFYUI_START_TIMEOUT, comfy.log_file.clone()));
        if reuse.is_none() {
            supervisor.start(comfy).map_err(|e| format!("Couldn't start ComfyUI: {e}"))?;
        }
    }
    // After the ComfyUI vars, so a provider key can't take their names.
    spec.env.extend(crate::secrets::host_env(&paths, &resource_dir(app)?, &spec.env));
    supervisor.start(spec).map_err(|e| format!("Couldn't start the host: {e}"))?;
    tauri::async_runtime::spawn_blocking(move || {
        let _ = write_hardware(&paths); // without it the host recommends models for the CPU
        for (name, timeout, log) in waits {
            wait_running(&supervisor, name, timeout).map_err(|e| format!("{e}\n{}", log_tail(&log, 15)))?;
        }
        Ok::<_, String>(())
    })
    .await
    .map_err(|e| e.to_string())??;
    *host = Some(info.clone());
    Ok(info)
}

/// Whether `install_core` has finished once, so the host can start without the setup window.
pub fn core_installed(app: &AppHandle) -> bool {
    app_paths(app).is_ok_and(|p| p.venv_python().exists())
}

impl Runtime {
    /// The started host (None before the first start), or Err while a start or an install is under way.
    pub(crate) fn host_now(&self) -> Result<Option<HostInfo>, ()> {
        let _installing = self.installing.try_lock().map_err(|_| ())?;
        self.host.try_lock().map(|h| h.clone()).map_err(|_| ())
    }

    /// One line for the menu-bar menu.
    pub fn status_line(&self) -> &'static str {
        use ProcessStatus::*;
        match (self.supervisor.status(HOST), self.supervisor.status(COMFYUI)) {
            (Running, Running | Stopped | Starting) => "● Running",
            (Running, _) => "⚠ The local engine stopped",
            (Starting, _) => "Starting…",
            (Stopped, _) => "○ Not running",
            _ => "⚠ Stopped: open Setup to repair",
        }
    }
}

/// The host can't see the GPU itself, so it recommends models from the app's scan.
fn write_hardware(paths: &Paths) -> io::Result<()> {
    fs::write(paths.hardware_file(), serde_json::to_vec(&scan::scan())?)
}

fn app_paths(app: &AppHandle) -> Result<Paths, String> {
    let root = app.path().app_data_dir().map_err(|e| e.to_string())?;
    Ok(Paths { root })
}

fn resource_dir(app: &AppHandle) -> Result<PathBuf, String> {
    app.path().resource_dir().map_err(|e| e.to_string())
}

/// Runs every step in order, reporting each one.
pub fn install(paths: &Paths, host_dir: &Path, report: impl Fn(Progress)) -> Result<(), String> {
    for step in [Step::Uv, Step::Python, Step::Venv, Step::Host] {
        report(Progress { step, done: false });
        match step {
            Step::Uv => ensure_uv(paths)?,
            Step::Python => run(uv(paths).args(["python", "install", PYTHON]))?,
            Step::Venv => {
                if !paths.venv_python().exists() {
                    run(uv(paths).args(["venv", "--clear", "--python", PYTHON]).arg(paths.venv()))?;
                }
            }
            Step::Host => run(
                uv(paths)
                    .args(["pip", "install", "--python"])
                    .arg(paths.venv_python())
                    .arg("-r")
                    .arg(host_dir.join("pyproject.toml")),
            )?,
            Step::Torch | Step::Comfyui => unreachable!("engine steps run in setup_engine"),
        }
        report(Progress { step, done: true });
    }
    Ok(())
}

/// Torch, then ComfyUI's code and requirements (torch is already satisfied, so it stays as installed).
/// `index` is the PyTorch wheel index for this machine; None means PyPI.
pub fn setup_engine(paths: &Paths, index: Option<&str>, report: impl Fn(Progress)) -> Result<(), String> {
    // shortcut: no byte progress for the GB-sized torch download; stream uv's output if the wait confuses people.
    let pip = || {
        let mut cmd = uv(paths);
        cmd.args(["pip", "install", "--python"]).arg(paths.venv_python());
        cmd
    };
    report(Progress { step: Step::Torch, done: false });
    let mut torch = pip();
    torch.args(["torch", "torchvision"]);
    if let Some(index) = index {
        torch.args(["--index-url", index]);
    }
    run(&mut torch)?;
    report(Progress { step: Step::Torch, done: true });

    report(Progress { step: Step::Comfyui, done: false });
    ensure_comfyui(paths)?;
    let code = comfyui_code(paths);
    run(pip().arg("-r").arg(code.join("requirements.txt")))?;
    fs::write(code.join(READY_MARKER), "").map_err(|e| e.to_string())?;
    report(Progress { step: Step::Comfyui, done: true });
    Ok(())
}

fn torch_index_for(hw: &scan::HardwareProfile) -> Option<&'static str> {
    let driver = hw.gpus.iter().find(|g| g.vendor == Vendor::Nvidia).and_then(|g| g.driver.as_deref());
    torch_index(&hw.os, hw.backend, driver)
}

/// Which PyTorch wheels fit this machine. CUDA 13 wheels need driver 580+; older drivers get CUDA 12.6.
/// macOS (Apple GPU or CPU) uses the default PyPI build.
fn torch_index(os: &str, backend: Backend, nvidia_driver: Option<&str>) -> Option<&'static str> {
    match backend {
        Backend::Mps => None,
        Backend::Cpu if os == "macos" => None,
        Backend::Cpu => Some("https://download.pytorch.org/whl/cpu"),
        Backend::Cuda => {
            let major = nvidia_driver.and_then(|d| d.split('.').next()?.trim().parse::<u32>().ok());
            Some(if major.is_some_and(|m| m >= 580) {
                "https://download.pytorch.org/whl/cu130"
            } else {
                "https://download.pytorch.org/whl/cu126"
            })
        }
        Backend::Rocm => Some("https://download.pytorch.org/whl/rocm7.2"),
        Backend::Xpu => Some("https://download.pytorch.org/whl/xpu"),
    }
}

const READY_MARKER: &str = ".glint-ready";

/// ComfyUI's code, one folder per pinned version.
fn comfyui_code(paths: &Paths) -> PathBuf {
    paths.runtime().join(format!("ComfyUI-{COMFYUI_VERSION}"))
}

/// Written once ComfyUI's requirements are installed, so a half-done install isn't started.
fn engine_installed(paths: &Paths) -> bool {
    comfyui_code(paths).join(READY_MARKER).exists()
}

fn ensure_comfyui(paths: &Paths) -> Result<(), String> {
    let code = comfyui_code(paths);
    if code.join("main.py").exists() {
        return Ok(()); // unpacked elsewhere and moved in whole, so main.py means complete
    }
    let url = format!("https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/v{COMFYUI_VERSION}.tar.gz");
    let tmp = fetch_and_unpack(paths, &url, COMFYUI_SHA, "ComfyUI")?;
    let _ = fs::remove_dir_all(&code);
    fs::rename(tmp.join(format!("ComfyUI-{COMFYUI_VERSION}")), &code).map_err(|e| e.to_string())?;
    let _ = fs::remove_dir_all(&tmp);
    Ok(())
}

/// ComfyUI on localhost only; the host is its one client.
pub fn comfyui_spec(paths: &Paths, port: u16) -> ProcessSpec {
    let path = |p: PathBuf| p.to_string_lossy().into_owned();
    let mut args: Vec<String> = ["main.py", "--listen", "127.0.0.1", "--port", &port.to_string()].map(String::from).to_vec();
    args.extend(["--base-directory".into(), path(paths.comfyui_data())]);
    args.extend(["--models-directory".into(), path(paths.models())]);
    args.push("--disable-auto-launch".into());
    ProcessSpec {
        name: COMFYUI.into(),
        program: paths.venv_python(),
        args,
        cwd: Some(comfyui_code(paths)),
        env: [("PYTHONUNBUFFERED", "1"), ("PYTORCH_ENABLE_MPS_FALLBACK", "1")]
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .to_vec(),
        health_url: Some(format!("http://127.0.0.1:{port}/system_stats")),
        log_file: paths.logs().join("comfyui.log"),
    }
}

/// The host finds ComfyUI by URL, and its temp folder (for cleanup) under the base directory.
fn point_host_at_comfyui(host: &mut ProcessSpec, paths: &Paths, comfy_port: u16) {
    host.env.push(("COMFYUI_URL".into(), format!("http://127.0.0.1:{comfy_port}")));
    host.env.push(("COMFYUI_DIR".into(), paths.comfyui_data().to_string_lossy().into_owned()));
}

/// The release archive name and its sha256 for this OS and CPU.
fn uv_archive(os: &str, arch: &str) -> Option<(String, &'static str)> {
    let vendor = match os {
        "macos" => "apple-darwin",
        "linux" => "unknown-linux-gnu",
        "windows" => "pc-windows-msvc",
        _ => return None,
    };
    let triple = format!("{arch}-{vendor}");
    let (_, ext, sha) = UV_ARCHIVES.iter().find(|(t, _, _)| *t == triple)?;
    Some((format!("uv-{triple}.{ext}"), sha))
}

fn ensure_uv(paths: &Paths) -> Result<(), String> {
    let uv = paths.uv();
    if uv.exists() {
        return Ok(());
    }
    let (name, sha) = uv_archive(std::env::consts::OS, std::env::consts::ARCH)
        .ok_or("There's no uv build for this system.")?;
    let url = format!("https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{name}");
    let tmp = fetch_and_unpack(paths, &url, sha, "uv")?;
    let found = find_file(&tmp, &exe("uv")).ok_or("The uv archive has no uv binary.")?;
    fs::create_dir_all(uv.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::rename(&found, &uv).map_err(|e| e.to_string())?;
    let _ = fs::remove_dir_all(&tmp);
    Ok(())
}

/// Downloads `url` into a fresh runtime/download-<what> folder, checks its sha256 and unpacks it there.
/// Returns that folder; the caller moves out what it needs and deletes it.
fn fetch_and_unpack(paths: &Paths, url: &str, sha: &str, what: &str) -> Result<PathBuf, String> {
    let archive = fetch(paths, url, sha, what)?;
    let tmp = archive.parent().unwrap().to_path_buf();
    run(Command::new(system_tool("tar")).arg("-xf").arg(&archive).arg("-C").arg(&tmp))?;
    Ok(tmp)
}

/// Downloads `url` into a fresh folder of its own (two downloads at once don't meet) and checks its sha256.
pub(crate) fn fetch(paths: &Paths, url: &str, sha: &str, what: &str) -> Result<PathBuf, String> {
    let tmp = paths.runtime().join(format!("download-{}", what.to_lowercase().replace(' ', "-")));
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    let archive = tmp.join(url.rsplit('/').next().unwrap_or("archive"));
    // shortcut: system curl and tar (in Windows 10+, macOS and desktop Linux) instead of an HTTP
    // and archive crate; switch if the app ever needs an HTTP client of its own.
    run(Command::new(system_tool("curl")).args(["-fsSL", "--retry", "3", "-o"]).arg(&archive).arg(url))?;
    let got = sha256_file(&archive).map_err(|e| e.to_string())?;
    if got != sha {
        return Err(format!("The {what} download is corrupt (sha256 {got}, expected {sha}). Try again."));
    }
    Ok(archive)
}

/// uv with everything it writes (Pythons, cache) kept inside our runtime folder, and the user's
/// own uv config and active venv ignored.
fn uv(paths: &Paths) -> Command {
    let rt = paths.runtime();
    let mut cmd = Command::new(paths.uv());
    cmd.env("UV_PYTHON_INSTALL_DIR", rt.join("python"))
        .env("UV_CACHE_DIR", rt.join("cache"))
        .env("UV_PYTHON_PREFERENCE", "only-managed")
        .env("UV_NO_CONFIG", "1")
        .env_remove("VIRTUAL_ENV");
    cmd
}

/// Windows has curl and tar in System32; a Git or MSYS copy earlier on PATH behaves differently.
pub(crate) fn system_tool(name: &str) -> PathBuf {
    if cfg!(windows) {
        let root = std::env::var_os("SystemRoot").unwrap_or_else(|| "C:\\Windows".into());
        PathBuf::from(root).join("System32").join(exe(name))
    } else {
        PathBuf::from(name)
    }
}

/// Runs to completion; on failure the error carries the tool's last lines of stderr.
pub(crate) fn run(cmd: &mut Command) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let program = Path::new(cmd.get_program()).file_name().unwrap_or_default().to_string_lossy().into_owned();
    let out = cmd
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("Couldn't run {program}: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    let lines: Vec<&str> = stderr.lines().filter(|l| !l.trim().is_empty()).collect();
    let tail = lines[lines.len().saturating_sub(8)..].join("\n");
    Err(format!("{program} failed ({}):\n{tail}", out.status))
}

pub(crate) fn sha256_file(path: &Path) -> io::Result<String> {
    let mut hasher = Sha256::new();
    io::copy(&mut fs::File::open(path)?, &mut hasher)?;
    Ok(hex(&hasher.finalize()))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub(crate) fn find_file(dir: &Path, name: &str) -> Option<PathBuf> {
    for entry in fs::read_dir(dir).ok()?.flatten() {
        let path = entry.path();
        if path.is_dir() {
            if let Some(found) = find_file(&path, name) {
                return Some(found);
            }
        } else if path.file_name().is_some_and(|n| n == name) {
            return Some(path);
        }
    }
    None
}

/// The host's usual port; another one only when something else holds it.
const HOST_PORT: u16 = 7788;

fn host_port(last: Option<&HostInfo>, lan: bool) -> io::Result<u16> {
    let addr = if lan { "0.0.0.0" } else { "127.0.0.1" };
    let last = last.and_then(|i| i.url.rsplit(':').next()?.parse().ok());
    // shortcut: probe-then-release can race another app for the port; the host then fails to start and the
    // next try picks a free one. Pass the bound socket to the host if that ever shows up.
    for port in last.into_iter().chain([HOST_PORT]) {
        if TcpListener::bind((addr, port)).is_ok() {
            return Ok(port);
        }
    }
    free_port()
}

pub(crate) fn new_key() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).map_err(|e| format!("No randomness for the API key: {e}"))?;
    Ok(hex(&bytes))
}

/// Webview origins that may call the host: macOS/Linux, Windows, and `tauri dev`.
fn webview_origins() -> &'static str {
    if cfg!(debug_assertions) {
        "tauri://localhost,http://tauri.localhost,http://localhost:1420"
    } else {
        "tauri://localhost,http://tauri.localhost"
    }
}

/// `resources` holds `host/` (the app package and pyproject.toml), `catalog/` and `dashboard/` (the built UI).
/// `lan` binds every address; the API key is required either way, and the dashboard pages stay local-only.
pub fn host_spec(paths: &Paths, resources: &Path, port: u16, api_key: &str, lan: bool) -> ProcessSpec {
    let url = format!("http://127.0.0.1:{port}");
    let path = |p: PathBuf| p.to_string_lossy().into_owned();
    let env = [
        ("API_KEY", api_key.to_string()),
        ("PUBLIC_BASE_URL", url.clone()),
        ("CORS_ORIGINS", webview_origins().to_string()),
        ("CATALOG_DIR", path(resources.join("catalog"))),
        ("OUTPUT_DIR", path(paths.outputs())),
        ("WORKFLOWS_DIR", path(paths.workflows())),
        ("USER_STYLES_DIR", path(paths.styles())),
        ("MODELS_DIR", path(paths.models())),
        ("HARDWARE_FILE", path(paths.hardware_file())),
        ("STATS_DB", path(paths.stats_db())),
        ("DASHBOARD_DIR", path(resources.join("dashboard"))),
        ("NETWORK_FILE", path(paths.network_file())),
        ("TUNNEL_FILE", path(paths.tunnel_file())),
        ("SERVE_ON_NETWORK", lan.to_string()),
        // The dashboard pages run on a login cookie; refusing other Host headers there stops DNS rebinding.
        ("ALLOWED_HOSTS", "127.0.0.1,localhost".into()),
        ("PYTHONDONTWRITEBYTECODE", "1".into()), // the app bundle is read-only
        ("PYTHONUNBUFFERED", "1".into()),        // logs reach host.log as they happen
    ];
    ProcessSpec {
        name: HOST.into(),
        program: paths.venv_python(),
        // No access log: the supervisor's health check alone would add a line every second.
        // Uvicorn trusts X-Forwarded-Proto from 127.0.0.1 only: the tunnel, so its image links are https.
        args: ["-m", "uvicorn", "app.main:app", "--host", if lan { "0.0.0.0" } else { "127.0.0.1" }, "--port", &port.to_string(), "--no-access-log"]
            .map(String::from)
            .to_vec(),
        cwd: Some(resources.join("host")),
        env: env.into_iter().map(|(k, v)| (k.to_string(), v)).collect(),
        health_url: Some(format!("{url}/livez")),
        log_file: paths.logs().join("host.log"),
    }
}

// shortcut: a host that dies at import keeps getting restarted until the supervisor gives up (~35 s);
// watch the exit code here and fail on the first crash if that wait annoys people.
fn wait_running(supervisor: &Supervisor, name: &str, timeout: Duration) -> Result<(), String> {
    let started = Instant::now();
    let label = if name == HOST { "The host" } else { "ComfyUI" };
    loop {
        match supervisor.status(name) {
            ProcessStatus::Running => return Ok(()),
            ProcessStatus::Crashed { .. } | ProcessStatus::Stopped => {
                return Err(format!("{label} stopped while starting."));
            }
            _ if started.elapsed() > timeout => return Err(format!("{label} didn't start in time.")),
            _ => thread::sleep(Duration::from_millis(250)),
        }
    }
}

pub(crate) fn log_tail(path: &Path, n: usize) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("glint-runtime-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn uv_archive_per_platform() {
        assert_eq!(uv_archive("macos", "aarch64").unwrap().0, "uv-aarch64-apple-darwin.tar.gz");
        assert_eq!(uv_archive("linux", "x86_64").unwrap().0, "uv-x86_64-unknown-linux-gnu.tar.gz");
        assert_eq!(uv_archive("windows", "x86_64").unwrap().0, "uv-x86_64-pc-windows-msvc.zip");
        assert_eq!(uv_archive("linux", "riscv64"), None);
        assert_eq!(uv_archive("freebsd", "x86_64"), None);
        for (_, _, sha) in UV_ARCHIVES {
            assert!(sha.len() == 64 && sha.bytes().all(|b| b.is_ascii_hexdigit()));
        }
    }

    #[test]
    fn sha256_of_a_file() {
        let dir = test_dir("sha");
        let file = dir.join("abc");
        fs::write(&file, "abc").unwrap();
        assert_eq!(
            sha256_file(&file).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn finds_a_nested_file() {
        let dir = test_dir("find");
        fs::create_dir_all(dir.join("uv-x/inner")).unwrap();
        fs::write(dir.join("uv-x/uvx"), "").unwrap();
        fs::write(dir.join("uv-x/inner/uv"), "").unwrap();
        assert_eq!(find_file(&dir, "uv"), Some(dir.join("uv-x/inner/uv")));
        assert_eq!(find_file(&dir, "nope"), None);
    }

    #[test]
    fn host_port_keeps_the_last_port_while_it_is_free() {
        let info = |port: u16| HostInfo { url: format!("http://127.0.0.1:{port}"), api_key: String::new(), lan: false, comfy_port: None };
        let free = free_port().unwrap();
        assert_eq!(host_port(Some(&info(free)), false).unwrap(), free);
        let held = TcpListener::bind("127.0.0.1:0").unwrap();
        let busy = held.local_addr().unwrap().port();
        let port = host_port(Some(&info(busy)), false).unwrap();
        assert_ne!(port, busy);
        TcpListener::bind(("127.0.0.1", port)).unwrap();
    }

    #[test]
    fn keys_are_random_hex() {
        let (a, b) = (new_key().unwrap(), new_key().unwrap());
        assert_eq!(a.len(), 64);
        assert!(a.bytes().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn host_spec_points_at_app_data_and_resources() {
        let paths = Paths { root: PathBuf::from("/data") };
        let spec = host_spec(&paths, Path::new("/res"), 4321, "k", false);
        let env = |k: &str| spec.env.iter().find(|(key, _)| key == k).map(|(_, v)| v.as_str());
        assert_eq!(env("API_KEY"), Some("k"));
        assert_eq!(env("PUBLIC_BASE_URL"), Some("http://127.0.0.1:4321"));
        assert!(env("CORS_ORIGINS").unwrap().contains("tauri://localhost"));
        assert_eq!(env("CATALOG_DIR").map(PathBuf::from), Some(PathBuf::from("/res").join("catalog")));
        assert_eq!(spec.cwd, Some(PathBuf::from("/res").join("host")));
        assert_eq!(env("MODELS_DIR").map(PathBuf::from), Some(PathBuf::from("/data").join("models")));
        assert_eq!(env("DASHBOARD_DIR").map(PathBuf::from), Some(PathBuf::from("/res").join("dashboard")));
        assert_eq!(env("ALLOWED_HOSTS"), Some("127.0.0.1,localhost"));
        assert_eq!(spec.health_url.as_deref(), Some("http://127.0.0.1:4321/livez"));
        assert!(spec.args.contains(&"4321".to_string()));
        assert!(spec.args.contains(&"127.0.0.1".to_string()));
        assert_eq!(env("SERVE_ON_NETWORK"), Some("false"));
        assert_eq!(env("NETWORK_FILE").map(PathBuf::from), Some(PathBuf::from("/data").join("network.json")));

        let lan = host_spec(&paths, Path::new("/res"), 4321, "k", true);
        assert!(lan.args.contains(&"0.0.0.0".to_string()));
        assert!(lan.env.contains(&("SERVE_ON_NETWORK".into(), "true".into())));
    }

    #[test]
    fn torch_index_per_backend() {
        assert_eq!(torch_index("macos", Backend::Mps, None), None);
        assert_eq!(torch_index("macos", Backend::Cpu, None), None);
        assert_eq!(torch_index("linux", Backend::Cpu, None), Some("https://download.pytorch.org/whl/cpu"));
        let cuda = |d| torch_index("windows", Backend::Cuda, d).unwrap().rsplit('/').next().unwrap();
        assert_eq!(cuda(Some("581.57")), "cu130");
        assert_eq!(cuda(Some("580.95.05")), "cu130");
        assert_eq!(cuda(Some("566.36")), "cu126");
        assert_eq!(cuda(None), "cu126");
        assert_eq!(cuda(Some("garbage")), "cu126");
        assert!(torch_index("linux", Backend::Rocm, None).unwrap().ends_with("rocm7.2"));
        assert!(torch_index("windows", Backend::Xpu, None).unwrap().ends_with("xpu"));
    }

    #[test]
    fn comfyui_spec_uses_app_data_and_localhost() {
        let paths = Paths { root: PathBuf::from("/data") };
        let spec = comfyui_spec(&paths, 5555);
        let arg = |flag: &str| spec.args.iter().position(|a| a == flag).map(|i| spec.args[i + 1].clone());
        assert_eq!(spec.args[0], "main.py");
        assert_eq!(arg("--listen").as_deref(), Some("127.0.0.1"));
        assert_eq!(arg("--port").as_deref(), Some("5555"));
        assert_eq!(arg("--base-directory").map(PathBuf::from), Some(paths.comfyui_data()));
        assert_eq!(arg("--models-directory").map(PathBuf::from), Some(paths.models()));
        assert_eq!(spec.cwd, Some(comfyui_code(&paths)));
        assert_eq!(spec.health_url.as_deref(), Some("http://127.0.0.1:5555/system_stats"));
        assert!(!engine_installed(&paths));
    }

    /// The real thing, against the network: installs into the temp dir (kept, so a re-run is fast)
    /// and starts the host from this repo.
    /// `cargo test real_install_and_host -- --ignored --nocapture`
    #[test]
    #[ignore]
    fn real_install_and_host() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
        let paths = Paths { root: std::env::temp_dir().join("glint-runtime-real") };
        let started = Instant::now();
        install(&paths, &repo.join("host"), |p| println!("{:>6.1}s {p:?}", started.elapsed().as_secs_f64()))
            .unwrap();

        let supervisor = Supervisor::new();
        let port = free_port().unwrap();
        supervisor.start(host_spec(&paths, &repo, port, "test-key", false)).unwrap();
        let result = wait_running(&supervisor, HOST, HOST_START_TIMEOUT);
        println!("host on :{port} -> {result:?}\n{}", log_tail(&paths.logs().join("host.log"), 15));
        supervisor.stop_all();
        result.unwrap();
    }

    /// Engine install on this machine (~2 GB on a Mac, more on CUDA/ROCm), then ComfyUI and the host
    /// together; passes when the host reports ComfyUI connected.
    /// `cargo test real_engine -- --ignored --nocapture` (after real_install_and_host)
    #[test]
    #[ignore]
    fn real_engine() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
        let paths = Paths { root: std::env::temp_dir().join("glint-runtime-real") };
        let started = Instant::now();
        let report = |p| println!("{:>6.1}s {p:?}", started.elapsed().as_secs_f64());
        setup_engine(&paths, torch_index_for(&scan::scan()), report).unwrap();
        assert!(engine_installed(&paths));

        let supervisor = Supervisor::new();
        let (result, url) = start_engine_and_host(&supervisor, &paths, &repo);
        let body = Command::new("curl").arg("-s").arg(format!("{url}/health")).output().map(|o| String::from_utf8_lossy(&o.stdout).into_owned());
        let log = log_tail(&paths.logs().join("comfyui.log"), 15);
        println!("{:>6.1}s {result:?}\n/health -> {body:?}\n{log}", started.elapsed().as_secs_f64());
        supervisor.stop_all();
        result.unwrap();
        assert!(body.unwrap().contains("\"comfyui_connected\":true"));
    }
    /// Starts ComfyUI and the host (key "test-key") on `paths`; returns the result and the host URL.
    fn start_engine_and_host(supervisor: &Supervisor, paths: &Paths, repo: &Path) -> (Result<(), String>, String) {
        fs::create_dir_all(paths.models()).unwrap();
        fs::create_dir_all(paths.comfyui_data().join("custom_nodes")).unwrap();
        let comfy_port = free_port().unwrap();
        let mut host = host_spec(paths, repo, free_port().unwrap(), "test-key", false);
        let url = host.health_url.clone().unwrap().replace("/livez", "");
        point_host_at_comfyui(&mut host, paths, comfy_port);
        supervisor.start(comfyui_spec(paths, comfy_port)).unwrap();
        supervisor.start(host).unwrap();
        let result = wait_running(supervisor, COMFYUI, COMFYUI_START_TIMEOUT)
            .and_then(|_| wait_running(supervisor, HOST, HOST_START_TIMEOUT));
        (result, url)
    }

    /// Downloads Z-Image Turbo for this machine (~21 GB in bf16) through the host, then makes one
    /// image through its OpenAI endpoint; the response is saved as first-image.json next to the runtime.
    /// `cargo test real_first_image -- --ignored --nocapture` (after real_engine)
    #[test]
    #[ignore]
    fn real_first_image() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..").canonicalize().unwrap();
        let paths = Paths { root: std::env::temp_dir().join("glint-runtime-real") };
        assert!(engine_installed(&paths), "run real_engine first");
        let started = Instant::now();
        write_hardware(&paths).unwrap();
        let supervisor = Supervisor::new();
        let (result, url) = start_engine_and_host(&supervisor, &paths, &repo);
        let curl = |args: &[&str]| {
            let out = Command::new("curl").args(["-s", "-H", "X-API-Key: test-key"]).args(args).output().unwrap();
            String::from_utf8_lossy(&out.stdout).into_owned()
        };
        let download = format!("{url}/api/v1/local-models/z-image-turbo/download");
        if result.is_ok() {
            let mut state = curl(&["-X", "POST", &download]);
            while state.contains("\"downloading\"") {
                println!("{:>6.1}s {state}", started.elapsed().as_secs_f64());
                thread::sleep(Duration::from_secs(30));
                state = curl(&[&download]);
            }
            println!("{:>6.1}s {state}", started.elapsed().as_secs_f64());
        }

        let out = paths.root.join("first-image.json");
        let body = r#"{"model":"z-image-turbo","prompt":"a lighthouse on a cliff at dawn, watercolor","response_format":"b64_json"}"#;
        let generate = result.clone().map(|_| {
            Command::new("curl")
                .args(["-s", "-m", "900", "-H", "X-API-Key: test-key", "-H", "Content-Type: application/json", "-d", body, "-o"])
                .arg(&out)
                .arg(format!("{url}/v1/images/generations"))
                .status()
        });
        println!("{:>6.1}s {result:?} {generate:?}", started.elapsed().as_secs_f64());
        let log = log_tail(&paths.logs().join("comfyui.log"), 15);
        supervisor.stop_all();
        let json = fs::read_to_string(&out).unwrap_or_default();
        assert!(json.contains("\"b64_json\":\"iVBORw0KGgo"), "{}\n{log}", &json[..json.len().min(500)]);
    }
}
