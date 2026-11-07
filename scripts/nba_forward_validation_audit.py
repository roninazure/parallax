#!/usr/bin/env python3
"""Read-only prospective NBA Elo validation; NEVER grants certification or BUY.

The evaluated cohort begins on 2026-11-08 ET, after this protocol was frozen.
Input is an independently obtained, authorized final-results CSV; this script
does not fetch data, select models, alter artifacts or publish signals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from parallax.league_elo_experimental import BASE_RATING, load_final_games_csv
from parallax.league_model import (
    _apply_calibration, _metrics, _replay, verify_artifact,
    verify_history_snapshot,
)

START = date(2026, 11, 8)
EXPECTED_ARTIFACT_SHA = "2f86382704b76750a9e45440bf2204b1ff5f27671ca2f705e9d51cc30dfaabe1"
ECE_CEILING = 0.06007751937984496
ET = ZoneInfo("America/New_York")


def evaluate(history_path: Path, artifact_path: Path, forward_path: Path, *, now: datetime) -> dict:
    if any(path.is_symlink() for path in (history_path, artifact_path, forward_path)):
        raise ValueError("symlink inputs are prohibited")
    source_sha = hashlib.sha256(history_path.read_bytes()).hexdigest()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    if artifact.get("artifact_sha256") != EXPECTED_ARTIFACT_SHA or artifact.get("source_sha256") != source_sha:
        raise ValueError("frozen NBA model or history does not match protocol")
    verify_artifact(artifact, sport="NBA", now=datetime.fromisoformat(artifact["generated_at"]), require_certified=False)
    history = load_final_games_csv(history_path)
    verify_history_snapshot(history, artifact, sport="NBA")
    if artifact.get("trained_through") != "2026-04-12" or artifact.get("certified") is not False:
        raise ValueError("unexpected model generation or certification state")
    forward_sha = hashlib.sha256(forward_path.read_bytes()).hexdigest()
    forward = load_final_games_csv(forward_path)
    if hashlib.sha256(forward_path.read_bytes()).hexdigest() != forward_sha:
        raise ValueError("forward file changed during parsing")
    if not forward:
        raise ValueError("no independent forward final results")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cutoff = now.astimezone(ET).date()
    if any(g.sport != "NBA" or g.season != 2026 or g.game_date < START
           or g.game_date >= cutoff for g in forward):
        raise ValueError("forward rows must be completed NBA 2026-27 games after protocol start")
    if set(g.game_id for g in forward) & set(g.game_id for g in history):
        raise ValueError("forward source duplicates historical game IDs")

    hp, k = float(artifact["elo"]["home_advantage"]), float(artifact["elo"]["k"])
    _, historical_ratings = _replay(history, hp, k)
    ratings = dict(historical_ratings)
    ps, ys = [], []
    index = 0
    while index < len(forward):
        end = index + 1
        while end < len(forward) and forward[end].game_date == forward[index].game_date:
            end += 1
        changes = {}
        for game in forward[index:end]:
            home = ratings.get(("NBA", game.home_team), BASE_RATING)
            away = ratings.get(("NBA", game.away_team), BASE_RATING)
            raw = 1 / (1 + 10 ** (-((home + hp - away) / 400)))
            ps.append(_apply_calibration(artifact["calibration"], raw))
            outcome = int(game.winner_team == game.home_team)
            ys.append(outcome)
            delta = k * (outcome - raw)
            hk, ak = ("NBA", game.home_team), ("NBA", game.away_team)
            changes[hk] = changes.get(hk, 0.0) + delta
            changes[ak] = changes.get(ak, 0.0) - delta
        for key, delta in changes.items():
            ratings[key] = ratings.get(key, BASE_RATING) + delta
        index = end
    metrics, neutral = _metrics(ps, ys), _metrics([0.5] * len(ys), ys)
    return {
        "status": "FORWARD_RESEARCH_ONLY_NOT_CERTIFIED",
        "frozen_artifact_sha256": EXPECTED_ARTIFACT_SHA,
        "history_sha256": source_sha,
        "forward_sha256": forward_sha,
        "cohort_start": START.isoformat(),
        "first_game_date": forward[0].game_date.isoformat(),
        "last_game_date": forward[-1].game_date.isoformat(),
        "metrics": metrics,
        "baseline_50_50": neutral,
        "ece_ceiling": ECE_CEILING,
        "metric_gates": {
            "beats_half_brier": metrics["brier"] < neutral["brier"],
            "beats_half_log_loss": metrics["log_loss"] < neutral["log_loss"],
            "ece_under_frozen_ceiling": metrics["ece_10_bins"] <= ECE_CEILING,
        },
        "independent_source_verified": False,
        "source_authorization_documented": False,
        "model_owner_approval_documented": False,
        "model_certified": False,
        "buy_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--forward", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.history, args.baseline, args.forward, now=datetime.now(UTC)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
