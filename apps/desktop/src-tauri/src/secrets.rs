//! Cloud provider API keys. Keys live only in the OS keychain (macOS Keychain, Windows Credential
//! Manager, Linux Secret Service); the host gets them as env vars at start and never from disk.
//! Which providers exist comes from `providers.json` in app data, first copied from the shipped
//! `host/providers.example.json` and the user's to edit afterwards.

use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::paths::Paths;
use crate::runtime::{self, HostInfo, Runtime};

const SERVICE: &str = "dev.glintstudio.app";
const HOST: &str = "host"; // the host's process name in runtime.rs
const HOST_KEY_ENTRY: &str = "glint-host-api-key"; // not a provider id, so list_providers never shows it

#[derive(Debug, Deserialize)]
struct ProvidersFile {
    #[serde(default)]
    providers: Vec<Provider>,
}

#[derive(Debug, Deserialize)]
pub struct Provider {
    id: String,
    #[serde(default)]
    name: String,
    /// The env var the host reads the key from; None for servers that need no key.
    api_key_env: Option<String>,
}

/// What the UI sees: never the key, only whether one is stored.
#[derive(Debug, Serialize)]
pub struct ProviderStatus {
    id: String,
    name: String,
    has_key: bool,
}

#[tauri::command]
pub async fn list_providers(app: AppHandle) -> Result<Vec<ProviderStatus>, String> {
    let mut out = Vec::new();
    for p in keyed_providers(&app)? {
        let has_key = match entry(&p.id)?.get_password() {
            Ok(_) => true,
            Err(keyring::Error::NoEntry) => false,
            Err(e) => return Err(format!("Couldn't read the keychain: {e}")),
        };
        let name = if p.name.is_empty() { p.id.clone() } else { p.name };
        out.push(ProviderStatus { id: p.id, name, has_key });
    }
    Ok(out)
}

/// Stores the key, then restarts the host so it picks the key up. Returns the new host.
#[tauri::command]
pub async fn set_provider_key(app: AppHandle, state: State<'_, Runtime>, id: String, key: String) -> Result<HostInfo, String> {
    let key = key.trim();
    if key.is_empty() {
        return Err("Paste a key first.".into());
    }
    known(&app, &id)?;
    entry(&id)?.set_password(key).map_err(|e| format!("Couldn't save the key in the keychain: {e}"))?;
    restart_host(app, state).await
}

#[tauri::command]
pub async fn delete_provider_key(app: AppHandle, state: State<'_, Runtime>, id: String) -> Result<HostInfo, String> {
    known(&app, &id)?;
    match entry(&id)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => {}
        Err(e) => return Err(format!("Couldn't remove the key from the keychain: {e}")),
    }
    restart_host(app, state).await
}

/// The host's extra env: `PROVIDERS_FILE` plus each stored key under its provider's env var.
/// A bad providers file or a keychain that can't be read only costs the cloud models, so the host
/// still starts (and logs what's wrong with the file); the Providers screen shows the error.
pub fn host_env(paths: &Paths, resources: &Path, taken: &[(String, String)]) -> Vec<(String, String)> {
    let providers = providers(paths, resources).unwrap_or_default();
    let mut env = vec![("PROVIDERS_FILE".to_string(), paths.providers_file().to_string_lossy().into_owned())];
    env.extend(key_env(&providers, taken, |id| entry(id).ok()?.get_password().ok()));
    env
}

/// (env var, key) for every provider with a stored key. Skips env names the host already uses
/// (a hand-edited `"api_key_env": "API_KEY"` must not replace the host's own key) and odd names.
fn key_env(providers: &[Provider], taken: &[(String, String)], key: impl Fn(&str) -> Option<String>) -> Vec<(String, String)> {
    providers
        .iter()
        .filter_map(|p| {
            let var = p.api_key_env.as_ref()?;
            let valid = !var.is_empty() && var.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_');
            if !valid || taken.iter().any(|(k, _)| k == var) {
                return None;
            }
            Some((var.clone(), key(&p.id)?))
        })
        .collect()
}

// shortcut: a copy made once, so providers added to the shipped list later don't reach existing
// users; merge new ids in by id once the shipped list grows.
fn ensure_providers_file(shipped: &Path, target: &Path) -> Result<(), String> {
    if target.exists() {
        return Ok(());
    }
    if let Some(dir) = target.parent() {
        fs::create_dir_all(dir).map_err(|e| format!("Couldn't create {}: {e}", dir.display()))?;
    }
    fs::copy(shipped, target).map_err(|e| format!("Couldn't copy {}: {e}", shipped.display()))?;
    Ok(())
}

