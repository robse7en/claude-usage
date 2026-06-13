#!/usr/bin/env python3
"""Notify via Pushover when Claude Code 5h or weekly blocks reset."""

from __future__ import annotations

import asyncio
import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
VENV_SITE_PACKAGES = SCRIPT_DIR / ".venv" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.exists():
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

import httpx  # noqa: E402


APP_NAME = "ClaudeUsage"
POLL_INTERVAL = 60
API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


class AuthError(Exception):
    """Claude credentials were rejected by the API."""


@dataclass(frozen=True)
class UsageSnapshot:
    five_hour_utilization: int
    five_hour_reset_ts: float
    five_hour_status: str
    weekly_utilization: int
    weekly_reset_ts: float


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / APP_NAME


def build_logger() -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_dir = app_data_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "notifier.log",
        maxBytes=512 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


LOGGER = build_logger()


def log(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    LOGGER.info(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return SCRIPT_DIR


def config_candidates() -> list[Path]:
    for env_name in ("CLAUDE_USAGE_CONFIG", "CLAUDE_RESET_NOTIFIER_CONFIG"):
        value = os.environ.get(env_name)
        if value:
            return [Path(value)]

    candidates = [app_data_dir() / "config.json", app_dir() / "config.json"]
    if SCRIPT_DIR not in (app_dir(), app_data_dir()):
        candidates.append(SCRIPT_DIR / "config.json")
    return candidates


def load_config() -> dict[str, Any]:
    config_path = next((path for path in config_candidates() if path.exists()), config_candidates()[0])
    config = load_json(config_path)

    env_map = {
        "PUSHOVER_APP_TOKEN": "pushover_app_token",
        "PUSHOVER_API_TOKEN": "pushover_app_token",
        "PUSHOVER_USER_KEY": "pushover_user_key",
        "PUSHOVER_DEVICE": "pushover_device",
        "PUSHOVER_SOUND": "pushover_sound",
        "CLAUDE_USAGE_POLL_SECONDS": "poll_interval_seconds",
        "CLAUDE_RESET_POLL_SECONDS": "poll_interval_seconds",
    }
    for env_name, key in env_map.items():
        value = os.environ.get(env_name)
        if value:
            config[key] = value

    config.setdefault("poll_interval_seconds", POLL_INTERVAL)
    return config


def credential_candidates() -> list[Path]:
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]

    home = Path.home()
    local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",
        local_appdata / "Claude" / ".credentials.json",
        appdata / "Claude" / ".credentials.json",
    ]


def extract_access_token(blob: str) -> str | None:
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        token = data.get("accessToken")
        if isinstance(token, str) and token.strip():
            return token.strip()
        for value in data.values():
            if isinstance(value, dict):
                token = value.get("accessToken")
                if isinstance(token, str) and token.strip():
                    return token.strip()

    match = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def read_token() -> str | None:
    for path in credential_candidates():
        try:
            token = extract_access_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if token:
            return token
    return None


def parse_reset_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        reset_ts = float(value)
    except ValueError:
        return 0.0
    return reset_ts if reset_ts > 0 else 0.0


