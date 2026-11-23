mod dashboard;
mod erase;
mod network;
mod paths;
mod runtime;
mod scan;
mod secrets;
mod supervisor;
mod tray;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(runtime::Runtime::default())
        .setup(|app| {
            tray::create_tray(app.handle())?;
            network::spawn(app.handle().clone());
            erase::spawn(app.handle().clone());
            // Installed: live in the menu bar and open the dashboard. First run: the setup window.
            if runtime::core_installed(app.handle()) {
                dashboard::hide_window(app.handle());
                dashboard::open_dashboard_or_setup(app.handle());
            } else {
                dashboard::show_window(app.handle());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window keeps the host running; Quit is in the menu-bar menu.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                dashboard::hide_window(window.app_handle());
            }
        })
        .invoke_handler(tauri::generate_handler![
            scan::scan_hardware,
            runtime::install_core,
            runtime::install_engine,
            runtime::stop_install,
            runtime::start_host,
            runtime::setup_done,
            dashboard::open_dashboard,
            secrets::list_providers,
            secrets::set_provider_key,
            secrets::delete_provider_key,
            secrets::rotate_host_key
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| match event {
            tauri::RunEvent::Exit => {
                runtime::kill_install();
                app.state::<runtime::Runtime>().supervisor.stop_all();
            }
            // Opening the app again (Finder, Spotlight, Launchpad) brings up Settings.
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Reopen { .. } => dashboard::show_window(app),
            _ => {}
        });
}
