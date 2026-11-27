"""One window and tray icon for observing the local BitCadence runtime."""
from __future__ import annotations

import argparse
import ctypes
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from filelock import FileLock, Timeout

from .controller import DesktopController, RuntimeView


def make_icon():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), "#122238")
    draw = ImageDraw.Draw(image)
    for x, height in ((14, 20), (26, 38), (38, 28), (50, 46)):
        draw.rounded_rectangle((x - 4, 32 - height // 2, x + 3, 32 + height // 2), radius=3, fill="#52dfc1")
    return image


def status_line(view: RuntimeView) -> str:
    states = {
        "never-started": "Offline · runtime has not started",
        "stopped": "Offline · runtime stopped",
        "crashed": "Offline · runtime crashed",
        "dead": "Offline · runtime process is gone",
        "stale": "Runtime heartbeat is stale",
        "unreachable": "Runtime control API is unreachable",
        "running": "Runtime running · observing",
        "maintenance": "Runtime in maintenance · observing",
        "quiesced": "Runtime quiesced by STOP · observing",
    }
    return " · ".join(filter(None, (states.get(view.state, view.state), view.config_error, view.error)))


class DesktopApp:
    def __init__(self, controller: DesktopController):
        self.controller = controller
        self.messages = queue.Queue()
        self.closing = threading.Event()
        self.selected: str | None = None
        self.root = tk.Tk()
        self.root.title("BitCadence — Runtime Status")
        self.root.geometry("980x680")
        self.root.minsize(780, 520)
        from PIL import ImageTk

        self.icon_image = ImageTk.PhotoImage(make_icon())
        self.root.iconphoto(True, self.icon_image)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=(12, 8))
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        style.configure("TLabel", font=("Segoe UI", 10))
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="BitCadence", font=("Segoe UI", 24, "bold")).pack(side="left")
        ttk.Button(toolbar, text="Open console", command=self.open_console).pack(side="right")
        self.status = tk.StringVar(value="Checking runtime…")
        ttk.Label(frame, textvariable=self.status, wraplength=735).pack(anchor="w", pady=12)

        self.table = ttk.Treeview(frame, columns=("state", "pid", "error"), height=8, selectmode="browse")
        self.table.bind("<<TreeviewSelect>>", self.select_worker)
        self.table.heading("#0", text="Component")
        self.table.column("#0", width=180)
        for column, title, width in (("state", "Status", 165), ("pid", "Process", 115), ("error", "Last error", 330)):
            self.table.heading(column, text=title)
            self.table.column(column, width=width)
        self.table.pack(fill="x")
        ttk.Label(frame, text="Runtime logs · select a component to filter").pack(anchor="w", pady=(12, 0))
        self.log = tk.Text(frame, height=12, background="#122238", foreground="#dce8f4",
                           font=("Consolas", 9), state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

        self.tray = None
        try:
            import pystray

            self.tray = pystray.Icon("BitCadence", make_icon(), "BitCadence — Runtime Status", pystray.Menu(
                pystray.MenuItem("Open BitCadence", lambda *_: self.messages.put(("show", None)), default=True),
                pystray.MenuItem("Open console", lambda *_: self.open_console()),
                pystray.MenuItem("Exit", lambda *_: self.messages.put(("exit", None)))))
            self.tray.run_detached()
        except (ImportError, RuntimeError):
            self.tray = None
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        threading.Thread(target=self.poll_runtime, daemon=True, name="BitCadence-status").start()
        self.root.after(100, self.pump)

    def open_console(self):
        webbrowser.open(self.controller.url + "/console")

    def select_worker(self, _event=None):
        selection = self.table.selection()
        self.selected = selection[0] if selection else None

    def poll_runtime(self):
        while not self.closing.is_set():
            selected = self.selected
            self.messages.put(("snapshot", (selected, self.controller.snapshot(selected))))
            self.closing.wait(2)

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def pump(self):
        exit_request = self.controller.runtime_dir / "exit-request"
        if exit_request.exists():
            exit_request.unlink(missing_ok=True)
            self.exit()
            return
        show_request = self.controller.runtime_dir / "show-window"
        if show_request.exists():
            show_request.unlink(missing_ok=True)
            self.show()
        while not self.messages.empty():
            kind, value = self.messages.get_nowait()
            if kind == "show":
                self.show()
            elif kind == "exit":
                self.exit()
                return
            elif kind == "snapshot":
                self.render(*value)
        self.root.after(150, self.pump)

    def render(self, selected: str | None, view: RuntimeView):
        self.status.set(status_line(view))
        for obsolete in set(self.table.get_children()) - {row[0] for row in view.rows}:
            self.table.delete(obsolete)
        for name, *values in view.rows:
            if self.table.exists(name):
                self.table.item(name, values=values)
            else:
                self.table.insert("", "end", iid=name, text=name, values=values)
        self.select_worker()
        lines = view.lines if selected == self.selected else []
        content = "\n".join(lines)
        if self.log.get("1.0", "end-1c") != content:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("1.0", content)
            self.log.see("end")
            self.log.configure(state="disabled")

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.exit()

    def exit(self):
        self.closing.set()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18789)
    parser.add_argument("--quit", action="store_true", help="Close the running desktop observer")
    # Previous sign-in shortcuts still pass this argument.
    parser.add_argument("--start-all", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--minimized", action="store_true", help="Start in the tray when available")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("The desktop observer currently supports Windows.")
    controller = DesktopController(port=args.port)
    directory = controller.runtime_dir
    if args.quit and not directory.is_dir():
        return
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(directory / "observer.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        (directory / ("exit-request" if args.quit else "show-window")).touch()
        return
    try:
        if args.quit:
            return
        for marker in ("exit-request", "show-window", "start-all-request"):
            (directory / marker).unlink(missing_ok=True)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BitCadence.LocalControl")
        try:
            app = DesktopApp(controller)
            if args.minimized and app.tray:
                app.root.after(0, app.root.withdraw)
            app.run()
        except Exception as exc:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("BitCadence", str(exc), parent=root)
            root.destroy()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
