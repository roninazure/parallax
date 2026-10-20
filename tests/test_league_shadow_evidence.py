"""Offline integration tests: existing venue normalization -> Evidence -> qualify.

Every market fixture here is SYNTHETIC. No provider calls or BUY authority.
"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parallax.league_elo_experimental import Forecast, MODEL_STATUS
from parallax.league_shadow_evidence import ShadowFixture, evaluate_shadow, shadow_evidence
from parallax.models import Action, Side, Venue
from parallax.normalization import normalize_kalshi, normalize_pmus

NOW = datetime(2026, 10, 19, 17, 0, tzinfo=UTC)
TIP = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)


def fixture(sport="NBA"):
    return ShadowFixture(sport, "game:42", 2026, TIP, "NYK", "BOS")


def forecast(sport="NBA", home_probability=0.10):
    return Forecast(
        sport=sport, game_id="game:42", season=2026,
        game_date=TIP.date(), home_team="NYK", away_team="BOS",
        home_win_probability=home_probability, home_won=None,
        model_status=MODEL_STATUS,
    )


def kalshi(sport="NBA", **changes):
    raw = {
        "ticker": f"KX{sport}GAME-26OCT20BOSNYK-BOS",
        "event_ticker": f"KX{sport}GAME-26OCT20BOSNYK",
        "title": "Full-game winner",
        "yes_sub_title": "BOS",
        "no_sub_title": "NYK",
        "status": "active",
        "league": sport,
        "marketType": "moneyline",
        "homeTeam": "NYK",
        "awayTeam": "BOS",
        "gameStartTime": TIP.isoformat(),
        "settlement_scope": ("full_game_including_ot_so" if sport == "NHL" else "full_game"),
        "rules_primary": "Winner of full game including overtime and shootout",
        "price_level_structure": "linear_cent",
        "market_type": "binary",
        "notional_value_dollars": "1",
        "expected_expiration_time": (TIP + timedelta(hours=3)).isoformat(),
    }
    raw.update(changes)
    book = {"orderbook_fp": {"yes_dollars": [["0.48", "250"]],
                             "no_dollars": [["0.50", "250"]]}}
    return normalize_kalshi(raw, book, NOW.isoformat())


def pmus():
    raw = {
        "league": "NBA", "marketType": "moneyline",
        "homeTeam": "NYK", "awayTeam": "BOS",
        "gameStartTime": TIP.isoformat(), "settlement_scope": "full_game",
        "description": "Full-game winner including overtime",
        "marketSides": [
            {"long": True, "description": "Boston Celtics",
             "team": {"abbreviation": "BOS", "ordering": "away"}},
            {"long": False, "description": "New York Knicks",
             "team": {"abbreviation": "NYK", "ordering": "home"}},
        ],
    }
    return normalize_pmus({
        "id": "pmus:game:42", "slug": "nba-bos-nyk", "question": "Boston wins?",
        "event_id": "nba:42", "active": True, "closed": False,
        "accepting_orders": True, "raw": raw,
    }, {}, NOW.isoformat())


def test_existing_kalshi_normalization_orientation_and_buy_block():
    market = kalshi()
    evidence = shadow_evidence(market, fixture(), forecast(), now=NOW)
    assert evidence is not None
    assert evidence.fair_probability == pytest.approx(.90)
    assert evidence.validation_reference == ""
    assert evidence.validation_status == "UNVALIDATED"
    assert evidence.market_id == market.venue_market_id
    for side in (Side.YES, Side.NO):
        play = evaluate_shadow(market, fixture(), forecast(), side=side, now=NOW)
        assert play is not None
        assert play.suggested_action != Action.BUY
        assert "confidence" in play.verdict.failed_gates


def test_existing_pmus_normalization_structured_selected_team():
    market = pmus()
    assert market.venue == Venue.POLYMARKET
    evidence = shadow_evidence(market, fixture(), forecast(), now=NOW)
    assert evidence is not None
    assert evidence.fair_probability == pytest.approx(.90)
    assert evaluate_shadow(market, fixture(), forecast(), side=Side.YES, now=NOW).suggested_action != Action.BUY


@pytest.mark.parametrize("change", [
    {"marketType": "spread"}, {"league": "NFL"},
    {"homeTeam": "BKN"}, {"awayTeam": "CHI"},
    {"settlement_scope": "regulation"},
    {"gameStartTime": "2026-10-21T23:00:00+00:00"},
    {"gameStartTime": "2026-10-20"},
    {"ticker": "KXNBAGAME-26OCT20BOSNYK-LAL"},
    {"ticker": "UNVERIFIED-26OCT20BOSNYK-BOS"},
    {"yes_sub_title": "NYK"},
    {"rules_primary": "Regulation only — 3-way"},
])
def test_invalid_contract_or_identity_fails_closed(change):
    assert shadow_evidence(kalshi(**change), fixture(), forecast(), now=NOW) is None


def test_nhl_requires_ot_shootout_scope_and_distinct_sport():
    assert shadow_evidence(kalshi("NHL"), fixture("NHL"), forecast("NHL"), now=NOW) is not None
    assert shadow_evidence(kalshi("NHL", settlement_scope="full_game"),
                           fixture("NHL"), forecast("NHL"), now=NOW) is None
    assert shadow_evidence(kalshi("NHL"), fixture("NBA"), forecast("NBA"), now=NOW) is None


def test_pregame_and_unresolved_forecast_required():
    market = kalshi()
    assert shadow_evidence(market, fixture(), forecast(), now=TIP) is None
    assert shadow_evidence(market, fixture(), replace(forecast(), home_won=True), now=NOW) is None
    assert shadow_evidence(market, fixture(), replace(forecast(), game_id="another"), now=NOW) is None
    assert shadow_evidence(market, fixture(), replace(forecast(), model_status="CALIBRATED"), now=NOW) is None


def test_ambiguous_pmus_sides_rejected():
    market = pmus()
    raw = dict(market.original_metadata["market"])
    sides = [dict(x) for x in raw["marketSides"]]
    sides[1]["long"] = True
    raw["marketSides"] = sides
    ambiguous = replace(market, original_metadata={"market": raw})
    assert shadow_evidence(ambiguous, fixture(), forecast(), now=NOW) is None
    sides[1]["long"] = False
    sides[1]["team"] = {"abbreviation": "CHI"}
    raw["marketSides"] = sides
    wrong_opponent = replace(market, original_metadata={"market": raw})
    assert shadow_evidence(wrong_opponent, fixture(), forecast(), now=NOW) is None
