#!/usr/bin/env python3
"""Read-only NBA Kalshi fee metadata preflight. NEVER authorizes BUY.

Checks the exact KXNBAGAME market/event/series binding and the maker/taker
fee metadata required before a separate human-reviewed fee-policy decision.
The official fee schedule MUST be reviewed independently: API metadata alone
cannot establish the governing published trading fees.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

SERIES_TICKER = "KXNBAGAME"
SCHEDULE_URL = "https://kalshi.com/fee-schedule"
SERIES_URL = "https://api.elections.kalshi.com/trade-api/v2/series/KXNBAGAME"
ELIGIBLE_FEE_TYPES = {"quadratic", "quadratic_with_maker_fees"}


def _num(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def inspect(market: dict, event: dict, series: dict, *, observed_at: str, now: datetime) -> dict:
    """Fail closed on ambiguous identity, stale evidence and fee overrides."""
    reasons = []
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None or now.tzinfo is None:
            raise ValueError("timezone missing")
        age = (now - observed).total_seconds()
    except (ValueError, TypeError, AttributeError):
        age = float("inf")
    if not 0 <= age < 60:
        reasons.append("METADATA_STALE_OR_INVALID")
    if not all(isinstance(v, dict) for v in (market, event, series)):
        raise ValueError("market, event and series must be JSON objects")
    ticker = market.get("ticker")
    event_ticker = market.get("event_ticker")
    if not isinstance(ticker, str) or not ticker.startswith(SERIES_TICKER + "-"):
        reasons.append("NOT_NBA_GAME_MARKET")
    if (not isinstance(event_ticker, str)
            or not ticker.startswith(event_ticker + "-") if isinstance(ticker, str) and isinstance(event_ticker, str)
            else True):
        reasons.append("MARKET_EVENT_IDENTITY_MISMATCH")
    if not event_ticker or event.get("event_ticker") != event_ticker:
        reasons.append("EVENT_MISMATCH")
    if event.get("series_ticker") != SERIES_TICKER or series.get("ticker") != SERIES_TICKER:
        reasons.append("SERIES_MISMATCH")
    effective_kind = event.get("fee_type_override")
    if effective_kind is None:
        effective_kind = series.get("fee_type")
    if effective_kind not in ELIGIBLE_FEE_TYPES:
        reasons.append("UNSUPPORTED_FEE_TYPE")
    multiplier = event.get("fee_multiplier_override")
    if multiplier is None:
        multiplier = series.get("fee_multiplier")
    m = _num(multiplier)
    if m is None or m < 0 or m > 10:
        reasons.append("INVALID_FEE_MULTIPLIER")
    elif m == 0:
        reasons.append("ZERO_FEE_REQUIRES_SPECIFIC_SCHEDULE_PROOF")
    unexpected = [key for key, value in market.items()
                  if key.startswith("fee_") and key != "fee_waiver_expiration_time"
                  and value is not None]
    if unexpected or market.get("fee_waiver_expiration_time") is not None:
        reasons.append("UNVERIFIED_MARKET_FEE_OVERRIDE")
    # The published Kalshi fee schedule and clearing/rounding regime have not
    # been independently authenticated for this specific market by this tool.
    return {
        "sport": "NBA", "series": SERIES_TICKER, "market_id": ticker,
        "event_id": event_ticker, "observed_at": observed_at,
        "schedule_source": SCHEDULE_URL, "series_metadata_source": SERIES_URL,
        "effective_fee_type": effective_kind if effective_kind in ELIGIBLE_FEE_TYPES else None,
        "effective_fee_multiplier": m,
        "metadata_consistent": not reasons,
        "blockers": sorted(reasons),
        "published_schedule_verified": False,
        "rounding_account_verified": False,
        "status": "METADATA_CHECK_ONLY_NOT_BUY_AUTHORIZED",
        "buy_authorized": False, "publication_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True,
                        help="Local JSON with market/event/series/observed_at, no credentials")
    args = parser.parse_args()
    if args.input.is_symlink() or args.input.stat().st_size > 1000000:
        parser.error("input is symlink or too large")
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        parser.error("input must be an object")
    print(json.dumps(inspect(raw["market"], raw["event"], raw["series"],
                             observed_at=raw["observed_at"],
                             now=datetime.now(timezone.utc)), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
