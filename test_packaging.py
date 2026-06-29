import asyncio
import os
import tempfile
import unittest
import json
import datetime as dt
from pathlib import Path
from unittest import mock

os.environ["LOCALAPPDATA"] = str(Path.cwd() / ".test-localappdata")

import claude_reset_notifier
import tray_windows


class TrayPackagingTests(unittest.TestCase):
    def test_module_entrypoint_calls_dispatch_main(self) -> None:
        source = Path(__file__).with_name("tray_windows.py").read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":\n    main()', source)

    def test_build_includes_auth_relaunch_helper_script(self) -> None:
        source = Path(__file__).with_name("build-windows.ps1").read_text(encoding="utf-8")
        self.assertIn("run-claude-minimized.ps1", source)
        self.assertIn("--add-data", source)

    def test_frozen_autostart_uses_executable(self) -> None:
        exe = r"C:\Tools\ClaudeUsage.exe"
        with mock.patch.object(tray_windows.sys, "executable", exe), mock.patch(
            "tray_windows.is_frozen", return_value=True
        ):
            self.assertEqual(tray_windows.autostart_command(), f'"{exe}"')

    def test_frozen_daemon_command_reuses_executable(self) -> None:
        exe = r"C:\Tools\ClaudeUsage.exe"
        with mock.patch.object(tray_windows.sys, "executable", exe), mock.patch(
            "tray_windows.is_frozen", return_value=True
        ):
            self.assertEqual(tray_windows.notifier_command_args("--daemon"), [exe, "--daemon"])

    def test_source_daemon_command_uses_pythonw_and_script(self) -> None:
        with mock.patch("tray_windows.is_frozen", return_value=False), mock.patch(
            "tray_windows.pythonw_path", return_value=r"C:\Python\pythonw.exe"
        ):
            self.assertEqual(
                tray_windows.notifier_command_args("--test-pushover"),
                [
                    r"C:\Python\pythonw.exe",
                    str(tray_windows.SCRIPT_DIR / "claude_reset_notifier.py"),
                    "--test-pushover",
                ],
            )

    def test_load_pushover_config_returns_defaults_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(tray_windows.os.environ, {"LOCALAPPDATA": tmp}):
                self.assertEqual(
                    tray_windows.load_pushover_config(),
                    {
                        "pushover_app_token": "",
                        "pushover_user_key": "",
                        "pushover_device": "",
                        "pushover_sound": "",
                        "safety_refresh_seconds": 900,
                    },
                )

    def test_save_pushover_config_writes_local_app_data_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(tray_windows.os.environ, {"LOCALAPPDATA": tmp}):
                tray_windows.save_pushover_config(
                    {
                        "pushover_app_token": "app",
                        "pushover_user_key": "user",
                        "pushover_device": "phone",
                        "pushover_sound": "pushover",
                        "safety_refresh_seconds": "120",
                    }
                )

                path = Path(tmp) / "ClaudeUsage" / "config.json"
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    {
                        "pushover_app_token": "app",
                        "pushover_user_key": "user",
                        "pushover_device": "phone",
                        "pushover_sound": "pushover",
                        "safety_refresh_seconds": 120,
                    },
                )

    def test_save_pushover_config_clamps_invalid_safety_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(tray_windows.os.environ, {"LOCALAPPDATA": tmp}):
                tray_windows.save_pushover_config({"safety_refresh_seconds": "bad"})

                config = json.loads((Path(tmp) / "ClaudeUsage" / "config.json").read_text())
                self.assertEqual(config["safety_refresh_seconds"], 900)

    def test_apply_pushover_settings_restarts_running_daemon(self) -> None:
        controller = mock.Mock()
        controller.is_running.return_value = True
        with mock.patch("tray_windows.save_pushover_config") as save_config:
            tray_windows.apply_pushover_settings({"pushover_app_token": "app"}, controller)

        save_config.assert_called_once_with({"pushover_app_token": "app"})
        controller.stop.assert_called_once_with()
        controller.start.assert_called_once_with()

    def test_apply_pushover_settings_does_not_start_stopped_daemon(self) -> None:
        controller = mock.Mock()
        controller.is_running.return_value = False
        with mock.patch("tray_windows.save_pushover_config"):
            tray_windows.apply_pushover_settings({"pushover_app_token": "app"}, controller)

        controller.stop.assert_not_called()
        controller.start.assert_not_called()

    def test_runtime_status_uses_connected_wording(self) -> None:
        with mock.patch("tray_windows.time.time", return_value=1000):
            self.assertEqual(
                tray_windows.runtime_status(
                    {"runtime": {"status": "running", "updated_at": 999, "last_poll_at_local": "Today 2:30 AM"}},
                    True,
                ),
                ("running", "Connected - last poll Today 2:30 AM"),
            )

    def test_runtime_status_waiting_uses_connected_wording(self) -> None:
        self.assertEqual(
            tray_windows.runtime_status({}, True),
            ("running", "Connected - waiting for first status"),
        )

    def test_build_icons_uses_pixel_creature_with_status_badge(self) -> None:
        icons = tray_windows.build_icons()
        running = icons["running"]

        self.assertEqual(running.size, (64, 64))
        self.assertEqual(running.mode, "RGBA")
        self.assertEqual(running.getpixel((30, 28))[:3], (211, 132, 107))
        self.assertEqual(running.getpixel((52, 52))[:3], (32, 160, 96))


