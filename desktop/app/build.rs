fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["report_first_paint"])),
    )
    .expect("validate desktop configuration");
}
