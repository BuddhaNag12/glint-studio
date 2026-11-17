//! "Erase all data and quit", from the dashboard's Settings. The dashboard is a web page and can't call
//! the app, so the host drops `erase_requested` in app data (host/app/net_settings.py); a thread here
//! sees it, stops the host and ComfyUI, deletes the keychain entries and the app-data folder, and quits.

use std::ffi::OsStr;
use std::fs;
use std::path::Path;
use std::thread;
use std::time::Duration;

use tauri::{AppHandle, Manager};
use tauri_plugin_opener::OpenerExt;

use crate::paths::Paths;
use crate::runtime::Runtime;
use crate::secrets;

const REQUEST: &str = "erase_requested";
/// tauri.conf.json's identifier: app_data_dir always ends in it.
const APP_FOLDER: &str = "dev.glintstudio.app";

pub fn spawn(app: AppHandle) {
    let Ok(root) = app.path().app_data_dir() else { return };
    let paths = Paths { root };
    let _ = fs::remove_file(paths.root.join(REQUEST)); // only a request made while the app runs counts
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(1));
        if !paths.root.join(REQUEST).exists() {
            continue;
        }
        // shortcut: the network thread may still restart the host in the moment before exit (a LAN switch
        // it reads from the deleted network.json); the start fails without the venv. Hold Runtime's locks if
        // that ever leaves files behind.
        app.state::<Runtime>().supervisor.stop_all();
        // Keys first: which providers have one is in providers.json, about to go.
        if let Ok(resources) = app.path().resource_dir() {
            secrets::erase_keys(&paths, &resources);
        }
        if let Err(e) = erase(&paths.root) {
            eprintln!("erase: {e}");
            // Show what's left so it can be deleted by hand.
            let _ = app.opener().open_path(paths.root.to_string_lossy(), None::<&str>);
        }
        app.exit(0);
        return;
    });
}

/// Deletes the app-data folder and everything in it. Refuses any other folder: it must be an absolute
/// path to a real folder (not a link) named after the app, so a wrong root (home, "/") is never wiped.
/// Links inside are removed, not followed, so model folders linked from another ComfyUI stay.
fn erase(root: &Path) -> Result<(), String> {
    let ours = root.is_absolute()
        && root.file_name() == Some(OsStr::new(APP_FOLDER))
        && fs::symlink_metadata(root).is_ok_and(|m| m.is_dir());
    if !ours {
        return Err(format!("{} isn't Glint Studio's data folder; nothing was deleted.", root.display()));
    }
    fs::remove_dir_all(root).map_err(|e| format!("Couldn't delete everything in {}: {e}", root.display()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn erase_deletes_only_the_app_data_folder() {
        let tmp = std::env::temp_dir().join(format!("glint-erase-{}", std::process::id()));
        let _ = fs::remove_dir_all(&tmp);
        let root = tmp.join(APP_FOLDER);
        let paths = Paths { root: root.clone() };
        for dir in [paths.runtime(), paths.models().join("checkpoints"), paths.outputs(), paths.styles(), paths.logs()] {
            fs::create_dir_all(dir).unwrap();
        }
        fs::write(paths.models().join("checkpoints").join("m.safetensors"), "model").unwrap();
        fs::write(paths.engine_file(), "{}").unwrap();
        // Beside the root, and linked from inside it.
        let outside = tmp.join("other-comfyui");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("keep.safetensors"), "theirs").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, paths.models().join("linked")).unwrap();

        // Anything but the app's own folder is refused and left alone.
        for wrong in [tmp.clone(), outside.clone(), Path::new("/").into(), Path::new(APP_FOLDER).into(), root.join("models")] {
            assert!(erase(&wrong).is_err(), "{}", wrong.display());
        }
        #[cfg(unix)]
        {
            let link = tmp.join("link").join(APP_FOLDER); // named right, but a link to someone else's folder
            fs::create_dir_all(link.parent().unwrap()).unwrap();
            std::os::unix::fs::symlink(&outside, &link).unwrap();
            assert!(erase(&link).is_err());
        }
        assert!(paths.models().join("checkpoints").join("m.safetensors").exists());

        erase(&root).unwrap();
        assert!(!root.exists());
        assert_eq!(fs::read_to_string(outside.join("keep.safetensors")).unwrap(), "theirs");
        fs::remove_dir_all(&tmp).unwrap();
    }
}
