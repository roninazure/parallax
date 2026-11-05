#!/usr/bin/env python3
"""NBA-only offline BUY economics audit of frozen forecasts and stored order books.

A quote that cleared economics historically is NOT a publishable BUY signal.
No provider network calls, alerts, order placement, model promotion, or feed writes.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from nba_forward_reconcile import archived_forecasts, canonical, parse_stamp

ET = ZoneInfo("America/New_York")
APPROVED_FEE_STATES = {"REVIEWED", "VERIFIED_SCHEDULE", "VERIFIED_UPPER_BOUND"}
MAX_OBSERVATION_AGE_SECONDS = 60
MIN_EDGE = .05
MIN_NET_RETURN = .05
MAX_SPREAD = .05


def _numeric(value):
    if isinstance(value, bool):
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _evaluate_quote(snapshot: dict, forecasts: dict) -> list[dict]:
    if snapshot.get("sport") != "NBA":
        return []
    try:
        home, away = canonical(snapshot["home_team"]), canonical(snapshot["away_team"])
        selected = canonical(snapshot["selected_yes_team"])
        kickoff = parse_stamp(snapshot["scheduled_start"])
        observed = parse_stamp(snapshot["source_observed_at"])
        captured = parse_stamp(snapshot["captured_at"])
        key = (kickoff.astimezone(ET).strftime("%Y%m%d"), away, home)
    except (KeyError, TypeError, ValueError, AttributeError):
        return [{"status": "BLOCKED", "side": "UNKNOWN", "blocking_reasons": ["INVALID_IDENTITY_OR_TIMESTAMPS"], "economic_check": "BLOCKED", "publication_eligible": False, "buy_authorized": False}]
    match = forecasts.get(key)
    identity = {
        "game_id": snapshot.get("game_id"), "matchup": f"{away}@{home}",
        "market_id": snapshot.get("market_id"), "venue": snapshot.get("venue"),
        "source_observed_at": observed.isoformat(),
    }
    failure = []
    if not match:
        failure.append("NO_FROZEN_FORECAST")
    if selected not in (home, away) or home == away:
        failure.append("INVALID_SELECTED_TEAM")
    if not (observed < kickoff and captured < kickoff and captured >= observed):
        failure.append("NOT_VERIFIED_PREGAME_QUOTE")
    if abs((captured - observed).total_seconds()) > MAX_OBSERVATION_AGE_SECONDS:
        failure.append("STALE_AT_CAPTURE")
    if match and parse_stamp(match["captured_at_utc"]) > observed:
        failure.append("FORECAST_NOT_AVAILABLE_AT_QUOTE_TIME")
    if match and parse_stamp(match["kickoff_utc"]) != kickoff:
        failure.append("FORECAST_KICKOFF_MISMATCH")
    if not isinstance(snapshot.get("rules_sha256"), str) or len(snapshot["rules_sha256"]) != 64:
        failure.append("RULES_HASH_MISSING")
    if snapshot.get("status") != "OPEN":
        failure.append("MARKET_NOT_OPEN")
    fee_status = snapshot.get("fee_status", "UNVERIFIED")
    # A fee status by itself is not proof that a reviewed schedule remained
    # valid at the observed quote time. Older snapshot schema did not retain
    # the expiration and must remain blocked rather than invent eligibility.
    fee_expiration = snapshot.get("fee_valid_until")
    try:
        fees_current = bool(fee_expiration and observed < parse_stamp(fee_expiration))
    except (TypeError, ValueError, AttributeError):
        fees_current = False
    if not fees_current:
        failure.append("FEE_VALIDITY_UNPROVEN")
    if fee_status not in APPROVED_FEE_STATES:
        failure.append("FEES_UNVERIFIED")
    scenarios = snapshot.get("fee_scenario_25", {})
    result = []
    for side in ("YES", "NO"):
        reasons = list(failure)
        scenario = scenarios.get(side) if isinstance(scenarios, dict) else None
        ask = _numeric(snapshot.get("yes_ask" if side == "YES" else "no_ask"))
        bid = _numeric(snapshot.get("yes_bid" if side == "YES" else "no_bid"))
        if ask is None or bid is None or bid < 0 or bid > ask or ask - bid > MAX_SPREAD + 1e-9:
            reasons.append("SPREAD_UNVERIFIED_OR_WIDE")
        depth = snapshot.get("executable_depth", {})
        side_depth = depth.get(side) if isinstance(depth, dict) else None
        if not isinstance(side_depth, list) or not side_depth:
            reasons.append("ORDERBOOK_DEPTH_NOT_VERIFIED")
        else:
            try:
                first_price, first_size = side_depth[0]
                if (len(side_depth[0]) != 2 or _numeric(first_price) is None
                        or _numeric(first_size) is None or float(first_size) <= 0
                        or abs(float(first_price) - ask) > 1e-8):
                    reasons.append("ORDERBOOK_DEPTH_NOT_VERIFIED")
            except (TypeError, ValueError, IndexError):
                reasons.append("ORDERBOOK_DEPTH_NOT_VERIFIED")
        contracts = _numeric(scenario.get("contracts")) if isinstance(scenario, dict) else None
        # A provider's scenario.available flag is not enough: verify the
        # captured order book can actually fill the scenario's contracts.
        book_total = 0.0
        book_cost = 0.0
        book_levels_valid = True
        if isinstance(side_depth, list):
            last_price = 0.0
            for level in side_depth:
                if not isinstance(level, (tuple, list)) or len(level) != 2:
                    book_levels_valid = False
                    break
                p, size = (_numeric(v) for v in level)
                if p is None or not 0 < p < 1 or size is None or size <= 0 or p < last_price:
                    book_levels_valid = False
                    break
                last_price = p
                if contracts is not None and contracts > book_total:
                    take = min(size, contracts - book_total)
                    book_cost += take * p
                book_total += size
        else:
            book_levels_valid = False
        if not book_levels_valid:
            reasons.append("ORDERBOOK_DEPTH_NOT_VERIFIED")
        if contracts is not None and contracts > book_total + 1e-8:
            reasons.append("INSUFFICIENT_ORDERBOOK_DEPTH")
        total = _numeric(scenario.get("total_cost")) if isinstance(scenario, dict) else None
        fee = _numeric(scenario.get("fees_estimate")) if isinstance(scenario, dict) else None
        if not (0 < (ask or 0) < 1):
            reasons.append("INVALID_EXECUTABLE_ASK")
        if (not isinstance(scenario, dict) or scenario.get("available") is not True
                or contracts is None or contracts <= 0 or total is None or total <= 0):
            reasons.append("INSUFFICIENT_EXECUTABLE_LIQUIDITY")
        if fee is None or fee < 0:
            reasons.append("UNKNOWN_FEE_ESTIMATE")
        if total is not None and contracts is not None and ask is not None and fee is not None:
            if total + 1e-8 < contracts * ask + fee:
                reasons.append("INCONSISTENT_COST_ACCOUNTING")
            if book_levels_valid and total + 1e-8 < book_cost + fee:
                reasons.append("ORDERBOOK_SLIPPAGE_NOT_ACCOUNTED")
        side_team = selected if side == "YES" else (away if selected == home else home)
        probability = None
        gross_edge = None
        net_ev = None
        net_return = None
        if match and selected in (home, away):
            phome = match["home_probability"]
            probability = phome if side_team == home else 1.0 - phome
            if ask is not None:
                gross_edge = probability - ask
                if gross_edge < MIN_EDGE:
                    reasons.append("GROSS_EDGE_BELOW_THRESHOLD")
            if (contracts is not None and contracts > 0 and total is not None and total > 0
                    and fee is not None and fee >= 0 and fee_status in APPROVED_FEE_STATES
                    and "INCONSISTENT_COST_ACCOUNTING" not in reasons
                    and "ORDERBOOK_SLIPPAGE_NOT_ACCOUNTED" not in reasons
                    and "INSUFFICIENT_ORDERBOOK_DEPTH" not in reasons):
                net_ev = probability * contracts - total
                net_return = net_ev / total
                if net_return < MIN_NET_RETURN:
                    reasons.append("NET_RETURN_BELOW_THRESHOLD")
        # No direct BUY authority even when a historical observation clears all
        # the economic checks: certified model and current execution are absent.
        result.append({
            **identity, "side": side, "selected_team": side_team,
            "model_probability": probability, "ask": ask, "contracts": contracts,
            "total_cost": total, "fee_estimate": fee, "fee_status": fee_status,
            "gross_edge": gross_edge, "net_ev_25": net_ev,
            "net_expected_return": net_return,
            "economic_check": "PASSED_HISTORICAL_ONLY" if not reasons else "BLOCKED",
            "blocking_reasons": sorted(set(reasons)),
            "publication_eligible": False, "buy_authorized": False,
        })
    return result


def audit(forecast_dir: Path, quote_dir: Path, *, max_rows: int = 10000) -> dict:
    if not 1 <= max_rows <= 100000:
        raise ValueError("max_rows out of range")
    if not forecast_dir.is_dir() or not quote_dir.is_dir():
        raise ValueError("required NBA input directories missing")
    forecasts, source = archived_forecasts(forecast_dir)
    if not forecasts:
        raise ValueError("no valid pregame forecasts")
    if source.get("malformed_snapshots", 0) or source.get("invalid_rows", 0):
        raise ValueError("malformed forecast archive; fail closed")
    all_rows = []
    examined = 0
    for path in sorted(quote_dir.glob("parallax-league-quotes-*.jsonl")):
        if path.is_symlink():
            raise ValueError("symlink quote archive rejected")
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                examined += 1
                if examined > max_rows:
                    raise ValueError("quote row budget exceeded")
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("invalid quote record")
                all_rows.extend(_evaluate_quote(raw, forecasts))
    failures = Counter(reason for row in all_rows for reason in row["blocking_reasons"])
    return {
        "status": "NBA_BUY_ECONOMICS_RESEARCH_ONLY",
        "forecast_games": len(forecasts), "quote_rows_examined": examined,
        "nba_side_evaluations": len(all_rows),
        "historical_economic_passes": sum(row["economic_check"] == "PASSED_HISTORICAL_ONLY" for row in all_rows),
        "blocked_side_evaluations": sum(row["economic_check"] == "BLOCKED" for row in all_rows),
        "failure_counts": dict(sorted(failures.items())),
        "independent_model_certified": False,
        "live_executable_coverage_verified": False,
        "buy_authorized": False, "publication_eligible": False,
        "rows": all_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forecasts", type=Path, required=True)
    parser.add_argument("--quotes", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=10000)
    parser.add_argument("--output", type=Path, help="Research JSON path; refuses overwrite")
    args = parser.parse_args()
    report = audit(args.forecasts, args.quotes, max_rows=args.max_rows)
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"},
                     indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
