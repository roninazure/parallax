#!/usr/bin/env python3
"""Bounded, read-only NBA contract fee METADATA audit (not fee certification).

3 Kalshi GETs: market, event, series. 1 PMUS exact slug discovery.
Never uses credentials, publishes plays, retries, or modifies production fees.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from scripts.nba_dual_venue_shadow_snapshot import TICKER, SLUG, kalshi_json

KALSHI_ROOT = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_EVENT = "KXNBAGAME-26OCT26GSWDEN"
KALSHI_SERIES = "KXNBAGAME"
PMUS_SCHEDULE = "https://docs.polymarket.us/fees"
KALSHI_SCHEDULE = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"


def verify_metadata(kalshi_market: dict, kalshi_event: dict,
                    kalshi_series: dict, pmus: dict) -> dict:
    if (kalshi_market.get("ticker") != TICKER
            or kalshi_market.get("event_ticker") != KALSHI_EVENT
            or kalshi_event.get("event_ticker") != KALSHI_EVENT
            or kalshi_event.get("series_ticker") != KALSHI_SERIES
            or kalshi_series.get("ticker") != KALSHI_SERIES
            or str(pmus.get("id")) != "1386496"
            or pmus.get("slug") != SLUG
            or pmus.get("marketType") != "moneyline"):
        raise ValueError("STOP: fee audit market, event or series identity mismatch")
    if (kalshi_market.get("status") not in {"active", "open"}
            or pmus.get("status") != "MARKET_STATUS_OPEN"):
        raise ValueError("STOP: market no longer open")
    return {
        "research_only": True,
        "fee_certification": "PENDING_MARKET_BOUND_REVIEW",
        "source_schedules": {
            "kalshi": KALSHI_SCHEDULE,
            "polymarket_us": PMUS_SCHEDULE,
        },
        "kalshi": {
            "ticker": TICKER,
            "event": KALSHI_EVENT,
            "series": KALSHI_SERIES,
            "series_fee_type": kalshi_series.get("fee_type"),
            "series_fee_multiplier": kalshi_series.get("fee_multiplier"),
            "event_fee_type_override": kalshi_event.get("fee_type_override"),
            "event_fee_multiplier_override": kalshi_event.get("fee_multiplier_override"),
            "market_fee_related_fields": {
                k: v for k, v in kalshi_market.items()
                if k.startswith("fee_")
            },
        },
        "polymarket_us": {
            "slug": SLUG,
            "market_feeCoefficient_raw": pmus.get("feeCoefficient"),
            "documented_standard_taker_theta_oct19_2026": 0.0695,
            "rebate_tier_verified": False,
        },
        "net_edge_after_fees": None,
        "buy_authorized": False,
    }


def main() -> None:
    market = kalshi_json(f"{KALSHI_ROOT}/markets/{TICKER}").get("market")
    event = kalshi_json(f"{KALSHI_ROOT}/events/{KALSHI_EVENT}").get("event")
    series = kalshi_json(f"{KALSHI_ROOT}/series/{KALSHI_SERIES}").get("series")
    if not all(isinstance(obj, dict) for obj in (market, event, series)):
        raise SystemExit("STOP: Kalshi fee metadata response missing")
    client = PolymarketUSPublicClient(max_requests_per_minute=1)
    try:
        rows = client.markets_page(
            limit=5, offset=0, slugs=[SLUG], retry_transport_errors=False
        )
    finally:
        client.close()
    if len(rows) != 1 or rows[0]["slug"] != SLUG:
        raise SystemExit("STOP: exact Polymarket contract not found")
    report = verify_metadata(market, event, series, rows[0]["raw"])
    report["observed_at_utc"] = datetime.now(UTC).isoformat()
    output_dir = Path.home() / "parallax-research/nba/forward"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"fee-metadata-gsw-den-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    print("SAVED:", path)
    print("Research metadata only; effective net fees and BUY remain unverified")


if __name__ == "__main__":
    main()
