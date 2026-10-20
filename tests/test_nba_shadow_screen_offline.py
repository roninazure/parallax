"""Offline-only commercial gates for experimental NBA two-venue snapshots."""
import copy

import pytest

from scripts.nba_shadow_screen_offline import screen_quote, screen_snapshot


def sample_quote(*, yes: bool, market: str) -> dict:
    data = {
        ("kalshi", True): ("GSW", .173345, .19, .48, 250, -.306655),
        ("kalshi", False): ("DEN", .826655, .52, .81, 1, .016655),
        ("pmus", True): ("GSW", .173345, .11, .54, 5, -.366655),
        ("pmus", False): ("DEN", .826655, .46, .89, 272, -.063345),
    }
    team, fair, bid, ask, depth, edge = data[(market, yes)]
    return {
        "outcome": team,
        "fair_probability_experimental": fair,
        "bid": bid, "ask": ask,
        "spread": round(ask - bid, 6),
        "available_at_ask": depth,
        "nominal_edge_before_costs": edge,
        "fee_status": "UNVERIFIED",
        "net_edge_after_fees": None,
        "shadow_action": "WATCH" if market == "kalshi" and not yes else "PASS",
        "buy_authorized": False,
    }


def sample_snapshot() -> dict:
    return {
        "research_only": True,
        "buy_authorized": False,
        "fee_calculation": "UNAVAILABLE_EXPIRED_REVIEW",
        "fixture": {
            "game_id": "bdl:21717970", "home": "DEN", "away": "GSW",
            "sport": "NBA", "tipoff_utc": "2026-10-27T02:00:00+00:00",
        },
        "venues": {
            "KALSHI": {
                "observed_at_utc": "2026-10-20T13:37:19+00:00",
                "status": "OPEN", "fee_review_status": "UNVERIFIED",
                "sides": {
                    "YES": sample_quote(yes=True, market="kalshi"),
                    "NO": sample_quote(yes=False, market="kalshi"),
                },
            },
            "POLYMARKET_US": {
                "observed_at_utc": "2026-10-20T13:37:24+00:00",
                "status": "OPEN", "fee_review_status": "UNVERIFIED",
                "sides": {
                    "YES": sample_quote(yes=True, market="pmus"),
                    "NO": sample_quote(yes=False, market="pmus"),
                },
            },
        },
    }


def test_report_blocks_all_four_2026_10_20_market_sides():
    report = screen_snapshot(sample_snapshot())
    assert report["buy_authorized"] is False
    assert report["cross_venue_observation_lag_seconds"] == 5
    for venue in report["venues"].values():
        for side in venue["sides"].values():
            assert side["eligible_for_buy"] is False
            assert "FEES_NOT_VERIFIED" in side["blockers"]
            assert "MODEL_NOT_CALIBRATED" in side["blockers"]
            assert side["net_edge_after_fees"] is None
            assert side["price_spread_gate"] is False
    den = report["venues"]["KALSHI"]["sides"]["NO"]
    assert den["experimental_nominal_edge"] == pytest.approx(.016655)
    assert den["top_of_book_ask_contracts"] == 1
    assert den["contracts_for_25_usd_top_ask_scenario"] == 30
    assert "INSUFFICIENT_DEPTH_FOR_25_USD" in den["blockers"]


def test_all_three_market_gates_passing_still_cannot_authorize_buy():
    quote = sample_quote(yes=True, market="kalshi")
    quote.update(bid=.55, ask=.58, spread=.03,
                 fair_probability_experimental=.70,
                 nominal_edge_before_costs=.12,
                 available_at_ask=100)
    report = screen_quote(quote, side="YES")
    assert report["research_gates_all_passed"] is True
    assert report["eligible_for_buy"] is False
    assert report["blockers"] == ["FEES_NOT_VERIFIED", "MODEL_NOT_CALIBRATED"]


@pytest.mark.parametrize("key,value", [
    ("buy_authorized", True),
    ("net_edge_after_fees", .1),
    ("fee_status", "VERIFIED_UPPER_BOUND"),
    ("shadow_action", "BUY"),
    ("outcome", "LAL"),
    ("ask", 0.01),
])
def test_suspicious_or_inconsistent_quote_fails_closed(key, value):
    quote = sample_quote(yes=True, market="kalshi")
    quote[key] = value
    with pytest.raises(ValueError):
        screen_quote(quote, side="YES")


def test_market_identity_mismatch_fails_closed():
    data = sample_snapshot()
    data["fixture"]["away"] = "LAL"
    with pytest.raises(ValueError, match="unexpected fixture"):
        screen_snapshot(data)


def test_single_venue_snapshot_is_not_comparable():
    data = sample_snapshot()
    del data["venues"]["POLYMARKET_US"]
    with pytest.raises(ValueError, match="both verified venues"):
        screen_snapshot(data)


def test_explicitly_unverified_model_and_fee_statuses_required():
    data = sample_snapshot()
    data["fee_calculation"] = "VERIFIED"
    with pytest.raises(ValueError, match="fee-assessment"):
        screen_snapshot(data)
