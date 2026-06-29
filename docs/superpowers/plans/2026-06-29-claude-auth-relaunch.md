# Claude Auth Relaunch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Launch the existing `run-claude-minimized.ps1` helper once when the daemon detects missing or rejected Claude authentication.

**Architecture:** Keep the window-control behavior in PowerShell and add a small Python launcher in `claude_reset_notifier.py`. Store a guard in runtime state so repeated auth failures do not repeatedly open Claude; clear that guard after a successful poll.

**Tech Stack:** Python `asyncio`, `subprocess`, `unittest.mock`, PowerShell, PyInstaller.

---

## File Structure

- Modify `claude_reset_notifier.py`: import `subprocess`; add helper path resolution and launch helpers; call the helper from auth-only failure branches; clear the guard on success.
- Modify `test_packaging.py`: add unit tests for auth-only launch behavior, one-shot guard behavior, success reset, and missing helper handling.
- Modify `build-windows.ps1`: include `run-claude-minimized.ps1` as PyInstaller data so frozen builds can locate it.

### Task 1: Add Failing Tests For Auth Relaunch

**Files:**
- Modify: `test_packaging.py`

- [ ] **Step 1: Add tests for launcher path, missing helper, and auth branches**

Add these tests to `NotifierPackagingTests`:

```python
    def test_auth_relaunch_script_candidates_include_exe_dir_and_meipass_when_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "dist" / "ClaudeUsage.exe"
            meipass = tmp_path / "bundle"
            with mock.patch.object(claude_reset_notifier.sys, "executable", str(exe)), mock.patch(
                "claude_reset_notifier.is_frozen", return_value=True
            ), mock.patch.object(claude_reset_notifier.sys, "_MEIPASS", str(meipass), create=True):
                self.assertEqual(
                    claude_reset_notifier.auth_relaunch_script_candidates(),
                    [
                        exe.parent / "run-claude-minimized.ps1",
                        meipass / "run-claude-minimized.ps1",
                    ],
                )

    def test_maybe_launch_auth_relaunch_skips_missing_script_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state: dict[str, object] = {}
            with mock.patch("claude_reset_notifier.auth_relaunch_script_candidates", return_value=[Path(tmp) / "missing.ps1"]), mock.patch(
                "claude_reset_notifier.subprocess.Popen"
            ) as popen:
                self.assertFalse(claude_reset_notifier.maybe_launch_auth_relaunch(state, "No Claude token found"))

            popen.assert_not_called()
            self.assertTrue(state["runtime"]["auth_relaunch_attempted"])

    def test_run_once_launches_auth_relaunch_once_when_token_missing(self) -> None:
        state: dict[str, object] = {}
        with mock.patch("claude_reset_notifier.read_token", return_value=None), mock.patch(
            "claude_reset_notifier.maybe_launch_auth_relaunch", return_value=True
        ) as launch:
            self.assertFalse(asyncio.run(claude_reset_notifier.run_once({}, state)))

        launch.assert_called_once_with(state, "No Claude token found")

    def test_run_once_launches_auth_relaunch_once_when_token_rejected(self) -> None:
        async def rejected(_token: str):
            raise claude_reset_notifier.AuthError(401)

        state: dict[str, object] = {}
        with mock.patch("claude_reset_notifier.read_token", return_value="token"), mock.patch(
            "claude_reset_notifier.poll_api", rejected
        ), mock.patch("claude_reset_notifier.maybe_launch_auth_relaunch", return_value=True) as launch:
            self.assertFalse(asyncio.run(claude_reset_notifier.run_once({}, state)))

        launch.assert_called_once_with(state, "Claude token rejected")

    def test_run_once_does_not_launch_auth_relaunch_for_generic_poll_failure(self) -> None:
        async def failed(_token: str):
            return None

        state: dict[str, object] = {}
        with mock.patch("claude_reset_notifier.read_token", return_value="token"), mock.patch(
            "claude_reset_notifier.poll_api", failed
        ), mock.patch("claude_reset_notifier.maybe_launch_auth_relaunch") as launch:
            self.assertFalse(asyncio.run(claude_reset_notifier.run_once({}, state)))

        launch.assert_not_called()

    def test_run_once_clears_auth_relaunch_guard_after_successful_poll(self) -> None:
        async def successful(_token: str):
            return claude_reset_notifier.UsageSnapshot(0, 0.0, "allowed", 0, 0.0)

        state: dict[str, object] = {"runtime": {"auth_relaunch_attempted": True}}
        with mock.patch("claude_reset_notifier.read_token", return_value="token"), mock.patch(
            "claude_reset_notifier.poll_api", successful
        ), mock.patch("claude_reset_notifier.send_pushover", return_value=False):
            self.assertTrue(asyncio.run(claude_reset_notifier.run_once({}, state)))

        self.assertNotIn("auth_relaunch_attempted", state["runtime"])
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_packaging.NotifierPackagingTests
```

