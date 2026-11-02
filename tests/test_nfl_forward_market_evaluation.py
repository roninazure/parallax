import unittest
from datetime import UTC, datetime, timedelta

from parallax.nfl import NFLGame
from scripts.nfl_forward_market_evaluation import evaluate


KICKOFF = datetime(2026, 11, 8, 18, tzinfo=UTC)
GAME = NFLGame("2026_09_IND_JAX", 2026, "REG", KICKOFF.isoformat(), "JAX", "IND", 24, 17)


def snapshot(observed, probability=.65, *, buy=False):
    return {"schema_version": "nfl-sportsbook-research-v1", "research_only": True,
            "buy_publication_eligible": buy,
            "captured_at_utc": observed.isoformat(), "evidence": [{
                "game_id": GAME.game_id, "observed_at": observed.isoformat(),
                "home_probability": probability, "away_probability": 1-probability,
                "bookmaker_count": 4}]}


class NFLForwardMarketEvaluationTests(unittest.TestCase):
    def test_valid_pregame_observation_scored(self):
        result = evaluate([GAME], [snapshot(KICKOFF-timedelta(hours=2))], min_scored=1)
        self.assertEqual(result["status"], "SUFFICIENT_FORWARD_SAMPLE")
        self.assertEqual(result["matched_completed_games"], 1)
        self.assertFalse(result["buy_publication_eligible"])

    def test_after_cutoff_and_postkickoff_rejected(self):
        for observed in (KICKOFF-timedelta(minutes=30), KICKOFF+timedelta(minutes=5)):
            result = evaluate([GAME], [snapshot(observed)], min_scored=1)
            self.assertEqual(result["matched_completed_games"], 0)

    def test_old_observation_rejected(self):
        result = evaluate([GAME], [snapshot(KICKOFF-timedelta(hours=30))], min_scored=1)
        self.assertEqual(result["matched_completed_games"], 0)

    def test_latest_eligible_observation_selected(self):
        result = evaluate([GAME], [snapshot(KICKOFF-timedelta(hours=3), .25),
                                   snapshot(KICKOFF-timedelta(hours=2), .75)], min_scored=1)
        self.assertAlmostEqual(result["market"]["brier"], .0625)

    def test_buy_flag_or_invalid_probability_rejected(self):
        for item in (snapshot(KICKOFF-timedelta(hours=2), buy=True),
                     snapshot(KICKOFF-timedelta(hours=2), probability=float("nan"))):
            self.assertEqual(evaluate([GAME], [item], min_scored=1)["matched_completed_games"], 0)

    def test_insufficient_sample_does_not_certify(self):
        result = evaluate([GAME], [snapshot(KICKOFF-timedelta(hours=2))], min_scored=100)
        self.assertEqual(result["status"], "INSUFFICIENT_FORWARD_SAMPLE")


if __name__ == "__main__":
    unittest.main()
