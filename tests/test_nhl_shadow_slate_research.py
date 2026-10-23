"""Offline contract tests for the isolated NHL slate research scanner."""
from datetime import UTC, datetime

import pytest

from scripts import nhl_shadow_slate_research as scan


def official_game(*, home="CGY", away="TOR", place_home="Calgary", place_away="Toronto"):
    return {
        "id": 2026020186,
        "season": 20262027,
        "gameType": 2,
        "gameState": "FUT",
        "startTimeUTC": "2026-10-27T00:30:00Z",
        "homeTeam": {"abbrev": home, "placeName": {"default": place_home}},
        "awayTeam": {"abbrev": away, "placeName": {"default": place_away}},
    }


def test_official_fixture_identity_independent_of_kalshi():
    now = datetime(2026, 10, 22, tzinfo=UTC)
    rows = scan.nhl_fixtures({"gameWeek": [{"games": [official_game()]}]}, now)
    assert list(rows) == ["KXNHLGAME-26OCT26TORCGY"]
    fixture = rows["KXNHLGAME-26OCT26TORCGY"]
    assert (fixture.game_id, fixture.season) == ("nhl:2026020186", 2026)
    assert (fixture.home_team, fixture.away_team) == ("CGY", "TOR")
    assert (fixture.venue_home_name, fixture.venue_away_name) == (
        "Calgary", "Toronto"
    )
    assert fixture.kickoff == datetime(2026, 10, 27, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("change", [
    {"gameType": 3},
    {"gameState": "FINAL"},
])
def test_non_eligible_games_are_skipped(change):
    game = {**official_game(), **change}
    assert scan.nhl_fixtures(
        {"gameWeek": [{"games": [game]}]},
        datetime(2026, 10, 22, tzinfo=UTC),
    ) == {}


@pytest.mark.parametrize("change", [
    {"homeTeam": {"abbrev": "CGY"}},
    {"homeTeam": {"abbrev": "TOR", "placeName": {"default": "Toronto"}}},
    {"season": 20252026},
])
def test_source_alias_and_season_conflicts_fail_closed(change):
    game = {**official_game(), **change}
    with pytest.raises(ValueError):
        scan.nhl_fixtures(
            {"gameWeek": [{"games": [game]}]},
            datetime(2026, 10, 22, tzinfo=UTC),
        )


def test_duplicate_game_ids_fail_closed():
    with pytest.raises(ValueError, match="Duplicate"):
        scan.nhl_fixtures(
            {"gameWeek": [{"games": [official_game(), official_game()]}]},
            datetime(2026, 10, 22, tzinfo=UTC),
        )


def test_malformed_listing_fails_closed():
    with pytest.raises(ValueError):
        scan.nhl_fixtures({}, datetime(2026, 10, 22, tzinfo=UTC))


def test_kalshi_pagination_requires_complete_results(monkeypatch):
    i = 0
    def f(url):
        nonlocal i
        i += 1
        return {
            "events": [{
                "series_ticker": "KXNHLGAME",
                "event_ticker": f"KXNHLGAME-{i}",
                "markets": [],
            }],
            "cursor": "more",
        }

    monkeypatch.setattr(scan, "fetch_json", f)
    with pytest.raises(ValueError, match="pagination incomplete"):
        scan.kalshi_events()
    assert i == scan.MAX_EVENT_PAGES


def test_market_pair_must_be_exact_before_any_book_calls(monkeypatch):
    def no_calls(url):
        raise AssertionError("No provider requests permitted")

    monkeypatch.setattr(scan, "fetch_json", no_calls)
    fixture = scan.nhl_fixtures(
        {"gameWeek": [{"games": [official_game()]}]},
        datetime(2026, 10, 22, tzinfo=UTC),
    )["KXNHLGAME-26OCT26TORCGY"]
    event = {
        "event_ticker": "KXNHLGAME-26OCT26TORCGY",
        "series_ticker": "KXNHLGAME",
        "markets": [
            {"ticker": "KXNHLGAME-26OCT26TORCGY-TOR"},
            {"ticker": "KXNHLGAME-26OCT26TORCGY-DET"},
        ],
    }
    with pytest.raises(ValueError, match="Market pair"):
        scan.evaluate_fixture(fixture, event, [])
