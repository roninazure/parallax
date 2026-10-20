"""Source-bound NBA opening-day quote screen: no network or order operations."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from parallax.league_elo_experimental import Forecast, MODEL_STATUS
from parallax.league_shadow_evidence import ShadowFixture, shadow_evidence
from parallax.models import Side
from parallax.normalization import normalize_kalshi, normalize_pmus
from scripts.nba_opening_day_identity_probe import GAMES
from scripts.nba_opening_day_quote_snapshot import (
    scenario, validate_receipt, verified_quote,
)

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)


def receipt():
    games = []
    for cfg, game_id, prob in zip(
        GAMES, ("bdl:21717856", "bdl:21717857"), (.687662, .597670)
    ):
        event = cfg["kalshi_event"]
        games.append({
            "matchup": cfg["label"], "bdl_game_id": game_id,
            "tipoff_utc": cfg["start"], "home_code": cfg["home"],
            "away_code": cfg["away"],
            "model_home_win_probability_experimental": prob,
            "kalshi": {
                "event": event,
                "markets": [
                    {"ticker": event + "-" + cfg["away"], "status": "active"},
                    {"ticker": event + "-" + cfg["home"], "status": "active"},
                ],
            },
            "polymarket_us": {
                "id": cfg["pmus_id"], "slug": cfg["pmus_slug"],
                "gameStartTime": cfg["start"].replace("+00:00", "Z"),
                "description": "This market will settle to the winner of the ...",
                "outcomes": [
                    {"long_yes": True, "ordering": "away",
                     "team_code": cfg["pmus_away"].lower()},
                    {"long_yes": False, "ordering": "home",
                     "team_code": cfg["pmus_home"].lower()},
                ],
            },
        })
    return {"research_only": True, "buy_authorized": False, "games": games}


def fixture(cfg, game_id):
    return ShadowFixture(
        sport="NBA", game_id=game_id, season=2026,
        kickoff=datetime.fromisoformat(cfg["start"]),
        home_team=cfg["home"], away_team=cfg["away"],
        venue_home_name=cfg["venue_home_name"],
        venue_away_name=cfg["venue_away_name"],
    )


def forecast(fx, home_probability):
    return Forecast(
        sport="NBA", game_id=fx.game_id, season=2026,
        game_date=fx.kickoff.astimezone(ZoneInfo("America/New_York")).date(),
        home_team=fx.home_team, away_team=fx.away_team,
        home_win_probability=home_probability,
        home_won=None, model_status=MODEL_STATUS,
    )


def pmus_market(cfg, *, changed=None):
    local_time = (
        "Oct 20, 2026 at 7:00PM" if cfg["label"] == "PHI@NYK"
        else "Oct 20, 2026 at 9:30PM"
    )
    away_name, home_name = (
        ("76ers", "Knicks") if cfg["label"] == "PHI@NYK"
        else ("Thunder", "Spurs")
    )
    desc = (
        f'This market will settle to the winner of the '
        f'{cfg["pmus_away"]} {away_name} vs {cfg["pmus_home"]} {home_name} '
        f'professional basketball game originally scheduled for '
        f'{local_time} ET. Overtime is included if played. '
        f'Outcome sourced from the relevant governing body.'
    )
    raw = {
        "id": int(cfg["pmus_id"]), "slug": cfg["pmus_slug"],
        "status": "MARKET_STATUS_OPEN", "marketType": "moneyline",
        "gameStartTime": cfg["start"].replace("+00:00", "Z"),
        "description": desc,
        "marketSides": [
            {"long": True, "description": away_name,
             "team": {"name": away_name, "abbreviation": cfg["pmus_away"].lower(),
                      "ordering": "away", "league": "nba"}},
            {"long": False, "description": home_name,
             "team": {"name": home_name, "abbreviation": cfg["pmus_home"].lower(),
                      "ordering": "home", "league": "nba"}},
        ],
    }
    if changed:
        raw.update(changed)
    wrapper = {
        "id": cfg["pmus_id"], "slug": cfg["pmus_slug"],
        "question": "NBA full-game winner", "event_id": cfg["pmus_id"],
        "active": True, "closed": False, "accepting_orders": True,
        "raw": raw,
    }
    return normalize_pmus(wrapper, {}, NOW.isoformat())


def kalshi_market(cfg, *, selected):
    event = cfg["kalshi_event"]
    names = {
        cfg["away"]: cfg["venue_away_name"],
        cfg["home"]: cfg["venue_home_name"],
    }
    name = names[selected]
    raw = {
        "ticker": event + "-" + selected, "event_ticker": event,
        "title": name + " wins", "yes_sub_title": name,
        "rules_primary": (
            f"If {name} wins the {cfg['venue_away_name']} vs "
            f"{cfg['venue_home_name']} Pro Basketball game originally "
            "scheduled for Oct 20, 2026, then the market resolves to Yes."
        ),
        "rules_secondary": "", "status": "active",
        "market_type": "binary", "notional_value_dollars": "1",
        "price_level_structure": "linear_cent",
    }
    book = {"orderbook_fp": {"yes_dollars": [["0.30", "100"]],
                             "no_dollars": [["0.50", "100"]]}}
    return normalize_kalshi(raw, book, NOW.isoformat())


def test_two_saved_opening_day_fixture_receipts_accepted():
    checked = validate_receipt(receipt(), now=NOW)
    assert [cfg["label"] for cfg, _ in checked] == ["PHI@NYK", "OKC@SAS"]


@pytest.mark.parametrize("change", [
    lambda d: d["games"].pop(),
    lambda d: d["games"][0].update(bdl_game_id="bdl:999"),
    lambda d: d["games"][0]["kalshi"]["markets"].pop(),
    lambda d: d["games"][0]["polymarket_us"]["outcomes"][0].update(team_code="bos"),
    lambda d: d["games"][0]["polymarket_us"]["outcomes"][0].update(long_yes=False),
    lambda d: d.update(buy_authorized=True),
])
def test_identity_receipt_mismatch_fails_closed(change):
    data = receipt()
    change(data)
    with pytest.raises(ValueError, match="STOP"):
        validate_receipt(data, now=NOW)


def test_no_quotes_requested_after_tipoff():
    with pytest.raises(ValueError, match="pregame bound"):
        validate_receipt(
            receipt(), now=datetime(2026, 10, 21, 2, tzinfo=UTC)
        )


@pytest.mark.parametrize("idx,home_prob", [(0, .687662), (1, .59767)])
def test_verified_pmus_nyk_and_sas_aliases_match_real_contract_structure(idx, home_prob):
    cfg = GAMES[idx]
    fx = fixture(cfg, ("bdl:21717856", "bdl:21717857")[idx])
    market = pmus_market(cfg)
    e = shadow_evidence(market, fx, forecast(fx, home_prob), now=NOW)
    assert e is not None
    assert e.fair_probability == pytest.approx(1-home_prob)
    assert e.validation_status == "UNVALIDATED"
    assert e.validation_reference == ""


@pytest.mark.parametrize("idx", [0, 1])
def test_pmus_outcome_code_mismatch_rejected(idx):
    cfg = GAMES[idx]
    fx = fixture(cfg, ("bdl:21717856", "bdl:21717857")[idx])
    raw = deepcopy(pmus_market(cfg).original_metadata["market"])
    raw["marketSides"][1]["team"]["abbreviation"] = "wrong"
    bad = pmus_market(cfg, changed={"marketSides": raw["marketSides"]})
    assert shadow_evidence(bad, fx, forecast(fx, .6), now=NOW) is None


@pytest.mark.parametrize("idx", [0, 1])
@pytest.mark.parametrize("team_side", ["home", "away"])
def test_both_team_yes_kalshi_contracts_bind_correctly(idx, team_side):
    cfg = GAMES[idx]
    fx = fixture(cfg, ("bdl:21717856", "bdl:21717857")[idx])
    model = forecast(fx, .6)
    selected = cfg[team_side]
    market = kalshi_market(cfg, selected=selected)
    e = shadow_evidence(market, fx, model, now=NOW)
    assert e is not None
    assert e.fair_probability == pytest.approx(.6 if team_side == "home" else .4)


def test_one_fill_25_usd_depth_and_fees_never_authorize_buy():
    fake = SimpleNamespace(
        yes_bid=.60, yes_ask=.65, no_bid=.35, no_ask=.40,
        mechanics=SimpleNamespace(minimum_quantity=1),
        executable_depth={Side.YES: ((.65, 2),),
                          Side.NO: ((.40, 200),)},
    )
    row = scenario(fake, side=Side.YES, fair=.72, venue="KALSHI")
    assert row["top_ask_contracts_for_25usd"] == 38
    assert row["available_at_ask"] == 2
    assert "INSUFFICIENT_TOP_ASK_DEPTH_FOR_25_USD" in row["blockers"]
    assert "ELO_UNCALIBRATED" in row["blockers"]
    assert row["eligible_for_buy"] is False
    assert row["hypothetical_one_fill"]["quantity"] == 2


def test_no_top_ask_or_bad_spread_is_not_a_qualified_candidate():
    fake = SimpleNamespace(
        yes_bid=.7, yes_ask=None, no_bid=None, no_ask=None,
        mechanics=SimpleNamespace(minimum_quantity=1),
        executable_depth={Side.YES: (), Side.NO: ()},
    )
    row = scenario(fake, side=Side.YES, fair=.9, venue="KALSHI")
    assert row["hypothetical_one_fill"] is None
    assert "SPREAD_MISSING_OR_GT_5_CENTS" in row["blockers"]
    assert row["eligible_for_buy"] is False
