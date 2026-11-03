"""Forward-only NFL forecast comparison from pre-kickoff research snapshots.

No live scanning, publishing, trading, source fetching, or model promotion.
The fixed 50/50 blend is an experimental reference, not a calibrated BUY model.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean

from parallax.nfl import NFLGame, _advance, _probability, fetch_games, parse_games


def _aware_utc(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def _loss(probability: float, outcome: int, metric: str) -> float:
    if metric == "brier":
        return (probability - outcome) ** 2
    clipped = min(max(probability, 1e-12), 1 - 1e-12)
    return -(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped))


def _scores(probabilities: list[float], outcomes: list[int]) -> dict:
    if not outcomes:
        return {"n": 0, "brier": None, "log_loss": None}
    return {
        "n": len(outcomes),
        "brier": round(mean((p - y) ** 2 for p, y in zip(probabilities, outcomes)), 8),
        "log_loss": round(mean(_loss(p, y, "log_loss") for p, y in zip(probabilities, outcomes)), 8),
    }


def _paired_bootstrap(rows: list[dict], challenger: str, metric: str, *,
                      iterations: int = 2000, seed: int = 20261103) -> dict:
    """Paired week-cluster bootstrap of challenger-minus-Elo loss.

    All games from a calendar week are resampled together to reduce the
    independence assumption across games sharing weekly conditions. Negative
    deltas favor the challenger. This remains uncertainty quantification,
    not a substitute for a predeclared untouched holdout.
    """
    if not rows:
        return {"n": 0, "clusters": 0, "delta": None, "ci95": [None, None],
                "interpretation": "NO_DATA"}
    clusters: dict[str, list[float]] = {}
    for row in rows:
        kickoff = _aware_utc(row["kickoff"])
        if kickoff is None:
            continue
        week_start = (kickoff.date() - timedelta(days=kickoff.weekday())).isoformat()
        clusters.setdefault(week_start, []).append(
            _loss(row[challenger], row["home_win"], metric)
            - _loss(row["elo"], row["home_win"], metric)
        )
    if not clusters:
        return {"n": 0, "clusters": 0, "delta": None, "ci95": [None, None],
                "interpretation": "NO_VALID_CLUSTERS"}
    groups = list(clusters.values())
    deltas = [value for group in groups for value in group]
    point = mean(deltas)
    rng = random.Random(seed)
    samples = []
    for _ in range(iterations):
        selected = [groups[rng.randrange(len(groups))] for _ in range(len(groups))]
        samples.append(mean(value for group in selected for value in group))
    samples.sort()
    low = samples[int(0.025 * (iterations - 1))]
    high = samples[int(0.975 * (iterations - 1))]
    return {
        "n": len(deltas),
        "clusters": len(groups),
        "delta": round(point, 8),
        "ci95": [round(low, 8), round(high, 8)],
        "interpretation": "CHALLENGER_IMPROVEMENT_SUPPORTED" if high < 0 else (
            "ELO_IMPROVEMENT_SUPPORTED" if low > 0 else "NO_CLEAR_DIFFERENCE"
        ),
        "delta_definition": "mean challenger loss minus mean Elo loss; negative favors challenger",
        "resampling_unit": "calendar_week_cluster",
        "bootstrap_iterations": iterations,
        "seed": seed,
    }


def evaluate(games: list[NFLGame], snapshots: list[dict], *,
             min_lead_minutes: int = 60, max_lookback_hours: int = 24,
             min_scored: int = 100, min_week_clusters: int = 12) -> dict:
    if min_lead_minutes < 0 or max_lookback_hours < 1 or min_scored < 1 or min_week_clusters < 1:
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
    week_clusters = {
        (kickoff := _aware_utc(row["kickoff"])).date() - timedelta(days=kickoff.weekday())
        for row in rows if _aware_utc(row["kickoff"]) is not None
    }
    enough = len(rows) >= min_scored and len(week_clusters) >= min_week_clusters
    status = "SUFFICIENT_FORWARD_SAMPLE" if enough else "INSUFFICIENT_FORWARD_SAMPLE"
    paired = {}
    for model in ("market", "fixed_blend"):
        paired[model] = {
            metric: _paired_bootstrap(rows, model, metric)
            for metric in ("brier", "log_loss")
        }
        paired[model]["evidence_gate"] = (
            "MINIMUM_GAME_OR_WEEK_SAMPLE_NOT_MET" if not enough else
            "BOTH_METRICS_IMPROVE" if all(
                paired[model][metric]["ci95"][1] < 0 for metric in ("brier", "log_loss")
            ) else "IMPROVEMENT_NOT_ESTABLISHED"
        )
    return {
        "status": status,
        "matched_completed_games": len(rows),
        "min_required_games": min_scored,
        "matched_calendar_weeks": len(week_clusters),
        "min_required_calendar_weeks": min_week_clusters,
        "decision_cutoff_minutes_before_kickoff": min_lead_minutes,
        "max_quote_lookback_hours": max_lookback_hours,
        "elo": _scores([row["elo"] for row in rows], outcomes),
        "market": _scores([row["market"] for row in rows], outcomes),
        "fixed_blend_research_only": _scores([row["fixed_blend"] for row in rows], outcomes),
        "paired_comparisons_vs_elo": paired,
        "research_only": True,
        "buy_publication_eligible": False,
        "limits": ["Snapshots must have been captured before a fixed kickoff cutoff.",
                   "Week-cluster bootstrap intervals quantify sampling uncertainty, not causal proof.",
                   "No execution prices, betting profitability, or BUY eligibility inferred.",
                   "Experimental blend is not fitted, selected, or production-certified."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    schedule = parser.add_mutually_exclusive_group(required=True)
    schedule.add_argument("--games-csv", type=Path)
    schedule.add_argument("--fetch-schedule", action="store_true",
                          help="Fetch the official nflverse schedule once")
    parser.add_argument("--snapshots", type=Path, nargs="+", required=True)
    parser.add_argument("--min-scored", type=int, default=100)
    args = parser.parse_args()
    snapshots = [json.loads(path.read_text(encoding="utf-8")) for path in args.snapshots]
    games = fetch_games() if args.fetch_schedule else parse_games(
        args.games_csv.read_text(encoding="utf-8")
    )
    report = evaluate(games, snapshots, min_scored=args.min_scored)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "SUFFICIENT_FORWARD_SAMPLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