fn providers(paths: &Paths, resources: &Path) -> Result<Vec<Provider>, String> {
    let file = paths.providers_file();
    ensure_providers_file(&resources.join("host").join("providers.example.json"), &file)?;
    read_providers(&file)
}

fn read_providers(path: &Path) -> Result<Vec<Provider>, String> {
    let text = fs::read_to_string(path).map_err(|e| format!("Couldn't read {}: {e}", path.display()))?;
    let file: ProvidersFile = serde_json::from_str(&text).map_err(|e| format!("{} is invalid: {e}", path.display()))?;
    Ok(file.providers)
}

fn keyed_providers(app: &AppHandle) -> Result<Vec<Provider>, String> {
    let root = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let resources = app.path().resource_dir().map_err(|e| e.to_string())?;
    Ok(providers(&Paths { root }, &resources)?.into_iter().filter(|p| p.api_key_env.is_some()).collect())
}

/// Only providers in the file get a keychain entry: the id comes from the webview.
fn known(app: &AppHandle, id: &str) -> Result<(), String> {
    if keyed_providers(app)?.iter().any(|p| p.id == id) {
        Ok(())
    } else {
        Err(format!("There's no provider '{id}' that takes a key."))
    }
}

/// The host's own API key: made once, then kept in the keychain so it survives restarts.
pub fn host_api_key() -> Result<String, String> {
    let entry = entry(HOST_KEY_ENTRY)?;
    match entry.get_password() {
        Ok(key) => Ok(key),
        Err(keyring::Error::NoEntry) => {
            let key = runtime::new_key()?;
            entry.set_password(&key).map_err(|e| format!("Couldn't save the host key: {e}"))?;
            Ok(key)
        }
        Err(e) => Err(format!("Couldn't read the host key: {e}")),
    }
}

fn entry(id: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, id).map_err(|e| format!("The keychain isn't available: {e}"))
}

async fn restart_host(app: AppHandle, state: State<'_, Runtime>) -> Result<HostInfo, String> {
    state.supervisor.stop(HOST);
    runtime::ensure_host(&app)
        .await
        .map_err(|e| format!("The key is saved, but the host didn't restart: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn provider(id: &str, env: Option<&str>) -> Provider {
        Provider { id: id.into(), name: String::new(), api_key_env: env.map(String::from) }
    }

    #[test]
    fn key_env_maps_stored_keys_to_their_vars() {
        let providers = [
            provider("openai", Some("OPENAI_API_KEY")),
            provider("nokey", Some("NOKEY_API_KEY")),   // nothing stored
            provider("local", None),                    // needs no key
            provider("sneaky", Some("API_KEY")),        // the host's own key
            provider("odd", Some("path; rm")),          // not an env name
        ];
        let taken = [("API_KEY".to_string(), "host".to_string())];
        let env = key_env(&providers, &taken, |id| (id != "nokey").then(|| format!("sk-{id}")));
        assert_eq!(env, [("OPENAI_API_KEY".to_string(), "sk-openai".to_string())]);
    }

    #[test]
    fn providers_file_is_copied_once_from_the_shipped_example() {
        let dir = std::env::temp_dir().join(format!("glint-secrets-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let shipped = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../host/providers.example.json");
        let target = dir.join("data").join("providers.json");

        ensure_providers_file(&shipped, &target).unwrap();
        let providers = read_providers(&target).unwrap();
        assert!(providers.iter().any(|p| p.id == "openai" && p.api_key_env.as_deref() == Some("OPENAI_API_KEY")));

        fs::write(&target, r#"{"providers": []}"#).unwrap(); // the user's edits survive
        ensure_providers_file(&shipped, &target).unwrap();
        assert!(read_providers(&target).unwrap().is_empty());
    }

    /// The real OS keychain: writes, reads and deletes a throwaway entry.
    /// `cargo test real_keychain_round_trip -- --ignored`
    #[test]
    #[ignore]
    fn real_keychain_round_trip() {
        let e = keyring::Entry::new(SERVICE, "glint-test-round-trip").unwrap();
        e.set_password("sk-round-trip").unwrap();
        assert_eq!(e.get_password().unwrap(), "sk-round-trip");
        e.delete_credential().unwrap();
        assert!(matches!(e.get_password(), Err(keyring::Error::NoEntry)));
    }
}
