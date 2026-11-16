//! Sharing the host beyond this computer: on the LAN, and through a Cloudflare quick tunnel.
//! The dashboard asks through the host (network.json); a thread here applies it and reports the
//! tunnel back (tunnel.json), so the host never has to start processes itself.

use std::fs;
use std::path::Path;
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::paths::Paths;
use crate::runtime::{self, Runtime};
use crate::supervisor::{ProcessSpec, ProcessStatus, Supervisor};

const TUNNEL: &str = "tunnel";
const CLOUDFLARED_VERSION: &str = "2026.11.0";
/// Release asset, its sha256, and whether it's a .tgz (the others are the bare binary). The sha256 is
/// GitHub's digest of the asset; the release notes hash the binary inside the macOS .tgz instead.
const CLOUDFLARED: &[(&str, &str, &str, &str, bool)] = &[
    ("macos", "aarch64", "cloudflared-darwin-arm64.tgz", "8c2f90277ef06a6080bb40b964d33f09f1188102f8798f2e344174b6cf41accf", true),
    ("macos", "x86_64", "cloudflared-darwin-amd64.tgz", "8a59d51120cebbe902f0a8d6e1b7a7fd4f9a5bb4dcb3e0f05872483e75e7db6b", true),
    ("linux", "x86_64", "cloudflared-linux-amd64", "51f7c68b57ed7269164075d7e789efce5b59355982f06392deeb037d982a51fd", false),
    ("linux", "aarch64", "cloudflared-linux-arm64", "abe54a006acf9faf2d19ee9d953cb0d0804e92f59c5382a162e57b84d086f56f", false),
    ("windows", "x86_64", "cloudflared-windows-amd64.exe", "d5c1d82031da6253dcdcff64c944cd0fe063016edeb2e4fd088075fc9bd52d0a", false),
];

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(default)]
pub struct Wanted {
    pub lan: bool,
    pub tunnel: bool,
}

