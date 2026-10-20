"""Offline integration tests with sanitized *real* Kalshi NBA/NHL rule fields.

Kalshi metadata/rules came from user-inspected October 19, 2026 public
markets. Kickoff times match independently published NBA/NHL schedules;
market order book and expiration are synthetic. Fixture IDs in this test are
NOT yet reconciled to the BALLDONTLIE provider IDs used by the Elo snapshot.
Polymarket US fixture is synthetic/unverified. No provider calls or BUYs.
"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from parallax.league_elo_experimental import Forecast, MODEL_STATUS
from parallax.league_shadow_evidence import ShadowFixture, evaluate_shadow, shadow_evidence
from parallax.models import Action, Side, Venue
from parallax.normalization import normalize_kalshi, normalize_pmus

NOW = datetime(2026, 10, 19, 17, 0, tzinfo=UTC)
_ET = ZoneInfo("America/New_York")
# Independently verified matchup/start, NOT inferred from Kalshi occurrence_datetime.
# NBA: https://www.nba.com/team/1610612744/schedule (Oct 26 @ DEN, 10 PM ET)
# NHL: https://www.nhl.com/schedule/2026-10-23 (Oct 26 TOR @ CGY, 8:30 PM ET)
# These are sourced TEST fixtures, not a licensed automated production feed.
TIPS = {
    "NBA": datetime(2026, 10, 27, 2, 0, tzinfo=UTC),
    "NHL": datetime(2026, 10, 27, 0, 30, tzinfo=UTC),
}

MARKETS = {
    "NBA": {
        "ticker": "KXNBAGAME-26OCT26GSWDEN-GSW",
        "event_ticker": "KXNBAGAME-26OCT26GSWDEN",
        "title": "Golden State wins",
        "yes_sub_title": "Golden State",
        "no_sub_title": "Golden State",
        "rules_primary": (
            "If Golden State wins the Golden State vs Denver Pro Basketball "
            "game originally scheduled for Oct 26, 2026, then the market resolves to Yes."
        ),
        "rules_secondary": (
            "If the game is postponed but begins within 48 hours of its originally "
            "scheduled start time, the market will remain open and resolve based "
            "on the official final result. If the game is cancelled or not started "
            "within 48 hours of its originally scheduled start, all markets will "
            "resolve to a fair price."
        ),
        "occurrence_datetime": "2026-10-27T05:00:00Z",
    },
    "NHL": {
        "ticker": "KXNHLGAME-26OCT26TORCGY-TOR",
        "event_ticker": "KXNHLGAME-26OCT26TORCGY",
        "title": "Toronto wins",
        "yes_sub_title": "Toronto",
        "no_sub_title": "Toronto",
        "rules_primary": (
            "If Toronto wins the Toronto vs Calgary NHL game originally "
            "scheduled for Oct 26, 2026, then the market resolves to Yes."
        ),
        "rules_secondary": (
            "If the game is postponed but begins within 48 hours of its originally "
            "scheduled start time, the market will remain open and resolve based "
            "on the official final result. If the game is cancelled or not started "
            "within 48 hours of its originally scheduled start, all markets will "
            "resolve to a fair price."
        ),
        "occurrence_datetime": "2026-10-27T03:30:00Z",
    },
}


def fixture(sport="NBA", **changes):
    if sport == "NBA":
        kwargs = dict(sport=sport, game_id="nba.com:0022600127",
                      season=2026, kickoff=TIPS[sport],
                      home_team="DEN", away_team="GSW",
                      venue_home_name="Denver", venue_away_name="Golden State")
    else:
        kwargs = dict(sport=sport, game_id="nhl-schedule:2026-10-26:TOR@CGY",
                      season=2026, kickoff=TIPS[sport],
                      home_team="CGY", away_team="TOR",
                      venue_home_name="Calgary", venue_away_name="Toronto")
    kwargs.update(changes)
    return ShadowFixture(**kwargs)


def forecast(sport="NBA", home_probability=.10, **changes):
    fx = fixture(sport)
    kwargs = dict(
        sport=sport, game_id=fx.game_id, season=fx.season,
        game_date=fx.kickoff.astimezone(_ET).date(),
        home_team=fx.home_team, away_team=fx.away_team,
        home_win_probability=home_probability, home_won=None,
        model_status=MODEL_STATUS,
    )
    kwargs.update(changes)
    return Forecast(**kwargs)


def kalshi(sport="NBA", **changes):
    raw = {
        **MARKETS[sport],
        "status": "active",
        "market_type": "binary",
        "price_level_structure": "linear_cent",
        "notional_value_dollars": "1",
        "expected_expiration_time": (TIPS[sport] + timedelta(hours=3)).isoformat(),
    }
    raw.update(changes)
    book = {"orderbook_fp": {"yes_dollars": [["0.48", "250"]],
                             "no_dollars": [["0.50", "250"]]}}
    return normalize_kalshi(raw, book, NOW.isoformat())


def pmus():
    raw = {
        # SYNTHETIC: actual PMUS mapping has not been verified.
        "league": "NBA", "marketType": "moneyline",
        "homeTeam": "DEN", "awayTeam": "GSW",
        "gameStartTime": TIPS["NBA"].isoformat(), "settlement_scope": "full_game",
        "description": "Full-game winner including overtime",
        "marketSides": [
            {"long": True, "description": "Golden State Warriors",
             "team": {"abbreviation": "GSW", "ordering": "away"}},
            {"long": False, "description": "Denver Nuggets",
             "team": {"abbreviation": "DEN", "ordering": "home"}},
        ],
    }
    return normalize_pmus({
        "id": "pmus:game:42", "slug": "nba-gsw-den", "question": "Golden State wins?",
        "event_id": "nba:42", "active": True, "closed": False,
        "accepting_orders": True, "raw": raw,
    }, {}, NOW.isoformat())


@pytest.mark.parametrize("sport", ["NBA", "NHL"])
def test_actual_kalshi_contract_metadata_maps_to_existing_evidence(sport):
    market = kalshi(sport)
    assert market.venue == Venue.KALSHI
    for invented_field in ("league", "homeTeam", "awayTeam",
                           "gameStartTime", "settlement_scope", "marketType"):
        assert invented_field not in market.original_metadata["market"]
    e = shadow_evidence(market, fixture(sport), forecast(sport), now=NOW)
    assert e is not None
    assert e.fair_probability == pytest.approx(.90)
    assert e.market_id == market.venue_market_id
    assert not e.validation_reference
    assert e.validation_status == "UNVALIDATED"
    for side in (Side.YES, Side.NO):
        play = evaluate_shadow(market, fixture(sport), forecast(sport),
                               side=side, now=NOW)
        assert play is not None
        assert play.suggested_action != Action.BUY
        assert "confidence" in play.verdict.failed_gates


def test_actual_fixture_times_and_eastern_game_date_are_not_venue_expirations():
    assert TIPS["NBA"].astimezone(_ET).isoformat() == "2026-10-26T22:00:00-04:00"
    assert TIPS["NHL"].astimezone(_ET).isoformat() == "2026-10-26T20:30:00-04:00"
    assert TIPS["NBA"] != TIPS["NHL"]
    for sport in ("NBA", "NHL"):
        fx = fixture(sport)
        pred = forecast(sport)
        assert pred.game_date.isoformat() == "2026-10-26"
        assert fx.kickoff == TIPS[sport]
        # Kalshi occurrence_datetime is later than tipoff: never use it as kickoff.
        occurrence = datetime.fromisoformat(
            MARKETS[sport]["occurrence_datetime"].replace("Z", "+00:00"))
        assert occurrence > fx.kickoff
        assert shadow_evidence(kalshi(sport), fx, pred, now=NOW) is not None
    market = kalshi(occurrence_datetime="2026-11-01T00:00:00Z")
    assert shadow_evidence(market, fixture(), forecast(), now=NOW) is not None


@pytest.mark.parametrize("changes", [
    {"ticker": "KXNBAGAME-26OCT26GSWDEN-LAL"},
    {"ticker": "KXNHLGAME-26OCT26GSWDEN-GSW"},
    {"ticker": "KXNBAGAME-26OCT27GSWDEN-GSW"},
    {"ticker": "KXNBAGAME-26OCT26GSWLAL-GSW"},
    {"ticker": "KXNBAGAME-26OCT26GSWDEN-GSW-JUNK"},
    {"event_ticker": "KXNBAGAME-26OCT27GSWDEN"},
    {"title": "Denver wins"},
    {"yes_sub_title": "Denver"},
    {"rules_primary": "Regulation only — Golden State wins"},
    {"rules_primary": "If Golden State wins the Golden State vs Denver NHL game originally scheduled for Oct 26, 2026, then the market resolves to Yes."},
    {"rules_primary": "If Golden State wins the Golden State vs LA Pro Basketball game originally scheduled for Oct 26, 2026, then the market resolves to Yes."},
    {"rules_primary": "If Golden State wins the Golden State vs Denver Pro Basketball game originally scheduled for Oct 27, 2026, then the market resolves to Yes."},
    {"rules_primary": ""},
    {"status": "closed"},
])
def test_kalshi_identity_or_rules_conflict_fails_closed(changes):
    assert shadow_evidence(kalshi(**changes), fixture(), forecast(), now=NOW) is None


def test_synthetic_pmus_orientation_is_still_uncertified():
    market = pmus()
    e = shadow_evidence(market, fixture(), forecast(), now=NOW)
    assert e is not None
    assert e.fair_probability == pytest.approx(.90)
    assert evaluate_shadow(market, fixture(), forecast(), side=Side.YES,
                           now=NOW).suggested_action != Action.BUY


def test_wrong_fixture_or_missing_verified_alias_fails_closed():
    market = kalshi()
    assert shadow_evidence(market, fixture(home_team="LAL"), forecast(), now=NOW) is None
    assert shadow_evidence(market, fixture(kickoff=TIPS["NBA"] + timedelta(days=1)),
                           forecast(), now=NOW) is None
    assert shadow_evidence(market, fixture(venue_home_name="Denver Nuggets"),
                           forecast(), now=NOW) is None
    assert shadow_evidence(market, fixture(venue_home_name="",
                                          venue_away_name=""), forecast(), now=NOW) is None
    with pytest.raises(ValueError):
        fixture(venue_home_name="Denver", venue_away_name="Denver")


def test_pregame_and_unresolved_forecast_required():
    market = kalshi()
    assert shadow_evidence(market, fixture(), forecast(), now=TIPS["NBA"]) is None
    assert shadow_evidence(market, fixture(), replace(forecast(), home_won=True), now=NOW) is None
    assert shadow_evidence(market, fixture(), forecast(game_id="another"), now=NOW) is None
    assert shadow_evidence(market, fixture(), forecast(model_status="CALIBRATED"), now=NOW) is None


def test_ambiguous_pmus_sides_rejected():
    market = pmus()
    raw = dict(market.original_metadata["market"])
    sides = [dict(x) for x in raw["marketSides"]]
    sides[1]["long"] = True
    raw["marketSides"] = sides
    assert shadow_evidence(replace(market, original_metadata={"market": raw}),
                           fixture(), forecast(), now=NOW) is None
    sides[1]["long"] = False
    sides[1]["team"] = {"abbreviation": "CHI"}
    raw["marketSides"] = sides
    assert shadow_evidence(replace(market, original_metadata={"market": raw}),
                           fixture(), forecast(), now=NOW) is None