class NotifierPackagingTests(unittest.TestCase):
    def test_config_candidates_prefer_env_override(self) -> None:
        with mock.patch.dict(
            claude_reset_notifier.os.environ,
            {"CLAUDE_RESET_NOTIFIER_CONFIG": r"C:\Config\custom.json"},
        ):
            self.assertEqual(
                claude_reset_notifier.config_candidates(),
                [Path(r"C:\Config\custom.json")],
            )

    def test_frozen_config_candidates_include_app_data_and_exe_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            exe = tmp_path / "dist" / "ClaudeUsage.exe"
            local_app_data = tmp_path / "local"
            with mock.patch.object(claude_reset_notifier.sys, "executable", str(exe)), mock.patch(
                "claude_reset_notifier.is_frozen", return_value=True
            ), mock.patch.dict(
                claude_reset_notifier.os.environ,
                {"LOCALAPPDATA": str(local_app_data)},
                clear=False,
            ):
                self.assertEqual(
                    claude_reset_notifier.config_candidates()[:2],
                    [
                        local_app_data / "ClaudeUsage" / "config.json",
                        exe.parent / "config.json",
                    ],
                )

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
            with mock.patch(
                "claude_reset_notifier.auth_relaunch_script_candidates",
                return_value=[Path(tmp) / "missing.ps1"],
            ), mock.patch("claude_reset_notifier.subprocess.Popen") as popen:
                self.assertFalse(
                    claude_reset_notifier.maybe_launch_auth_relaunch(
                        state,
                        "No Claude token found",
                    )
                )

            popen.assert_not_called()
            self.assertTrue(state["runtime"]["auth_relaunch_attempted"])

    def test_maybe_launch_auth_relaunch_starts_existing_script_with_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "run-claude-minimized.ps1"
            script.write_text("", encoding="utf-8")
            state: dict[str, object] = {}
            with mock.patch(
                "claude_reset_notifier.auth_relaunch_script_candidates",
                return_value=[script],
            ), mock.patch("claude_reset_notifier.subprocess.Popen") as popen:
                self.assertTrue(
                    claude_reset_notifier.maybe_launch_auth_relaunch(
                        state,
                        "No Claude token found",
                    )
                )

            popen.assert_called_once_with(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                cwd=str(script.parent),
                stdin=claude_reset_notifier.subprocess.DEVNULL,
                stdout=claude_reset_notifier.subprocess.DEVNULL,
                stderr=claude_reset_notifier.subprocess.DEVNULL,
                creationflags=getattr(claude_reset_notifier.subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertTrue(state["runtime"]["auth_relaunch_attempted"])

    def test_maybe_launch_auth_relaunch_skips_when_already_attempted(self) -> None:
        state: dict[str, object] = {"runtime": {"auth_relaunch_attempted": True}}
        with mock.patch("claude_reset_notifier.subprocess.Popen") as popen:
            self.assertFalse(
                claude_reset_notifier.maybe_launch_auth_relaunch(
                    state,
                    "Claude token rejected",
                )
            )

        popen.assert_not_called()

    def test_run_once_launches_auth_relaunch_once_when_token_missing(self) -> None:
        state: dict[str, object] = {}
        with mock.patch("claude_reset_notifier.read_token", return_value=None), mock.patch(
            "claude_reset_notifier.maybe_launch_auth_relaunch",
            return_value=True,
        ) as launch:
            self.assertFalse(asyncio.run(claude_reset_notifier.run_once({}, state)))

        launch.assert_called_once_with(state, "No Claude token found")

    def test_run_once_launches_auth_relaunch_once_when_token_rejected(self) -> None:
        async def rejected(_token: str):
            raise claude_reset_notifier.AuthError(401)

        state: dict[str, object] = {}
        with mock.patch("claude_reset_notifier.read_token", return_value="token"), mock.patch(
            "claude_reset_notifier.poll_api",
            rejected,
        ), mock.patch("claude_reset_notifier.maybe_launch_auth_relaunch", return_value=True) as launch:
            self.assertFalse(asyncio.run(claude_reset_notifier.run_once({}, state)))

        launch.assert_called_once_with(state, "Claude token rejected")

    def test_run_once_does_not_launch_auth_relaunch_for_generic_poll_failure(self) -> None:
        async def failed(_token: str):
            return None

        state: dict[str, object] = {}
        with mock.patch("claude_reset_notifier.read_token", return_value="token"), mock.patch(
            "claude_reset_notifier.poll_api",
            failed,
        ), mock.patch("claude_reset_notifier.maybe_launch_auth_relaunch") as launch:
            self.assertFalse(asyncio.run(claude_reset_notifier.run_once({}, state)))

        launch.assert_not_called()

    def test_run_once_clears_auth_relaunch_guard_after_successful_poll(self) -> None:
        async def successful(_token: str):
            return claude_reset_notifier.UsageSnapshot(0, 0.0, "allowed", 0, 0.0)

        state: dict[str, object] = {"runtime": {"auth_relaunch_attempted": True}}
        with mock.patch("claude_reset_notifier.read_token", return_value="token"), mock.patch(
            "claude_reset_notifier.poll_api",
            successful,
        ), mock.patch("claude_reset_notifier.send_pushover", return_value=False):
            self.assertTrue(asyncio.run(claude_reset_notifier.run_once({}, state)))

        self.assertNotIn("auth_relaunch_attempted", state["runtime"])

    def test_format_display_time_uses_today_for_current_date(self) -> None:
        now = dt.datetime(2026, 6, 13, 22, 0)
        value = dt.datetime(2026, 6, 13, 2, 30)
        self.assertEqual(
            claude_reset_notifier.format_display_time(value.timestamp(), now=now),
            "Today 2:30 AM",
        )

    def test_format_display_time_uses_weekday_for_near_future_date(self) -> None:
        now = dt.datetime(2026, 6, 13, 22, 0)
        value = dt.datetime(2026, 6, 16, 19, 0)
        self.assertEqual(
            claude_reset_notifier.format_display_time(value.timestamp(), now=now),
            "Tue 7:00 PM",
        )

    def test_format_log_time_omits_timezone_name(self) -> None:
        value = dt.datetime(2026, 6, 14, 2, 30)
        self.assertEqual(
            claude_reset_notifier.format_log_time(value.timestamp()),
            "2026-06-14 02:30",
        )

    def test_poll_api_uses_rate_limit_headers_from_429_response(self) -> None:
        reset_ts = 1781403600
        weekly_reset_ts = 1781593200

        def handler(request: claude_reset_notifier.httpx.Request) -> claude_reset_notifier.httpx.Response:
            return claude_reset_notifier.httpx.Response(
                429,
                headers={
                    "anthropic-ratelimit-unified-5h-utilization": "1.0",
                    "anthropic-ratelimit-unified-5h-reset": str(reset_ts),
                    "anthropic-ratelimit-unified-5h-status": "rejected",
                    "anthropic-ratelimit-unified-7d-utilization": "0.29",
                    "anthropic-ratelimit-unified-7d-reset": str(weekly_reset_ts),
                },
                json={"type": "error", "error": {"type": "rate_limit_error"}},
                request=request,
            )

        real_client = claude_reset_notifier.httpx.AsyncClient

        def client_factory(*_args: object, **_kwargs: object) -> claude_reset_notifier.httpx.AsyncClient:
            return real_client(
                transport=claude_reset_notifier.httpx.MockTransport(handler)
            )

        with mock.patch.object(claude_reset_notifier.httpx, "AsyncClient", client_factory):
            snapshot = asyncio.run(claude_reset_notifier.poll_api("token"))

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.five_hour_utilization, 100)
        self.assertEqual(snapshot.five_hour_reset_ts, reset_ts)
        self.assertEqual(snapshot.five_hour_status, "rejected")
        self.assertEqual(snapshot.weekly_utilization, 29)
        self.assertEqual(snapshot.weekly_reset_ts, weekly_reset_ts)

    def test_next_poll_delay_uses_nearest_reset_before_safety_refresh(self) -> None:
        state = {
            "five_hour": {"reset_ts": 1_700_000_120.0},
            "weekly": {"reset_ts": 1_700_003_600.0},
        }

        self.assertEqual(
            claude_reset_notifier.next_poll_delay(
                state,
                now=1_700_000_000.0,
                safety_refresh_seconds=900,
            ),
            120.0,
        )

    def test_next_poll_delay_uses_safety_refresh_when_resets_are_later(self) -> None:
        state = {
            "five_hour": {"reset_ts": 1_700_003_600.0},
            "weekly": {"reset_ts": 1_700_007_200.0},
        }

        self.assertEqual(
            claude_reset_notifier.next_poll_delay(
                state,
                now=1_700_000_000.0,
                safety_refresh_seconds=900,
            ),
            900.0,
        )

    def test_save_state_retries_transient_replace_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ClaudeUsage" / "state.json"
            original_replace = Path.replace
            attempts = 0

            def flaky_replace(self: Path, target_path: Path) -> Path:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError("simulated transient Windows lock")
                return original_replace(self, target_path)

            with mock.patch("claude_reset_notifier.state_path", return_value=target), mock.patch.object(
                claude_reset_notifier.time, "sleep", return_value=None
            ), mock.patch.object(Path, "replace", flaky_replace):
                claude_reset_notifier.save_state({"runtime": {"status": "running"}})

            self.assertEqual(attempts, 2)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"runtime": {"status": "running"}},
            )

    def test_save_state_does_not_crash_when_replace_remains_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ClaudeUsage" / "state.json"
            attempts = 0

            def locked_replace(self: Path, target_path: Path) -> Path:
                nonlocal attempts
                attempts += 1
                raise PermissionError("simulated persistent Windows lock")

            with mock.patch("claude_reset_notifier.state_path", return_value=target), mock.patch.object(
                claude_reset_notifier.time, "sleep", return_value=None
            ), mock.patch.object(Path, "replace", locked_replace):
                claude_reset_notifier.save_state({"runtime": {"status": "running"}})

            self.assertGreater(attempts, 1)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
