"""Behavior tests for the dependency-free Codex Claude Vitals dashboard."""

import importlib
import json
import os
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))
codex_pulse = importlib.import_module("ai_vitals")


class SessionSummaryTests(unittest.TestCase):
    """Verify JSONL parsing and the loopback dashboard contract."""

    def setUp(self):
        """Create one complete synthetic Codex session."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temp_dir.name) / "codex"
        self.state_home = Path(self.temp_dir.name) / "state"
        self.claude_home = Path(self.temp_dir.name) / "claude"
        self.agy_home = Path(self.temp_dir.name) / "agy"
        self.agy_home.mkdir(parents=True, exist_ok=True)
        self.session_path = self.codex_home / "sessions" / "2026" / "09" / "session.jsonl"
        self.session_path.parent.mkdir(parents=True)
        records = [
            {
                "type": "session_meta",
                "timestamp": "2026-09-06T00:00:00Z",
                "payload": {"session_id": "demo", "cwd": "/work/demo"},
            },
            {
                "type": "turn_context",
                "timestamp": "2026-09-06T00:00:01Z",
                "payload": {"model": "gpt-5.5", "effort": "medium"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-09-06T00:00:02Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "<recommended_plugins>\nplugin list"}
                    ],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-09-06T00:00:03Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Build the dashboard\nwith compact cards"}
                    ],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-06T00:00:04Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 200,
                            "cached_input_tokens": 50,
                            "output_tokens": 10,
                        },
                        "total_token_usage": {
                            "input_tokens": 800,
                            "cached_input_tokens": 400,
                            "output_tokens": 20,
                        },
                        "model_context_window": 1000,
                    },
                    "rate_limits": {
                        "primary": {
                            "used_percent": 28.1,
                            "window_minutes": 300,
                            "resets_at": 1_788_686_400,
                        },
                        "secondary": {
                            "used_percent": 37,
                            "window_minutes": 10080,
                            "resets_at": 1_788_940_800,
                        },
                    },
                },
            },
        ]
        self.session_path.write_text(
            "\n".join(json.dumps(record) for record in records), encoding="utf-8"
        )

    def tearDown(self):
        """Remove the temporary session hierarchy."""
        self.temp_dir.cleanup()

    def test_summarize_session_reads_limits_context_and_task_metadata(self):
        """Extract structured usage and the first genuine user prompt."""
        summary = codex_pulse.summarize_session(self.session_path)
        if summary is None:
            self.fail("synthetic session was not summarized")

        self.assertEqual(summary["limits"]["primary"]["label"], "5h")
        self.assertEqual(summary["limits"]["primary"]["used_percent"], 28)
        self.assertEqual(summary["context"]["used_percent"], 20)
        self.assertEqual(summary["task"]["cwd"], "/work/demo")
        self.assertEqual(summary["task"]["prompt"], "Build the dashboard")
        self.assertEqual(summary["model"], "gpt-5.5")
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.00041125)

    def test_api_snapshot_and_dashboard_are_served_on_loopback(self):
        """Serve both API and dashboard through the local server."""
        with running_server(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        ) as base_url:
            with urlopen(base_url + "/api/snapshot") as response:  # nosec B310
                snapshot = json.load(response)
            with urlopen(base_url + "/") as response:  # nosec B310
                page = response.read().decode("utf-8")

        self.assertEqual(snapshot["current"]["task"]["cwd"], "/work/demo")
        self.assertIn("5h", page)
        self.assertIn("解除:", page)
        self.assertIn("used_tokens", page)
        self.assertIn("window_tokens", page)
        self.assertIn("OpenAI Status", page)
        self.assertIn("status-groups", page)
        self.assertIn("task-index", page)
        self.assertIn("task-time", page)
        self.assertIn(".task-head b{min-width:0}", page)
        self.assertIn(
            "const indicatorColor=value=>value>=80?'#ff7a7a':value>=60?'#ffce6a':'#72dfb5'", page
        )
        self.assertIn("AI Vitals", page)
        self.assertIn("Claude Code", page)

    def test_snapshot_reads_claude_usage_and_keeps_ten_recent_blocks(self):
        """Expose Claude Code session totals and retain only ten watch blocks."""
        claude_session = self.claude_home / "projects" / "demo" / "session.jsonl"
        claude_session.parent.mkdir(parents=True)
        claude_session.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-06T02:00:00Z",
                    "sessionId": "claude-demo",
                    "message": {
                        "model": "claude-sonnet-demo",
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 20,
                            "cache_read_input_tokens": 30,
                            "output_tokens": 40,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        watch_log = self.state_home / "codex-usage-monitor" / "watch.log"
        watch_log.parent.mkdir(parents=True)
        watch_log.write_text("\n\n".join(f"block-{index}" for index in range(12)), encoding="utf-8")

        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )

        self.assertEqual(len(snapshot["history"]), 12)
        self.assertEqual(snapshot["history"][0], "block-0")
        self.assertEqual(snapshot["claude"]["model"], "claude-sonnet-demo")
        self.assertEqual(snapshot["claude"]["usage"]["total_tokens"], 190)
        self.assertIsNone(snapshot["claude"]["estimated_cost_usd"])

    def test_claude_status_groups_include_claude_code_incidents(self):
        """Expose Claude Code's public status with a 90-day incident marker."""
        groups = codex_pulse.status_groups(
            [{"name": "Claude Code", "status": "operational"}],
            [
                {
                    "name": "Claude Code degraded performance",
                    "impact": "minor",
                    "created_at": "2026-09-06T01:00:00Z",
                }
            ],
            date(2026, 9, 6),
            names=("Claude Code",),
        )

        self.assertEqual(groups[0]["name"], "Claude Code")
        self.assertEqual(groups[0]["days"][-1], "warn")

    def test_claude_task_is_merged_with_codex_active_tasks(self):
        """Show a genuine Claude user prompt alongside Codex tasks."""
        claude_session = self.claude_home / "projects" / "demo" / "active.jsonl"
        claude_session.parent.mkdir(parents=True)
        claude_session.write_text(
            "\n".join(
                json.dumps(record)
                for record in (
                    {
                        "type": "user",
                        "timestamp": "2026-09-06T02:00:00Z",
                        "cwd": "/work/claude",
                        "message": {"content": "Review the parser\nwith a short summary"},
                    },
                    {
                        "type": "assistant",
                        "timestamp": "2026-09-06T02:00:01Z",
                        "message": {
                            "model": "claude-sonnet-5",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        },
                    },
                )
            ),
            encoding="utf-8",
        )

        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )

        claude_task = next(task for task in snapshot["tasks"] if task["provider"] == "Claude")
        self.assertEqual(claude_task["task"]["prompt"], "Review the parser")
        self.assertEqual(claude_task["task"]["cwd"], "/work/claude")

    def test_claude_sonnet_cost_is_a_range_when_cache_ttl_is_unknown(self):
        """Avoid a false-precise Claude API-equivalent cost for cache creation."""
        estimate = codex_pulse.claude_usage_cost(
            "claude-sonnet-5",
            {
                "input_tokens": 1_000_000,
                "cache_creation_input_tokens": 1_000_000,
                "cache_read_input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            },
        )

        self.assertEqual(estimate, {"minimum_usd": 14.7, "maximum_usd": 16.2})

    def test_dashboard_exposes_reorderable_provider_blocks(self):
        """Return independent provider blocks and a local layout settings entry point."""
        page = codex_pulse.dashboard_html()

        self.assertIn('id="layout-settings"', page)
        self.assertIn('id="notify-toggle"', page)
        self.assertIn('data-block="overview"', page)
        self.assertIn('data-block="codex"', page)
        self.assertIn('data-block="claude"', page)
        self.assertIn('data-block="agy"', page)
        self.assertIn('data-block="tasks"', page)
        self.assertIn('data-block="history"', page)
        self.assertIn('id="ov-tasks"', page)
        self.assertIn('id="ov-cost"', page)
        self.assertIn('id="ov-tokens"', page)
        self.assertIn('id="ov-active-tool"', page)
        self.assertIn('id="overview-health-badge"', page)
        self.assertIn('id="codex-sync-badge"', page)
        self.assertIn('id="claude-sync-badge"', page)
        self.assertIn('id="agy-sync-badge"', page)
        self.assertIn('id="openai-status-badge"', page)
        self.assertIn('id="claude-status-badge"', page)
        self.assertIn('id="google-status-badge"', page)

    def test_dashboard_html_contains_vitals_monitoring_features(self):
        """Verify dashboard HTML includes the 5 enhanced monitoring features."""
        page = codex_pulse.dashboard_html()

        # 1. Realtime Countdown
        self.assertIn("formatRemaining", page)
        self.assertIn("updateCountdowns", page)
        self.assertIn("data-resets-at", page)

        # 2. Burn Rate
        self.assertIn("burnRateBadge", page)
        self.assertIn(".burn-badge", page)
        self.assertIn("⚡ 安全ペース", page)
        self.assertIn("⚡ ハイペース注意", page)

        # 3. Overview
        self.assertIn("renderOverview", page)
        self.assertIn("Today's Overview", page)

        # 4. Bloat Alert
        self.assertIn("renderBloat", page)
        self.assertIn(".bloat-alert", page)
        self.assertIn("⚠️ 会話肥大化", page)

        # 5. Desktop Notifications
        self.assertIn("checkNotify", page)
        self.assertIn("updateNotifyButton", page)
        self.assertIn("notifyEnabled", page)
        self.assertIn("Notification.requestPermission", page)

    def test_dashboard_html_contains_color_themes(self):
        """Verify dashboard HTML includes color theme selectors and styles."""
        page = codex_pulse.dashboard_html()

        self.assertIn('id="theme-select"', page)
        self.assertIn('id="theme-select-dialog"', page)
        self.assertIn('html[data-theme="dracula"]', page)
        self.assertIn('html[data-theme="cyberpunk"]', page)
        self.assertIn('html[data-theme="synthwave"]', page)
        self.assertIn('html[data-theme="monokai"]', page)
        self.assertIn('html[data-theme="nord"]', page)
        self.assertIn('html[data-theme="github-light"]', page)
        self.assertIn('html[data-theme="solarized-light"]', page)
        self.assertIn('html[data-theme="one-light"]', page)
        self.assertIn(".btn-small,.layout-button,#shutdown", page)
        self.assertIn("applyTheme", page)
        self.assertIn("ai-vitals.theme", page)

    def test_dashboard_exposes_a_confirmed_stop_button(self):
        """Let the local dashboard stop its own server after confirmation."""
        page = codex_pulse.dashboard_html()

        self.assertIn('id="shutdown"', page)
        self.assertIn("/api/shutdown", page)
        self.assertIn("confirm('AI Vitalsを停止しますか？')", page)

    def test_dashboard_exposes_explicit_subscription_plan_selectors(self):
        """Let people label subscription plans without inspecting account credentials."""
        page = codex_pulse.dashboard_html()

        self.assertIn('id="codex-plan-select"', page)
        self.assertIn('id="claude-plan-select"', page)
        self.assertIn('id="codex-plan-view"', page)
        self.assertIn('id="claude-plan-view"', page)
        self.assertIn("API 従量課金", page)

    def test_dashboard_exposes_limit_usage_and_remaining_indicators(self):
        """Show elapsed and remaining usage directly beside each limit bar."""
        page = codex_pulse.dashboard_html()

        self.assertIn('id="primary-detail"', page)
        self.assertIn('id="secondary-detail"', page)
        self.assertIn('id="claude-primary-detail"', page)
        self.assertIn('id="claude-secondary-detail"', page)
        self.assertIn('id="context-remaining"', page)

    def test_background_command_starts_a_server_without_reopening_the_browser(self):
        """Keep -b out of the child command and retain its custom port."""
        command = codex_pulse.background_command(["-b", "--port", "4300"])

        self.assertEqual(
            command,
            [
                sys.executable,
                str(Path(codex_pulse.__file__).resolve()),
                "--port",
                "4300",
                "--no-open",
            ],
        )
        self.assertTrue(codex_pulse.detached_process_options("posix")["start_new_session"])
        self.assertEqual(
            codex_pulse.detached_process_options("posix")["stdout"], subprocess.DEVNULL
        )

    def test_open_dashboard_delegates_to_the_default_browser(self):
        """Open the local URL through the platform's configured browser."""
        opened: list[str] = []

        result = codex_pulse.open_dashboard("http://127.0.0.1:4202", opened.append)

        self.assertIsNone(result)
        self.assertEqual(opened, ["http://127.0.0.1:4202"])

    def test_save_claude_usage_stores_plan_limits_for_the_dashboard(self):
        """Persist the manually read /usage values without saving credentials."""
        codex_pulse.save_claude_usage(
            self.state_home,
            primary_used_percent=25,
            primary_resets_at=1_788_686_400,
            secondary_used_percent=70,
            secondary_resets_at=1_788_940_800,
        )

        saved = json.loads(
            (self.state_home / "ai-vitals" / "claude-usage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["primary"]["used_percent"], 25)
        self.assertEqual(saved["secondary"]["resets_at"], 1_788_940_800)

    def test_main_refuses_non_loopback_host(self):
        """Reject a bind address that could expose task metadata."""
        with self.assertRaises(SystemExit):
            codex_pulse.main(["--host", "0.0." + "0"])

    def test_terra_cost_uses_its_official_api_rate(self):
        """Use the published GPT-5.6 Terra input and output rates."""
        cost = codex_pulse.usage_cost(
            "gpt-5.6-terra", {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 1}
        )
        if cost is None:
            self.fail("cost is None")
        self.assertAlmostEqual(cost, 0.000212, places=6)

    def test_snapshot_omits_sessions_without_a_user_prompt(self):
        """Exclude guardian-review sessions from the task list."""
        guardian = self.codex_home / "sessions" / "2026" / "09" / "guardian.jsonl"
        guardian.write_text(
            "\n".join(
                json.dumps(record)
                for record in [
                    {
                        "type": "session_meta",
                        "payload": {"cwd": "/work/demo", "thread_source": "guardian_review"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "[1] user: Build an unrelated review",
                                }
                            ],
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )

        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )

        self.assertEqual(len(snapshot["tasks"]), 1)
        self.assertEqual(snapshot["tasks"][0]["task"]["prompt"], "Build the dashboard")

    def test_transcript_wrapper_is_not_a_task_prompt(self):
        """Do not use transcript wrappers as user task titles."""
        payload = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": ">>> TRANSCRIPT START\nreview content"}],
        }

        self.assertIsNone(codex_pulse.first_user_prompt(payload))

    def test_status_groups_marks_public_incidents_in_the_90_day_bar(self):
        """Show only the requested product groups and preserve severity."""
        groups = codex_pulse.status_groups(
            [{"name": "Codex API", "status": "operational"}],
            [
                {
                    "name": "Codex API latency",
                    "impact": "major",
                    "created_at": "2026-09-06T01:00:00Z",
                },
                {
                    "name": "ChatGPT conversation delay",
                    "impact": "minor",
                    "created_at": "2026-09-05T01:00:00Z",
                },
            ],
            date(2026, 9, 6),
        )

        self.assertEqual([group["name"] for group in groups], ["ChatGPT", "Codex"])
        self.assertEqual(groups[0]["days"][-2], "warn")
        self.assertEqual(groups[1]["days"][-1], "bad")
        self.assertEqual(groups[1]["components"][0]["name"], "Codex API")

    def test_collect_snapshot_fallback_history_when_watch_log_missing(self):
        """Generate snapshot history from local sessions when watch.log does not exist."""
        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        self.assertTrue(len(snapshot["history"]) > 0)
        first_item = snapshot["history"][0]
        self.assertIn("Codex", first_item)
        self.assertIn("gpt-5.5", first_item)
        self.assertIn("Build the dashboard", first_item)

    def test_default_state_home_on_windows(self):
        """Respect LOCALAPPDATA when running on Windows platform."""
        original_env = dict(os.environ)
        try:
            os.environ.pop("XDG_STATE_HOME", None)
            os.environ["LOCALAPPDATA"] = "C:\\Users\\Test\\AppData\\Local"
            state_path = codex_pulse.default_state_home(platform="win32")
            self.assertEqual(str(state_path), "C:\\Users\\Test\\AppData\\Local")
        finally:
            os.environ.clear()
            os.environ.update(original_env)

    def test_dashboard_html_contains_plan_badge_and_guide(self):
        """Verify the dashboard HTML exposes clickable plan badges and setting guidance."""
        html = codex_pulse.dashboard_html()
        self.assertIn("plan-badge", html)
        self.assertIn("openPlanSetting", html)
        self.assertIn("Codex plan（表示用）", html)
        self.assertIn("Claude plan（表示用）", html)
        self.assertIn("⚙️ 変更", html)

    def test_windows_background_batch_script_exists_and_valid(self):
        """Verify the Windows background execution batch file exists and has correct flags."""
        bat_path = APP_DIR / "ai-vitals-bg.bat"
        self.assertTrue(bat_path.is_file())
        content = bat_path.read_text(encoding="utf-8")
        self.assertIn("ai_vitals.py", content)
        self.assertIn("-b", content)
        self.assertIn("%~dp0", content)
        self.assertIn("chcp 65001", content)

    def test_windows_powershell_script_exists_and_valid(self):
        """Verify the Windows PowerShell background launcher script exists and has correct flags."""
        ps1_path = APP_DIR / "ai-vitals-bg.ps1"
        self.assertTrue(ps1_path.is_file())
        content = ps1_path.read_text(encoding="utf-8")
        self.assertIn("ai_vitals.py", content)
        self.assertIn("-b", content)

    def test_linux_background_shell_script_exists_and_valid(self):
        """Verify the Linux background shell launcher script exists and has correct flags."""
        sh_path = APP_DIR / "ai-vitals-bg.sh"
        self.assertTrue(sh_path.is_file())
        content = sh_path.read_text(encoding="utf-8")
        self.assertIn("ai_vitals.py", content)
        self.assertIn("-b", content)

    def test_summarize_agy_session_reads_transcript_jsonl(self):
        """Extract session info from a synthetic Antigravity transcript."""
        brain_dir = self.agy_home / "brain" / "test-session"
        log_dir = brain_dir / ".system_generated" / "logs"
        log_dir.mkdir(parents=True)
        records = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-09-07T16:00:00Z",
                "content": "<USER_REQUEST>\nRefactor the dashboard\nwith dark theme\n</USER_REQUEST>\n<ADDITIONAL_METADATA>extra</ADDITIONAL_METADATA>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-09-07T16:00:01Z",
                "content": "Starting refactor",
                "tool_calls": [],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-09-07T16:00:02Z",
                "content": "Done",
            },
        ]
        (log_dir / "transcript.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8"
        )

        summary = codex_pulse.summarize_agy_session(brain_dir)

        self.assertIsNotNone(summary)
        assert summary is not None  # nosec B101
        self.assertEqual(summary["task"]["prompt"], "Refactor the dashboard")
        self.assertEqual(summary["usage"]["steps"], 2)
        self.assertEqual(summary["observed_at"], "2026-09-07T16:00:02Z")

    def test_snapshot_includes_agy_sessions_in_active_tasks(self):
        """Include Antigravity sessions in the active tasks list."""
        brain_dir = self.agy_home / "brain" / "agy-task-session"
        log_dir = brain_dir / ".system_generated" / "logs"
        log_dir.mkdir(parents=True)
        records = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "status": "DONE",
                "created_at": "2026-09-07T17:00:00Z",
                "content": "<USER_REQUEST>\nBuild the AGY monitor\n</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "status": "DONE",
                "created_at": "2026-09-07T17:00:01Z",
            },
        ]
        (log_dir / "transcript.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records), encoding="utf-8"
        )

        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )

        agy_task = next((t for t in snapshot["tasks"] if t["provider"] == "Antigravity"), None)
        self.assertIsNotNone(agy_task)
        assert agy_task is not None  # nosec B101
        self.assertEqual(agy_task["task"]["prompt"], "Build the AGY monitor")

    def test_dashboard_exposes_agy_block(self):
        """Verify the dashboard HTML includes the Antigravity block."""
        page = codex_pulse.dashboard_html()

        self.assertIn('data-block="agy"', page)
        self.assertIn('id="agy-plan-select"', page)
        self.assertIn("Google AI", page)

    def test_save_agy_usage_stores_rate_limit(self):
        """Persist manually observed AGY usage values."""
        codex_pulse.save_agy_usage(
            self.state_home,
            used_percent=45,
            resets_at=1_789_000_000,
        )

        saved = json.loads(
            (self.state_home / "ai-vitals" / "agy-usage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["rate_limit"]["used_percent"], 45)
        self.assertEqual(saved["rate_limit"]["resets_at"], 1_789_000_000)

    def test_dashboard_exposes_google_status(self):
        """Verify the dashboard HTML includes the Google Status panel."""
        page = codex_pulse.dashboard_html()
        self.assertIn("Google Status", page)
        self.assertIn('id="google-status-badge"', page)
        self.assertIn('id="google-status-groups"', page)

    def test_post_agy_usage_endpoint_updates_saved_file(self):
        """Verify POST /api/agy-usage persists usage data and updates state."""
        with running_server(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        ) as base_url:
            req = Request(
                base_url + "/api/agy-usage",
                data=json.dumps({"used_percent": 68, "resets_at": 1_789_500_000}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as response:  # nosec B310
                result = json.load(response)
            self.assertTrue(result.get("ok"))

        saved = json.loads(
            (self.state_home / "ai-vitals" / "agy-usage.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved["rate_limit"]["used_percent"], 68)
        self.assertEqual(saved["rate_limit"]["resets_at"], 1_789_500_000)

    def test_cached_google_status_builds_operational_groups(self):
        """Verify Google Cloud status parser builds standard groups from sample incidents."""
        incidents = [
            {
                "id": "inc-1",
                "begin": "2026-09-05T12:00:00Z",
                "severity": "high",
                "external_desc": "Gemini API experiencing errors",
                "affected_products": [{"title": "Vertex Gemini API"}],
            }
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(incidents, f)
            temp_path = f.name
        try:
            _, status = codex_pulse.cached_google_status(None, "file://" + temp_path)
            self.assertEqual(len(status["groups"]), 2)
            self.assertEqual(status["groups"][0]["name"], "Gemini & Vertex AI")
            self.assertEqual(status["groups"][1]["name"], "Google Cloud Core")
            self.assertTrue(
                "bad" in status["groups"][0]["days"] or "warn" in status["groups"][0]["days"]
            )
        finally:
            os.unlink(temp_path)

    def test_classify_agy_model(self):
        """Verify Antigravity models are correctly sorted into gemini and claude_gpt families."""
        self.assertEqual(codex_pulse.classify_agy_model("Gemini 3.8 Flash Medium Fast"), "gemini")
        self.assertEqual(codex_pulse.classify_agy_model("Gemini 2.5 Flash"), "gemini")
        self.assertEqual(codex_pulse.classify_agy_model("Gemini 3.1 Pro Low"), "gemini")
        self.assertEqual(
            codex_pulse.classify_agy_model("Claude Sonnet 4.6 (Thinking)"), "claude_gpt"
        )
        self.assertEqual(codex_pulse.classify_agy_model("Claude Opus 4.6 (Thinking)"), "claude_gpt")
        self.assertEqual(codex_pulse.classify_agy_model("GPT-OSS 120B (Medium)"), "claude_gpt")

    def test_save_and_load_agy_dual_track_usage(self):
        """Verify saving and reading dual-track rate limits for Gemini and Claude/GPT."""
        codex_pulse.save_agy_usage(
            self.state_home,
            gemini_5h_used=16,
            gemini_7d_used=8,
            claude_5h_used=100,
            claude_7d_used=34,
        )
        loaded = codex_pulse.load_agy_usage(self.state_home)
        self.assertEqual(loaded["gemini"]["primary"]["used_percent"], 16)
        self.assertEqual(loaded["gemini"]["secondary"]["used_percent"], 8)
        self.assertEqual(loaded["claude_gpt"]["primary"]["used_percent"], 100)
        self.assertEqual(loaded["claude_gpt"]["secondary"]["used_percent"], 34)

    def test_post_agy_usage_endpoint_with_remaining_percentages(self):
        """Verify POST /api/agy-usage converts remaining percentages into used percentages."""
        with running_server(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        ) as base_url:
            req = Request(
                base_url + "/api/agy-usage",
                data=json.dumps(
                    {
                        "gemini_5h_remaining": 84,
                        "gemini_7d_remaining": 92,
                        "claude_5h_remaining": 0,
                        "claude_7d_remaining": 66,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as response:  # nosec B310
                result = json.load(response)
            self.assertTrue(result.get("ok"))

        loaded = codex_pulse.load_agy_usage(self.state_home)
        self.assertEqual(loaded["gemini"]["primary"]["used_percent"], 16)
        self.assertEqual(loaded["gemini"]["secondary"]["used_percent"], 8)
        self.assertEqual(loaded["claude_gpt"]["primary"]["used_percent"], 100)
        self.assertEqual(loaded["claude_gpt"]["secondary"]["used_percent"], 34)

    def test_dashboard_html_contains_dual_track_agy_elements(self):
        """Verify the dashboard HTML exposes Gemini and Claude/GPT cards and the usage dialog."""
        page = codex_pulse.dashboard_html()
        self.assertIn("GEMINI MODELS", page)
        self.assertIn("CLAUDE &amp; GPT", page)
        self.assertIn('id="agy-gem-5h-val"', page)
        self.assertIn('id="agy-cld-5h-val"', page)
        self.assertIn('id="agy-usage-dialog"', page)

    def test_dashboard_html_tasks_has_scroll_css(self):
        """Verify the dashboard HTML sets max-height and scrolling on #tasks."""
        page = codex_pulse.dashboard_html()
        self.assertIn("#tasks{max-height:480px;overflow-y:auto", page)

    def test_tasks_sorted_by_observed_at_descending(self):
        """Verify active tasks from Codex, Claude, and AGY are sorted by observed_at descending."""
        # 1. Codex session at 2026-09-08T01:00:00Z (oldest)
        # Already has self.session_path from setUp with observed_at 2026-09-06T00:00:04Z

        # 2. Claude session at 2026-09-08T03:00:00Z (newest)
        claude_proj = self.claude_home / "projects" / "test-proj"
        claude_proj.mkdir(parents=True, exist_ok=True)
        (claude_proj / "session.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-09-08T02:59:00Z",
                    "message": {"content": "Claude newest task"},
                    "cwd": "/work/claude",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-08T03:00:00Z",
                    "message": {
                        "model": "claude-3-7-sonnet-20250219",
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        # 3. AGY session at 2026-09-08T02:00:00Z (middle)
        agy_brain_dir = self.agy_home / "brain" / "test-brain-1"
        agy_logs = agy_brain_dir / ".system_generated" / "logs"
        agy_logs.mkdir(parents=True, exist_ok=True)
        (agy_logs / "transcript.jsonl").write_text(
            json.dumps(
                {
                    "type": "USER_INPUT",
                    "created_at": "2026-09-08T02:00:00Z",
                    "content": "<USER_REQUEST>\nAGY middle task\n</USER_REQUEST>",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-09-08T02:00:05Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        tasks = snapshot["tasks"]
        self.assertGreaterEqual(len(tasks), 3)
        observed_times = [t.get("observed_at") for t in tasks if t.get("observed_at")]
        self.assertEqual(observed_times, sorted(observed_times, reverse=True))
        # Top task should be the newest (Claude: 2026-09-08T03:00:00Z)
        self.assertEqual(tasks[0]["provider"], "Claude")
        self.assertEqual(tasks[0]["task"]["prompt"], "Claude newest task")
        # Second should be Antigravity (2026-09-08T02:00:05Z)
        self.assertEqual(tasks[1]["provider"], "Antigravity")
        self.assertEqual(tasks[1]["task"]["prompt"], "AGY middle task")

    def test_normalize_limit_sets_is_reset_flag(self):
        """Verify normalize_limit sets is_reset=True when resets_at is in the past."""
        past_limit = codex_pulse.normalize_limit(
            {
                "window_minutes": 300,
                "used_percent": 84,
                "resets_at": 1000,  # Far past
            }
        )
        assert past_limit is not None  # nosec B101
        self.assertTrue(past_limit["is_reset"])
        self.assertEqual(past_limit["used_percent"], 84)

        future_limit = codex_pulse.normalize_limit(
            {
                "window_minutes": 300,
                "used_percent": 84,
                "resets_at": int(time.time()) + 10000,  # Future
            }
        )
        assert future_limit is not None  # nosec B101
        self.assertFalse(future_limit["is_reset"])

    def test_collect_snapshot_includes_snapshot_at(self):
        """Verify collect_snapshot returns snapshot_at timestamp."""
        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        self.assertIn("snapshot_at", snapshot)
        self.assertIsNotNone(snapshot["snapshot_at"])

    def test_collect_snapshot_includes_agy_time_window_steps(self):
        """Verify collect_snapshot aggregates steps_5h and steps_7d for Antigravity."""
        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        self.assertIn("agy", snapshot)
        usage = snapshot["agy"]["usage"]
        self.assertIn("gemini", usage)
        self.assertIn("claude_gpt", usage)
        self.assertIn("steps_5h", usage["gemini"])
        self.assertIn("steps_7d", usage["gemini"])
        self.assertIn("steps_5h", usage["claude_gpt"])
        self.assertIn("steps_7d", usage["claude_gpt"])

    @patch("ai_vitals.get_cached_agy_live_status")
    def test_collect_snapshot_incorporates_live_status(self, mock_live):
        """Verify collect_snapshot injects real-time live status from language_server."""
        mock_live.return_value = {
            "source": "live_api",
            "plan": "Google AI Pro",
            "gemini": {
                "remaining_percent": 77,
                "used_percent": 23,
                "resets_at": 1788838131,
                "is_exhausted": False,
            },
            "claude_gpt": {
                "remaining_percent": 0,
                "used_percent": 100,
                "resets_at": 1788838677,
                "is_exhausted": True,
            },
        }
        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        self.assertIn("agy", snapshot)
        self.assertIn("live_status", snapshot["agy"])
        self.assertEqual(snapshot["agy"]["plan"], "Google AI Pro")
        gem_pri = snapshot["agy"]["limits"]["gemini"]["primary"]
        self.assertTrue(gem_pri.get("is_live"))
        self.assertEqual(gem_pri["used_percent"], 23)
        self.assertEqual(gem_pri["resets_at"], 1788838131)
        cld_pri = snapshot["agy"]["limits"]["claude_gpt"]["primary"]
        self.assertTrue(cld_pri.get("is_live"))
        self.assertEqual(cld_pri["used_percent"], 100)
        self.assertEqual(cld_pri["resets_at"], 1788838677)

    @patch.object(codex_pulse, "get_cached_agy_live_status")
    def test_collect_snapshot_incorporates_live_status_structured(self, mock_live):
        """Verify collect_snapshot injects both primary and secondary live limits."""
        mock_live.return_value = {
            "source": "live_api",
            "plan": "Google AI Pro",
            "gemini": {
                "primary": {
                    "label": "5h",
                    "used_percent": 3,
                    "remaining_percent": 97,
                    "window_minutes": 300,
                    "resets_at": 1788856131,
                    "is_live": True,
                },
                "secondary": {
                    "label": "weekly",
                    "used_percent": 16,
                    "remaining_percent": 84,
                    "window_minutes": 10080,
                    "resets_at": 1789326152,
                    "is_live": True,
                },
            },
            "claude_gpt": {
                "primary": {
                    "label": "5h",
                    "used_percent": 0,
                    "remaining_percent": 100,
                    "window_minutes": 300,
                    "resets_at": 1788858546,
                    "is_live": True,
                },
                "secondary": {
                    "label": "weekly",
                    "used_percent": 67,
                    "remaining_percent": 33,
                    "window_minutes": 10080,
                    "resets_at": 1789403026,
                    "is_live": True,
                },
            },
        }
        snapshot = codex_pulse.collect_snapshot(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        self.assertIn("agy", snapshot)
        gem_pri = snapshot["agy"]["limits"]["gemini"]["primary"]
        gem_sec = snapshot["agy"]["limits"]["gemini"]["secondary"]
        self.assertTrue(gem_pri.get("is_live"))
        self.assertEqual(gem_pri["used_percent"], 3)
        self.assertTrue(gem_sec.get("is_live"))
        self.assertEqual(gem_sec["used_percent"], 16)

        cld_pri = snapshot["agy"]["limits"]["claude_gpt"]["primary"]
        cld_sec = snapshot["agy"]["limits"]["claude_gpt"]["secondary"]
        self.assertTrue(cld_pri.get("is_live"))
        self.assertEqual(cld_pri["used_percent"], 0)
        self.assertTrue(cld_sec.get("is_live"))
        self.assertEqual(cld_sec["used_percent"], 67)

    def test_dashboard_html_contains_reset_expired_logic_and_codex_model(self):
        """Verify dashboard HTML includes expired limit handling and codex-model element."""
        page = codex_pulse.dashboard_html()
        self.assertIn('id="codex-model"', page)
        self.assertIn("解除: リセット済み", page)
        self.assertIn("isExpired", page)
        self.assertIn("5h: リセット済み", page)
        self.assertIn("7d: リセット済み", page)

    def test_dashboard_html_contains_auto_shutdown_on_pagehide(self):
        """Verify dashboard HTML includes auto-shutdown via sendBeacon on pagehide/beforeunload."""
        page = codex_pulse.dashboard_html()
        self.assertIn("pagehide", page)
        self.assertIn("beforeunload", page)
        self.assertIn("navigator.sendBeacon('/api/shutdown?delay=3')", page)
        self.assertIn("serverStopped", page)

    def test_create_server_delayed_shutdown_and_cancellation(self):
        """Verify /api/shutdown supports delay and is canceled by subsequent GET requests."""
        with running_server(
            self.codex_home, self.state_home, self.claude_home, self.agy_home
        ) as base_url:
            # Send delayed shutdown request
            req = Request(
                f"{base_url}/api/shutdown?delay=1.0",
                headers={"Origin": base_url},
                data=b"",
                method="POST",
            )
            with urlopen(req) as resp:  # nosec B310
                self.assertEqual(resp.status, 200)

            # A subsequent GET request cancels the delayed shutdown
            get_req = Request(f"{base_url}/api/snapshot", headers={"Origin": base_url})
            with urlopen(get_req) as resp:  # nosec B310
                self.assertEqual(resp.status, 200)

            # Wait beyond delay to ensure server did not shut down
            time.sleep(1.2)
            with urlopen(get_req) as resp:  # nosec B310
                self.assertEqual(resp.status, 200)


@contextmanager
def running_server(
    codex_home: Path, state_home: Path, claude_home: Path, agy_home: Path | None = None
):
    server = codex_pulse.create_server(
        "127.0.0.1", 0, codex_home, state_home, claude_home, agy_home
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