/// What the dashboard last asked for; off when it never asked or the file is unreadable.
pub fn wanted(paths: &Paths) -> Wanted {
    fs::read(paths.network_file()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

#[derive(Clone, Debug, Default, Serialize, PartialEq)]
struct Report {
    status: &'static str, // off, downloading, starting, running, failed
    url: Option<String>,
    error: Option<String>,
}

/// Starts the thread that applies network.json. The LAN setting carries over between launches; a public
/// link doesn't, so nobody's machine is left open on the internet because they once tried it.
pub fn spawn(app: AppHandle) {
    let Ok(root) = app.path().app_data_dir() else { return };
    let paths = Paths { root };
    let lan = wanted(&paths).lan;
    let _ = write_json(&paths.network_file(), &Wanted { lan, tunnel: false });
    let _ = write_json(&paths.tunnel_file(), &Report { status: "off", ..Default::default() });
    thread::spawn(move || {
        let mut tunnel = Tunnel { report: Report { status: "off", ..Default::default() }, target: None };
        let mut lan_failed: Option<bool> = None;
        loop {
            thread::sleep(Duration::from_secs(2));
            let state = app.state::<Runtime>();
            let Ok(host) = state.host_now() else { continue }; // a start or install is under way
            let want = wanted(&paths);
            if let Some(info) = &host {
                if info.lan != want.lan && lan_failed != Some(want.lan) {
                    match tauri::async_runtime::block_on(runtime::ensure_host(&app)) {
                        Ok(_) => lan_failed = None,
                        Err(e) => {
                            eprintln!("network: couldn't switch the LAN setting: {e}");
                            lan_failed = Some(want.lan); // not again until it's switched back
                        }
                    }
                    continue;
                }
            }
            if lan_failed.is_some_and(|f| f != want.lan) {
                lan_failed = None;
            }
            let before = tunnel.report.clone();
            tunnel.step(&paths, &state.supervisor, want.tunnel, host.map(|h| h.url));
            if tunnel.report != before {
                let _ = write_json(&paths.tunnel_file(), &tunnel.report);
            }
        }
    });
}

struct Tunnel {
    report: Report,
    /// The host URL the running cloudflared forwards to.
    target: Option<String>,
}

impl Tunnel {
    fn step(&mut self, paths: &Paths, supervisor: &Supervisor, on: bool, host: Option<String>) {
        let Some(host) = host.filter(|_| on) else {
            if self.target.take().is_some() {
                supervisor.stop(TUNNEL);
            }
            self.report = Report { status: "off", ..Default::default() };
            return;
        };
        if self.report.status == "failed" {
            return; // until it's switched off and on again
        }
        if self.target.as_deref() != Some(host.as_str()) {
            if let Err(e) = self.start(paths, supervisor, &host) {
                self.report = Report { status: "failed", url: None, error: Some(e) };
            }
            return;
        }
        let log = paths.logs().join("tunnel.log");
        match supervisor.status(TUNNEL) {
            // cloudflared exits when it can't get a link; after a few restarts the supervisor gives up.
            ProcessStatus::Crashed { .. } | ProcessStatus::Stopped => {
                let tail = runtime::log_tail(&log, 4);
                self.report = Report { status: "failed", url: None, error: Some(format!("The tunnel stopped.\n{tail}")) };
            }
            _ => {
                // A restarted cloudflared gets a new link; the newest one in the log is the live one.
                let url = fs::read_to_string(&log).ok().and_then(|t| last_url(&t));
                let status = if url.is_some() { "running" } else { "starting" };
                self.report = Report { status, url, error: None };
            }
        }
    }

    fn start(&mut self, paths: &Paths, supervisor: &Supervisor, host: &str) -> Result<(), String> {
        let bin = paths.cloudflared();
        if !bin.exists() {
            self.report = Report { status: "downloading", ..Default::default() };
            let _ = write_json(&paths.tunnel_file(), &self.report);
            // shortcut: downloads on this thread, so a LAN switch waits for it (~20 MB); move it off if that shows.
            install_cloudflared(paths)?;
        }
        let log = paths.logs().join("tunnel.log");
        let _ = fs::create_dir_all(paths.logs());
        let _ = fs::write(&log, ""); // so the link found is this run's
        supervisor
            .start(ProcessSpec {
                name: TUNNEL.into(),
                program: bin,
                args: ["tunnel", "--no-autoupdate", "--url", host].map(String::from).to_vec(),
                cwd: None,
                env: vec![],
                health_url: None, // running while alive; the link in the log says it's up
                log_file: log,
            })
            .map_err(|e| format!("Couldn't start the tunnel: {e}"))?;
        self.target = Some(host.to_string());
        self.report = Report { status: "starting", ..Default::default() };
        Ok(())
    }
}

fn install_cloudflared(paths: &Paths) -> Result<(), String> {
    let (_, _, asset, sha, tgz) = CLOUDFLARED
        .iter()
        .find(|(os, arch, ..)| *os == std::env::consts::OS && *arch == std::env::consts::ARCH)
        .ok_or("There's no cloudflared build for this system.")?;
    let url = format!("https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/{asset}");
    let file = runtime::fetch(paths, &url, sha, "cloudflared")?;
    let dir = file.parent().unwrap().to_path_buf();
    let found = if *tgz {
        runtime::run(std::process::Command::new(runtime::system_tool("tar")).arg("-xzf").arg(&file).arg("-C").arg(&dir))?;
        runtime::find_file(&dir, "cloudflared").ok_or("The cloudflared archive has no cloudflared binary.")?
    } else {
        file
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&found, fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
    }
    let bin = paths.cloudflared();
    fs::create_dir_all(bin.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::rename(&found, &bin).map_err(|e| e.to_string())?;
    let _ = fs::remove_dir_all(&dir);
    Ok(())
}

/// The last https://<name>.trycloudflare.com in cloudflared's output.
fn last_url(log: &str) -> Option<String> {
    const END: &str = ".trycloudflare.com";
    log.match_indices(END).filter_map(|(i, _)| {
        let start = log[..i].rfind("https://")? + "https://".len();
        let name = &log[start..i];
        (!name.is_empty() && name.bytes().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'-'))
            .then(|| format!("https://{name}{END}"))
    }).last()
}

/// Written whole or not at all, so the host never reads half a file.
fn write_json(path: &Path, value: &impl Serialize) -> std::io::Result<()> {
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_vec(value)?)?;
    fs::rename(tmp, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_the_newest_tunnel_link() {
        let log = "INF Requesting new quick Tunnel on trycloudflare.com...\n\
            INF |  https://calm-river-ab12.trycloudflare.com  |\n\
            ERR see https://www.cloudflare.com/x\n\
            INF |  https://new-one-9.trycloudflare.com  |\n";
        assert_eq!(last_url(log).as_deref(), Some("https://new-one-9.trycloudflare.com"));
        assert_eq!(last_url("Requesting new quick Tunnel on trycloudflare.com"), None);
        assert_eq!(last_url("https://evil.com/?x=a.trycloudflare.com"), None);
    }

    #[test]
    fn a_cloudflared_build_for_each_release_platform() {
        for (os, arch) in [("macos", "aarch64"), ("macos", "x86_64"), ("linux", "x86_64"), ("windows", "x86_64")] {
            let found = CLOUDFLARED.iter().find(|(o, a, ..)| *o == os && *a == arch).unwrap();
            assert_eq!(found.3.len(), 64);
        }
    }

    #[test]
    fn wanted_reads_what_the_host_wrote() {
        let root = std::env::temp_dir().join(format!("glint-network-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        let paths = Paths { root: root.clone() };
        assert_eq!(wanted(&paths), Wanted::default());
        write_json(&paths.network_file(), &Wanted { lan: true, tunnel: false }).unwrap();
        assert_eq!(wanted(&paths), Wanted { lan: true, tunnel: false });
        fs::write(paths.network_file(), "{\"tunnel\": true}").unwrap();
        assert_eq!(wanted(&paths), Wanted { lan: false, tunnel: true });
        fs::write(paths.network_file(), "garbage").unwrap();
        assert_eq!(wanted(&paths), Wanted::default());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_tunnel_stays_off_without_a_host() {
        let root = std::env::temp_dir().join(format!("glint-tunnel-{}", std::process::id()));
        let paths = Paths { root: root.clone() };
        let supervisor = Supervisor::new();
        let mut t = Tunnel { report: Report { status: "failed", ..Default::default() }, target: None };
        t.step(&paths, &supervisor, true, None);
        assert_eq!(t.report.status, "off"); // and a failure is forgotten once it's off
        t.step(&paths, &supervisor, false, Some("http://127.0.0.1:1".into()));
        assert_eq!(t.report.status, "off");
        assert_eq!(supervisor.status(TUNNEL), ProcessStatus::Stopped);
    }

    /// Downloads cloudflared and opens a real quick tunnel to GLINT_TUNNEL_TARGET; prints the link.
    /// `cargo test --lib real_tunnel -- --ignored --nocapture`
    #[test]
    #[ignore = "downloads cloudflared and opens a public link"]
    fn real_tunnel() {
        let target = std::env::var("GLINT_TUNNEL_TARGET").unwrap_or("http://127.0.0.1:7788".into());
        let paths = Paths { root: std::env::temp_dir().join("glint-tunnel-real") };
        let supervisor = Supervisor::new();
        let mut t = Tunnel { report: Report::default(), target: None };
        for _ in 0..60 {
            t.step(&paths, &supervisor, true, Some(target.clone()));
            println!("{:?}", t.report);
            if t.report.status == "running" || t.report.status == "failed" {
                break;
            }
            thread::sleep(Duration::from_secs(2));
        }
        assert_eq!(t.report.status, "running", "{:?}", t.report.error);
        println!("TUNNEL_URL={}", t.report.url.as_deref().unwrap());
        let hold = std::env::var("GLINT_TUNNEL_HOLD_SECS").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
        thread::sleep(Duration::from_secs(hold));
        t.step(&paths, &supervisor, false, Some(target));
        assert_eq!(supervisor.status(TUNNEL), ProcessStatus::Stopped);
    }
}
