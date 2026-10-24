"""Offline integration tests with sanitized observed Kalshi/PMUS market fields.

Observed metadata is limited to user-inspected October 2026 contracts.
NBA kickoff/BDL game ID are independently verified. Order books here are
synthetic test data; no provider requests, feed publishing or BUYs.
"""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from parallax.league_elo_experimental import Forecast, MODEL_STATUS, predict_future
from parallax.league_shadow_evidence import (
    ShadowFixture, evaluate_shadow, shadow_evidence, nba_fixture_from_balldontlie,
)
from parallax.models import Action, Side, Venue
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.nba_nhl import scan_league, fetch_nba_fixtures, fetch_nhl_fixtures
from parallax.public_feed import sanitize_completed_scan

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

# BALLDONTLIE Oct 26, 2026 fixture identity verified via one bounded query.
# Team objects retain only fields required by the offline adapter.
NBA_BDL_GAME = {
    "id": 21717970,
    "season": 2026,
    "date": "2026-10-26",
    "status_state": "scheduled",
    "postseason": False,
    "start_time_utc": None,
    "datetime": "2026-10-27T02:00:00.000Z",
    "home_team": {"abbreviation": "DEN"},
    "visitor_team": {"abbreviation": "GSW"},
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
        kwargs = dict(sport=sport, game_id="bdl:21717970",
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


PMUS_NBA_DESCRIPTION = (
    "This market will settle to the winner of the GS Warriors vs DEN Nuggets "
    "professional basketball game originally scheduled for Oct 26, 2026 at "
    "10:00PM ET. Overtime is included if played. If the game is delayed, "
    "postponed, suspended, or otherwise rescheduled and is not rescheduled "
    "to start within two calendar days of the originally scheduled date "
    "and time, the market will settle to the last fair market price. "
    "Outcome sourced from the relevant governing body."
)


def pmus(**changes):
    """Offline clone of inspected Oct 26 PMUS NBA market ID 1386496.

    The discovery payload verified code gs (not GSW) and source ordering.
    No live book or network requests in this test helper.
    """
    raw = {
        "id": 1386496, "slug": "aec-nba-gs-den-2026-10-26",
        "marketType": "moneyline", "status": "MARKET_STATUS_OPEN",
        "gameStartTime": TIPS["NBA"].isoformat(),
        "description": PMUS_NBA_DESCRIPTION,
        "marketSides": [
            {"long": True, "description": "Warriors",
             "team": {"abbreviation": "gs", "name": "Warriors",
                      "league": "nba", "ordering": "away"}},
            {"long": False, "description": "Nuggets",
             "team": {"abbreviation": "den", "name": "Nuggets",
                      "league": "nba", "ordering": "home"}},
        ],
    }
    raw.update(changes)
    return normalize_pmus({
        "id": "1386496", "slug": "aec-nba-gs-den-2026-10-26",
        "question": "Who will win: GS Warriors vs DEN Nuggets?",
        "event_id": "nba:gs-den:2026-10-26", "active": True,
        "closed": False, "accepting_orders": True, "raw": raw,
    }, {}, NOW.isoformat())


def test_real_nba_provider_id_to_model_to_kalshi_evidence():
    fx = nba_fixture_from_balldontlie(
        NBA_BDL_GAME, regular_season_query_verified=True,
        venue_home_name="Denver", venue_away_name="Golden State",
    )
    assert fx.game_id == "bdl:21717970"
    assert fx.season == 2026
    assert fx.kickoff == TIPS["NBA"]
    assert (fx.home_team, fx.away_team) == ("DEN", "GSW")
    # The existing model is uncalibrated. Empty history tests ONLY identity
    # binding, not predicted accuracy or economic edge.
    prediction = predict_future(
        [], sport=fx.sport, game_date=fx.kickoff.astimezone(_ET).date(),
        home_team=fx.home_team, away_team=fx.away_team,
        game_id=fx.game_id, season=fx.season,
    )
    evidence = shadow_evidence(kalshi(), fx, prediction, now=NOW)
    assert evidence is not None
    assert evidence.review_reference == "shadow:NBA:bdl:21717970"
    assert not evidence.validation_reference
    assert evaluate_shadow(kalshi(), fx, prediction, side=Side.YES,
                           now=NOW).suggested_action != Action.BUY
    assert shadow_evidence(kalshi(), fx,
                           replace(prediction, game_id="future"), now=NOW) is None


def test_mocked_provider_fixture_to_normalized_market_and_risk_qualification():
    fx = nba_fixture_from_balldontlie(
        NBA_BDL_GAME, regular_season_query_verified=True,
        venue_home_name="Denver", venue_away_name="Golden State",
    )
    raw = {**MARKETS["NBA"], "status": "active", "market_type": "binary",
           "price_level_structure": "linear_cent", "notional_value_dollars": "1",
           "expected_expiration_time": "2026-10-27T05:00:00Z"}
    class Book:
        def book(self, _ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.48", "250"]],
                                     "no_dollars": [["0.50", "250"]]}}
    result = scan_league("NBA", [fx], [], [raw], [], kalshi_client=Book(), now=NOW)
    assert result["read_only"] is True and result["orders"] == 0
    assert len(result["rows"]) == 2
    assert all(row["mapping_status"] == "MAPPED" for row in result["rows"])
    assert all(row["evidence_validation_status"] == "UNVALIDATED" for row in result["rows"])
    assert all(row["publication_eligible"] is False for row in result["rows"])
    safe = sanitize_completed_scan("nba", __import__("json").dumps(result), generated_at=NOW)
    assert safe["summary"]["buy"] == 0
    assert {row["action"] for row in safe["plays"]}.issubset({"WATCH", "PASS"})


def test_mocked_pmus_client_market_uses_same_fixture_match_normalize_qualify_path():
    normalized = pmus()
    raw = {"id": normalized.venue_market_id, "slug": normalized.slug,
        "question": normalized.title, "event_id": "nba:gs-den:2026-10-26",
        "active": True, "closed": False, "accepting_orders": True,
        "raw": normalized.original_metadata["market"]}
    class PMUS:
        def book(self, slug):
            return {f"{slug}::YES": {"best_bid": .45, "best_ask": .47,
                    "bid_size_shares": 100, "ask_size_shares": 100},
                    f"{slug}::NO": {"best_bid": .53, "best_ask": .55,
                    "bid_size_shares": 100, "ask_size_shares": 100}}
    result = scan_league("NBA", [fixture()], [], [], [raw], pmus_client=PMUS(), now=NOW)
    assert len(result["rows"]) == 2
    assert all(row["acquisition_status"] == "ACQUIRED" for row in result["rows"])
    assert all(row["publication_eligible"] is False for row in result["rows"])


def test_mocked_bdl_schedule_response_uses_existing_verified_fixture_adapter():
    row = {**NBA_BDL_GAME,
        "home_team": {"abbreviation": "DEN", "city": "Denver"},
        "visitor_team": {"abbreviation": "GSW", "city": "Golden State"}}
    payload = {"data": [row], "meta": {"next_cursor": None}}
    seen = []
    query_now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    result = fetch_nba_fixtures(now=query_now, transport=lambda url: (seen.append(url), payload)[1])
    assert len(result) == 1
    assert result[0].game_id == "bdl:21717970"
    query = parse_qs(urlsplit(seen[0]).query)
    assert query == {
        "seasons[]": ["2026"], "season_type": ["regular"], "per_page": ["100"],
        "dates[]": ["2026-10-26", "2026-10-27", "2026-10-28"],
    }


def test_kalshi_discovery_uses_paginated_nested_events_and_exact_fixture_ticker(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import _league_live_scan as scanner

    class Kalshi:
        request_count = 0
        def events_page(self, *, series_ticker, limit, cursor, with_nested_markets):
            assert series_ticker == "KXNBAGAME"
            assert limit == 200 and with_nested_markets is True
            self.request_count += 1
            return {"events": [
                {"event_ticker": "KXNBAGAME-26OCT25UNRELATED", "series_ticker": series_ticker, "markets": []},
                {"event_ticker": MARKETS["NBA"]["event_ticker"], "series_ticker": series_ticker,
                 "markets": [{**MARKETS["NBA"], "ticker": MARKETS["NBA"]["ticker"]}]},
            ], "cursor": ""}
        def close(self):
            pass

    class PMUS:
        def markets_page(self, *, limit, offset, retry_transport_errors):
            assert retry_transport_errors is False
            return {"markets": []}
        def close(self):
            pass

    kalshi = Kalshi()
    monkeypatch.setattr(scanner, "KalshiPublicClient", lambda: kalshi)
    monkeypatch.setattr(scanner, "PolymarketUSPublicClient", PMUS)

    class Acquisition:
        def discover(self, _key, fetch):
            return fetch()

    target = fixture()
    early = [
        replace(target, game_id=f"early-{i}", kickoff=NOW + timedelta(hours=i),
                away_team=f"A{i}", home_team=f"H{i}")
        for i in (1, 2, 3)
    ]
    rows, pmus_rows, health, _k, _p = scanner._live_markets(
        "NBA", NOW, Acquisition(), [*early, target],
    )
    assert len(rows) == 1 and rows[0]["ticker"] == MARKETS["NBA"]["ticker"]
    assert pmus_rows == []
    assert kalshi.request_count == 1
    assert health["KALSHI"]["coverage"]["exact_fixture_candidates"] == 1
    assert health["KALSHI"]["coverage"]["request_count"] == 1


def test_bdl_credential_reuses_owner_only_research_key_file(tmp_path, monkeypatch):
    from parallax import nba_nhl

    key_file = tmp_path / ".bdl-api-key"
    key_file.write_text("test-only-credential", encoding="ascii")
    key_file.chmod(0o600)
    monkeypatch.setattr(nba_nhl, "_BDL_KEY_FILE", key_file)
    monkeypatch.delenv("BALLDONTLIE_API_KEY", raising=False)
    assert nba_nhl._balldontlie_api_key() == "test-only-credential"


def test_scanner_reuses_research_nba_history_when_runtime_csv_is_absent(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from _league_live_scan import _load_existing_history
    history = tmp_path / "parallax-research/nba/nba-2024-2025-canonical.csv"
    history.parent.mkdir(parents=True)
    history.write_text(
        "sport,game_id,season,game_date,home_team,away_team,winner_team\n"
        "NBA,bdl:1,2025,2025-01-01,BOS,NYK,BOS\n", encoding="utf-8",
    )
    loaded = _load_existing_history("NBA", tmp_path / "missing.csv", explicit=False)
    assert [(game.game_id, game.winner_team) for game in loaded] == [("bdl:1", "BOS")]


def test_mocked_nhl_schedule_response_builds_only_future_regular_fixtures():
    payload = {"gameWeek": [{"games": [{
        "id": 2026020012, "season": 20262027, "gameType": 2, "gameState": "FUT", "gameScheduleState": "OK",
        "startTimeUTC": "2026-10-27T00:30:00Z",
        "homeTeam": {"abbrev": "CGY", "placeName": {"default": "Calgary"}},
        "awayTeam": {"abbrev": "TOR", "placeName": {"default": "Toronto"}},
    }, {
        "id": 2026020013, "season": 20262027, "gameType": 3, "gameState": "FUT", "gameScheduleState": "OK",
        "startTimeUTC": "2026-10-27T01:00:00Z",
        "homeTeam": {"abbrev": "CGY", "placeName": {"default": "Calgary"}},
        "awayTeam": {"abbrev": "TOR", "placeName": {"default": "Toronto"}},
    }, {
        "id": 2026020014, "season": 20262027, "gameType": 2, "gameState": "PRE", "gameScheduleState": "OK",
        "startTimeUTC": "2026-10-27T01:30:00Z",
        "homeTeam": {"abbrev": "CGY", "placeName": {"default": "Calgary"}},
        "awayTeam": {"abbrev": "TOR", "placeName": {"default": "Toronto"}},
    }]}]}
    fixtures = fetch_nhl_fixtures(now=NOW, transport=lambda _url: payload)
    assert len(fixtures) == 2
    assert fixtures[0].game_id == "nhl-schedule:2026020012"
    assert (fixtures[0].home_team, fixtures[0].away_team) == ("CGY", "TOR")
    assert fixtures[1].game_id == "nhl-schedule:2026020014"


def test_synthetic_certified_provider_to_ntfy_lifecycle_is_deduplicated(monkeypatch, tmp_path):
    from dataclasses import replace as dc_replace
    from types import SimpleNamespace
    from parallax import nba_nhl
    from parallax.alerts import AlertConfig, AlertDeliveryStore, AlertDispatcher, DeliveryResult
    from parallax.models import Evidence
    from parallax.normalization import rules_digest

    class Transport:
        calls = []
        def post_json(self, url, payload):
            self.calls.append((url, payload))
            return DeliveryResult("SENT", http_status=200)

    transport = Transport()
    dispatcher = AlertDispatcher(AlertDeliveryStore(tmp_path / "synthetic-alerts.sqlite"),
        AlertConfig(mode="ntfy", ntfy_topic="synthetic-topic", ntfy_server="https://ntfy.example.test"), transport)
    fx = fixture()
    raw = {**MARKETS["NBA"], "status": "active", "market_type": "binary",
           "price_level_structure": "linear_cent", "notional_value_dollars": "1",
           "expected_expiration_time": "2026-10-27T05:00:00Z"}
    class Book:
        def book(self, _ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.48", "250"]],
                                     "no_dollars": [["0.50", "250"]]}}
    def synthetic_evidence(market, fixture, forecast, *, now):
        return Evidence(venue=market.venue, market_id=market.venue_market_id,
            fair_probability=.80, source="SYNTHETIC_TEST_PROVIDER", model_version="nba-synthetic-certified-v1",
            observed_at=now.isoformat(), valid_until=(now + timedelta(minutes=5)).isoformat(),
            rules_digest=rules_digest(market), review_reference="SYNTHETIC_TEST_ONLY",
            rationale="Synthetic fixture only", independent_sources=("synthetic-a", "synthetic-b"),
            validation_reference="SYNTHETIC_TEST_ONLY", validation_status="CALIBRATED",
            source_independence="INDEPENDENT")
    monkeypatch.setattr(nba_nhl, "shadow_evidence", synthetic_evidence)
    monkeypatch.setattr(nba_nhl, "attach_fees", lambda market, _now, **_kw: dc_replace(market,
        mechanics=dc_replace(market.mechanics, fee_rate=.001, fee_source="SYNTHETIC_TEST_ONLY",
            fee_valid_until="2026-10-27T00:00:00+00:00", fee_status="VERIFIED_SCHEDULE")))
    first = scan_league("NBA", [fx], [], [raw], [], kalshi_client=Book(), alert_dispatcher=dispatcher, now=NOW)
    second = scan_league("NBA", [fx], [], [raw], [], kalshi_client=Book(), alert_dispatcher=dispatcher, now=NOW)
    assert any(row["publication_eligible"] for row in first["rows"])
    assert first["alerts"] == 1 and second["alerts"] == 0
    assert len(transport.calls) == 1
    assert transport.calls[0][1]["topic"] == "synthetic-topic"


def test_production_environment_flag_cannot_enable_synthetic_certification():
    code = '''
from datetime import UTC, datetime
from types import SimpleNamespace
from parallax.alerts import dispatch_scored_buy
from parallax.public_feed import _league_buy_eligible
now=datetime(2026,10,24,12,0,tzinfo=UTC)
row={"publication_eligible":True,"suggested_action":"BUY","mapping_status":"MAPPED","acquisition_status":"ACQUIRED","market_status":"OPEN","data_freshness":"FRESH","status":"CURRENT","failed_gates":[],"fee_status":"VERIFIED_SCHEDULE","evidence_validation_status":"CALIBRATED","evidence_validation_reference":"SYNTHETIC_TEST_ONLY","evidence_source":"synthetic","evidence_model_version":"nba-synthetic-v1","venue":"KALSHI","market_id":"synthetic","side":"YES","game_id":"g","selected_team":"H","home_team":"H","away_team":"A","executable_price":.5,"model_probability":.7,"fee":.1,"raw_edge":20.,"liquidity":50.,"net_ev_25":4.,"expected_return":.1,"updated_at":"2026-10-24T11:00:00Z","expires_at":"2026-10-24T13:00:00Z","game_start":"2026-10-25T00:00:00Z"}
e=SimpleNamespace(validation_status="CALIBRATED",validation_reference="SYNTHETIC_TEST_ONLY",model_version="nba-synthetic-v1",source_independence="INDEPENDENT")
play=SimpleNamespace(suggested_action="BUY",evidence=e,verdict=SimpleNamespace(failed_gates=()),data_freshness="FRESH",status="CURRENT")
market=SimpleNamespace(status="OPEN",mechanics=SimpleNamespace(fee_status="VERIFIED_SCHEDULE"))
try: alert=dispatch_scored_buy(None,play,market,sport="NBA",matchup="synthetic",detected_at="2026-10-24T11:00:00Z")
except Exception: alert="DISPATCH_ATTEMPTED"
print(_league_buy_eligible(row,now),alert)
'''
    env = dict(os.environ, PYTEST_CURRENT_TEST="synthetic-runtime-bypass-probe")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True)
    assert result.stdout.strip() == "False None"


def test_nba_provider_query_must_be_verified():
    with pytest.raises(ValueError, match="Verified NBA regular-season query"):
        nba_fixture_from_balldontlie(
            NBA_BDL_GAME, venue_home_name="Denver",
            venue_away_name="Golden State",
        )


@pytest.mark.parametrize("changes", [
    {"id": None},
    {"season": "2026"},
    {"date": "2026-10-27"},
    {"datetime": "2026-10-26T02:00:00.000Z"},  # Oct 25 ET conflicts with Oct 26 date
    {"datetime": None},
    {"datetime": "2026-10-27T02:00:00"},
    {"status_state": "final"},
    {"status_state": "postponed"},
    {"postseason": True},
    {"postponed": True},
    {"season_type": "playoffs"},
    {"ist_stage": "Championship"},
    {"visitor_team": {"abbreviation": ""}},
])
def test_nba_provider_bad_identity_or_status_fails_closed(changes):
    game = dict(NBA_BDL_GAME, **changes)
    with pytest.raises(ValueError):
        nba_fixture_from_balldontlie(
            game, regular_season_query_verified=True,
            venue_home_name="Denver", venue_away_name="Golden State",
        )


def test_provider_game_team_disagreement_is_rejected_at_market_mapping():
    # A different valid home-team code is not intrinsically malformed.
    # The correct FAIL-CLOSED gate is independent Kalshi market matching.
    wrong_game = dict(NBA_BDL_GAME, home_team={"abbreviation": "BOS"})
    fx = nba_fixture_from_balldontlie(
        wrong_game, regular_season_query_verified=True,
        venue_home_name="Denver", venue_away_name="Golden State",
    )
    prediction = predict_future(
        [], sport=fx.sport, game_date=fx.kickoff.astimezone(_ET).date(),
        home_team=fx.home_team, away_team=fx.away_team,
        game_id=fx.game_id, season=fx.season,
    )
    assert shadow_evidence(kalshi(), fx, prediction, now=NOW) is None


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


def test_observed_pmus_nba_moneyline_maps_to_experimental_evidence():
    market = pmus()
    e = shadow_evidence(market, fixture(), forecast(), now=NOW)
    assert e is not None
    assert e.market_id == "1386496"
    assert e.fair_probability == pytest.approx(.90)
    assert e.validation_status == "UNVALIDATED"
    assert e.validation_reference == ""
    for side in (Side.YES, Side.NO):
        play = evaluate_shadow(market, fixture(), forecast(), side=side, now=NOW)
        assert play is not None
        assert play.suggested_action != Action.BUY


@pytest.mark.parametrize("changes", [
    {"marketType": "futures"},
    {"id": 123},
    {"slug": "aec-nba-gs-lal-2026-10-26"},
    {"status": "MARKET_STATUS_CLOSED"},
    {"gameStartTime": None},
    {"gameStartTime": "2026-10-27T03:00:00+00:00"},
    {"description": PMUS_NBA_DESCRIPTION.replace("Oct 26", "Oct 27")},
    {"description": PMUS_NBA_DESCRIPTION.replace("10:00PM", "9:00PM")},
    {"description": PMUS_NBA_DESCRIPTION.replace("Overtime is included if played.", "Regulation only.")},
    {"description": PMUS_NBA_DESCRIPTION.replace("GS Warriors", "GS Valkyries")},
])
def test_observed_pmus_nba_contract_conflicts_fail_closed(changes):
    assert shadow_evidence(pmus(**changes), fixture(), forecast(), now=NOW) is None


@pytest.mark.parametrize("team_change", [
    {"abbreviation": "gsw"},
    {"league": "wnba"},
    {"ordering": "home"},
    {"name": "Valkyries"},
])
def test_pmus_wrong_away_team_identity_fails_closed(team_change):
    market = pmus()
    raw = dict(market.original_metadata["market"])
    sides = [dict(s) for s in raw["marketSides"]]
    sides[0]["team"] = dict(sides[0]["team"], **team_change)
    raw["marketSides"] = sides
    assert shadow_evidence(replace(market, original_metadata={"market": raw}),
                           fixture(), forecast(), now=NOW) is None


def test_pmus_nhl_remains_unverified_and_blocked():
    assert shadow_evidence(pmus(), fixture("NHL"), forecast("NHL"), now=NOW) is None


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
