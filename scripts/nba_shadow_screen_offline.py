#!/usr/bin/env python3
"""Read-only commercial gate report from an EXISTING NBA dual-venue JSON snapshot.

No network requests, no orders, no odds polling, no production imports.
Never certifies BUY or returns a net positive edge with expired fees/model.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

MIN_EDGE = 0.05
MAX_SPREAD = 0.05
RETAIL_STAKE = Decimal("25")
EXPECTED_VENUES = ("KALSHI", "POLYMARKET_US")
EXPECTED_OUTCOMES = {"YES": "GSW", "NO": "DEN"}


def _finite(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Missing or invalid {name}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Nonfinite {name}")
    return number


def screen_quote(quote: dict, *, side: str) -> dict:
    if quote.get("outcome") != EXPECTED_OUTCOMES[side]:
        raise ValueError(f"STOP: expected {side}={EXPECTED_OUTCOMES[side]}")
    p = _finite(quote.get("fair_probability_experimental"), name="fair probability")
    bid = _finite(quote.get("bid"), name="bid")
    ask = _finite(quote.get("ask"), name="ask")
    depth = _finite(quote.get("available_at_ask"), name="ask depth")
    if not (0 < p < 1 and 0 < bid < ask < 1 and depth >= 0):
        raise ValueError("STOP: invalid probability/book/size")
    spread = ask - bid
    nominal_edge = p - ask
    if abs(spread - _finite(quote.get("spread"), name="spread")) > 1e-5:
        raise ValueError("STOP: snapshot spread mismatch")
    if abs(nominal_edge - _finite(
        quote.get("nominal_edge_before_costs"), name="nominal edge"
    )) > 1e-5:
        raise ValueError("STOP: snapshot edge mismatch")
    if quote.get("net_edge_after_fees") is not None or quote.get("buy_authorized") is not False:
        raise ValueError("STOP: prequalified or unknown BUY/net edge from input")
    if quote.get("fee_status") != "UNVERIFIED":
        raise ValueError("STOP: unexpected fee-review status")
    if quote.get("shadow_action") == "BUY":
        raise ValueError("STOP: shadow BUY must never be present")
    required_for_stake = int(
        (RETAIL_STAKE / Decimal(str(ask))).to_integral_value(rounding=ROUND_FLOOR)
    )
    spread_ok = spread <= MAX_SPREAD + 1e-9
    edge_ok = nominal_edge >= MIN_EDGE - 1e-9
    depth_ok = depth >= required_for_stake and required_for_stake > 0
    blockers = []
    if not spread_ok:
        blockers.append("SPREAD_GT_5_CENTS")
    if not edge_ok:
        blockers.append("GROSS_EDGE_LT_5_POINTS")
    if not depth_ok:
        blockers.append("INSUFFICIENT_DEPTH_FOR_25_USD")
    blockers += ["FEES_NOT_VERIFIED", "MODEL_NOT_CALIBRATED"]
    return {
        "outcome": quote["outcome"],
        "ask": ask,
        "bid": bid,
        "spread": round(spread, 6),
        "top_of_book_ask_contracts": depth,
        "experimental_nominal_edge": round(nominal_edge, 6),
        "contracts_for_25_usd_top_ask_scenario": required_for_stake,
        "quoted_top_ask_notional_usd": round(depth * ask, 2),
        "price_spread_gate": spread_ok,
        "nominal_edge_gate": edge_ok,
        "retail_depth_gate": depth_ok,
        "research_gates_all_passed": spread_ok and edge_ok and depth_ok,
        "blockers": blockers,
        "net_edge_after_fees": None,
        "eligible_for_buy": False,
    }


def screen_snapshot(snapshot: dict) -> dict:
    if snapshot.get("research_only") is not True or snapshot.get("buy_authorized") is not False:
        raise ValueError("STOP: not an explicitly read-only shadow snapshot")
    if snapshot.get("fee_calculation") != "UNAVAILABLE_EXPIRED_REVIEW":
        raise ValueError("STOP: unrecognized fee-assessment status")
    fixture = snapshot.get("fixture") or {}
    if (fixture.get("game_id") != "bdl:21717970" or
        fixture.get("home") != "DEN" or
        fixture.get("away") != "GSW" or
        fixture.get("sport") != "NBA" or
        fixture.get("tipoff_utc") != "2026-10-27T02:00:00+00:00"):
        raise ValueError("STOP: unexpected fixture")
    venue_data = snapshot.get("venues")
    if not isinstance(venue_data, dict) or set(venue_data) != set(EXPECTED_VENUES):
        raise ValueError("STOP: both verified venues required")
    per_venue = {}
    for name in EXPECTED_VENUES:
        venue = venue_data[name]
        if venue.get("status") != "OPEN" or venue.get("fee_review_status") != "UNVERIFIED":
            raise ValueError(f"STOP: invalid status/fee review for {name}")
        when = datetime.fromisoformat(venue["observed_at_utc"].replace("Z", "+00:00"))
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("STOP: missing timestamp timezone")
        rows = venue.get("sides")
        if not isinstance(rows, dict) or set(rows) != set(EXPECTED_OUTCOMES):
            raise ValueError("STOP: missing YES/NO outcome")
        per_venue[name] = {
            "observed_at_utc": when.astimezone(UTC).isoformat(),
            "sides": {
                side: screen_quote(rows[side], side=side)
                for side in EXPECTED_OUTCOMES
            },
        }
    times = [
        datetime.fromisoformat(v["observed_at_utc"])
        for v in per_venue.values()
    ]
    lag = abs((times[0] - times[1]).total_seconds())
    return {
        "research_only": True,
        "buy_authorized": False,
        "fee_and_model_gate": "BLOCKED",
        "cross_venue_observation_lag_seconds": round(lag, 2),
        "warning": "Historical quotes, NOT current prices. Gross edges only; no certified net edge.",
        "venues": per_venue,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="Existing saved two-venue JSON")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    report = screen_snapshot(snapshot)
    print("OFFLINE / RESEARCH ONLY / NO BUY")
    print("Original snapshot:", args.snapshot)
    print("Venue capture lag (seconds):", report["cross_venue_observation_lag_seconds"])
    for venue, detail in report["venues"].items():
        for side, quote in detail["sides"].items():
            print(
                f'{venue:14s} {side} {quote["outcome"]}: '
                f'ask={quote["ask"]:.2f} '
                f'edge={quote["experimental_nominal_edge"]:+.4f} '
                f'spread={quote["spread"]:.2f} '
                f'ask_depth={quote["top_of_book_ask_contracts"]:g}/'
                f'{quote["contracts_for_25_usd_top_ask_scenario"]} '
                f'blockers={",".join(quote["blockers"])}'
            )
    print(report["warning"])


if __name__ == "__main__":
    main()
