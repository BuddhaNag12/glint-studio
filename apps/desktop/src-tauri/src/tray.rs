//! The menu-bar menu: the server's address, API, the model kept in memory, Start/Stop, and a
//! shortcut to each dashboard page. A thread refreshes it from the host every 3 seconds.

use std::thread;
use std::time::Duration;

use serde_json::{Value, json};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::dashboard::{open_page_or_setup, show_window};
use crate::runtime::{self, Health, Runtime};
use crate::supervisor::http_json;

const REFRESH: Duration = Duration::from_secs(3);

pub fn create_tray(app: &AppHandle) -> tauri::Result<()> {
    let item = |id: &str, text: &str| MenuItem::with_id(app, id, text, true, None::<&str>);
    let status = MenuItem::with_id(app, "status", "Server: starting…", false, None::<&str>)?;
    let server = item("server", "Stop Server")?;
    let models = Submenu::with_id(app, "models", "Models", false)?;
    let separator = || PredefinedMenuItem::separator(app);
    let menu = Menu::with_items(
        app,
        &[
            &status,
            &separator()?,
            &item("connect", "API")?,
            &models,
            &server,
            &separator()?,
            &item("open", "Dashboard")?,
            &item("generate", "Generate")?,
            &separator()?,
            &item("about", "About")?,
            &MenuItem::with_id(app, "settings", "Settings", true, Some("CmdOrCtrl+,"))?,
            &separator()?,
            &MenuItem::with_id(app, "quit", "Quit", true, Some("CmdOrCtrl+Q"))?,
        ],
    )?;
    let mut builder = TrayIconBuilder::with_id("main").tooltip("Glint Studio").menu(&menu).show_menu_on_left_click(true);
    // macOS tints a black template icon to match the menu bar; other taskbars get the colour icon.
    if cfg!(target_os = "macos") {
        builder = builder.icon(tauri::include_image!("icons/tray-template.png")).icon_as_template(true);
    } else if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.on_menu_event(|app, event| on_click(app, event.id.as_ref())).build(app)?;

    let mut tray = Tray {
        status,
        text: String::new(),
        server,
        models,
        listed: None,
        checks: Vec::new(),
        unload: None,
    };
    #[cfg(target_os = "macos")]
    tint::watch();
    let app = app.clone();
    thread::spawn(move || {
        loop {
            if let Err(e) = tray.refresh(&app) {
                eprintln!("menu: {e}");
            }
            thread::sleep(REFRESH);
        }
    });
    Ok(())
}

/// The status line's text colour, as oMLX shows it. Only macOS menus can colour text.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Tint {
    Green,
    Orange,
    Red,
}

/// The status line and its colour.
fn status_line(health: Health, url: Option<&str>) -> (String, Option<Tint>) {
    let port = url.and_then(|u| u.rsplit(':').next()).map(|p| format!(" (port {p})")).unwrap_or_default();
    match health {
        Health::Running => (format!("Server: running{port}"), Some(Tint::Green)),
        Health::EngineStopped => (format!("Server: running{port}, engine stopped"), Some(Tint::Orange)),
        Health::Starting => ("Server: starting…".into(), None),
        Health::Stopped => ("Server: stopped".into(), None),
        Health::Broken => ("Server: failed, open Settings to repair".into(), Some(Tint::Red)),
    }
}

#[cfg(target_os = "macos")]
mod tint {
    //! Tauri can't colour a menu item's text, so this reaches the NSMenu: it's caught the first time the menu
    //! opens, and the status line (item 0) gets a coloured title then and each time its text changes.
    use std::cell::RefCell;
    use std::ptr::NonNull;
    use std::sync::Mutex;

    use block2::RcBlock;
    use objc2::rc::Retained;
    use objc2::AnyThread;
    use objc2::runtime::AnyObject;
    use objc2_app_kit::{
        NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSMenu, NSMenuDidBeginTrackingNotification,
    };
    use objc2_foundation::{NSAttributedString, NSDictionary, NSNotification, NSNotificationCenter};

    use super::Tint;

    static TINT: Mutex<Option<Tint>> = Mutex::new(None);
    thread_local! {
        static MENU: RefCell<Option<Retained<NSMenu>>> = const { RefCell::new(None) };
    }

