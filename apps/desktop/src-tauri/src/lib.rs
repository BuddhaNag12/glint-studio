mod scan;
#[allow(dead_code)] // started by the runtime installer (Phase 2)
mod supervisor;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![scan::scan_hardware])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
