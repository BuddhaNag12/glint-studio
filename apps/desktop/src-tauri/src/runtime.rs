//! First-run install of the core runtime (uv, Python 3.12, a venv, the host's dependencies) and
//! starting the host. Every install step is safe to re-run: finished work is skipped or is a fast no-op.
//! The host's own code ships inside the app (bundle resources); only its dependencies come from PyPI.

use std::fs;
use std::io;
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

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Step {
    Uv,
    Python,
    Venv,
    Host,
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

#[tauri::command]
pub async fn start_host(app: AppHandle, state: State<'_, Runtime>) -> Result<HostInfo, String> {
    let mut host = state.host.lock().await;
    let supervisor = state.supervisor.clone();
    if let Some(info) = host.as_ref() {
        if supervisor.status(HOST) == ProcessStatus::Running {
            return Ok(info.clone());
        }
    }
    let paths = app_paths(&app)?;
    if !paths.venv_python().exists() {
        return Err("The runtime isn't installed yet.".into());
    }
    for dir in [paths.outputs(), paths.styles(), paths.workflows()] {
        fs::create_dir_all(&dir).map_err(|e| format!("Couldn't create {}: {e}", dir.display()))?;
    }
    let port = free_port().map_err(|e| e.to_string())?;
    let info = HostInfo { url: format!("http://127.0.0.1:{port}"), api_key: new_key()? };
    let spec = host_spec(&paths, &resource_dir(&app)?, port, &info.api_key);
    let log = spec.log_file.clone();
    supervisor.start(spec).map_err(|e| format!("Couldn't start the host: {e}"))?;
    tauri::async_runtime::spawn_blocking(move || wait_running(&supervisor, HOST, HOST_START_TIMEOUT))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| format!("{e}\n{}", log_tail(&log, 15)))?;
    *host = Some(info.clone());
    Ok(info)
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
        }
        report(Progress { step, done: true });
    }
    Ok(())
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
    let tmp = paths.runtime().join("download");
    let _ = fs::remove_dir_all(&tmp);
    fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    let archive = tmp.join(&name);

    // shortcut: system curl and tar (in Windows 10+, macOS and desktop Linux) instead of an HTTP
    // and archive crate; switch when download.rs brings an HTTP client with progress and resume.
    run(Command::new(system_tool("curl")).args(["-fsSL", "--retry", "3", "-o"]).arg(&archive).arg(&url))?;
    let got = sha256_file(&archive).map_err(|e| e.to_string())?;
    if got != sha {
        return Err(format!("The uv download is corrupt (sha256 {got}, expected {sha}). Try again."));
    }
    run(Command::new(system_tool("tar")).arg("-xf").arg(&archive).arg("-C").arg(&tmp))?;
    let found = find_file(&tmp, &exe("uv")).ok_or("The uv archive has no uv binary.")?;
    fs::create_dir_all(uv.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::rename(&found, &uv).map_err(|e| e.to_string())?;
    let _ = fs::remove_dir_all(&tmp);
    Ok(())
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
fn system_tool(name: &str) -> PathBuf {
    if cfg!(windows) {
        let root = std::env::var_os("SystemRoot").unwrap_or_else(|| "C:\\Windows".into());
        PathBuf::from(root).join("System32").join(exe(name))
    } else {
        PathBuf::from(name)
    }
}

/// Runs to completion; on failure the error carries the tool's last lines of stderr.
fn run(cmd: &mut Command) -> Result<(), String> {
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

fn sha256_file(path: &Path) -> io::Result<String> {
    let mut hasher = Sha256::new();
    io::copy(&mut fs::File::open(path)?, &mut hasher)?;
    Ok(hex(&hasher.finalize()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn find_file(dir: &Path, name: &str) -> Option<PathBuf> {
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

fn new_key() -> Result<String, String> {
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

/// `resources` holds `host/` (the app package and pyproject.toml) and `catalog/`.
pub fn host_spec(paths: &Paths, resources: &Path, port: u16, api_key: &str) -> ProcessSpec {
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
        ("PYTHONDONTWRITEBYTECODE", "1".into()), // the app bundle is read-only
        ("PYTHONUNBUFFERED", "1".into()),        // logs reach host.log as they happen
    ];
    ProcessSpec {
        name: HOST.into(),
        program: paths.venv_python(),
        args: ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", &port.to_string()]
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
    loop {
        match supervisor.status(name) {
            ProcessStatus::Running => return Ok(()),
            ProcessStatus::Crashed { .. } | ProcessStatus::Stopped => {
                return Err("The host stopped while starting.".into());
            }
            _ if started.elapsed() > timeout => return Err("The host didn't start in time.".into()),
            _ => thread::sleep(Duration::from_millis(250)),
        }
    }
}

fn log_tail(path: &Path, n: usize) -> String {
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
    fn keys_are_random_hex() {
        let (a, b) = (new_key().unwrap(), new_key().unwrap());
        assert_eq!(a.len(), 64);
        assert!(a.bytes().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, b);
    }

    #[test]
    fn host_spec_points_at_app_data_and_resources() {
        let paths = Paths { root: PathBuf::from("/data") };
        let spec = host_spec(&paths, Path::new("/res"), 4321, "k");
        let env = |k: &str| spec.env.iter().find(|(key, _)| key == k).map(|(_, v)| v.as_str());
        assert_eq!(env("API_KEY"), Some("k"));
        assert_eq!(env("PUBLIC_BASE_URL"), Some("http://127.0.0.1:4321"));
        assert!(env("CORS_ORIGINS").unwrap().contains("tauri://localhost"));
        assert_eq!(env("CATALOG_DIR").map(PathBuf::from), Some(PathBuf::from("/res").join("catalog")));
        assert_eq!(spec.cwd, Some(PathBuf::from("/res").join("host")));
        assert_eq!(spec.health_url.as_deref(), Some("http://127.0.0.1:4321/livez"));
        assert!(spec.args.contains(&"4321".to_string()));
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
        supervisor.start(host_spec(&paths, &repo, port, "test-key")).unwrap();
        let result = wait_running(&supervisor, HOST, HOST_START_TIMEOUT);
        println!("host on :{port} -> {result:?}\n{}", log_tail(&paths.logs().join("host.log"), 15));
        supervisor.stop_all();
        result.unwrap();
    }
}
