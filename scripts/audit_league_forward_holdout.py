"""Offline audit of NBA/NHL prospective final-results holdouts.

This is a fail-closed evidence *screen*, not a provider-independence,
authorization, model certification, or BUY-publication decision. No network IO.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model_v2 import reconcile_independent_history


def _read_checked(path: Path, sport: str):
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    games = [g for g in load_final_games_csv(path) if g.sport == sport]
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise ValueError("source modified during parsing: " + path.name)
    if not games:
        raise ValueError("no selected-league games: " + path.name)
    return games, before


def _fixture(game):
    return (game.season, game.game_date, game.home_team, game.away_team)


def _fixtures(games, label):
    identity = [_fixture(game) for game in games]
    if len(identity) != len(set(identity)):
        raise ValueError("duplicate fixture in " + label)
    if any(team != team.strip().upper() for game in games
           for team in (game.home_team, game.away_team)):
        raise ValueError("noncanonical team identity in " + label)
    return set(identity)


def audit(*, sport: str, historical: Path, forward: Path,
          independent: Path, as_of: date) -> dict:
    if sport not in {"NBA", "NHL"}:
        raise ValueError("sport must be NBA or NHL")
    train, train_sha = _read_checked(historical, sport)
    primary, primary_sha = _read_checked(forward, sport)
    secondary, secondary_sha = _read_checked(independent, sport)
    train_keys = _fixtures(train, "historical snapshot")
    primary_keys = _fixtures(primary, "forward holdout")
    _fixtures(secondary, "independent forward holdout")
    train_end = max(g.game_date for g in train)
    forward_start = min(g.game_date for g in primary)
    forward_end = max(g.game_date for g in primary)
    if forward_start <= train_end or train_keys.intersection(primary_keys):
        raise ValueError("forward fixtures overlap or predate historical snapshot")
    if forward_end > as_of:
        raise ValueError("future-dated final results cannot be treated as resolved")
    if primary_sha == secondary_sha:
        raise ValueError("identical primary and secondary forward source bytes")
    reconcile_independent_history(primary, secondary, sport=sport)
    return {
        "sport": sport,
        "status": "FORWARD_FIXTURES_RECONCILED_NON_CERTIFYING",
        "as_of": as_of.isoformat(),
        "training_through": train_end.isoformat(),
        "forward_start": forward_start.isoformat(),
        "forward_end": forward_end.isoformat(),
        "forward_games": len(primary),
        "historical_sha256": train_sha,
        "forward_sha256": primary_sha,
        "independent_forward_sha256": secondary_sha,
        "fixture_consistency_passed": True,
        "provider_independence_verified": False,
        "source_authorization_verified": False,
        "model_calibration_verified": False,
        "production_buy_authorized": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", required=True, choices=("NBA", "NHL"))
    parser.add_argument("--historical", type=Path, required=True)
    parser.add_argument("--forward", type=Path, required=True)
    parser.add_argument("--independent-forward", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    try:
        report = audit(sport=args.sport, historical=args.historical,
                       forward=args.forward, independent=args.independent_forward,
                       as_of=args.as_of)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"FORWARD_AUDIT_BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