    /// Main thread, once.
    pub fn watch() {
        let block = RcBlock::new(|note: NonNull<NSNotification>| {
            let menu = unsafe { note.as_ref() }.object().and_then(|o| o.downcast::<NSMenu>().ok());
            let Some(menu) = menu else { return };
            if menu.itemAtIndex(0).is_some_and(|i| i.title().to_string().starts_with("Server:")) {
                MENU.with(|m| *m.borrow_mut() = Some(menu));
                apply();
            }
        });
        let center = NSNotificationCenter::defaultCenter();
        let name = unsafe { NSMenuDidBeginTrackingNotification };
        // The observer lives as long as the app.
        std::mem::forget(unsafe { center.addObserverForName_object_queue_usingBlock(Some(name), None, None, &block) });
    }

    pub fn set(tint: Option<Tint>) {
        *TINT.lock().unwrap() = tint;
    }

    /// Main thread only.
    pub fn apply() {
        MENU.with(|m| {
            let Some(item) = m.borrow().as_ref().and_then(|menu| menu.itemAtIndex(0)) else { return };
            let colour = match *TINT.lock().unwrap() {
                Some(Tint::Green) => NSColor::systemGreenColor(),
                Some(Tint::Orange) => NSColor::systemOrangeColor(),
                Some(Tint::Red) => NSColor::systemRedColor(),
                None => return item.setAttributedTitle(None),
            };
            let font = NSFont::menuFontOfSize(0.0); // without it an attributed title falls back to Helvetica 12
            let keys = unsafe { [NSForegroundColorAttributeName, NSFontAttributeName] };
            let values: [&AnyObject; 2] = [&colour, &font];
            let attrs = NSDictionary::from_slices(&keys, &values);
            let title = unsafe {
                NSAttributedString::initWithString_attributes(NSAttributedString::alloc(), &item.title(), Some(&attrs))
            };
            item.setAttributedTitle(Some(&title));
        });
    }
}

fn on_click(app: &AppHandle, id: &str) {
    match id {
        "status" => open_page_or_setup(app, "connect/this-computer"), // the address, with copy buttons
        "server" => toggle_server(app),
        "generate" => open_page_or_setup(app, "generate"),
        "connect" => open_page_or_setup(app, "connect"),
        "open" => open_page_or_setup(app, ""),
        "browse" => open_page_or_setup(app, "models/discover"),
        "unload" => host_call(app, "/api/v1/engine/unload", None),
        "settings" => show_window(app),
        "about" => {
            show_window(app);
            let _ = app.emit("pane", "about"); // Settings.tsx opens its About pane
        }
        "quit" => app.exit(0),
        _ => {
            if let Some(model) = id.strip_prefix("model:") {
                host_call(app, "/api/v1/engine/load", Some(json!({ "model": model }).to_string()));
            }
        }
    }
}

fn toggle_server(app: &AppHandle) {
    let state = app.state::<Runtime>();
    if state.host_on() {
        state.stop_server();
        return;
    }
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = runtime::ensure_host(&app).await {
            eprintln!("server: {e}");
            show_window(&app); // the setup window shows what's wrong and can repair it
        }
    });
}

/// POSTs to the running host off the main thread; the next refresh shows the result.
fn host_call(app: &AppHandle, path: &'static str, body: Option<String>) {
    let Ok(Some(host)) = app.state::<Runtime>().host_now() else { return };
    thread::spawn(move || {
        if http_json("POST", &format!("{}{path}", host.url), &host.api_key, body.as_deref()).is_none() {
            eprintln!("menu: {path} failed");
        }
    });
}

struct Tray {
    status: MenuItem<Wry>,
    /// The status text shown, so it's only reset (which drops its colour) when it changes.
    text: String,
    server: MenuItem<Wry>,
    models: Submenu<Wry>,
    /// The downloaded models the Models menu lists (id, name), so it's rebuilt only when they change.
    listed: Option<Vec<(String, String)>>,
    checks: Vec<CheckMenuItem<Wry>>,
    unload: Option<MenuItem<Wry>>,
}

