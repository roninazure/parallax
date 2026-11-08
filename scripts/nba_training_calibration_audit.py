#!/usr/bin/env python3
"""NBA training/calibration-only Platt audit; never touches the holdout outcomes.

Research only: no model writes, external requests, certification or BUY authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from parallax.league_elo_experimental import load_final_games_csv
from parallax.league_model import (
    _apply_calibration, _fit_isotonic, _fit_platt, _logit, _metrics,
    _nba_calibration_date_cut, _replay, _sigmoid, verify_artifact,
    verify_history_snapshot,
)


def audit(history_path: Path, baseline_path: Path) -> dict:
    if history_path.is_symlink() or baseline_path.is_symlink():
        raise ValueError("symlinks are not permitted")
    source_sha = hashlib.sha256(history_path.read_bytes()).hexdigest()
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("sport") != "NBA" or baseline.get("source_sha256") != source_sha:
        raise ValueError("NBA frozen source mismatch")
    verify_artifact(baseline, sport="NBA", now=datetime.fromisoformat(baseline["generated_at"]), require_certified=False)
    rows = load_final_games_csv(history_path)
    if hashlib.sha256(history_path.read_bytes()).hexdigest() != source_sha:
        raise ValueError("source changed while parsing")
    verify_history_snapshot(rows, baseline, sport="NBA")
    if baseline.get("certified") is not False:
        raise ValueError("expected uncertified baseline")
    split = baseline["splits"]
    train = [g for g in rows if g.game_date.isoformat() <= split["train_end"]]
    cal = [g for g in rows if split["calibration_start"] <= g.game_date.isoformat() <= split["calibration_end"]]
    if len(train) != split["train_games"] or len(cal) != split["calibration_games"]:
        raise ValueError("train/calibration population mismatch")
    if train[-1].game_date >= cal[0].game_date:
        raise ValueError("training/calibration date overlap")
    # Deliberately replay train+cal only; do not include any holdout games.
    predictions, _ = _replay(train + cal, float(baseline["elo"]["home_advantage"]), float(baseline["elo"]["k"]))
    ps = [predictions[("NBA", game.game_id)] for game in cal]
    ys = [int(game.winner_team == game.home_team) for game in cal]
    cut = _nba_calibration_date_cut([g.game_date for g in cal])
    fit_ps, fit_ys = ps[:cut], ys[:cut]
    val_ps, val_ys = ps[cut:], ys[cut:]
    options = {"raw_elo": _metrics(val_ps, val_ys)}
    a, b = _fit_platt(fit_ps, fit_ys)
    options["platt_logistic"] = _metrics([_sigmoid(a + b * _logit(p)) for p in val_ps], val_ys)
    knots = _fit_isotonic(fit_ps, fit_ys)
    options["isotonic"] = _metrics([_apply_calibration({"method": "isotonic", "knots": knots}, p) for p in val_ps], val_ys)
    # Refit only on the original calibration population to check artifact parameters.
    full_a, full_b = _fit_platt(ps, ys)
    frozen = baseline["calibration"]
    if frozen.get("method") != "platt_logistic" or abs(full_a - frozen["intercept"]) > 1e-9 or abs(full_b - frozen["slope"]) > 1e-9:
        raise ValueError("frozen Platt fit does not reproduce from calibration data")
    selection = min((options[name]["log_loss"], name) for name in ("platt_logistic", "isotonic"))[1]
    if selection != frozen["method"]:
        raise ValueError("internal method selection does not reproduce")
    return {
        "status": "TRAINING_CALIBRATION_ONLY_NO_HOLDOUT_SCORING",
        "source_sha256": source_sha,
        "train_games": len(train),
        "calibration_fit_games": cut,
        "internal_validation_games": len(val_ps),
        "calibration_fit_end": cal[cut - 1].game_date.isoformat(),
        "internal_validation_start": cal[cut].game_date.isoformat(),
        "internal_validation_end": cal[-1].game_date.isoformat(),
        "internal_validation_metrics": options,
        "selected_method": selection,
        "internal_platt_intercept": a,
        "internal_platt_slope": b,
        "full_calibration_platt_intercept": full_a,
        "full_calibration_platt_slope": full_b,
        "holdout_scored": False,
        "buy_authorized": False,
        "model_certified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.history, args.baseline), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
