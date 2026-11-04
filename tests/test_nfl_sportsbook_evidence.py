import unittest
from datetime import UTC, datetime, timedelta

from parallax.nfl import NFLGame
from sports.nfl_research_fair_value import BookMoneyline
from sports.nfl_evidence import match_nfl_evidence
from sports.odds_provider import SportsEvent


NOW = datetime(2026, 11, 1, 15, tzinfo=UTC)
START = NOW + timedelta(hours=3)
GAME = NFLGame("2026_09_IND_JAX", 2026, "REG", START.isoformat(), "JAX", "IND", None, None)


def event(*quotes, home="Jacksonville Jaguars", away="Indianapolis Colts", start=None):
    return SportsEvent("evt", "americanfootball_nfl", "NFL", home, away,
                       start or START.isoformat(), tuple(quotes))


class NFLSportsbookEvidenceTests(unittest.TestCase):
    def test_two_independent_books_match_game_and_sides(self):
        result = match_nfl_evidence(event(
            BookMoneyline("A", 1.8, 2.1, 20), BookMoneyline("B", 1.85, 2.05, 30)),
            [GAME], observed_at=NOW)
        self.assertIsNotNone(result)
        self.assertEqual(result.game_id, GAME.game_id)
        self.assertEqual(result.bookmaker_count, 2)
        self.assertAlmostEqual(result.home_probability + result.away_probability, 1)

    def test_duplicate_book_is_not_independent(self):
        self.assertIsNone(match_nfl_evidence(event(
            BookMoneyline("A", 1.8, 2.1, 20), BookMoneyline("a", 1.85, 2.05, 10)),
            [GAME], observed_at=NOW))

    def test_stale_or_future_quote_fails_closed(self):
        for age in (-5, 301):
            self.assertIsNone(match_nfl_evidence(event(
                BookMoneyline("A", 1.8, 2.1, age), BookMoneyline("B", 1.85, 2.05, 20)),
                [GAME], observed_at=NOW))

    def test_wrong_team_and_ambiguous_fixture_fail_closed(self):
        quotes = (BookMoneyline("A", 1.8, 2.1, 20), BookMoneyline("B", 1.85, 2.05, 20))
        self.assertIsNone(match_nfl_evidence(event(*quotes, home="Houston Texans"), [GAME], observed_at=NOW))
        self.assertIsNone(match_nfl_evidence(event(*quotes), [GAME, GAME], observed_at=NOW))

    def test_kickoff_mismatch_and_postkickoff_fail_closed(self):
        quotes = (BookMoneyline("A", 1.8, 2.1, 20), BookMoneyline("B", 1.85, 2.05, 20))
        self.assertIsNone(match_nfl_evidence(event(*quotes, start=(START + timedelta(hours=1)).isoformat()), [GAME], observed_at=NOW))
        self.assertIsNone(match_nfl_evidence(event(*quotes), [GAME], observed_at=START))

    def test_timezone_naive_observation_is_rejected(self):
        self.assertIsNone(match_nfl_evidence(event(
            BookMoneyline("A", 1.8, 2.1, 20), BookMoneyline("B", 1.85, 2.05, 20)),
            [GAME], observed_at=NOW.replace(tzinfo=None)))

    def test_non_nfl_event_rejected(self):
        quotes = (BookMoneyline("A", 1.8, 2.1, 20), BookMoneyline("B", 1.85, 2.05, 20))
        candidate = event(*quotes)
        from dataclasses import replace
        self.assertIsNone(match_nfl_evidence(replace(candidate, league="CFB"), [GAME], observed_at=NOW))


if __name__ == "__main__":
    unittest.main()
