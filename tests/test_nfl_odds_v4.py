import unittest
import json
from unittest.mock import patch
from datetime import UTC, datetime, timedelta

from sports.nfl_odds_v4 import fetch_nfl_v4_once, parse_nfl_v4


NOW = datetime(2026, 11, 1, 15, tzinfo=UTC)


def payload(*, updated=None, duplicate=False):
    book = {"key": "fanduel", "markets": [{"key": "h2h", "last_update": updated or NOW.isoformat(),
            "outcomes": [{"name": "Indianapolis Colts", "price": 120},
                         {"name": "Jacksonville Jaguars", "price": -140}]}]}
    return [{"id": "evt", "sport_key": "americanfootball_nfl",
             "home_team": "Jacksonville Jaguars", "away_team": "Indianapolis Colts",
             "commence_time": (NOW + timedelta(hours=3)).isoformat(),
             "bookmakers": [book, book] if duplicate else [book]}]


class NFLOddsV4Tests(unittest.TestCase):
    def test_market_timestamp_and_v4_headers(self):
        response = parse_nfl_v4(payload(), now=NOW,
                                headers={"x-requests-remaining": "92", "x-requests-used": "8"})
        self.assertEqual(response.quota.remaining, 92)
        self.assertEqual(len(response.events), 1)
        self.assertEqual(response.events[0].moneylines[0].age_seconds, 0)

    def test_future_market_timestamp_preserves_negative_age(self):
        response = parse_nfl_v4(payload(updated=(NOW + timedelta(minutes=2)).isoformat()),
                                now=NOW, headers={})
        self.assertEqual(response.events[0].moneylines[0].age_seconds, -120)

    def test_naive_market_timestamp_is_rejected(self):
        response = parse_nfl_v4(payload(updated="2026-11-01T15:00:00"), now=NOW, headers={})
        self.assertEqual(response.events[0].moneylines, ())

    def test_non_v4_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_nfl_v4({"data": []}, now=NOW, headers={})

    def test_fetch_ages_quotes_at_response_receipt_not_request_start(self):
        class FakeResponse:
            status = 200
            headers = {"x-requests-remaining": "92", "x-requests-used": "8"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload(updated=NOW.isoformat())).encode("utf-8")

        received = NOW + timedelta(seconds=17)
        with patch("urllib.request.urlopen", return_value=FakeResponse()):
            response = fetch_nfl_v4_once(
                api_key="TEST_KEY",
                now=NOW,
                response_clock=lambda: received,
            )
        self.assertEqual(response.events[0].moneylines[0].age_seconds, 17)


if __name__ == "__main__":
    unittest.main()
