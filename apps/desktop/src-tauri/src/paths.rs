//! Where the app keeps its data: one folder under the OS app-data directory
//! (macOS ~/Library/Application Support/dev.glintstudio.app, Windows %APPDATA%\dev.glintstudio.app,
//! Linux ~/.local/share/dev.glintstudio.app).

use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct Paths {
    pub root: PathBuf,
}

impl Paths {
    /// uv, Python, the venv and (later) ComfyUI. "Repair runtime" can wipe this alone.
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
    pub fn logs(&self) -> PathBuf {
        self.root.join("logs")
    }
}

/// `uv` -> `uv.exe` on Windows.
pub fn exe(name: &str) -> String {
    if cfg!(windows) { format!("{name}.exe") } else { name.to_string() }
}
