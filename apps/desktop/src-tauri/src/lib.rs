mod paths;
mod runtime;
mod scan;
mod supervisor;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(runtime::Runtime::default())
        .invoke_handler(tauri::generate_handler![
            scan::scan_hardware,
            runtime::install_core,
            runtime::start_host
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                app.state::<runtime::Runtime>().supervisor.stop_all();
            }
        });
}
