"""Unit and integration tests for monthly usage and quota tracking in AI Vitals."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))
import ai_vitals


class MonthlyUsageTests(unittest.TestCase):
    """Verify monthly window calculation, settings storage, log aggregation, and API."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.codex_home = self.root / "codex"
        self.state_home = self.root / "state"
        self.claude_home = self.root / "claude"
        self.agy_home = self.root / "agy"
        for p in (self.codex_home, self.state_home, self.claude_home, self.agy_home):
            p.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_and_save_monthly_settings(self):
        """Verify default settings, saving, and loading from state directory."""
        defaults = ai_vitals.default_monthly_settings()
        self.assertEqual(defaults["billing_day"], 1)
        self.assertEqual(defaults["codex"]["budget_usd"], 0.0)

        # Initial load when file does not exist
        loaded = ai_vitals.load_monthly_settings(self.state_home)
        self.assertEqual(loaded["billing_day"], 1)

        # Save custom settings
        payload = {
            "billing_day": 15,
            "codex": {"budget_usd": 15.5, "token_limit": 2000000},
            "claude": {"budget_usd": 25.0, "token_limit": 3000000},
            "agy": {"budget_usd": 10.0, "token_limit": 5000000},
            "total_budget_usd": 50.5,
            "total_token_limit": 10000000,
        }
        ai_vitals.save_monthly_settings(self.state_home, payload)
        reloaded = ai_vitals.load_monthly_settings(self.state_home)
        self.assertEqual(reloaded["billing_day"], 15)
        self.assertEqual(reloaded["codex"]["budget_usd"], 15.5)
        self.assertEqual(reloaded["codex"]["token_limit"], 2000000)
        self.assertEqual(reloaded["claude"]["budget_usd"], 25.0)
        self.assertEqual(reloaded["agy"]["budget_usd"], 10.0)
        self.assertEqual(reloaded["total_budget_usd"], 50.5)
        self.assertEqual(reloaded["total_token_limit"], 10000000)

    def test_get_monthly_window(self):
        """Verify billing window calculation across calendar boundaries."""
        # 1st of month
        now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        start, end = ai_vitals.get_monthly_window(1, now)
        self.assertEqual(start, datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))

        # Mid-month start (after billing day)
        start, end = ai_vitals.get_monthly_window(15, now)
        self.assertEqual(start, datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc))

        # Mid-month start (before billing day)
        start, end = ai_vitals.get_monthly_window(20, now)
        self.assertEqual(start, datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc))

        # 31st in month with fewer days (February)
        feb_now = datetime(2026, 2, 10, tzinfo=timezone.utc)
        start, end = ai_vitals.get_monthly_window(31, feb_now)
        self.assertEqual(start, datetime(2026, 1, 31, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 2, 28, 0, 0, tzinfo=timezone.utc))

    def test_calculate_burn_rate(self):
        """Verify burn rate categorizations."""
        self.assertIsNone(ai_vitals.calculate_burn_rate(None, 50.0))
        # Ahead by > 15% -> warn
        self.assertEqual(ai_vitals.calculate_burn_rate(70.0, 50.0), "warn")
        # Behind by < -10% -> safe
        self.assertEqual(ai_vitals.calculate_burn_rate(35.0, 50.0), "safe")
        # Near pace -> normal
        self.assertEqual(ai_vitals.calculate_burn_rate(52.0, 50.0), "normal")

    def test_collect_monthly_usage_aggregation(self):
        """Verify monthly usage aggregation for Codex, Claude, and Antigravity."""
        now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

        # 1. Create a Codex session within window
        codex_sess = self.codex_home / "sessions" / "2026" / "10" / "session1.jsonl"
        codex_sess.parent.mkdir(parents=True, exist_ok=True)
        codex_sess.write_text(
            json.dumps({"type": "session_meta", "timestamp": "2026-10-10T10:00:00Z", "payload": {"id": "c1", "cwd": "/work"}}) + "\n" +
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5"}}) + "\n" +
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": "Hello Codex"}}) + "\n" +
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 500, "total_tokens": 1500}}}}) + "\n",
            encoding="utf-8",
        )

        # 2. Create a Claude session within window
        claude_sess = self.claude_home / "projects" / "p1" / "session1.jsonl"
        claude_sess.parent.mkdir(parents=True, exist_ok=True)
        claude_sess.write_text(
            json.dumps({"type": "user", "timestamp": "2026-10-12T10:00:00Z", "cwd": "/work", "message": {"content": "Hello Claude"}}) + "\n" +
            json.dumps({"type": "assistant", "timestamp": "2026-10-12T10:00:05Z", "message": {"model": "claude-sonnet-4.6", "usage": {"input_tokens": 2000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1000}}}) + "\n",
            encoding="utf-8",
        )

        # 3. Create an Antigravity session within window
        agy_brain_dir = self.agy_home / "brain" / "sess-1"
        agy_logs = agy_brain_dir / ".system_generated" / "logs"
        agy_logs.mkdir(parents=True, exist_ok=True)
        (agy_logs / "transcript_full.jsonl").write_text(
            json.dumps({"type": "USER_INPUT", "created_at": "2026-10-14T10:00:00Z", "content": "<USER_REQUEST>\nBuild monthly vitals\n</USER_REQUEST>"}) + "\n" +
            json.dumps({"type": "PLANNER_RESPONSE", "created_at": "2026-10-14T10:00:02Z", "content": "Working on it...", "thinking": "Let me plan..."}) + "\n",
            encoding="utf-8",
        )

        # Save monthly settings with budgets
        ai_vitals.save_monthly_settings(
            self.state_home,
            {
                "billing_day": 1,
                "codex": {"budget_usd": 10.0, "token_limit": 100000},
                "claude": {"budget_usd": 20.0, "token_limit": 200000},
                "agy": {"budget_usd": 5.0, "token_limit": 50000},
            },
        )

        monthly = ai_vitals.collect_monthly_usage(
            self.codex_home, self.state_home, self.claude_home, self.agy_home, now=now
        )

        self.assertEqual(monthly["billing_day"], 1)
        self.assertEqual(monthly["codex"]["tasks"], 1)
        self.assertEqual(monthly["codex"]["total_tokens"], 1500)
        self.assertGreater(monthly["codex"]["estimated_cost_usd"], 0)

        self.assertEqual(monthly["claude"]["tasks"], 1)
        self.assertEqual(monthly["claude"]["total_tokens"], 3000)
        self.assertGreater(monthly["claude"]["estimated_cost_usd"], 0)

        self.assertEqual(monthly["agy"]["tasks"], 1)
        self.assertEqual(monthly["agy"]["steps"], 1)
        self.assertGreater(monthly["agy"]["total_tokens"], 0)

        self.assertEqual(monthly["total"]["tasks"], 3)
        self.assertEqual(monthly["total"]["budget_usd"], 35.0)
        self.assertEqual(monthly["total"]["token_limit"], 350000)
        self.assertIsNotNone(monthly["total"]["used_cost_percent"])

    def test_monthly_settings_api_and_snapshot(self):
        """Verify POST /api/monthly-settings saves settings and appears in /api/snapshot."""
        server = ai_vitals.create_server(
            "127.0.0.1", 0, self.codex_home, self.state_home, self.claude_home, self.agy_home
        )
        port = server.server_port
        import threading
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            # 1. POST /api/monthly-settings
            payload = {
                "billing_day": 10,
                "codex": {"budget_usd": 12.0, "token_limit": 500000},
                "claude": {"budget_usd": 18.0, "token_limit": 600000},
                "agy": {"budget_usd": 8.0, "token_limit": 200000},
            }
            req = Request(
                f"http://127.0.0.1:{port}/api/monthly-settings",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=3) as res:
                self.assertEqual(res.status, 200)
                body = json.loads(res.read().decode("utf-8"))
                self.assertTrue(body.get("ok"))

            # 2. GET /api/snapshot
            with urlopen(f"http://127.0.0.1:{port}/api/snapshot", timeout=3) as res:
                self.assertEqual(res.status, 200)
                snap = json.loads(res.read().decode("utf-8"))
                self.assertIn("monthly", snap)
                self.assertEqual(snap["monthly"]["billing_day"], 10)
                self.assertEqual(snap["monthly"]["settings"]["codex"]["budget_usd"], 12.0)
        finally:
            server.shutdown()
            server.server_close()

    def test_dashboard_html_contains_monthly_elements(self):
        """Verify dashboard HTML includes monthly input fields, badges, and progress elements."""
        html = ai_vitals.dashboard_html()
        self.assertIn("monthly-billing-day", html)
        self.assertIn("monthly-codex-budget", html)
        self.assertIn("monthly-claude-budget", html)
        self.assertIn("monthly-agy-budget", html)
        self.assertIn("save-monthly-btn", html)
        self.assertIn("ov-monthly-tasks", html)
        self.assertIn("ov-monthly-cost", html)
        self.assertIn("ov-monthly-tokens", html)
        self.assertIn("ov-monthly-pace", html)
        self.assertIn("codex-monthly-badge", html)
        self.assertIn("claude-monthly-badge", html)
        self.assertIn("codex-1m-val", html)
        self.assertIn("claude-1m-val", html)
        self.assertIn("agy-1m-val", html)
        self.assertIn("renderMonthlyCard", html)
        self.assertIn("monthlyBurnBadge", html)

    def test_detect_claude_billing_day(self):
        """Verify detect_claude_billing_day correctly parses subscriptionCreatedAt."""
        claude_json = self.root / ".claude.json"
        claude_json.write_text(
            json.dumps({"oauthAccount": {"subscriptionCreatedAt": "2026-07-14T16:24:27.867691Z"}}),
            encoding="utf-8",
        )
        detected = ai_vitals.detect_claude_billing_day(self.claude_home)
        self.assertEqual(detected, 14)

    def test_cli_save_monthly_settings(self):
        """Verify saving monthly settings via CLI arguments."""
        ai_vitals.main(
            [
                "--state-home",
                str(self.state_home),
                "--save-monthly-settings",
                "--billing-day",
                "25",
                "--monthly-budget-codex",
                "30.0",
                "--monthly-tokens-codex",
                "4000000",
            ]
        )
        saved = ai_vitals.load_monthly_settings(self.state_home)
        self.assertEqual(saved["billing_day"], 25)
        self.assertEqual(saved["codex"]["budget_usd"], 30.0)
        self.assertEqual(saved["codex"]["token_limit"], 4000000)

    def test_version_agnostic_pricing_and_gpt_oss_zero_cost(self):
        """Verify version-agnostic model matching and zero-cost for gpt-oss."""
        # 1. gpt-oss is zero-cost in both Codex and Antigravity
        codex_oss_cost = ai_vitals.usage_cost("gpt-oss", {"input_tokens": 10000, "cached_input_tokens": 5000, "output_tokens": 2000})
        self.assertEqual(codex_oss_cost, 0.0)

        agy_oss_cost = ai_vitals.agy_usage_cost("GPT-OSS 120B (Medium)", {"input_tokens": 10000, "output_tokens": 2000})
        self.assertEqual(agy_oss_cost, 0.0)

        # 2. Claude version-agnostic resolution (sonnet, opus, haiku, fable)
        usage = {"input_tokens": 1_000_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1_000_000}
        cost_sonnet_55 = ai_vitals.claude_usage_cost("claude-sonnet-5-5", usage)
        cost_sonnet_name = ai_vitals.claude_usage_cost("sonnet", usage)
        self.assertIsNotNone(cost_sonnet_55)
        self.assertEqual(cost_sonnet_55, cost_sonnet_name)
        # Sonnet 5.5: input $2.0 + output $10.0 = $12.0
        self.assertEqual(cost_sonnet_55["minimum_usd"], 12.0)

        cost_opus_55 = ai_vitals.claude_usage_cost("claude-opus-5-5", usage)
        cost_opus_name = ai_vitals.claude_usage_cost("opus", usage)
        self.assertIsNotNone(cost_opus_55)
        self.assertEqual(cost_opus_55, cost_opus_name)
        # Opus 5.5: input $4.0 + output $20.0 = $24.0
        self.assertEqual(cost_opus_55["minimum_usd"], 24.0)

        cost_haiku_55 = ai_vitals.claude_usage_cost("claude-haiku-5-5", usage)
        cost_haiku_name = ai_vitals.claude_usage_cost("haiku", usage)
        self.assertIsNotNone(cost_haiku_55)
        self.assertEqual(cost_haiku_55, cost_haiku_name)
        # Haiku 5.5: input $0.10 + output $0.50 = $0.60
        self.assertEqual(cost_haiku_55["minimum_usd"], 0.60)

        cost_fable_51 = ai_vitals.claude_usage_cost("claude-fable-5-1", usage)
        cost_fable_name = ai_vitals.claude_usage_cost("fable", usage)
        self.assertIsNotNone(cost_fable_51)
        self.assertEqual(cost_fable_51, cost_fable_name)
        # Fable 5.1: input $10.0 + output $50.0 = $60.0
        self.assertEqual(cost_fable_51["minimum_usd"], 60.0)

        # 3. Codex version-agnostic resolution (gpt-6, gpt-5, etc.)
        codex_usage = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 1_000_000}
        cost_gpt6_astra = ai_vitals.usage_cost("gpt-6-astra", codex_usage)
        cost_gpt6 = ai_vitals.usage_cost("gpt-6", codex_usage)
        self.assertIsNotNone(cost_gpt6_astra)
        self.assertEqual(cost_gpt6_astra, cost_gpt6)
        # GPT-6: input $2.50 + output $10.0 = $12.50
        self.assertEqual(cost_gpt6, 12.50)

        # 4. Antigravity IDE models
        agy_usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        flash_cost = ai_vitals.agy_usage_cost("Gemini 3.8 Flash Low", agy_usage)
        self.assertEqual(flash_cost, 0.75)  # 0.15 + 0.60
        argon_cost = ai_vitals.agy_usage_cost("Gemini 4 Argon", agy_usage)
        self.assertEqual(argon_cost, 6.25)  # 1.25 + 5.00


if __name__ == "__main__":
    unittest.main()
