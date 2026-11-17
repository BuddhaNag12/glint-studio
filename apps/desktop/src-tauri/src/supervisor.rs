//! Runs the host and ComfyUI as child processes: logs to files, health checks,
//! restarts with backoff, and stops everything on request.

use std::collections::{HashMap, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

/// Give up after this many restarts inside the restart window.
const MAX_RESTARTS: usize = 5;
const RESTART_WINDOW: Duration = Duration::from_secs(120);
const HEALTH_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Debug)]
pub struct ProcessSpec {
    pub name: String,
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: Option<PathBuf>,
    pub env: Vec<(String, String)>,
    /// `http://127.0.0.1:PORT/path`; a 200 answer means healthy.
    pub health_url: Option<String>,
    pub log_file: PathBuf,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProcessStatus {
    Stopped,
    /// Spawned (or waiting to be respawned) and not yet healthy.
    Starting,
    Running,
    /// Was healthy once, now fails its health check.
    Unhealthy,
    /// Gave up after too many restarts.
    Crashed { exit_code: Option<i32>, restarts: u32 },
}

struct Managed {
    child: Option<Child>,
    status: ProcessStatus,
    stopping: bool,
}

/// Cheap to clone; every clone sees the same processes. Call `stop_all` when the app quits.
#[derive(Clone)]
pub struct Supervisor {
    procs: Arc<Mutex<HashMap<String, Arc<Mutex<Managed>>>>>,
    /// Poll interval and first backoff step; the backoff cap is 30x this.
    base: Duration,
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl Supervisor {
    pub fn new() -> Self {
        Self::with_base(Duration::from_secs(1))
    }

    /// A shorter `base` makes tests fast.
    pub fn with_base(base: Duration) -> Self {
        Supervisor { procs: Arc::new(Mutex::new(HashMap::new())), base }
    }

    /// Starts `spec`, first stopping any process already running under the same name.
    pub fn start(&self, spec: ProcessSpec) -> io::Result<()> {
        self.stop(&spec.name);
        let child = spawn(&spec)?;
        let entry = Arc::new(Mutex::new(Managed {
            child: Some(child),
            status: ProcessStatus::Starting,
            stopping: false,
        }));
        let old = lock(&self.procs).insert(spec.name.clone(), Arc::clone(&entry));
        if let Some(old) = old {
            // Another start() of the same name raced us; keep only ours.
            shutdown(&old);
        }
        let base = self.base;
        thread::spawn(move || watch(entry, spec, base));
        Ok(())
    }

    pub fn stop(&self, name: &str) {
        let entry = lock(&self.procs).remove(name);
        if let Some(entry) = entry {
            shutdown(&entry);
        }
    }

    pub fn stop_all(&self) {
        let entries: Vec<Arc<Mutex<Managed>>> = lock(&self.procs).drain().map(|(_, e)| e).collect();
        for entry in entries {
            shutdown(&entry);
        }
    }

    pub fn status(&self, name: &str) -> ProcessStatus {
        let entry = lock(&self.procs).get(name).cloned();
        match entry {
            Some(entry) => lock(&entry).status.clone(),
            None => ProcessStatus::Stopped,
        }
    }
}

/// A port that was free a moment ago on 127.0.0.1.
// shortcut: another process can grab the port between this call and the child binding it; retry the start if that bites.
pub fn free_port() -> io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

/// 1x, 2x, 4x ... `base`, capped at 30x `base` (1s, 2s, 4s ... 30s by default).
fn backoff(base: Duration, attempt: u32) -> Duration {
    base.saturating_mul(1u32 << attempt.min(16)).min(base.saturating_mul(30))
}

/// "HTTP/1.1 200 OK" -> Some(200).
fn parse_status_line(line: &str) -> Option<u16> {
    let mut parts = line.split_whitespace();
    if !parts.next()?.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

/// "http://127.0.0.1:8000/health" -> ("127.0.0.1:8000", "/health").
fn split_http_url(url: &str) -> Option<(&str, &str)> {
    let rest = url.strip_prefix("http://")?;
    Some(match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    })
}

/// The status code of a plain HTTP/1.1 GET, or None if anything fails.
fn http_status(url: &str, timeout: Duration) -> Option<u16> {
    let (host, path) = split_http_url(url)?;
    let addr = host.to_socket_addrs().ok()?.next()?;
    let mut stream = TcpStream::connect_timeout(&addr, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;
    let request = format!("GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 256];
    while !buf.contains(&b'\n') && buf.len() < 1024 {
        let n = stream.read(&mut chunk).ok()?;
        if n == 0 {
            break;
        }
        buf.extend_from_slice(&chunk[..n]);
    }
    let text = String::from_utf8_lossy(&buf);
    let line = text.lines().next()?;
    parse_status_line(line)
}

/// A JSON request to the local host with its API key; None on any failure or a non-2xx status.
/// shortcut: plain HTTP/1.1 read to the end, which suits the host's small JSON replies (they carry a
/// Content-Length, never chunked); use an HTTP crate if this ever talks to anything else.
pub fn http_json(method: &str, url: &str, key: &str, body: Option<&str>) -> Option<serde_json::Value> {
    let (host, path) = split_http_url(url)?;
    let timeout = Duration::from_secs(5);
    let mut stream = TcpStream::connect_timeout(&host.to_socket_addrs().ok()?.next()?, timeout).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    let body = body.unwrap_or("");
    let request = format!(
        "{method} {path} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {key}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).ok()?;
    let mut reply = Vec::new();
    stream.read_to_end(&mut reply).ok()?;
    let text = String::from_utf8_lossy(&reply);
    let (head, body) = text.split_once("\r\n\r\n")?;
    let status = parse_status_line(head.lines().next()?)?;
    if !(200..300).contains(&status) {
        return None;
    }
    serde_json::from_str(body).ok()
}

fn spawn(spec: &ProcessSpec) -> io::Result<Child> {
    if let Some(dir) = spec.log_file.parent() {
        fs::create_dir_all(dir)?;
    }
    let out = OpenOptions::new().create(true).append(true).open(&spec.log_file)?;
    let err = out.try_clone()?;
    let mut cmd = Command::new(&spec.program);
    cmd.args(&spec.args)
        .stdin(Stdio::null())
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err));
    for (key, value) in &spec.env {
        cmd.env(key, value);
    }
    if let Some(cwd) = &spec.cwd {
        cmd.current_dir(cwd);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console pops up from the GUI app
    }
    cmd.spawn()
}

fn shutdown(entry: &Mutex<Managed>) {
    let child = {
        let mut m = lock(entry);
        m.stopping = true;
        m.status = ProcessStatus::Stopped;
        m.child.take()
    };
    if let Some(mut child) = child {
        // shortcut: hard kill (std has no SIGTERM) and only the direct child, not its process tree;
        // send SIGTERM / kill the job object if the host or ComfyUI ever needs a clean shutdown or spawns workers.
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// One per process: polls exit and health every `base`, restarts with backoff, gives up after
/// MAX_RESTARTS inside the window. Exits as soon as it sees `stopping`.
// shortcut: a process that hangs but stays alive is reported Unhealthy, not restarted; kill it after N failed checks if that happens.
fn watch(entry: Arc<Mutex<Managed>>, spec: ProcessSpec, base: Duration) {
    let mut recent: VecDeque<Instant> = VecDeque::new();
    let mut total_restarts: u32 = 0;
    let mut was_healthy = false;
    loop {
        thread::sleep(base);

        // Some(code) once the child is gone; None while it runs.
        let exited: Option<Option<i32>> = {
            let mut m = lock(&entry);
            if m.stopping {
                return;
            }
            let exited = match m.child.as_mut() {
                None => Some(None), // the last respawn failed
                Some(child) => match child.try_wait() {
                    Ok(None) => None,
                    Ok(Some(status)) => Some(status.code()),
                    Err(_) => {
                        let _ = child.kill();
                        Some(None)
                    }
                },
            };
            exited
        };

        let Some(exit_code) = exited else {
            let status = match &spec.health_url {
                None => ProcessStatus::Running,
                Some(url) => {
                    let ok = http_status(url, HEALTH_TIMEOUT) == Some(200);
                    was_healthy |= ok;
                    if ok {
                        ProcessStatus::Running
                    } else if was_healthy {
                        ProcessStatus::Unhealthy
                    } else {
                        ProcessStatus::Starting
                    }
                }
            };
            let mut m = lock(&entry);
            if m.stopping {
                return;
            }
            m.status = status;
            continue;
        };

        let now = Instant::now();
        while recent.front().is_some_and(|t| now.duration_since(*t) > RESTART_WINDOW) {
            recent.pop_front();
        }
        if recent.len() >= MAX_RESTARTS {
            let mut m = lock(&entry);
            if !m.stopping {
                m.status = ProcessStatus::Crashed { exit_code, restarts: total_restarts };
            }
            return;
        }

        {
            let mut m = lock(&entry);
            if m.stopping {
                return;
            }
            m.status = ProcessStatus::Starting;
        }
        thread::sleep(backoff(base, recent.len() as u32));
        recent.push_back(Instant::now());
        total_restarts += 1;
        was_healthy = false;

        // Spawn under the lock so stop() either sees the new child or we see `stopping`.
        let mut m = lock(&entry);
        if m.stopping {
            return;
        }
        m.child = spawn(&spec).ok();
    }
}

/// Lock that survives a panicked holder; the data stays usable.
fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_line_parsing() {
        assert_eq!(parse_status_line("HTTP/1.1 200 OK"), Some(200));
        assert_eq!(parse_status_line("HTTP/1.0 503 Service Unavailable"), Some(503));
        assert_eq!(parse_status_line("HTTP/1.1 404"), Some(404));
        assert_eq!(parse_status_line("SSH-2.0-OpenSSH"), None);
        assert_eq!(parse_status_line("HTTP/1.1 abc OK"), None);
        assert_eq!(parse_status_line(""), None);
    }

    #[test]
    fn url_splitting() {
        assert_eq!(split_http_url("http://127.0.0.1:8000/health"), Some(("127.0.0.1:8000", "/health")));
        assert_eq!(split_http_url("http://127.0.0.1:8000"), Some(("127.0.0.1:8000", "/")));
        assert_eq!(split_http_url("https://127.0.0.1:8000/health"), None);
    }

    #[test]
    #[ignore = "needs a host running on 127.0.0.1:7788"]
    fn http_json_talks_to_a_running_host() {
        let health = http_json("GET", "http://127.0.0.1:7788/health", "", None).expect("health");
        assert!(health.is_object());
        assert_eq!(http_json("GET", "http://127.0.0.1:7788/api/v1/system", "wrong-key", None), None); // 401
    }

    #[test]
    fn backoff_schedule() {
        let s = Duration::from_secs(1);
        let got: Vec<u64> = (0..8).map(|n| backoff(s, n).as_secs()).collect();
        assert_eq!(got, vec![1, 2, 4, 8, 16, 30, 30, 30]);
        assert_eq!(backoff(s, 1000), Duration::from_secs(30));
    }

    #[test]
    fn free_port_is_a_real_port() {
        // Not re-bound here: a parallel test may take the same port first (AddrInUse on CI).
        assert_ne!(free_port().unwrap(), 0);
    }

    #[test]
    fn http_status_reads_a_real_response() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut req: Vec<u8> = Vec::new();
            let mut chunk = [0u8; 256];
            // Read the whole request so closing doesn't reset the connection.
            while !req.windows(4).any(|w| w == &b"\r\n\r\n"[..]) {
                let n = s.read(&mut chunk).unwrap();
                if n == 0 {
                    break;
                }
                req.extend_from_slice(&chunk[..n]);
            }
            s.write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n").unwrap();
            String::from_utf8_lossy(&req).into_owned()
        });
        let url = format!("http://127.0.0.1:{port}/health");
        assert_eq!(http_status(&url, Duration::from_secs(5)), Some(503));
        assert!(server.join().unwrap().starts_with("GET /health HTTP/1.1\r\n"));
    }

