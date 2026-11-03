import unittest
from datetime import UTC, datetime, timedelta

from scripts.nfl_sportsbook_capture import capture, capture_games, quota_headroom_verified
from parallax.nfl import parse_games
from sports.fair_value import BookMoneyline
from sports.odds_provider import OddsQuota, OddsResponse, SportsEvent


NOW = datetime(2026, 11, 1, 15, tzinfo=UTC)
START = NOW + timedelta(hours=3)
CSV = ("game_id,season,game_type,gameday,gametime,home_team,away_team,home_score,away_score\n"
       "2026_09_IND_JAX,2026,REG,2026-11-01,14:00,JAX,IND,,\n")


class NFLOddsCaptureTests(unittest.TestCase):
    def test_only_matching_fresh_independent_nfl_evidence_is_captured(self):
        # Local Eastern 14:00 is UTC 19:00 after daylight saving transition.
        kickoff = datetime(2026, 11, 1, 19, tzinfo=UTC)
        event = SportsEvent("e1", "americanfootball_nfl", "NFL",
                            "Jacksonville Jaguars", "Indianapolis Colts",
                            kickoff.isoformat(), (
                                BookMoneyline("A", 1.8, 2.1, 20),
                                BookMoneyline("B", 1.85, 2.05, 30),
                            ))
        invalid = SportsEvent("e2", "americanfootball_nfl", "NFL",
                              "Houston Texans", "Indianapolis Colts",
                              kickoff.isoformat(), event.moneylines)
        report = capture(OddsResponse((event, invalid), OddsQuota(100, 2, 98, None)),
                         CSV, observed_at=NOW)
        self.assertTrue(report["research_only"])
        self.assertFalse(report["buy_publication_eligible"])
        self.assertEqual(report["events_seen"], 2)
        self.assertEqual(report["events_accepted"], 1)
        self.assertEqual(report["evidence"][0]["bookmaker_count"], 2)

    def test_capture_games_accepts_existing_official_schedule_objects(self):
        kickoff = datetime(2026, 11, 1, 19, tzinfo=UTC)
        event = SportsEvent("e1", "americanfootball_nfl", "NFL",
                            "Jacksonville Jaguars", "Indianapolis Colts",
                            kickoff.isoformat(), (
                                BookMoneyline("A", 1.8, 2.1, 20),
                                BookMoneyline("B", 1.85, 2.05, 30),
                            ))
        report = capture_games(
            OddsResponse((event,), OddsQuota(100, 2, 98, None)),
            parse_games(CSV), observed_at=NOW,
        )
        self.assertEqual(report["events_accepted"], 1)
        self.assertFalse(report["buy_publication_eligible"])

    def test_naive_capture_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            capture(OddsResponse((), OddsQuota(None, None, None, None)), CSV,
                    observed_at=NOW.replace(tzinfo=None))

    def test_missing_or_malformed_quota_is_blocked(self):
        for state in (None, {}, {"remaining": None}, {"remaining": "unknown"},
                      {"remaining": 10}, {"remaining": -1}):
            self.assertFalse(quota_headroom_verified(state, reserve=10))
        self.assertTrue(quota_headroom_verified({"remaining": 11}, reserve=10))


if __name__ == "__main__":
    unittest.main()
