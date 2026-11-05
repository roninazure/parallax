#!/usr/bin/env python3
"""Research-only chronological 2026 NBA Elo replay from immutable finals.

Uses only prior ET-date outcomes for a forecast. Does not update production
models, BUY eligibility, publication or scheduled scans.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from parallax.league_elo_experimental import holdout_metrics, load_final_games_csv, replay

def evaluate(history: Path, current: Path, provenance: Path) -> dict:
    manifest = json.loads(provenance.read_text(encoding="utf-8"))
    observed = hashlib.sha256(current.read_bytes()).hexdigest()
    if manifest.get("csv_sha256") != observed or manifest.get("source") != "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY":
        raise ValueError("Research finals provenance missing or hash mismatch")
    history_rows = load_final_games_csv(history)
    current_rows = load_final_games_csv(current)
    if not history_rows or not current_rows:
        raise ValueError("Both historical and current-season files must contain games")
    if any(g.sport != "NBA" for g in history_rows + current_rows):
        raise ValueError("NBA-only replay")
    if any(g.season != 2026 or g.game_date < date(2026,10,1) for g in current_rows):
        raise ValueError("Expected 2026 NBA regular-season finals")
    historical_end = max(g.game_date for g in history_rows)
    current_start = min(g.game_date for g in current_rows)
    if historical_end >= current_start:
        raise ValueError("History overlaps current-season replay")
    keys = [(g.season, g.game_date, g.home_team, g.away_team) for g in history_rows + current_rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate fixtures across combined histories")
    predictions = replay([*history_rows, *current_rows])
    score = holdout_metrics(predictions, sport="NBA", season=2026)
    if score.n_games != len(current_rows):
        raise ValueError("Replay did not evaluate all 2026 results")
    return {"status": "RESEARCH_ONLY_NOT_CERTIFICATION", "source_sha256": observed,
            "history_games": len(history_rows), "current_games": len(current_rows),
            "first_current_day": current_start.isoformat(),
            "last_current_day": max(g.game_date for g in current_rows).isoformat(),
            "n_games": score.n_games, "brier": score.brier,
            "log_loss": score.log_loss, "ece_10_bins": score.ece_10_bins,
            "same_day_results_excluded": True,
            "model_changed": False, "buy_authorized": False,
            "independent_source_verified": False}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.history, args.current,
                              args.current.with_suffix(".provenance.json")),
                     indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
