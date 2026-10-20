#!/usr/bin/env python3
"""One-shot NBA opening-day cross-venue order-book research, 2026-10-20 only.

SOURCE OF TRUTH: existing locally saved 2026-10-20 fixture/contract receipt.
NO NBA provider calls. Max ten venue GETs: two Kalshi nested-event payloads,
four Kalshi contract order books, two PMUS exact markets and two PMUS books.
Zero retries, no orders, no scheduler/ntfy/production writes or BUY signals.
Fees below are explicitly hypothetical, not exchange/account certification.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from zoneinfo import ZoneInfo

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.economics import kalshi_fill_fees, pmus_fill_fees
from parallax.fees import attach_fees
from parallax.league_elo_experimental import (
    MODEL_STATUS, load_final_games_csv, predict_future,
)
from parallax.league_shadow_evidence import ShadowFixture, evaluate_shadow, shadow_evidence
from parallax.models import Side
from parallax.normalization import normalize_kalshi, normalize_pmus
from scripts.nba_dual_venue_shadow_snapshot import kalshi_json
from scripts.nba_opening_day_identity_probe import GAMES

_ET = ZoneInfo("America/New_York")
_KALSHI = "https://external-api.kalshi.com/trade-api/v2"
_STAKE = Decimal("25")
_REFERENCE_KALSHI_FEE = .07
_REFERENCE_PMUS_FEE = .0695
_CODE = {"PHI@NYK": "bdl:21717856", "OKC@SAS": "bdl:21717857"}


def validate_receipt(receipt: dict, *, now: datetime) -> list[tuple[dict, dict]]:
    """Fail closed on ANY incomplete or other-game source evidence."""
    if receipt.get("research_only") is not True or receipt.get("buy_authorized") is not False:
        raise ValueError("STOP: source receipt is not research only")
    rows = receipt.get("games")
    if not isinstance(rows, list) or len(rows) != len(GAMES):
        raise ValueError("STOP: expected both opening-day fixture receipts")
    result = []
    by_label = {row.get("matchup"): row for row in rows if isinstance(row, dict)}
    if len(by_label) != len(GAMES):
        raise ValueError("STOP: duplicate or missing fixture receipt")
    for cfg in GAMES:
        row = by_label.get(cfg["label"])
        if not row:
            raise ValueError("STOP: missing fixture")
        if (row.get("bdl_game_id") != _CODE[cfg["label"]]
                or row.get("tipoff_utc") != cfg["start"]
                or row.get("home_code") != cfg["home"]
                or row.get("away_code") != cfg["away"]
                or now >= datetime.fromisoformat(cfg["start"])):
            raise ValueError(f'STOP: fixture identity or pregame bound failed: {cfg["label"]}')
        kalshi = row.get("kalshi") or {}
        pmus = row.get("polymarket_us") or {}
        expected_tickers = {cfg["kalshi_event"] + "-" + cfg["home"],
                            cfg["kalshi_event"] + "-" + cfg["away"]}
        markets = kalshi.get("markets")
        if (kalshi.get("event") != cfg["kalshi_event"]
                or not isinstance(markets, list) or len(markets) != 2
                or {m.get("ticker") for m in markets if isinstance(m, dict)} != expected_tickers
                or any(m.get("status") not in ("active", "open") for m in markets)):
            raise ValueError(f'STOP: missing two Kalshi team-win contracts: {cfg["label"]}')
        if (pmus.get("id") != cfg["pmus_id"]
                or pmus.get("slug") != cfg["pmus_slug"]
                or pmus.get("gameStartTime") not in (
                    cfg["start"], cfg["start"].replace("+00:00", "Z")
                )):
            raise ValueError(f'STOP: PMUS contract identity mismatch: {cfg["label"]}')
        outcomes = pmus.get("outcomes")
        if (not isinstance(outcomes, list) or len(outcomes) != 2
                or {(o.get("ordering"), o.get("long_yes"), str(o.get("team_code")).upper())
                    for o in outcomes if isinstance(o, dict)}
                != {("away", True, cfg["pmus_away"]), ("home", False, cfg["pmus_home"])}):
            raise ValueError(f'STOP: PMUS YES/NO identity mismatch: {cfg["label"]}')
        prob = row.get("model_home_win_probability_experimental")
        if (isinstance(prob, bool) or not isinstance(prob, (int, float))
                or not 0 < prob < 1):
            raise ValueError("STOP: missing model snapshot")
        result.append((cfg, row))
    return result


def scenario(market, *, side: Side, fair: float, venue: str) -> dict:
    """Research-only top-of-book, one-fill $25 scenario; no certification."""
    bid = market.yes_bid if side == Side.YES else market.no_bid
    ask = market.yes_ask if side == Side.YES else market.no_ask
    depth_levels = market.executable_depth.get(side, ())
    depth = sum(q for price, q in depth_levels if price == ask and q > 0) if ask else 0.0
    edge = fair - ask if ask is not None else None
    spread = ask - bid if ask is not None and bid is not None else None
    blockers = []
    if spread is None or not (0 <= spread <= .05 + 1e-9):
        blockers.append("SPREAD_MISSING_OR_GT_5_CENTS")
    if edge is None or edge < .05 - 1e-9:
        blockers.append("GROSS_EDGE_MISSING_OR_LT_5_POINTS")
    required = int((_STAKE / Decimal(str(ask))).to_integral_value(
        rounding=ROUND_FLOOR
    )) if ask is not None and 0 < ask < 1 else 0
    if not required or depth < required:
        blockers.append("INSUFFICIENT_TOP_ASK_DEPTH_FOR_25_USD")
    # Even a nominally attractive quote is never a certified play.
    blockers.extend(("FEE_POLICY_NOT_CERTIFIED", "ELO_UNCALIBRATED"))
    one_fill = None
    if (ask is not None and 0 < ask < 1 and
            depth >= 1 and (market.mechanics.minimum_quantity or 1) <= depth):
        qty = min(required, int(Decimal(str(depth)).to_integral_value(rounding=ROUND_FLOOR)))
        if qty > 0:
            if venue == "KALSHI":
                fee = kalshi_fill_fees(
                    [(float(qty), ask)], _REFERENCE_KALSHI_FEE, balance_precision=.01
                )[0]
            else:
                fee = pmus_fill_fees([(float(qty), ask)], theta=_REFERENCE_PMUS_FEE)[0]
            ev = (Decimal(str(fair)) - Decimal(str(ask))) * qty - fee
            one_fill = {
                "quantity": qty,
                "entry_notional_usd": round(ask * qty, 4),
                "estimated_fee_usd": float(fee),
                "experimental_expected_value_usd": round(float(ev), 4),
                "single_fill_hypothetical_only": True,
            }
    return {
        "bid": bid, "ask": ask, "spread": round(spread, 6) if spread is not None else None,
        "available_at_ask": depth,
        "experimental_fair_probability": round(fair, 6),
        "nominal_edge_before_fees": round(edge, 6) if edge is not None else None,
        "top_ask_contracts_for_25usd": required,
        "hypothetical_one_fill": one_fill,
        "blockers": blockers,
        "eligible_for_buy": False,
    }


def verified_quote(market, *, fixture, forecast, now, side, outcome_team) -> dict:
    e = shadow_evidence(market, fixture, forecast, now=now)
    if e is None or e.validation_status != "UNVALIDATED":
        raise ValueError("STOP: cannot prove contract/rules/fixture binding")
    expected = (
        forecast.home_win_probability if outcome_team == fixture.home_team
        else 1 - forecast.home_win_probability
    )
    fair = e.fair_probability if side == Side.YES else 1 - e.fair_probability
    if abs(fair - expected) > 1e-7:
        raise ValueError("STOP: outcome orientation/forecast conflict")
    reviewed = attach_fees(market, now)
    if reviewed.mechanics.fee_status != "UNVERIFIED":
        raise ValueError("STOP: cannot rely on previously reviewed fee policy")
    play = evaluate_shadow(reviewed, fixture, forecast, side=side, now=now)
    if play is None or str(play.suggested_action) == "BUY":
        raise ValueError("STOP: unexpected shadow authorization")
    return {
        "venue": str(market.venue), "market_id": market.venue_market_id,
        "side": str(side), "outcome_team": outcome_team,
        "observed_at_utc": now.isoformat(),
        "shadow_action": str(play.suggested_action),
        "shadow_failed_gates": list(play.verdict.failed_gates),
        **scenario(reviewed, side=side, fair=fair, venue=str(market.venue)),
        "buy_authorized": False,
    }


def run_one(cfg: dict, record: dict, history: list, pmus_client) -> dict:
    now = datetime.now(UTC)
    if now >= datetime.fromisoformat(cfg["start"]):
        raise ValueError(f'STOP: {cfg["label"]} already started')
    fixture = ShadowFixture(
        "NBA", record["bdl_game_id"], 2026,
        datetime.fromisoformat(cfg["start"]),
        cfg["home"], cfg["away"],
        cfg["venue_home_name"], cfg["venue_away_name"],
    )
    forecast = predict_future(
        history, sport="NBA", game_date=fixture.kickoff.astimezone(_ET).date(),
        home_team=fixture.home_team, away_team=fixture.away_team,
        game_id=fixture.game_id, season=2026,
    )
    if (forecast.model_status != MODEL_STATUS or forecast.home_won is not None
            or abs(forecast.home_win_probability -
                   record["model_home_win_probability_experimental"]) > 2e-6):
        raise ValueError("STOP: forecast diverges from original local fixture snapshot")
    event_url = f'{_KALSHI}/events/{cfg["kalshi_event"]}?with_nested_markets=true'
    event_payload = kalshi_json(event_url).get("event")
    if not isinstance(event_payload, dict) or event_payload.get("event_ticker") != cfg["kalshi_event"]:
        raise ValueError("STOP: Kalshi event ID mismatch")
    if (event_payload.get("fee_type_override") is not None or
            event_payload.get("fee_multiplier_override") is not None):
        raise ValueError("STOP: Kalshi game-specific fee overrides require separate review")
    rows = event_payload.get("markets")
    expected = {m["ticker"]: m for m in record["kalshi"]["markets"]}
    if not isinstance(rows, list) or len(rows) != 2 or {r.get("ticker") for r in rows} != set(expected):
        raise ValueError("STOP: unexpected Kalshi markets on event")
    quotes = []
    for raw in rows:
        ticker = raw["ticker"]
        prior = expected[ticker]
        for field in ("ticker", "title", "rules_primary", "rules_secondary"):
            if raw.get(field) != prior.get(field):
                raise ValueError(f"STOP: Kalshi {field} changed since identity receipt")
        selected_team = ticker.rsplit("-", 1)[-1]
        if selected_team not in (fixture.home_team, fixture.away_team):
            raise ValueError("STOP: Kalshi YES team not in fixture")
        ob = kalshi_json(f"{_KALSHI}/markets/{ticker}/orderbook")
        moment = datetime.now(UTC)
        market = normalize_kalshi(raw, ob, moment.isoformat())
        for side in (Side.YES, Side.NO):
            outcome = selected_team if side == Side.YES else (
                fixture.away_team if selected_team == fixture.home_team else fixture.home_team
            )
            quotes.append(verified_quote(
                market, fixture=fixture, forecast=forecast, now=moment,
                side=side, outcome_team=outcome,
            ))
    rows = pmus_client.markets_page(
        limit=5, offset=0, slugs=[cfg["pmus_slug"]],
        retry_transport_errors=False,
    )
    if len(rows) != 1 or rows[0]["slug"] != cfg["pmus_slug"]:
        raise ValueError("STOP: PMUS exact market missing")
    raw = rows[0]["raw"]
    if (str(raw.get("id")) != cfg["pmus_id"]
            or raw.get("marketType") != "moneyline"
            or raw.get("description") != record["polymarket_us"]["description"]
            or raw.get("gameStartTime") not in (
                cfg["start"], cfg["start"].replace("+00:00", "Z")
            )
            or Decimal(str(raw.get("feeCoefficient"))) != Decimal(".0695")):
        raise ValueError("STOP: PMUS identity/rules/fee coefficient mismatch")
    book = pmus_client.book(cfg["pmus_slug"])
    moment = datetime.now(UTC)
    market = normalize_pmus(rows[0], book, moment.isoformat())
    for side, outcome in ((Side.YES, fixture.away_team), (Side.NO, fixture.home_team)):
        quotes.append(verified_quote(
            market, fixture=fixture, forecast=forecast, now=moment,
            side=side, outcome_team=outcome,
        ))
    return {
        "matchup": cfg["label"], "bdl_game_id": fixture.game_id,
        "tipoff_utc": fixture.kickoff.isoformat(),
        "experimental_home_win_probability": round(forecast.home_win_probability, 6),
        "experimental_away_win_probability": round(1-forecast.home_win_probability, 6),
        "quotes": quotes,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("identity_receipt", type=Path)
    args = p.parse_args()
    record = json.loads(args.identity_receipt.read_text(encoding="utf-8"))
    checked = validate_receipt(record, now=datetime.now(UTC))
    csv_path = Path.home() / "parallax-research/nba/2025-regular/nba-2025-canonical.csv"
    history = [g for g in load_final_games_csv(csv_path)
               if g.sport == "NBA" and g.season == 2025]
    if len(history) != 1230:
        raise ValueError("STOP: expected the verified 1,230 NBA 2025 finals")
    client = PolymarketUSPublicClient(minimum_interval_seconds=2, max_requests_per_minute=4)
    try:
        games = [run_one(cfg, row, history, client) for cfg, row in checked]
    finally:
        client.close()
    report = {
        "research_only": True, "buy_authorized": False,
        "source_receipt": args.identity_receipt.name,
        "request_ceiling": {"kalshi": 6, "polymarket_us": 4, "nba_provider": 0},
        "fee_assumptions": {
            "kalshi_reference_coefficient": .07,
            "polymarket_us_observed_coefficient": .0695,
            "account_precision_and_fee_policy": "NOT_CERTIFIED",
            "model": MODEL_STATUS,
        },
        "games": games,
    }
    folder = Path.home() / "parallax-research/nba/forward"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"opening-day-quotes-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("RESEARCH ONLY / NO BUY / HYPOTHETICAL FEES / QUOTES MAY STALE")
    for game in games:
        print("\nMATCHUP", game["matchup"], "HOME ELO", game["experimental_home_win_probability"])
        for q in game["quotes"]:
            hypo = q["hypothetical_one_fill"]
            print(q["venue"], q["market_id"], q["side"], q["outcome_team"],
                  "bid=", q["bid"], "ask=", q["ask"],
                  "size=", q["available_at_ask"], "gross_edge=", q["nominal_edge_before_fees"],
                  "hypo_EV=", hypo["experimental_expected_value_usd"] if hypo else None,
                  "action=", q["shadow_action"],
                  "blockers=", ",".join(q["blockers"]))
    print("SAVED:", path)


if __name__ == "__main__":
    main()
