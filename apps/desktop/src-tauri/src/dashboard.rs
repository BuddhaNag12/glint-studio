//! The dashboard. It is a web page the host serves; the app opens
//! it in the browser with a one-time login link signed with the host's API key (see
//! host/app/dashboard.py). The window is only for first-run setup, repair and cloud keys.

use std::time::{SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};
use tauri_plugin_opener::OpenerExt;

use crate::runtime::{self, hex};

/// How long a login link works; the host refuses links that claim longer.
const LOGIN_TTL_SECS: u64 = 60;

/// HMAC-SHA256 (RFC 2104), built on sha2 so the app needs no extra crate for one signature.
fn hmac_sha256(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let pad = |byte: u8| block.map(|b| b ^ byte);
    let inner = Sha256::new().chain_update(pad(0x36)).chain_update(data).finalize();
    Sha256::new().chain_update(pad(0x5c)).chain_update(inner).finalize().into()
}

/// Mirrors login_signature() in host/app/dashboard.py.
fn login_url(host_url: &str, api_key: &str, exp: u64) -> String {
    let sig = hex(&hmac_sha256(api_key.as_bytes(), format!("glint-dashboard-login:{exp}").as_bytes()));
    format!("{host_url}/login?exp={exp}&sig={sig}")
}

/// Starts the host if needed, opens the dashboard in the browser, and tucks the app into the menu bar.
#[tauri::command]
pub async fn open_dashboard(app: AppHandle) -> Result<(), String> {
    open_page(app, "").await
}

/// Opens one dashboard page ("generate", "models/discover"; "" for the overview). The page rides in the
/// fragment, which the browser keeps through the login redirect and never sends to the host.
pub async fn open_page(app: AppHandle, page: &str) -> Result<(), String> {
    let host = runtime::ensure_host(&app).await?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|e| e.to_string())?.as_secs();
    let mut url = login_url(&host.url, &host.api_key, now + LOGIN_TTL_SECS);
    if !page.is_empty() {
        url = format!("{url}#{page}");
    }
    app.opener().open_url(url, None::<&str>).map_err(|e| format!("Couldn't open the browser: {e}"))?;
    hide_window(&app);
    Ok(())
}

pub fn show_window(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Hides the window; on macOS the Dock icon goes too, leaving the menu-bar icon.
pub fn hide_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

/// Opens the dashboard, or the setup window if that fails (not installed yet, or broken).
pub fn open_dashboard_or_setup(app: &AppHandle) {
    open_page_or_setup(app, "");
}

pub fn open_page_or_setup(app: &AppHandle, page: &'static str) {
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = open_page(app.clone(), page).await {
            eprintln!("dashboard: {e}");
            show_window(&app);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hmac_matches_rfc_4231() {
        let mac = hmac_sha256(b"Jefe", b"what do ya want for nothing?");
        assert_eq!(hex(&mac), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
        // Test case 6: a key longer than the block is hashed first.
        let mac = hmac_sha256(&[0xaa; 131], b"Test Using Larger Than Block-Size Key - Hash Key First");
        assert_eq!(hex(&mac), "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54");
    }

    #[test]
    fn login_url_matches_the_host() {
        // The same value is asserted in host/tests/test_local_models.py.
        assert_eq!(
            login_url("http://127.0.0.1:1", "test-key", 1700000000),
            "http://127.0.0.1:1/login?exp=1700000000&sig=15a0213761010c51206ec491d60fffd0bc42a8254be811130cedec1122b7aa75"
        );
    }
}