def parse_percent(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(round(float(value) * 100))
    except ValueError:
        return 0


async def poll_api(token: str) -> UsageSnapshot | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            response = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as exc:
        log(f"API call failed: {exc}")
        return None

    if response.status_code in (401, 403):
        log(f"API HTTP {response.status_code}: {response.text[:200]}")
        raise AuthError(response.status_code)
    if response.status_code >= 400:
        log(f"API HTTP {response.status_code}: {response.text[:200]}")
        return None

    return UsageSnapshot(
        five_hour_utilization=parse_percent(
            response.headers.get("anthropic-ratelimit-unified-5h-utilization")
        ),
        five_hour_reset_ts=parse_reset_ts(
            response.headers.get("anthropic-ratelimit-unified-5h-reset")
        ),
        five_hour_status=response.headers.get(
            "anthropic-ratelimit-unified-5h-status", "unknown"
        ),
        weekly_utilization=parse_percent(
            response.headers.get("anthropic-ratelimit-unified-7d-utilization")
        ),
        weekly_reset_ts=parse_reset_ts(
            response.headers.get("anthropic-ratelimit-unified-7d-reset")
        ),
    )


def state_path() -> Path:
    return app_data_dir() / "state.json"


def command_path() -> Path:
    return app_data_dir() / "command.json"


def load_state() -> dict[str, Any]:
    return load_json(state_path())


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def runtime_state(state: dict[str, Any]) -> dict[str, Any]:
    value = state.get("runtime")
    if not isinstance(value, dict):
        value = {}
        state["runtime"] = value
    return value


def update_runtime(
    state: dict[str, Any],
    status: str,
    message: str = "",
    error: str = "",
) -> None:
    runtime = runtime_state(state)
    runtime.update(
        {
            "status": status,
            "message": message,
            "error": error,
            "pid": os.getpid(),
            "updated_at": time.time(),
            "updated_at_local": format_time(time.time()),
        }
    )
    save_state(state)


def read_command() -> str | None:
    path = command_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        return None
    command = data.get("command")
    return command if isinstance(command, str) else None


def format_time(timestamp: float) -> str:
    return format_display_time(timestamp)


def format_display_time(timestamp: float, now: dt.datetime | None = None) -> str:
    if timestamp <= 0:
        return "unknown"
    value = dt.datetime.fromtimestamp(timestamp).astimezone()
    current = now.astimezone(value.tzinfo) if now and now.tzinfo else now
    if current is None:
        current = dt.datetime.now(value.tzinfo)

    time_text = value.strftime("%I:%M %p").lstrip("0")
    if value.date() == current.date():
        return f"Today {time_text}"
    if value.date() == current.date() + dt.timedelta(days=1):
        return f"Tomorrow {time_text}"
    if value.date() < current.date() + dt.timedelta(days=7):
        return f"{value:%a} {time_text}"
    return f"{value:%b} {value.day} {time_text}"


def format_log_time(timestamp: float) -> str:
    if timestamp <= 0:
        return "unknown"
    return dt.datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M")


async def send_pushover(config: dict[str, Any], title: str, message: str) -> bool:
    token = str(config.get("pushover_app_token", "")).strip()
    user = str(config.get("pushover_user_key", "")).strip()
    if not token or not user:
        log("Pushover config missing pushover_app_token or pushover_user_key")
        return False

    data: dict[str, Any] = {
        "token": token,
        "user": user,
        "title": title,
        "message": message,
    }
    for config_key, pushover_key in (
        ("pushover_device", "device"),
        ("pushover_sound", "sound"),
    ):
        value = str(config.get(config_key, "")).strip()
        if value:
            data[pushover_key] = value

    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            response = await http.post(PUSHOVER_URL, data=data)
    except httpx.HTTPError as exc:
        log(f"Pushover send failed: {exc}")
        return False

    if response.status_code >= 400:
        log(f"Pushover HTTP {response.status_code}: {response.text[:200]}")
        return False
    return True


def bucket_state(state: dict[str, Any], bucket: str) -> dict[str, Any]:
    value = state.get(bucket)
    if not isinstance(value, dict):
        value = {}
        state[bucket] = value
    return value


async def maybe_notify_reset(
    config: dict[str, Any],
    state: dict[str, Any],
    bucket: str,
    display_name: str,
    current_reset_ts: float,
    utilization: int,
    status: str,
) -> bool:
    entry = bucket_state(state, bucket)
    last_known_reset_ts = float(entry.get("reset_ts") or 0)
    last_notified_reset_ts = float(entry.get("last_notified_reset_ts") or 0)
    now = time.time()

    if last_known_reset_ts <= 0:
        entry["reset_ts"] = current_reset_ts
        entry["utilization_percent"] = utilization
        entry["status"] = status
        log(f"Initialized {display_name} reset at {format_log_time(current_reset_ts)}")
        return False

    notified = False
    if now >= last_known_reset_ts and last_notified_reset_ts != last_known_reset_ts:
        next_line = (
            f"\nNext observed reset: {format_time(current_reset_ts)}"
            if current_reset_ts and current_reset_ts != last_known_reset_ts
            else ""
        )
        message = (
            f"{display_name} reset reached at {format_time(last_known_reset_ts)}."
            f"\nCurrent utilization: {utilization}%"
            f"\nStatus: {status}"
            f"{next_line}"
        )
        if await send_pushover(config, f"Claude {display_name} reset", message):
            entry["last_notified_reset_ts"] = last_known_reset_ts
            runtime_state(state)["last_notification"] = {
                "bucket": bucket,
                "label": display_name,
                "sent_at": time.time(),
                "sent_at_local": format_time(time.time()),
            }
            log(f"Sent Pushover notification for {display_name} reset")
            notified = True

    if current_reset_ts > 0:
        entry["reset_ts"] = current_reset_ts
    entry["utilization_percent"] = utilization
    entry["status"] = status
    entry["reset_at_local"] = format_time(current_reset_ts)
    return notified


async def run_once(config: dict[str, Any], state: dict[str, Any]) -> bool:
    update_runtime(state, "running", "Polling Claude usage")
    token = read_token()
    if not token:
        log("No Claude token found; run `claude login` and retry")
        update_runtime(state, "unauthenticated", "Run `claude login`", "No Claude token found")
        return False

    try:
        snapshot = await poll_api(token)
    except AuthError:
        log("Claude token rejected; run `claude login` and restart the notifier")
        update_runtime(
            state,
            "unauthenticated",
            "Run `claude login`",
            "Claude token rejected",
        )
        return False

    if snapshot is None:
        update_runtime(state, "error", "Polling failed", "Claude API request failed")
        return False

    five_notified = await maybe_notify_reset(
        config,
        state,
        "five_hour",
        "5h block",
        snapshot.five_hour_reset_ts,
        snapshot.five_hour_utilization,
        snapshot.five_hour_status,
    )
    weekly_notified = await maybe_notify_reset(
        config,
        state,
        "weekly",
        "weekly block",
        snapshot.weekly_reset_ts,
        snapshot.weekly_utilization,
        "allowed",
    )
    runtime = runtime_state(state)
    runtime["last_poll_at"] = time.time()
    runtime["last_poll_at_local"] = format_time(time.time())
    runtime["last_error"] = ""
    update_runtime(
        state,
        "running",
        "Notification sent" if five_notified or weekly_notified else "Waiting for reset",
    )
    save_state(state)
    log(
        "Polled usage: "
        f"5h={snapshot.five_hour_utilization}% reset={format_log_time(snapshot.five_hour_reset_ts)}; "
        f"weekly={snapshot.weekly_utilization}% reset={format_log_time(snapshot.weekly_reset_ts)}"
    )
    return True


async def main() -> None:
    parser = argparse.ArgumentParser(description="Claude usage Pushover notifier")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--test-pushover",
        action="store_true",
        help="send a test Pushover message and exit",
    )
    parser.add_argument("--daemon", action="store_true", help="run as tray-controlled daemon")
    args = parser.parse_args()

    config = load_config()
    state = load_state()

    if args.test_pushover:
        ok = await send_pushover(
            config,
            "Claude usage notifier test",
            "Pushover is configured correctly for Claude usage notifications.",
        )
        sys.exit(0 if ok else 1)

    if args.once:
        ok = await run_once(config, state)
        sys.exit(0 if ok else 1)

    poll_interval = int(config.get("poll_interval_seconds") or POLL_INTERVAL)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop(*_args: object) -> None:
        log("Stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, stop)
            except ValueError:
                pass

    log("=== Claude usage notifier started ===")
    log(f"Poll interval: {poll_interval}s")
    update_runtime(state, "running", "Started")
    while not stop_event.is_set():
        await run_once(config, state)
        deadline = time.time() + poll_interval
        while not stop_event.is_set() and time.time() < deadline:
            if read_command() == "poll_now":
                log("Poll requested by tray")
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
