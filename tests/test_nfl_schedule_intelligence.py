"""Schedule-first NFL intelligence must not depend on BUY qualification."""
from types import SimpleNamespace

from scripts import nfl_live_scan


def _game():
    return SimpleNamespace(game_id="g-1", home_team="SF", away_team="MIN", kickoff="2026-11-23T01:00:00+00:00")


def _scheduled():
    return [{
        "game_id": "g-1", "home_team": "SF", "away_team": "MIN",
        "start_time": "2026-11-23T01:00:00+00:00",
        "date": "2026-11-22", "schedule_status": "SCHEDULED",
    }]


def test_schedule_forecast_survives_failed_buy_calibration(monkeypatch):
    monkeypatch.setattr(nfl_live_scan, "probability_for_game", lambda game, games: 0.63)
    market_rows = [{
        "game_id": "g-1", "venue": "PMUS", "market_id": "p1",
        "side": "YES", "verdict": "WATCH",
        "failed_gates": ["nfl_calibration_safety"],
        "acquisition_status": "ACQUIRED", "data_freshness": "FRESH",
        "executable_price": 0.57, "publication_eligible": False,
    }]
    result = nfl_live_scan._schedule_intelligence([_game()], _scheduled(), market_rows)
    assert len(result) == 1
    assert result[0]["home_win_probability"] == 0.63
    assert result[0]["away_win_probability"] == 0.37
    assert result[0]["forecast_kind"] == "RESEARCH_ONLY"
    assert result[0]["certified_buy"] is False
    assert result[0]["venue_quotes"][0]["executable_price"] == 0.57


def test_stale_or_unacquired_prices_are_not_republished(monkeypatch):
    monkeypatch.setattr(nfl_live_scan, "probability_for_game", lambda game, games: 0.61)
    rows = [
        {"game_id": "g-1", "venue": "KALSHI", "side": "NO",
         "acquisition_status": "ACQUIRED", "data_freshness": "STALE",
         "executable_price": 0.4},
        {"game_id": "g-1", "venue": "PMUS", "side": "YES",
         "acquisition_status": "AVOIDED", "data_freshness": "FRESH",
         "executable_price": 0.6},
    ]
    game = nfl_live_scan._schedule_intelligence([_game()], _scheduled(), rows)[0]
    assert all(q["executable_price"] is None for q in game["venue_quotes"])
    assert all(q["quote_state"] == "UNVERIFIED" for q in game["venue_quotes"])


def test_scheduled_game_without_a_market_still_has_research(monkeypatch):
    monkeypatch.setattr(nfl_live_scan, "probability_for_game", lambda game, games: 0.5)
    game = nfl_live_scan._schedule_intelligence([_game()], _scheduled(), [])[0]
    assert game["home_win_probability"] == 0.5
    assert game["venue_quotes"] == []
    assert game["certified_buy"] is False


def test_unknown_schedule_identity_fails_closed():
    game = nfl_live_scan._schedule_intelligence([], _scheduled(), [])[0]
    assert game["home_win_probability"] is None
    assert game["forecast_kind"] == "UNAVAILABLE"


def test_public_feed_preserves_forecast_but_never_fakes_buy():
    from datetime import datetime, UTC
    from parallax.public_feed import _public_nfl_game_intelligence
    now = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
    game = {
        **_scheduled()[0],
        "home_win_probability": 0.63,
        "away_win_probability": 0.37,
        "forecast_kind": "RESEARCH_ONLY",
        "forecast_model": "nfl-v1-elo-rolling",
        "certified_buy": True,  # never trust this source hint
        "venue_quotes": [
            {"venue": "PMUS", "side": "YES", "market_id": "fresh",
             "executable_price": 0.55, "quote_observed_at": "2026-11-22T19:59:30+00:00",
             "quote_state": "FRESH"},
            {"venue": "KALSHI", "side": "YES", "market_id": "expired",
             "executable_price": 0.5, "quote_observed_at": "2026-11-22T19:58:30+00:00",
             "quote_state": "FRESH"},
            {"venue": "PMUS", "side": "NO", "market_id": "no-time",
             "executable_price": 0.3, "quote_state": "FRESH"},
        ],
    }
    public = _public_nfl_game_intelligence([game], now)[0]
    assert public["home_win_probability"] == 0.63
    assert "certified_buy" not in public
    assert public["venue_quotes"][0]["executable_price"] == 0.55
    assert "executable_price" not in public["venue_quotes"][1]
    assert "executable_price" not in public["venue_quotes"][2]


def test_public_feed_invalid_research_is_withheld():
    from datetime import datetime, UTC
    from parallax.public_feed import _public_nfl_game_intelligence
    game = {
        **_scheduled()[0],
        "home_win_probability": 0.63,
        "away_win_probability": 0.63,
        "forecast_kind": "RESEARCH_ONLY",
        "venue_quotes": [],
    }
    public = _public_nfl_game_intelligence([game], datetime(2026, 11, 22, tzinfo=UTC))[0]
    assert public["forecast_kind"] == "UNAVAILABLE"
    assert "home_win_probability" not in public
