# Claude usage + reset notifier

Small Windows tray notifier for Claude Code usage reset events.

It reads the Claude Code OAuth token from the Windows Claude credentials file,
makes a minimal Anthropic API call, and inspects the returned rate-limit
headers.

## What it notifies

- 5h block reset
- weekly reset

The daemon sends one Pushover message when the previously observed reset time
is reached. It initializes silently on first run so it does not send stale
messages.

The tray app monitors and controls the daemon. It shows:

- running, unauthenticated, error, or stopped status
- current 5h block usage and reset time
- current weekly usage and reset time
- last notification sent

The tray menu includes:

- Start daemon
- Stop daemon
- Poll now
- Send test Pushover
- Open logs
- Start at login
- Quit

## Install

From this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
```

The installer creates `.venv`, installs `httpx`, `pystray`, and `Pillow`,
registers a per-user login autostart entry, and starts the tray.

Configure Pushover from the tray menu:

```text
Pushover settings
```

Settings are saved to `%LOCALAPPDATA%\ClaudeUsage\config.json`, not to the repo
folder. This keeps private Pushover keys out of source control.

## Build single EXE

From this folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\build-windows.ps1
```

The packaged app is written to:

```powershell
.\dist\ClaudeUsage.exe
```

For the EXE build, configure Pushover from the tray menu. Settings are saved to
`%LOCALAPPDATA%\ClaudeUsage\config.json`. Environment variables still override
file-based configuration.

## Logs and state

Logs:

```powershell
Get-Content "$env:LOCALAPPDATA\ClaudeUsage\notifier.log" -Tail 50
```

State:

```powershell
$env:LOCALAPPDATA\ClaudeUsage\state.json
```

## Manual run

```powershell
.\.venv\Scripts\python.exe .\claude_reset_notifier.py
```

Run the tray manually:

```powershell
.\.venv\Scripts\python.exe .\tray_windows.py
```

Send a test Pushover message:

```powershell
.\.venv\Scripts\python.exe .\claude_reset_notifier.py --test-pushover
```

Poll once and exit:

```powershell
.\.venv\Scripts\python.exe .\claude_reset_notifier.py --once
```

## Disable autostart

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall-windows.ps1
```
