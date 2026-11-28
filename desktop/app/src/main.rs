#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#![forbid(unsafe_code)]

use std::{
    io::{self, Write},
    time::{Duration, Instant},
};

struct SelfTest {
    started: Instant,
}

#[tauri::command]
fn report_first_paint(
    app: tauri::AppHandle,
    test: tauri::State<'_, Option<SelfTest>>,
    first_paint_ms: f64,
    first_contentful_paint_ms: f64,
    offline_visible: bool,
) -> Result<(), String> {
    let Some(test) = test.inner() else {
        return Ok(());
    };
    if !first_paint_ms.is_finite()
        || !first_contentful_paint_ms.is_finite()
        || first_paint_ms < 0.0
        || first_contentful_paint_ms < first_paint_ms
        || !offline_visible
    {
        app.exit(1);
        return Err("Offline screen did not paint".into());
    }

    let result = serde_json::json!({
        "ok": true,
        "first_paint_ms": first_paint_ms,
        "first_contentful_paint_ms": first_contentful_paint_ms,
        "startup_to_paint_report_ms": test.started.elapsed().as_secs_f64() * 1000.0,
        "runtime_state": "offline",
        "offline_visible": offline_visible
    });
    if let Err(error) = writeln!(io::stdout().lock(), "{result}") {
        app.exit(1);
        return Err(error.to_string());
    }
    app.exit(0);
    Ok(())
}

fn main() {
    let started = Instant::now();
    let self_test = std::env::args_os().skip(1).any(|arg| arg == "--self-test");
    let result = tauri::Builder::default()
        .manage(self_test.then_some(SelfTest { started }))
        .setup(move |app| {
            if self_test {
                let handle = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(20));
                    eprintln!("Timed out waiting for the offline screen to paint");
                    handle.exit(1);
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![report_first_paint])
        .run(tauri::generate_context!());
    if let Err(error) = result {
        eprintln!("Desktop startup failed: {error}");
        std::process::exit(1);
    }
}
