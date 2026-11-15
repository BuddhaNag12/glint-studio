mod download;
mod paths;
mod recommend;
mod runtime;
mod scan;
mod secrets;
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
            runtime::install_engine,
            runtime::start_host,
            recommend::recommend_models,
            download::download_model,
            secrets::list_providers,
            secrets::set_provider_key,
            secrets::delete_provider_key
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                app.state::<runtime::Runtime>().supervisor.stop_all();
                download::stop();
            }
        });
}
