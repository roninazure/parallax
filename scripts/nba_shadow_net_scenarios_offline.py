#!/usr/bin/env python3
"""Offline fee-inclusive NBA research SCENARIOS from saved quote + fee audit.

Not trade advice, not certified economics, not a production fee-policy update.
No network/provider requests. One hypothetical taker fill at recorded ask.
Historical prices may no longer be executable. Conservative Kalshi non-direct
$0.01 balance precision; PMUS assumes no taker rebate and whole contracts.
"""
from __future__ import annotations

import argparse
import json
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from parallax.economics import kalshi_fill_fees, pmus_fill_fees
from scripts.nba_shadow_screen_offline import screen_snapshot

EXPECTED_TICKER = "KXNBAGAME-26OCT26GSWDEN-GSW"
EXPECTED_SLUG = "aec-nba-gs-den-2026-10-26"
STAKE = Decimal("25")


def checked_rates(snapshot: dict, audit: dict) -> dict[str, float]:
    if audit.get("research_only") is not True or audit.get("buy_authorized") is not False:
        raise ValueError("STOP: audit lacks research-only safeguards")
    if audit.get("fee_certification") != "PENDING_MARKET_BOUND_REVIEW":
        raise ValueError("STOP: unsupported fee audit status")
    k = audit.get("kalshi") or {}
    p = audit.get("polymarket_us") or {}
    if (k.get("ticker") != EXPECTED_TICKER
            or k.get("event") != "KXNBAGAME-26OCT26GSWDEN"
            or k.get("series") != "KXNBAGAME"
            or k.get("series_fee_type") != "quadratic_with_maker_fees"
            or k.get("series_fee_multiplier") != 1
            or k.get("event_fee_type_override") is not None
            or k.get("event_fee_multiplier_override") is not None
            or k.get("market_fee_related_fields") != {}
            or p.get("slug") != EXPECTED_SLUG
            or p.get("rebate_tier_verified") is not False
            or Decimal(str(p.get("market_feeCoefficient_raw"))) != Decimal(".0695")
            or Decimal(str(p.get("documented_standard_taker_theta_oct19_2026"))) != Decimal(".0695")):
        raise ValueError("STOP: expected exact NBA event/fee conditions unavailable")
    venues = snapshot["venues"]
    if (venues["KALSHI"]["market_id"] != EXPECTED_TICKER
            or venues["POLYMARKET_US"]["market_id"] != "1386496"):
        raise ValueError("STOP: snapshot/fee audit contract identity differs")
    return {"KALSHI": .07, "POLYMARKET_US": .0695}


def one_fill_scenario(quote: dict, *, venue: str, rate: float) -> dict:
    ask = Decimal(str(quote["ask"]))
    fair = ask + Decimal(str(quote["experimental_nominal_edge"]))
    depth = Decimal(str(quote["top_of_book_ask_contracts"]))
    budget_count = int((STAKE / ask).to_integral_value(rounding=ROUND_FLOOR))
    available_count = int(depth.to_integral_value(rounding=ROUND_FLOOR))
    count = min(budget_count, available_count)
    if count < 1:
        return {
            "quantity": 0, "fill_possible": False,
            "estimated_fee_usd": None,
            "experimental_expected_value_usd": None,
            "experimental_expected_return": None,
            "fully_funded_25usd_scenario": False,
            "buy_authorized": False,
        }
    if venue == "KALSHI":
        fee = kalshi_fill_fees(
            [(float(count), float(ask))], rate,
            balance_precision=.01,
        )[0]
    elif venue == "POLYMARKET_US":
        fee = pmus_fill_fees([(float(count), float(ask))], theta=rate)[0]
    else:
        raise ValueError("Unsupported venue")
    entry = ask * count
    all_in = entry + fee
    ev = fair * count - all_in
    return {
        "quantity": count,
        "fill_possible": True,
        "fully_funded_25usd_scenario": count == budget_count,
        "entry_notional_usd": round(float(entry), 4),
        "estimated_fee_usd": round(float(fee), 4),
        "all_in_cost_usd": round(float(all_in), 4),
        "experimental_expected_value_usd": round(float(ev), 4),
        "experimental_expected_return": round(float(ev / all_in), 6),
        "fee_assumptions": (
            "one immediate taker fill, non-direct member $0.01 balance precision"
            if venue == "KALSHI"
            else "one immediate taker fill, no taker rebate, half-even rounding"
        ),
        "buy_authorized": False,
    }


def scenario_report(snapshot: dict, audit: dict) -> dict:
    screened = screen_snapshot(snapshot)
    rates = checked_rates(snapshot, audit)
    venues = {}
    for name, data in screened["venues"].items():
        venues[name] = {
            side: {
                **one_fill_scenario(
                    quote, venue=name, rate=rates[name]
                ),
                "commercial_screen_blockers": quote["blockers"],
            }
            for side, quote in data["sides"].items()
        }
    return {
        "research_only": True, "buy_authorized": False,
        "status": "HISTORICAL_HYPOTHETICAL_NOT_CERTIFIED",
        "warning": (
            "Last saved quotes only; experimental Elo fair probabilities; "
            "account-specific fees and executable fills not verified. "
            "Not actual P&L or a BUY signal."
        ),
        "venues": venues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("fee_audit", type=Path)
    args = parser.parse_args()
    snap = json.loads(args.snapshot.read_text(encoding="utf-8"))
    audit = json.loads(args.fee_audit.read_text(encoding="utf-8"))
    result = scenario_report(snap, audit)
    print("OFFLINE HYPOTHETICAL / NO BUY / NOT CURRENT PRICES")
    for name, venue in result["venues"].items():
        for side, scenario in venue.items():
            print(
                name, side,
                f'qty={scenario["quantity"]}',
                f'fee_usd={scenario["estimated_fee_usd"]}',
                f'expected_value_usd={scenario["experimental_expected_value_usd"]}',
                f'full_25usd_depth={scenario["fully_funded_25usd_scenario"]}',
                "blockers=" + ",".join(scenario["commercial_screen_blockers"]),
            )
    print(result["warning"])


if __name__ == "__main__":
    main()
