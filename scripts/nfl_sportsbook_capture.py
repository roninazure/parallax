#!/usr/bin/env python3
"""Explicit, one-shot NFL odds research capture. Never publishes BUY signals.

This is deliberately NOT installed as a scheduled production lane. A capture
requires a known quota state, an explicit sport key, and an explicit output.
No retry on any provider error. Future runs must be separately authorized.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parallax.nfl import NFLGame, fetch_games, parse_games
from scripts.sports_scan import load_quota_state, persist_quota_state, quota_allows_request
from sports.nfl_evidence import match_nfl_evidence
from sports.odds_provider import OddsResponse
from sports.nfl_odds_v4 import fetch_nfl_v4_once


def capture_games(response: OddsResponse, games: list[NFLGame], *, observed_at: datetime) -> dict:
    """Pure offline conversion; no network, notification, or publication."""
    if observed_at.tzinfo is None:
        raise ValueError("capture requires timezone-aware observed_at")
    matched = []
    for event in response.events:
        evidence = match_nfl_evidence(event, games, observed_at=observed_at)
        if evidence is not None:
            matched.append(asdict(evidence))
    return {
        "schema_version": "nfl-sportsbook-research-v1",
        "research_only": True,
        "buy_publication_eligible": False,
        "captured_at_utc": observed_at.astimezone(UTC).isoformat(),
        "events_seen": len(response.events),
        "events_accepted": len(matched),
        "evidence": matched,
        "limitations": [
            "Captured timestamps are retrieval times, not guaranteed provider quote observation times.",
            "Consensus is market evidence, not independently validated model alpha.",
            "No customer BUY publication, historical execution claims, or live forecast modification.",
        ],
    }


def capture(response: OddsResponse, games_csv: str, *, observed_at: datetime) -> dict:
    """Compatibility wrapper for deterministic offline CSV-based tests."""
    return capture_games(response, parse_games(games_csv), observed_at=observed_at)


def quota_headroom_verified(state: dict | None, *, reserve: int) -> bool:
    """Unknown or malformed quota means zero authorized requests."""
    if not isinstance(state, dict) or reserve < 1:
        return False
    try:
        remaining = int(state["remaining"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return remaining > reserve


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    schedule = parser.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--games-csv", type=Path)
    schedule.add_argument("--fetch-schedule", action="store_true",
                          help="Fetch the existing nflverse official schedule once")
    parser.add_argument("--quota-state", type=Path, default=Path("reports/sports_quota_state.json"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-quota-reserve", type=int, default=10)
    parser.add_argument("--fetch-once", action="store_true", help="Explicitly authorize one odds request")
    args = parser.parse_args()

    if not args.fetch_once:
        parser.error("no network request without --fetch-once")
    if args.output.exists():
        parser.error("refusing to overwrite existing snapshot")
    if args.min_quota_reserve < 1:
        parser.error("quota reserve must be positive")
    state = load_quota_state(args.quota_state)
    if not quota_headroom_verified(state, reserve=args.min_quota_reserve):
        parser.error("missing known quota headroom: refusing provider request")
    now = datetime.now(UTC)
    if not quota_allows_request(state, now=now, reserve=args.min_quota_reserve):
        parser.error("quota reserve reached: refusing provider request")
    api_key = os.environ.get("THE_ODDS_API_KEY")
    if not api_key:
        parser.error("THE_ODDS_API_KEY unavailable")

    # No retries; avoid a remote request if the supplied schedule is invalid.
    if args.fetch_schedule:
        try:
            games = fetch_games()
        except Exception as exc:
            parser.error(f"official NFL schedule fetch failed: {type(exc).__name__}")
    else:
        games = parse_games(args.games_csv.read_text(encoding="utf-8"))
    if not games:
        parser.error("empty or invalid official NFL schedule")
    response = fetch_nfl_v4_once(api_key=api_key, now=now)
    persist_quota_state(args.quota_state, response, now)
    report = capture_games(response, games, observed_at=now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(report, target, indent=2, sort_keys=True)
        target.write("\n")
    print(json.dumps({"output": str(args.output), "events_accepted": report["events_accepted"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