impl Tray {
    fn refresh(&mut self, app: &AppHandle) -> tauri::Result<()> {
        let state = app.state::<Runtime>();
        let host = state.host_now().ok().flatten();
        let get = |path: &str| host.as_ref().and_then(|h| http_json("GET", &format!("{}{path}", h.url), &h.api_key, None));
        let system = get("/api/v1/system");
        let up = system.is_some();
        let local = if up { get("/api/v1/local-models") } else { None };

        let health = state.health();
        let (text, tint) = status_line(health, host.as_ref().map(|h| h.url.as_str()));
        self.status.set_enabled(up)?;
        if text != self.text {
            self.status.set_text(&text)?;
            self.text = text;
            #[cfg(target_os = "macos")]
            {
                tint::set(tint);
                app.run_on_main_thread(tint::apply)?;
            }
        }
        let _ = tint; // only macOS colours it
        self.server.set_text(if state.host_on() { "Stop Server" } else { "Start Server" })?;
        self.models.set_enabled(local.is_some())?;
        if let (Some(system), Some(local)) = (&system, &local) {
            self.show_models(app, downloaded(local), &system["engine"]["model"])?;
        }
        Ok(())
    }

    fn show_models(&mut self, app: &AppHandle, models: Vec<(String, String)>, engine: &Value) -> tauri::Result<()> {
        if self.listed.as_ref() != Some(&models) {
            for old in self.models.items()? {
                self.models.remove(&old)?;
            }
            self.checks.clear();
            for (id, name) in &models {
                let check = CheckMenuItem::with_id(app, format!("model:{id}"), name, true, false, None::<&str>)?;
                self.models.append(&check)?;
                self.checks.push(check);
            }
            if models.is_empty() {
                self.models.append(&MenuItem::new(app, "No models downloaded yet", false, None::<&str>)?)?;
            }
            let unload = MenuItem::with_id(app, "unload", "Unload Model", false, None::<&str>)?;
            self.models.append(&PredefinedMenuItem::separator(app)?)?;
            self.models.append(&unload)?;
            self.models.append(&MenuItem::with_id(app, "browse", "Browse Models…", true, None::<&str>)?)?;
            self.unload = Some(unload);
            self.listed = Some(models);
        }
        let status = engine["status"].as_str().unwrap_or("idle");
        let current = engine["model"].as_str();
        for ((id, name), check) in self.listed.iter().flatten().zip(&self.checks) {
            let mine = current == Some(id.as_str());
            // Set every time: clicking a check item flips its tick before the host has answered.
            check.set_checked(mine && status == "loaded")?;
            check.set_text(match (mine, status) {
                (true, "loading") => format!("{name} (loading…)"),
                (true, "failed") => format!("{name} (couldn't load)"),
                _ => name.clone(),
            })?;
        }
        if let Some(unload) = &self.unload {
            unload.set_enabled(status == "loaded")?;
        }
        Ok(())
    }
}

/// The downloaded models from /api/v1/local-models, as (id, name).
fn downloaded(local: &Value) -> Vec<(String, String)> {
    let rows = local["models"].as_array().map(Vec::as_slice).unwrap_or_default();
    rows.iter()
        .filter(|m| m["installed"].as_bool() == Some(true))
        .filter_map(|m| Some((m["id"].as_str()?.to_owned(), m["name"].as_str()?.to_owned())))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_line_shows_the_port() {
        assert_eq!(status_line(Health::Running, Some("http://127.0.0.1:7788")), ("Server: running (port 7788)".into(), Some(Tint::Green)));
        assert_eq!(status_line(Health::Stopped, Some("http://127.0.0.1:7788")).0, "Server: stopped");
        assert_eq!(status_line(Health::Running, None).0, "Server: running");
    }

    #[test]
    fn only_downloaded_models_are_listed() {
        let local = json!({"models": [
            {"id": "a", "name": "A", "installed": true},
            {"id": "b", "name": "B", "installed": false},
        ]});
        assert_eq!(downloaded(&local), [("a".to_owned(), "A".to_owned())]);
        assert!(downloaded(&json!({})).is_empty());
    }
}
