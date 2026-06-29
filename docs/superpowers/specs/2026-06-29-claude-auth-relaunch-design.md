# Claude Auth Relaunch Design

## Goal

When the notifier daemon cannot authenticate with Claude because no token exists or the token is rejected, it should launch the existing `run-claude-minimized.ps1` helper so Claude can refresh/login. It must not launch the helper for generic network or API polling failures.

## Behavior

- Trigger the helper only from auth-related failures:
  - `read_token()` returns no token.
  - `poll_api()` raises `AuthError` for HTTP `401` or `403`.
- Start `run-claude-minimized.ps1` with:
  - `powershell.exe`
  - `-NoLogo`
  - `-ExecutionPolicy Bypass`
  - `-File <script path>`
- Resolve the helper beside the notifier application directory using the existing `app_dir()` path convention.
- If the helper file is missing, log the failure and continue running the daemon.
- Record a runtime/state guard after attempting the launch so the daemon does not open repeated Claude windows while auth remains broken.
- Clear the guard after a successful poll so a future auth failure can launch Claude again.
- Do not block the daemon while the helper is running.

## Packaging

Source runs work when `run-claude-minimized.ps1` is present beside `claude_reset_notifier.py`.

PyInstaller builds must include `run-claude-minimized.ps1` beside or inside the packaged application path used by `app_dir()`. The implementation should keep this dependency explicit so build/install updates are easy to verify.

## Tests

Add focused tests for:

- no-token auth failure launches the helper once.
- rejected token auth failure launches the helper once.
- generic polling failure does not launch the helper.
- successful poll clears the launch guard.
- missing helper path does not crash.
