#!/usr/bin/env python3
"""One-shot NBA Kalshi/Polymarket US read-only quote comparison.

Only the source-verified 2026-10-26 GSW@DEN regular-season fixture.
Four bounded public requests maximum: Kalshi market + book, PMUS exact
slug discovery + book. No loops/retries/orders/alerts/scheduler changes.
The Elo forecast is experimental; venue fees are not currently verified.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.fees import attach_fees
from parallax.league_elo_experimental import (
    MODEL_STATUS, load_final_games_csv, predict_future,
)
from parallax.league_shadow_evidence import ShadowFixture, evaluate_shadow, shadow_evidence
from parallax.models import Side
from parallax.normalization import normalize_kalshi, normalize_pmus, rules_digest

TICKER = "KXNBAGAME-26OCT26GSWDEN-GSW"
SLUG = "aec-nba-gs-den-2026-10-26"
BDL_ID = "bdl:21717970"
TIPOFF = datetime(2026, 10, 27, 2, tzinfo=UTC)


def kalshi_json(url: str) -> dict:
    # Single request. urllib does not automatically retry an HTTP 429.
    with urlopen(
        Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "PARALLAX-research-snapshot/0.1",
        }),
        timeout=12,
    ) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise ValueError("Unexpected Kalshi payload type")
    return body


def quote_row(market, evidence, fixture, forecast, side: Side, observed_at: datetime) -> dict:
    """Pure read-only assessment; net economics is UNKNOWN without current fees."""
    play = evaluate_shadow(
        market, fixture, forecast, side=side, now=observed_at
    )
    if play is None or evidence is None:
        raise ValueError("Unverified market/evidence identity")
    fair = (
        evidence.fair_probability if side == Side.YES
        else 1.0 - evidence.fair_probability
    )
    bid = market.yes_bid if side == Side.YES else market.no_bid
    ask = market.yes_ask if side == Side.YES else market.no_ask
    depth = market.executable_depth.get(side, ())
    ask_size = (
        sum(q for p, q in depth if p == ask and q > 0)
        if ask is not None else 0
    )
    return {
        "outcome": "GSW" if side == Side.YES else "DEN",
        "fair_probability_experimental": round(fair, 6),
        "bid": bid,
        "ask": ask,
        "spread": round(ask - bid, 6) if bid is not None and ask is not None else None,
        "available_at_ask": ask_size,
        "nominal_edge_before_costs": round(fair - ask, 6) if ask is not None else None,
        "fee_status": market.mechanics.fee_status,
        "net_edge_after_fees": None,
        "shadow_action": str(play.suggested_action),
        "failed_gates": list(play.verdict.failed_gates),
        "buy_authorized": False,
    }


def venue_report(market, fixture, forecast, observed_at: datetime) -> dict:
    if market.status != "OPEN":
        raise ValueError(f"STOP: market not open ({market.status})")
    evidence = shadow_evidence(
        market, fixture, forecast, now=observed_at
    )
    if evidence is None:
        raise ValueError("STOP: contract/fixture/rules identity failed")
    if evidence.validation_status != "UNVALIDATED" or evidence.validation_reference:
        raise ValueError("STOP: unexpected research validation status")
    # Never re-use a stale fee schedule to assert a net positive edge.
    reviewed = attach_fees(market, observed_at)
    if reviewed.mechanics.fee_status != "UNVERIFIED":
        raise ValueError("STOP: research fee review assumptions changed")
    return {
        "venue": str(market.venue),
        "market_id": market.venue_market_id,
        "observed_at_utc": observed_at.isoformat(),
        "status": market.status,
        "rules_sha256": hashlib.sha256(
            market.resolution_rules.encode("utf-8")
        ).hexdigest(),
        "rules_digest": rules_digest(market),
        "fee_review_status": reviewed.mechanics.fee_status,
        "fee_review_reason": reviewed.original_metadata["fee_provenance"]["reason"],
        "sides": {
            str(side): quote_row(reviewed, evidence, fixture, forecast, side, observed_at)
            for side in (Side.YES, Side.NO)
        },
    }


def main() -> None:
    csv = Path.home() / "parallax-research/nba/2025-regular/nba-2025-canonical.csv"
    games = [
        game for game in load_final_games_csv(csv)
        if game.sport == "NBA" and game.season == 2025
    ]
    if len(games) != 1230:
        raise SystemExit("STOP: expected verified 1,230-game NBA 2025 snapshot")
    fixture = ShadowFixture(
        "NBA", BDL_ID, 2026, TIPOFF,
        "DEN", "GSW", "Denver", "Golden State",
    )
    prediction = predict_future(
        games, sport="NBA", game_date=date(2026, 10, 26),
        home_team="DEN", away_team="GSW",
        game_id=BDL_ID, season=2026,
    )
    if prediction.model_status != MODEL_STATUS or prediction.home_won is not None:
        raise SystemExit("STOP: forecast is not experimental/pregame")
    if datetime.now(UTC) >= TIPOFF:
        raise SystemExit("STOP: verified fixture has already started")

    url = f"https://external-api.kalshi.com/trade-api/v2/markets/{TICKER}"
    km = kalshi_json(url).get("market")
    if not isinstance(km, dict) or km.get("ticker") != TICKER:
        raise SystemExit("STOP: Kalshi ticker mismatch")
    kb = kalshi_json(url + "/orderbook")
    kalshi_at = datetime.now(UTC)
    kalshi = normalize_kalshi(km, kb, kalshi_at.isoformat())
    kalshi_result = venue_report(kalshi, fixture, prediction, kalshi_at)

    pmus_client = PolymarketUSPublicClient(
        max_requests_per_minute=2, minimum_interval_seconds=2
    )
    try:
        rows = pmus_client.markets_page(
            limit=5, offset=0, slugs=[SLUG], retry_transport_errors=False
        )
        if len(rows) != 1 or rows[0]["slug"] != SLUG:
            raise ValueError("STOP: PMUS exact market mismatch")
        raw = rows[0]["raw"]
        if str(raw.get("id")) != rows[0]["id"] or raw.get("marketType") != "moneyline":
            raise ValueError("STOP: PMUS market identity/type mismatch")
        pb = pmus_client.book(SLUG)
        pmus_at = datetime.now(UTC)
    finally:
        pmus_client.close()
    pmus = normalize_pmus(rows[0], pb, pmus_at.isoformat())
    pmus_result = venue_report(pmus, fixture, prediction, pmus_at)

    # Both contracts settle YES on the Warriors. Compare like with like.
    if abs(
        kalshi_result["sides"]["YES"]["fair_probability_experimental"]
        - pmus_result["sides"]["YES"]["fair_probability_experimental"]
    ) > 1e-8:
        raise ValueError("STOP: venues do not refer to the same YES outcome")

    report = {
        "research_only": True,
        "buy_authorized": False,
        "fee_calculation": "UNAVAILABLE_EXPIRED_REVIEW",
        "forecast_quality": MODEL_STATUS,
        "fixture": {
            "game_id": BDL_ID, "sport": "NBA", "season": 2026,
            "home": "DEN", "away": "GSW",
            "tipoff_utc": TIPOFF.isoformat(),
        },
        "forecast": {
            "GSW": round(1 - prediction.home_win_probability, 6),
            "DEN": round(prediction.home_win_probability, 6),
        },
        "venues": {"KALSHI": kalshi_result, "POLYMARKET_US": pmus_result},
    }
    output_dir = Path.home() / "parallax-research/nba/forward"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"two-venue-gsw-den-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("RESEARCH ONLY - NO BUY / NO VERIFIED NET EDGE")
    print("GSW model probability:", report["forecast"]["GSW"])
    print("DEN model probability:", report["forecast"]["DEN"])
    for name, venue in report["venues"].items():
        for label, quote in venue["sides"].items():
            print(
                name, label, quote["outcome"],
                "bid=", quote["bid"], "ask=", quote["ask"],
                "spread=", quote["spread"],
                "ask_size=", quote["available_at_ask"],
                "gross_edge=", quote["nominal_edge_before_costs"],
                "shadow=", quote["shadow_action"],
            )
    print("SAVED:", output)


if __name__ == "__main__":
    main()
