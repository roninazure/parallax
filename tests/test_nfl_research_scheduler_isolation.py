"""NFL research lane isolation against the exact deployed supervisor baseline."""
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.parallax_unattended import (
    LANES, SPORTS_LANES, STARTUP_OFFSETS_SECONDS,
    NFL_SPORTSBOOK_RESEARCH_INTERVAL_SECONDS, UnattendedScheduler,\n    _safe_sportsbook_failure_detail,
)

class NFLResearchSchedulerIsolationTests(unittest.TestCase):
    def _scheduler(self, root, *, enabled):
        env = {"PARALLAX_NFL_SPORTSBOOK_RESEARCH_ENABLED": "1" if enabled else "0"}
        with patch.dict(os.environ, env):
            s = UnattendedScheduler(
                root=root, state_dir=root / "state",
                clock=lambda: datetime(2026, 11, 3, 16, tzinfo=UTC).isoformat(),
                monotonic=lambda: 5000.0,
            )
        return s

    def test_safe_error_reporting_extracts_credential_failure(self):
        stderr = ("usage: nfl_sportsbook_capture.py [-h] ...\n"
                  "nfl_sportsbook_capture.py: error: Odds API credential unavailable in runtime or operator Keychain\n")
        self.assertEqual(
            _safe_sportsbook_failure_detail(stderr),
            "Odds API credential unavailable in runtime or operator Keychain",
        )

    def test_safe_error_reporting_redacts_unexpected_urls(self):
        stderr = "nfl_sportsbook_capture.py: error: https://example.com/?apiKey=SECRET"
        self.assertNotIn("SECRET", _safe_sportsbook_failure_detail(stderr))
        self.assertIn("safe diagnostic", _safe_sportsbook_failure_detail(stderr))

    def test_disabled_by_default_and_no_extra_sports_feed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = self._scheduler(root, enabled=False)
            with patch.dict(os.environ, {"PARALLAX_NFL_SPORTSBOOK_RESEARCH_ENABLED": "0"}):
                commands = s.commands()
            self.assertNotIn("nfl_sportsbook_research", commands)
            self.assertNotIn("nfl_sportsbook_research", SPORTS_LANES)
            self.assertIn("nfl", commands)
            self.assertEqual(STARTUP_OFFSETS_SECONDS["nfl_sportsbook_research"], 3600)
            self.assertEqual(NFL_SPORTSBOOK_RESEARCH_INTERVAL_SECONDS, 86400)

    def test_enabled_collects_only_under_external_research_state(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = self._scheduler(root, enabled=True)
            with patch.dict(os.environ, {"PARALLAX_NFL_SPORTSBOOK_RESEARCH_ENABLED": "1"}):
                commands = s.commands()
            cmd = commands["nfl_sportsbook_research"]
            self.assertIn("--fetch-once", cmd)
            self.assertIn("--fetch-schedule", cmd)
            self.assertIn("--min-quota-reserve", cmd)
            self.assertIn("10", cmd)
            self.assertTrue(str(root / "state/research/nfl_sportsbook") in " ".join(cmd))
            self.assertFalse(any("public_feed" in value for value in cmd))
            self.assertEqual(s.next_due["nfl_sportsbook_research"], 8600.0)
            self.assertIn("nfl_sportsbook_research", LANES)

    def test_opt_in_after_daemon_start_gets_new_one_hour_grace(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            s = self._scheduler(root, enabled=False)
            self.assertEqual(s.next_due["nfl_sportsbook_research"], 8600.0)
            s.next_due["nfl_sportsbook_research"] = 1.0
            with patch.dict(os.environ, {"PARALLAX_NFL_SPORTSBOOK_RESEARCH_ENABLED": "1"}):
                s.commands()
            self.assertEqual(s.next_due["nfl_sportsbook_research"], 8600.0)

if __name__ == "__main__":
    unittest.main()
