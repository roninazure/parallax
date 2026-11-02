"""Forward-only NFL forecast comparison from pre-kickoff research snapshots.

No live scanning, publishing, trading, source fetching, or model promotion.
The fixed 50/50 blend is an experimental reference, not a calibrated BUY model.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean

from parallax.nfl import NFLGame, _advance, _probability, parse_games


def _aware_utc(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def _scores(probabilities: list[float], outcomes: list[int]) -> dict:
    if not outcomes:
        return {"n": 0, "brier": None, "log_loss": None}
    return {
        "n": len(outcomes),
        "brier": round(mean((p - y) ** 2 for p, y in zip(probabilities, outcomes)), 8),
        "log_loss": round(mean(-(y * math.log(p) + (1 - y) * math.log(1 - p))
                               for p, y in zip(probabilities, outcomes)), 8),
    }


def evaluate(games: list[NFLGame], snapshots: list[dict], *,
             min_lead_minutes: int = 60, max_lookback_hours: int = 24,
             min_scored: int = 100) -> dict:
    if min_lead_minutes < 0 or max_lookback_hours < 1 or min_scored < 1:
        raise ValueError("invalid predeclared evaluation policy")
    by_game: dict[str, list[tuple[datetime, float]]] = {}
    for snapshot in snapshots:
        if snapshot.get("schema_version") != "nfl-sportsbook-research-v1":
            continue
        if snapshot.get("research_only") is not True or snapshot.get("buy_publication_eligible") is not False:
            continue
        captured = _aware_utc(snapshot.get("captured_at_utc"))
        if captured is None:
            continue
        for evidence in snapshot.get("evidence", []):
            if not isinstance(evidence, dict) or _aware_utc(evidence.get("observed_at")) != captured:
                continue
            p = evidence.get("home_probability")
            away_p = evidence.get("away_probability")
            if not isinstance(p, (float, int)) or isinstance(p, bool) or not math.isfinite(p) or not 0 < p < 1:
                continue
            if not isinstance(away_p, (float, int)) or isinstance(away_p, bool) or not math.isfinite(away_p):
                continue
            if not isinstance(evidence.get("bookmaker_count"), int) or evidence["bookmaker_count"] < 2:
                continue
            if abs(p + away_p - 1) > 1e-6:
                continue
            by_game.setdefault(str(evidence.get("game_id")), []).append((captured, float(p)))

    rows = []
    ratings: dict[str, float] = {}
    games = sorted(games, key=lambda game: (game.kickoff, game.game_id))
    index = 0
    while index < len(games):
        kickoff_text = games[index].kickoff
        end = index
        while end < len(games) and games[end].kickoff == kickoff_text:
            end += 1
        predictions = [
            (game, _probability(ratings.get(game.home_team, 1500), ratings.get(game.away_team, 1500)))
            for game in games[index:end]
        ]
        for game, elo_p in predictions:
            kickoff = _aware_utc(game.kickoff)
            if kickoff is None or game.home_score is None or game.away_score is None or game.home_score == game.away_score:
                continue
            eligible = [
                (captured, p) for captured, p in by_game.get(game.game_id, [])
                if kickoff - timedelta(hours=max_lookback_hours) <= captured
                <= kickoff - timedelta(minutes=min_lead_minutes)
            ]
            if not eligible:
                continue
            captured, market_p = max(eligible, key=lambda row: row[0])
            rows.append({"game_id": game.game_id, "kickoff": game.kickoff,
                         "observation": captured.isoformat(),
                         "home_win": int(game.home_score > game.away_score),
                         "elo": elo_p, "market": market_p,
                         "fixed_blend": (elo_p + market_p) / 2})
        for game, p in predictions:
            _advance(ratings, game, p)
        index = end

    outcomes = [row["home_win"] for row in rows]
    status = "SUFFICIENT_FORWARD_SAMPLE" if len(rows) >= min_scored else "INSUFFICIENT_FORWARD_SAMPLE"
    return {
        "status": status,
        "matched_completed_games": len(rows),
        "min_required_games": min_scored,
        "decision_cutoff_minutes_before_kickoff": min_lead_minutes,
        "max_quote_lookback_hours": max_lookback_hours,
        "elo": _scores([row["elo"] for row in rows], outcomes),
        "market": _scores([row["market"] for row in rows], outcomes),
        "fixed_blend_research_only": _scores([row["fixed_blend"] for row in rows], outcomes),
        "research_only": True,
        "buy_publication_eligible": False,
        "limits": ["Snapshots must have been captured before a fixed kickoff cutoff.",
                   "No execution prices, betting profitability, or BUY eligibility inferred.",
                   "Experimental blend is not fitted, selected, or production-certified."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games-csv", type=Path, required=True)
    parser.add_argument("--snapshots", type=Path, nargs="+", required=True)
    parser.add_argument("--min-scored", type=int, default=100)
    args = parser.parse_args()
    snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in args.snapshots]
    report = evaluate(parse_games(args.games_csv.read_text(encoding="utf-8")), snapshots,
                      min_scored=args.min_scored)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "SUFFICIENT_FORWARD_SAMPLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