    #[test]
    fn http_status_none_when_nothing_listens() {
        let port = free_port().unwrap();
        let url = format!("http://127.0.0.1:{port}/health");
        assert_eq!(http_status(&url, Duration::from_millis(500)), None);
    }

    fn spec(name: &str, program: &str, args: &[&str], dir: &std::path::Path) -> ProcessSpec {
        ProcessSpec {
            name: name.to_string(),
            program: PathBuf::from(program),
            args: args.iter().map(|a| a.to_string()).collect(),
            cwd: None,
            env: vec![("GLINT_TEST".to_string(), "1".to_string())],
            health_url: None,
            log_file: dir.join("logs").join(format!("{name}.log")),
        }
    }

    fn test_dir(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("glint-supervisor-{tag}-{}", std::process::id()))
    }

    #[test]
    fn crashing_process_is_restarted_then_reported() {
        let dir = test_dir("crash");
        let s = if cfg!(windows) {
            spec("exit3", "cmd", &["/C", "exit", "3"], &dir)
        } else {
            spec("exit3", "sh", &["-c", "exit 3"], &dir)
        };
        let log = s.log_file.clone();
        let sup = Supervisor::with_base(Duration::from_millis(10));
        sup.start(s).unwrap();

        let deadline = Instant::now() + Duration::from_secs(20);
        let mut status = sup.status("exit3");
        while !matches!(status, ProcessStatus::Crashed { .. }) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
            status = sup.status("exit3");
        }
        assert_eq!(status, ProcessStatus::Crashed { exit_code: Some(3), restarts: MAX_RESTARTS as u32 });
        assert!(log.exists());
        sup.stop_all();
        assert_eq!(sup.status("exit3"), ProcessStatus::Stopped);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn stop_kills_a_running_process() {
        let dir = test_dir("stop");
        let s = if cfg!(windows) {
            spec("long", "ping", &["-n", "30", "127.0.0.1"], &dir)
        } else {
            spec("long", "sleep", &["30"], &dir)
        };
        let sup = Supervisor::with_base(Duration::from_millis(10));
        sup.start(s).unwrap();

        let deadline = Instant::now() + Duration::from_secs(10);
        while sup.status("long") != ProcessStatus::Running && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(sup.status("long"), ProcessStatus::Running);

        let started = Instant::now();
        sup.stop("long");
        assert!(started.elapsed() < Duration::from_secs(5));
        assert_eq!(sup.status("long"), ProcessStatus::Stopped);
        let _ = fs::remove_dir_all(&dir);
    }
}
