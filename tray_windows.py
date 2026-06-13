#!/usr/bin/env python3
"""Windows tray controller for the Claude usage notifier."""

from __future__ import annotations

import asyncio
import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
VENV_SITE_PACKAGES = SCRIPT_DIR / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists():
    site.addsitedir(str(VENV_SITE_PACKAGES))


APP_NAME = "ClaudeUsage"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeUsage"
SINGLETON_MUTEX_NAME = r"Local\ClaudeUsageTray"
ERROR_ALREADY_EXISTS = 183
DEFAULT_PUSHOVER_CONFIG = {
    "pushover_app_token": "",
    "pushover_user_key": "",
    "pushover_device": "",
    "pushover_sound": "",
    "safety_refresh_seconds": 15 * 60,
}


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    return app_data_dir() / "state.json"


def command_path() -> Path:
    return app_data_dir() / "command.json"


def log_path() -> Path:
    return app_data_dir() / "notifier.log"


def pushover_config_path() -> Path:
    return app_data_dir() / "config.json"


def normalize_pushover_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(DEFAULT_PUSHOVER_CONFIG)
    for key in ("pushover_app_token", "pushover_user_key", "pushover_device", "pushover_sound"):
        value = config.get(key, "")
        normalized[key] = str(value).strip() if value is not None else ""

    try:
        safety_refresh = int(
            config.get(
                "safety_refresh_seconds",
                DEFAULT_PUSHOVER_CONFIG["safety_refresh_seconds"],
            )
        )
    except (TypeError, ValueError):
        safety_refresh = DEFAULT_PUSHOVER_CONFIG["safety_refresh_seconds"]
    normalized["safety_refresh_seconds"] = max(1, safety_refresh)
    return normalized


def load_pushover_config() -> dict[str, Any]:
    try:
        loaded = json.loads(pushover_config_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    return normalize_pushover_config(loaded)


def save_pushover_config(config: dict[str, Any]) -> None:
    normalized = normalize_pushover_config(config)
    path = pushover_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")


def apply_pushover_settings(config: dict[str, Any], controller: Any) -> None:
    was_running = bool(controller.is_running())
    save_pushover_config(config)
    if was_running:
        controller.stop()
        controller.start()


def load_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def bucket_line(state: dict[str, Any], bucket: str, label: str) -> str:
    data = state.get(bucket)
    if not isinstance(data, dict):
        return f"{label}: unknown"
    usage = data.get("utilization_percent", "?")
    reset_at = data.get("reset_at_local") or "unknown"
    return f"{label}: {usage}% - reset {reset_at}"


def last_notification_line(state: dict[str, Any]) -> str:
    runtime = state.get("runtime")
    if not isinstance(runtime, dict):
        return "Last notification: none"
    notification = runtime.get("last_notification")
    if not isinstance(notification, dict):
        return "Last notification: none"
    label = notification.get("label", "reset")
    sent_at = notification.get("sent_at_local", "unknown")
    return f"Last notification: {label} - {sent_at}"


def runtime_status(state: dict[str, Any], daemon_running: bool) -> tuple[str, str]:
    if not daemon_running:
        return "stopped", "Stopped"
    runtime = state.get("runtime")
    if not isinstance(runtime, dict):
        return "running", "Connected - waiting for first status"

    status = str(runtime.get("status") or "running")
    message = str(runtime.get("message") or "")
    error = str(runtime.get("error") or "")
    updated_at = float(runtime.get("updated_at") or 0)
    if updated_at and time.time() - updated_at > 180:
        return "error", "Error: status stale"
    if status == "unauthenticated":
        return status, f"Unauthenticated: {message or error or 'run claude login'}"
    if status == "error":
        return status, f"Error: {error or message or 'unknown'}"
    last_poll = runtime.get("last_poll_at_local") or "never"
    return "running", f"Connected - last poll {last_poll}"


def acquire_single_instance():
    if sys.platform != "win32":
        return object()

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, True, SINGLETON_MUTEX_NAME)
    if not handle:
        return object()
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return SCRIPT_DIR


def pythonw_path() -> str:
    candidate = Path(sys.base_exec_prefix) / "pythonw.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def autostart_command() -> str:
    if is_frozen():
        return f'"{sys.executable}"'
    script = SCRIPT_DIR / "tray_windows.py"
    return f'"{pythonw_path()}" "{script}"'


