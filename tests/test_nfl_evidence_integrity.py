"""P1 evidence-integrity regressions; no network calls or production state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone

from parallax.nfl import _date, _kickoff
from sports.fair_value import BookMoneyline, consensus_two_way_moneyline
from sports.odds_provider import _parse_utc, _quote_age_seconds


class NFLKickoffIntegrityTests(unittest.TestCase):
    def test_naive_scheduled_date_is_eastern_not_host_local(self):
        self.assertEqual(_date("2026-09-13"), "2026-09-13T04:00:00+00:00")
        self.assertEqual(_date("2026-01-11"), "2026-01-11T05:00:00+00:00")
        self.assertEqual(_date("2026-11-02T12:00:00"), "2026-11-02T17:00:00+00:00")

    def test_fixture_fallback_is_deterministic_in_utc_and_eastern_processes(self):
        code = (
            "import json; from parallax.nfl import _kickoff;"
            "print(json.dumps(_kickoff({'gameday':'2026-09-13','gametime':''})))"
        )
        answers = []
        for tz in ("UTC", "America/New_York", "America/Los_Angeles"):
            env = dict(os.environ, TZ=tz)
            result = subprocess.run(
                [sys.executable, "-c", code],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            answers.append(json.loads(result.stdout))
        self.assertEqual(answers, ["2026-09-13T04:00:00+00:00"] * 3)

    def test_explicit_aware_game_time_preserved(self):
        self.assertEqual(_date("2026-11-02T17:00:00Z"), "2026-11-02T17:00:00+00:00")


class SportsbookEvidenceIntegrityTests(unittest.TestCase):
    def test_duplicate_bookmaker_cannot_fake_two_book_consensus(self):
        quotes = [
            BookMoneyline("Book-A", 1.85, 2.05, age_seconds=10),
            BookMoneyline(" book-a ", 1.70, 2.20, age_seconds=20),
        ]
        self.assertIsNone(consensus_two_way_moneyline(quotes, min_books=2))

    def test_distinct_books_remain_eligible_duplicate_uses_freshest(self):
        quotes = [
            BookMoneyline("Book-A", 1.85, 2.05, age_seconds=45),
            BookMoneyline("book-a", 1.90, 2.00, age_seconds=10),
            BookMoneyline("Book-B", 1.80, 2.10, age_seconds=12),
        ]
        result = consensus_two_way_moneyline(quotes)
        self.assertIsNotNone(result)
        self.assertEqual(result.sample_size, 2)
        self.assertEqual(len(result.bookmakers_used), 2)

    def test_future_and_nonfinite_quote_age_does_not_count(self):
        quotes = [
            BookMoneyline("future", 1.90, 2.00, age_seconds=-30),
            BookMoneyline("nan", 1.90, 2.00, age_seconds=float("nan")),
            BookMoneyline("good", 1.85, 2.05, age_seconds=20),
        ]
        self.assertIsNone(consensus_two_way_moneyline(quotes))

    def test_odds_timestamp_requires_offset_and_preserves_future_age(self):
        now = datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(_quote_age_seconds("2026-11-02T17:01:00Z", now), -60)
        with self.assertRaises(ValueError):
            _parse_utc("2026-11-02T17:00:00")


if __name__ == "__main__":
    unittest.main()
