import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parallax.nfl import NFLGame
from scripts.nfl_forward_market_evaluation import _paired_bootstrap, evaluate, main


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
        result = evaluate([GAME], [snapshot(KICKOFF-timedelta(hours=2))],
                          min_scored=1, min_week_clusters=1)
        self.assertEqual(result["status"], "SUFFICIENT_FORWARD_SAMPLE")
        self.assertEqual(result["matched_completed_games"], 1)
        self.assertFalse(result["buy_publication_eligible"])
        self.assertIn("paired_comparisons_vs_elo", result)
        self.assertEqual(result["paired_comparisons_vs_elo"]["market"]["brier"]["n"], 1)

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
        self.assertEqual(result["paired_comparisons_vs_elo"]["market"]["evidence_gate"],
                         "MINIMUM_GAME_OR_WEEK_SAMPLE_NOT_MET")
        self.assertEqual(result["matched_calendar_weeks"], 1)
        self.assertEqual(result["min_required_calendar_weeks"], 12)

    def test_paired_bootstrap_reports_challenger_improvement_when_consistent(self):
        rows = [{"home_win": 1, "elo": .40, "market": .80,
                 "kickoff": (KICKOFF + timedelta(days=7 * (i % 12))).isoformat()}
                for i in range(120)]
        result = _paired_bootstrap(rows, "market", "brier")
        self.assertLess(result["delta"], 0)
        self.assertLess(result["ci95"][1], 0)
        self.assertEqual(result["interpretation"], "CHALLENGER_IMPROVEMENT_SUPPORTED")

    def test_paired_bootstrap_is_deterministic(self):
        rows = [{"home_win": i % 2, "elo": .55, "market": .60,
                 "kickoff": (KICKOFF + timedelta(days=7 * (i % 8))).isoformat()}
                for i in range(40)]
        self.assertEqual(_paired_bootstrap(rows, "market", "log_loss"),
                         _paired_bootstrap(rows, "market", "log_loss"))


    def test_cli_fetches_official_schedule_without_manual_csv(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(
                json.dumps(snapshot(KICKOFF - timedelta(hours=2))),
                encoding="utf-8",
            )
            stdout = StringIO()
            argv = [
                "nfl_forward_market_evaluation",
                "--fetch-schedule",
                "--snapshots", str(snapshot_path),
            ]
            with (
                patch("sys.argv", argv),
                patch(
                    "scripts.nfl_forward_market_evaluation.fetch_games",
                    return_value=[GAME],
                ) as fetch_schedule,
                redirect_stdout(stdout),
            ):
                result = main()
            report = json.loads(stdout.getvalue())
            fetch_schedule.assert_called_once_with()
            self.assertEqual(result, 2)
            self.assertEqual(report["status"], "INSUFFICIENT_FORWARD_SAMPLE")
            self.assertFalse(report["buy_publication_eligible"])
            self.assertTrue(report["research_only"])

    def test_hundred_games_in_one_week_do_not_pass_week_breadth_gate(self):
        games = []
        snapshots = []
        observed = KICKOFF - timedelta(hours=2)
        for index in range(100):
            game_id = f"GAME_{index:03d}"
            games.append(NFLGame(game_id, 2026, "REG", KICKOFF.isoformat(),
                                 f"HOME{index}", f"AWAY{index}", 24, 17))
            snapshots.append({
                "schema_version": "nfl-sportsbook-research-v1",
                "research_only": True,
                "buy_publication_eligible": False,
                "captured_at_utc": observed.isoformat(),
                "evidence": [{
                    "game_id": game_id,
                    "observed_at": observed.isoformat(),
                    "home_probability": .65,
                    "away_probability": .35,
                    "bookmaker_count": 4,
                }],
            })
        result = evaluate(games, snapshots, min_scored=100, min_week_clusters=12)
        self.assertEqual(result["matched_completed_games"], 100)
        self.assertEqual(result["matched_calendar_weeks"], 1)
        self.assertEqual(result["status"], "INSUFFICIENT_FORWARD_SAMPLE")
        self.assertEqual(
            result["paired_comparisons_vs_elo"]["market"]["evidence_gate"],
            "MINIMUM_GAME_OR_WEEK_SAMPLE_NOT_MET",
        )

    def test_bootstrap_resamples_calendar_weeks_as_clusters(self):
        rows = [{"home_win": 1, "elo": .40, "market": .80,
                 "kickoff": (KICKOFF + timedelta(days=7 * i)).isoformat()}
                for i in range(8)]
        result = _paired_bootstrap(rows, "market", "brier")
        self.assertEqual(result["n"], 8)
        self.assertEqual(result["clusters"], 8)
        self.assertEqual(result["resampling_unit"], "calendar_week_cluster")


if __name__ == "__main__":
    unittest.main()
