#!/usr/bin/env python3
"""NBA Phase 1: frozen pre-2026 Elo calibration vs 2026 chronological outcomes.

Fit strictly on 2024/25 history. 2026 finals are an already-inspected diagnostic
period; never select a model, calibrator or threshold from their results.
Research-only: no credential, venue, order, scheduler or production writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model import (
    _apply_calibration, _metrics, _replay, train_artifact,
)

def evaluate(history: Path, current: Path, provenance: Path) -> dict:
    source = history.read_bytes()
    content = current.read_bytes()
    manifest = json.loads(provenance.read_text(encoding="utf-8"))
    digest = hashlib.sha256(content).hexdigest()
    if manifest.get("csv_sha256") != digest or manifest.get("source") != "ESPN_PUBLIC_SCOREBOARD_RESEARCH_ONLY":
        raise ValueError("2026 research snapshot provenance mismatch")
    historic = load_final_games_csv(history)
    recent = load_final_games_csv(current)
    if not historic or not recent:
        raise ValueError("missing historical or current games")
    if any(g.sport != "NBA" for g in historic + recent):
        raise ValueError("NBA records required")
    if any(g.season not in {2024, 2025} for g in historic):
        raise ValueError("2024/25-only historical calibration input required")
    if any(g.season != 2026 or g.game_date < date(2026, 10, 1) for g in recent):
        raise ValueError("2026-only evaluation required")
    if max(g.game_date for g in historic) >= min(g.game_date for g in recent):
        raise ValueError("historical calibration overlaps evaluation")
    keys = [(g.season, g.game_date, g.home_team, g.away_team) for g in historic + recent]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate fixture identity")
    # Existing fitter selects parameters and calibrator exclusively on historic.
    artifact = train_artifact(historic, sport="NBA",
                              source_sha256=hashlib.sha256(source).hexdigest())
    home = float(artifact["elo"]["home_advantage"])
    k = float(artifact["elo"]["k"])
    predictions, _ = _replay([*historic, *recent], home, k)
    ordered = sorted(recent, key=lambda g:(g.game_date, g.game_id))
    raw = [predictions[("NBA", g.game_id)] for g in ordered]
    calibrated = [_apply_calibration(artifact["calibration"], p) for p in raw]
    outcomes = [int(g.home_team == g.winner_team) for g in ordered]
    raw_metrics = _metrics(raw, outcomes)
    calibrated_metrics = _metrics(calibrated, outcomes)
    baseline = _metrics([0.5]*len(ordered), outcomes)
    return {
        "status": "DIAGNOSTIC_ALREADY_INSPECTED_2026_NO_MODEL_SELECTION",
        "historical_games": len(historic),
        "evaluation_games": len(recent),
        "train_calibration_source_sha256": hashlib.sha256(source).hexdigest(),
        "evaluation_source_sha256": digest,
        "historical_fit": {
            "training_end": artifact["splits"]["train_end"],
            "calibration_start": artifact["splits"]["calibration_start"],
            "calibration_end": artifact["splits"]["calibration_end"],
            "previously_inspected_historical_holdout": artifact["splits"]["holdout_end"],
            "elo": artifact["elo"], "calibration": artifact["calibration"],
            "research_artifact_sha256": artifact["artifact_sha256"],
        },
        "evaluation_start": min(g.game_date for g in recent).isoformat(),
        "evaluation_end": max(g.game_date for g in recent).isoformat(),
        "raw_fitted_elo": raw_metrics,
        "calibrated_fitted_elo": calibrated_metrics,
        "baseline_50_50": baseline,
        "calibration_delta_brier": calibrated_metrics["brier"]-raw_metrics["brier"],
        "calibration_delta_ece": calibrated_metrics["ece_10_bins"]-raw_metrics["ece_10_bins"],
        "calibration_selected_on_2026": False,
        "same_date_result_leakage": False,
        "independent_results_verified": False,
        "model_certified": False,
        "buy_authorized": False,
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.history, args.current,
                      args.current.with_suffix(".provenance.json"))
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