Expected: fail because `auth_relaunch_script_candidates` and `maybe_launch_auth_relaunch` do not exist, and `run_once()` does not call the launcher.

### Task 2: Implement Auth Relaunch Helper

**Files:**
- Modify: `claude_reset_notifier.py`

- [ ] **Step 1: Add the subprocess import and constant**

Near the imports/constants:

```python
import subprocess

AUTH_RELAUNCH_SCRIPT = "run-claude-minimized.ps1"
```

- [ ] **Step 2: Add helper path resolution and launch code**

Add after `app_dir()`:

```python
def auth_relaunch_script_candidates() -> list[Path]:
    candidates = [app_dir() / AUTH_RELAUNCH_SCRIPT]
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bundled = Path(meipass) / AUTH_RELAUNCH_SCRIPT
        if bundled not in candidates:
            candidates.append(bundled)
    return candidates


def auth_relaunch_script_path() -> Path | None:
    return next((path for path in auth_relaunch_script_candidates() if path.exists()), None)


def maybe_launch_auth_relaunch(state: dict[str, Any], reason: str) -> bool:
    runtime = runtime_state(state)
    if runtime.get("auth_relaunch_attempted"):
        log(f"Claude auth relaunch already attempted; skipping ({reason})")
        return False

    runtime["auth_relaunch_attempted"] = True
    runtime["auth_relaunch_attempted_at"] = time.time()
    runtime["auth_relaunch_reason"] = reason
    save_state(state)

    script = auth_relaunch_script_path()
    if script is None:
        log(
            "Claude auth relaunch helper not found; checked "
            + ", ".join(str(path) for path in auth_relaunch_script_candidates())
        )
        return False

    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            cwd=str(script.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        log(f"Failed to launch Claude auth helper {script}: {exc}")
        return False

    log(f"Launched Claude auth helper: {script}")
    return True
```

- [ ] **Step 3: Call the helper from auth-only branches and clear on success**

In `run_once()`:

```python
    if not token:
        log("No Claude token found; run `claude login` and retry")
        update_runtime(state, "unauthenticated", "Run `claude login`", "No Claude token found")
        maybe_launch_auth_relaunch(state, "No Claude token found")
        return False
```

In the `except AuthError` branch:

```python
        update_runtime(
            state,
            "unauthenticated",
            "Run `claude login`",
            "Claude token rejected",
        )
        maybe_launch_auth_relaunch(state, "Claude token rejected")
        return False
```

After a successful snapshot:

```python
    runtime = runtime_state(state)
    runtime.pop("auth_relaunch_attempted", None)
    runtime.pop("auth_relaunch_attempted_at", None)
    runtime.pop("auth_relaunch_reason", None)
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_packaging.NotifierPackagingTests
```

Expected: all tests pass.

### Task 3: Package The Helper Script

**Files:**
- Modify: `build-windows.ps1`
- Modify: `test_packaging.py`

- [ ] **Step 1: Add a build-script test**

Add to `TrayPackagingTests`:

```python
    def test_build_includes_auth_relaunch_helper_script(self) -> None:
        source = Path(__file__).with_name("build-windows.ps1").read_text(encoding="utf-8")
        self.assertIn("run-claude-minimized.ps1", source)
        self.assertIn("--add-data", source)
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_packaging.TrayPackagingTests.test_build_includes_auth_relaunch_helper_script
```

Expected: fail because `build-windows.ps1` does not include the helper yet.

- [ ] **Step 3: Include the helper in PyInstaller data**

In `build-windows.ps1`, add this PyInstaller argument before the hidden import:

```powershell
    --add-data "$(Join-Path $Root "run-claude-minimized.ps1");." `
```

- [ ] **Step 4: Run the packaging test**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_packaging.TrayPackagingTests.test_build_includes_auth_relaunch_helper_script
```

Expected: pass.

### Task 4: Full Verification

**Files:**
- No code changes.

- [ ] **Step 1: Run the full test suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_packaging.py
```

Expected: all tests pass.

- [ ] **Step 2: Inspect git diff**

Run:

```powershell
git diff -- claude_reset_notifier.py test_packaging.py build-windows.ps1 docs/superpowers/specs/2026-06-29-claude-auth-relaunch-design.md docs/superpowers/plans/2026-06-29-claude-auth-relaunch.md
```

Expected: changes are limited to auth relaunch behavior, tests, packaging, and planning docs.
