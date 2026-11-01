"""NBA Elo calibration research candidate using TRAIN and CALIBRATION only.

This module never loads, scores, selects on, or certifies a forward holdout.
Selection is a single chronological, ET-date-isolated tail of calibration;
after selection, refitting on calibration is explicitly not independent proof.
No network, venue, production, or publishing imports.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from .league_elo_experimental import FinalGame, _prepare
from .league_model import (
    _apply_calibration,
    _fit_platt,
    _metrics,
    _replay,
)

MIN_TRAIN_DATES = 20
MIN_CALIBRATION_DATES = 20
MIN_FIT_DATES = 10
MIN_VALIDATION_DATES = 5
FIT_FRACTION = 0.70
RECENT_FRACTION = 0.60


def _platt(rows: list[FinalGame], probabilities: dict[tuple[str, str], float]) -> dict[str, Any]:
    ps = [probabilities[("NBA", g.game_id)] for g in rows]
    ys = [int(g.winner_team == g.home_team) for g in rows]
    intercept, slope = _fit_platt(ps, ys)
    return {"method": "platt_logistic", "intercept": intercept, "slope": slope}


def evaluate_nba_elo_candidate(
    train: Iterable[FinalGame],
    calibration: Iterable[FinalGame],
    *,
    train_end: date,
    calibration_end: date,
    home_advantage: float,
    k: float,
) -> dict[str, Any]:
    """Compare raw Elo, all-window Platt, recent-window Platt on a CAL-only tail.

    The caller supplies disjoint TRAIN and CAL snapshots and immutable date
    boundaries. Any row outside those boundaries is rejected, not filtered.
    Historical Elo hyperparameters must have been chosen on TRAIN alone.
    """
    import math

    if not (math.isfinite(home_advantage) and math.isfinite(k) and k > 0):
        raise ValueError("finite Elo parameters and positive K required")
    if not train_end < calibration_end:
        raise ValueError("train/calibration date boundaries overlap")
    train_rows, cal_rows = _prepare(train), _prepare(calibration)
    if not train_rows or not cal_rows:
        raise ValueError("both TRAIN and CALIBRATION snapshots are required")
    if any(g.sport != "NBA" or g.game_date > train_end for g in train_rows):
        raise ValueError("TRAIN contains non-NBA or post-train-cutoff games")
    if any(
        g.sport != "NBA" or not train_end < g.game_date <= calibration_end
        for g in cal_rows
    ):
        raise ValueError("CALIBRATION contains non-NBA or out-of-window games")
    dates_train = sorted({g.game_date for g in train_rows})
    dates_cal = sorted({g.game_date for g in cal_rows})
    if len(dates_train) < MIN_TRAIN_DATES or len(dates_cal) < MIN_CALIBRATION_DATES:
        raise ValueError("insufficient distinct TRAIN/CALIBRATION dates")
    # Also reject duplicate IDs spanning the two separately supplied files.
    rows = _prepare([*train_rows, *cal_rows])
    forecasts, _ = _replay(rows, home_advantage, k)

    split = int(len(dates_cal) * FIT_FRACTION)
    fit_dates, validation_dates = dates_cal[:split], dates_cal[split:]
    if len(fit_dates) < MIN_FIT_DATES or len(validation_dates) < MIN_VALIDATION_DATES:
        raise ValueError("insufficient date-isolated calibration fit/validation dates")
    fit_rows = [g for g in cal_rows if g.game_date <= fit_dates[-1]]
    validation_rows = [g for g in cal_rows if g.game_date >= validation_dates[0]]
    if len({g.winner_team == g.home_team for g in fit_rows}) != 2:
        raise ValueError("calibration fit requires both outcomes")

    # These are model-selection comparisons, NOT independent holdout metrics.
    models: dict[str, dict[str, Any] | None] = {"elo_raw": None}
    models["platt_all"] = _platt(fit_rows, forecasts)
    recent_start = fit_dates[-max(MIN_FIT_DATES, int(len(fit_dates) * RECENT_FRACTION))]
    recent_rows = [g for g in fit_rows if g.game_date >= recent_start]
    if len({g.winner_team == g.home_team for g in recent_rows}) == 2:
        try:
            models["platt_recent"] = _platt(recent_rows, forecasts)
        except ValueError:
            pass  # The smaller window cannot safely fit a monotone Platt map.

    ys = [int(g.winner_team == g.home_team) for g in validation_rows]
    comparison: dict[str, dict[str, float | int]] = {}
    for name, model in models.items():
        ps = [
            forecasts[("NBA", g.game_id)] if model is None
            else _apply_calibration(model, forecasts[("NBA", g.game_id)])
            for g in validation_rows
        ]
        comparison[name] = _metrics(ps, ys)
    baseline = _metrics([0.5] * len(ys), ys)
    selected = min(comparison, key=lambda name: (comparison[name]["log_loss"], name))

    # Refit only AFTER selection; its coefficients are research parameters,
    # not independent validation and never a certified model artifact.
    refit: dict[str, Any] | None = None
    if selected != "elo_raw":
        refit_rows = cal_rows
        if selected == "platt_recent":
            start = dates_cal[-max(MIN_FIT_DATES, int(len(dates_cal) * RECENT_FRACTION))]
            refit_rows = [g for g in cal_rows if g.game_date >= start]
        refit = _platt(refit_rows, forecasts)

    return {
        "status": "RESEARCH_ONLY_UNCERTIFIED",
        "league": "NBA",
        "model": "Elo with optional time-aware Platt recalibration",
        "selection_method": "calibration-tail chronological log_loss",
        "train_end": train_end.isoformat(),
        "calibration_end": calibration_end.isoformat(),
        "train_dates": len(dates_train),
        "calibration_dates": len(dates_cal),
        "fit_end": fit_dates[-1].isoformat(),
        "validation_start": validation_dates[0].isoformat(),
        "fit_games": len(fit_rows),
        "validation_games": len(validation_rows),
        "candidate_validation": comparison,
        "baseline_50_50_validation": baseline,
        "selected_family": selected,
        "calibration_only_refit": refit,
        "forward_holdout_evaluated": False,
        "certified": False,
    }
