//! Downloads a model's files into the models folder. Each file goes to `<dest>.part`, resumes
//! after an interruption, and is renamed into place only once its sha256 matches the catalog.

use crate::paths::Paths;
use crate::recommend::{self, Variant};
use crate::runtime::{sha256_file, system_tool};
use crate::scan;
use serde::Serialize;
use std::ffi::OsString;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use tauri::ipc::Channel;
use tauri::{AppHandle, Manager};

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct DownloadProgress {
    pub done: u64,
    pub total: u64,
    /// Checking the sha256 of a finished file (a few seconds per GB).
    pub verifying: bool,
}

// shortcut: one download at a time and no cancel button; add a queue when there are more models.
static BUSY: AtomicBool = AtomicBool::new(false);
/// The running curl, so `stop` can end it when the app quits instead of leaving it writing.
static CURL: Mutex<Option<Child>> = Mutex::new(None);

#[tauri::command]
pub async fn download_model(app: AppHandle, id: String, on_progress: Channel<DownloadProgress>) -> Result<(), String> {
    if BUSY.swap(true, Ordering::SeqCst) {
        return Err("Another download is running.".into());
    }
    let catalog = recommend::catalog_dir(&app);
    let root = app.path().app_data_dir().map_err(|e| e.to_string());
    let result = tauri::async_runtime::spawn_blocking(move || {
        let models = recommend::load_catalog(&catalog?)?;
        let model = models.iter().find(|m| m.id == id).ok_or(format!("No model '{id}' in the catalog."))?;
        let variant = recommend::chosen_variant(&scan::scan(), model).ok_or("This model has nothing to download.")?;
        download_variant(&Paths { root: root? }.models(), variant, |p| {
            let _ = on_progress.send(p);
        })
    })
    .await
    .map_err(|e| e.to_string())
    .and_then(|r| r);
    BUSY.store(false, Ordering::SeqCst);
    result
}

/// Ends a running download; the .part file stays and the next download resumes it.
pub fn stop() {
    if let Some(child) = CURL.lock().unwrap().as_mut() {
        let _ = child.kill();
    }
}

pub fn download_variant(models_dir: &Path, v: &Variant, report: impl Fn(DownloadProgress)) -> Result<(), String> {
    let total = v.files.iter().map(|f| f.bytes).sum();
    let mut before = 0;
    for f in &v.files {
        let dest = dest_path(models_dir, &f.dest)?;
        if fs::metadata(&dest).is_ok_and(|m| m.len() == f.bytes) {
            before += f.bytes;
            continue;
        }
        fs::create_dir_all(dest.parent().unwrap()).map_err(|e| e.to_string())?;
        let mut part = OsString::from(&dest);
        part.push(".part");
        let part = PathBuf::from(part);
        let size = || fs::metadata(&part).map_or(0, |m| m.len());
        if size() < f.bytes {
            fetch(&f.url, &part, || report(DownloadProgress { done: before + size(), total, verifying: false }))?;
        }
        if size() != f.bytes {
            let got = size();
            let _ = fs::remove_file(&part);
            return Err(format!("{} came out at {got} bytes, expected {}. Try again.", f.dest, f.bytes));
        }
        report(DownloadProgress { done: before + f.bytes, total, verifying: true });
        let got = sha256_file(&part).map_err(|e| e.to_string())?;
        if got != f.sha256 {
            let _ = fs::remove_file(&part);
            return Err(format!("{} is corrupt (sha256 {got}, expected {}). Try again.", f.dest, f.sha256));
        }
        fs::rename(&part, &dest).map_err(|e| e.to_string())?;
        before += f.bytes;
    }
    report(DownloadProgress { done: total, total, verifying: false });
    Ok(())
}

/// Runs curl, resuming `part`, and calls `tick` twice a second until it exits.
// shortcut: no Hugging Face token, so gated models (requires_hf_token) can't download yet;
// add it from the keychain when the catalog has one.
fn fetch(url: &str, part: &Path, tick: impl Fn()) -> Result<(), String> {
    let mut cmd = Command::new(system_tool("curl"));
    cmd.args(["-fsSL", "--retry", "5", "--retry-delay", "2", "-C", "-", "-o"]).arg(part).arg(url);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let child = cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn();
    *CURL.lock().unwrap() = Some(child.map_err(|e| format!("Couldn't run curl: {e}"))?);
    let status = loop {
        let exited = CURL.lock().unwrap().as_mut().unwrap().try_wait().map_err(|e| e.to_string())?;
        if let Some(status) = exited {
            break status;
        }
        tick();
        std::thread::sleep(Duration::from_millis(500));
    };
    let out = CURL.lock().unwrap().take().unwrap().wait_with_output().map_err(|e| e.to_string())?;
    if status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    Err(format!("The download stopped ({status}): {}\nPress Download again to resume.", stderr.trim()))
}

/// `dest` comes from the catalog; keep it inside the models folder.
fn dest_path(models_dir: &Path, dest: &str) -> Result<PathBuf, String> {
    let rel = Path::new(dest);
    let plain = rel.components().all(|c| matches!(c, Component::Normal(_))) && rel.components().count() == 2;
    if !plain {
        return Err(format!("The catalog names a bad file path: {dest}"));
    }
    Ok(models_dir.join(rel))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::recommend::ModelFile;
    use std::cell::RefCell;

    fn test_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("glint-download-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn dest_stays_inside_the_models_folder() {
        let models = Path::new("/m");
        assert_eq!(dest_path(models, "vae/ae.safetensors").unwrap(), models.join("vae").join("ae.safetensors"));
        for bad in ["../x.safetensors", "vae/../../x", "/etc/passwd", "x.safetensors", "a/b/c"] {
            assert!(dest_path(models, bad).is_err(), "{bad}");
        }
    }

    /// A file:// URL exercises curl, the resume, the sha256 check and the rename without a network.
    #[test]
    fn downloads_resumes_and_checks_the_sha() {
        let dir = test_dir("sha");
        let src = dir.join("source.bin");
        let body = b"glint test model".repeat(1000);
        fs::write(&src, &body).unwrap();
        let sha = sha256_file(&src).unwrap();
        let path = src.display().to_string().replace('\\', "/");
        let url = if path.starts_with('/') { format!("file://{path}") } else { format!("file:///{path}") };
        let models = dir.join("models");
        let file = |sha: &str| ModelFile { url: url.clone(), sha256: sha.into(), bytes: body.len() as u64, dest: "vae/t.safetensors".into() };
        let variant = |sha: &str| Variant { precision: "bf16".into(), min_vram_gb: 0.0, files: vec![file(sha)] };

        // A half-finished .part from an earlier run is resumed, not restarted.
        fs::create_dir_all(models.join("vae")).unwrap();
        fs::write(models.join("vae/t.safetensors.part"), &body[..body.len() / 2]).unwrap();
        let seen = RefCell::new(vec![]);
        download_variant(&models, &variant(&sha), |p| seen.borrow_mut().push(p)).unwrap();
        assert_eq!(fs::read(models.join("vae/t.safetensors")).unwrap(), body);
        assert!(!models.join("vae/t.safetensors.part").exists());
        assert_eq!(seen.borrow().last().unwrap().done, body.len() as u64);

        // Wrong sha: the file is thrown away, not put in place.
        fs::remove_file(models.join("vae/t.safetensors")).unwrap();
        let err = download_variant(&models, &variant(&"0".repeat(64)), |_| {}).unwrap_err();
        assert!(err.contains("corrupt"), "{err}");
        assert!(!models.join("vae/t.safetensors").exists() && !models.join("vae/t.safetensors.part").exists());
    }
}