def notifier_command_args(*args: str) -> list[str]:
    if is_frozen():
        return [sys.executable, *args]
    script = SCRIPT_DIR / "claude_reset_notifier.py"
    return [pythonw_path(), str(script), *args]


def should_run_notifier(argv: list[str]) -> bool:
    notifier_flags = {"--daemon", "--once", "--test-pushover"}
    return any(arg in notifier_flags for arg in argv[1:])


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
    except FileNotFoundError:
        return False
    return str(value) == autostart_command()


def set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS


class DaemonController:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.is_running():
            return
        self.process = subprocess.Popen(
            notifier_command_args("--daemon"),
            cwd=str(app_dir()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags(),
        )

    def stop(self) -> None:
        if not self.is_running() or self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def poll_now(self) -> None:
        command_path().write_text(
            json.dumps({"command": "poll_now", "created_at": time.time()}),
            encoding="utf-8",
        )

    def send_test(self) -> None:
        subprocess.Popen(
            notifier_command_args("--test-pushover"),
            cwd=str(app_dir()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags(),
        )


def build_icons():
    from PIL import Image, ImageDraw

    badge_colors = {
        "running": "#20a060",
        "unauthenticated": "#f0a020",
        "error": "#d83b36",
        "stopped": "#6b7280",
    }
    body = "#d3846b"
    shade = "#b86f5e"
    eye = "#161616"
    outline = "#241b1a"
    scale = 3
    offset_x = 2
    offset_y = 5
    body_pixels = [
        "....................",
        "....................",
        ".....##########.....",
        ".....##########.....",
        ".....##.####.##.....",
        "..###############...",
        "..###############...",
        "..###############...",
        "..###############...",
        "...#############....",
        ".....##########.....",
        ".....##########.....",
        ".....##########.....",
        ".....##########.....",
        ".....#..#....#..#...",
        ".....#..#....#..#...",
        "....................",
        "....................",
        "....................",
        "....................",
    ]
    eye_pixels = {(7, 4), (13, 4), (7, 5), (13, 5)}

    icons = {}
    for state, badge_color in badge_colors.items():
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        for y, row in enumerate(body_pixels):
            for x, pixel in enumerate(row):
                if pixel != "#":
                    continue
                color = shade if y >= 12 else body
                x0 = offset_x + x * scale
                y0 = offset_y + y * scale
                draw.rectangle((x0, y0, x0 + scale - 1, y0 + scale - 1), fill=color)

        for x, y in eye_pixels:
            x0 = offset_x + x * scale
            y0 = offset_y + y * scale
            draw.rectangle((x0, y0, x0 + scale - 1, y0 + scale - 1), fill=eye)

        draw.rectangle((offset_x + 5 * scale, offset_y + 2 * scale, offset_x + 14 * scale - 1, offset_y + 2 * scale), fill=outline)
        draw.rectangle((offset_x + 2 * scale, offset_y + 6 * scale, offset_x + 2 * scale, offset_y + 9 * scale - 1), fill=outline)
        draw.rectangle((offset_x + 17 * scale - 1, offset_y + 6 * scale, offset_x + 17 * scale - 1, offset_y + 9 * scale - 1), fill=outline)

        draw.ellipse((44, 44, 63, 63), fill="#ffffff")
        draw.ellipse((47, 47, 60, 60), fill=badge_color)
        icons[state] = image
    return icons


def open_logs() -> None:
    target = log_path() if log_path().exists() else app_data_dir()
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]


def open_pushover_settings(controller: DaemonController) -> None:
    import tkinter as tk
    from tkinter import messagebox

    config = load_pushover_config()
    root = tk.Tk()
    root.title("Pushover settings")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.after(250, lambda: root.attributes("-topmost", False))

    fields = [
        ("Pushover application API token", "pushover_app_token", True),
        ("Pushover user/group key", "pushover_user_key", True),
        ("Device name", "pushover_device", False),
        ("Sound", "pushover_sound", False),
        ("Safety refresh seconds", "safety_refresh_seconds", False),
    ]
    entries: dict[str, tk.Entry] = {}

    frame = tk.Frame(root, padx=14, pady=14)
    frame.grid(row=0, column=0, sticky="nsew")

    for row, (label, key, secret) in enumerate(fields):
        tk.Label(frame, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=(0, 8))
        entry = tk.Entry(frame, width=48, show="*" if secret else "")
        entry.insert(0, str(config.get(key, "")))
        entry.grid(row=row, column=1, sticky="ew", pady=(0, 8), padx=(10, 0))
        entries[key] = entry

    def save() -> None:
        values = {key: entry.get() for key, entry in entries.items()}
        try:
            apply_pushover_settings(values, controller)
        except Exception as exc:
            messagebox.showerror("Pushover settings", f"Could not save settings: {exc}", parent=root)
            return
        messagebox.showinfo("Pushover settings", "Settings saved.", parent=root)
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.grid(row=len(fields), column=0, columnspan=2, sticky="e", pady=(4, 0))
    tk.Button(buttons, text="Cancel", command=root.destroy).grid(row=0, column=0, padx=(0, 8))
    tk.Button(buttons, text="Save", command=save).grid(row=0, column=1)

    entries["pushover_app_token"].focus_set()
    root.mainloop()


def run_tray() -> None:
    instance_lock = acquire_single_instance()
    if instance_lock is None:
        return

    import pystray
    from pystray import Menu, MenuItem

    controller = DaemonController()
    controller.start()
    icons = build_icons()

    def state_snapshot() -> dict[str, Any]:
        return load_state()

    def status_text(_item=None) -> str:
        _status, title = runtime_status(state_snapshot(), controller.is_running())
        return title

    def start_daemon(icon, _item) -> None:
        controller.start()
        icon.update_menu()

    def stop_daemon(icon, _item) -> None:
        controller.stop()
        icon.update_menu()

    def poll_now(icon, _item) -> None:
        if not controller.is_running():
            controller.start()
        controller.poll_now()
        icon.update_menu()

    def send_test(icon, _item) -> None:
        controller.send_test()
        icon.update_menu()

    def pushover_settings(icon, _item) -> None:
        open_pushover_settings(controller)
        icon.update_menu()

    def toggle_autostart(icon, _item) -> None:
        set_autostart(not is_autostart_enabled())
        icon.update_menu()

    def quit_app(icon, _item) -> None:
        controller.stop()
        icon.stop()

    icon = pystray.Icon(
        "ClaudeUsage",
        icons["stopped"],
        "Claude usage",
        Menu(
            MenuItem(status_text, None, enabled=False),
            MenuItem(lambda _item: bucket_line(state_snapshot(), "five_hour", "5h"), None, enabled=False),
            MenuItem(lambda _item: bucket_line(state_snapshot(), "weekly", "Weekly"), None, enabled=False),
            MenuItem(lambda _item: last_notification_line(state_snapshot()), None, enabled=False),
            MenuItem("-", None, enabled=False),
            MenuItem("Start daemon", start_daemon, enabled=lambda _item: not controller.is_running()),
            MenuItem("Stop daemon", stop_daemon, enabled=lambda _item: controller.is_running()),
            MenuItem("Poll now", poll_now),
            MenuItem("Send test Pushover", send_test),
            MenuItem("Pushover settings", pushover_settings),
            MenuItem("Open logs", lambda _icon, _item: open_logs()),
            MenuItem("Start at login", toggle_autostart, checked=lambda _item: is_autostart_enabled()),
            MenuItem("Quit", quit_app),
        ),
    )

    def refresh(icon_ref: pystray.Icon) -> None:
        icon_ref.visible = True
        previous_state = None
        previous_title = None
        while icon_ref._running:  # type: ignore[attr-defined]
            status, title = runtime_status(load_state(), controller.is_running())
            if status != previous_state:
                icon_ref.icon = icons.get(status, icons["error"])
                previous_state = status
            if title != previous_title:
                icon_ref.title = title
                previous_title = title
                icon_ref.update_menu()
            time.sleep(1)

    icon.run(setup=refresh)


def main() -> None:
    if should_run_notifier(sys.argv):
        import claude_reset_notifier

        asyncio.run(claude_reset_notifier.main())
        return
    run_tray()


if __name__ == "__main__":
    main()
