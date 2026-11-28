# Desktop scaffold

The Tauri 2 desktop is a Windows client. This package renders the disconnected state with a local React build. The native gateway and runtime client crates are placeholders for R1-02.

Prerequisites are the MSVC build tools, Microsoft Edge WebView2, Rust via rustup, Node 24, pnpm 10.30.3 and Python 3.14. The workspace pins Rust and pnpm. Run the following from `desktop/` in PowerShell 7.

```powershell
Push-Location ui
pnpm install --frozen-lockfile && pnpm typecheck && pnpm test && pnpm build
Pop-Location
cargo fmt --all --check && cargo clippy --locked --workspace --all-targets -- -D warnings && cargo test --locked --workspace
Push-Location app
../ui/node_modules/.bin/tauri.cmd build --no-bundle --ci -- --locked
Pop-Location
python tests/smoke_windows.py target/release/bitcadence-desktop.exe
```

The smoke test captures `--self-test` stdout and checks for the offline screen in a fresh profile. It verifies the executable's GUI subsystem and observes both launches for unexpected child processes and visible console windows. The normal launch stays open for 60 seconds. Run in an interactive Windows session with WebView2 installed.

The self-test reports the webview's paint timing and the elapsed time from native startup to receipt of the paint report. The main webview has one IPC permission for that report. Tauri's isolation hook checks the command and payload before forwarding it to the native app.

For UI-only development run `pnpm dev` from `ui/`.
