import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.nfl_sportsbook_capture import (
    capture, capture_games, eligible_capture_games, main, quota_headroom_verified,
    reserve_capture_attempt, update_capture_state, resolve_odds_api_key,
    fetch_free_usage_snapshot,
)
from parallax.nfl import NFLGame, parse_games
from sports.nfl_research_fair_value import BookMoneyline
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

    def test_free_quota_preflight_reads_zero_credit_provider_headers(self):
        class FakeResponse:
            status = 200
            headers = {
                "x-requests-remaining": "497",
                "x-requests-used": "3",
                "x-requests-last": "0",
            }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"[]"

        with patch("scripts.nfl_sportsbook_capture.urlopen", return_value=FakeResponse()) as opened:
            snapshot = fetch_free_usage_snapshot(api_key="TEST_SECRET", now=NOW)
        self.assertEqual(snapshot["remaining"], 497)
        self.assertEqual(snapshot["limit"], 500)
        self.assertEqual(snapshot["source"], "ZERO_COST_SPORTS_ENDPOINT")
        request = opened.call_args.args[0]
        self.assertIn("/v4/sports/", request.full_url)
        self.assertNotIn("americanfootball_nfl/odds", request.full_url)

    def test_free_quota_preflight_fails_closed_and_redacts_secret(self):
        class FakeResponse:
            status = 200
            headers = {
                "x-requests-remaining": "497",
                "x-requests-used": "3",
                "x-requests-last": "1",
            }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"[]"

        with patch("scripts.nfl_sportsbook_capture.urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(RuntimeError, "refusing paid odds request") as raised:
                fetch_free_usage_snapshot(api_key="TOP_SECRET", now=NOW)
        self.assertNotIn("TOP_SECRET", str(raised.exception))
        with patch("scripts.nfl_sportsbook_capture.urlopen",
                   side_effect=RuntimeError("URL contained TOP_SECRET")):
            with self.assertRaises(RuntimeError) as raised:
                fetch_free_usage_snapshot(api_key="TOP_SECRET", now=NOW)
        self.assertNotIn("TOP_SECRET", str(raised.exception))

    def test_keychain_fallback_is_mac_only_and_never_prints_secret(self):
        from subprocess import CompletedProcess
        with (
            patch.dict("os.environ", {"THE_ODDS_API_KEY": ""}),
            patch("scripts.nfl_sportsbook_capture.sys.platform", "darwin"),
            patch("scripts.nfl_sportsbook_capture.getuser", return_value="scottsteele"),
            patch("scripts.nfl_sportsbook_capture.subprocess.run",
                  return_value=CompletedProcess([], 0, "KEYCHAIN_SECRET\n", "")) as run,
        ):
            self.assertEqual(resolve_odds_api_key(), "KEYCHAIN_SECRET")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/security", "find-generic-password", "-w",
             "-s", "swarmaxis-the-odds-api", "-a", "scottsteele"],
        )
        self.assertTrue(run.call_args.kwargs["capture_output"])
        with (
            patch.dict("os.environ", {"THE_ODDS_API_KEY": ""}),
            patch("scripts.nfl_sportsbook_capture.sys.platform", "linux"),
            patch("scripts.nfl_sportsbook_capture.subprocess.run",
                  side_effect=AssertionError("non-mac must not access keychain")),
        ):
            self.assertIsNone(resolve_odds_api_key())

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
                patch("scripts.nfl_sportsbook_capture.fetch_free_usage_snapshot", side_effect=AssertionError("quota read")),
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
                patch(
                    "scripts.nfl_sportsbook_capture.fetch_free_usage_snapshot",
                    return_value={"remaining": 100, "captured_at_utc": datetime.now(UTC).isoformat()},
                ),
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

    def test_insufficient_free_quota_never_reaches_paid_odds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "games.csv"
            csv_path.write_text(CSV, encoding="utf-8")
            quota_path = root / "research-quota.json"
            capture_state = root / "capture-state.json"
            output = root / "snapshot.json"
            argv = [
                "nfl_sportsbook_capture",
                "--games-csv", str(csv_path),
                "--quota-state", str(quota_path),
                "--capture-state", str(capture_state),
                "--output", str(output),
                "--fetch-once",
            ]
            with (
                patch("sys.argv", argv),
                patch.dict("os.environ", {"THE_ODDS_API_KEY": "TEST_KEY"}),
                patch("scripts.nfl_sportsbook_capture.eligible_capture_games",
                      return_value=parse_games(CSV)),
                patch("scripts.nfl_sportsbook_capture.fetch_free_usage_snapshot",
                      return_value={
                          "remaining": 10,
                          "used": 490,
                          "limit": 500,
                          "captured_at_utc": datetime.now(UTC).isoformat(),
                      }),
                patch("scripts.nfl_sportsbook_capture.fetch_nfl_v4_once",
                      side_effect=AssertionError("paid odds request must not happen")),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main()
            self.assertNotEqual(raised.exception.code, 0)
            self.assertFalse(capture_state.exists())
            self.assertFalse(output.exists())
            self.assertEqual(__import__("json").loads(
                quota_path.read_text(encoding="utf-8")
            )["remaining"], 10)

    def test_free_quota_failure_never_reaches_paid_odds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / "games.csv"
            csv_path.write_text(CSV, encoding="utf-8")
            output = root / "snapshot.json"
            argv = [
                "nfl_sportsbook_capture",
                "--games-csv", str(csv_path),
                "--quota-state", str(root / "quota.json"),
                "--capture-state", str(root / "capture-state.json"),
                "--output", str(output),
                "--fetch-once",
            ]
            with (
                patch("sys.argv", argv),
                patch.dict("os.environ", {"THE_ODDS_API_KEY": "TEST_KEY"}),
                patch("scripts.nfl_sportsbook_capture.eligible_capture_games",
                      return_value=parse_games(CSV)),
                patch("scripts.nfl_sportsbook_capture.fetch_free_usage_snapshot",
                      side_effect=RuntimeError("FREE_QUOTA_ENDPOINT_UNAVAILABLE")),
                patch("scripts.nfl_sportsbook_capture.fetch_nfl_v4_once",
                      side_effect=AssertionError("paid odds request must not happen")),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main()
            self.assertNotIn("FREE_QUOTA_ENDPOINT_UNAVAILABLE", str(raised.exception))
            self.assertFalse(output.exists())
            self.assertFalse((root / "capture-state.json").exists())

    def test_capture_window_rejects_naive_clock(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            eligible_capture_games([], now=NOW.replace(tzinfo=None))

    def test_persistent_capture_throttle_survives_process_restart(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            reserve_capture_attempt(state, now=NOW)
            with self.assertRaisesRegex(RuntimeError, "24-hour paid-attempt"):
                reserve_capture_attempt(state, now=NOW + timedelta(seconds=3599))
            with self.assertRaisesRegex(RuntimeError, "24-hour paid-attempt"):
                reserve_capture_attempt(state, now=NOW + timedelta(hours=23, minutes=59))
            reserve_capture_attempt(state, now=NOW + timedelta(hours=24))

    def test_failed_attempt_still_consumes_24_hour_reservation(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            reserve_capture_attempt(state, now=NOW)
            update_capture_state(state, status="FAILED", last_error_type="TimeoutError")
            with self.assertRaisesRegex(RuntimeError, "24-hour paid-attempt"):
                reserve_capture_attempt(state, now=NOW + timedelta(minutes=30))
            with self.assertRaisesRegex(RuntimeError, "24-hour paid-attempt"):
                reserve_capture_attempt(state, now=NOW + timedelta(hours=23))

    def test_hard_monthly_budget_counts_failed_and_successful_attempts(self):
        import json
        with TemporaryDirectory() as directory:
            state = Path(directory) / "capture-state.json"
            reserve_capture_attempt(state, now=NOW)
            update_capture_state(state, status="FAILED", last_error_type="TimeoutError")
            payload = json.loads(state.read_text(encoding="utf-8"))
            payload["attempt_count_month_utc"] = 8
            state.write_text(json.dumps(payload), encoding="utf-8")
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["attempt_month_utc"], "2026-11")
            self.assertEqual(payload["attempt_count_month_utc"], 8)
            with self.assertRaisesRegex(RuntimeError, "monthly 8-attempt"):
                reserve_capture_attempt(state, now=NOW + timedelta(days=2))
            self.assertEqual(
                json.loads(state.read_text(encoding="utf-8"))["attempt_count_month_utc"], 8,
            )
            rolled = reserve_capture_attempt(state, now=datetime(2026, 12, 1, tzinfo=UTC))
            self.assertEqual(rolled["attempt_month_utc"], "2026-12")
            self.assertEqual(rolled["attempt_count_month_utc"], 1)

    def test_corrupt_or_legacy_monthly_budget_never_allows_request(self):
        import json
        for month_state in (
            {"attempt_month_utc": "2026-11"},
            {"attempt_count_month_utc": 7},
            {"attempt_month_utc": "2026-11", "attempt_count_month_utc": "7"},
            {"attempt_month_utc": "2026-13", "attempt_count_month_utc": 7},
            {"attempt_month_utc": "2026-12", "attempt_count_month_utc": 7},
            {},
        ):
            with TemporaryDirectory() as directory:
                state = Path(directory) / "capture-state.json"
                state.write_text(json.dumps({
                    "last_attempt_at_utc": (NOW - timedelta(days=2)).isoformat(),
                    **month_state,
                }), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "budget"):
                    reserve_capture_attempt(state, now=NOW)

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

    def test_missing_malformed_or_stale_quota_is_blocked(self):
        invalid_states = (
            None,
            {},
            {"remaining": None, "captured_at_utc": NOW.isoformat()},
            {"remaining": "unknown", "captured_at_utc": NOW.isoformat()},
            {"remaining": 10, "captured_at_utc": NOW.isoformat()},
            {"remaining": -1, "captured_at_utc": NOW.isoformat()},
            {"remaining": 11},
            {"remaining": 11, "captured_at_utc": "not-a-timestamp"},
            {"remaining": 11, "captured_at_utc": (NOW - timedelta(hours=3)).isoformat()},
            {"remaining": 11, "captured_at_utc": (NOW + timedelta(seconds=1)).isoformat()},
        )
        for state in invalid_states:
            self.assertFalse(quota_headroom_verified(state, reserve=10, now=NOW))
        self.assertTrue(quota_headroom_verified(
            {"remaining": 11, "captured_at_utc": NOW.isoformat()},
            reserve=10, now=NOW,
        ))


if __name__ == "__main__":
    unittest.main()
