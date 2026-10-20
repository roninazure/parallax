"""Offline regression: no fee-inclusive NBA scenario may become a BUY."""
import copy

import pytest

from scripts.nba_shadow_net_scenarios_offline import (
    checked_rates, one_fill_scenario, scenario_report,
)


def audit():
    return {
        "research_only": True, "buy_authorized": False,
        "fee_certification": "PENDING_MARKET_BOUND_REVIEW",
        "kalshi": {
            "ticker": "KXNBAGAME-26OCT26GSWDEN-GSW",
            "event": "KXNBAGAME-26OCT26GSWDEN",
            "series": "KXNBAGAME",
            "series_fee_type": "quadratic_with_maker_fees",
            "series_fee_multiplier": 1,
            "event_fee_type_override": None,
            "event_fee_multiplier_override": None,
            "market_fee_related_fields": {},
        },
        "polymarket_us": {
            "slug": "aec-nba-gs-den-2026-10-26",
            "market_feeCoefficient_raw": .0695,
            "documented_standard_taker_theta_oct19_2026": .0695,
            "rebate_tier_verified": False,
        },
    }


def base_snapshot():
    def row(side, fair, bid, ask, depth):
        return {
            "outcome": "GSW" if side == "YES" else "DEN",
            "fair_probability_experimental": fair,
            "bid": bid, "ask": ask,
            "spread": round(ask - bid, 6),
            "available_at_ask": depth,
            "nominal_edge_before_costs": round(fair - ask, 6),
            "net_edge_after_fees": None, "buy_authorized": False,
            "fee_status": "UNVERIFIED",
            "shadow_action": "WATCH" if side == "NO" else "PASS",
        }
    return {
        "research_only": True, "buy_authorized": False,
        "fee_calculation": "UNAVAILABLE_EXPIRED_REVIEW",
        "fixture": {
            "game_id": "bdl:21717970", "home": "DEN", "away": "GSW",
            "sport": "NBA", "tipoff_utc": "2026-10-27T02:00:00+00:00",
        },
        "venues": {
            "KALSHI": {
                "market_id": "KXNBAGAME-26OCT26GSWDEN-GSW",
                "observed_at_utc": "2026-10-20T13:37:19+00:00",
                "status": "OPEN", "fee_review_status": "UNVERIFIED",
                "sides": {
                    "YES": row("YES", .173345, .19, .48, 250),
                    "NO": row("NO", .826655, .52, .81, 1),
                },
            },
            "POLYMARKET_US": {
                "market_id": "1386496",
                "observed_at_utc": "2026-10-20T13:37:21+00:00",
                "status": "OPEN", "fee_review_status": "UNVERIFIED",
                "sides": {
                    "YES": row("YES", .173345, .11, .54, 5),
                    "NO": row("NO", .826655, .46, .89, 272),
                },
            },
        },
    }


def test_snapshot_fee_scenario_cannot_certify_a_buy():
    result = scenario_report(base_snapshot(), audit())
    assert result["buy_authorized"] is False
    assert result["status"] == "HISTORICAL_HYPOTHETICAL_NOT_CERTIFIED"
    outcomes = [
        detail for venue in result["venues"].values()
        for detail in venue.values()
    ]
    assert all(outcome["buy_authorized"] is False for outcome in outcomes)
    assert all(outcome["commercial_screen_blockers"] for outcome in outcomes)


def test_one_contract_kalshi_denver_loses_nominal_edge_under_cent_rounding():
    output = scenario_report(base_snapshot(), audit())["venues"]["KALSHI"]["NO"]
    assert output["quantity"] == 1
    assert output["fully_funded_25usd_scenario"] is False
    assert output["estimated_fee_usd"] == pytest.approx(.02)
    assert output["experimental_expected_value_usd"] == pytest.approx(-.0033)
    assert output["buy_authorized"] is False


def test_pmus_no_rebate_fee_is_computed_from_observed_ask():
    output = scenario_report(base_snapshot(), audit())["venues"]["POLYMARKET_US"]["NO"]
    assert output["quantity"] == 28
    assert output["fully_funded_25usd_scenario"] is True
    assert output["estimated_fee_usd"] == pytest.approx(.19)
    assert output["experimental_expected_value_usd"] < 0


@pytest.mark.parametrize("section,key,value", [
    ("kalshi", "series", "KXNFLGAME"),
    ("kalshi", "series_fee_multiplier", .5),
    ("kalshi", "series_fee_type", "flat"),
    ("kalshi", "event_fee_type_override", "custom"),
    ("polymarket_us", "market_feeCoefficient_raw", .05),
    ("polymarket_us", "rebate_tier_verified", True),
])
def test_unverified_fee_metadata_change_rejected(section, key, value):
    data = copy.deepcopy(audit())
    data[section][key] = value
    with pytest.raises(ValueError, match="STOP"):
        checked_rates(base_snapshot(), data)


def test_unknown_market_id_fails_closed():
    data = base_snapshot()
    data["venues"]["POLYMARKET_US"]["market_id"] = "invalid"
    with pytest.raises(ValueError, match="STOP"):
        checked_rates(data, audit())
