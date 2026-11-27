# Desktop Manager, Installer & System Tray

The Windows desktop window shows the state, components and logs of the local BitCadence runtime. Work continues if the window or tray icon exits.

## 1. Installation: One-Click Installer & Desktop Shortcut

See the [installation guide](../INSTALL.md) for a new BitCadence installation. Use this PowerShell command to install the desktop observer from a source checkout.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_desktop.ps1 -Python .\.venv\Scripts\python.exe
```

The script installs the Pillow and pystray dependencies and adds shortcuts to the desktop and Start menu. The shortcuts run `scripts\desktop.pyw` with `pythonw.exe`.

## Open the observer

Open the BitCadence desktop shortcut. Launching a second copy brings the existing window forward. The status line shows whether the runtime is running, in maintenance, quiesced by STOP, or offline. For runtime recovery, start with `bitcadence runtime status` and inspect the reported state.

- Select a component to filter the log pane to its last 100 lines.
- **Open console** opens the local gateway in your browser. Pass `--port` to the desktop launcher if your gateway uses another port.
- Closing the window hides it in the tray when available. Right-click the icon and choose **Open BitCadence**, **Open console**, or **Exit**. `bitcadence-desktop --quit` also exits the observer.

## 5. Configuring Autostart on Sign-in

The runtime has its own per-user sign-in task. Use this PowerShell command when the deployment is approved for activation.

```powershell
bitcadence runtime install-bootstrap
```

The command writes the registered task XML to `~/.mco/runtime/bootstrap.xml`. Follow the [runtime migration rules](../adr/0004-runtime-bootstrap-supervision.md#migration-rules) before moving any existing workload. Use `bitcadence runtime stop` to stop gracefully and `bitcadence runtime uninstall-bootstrap` to remove task registration.

To also launch the observer at sign-in, run the optional Startup shortcut script.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\desktop_autostart.ps1 -Pythonw .\.venv\Scripts\pythonw.exe
```

The same script with `-Remove` removes the observer shortcut. The desktop and the runtime have independent lifecycles.

## Where the logs live

| File or directory | Contents |
|---|---|
| `~/.mco/runtime/bootstrap.log` | Scheduled launcher output and retry decisions |
| `~/.mco/runtime/heartbeat.json` | Runtime and child state for local observers and watchdogs |
| `~/.mco/logs/` | Runtime and supervised component logs |

The Tkinter observer supports Windows. On other platforms, use the CLI or open the web console.
