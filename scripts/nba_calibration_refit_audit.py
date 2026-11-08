#!/usr/bin/env python3
"""Read-only NBA historical recalibration comparison against a frozen artifact.

This is an exploratory comparison on an ALREADY INSPECTED holdout. It cannot
certify a model, authorize BUY, write a model artifact, or change any lane.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model import (
    train_artifact, verify_artifact, verify_history_snapshot,
)

METRICS = ("brier", "log_loss", "ece_10_bins")


def compare(history_path: Path, baseline_path: Path) -> dict:
    if history_path.is_symlink() or baseline_path.is_symlink():
        raise ValueError("symlinks are not permitted for NBA audit inputs")
    raw = history_path.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    baseline_bytes = baseline_path.read_bytes()
    baseline = json.loads(baseline_bytes)
    if not isinstance(baseline, dict) or baseline.get("sport") != "NBA":
        raise ValueError("expected an NBA baseline artifact")
    if baseline.get("source_sha256") != source_sha:
        raise ValueError("NBA frozen baseline source digest mismatch")
    generated = datetime.fromisoformat(baseline["generated_at"])
    verify_artifact(baseline, sport="NBA", now=generated,
                    require_certified=False)
    rows = load_final_games_csv(history_path)
    if hashlib.sha256(history_path.read_bytes()).hexdigest() != source_sha:
        raise ValueError("NBA history changed during parsing")
    verify_history_snapshot(rows, baseline, sport="NBA")
    if baseline.get("certified") is not False:
        raise ValueError("expected previously unapproved NBA candidate")
    updated = train_artifact(
        rows, sport="NBA", source_sha256=source_sha, generated_at=generated
    )
    if (updated["source_game_count"] != baseline["source_game_count"]
            or updated["splits"] != baseline["splits"]
            or updated["elo"] != baseline["elo"]):
        raise ValueError("comparison changed data population, split or Elo fit")
    verify_artifact(updated, sport="NBA", now=generated,
                    require_certified=False)
    prior, next_metrics = baseline["holdout_metrics"], updated["holdout_metrics"]
    changes = {
        metric: {
            "before": prior[metric],
            "after": next_metrics[metric],
            "delta": next_metrics[metric] - prior[metric],
        }
        for metric in METRICS
    }
    return {
        "status": "EXPLORATORY_RECALIBRATION_COMPARISON_ONLY",
        "source_sha256": source_sha,
        "source_games": len(rows),
        "holdout_games": updated["splits"]["holdout_games"],
        "holdout_status": "PREVIOUSLY_INSPECTED_NOT_INDEPENDENT",
        "method_before": baseline["calibration"]["method"],
        "method_after": updated["calibration"]["method"],
        "model_artifact_sha256_before": baseline["artifact_sha256"],
        "research_candidate_sha256_after": updated["artifact_sha256"],
        "metric_changes": changes,
        "baseline_50_50": updated["baseline_50_50"],
        "acceptance_gates": updated["acceptance_gates"],
        "model_certified": False,
        "independent_results_verified": False,
        "forward_holdout_fresh": False,
        "publication_eligible": False,
        "buy_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.history, args.baseline)
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
