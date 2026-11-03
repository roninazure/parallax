import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.nfl_sportsbook_capture import (
    capture, capture_games, eligible_capture_games, main, quota_headroom_verified,
    reserve_capture_attempt, update_capture_state,
)
from parallax.nfl import NFLGame, parse_games
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


    def test_capture_window_matches_forward_evaluation_cutoffs(self):
        games = [
            NFLGame("too-soon", 2026, "REG", (NOW + timedelta(minutes=59)).isoformat(),
                    "JAX", "IND", None, None),
            NFLGame("lower-bound", 2026, "REG", (NOW + timedelta(minutes=60)).isoformat(),
                    "JAX", "IND", None, None),
            NFLGame("upper-bound", 2026, "REG", (NOW + timedelta(hours=24)).isoformat(),
                    "JAX", "IND", None, None),
            NFLGame("too-far", 2026, "REG", (NOW + timedelta(hours=24, seconds=1)).isoformat(),
                    "JAX", "IND", None, None),
            NFLGame("already-final", 2026, "REG", (NOW + timedelta(hours=2)).isoformat(),
                    "JAX", "IND", 24, 17),
        ]
        eligible = eligible_capture_games(games, now=NOW)
        self.assertEqual([game.game_id for game in eligible], ["lower-bound", "upper-bound"])

    def test_no_eligible_games_skips_before_quota_or_odds_api(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "snapshot.json"
            capture_state = root / "capture-state.json"
            argv = [
                "nfl_sportsbook_capture",
                "--fetch-schedule",
                "--quota-state", str(root / "missing-quota.json"),
                "--capture-state", str(capture_state),
                "--output", str(output),
                "--fetch-once",
            ]
            far_future = NFLGame(
                "far-future", 2030, "REG", "2030-01-01T20:00:00+00:00",
                "JAX", "IND", None, None,
            )
            stdout = StringIO()
            with (
                patch("sys.argv", argv),
                patch("scripts.nfl_sportsbook_capture.fetch_games", return_value=[far_future]),
                patch("scripts.nfl_sportsbook_capture.load_quota_state", side_effect=AssertionError("quota read")),
                patch("scripts.nfl_sportsbook_capture.fetch_nfl_v4_once", side_effect=AssertionError("odds request")),
                redirect_stdout(stdout),
            ):
                result = main()
            payload = __import__("json").loads(stdout.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(payload["status"], "SKIPPED_NO_ELIGIBLE_GAMES")
            self.assertFalse(output.exists())
            self.assertFalse(capture_state.exists())

    def test_provider_exception_is_sanitized_before_supervisor_logging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            games_csv = root / "games.csv"
            games_csv.write_text(CSV, encoding="utf-8")
            output = root / "snapshot.json"
            capture_state = root / "capture-state.json"
            argv = [
                "nfl_sportsbook_capture",
                "--games-csv", str(games_csv),
                "--quota-state", str(root / "quota.json"),
                "--capture-state", str(capture_state),
                "--output", str(output),
                "--fetch-once",
            ]
            with (
                patch("sys.argv", argv),
                patch.dict("os.environ", {"THE_ODDS_API_KEY": "TEST_KEY"}),
                patch("scripts.nfl_sportsbook_capture.load_quota_state", return_value={"remaining": 100}),
                patch("scripts.nfl_sportsbook_capture.quota_allows_request", return_value=True),
                patch("scripts.nfl_sportsbook_capture.eligible_capture_games", return_value=parse_games(CSV)),
                patch(
                    "scripts.nfl_sportsbook_capture.fetch_nfl_v4_once",
                    side_effect=RuntimeError("request URL contained SHOULD_NOT_LEAK"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "capture failed: RuntimeError") as raised:
                    main()
            self.assertNotIn("SHOULD_NOT_LEAK", str(raised.exception))
            state = __import__("json").loads(capture_state.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "FAILED")
            self.assertEqual(state["last_error_type"], "RuntimeError")

    def test_capture_window_rejects_naive_clock(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            eligible_capture_games([], now=NOW.replace(tzinfo=None))

    def test_persistent_capture_throttle_survives_process_restart(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            reserve_capture_attempt(state, now=NOW)
            with self.assertRaisesRegex(RuntimeError, "one-hour minimum"):
                reserve_capture_attempt(state, now=NOW + timedelta(seconds=3599))
            reserve_capture_attempt(state, now=NOW + timedelta(seconds=3600))

    def test_failed_attempt_still_consumes_one_hour_reservation(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            reserve_capture_attempt(state, now=NOW)
            update_capture_state(state, status="FAILED", last_error_type="TimeoutError")
            with self.assertRaisesRegex(RuntimeError, "one-hour minimum"):
                reserve_capture_attempt(state, now=NOW + timedelta(minutes=30))

    def test_corrupt_capture_state_fails_closed(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            state.write_text('{"last_attempt_at_utc":"not-a-timestamp"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "timestamp is invalid"):
                reserve_capture_attempt(state, now=NOW + timedelta(hours=2))

    def test_capture_throttle_rejects_naive_time_and_sub_hour_interval(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                reserve_capture_attempt(state, now=NOW.replace(tzinfo=None))
            with self.assertRaisesRegex(ValueError, "less than one hour"):
                reserve_capture_attempt(state, now=NOW, min_interval_seconds=3599)

    def test_missing_or_malformed_quota_is_blocked(self):
        for state in (None, {}, {"remaining": None}, {"remaining": "unknown"},
                      {"remaining": 10}, {"remaining": -1}):
            self.assertFalse(quota_headroom_verified(state, reserve=10))
        self.assertTrue(quota_headroom_verified({"remaining": 11}, reserve=10))


if __name__ == "__main__":
    unittest.main()
