//! Where the app keeps its data: one folder under the OS app-data directory
//! (macOS ~/Library/Application Support/dev.glintstudio.app, Windows %APPDATA%\dev.glintstudio.app,
//! Linux ~/.local/share/dev.glintstudio.app).

use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct Paths {
    pub root: PathBuf,
}

impl Paths {
    /// uv, Python, the venv and ComfyUI's code. "Repair runtime" can wipe this alone.
    pub fn runtime(&self) -> PathBuf {
        self.root.join("runtime")
    }
    pub fn uv(&self) -> PathBuf {
        self.runtime().join("bin").join(exe("uv"))
    }
    pub fn venv(&self) -> PathBuf {
        self.runtime().join("venv")
    }
    pub fn venv_python(&self) -> PathBuf {
        if cfg!(windows) {
            self.venv().join("Scripts").join("python.exe")
        } else {
            self.venv().join("bin").join("python")
        }
    }
    /// ComfyUI's base directory (custom_nodes, input, output, temp, user); outside runtime so a
    /// repair keeps it.
    pub fn comfyui_data(&self) -> PathBuf {
        self.root.join("comfyui")
    }
    /// Model files, shared by every engine version.
    pub fn models(&self) -> PathBuf {
        self.root.join("models")
    }
    pub fn outputs(&self) -> PathBuf {
        self.root.join("outputs")
    }
    pub fn styles(&self) -> PathBuf {
        self.root.join("styles")
    }
    pub fn workflows(&self) -> PathBuf {
        self.root.join("workflows")
    }
    /// Remote image APIs the host offers (keys are in the OS keychain, not here).
    pub fn providers_file(&self) -> PathBuf {
        self.root.join("providers.json")
    }
    /// The app's hardware scan, for the host's model recommendations.
    pub fn hardware_file(&self) -> PathBuf {
        self.root.join("hardware.json")
    }
    /// The host's job history, for the dashboard.
    pub fn stats_db(&self) -> PathBuf {
        self.root.join("stats.db")
    }
    /// Extra API keys (hashed) that the dashboard makes for other apps and phones.
    pub fn keys_file(&self) -> PathBuf {
        self.root.join("keys.json")
    }
    /// What the dashboard asks to share ({"lan", "tunnel"}); the host writes it, the app applies it.
    pub fn network_file(&self) -> PathBuf {
        self.root.join("network.json")
    }
    /// The app's report on the public link, for the host to show.
    pub fn tunnel_file(&self) -> PathBuf {
        self.root.join("tunnel.json")
    }
    pub fn cloudflared(&self) -> PathBuf {
        self.runtime().join("bin").join(exe("cloudflared"))
    }
    pub fn logs(&self) -> PathBuf {
        self.root.join("logs")
    }
}

/// `uv` -> `uv.exe` on Windows.
pub fn exe(name: &str) -> String {
    if cfg!(windows) { format!("{name}.exe") } else { name.to_string() }
}
